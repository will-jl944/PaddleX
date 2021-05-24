# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import os.path as osp
from functools import partial
import time
import copy
import math
import yaml
import json
import paddle
from paddle.io import DataLoader, DistributedBatchSampler
from paddle.jit import to_static
from paddleslim.analysis import flops
from paddleslim import L1NormFilterPruner, FPGMFilterPruner
import paddlex
from paddlex.cv.transforms import arrange_transforms
from paddlex.utils import (seconds_to_hms, get_single_card_bs, dict2str,
                           get_pretrain_weights, load_pretrain_weights,
                           SmoothedValue, TrainingStats,
                           _get_shared_memory_size_in_M, EarlyStop)
import paddlex.utils.logging as logging
from .slim.prune import _pruner_eval_fn, _pruner_template_input, sensitive_prune


class BaseModel:
    def __init__(self, model_type):
        self.model_type = model_type
        self.num_classes = None
        self.labels = None
        self.version = paddlex.__version__
        self.net = None
        self.optimizer = None
        self.test_inputs = None
        self.train_data_loader = None
        self.eval_data_loader = None
        self.eval_metrics = None
        # 是否使用多卡间同步BatchNorm均值和方差
        self.sync_bn = False
        self.status = 'Normal'
        # 已完成迭代轮数，为恢复训练时的起始轮数
        self.completed_epochs = 0
        self.pruner = None
        self.pruning_ratios = None

    def net_initialize(self, pretrain_weights=None, save_dir='.'):
        if pretrain_weights is not None and \
                not os.path.exists(pretrain_weights):
            if not os.path.isdir(save_dir):
                if os.path.exists(save_dir):
                    os.remove(save_dir)
                os.makedirs(save_dir)
            if self.model_type == 'classifier':
                scale = getattr(self, 'scale', None)
                pretrain_weights = get_pretrain_weights(
                    pretrain_weights,
                    self.__class__.__name__,
                    save_dir,
                    scale=scale)
            else:
                backbone_name = getattr(self, 'backbone_name', None)
                pretrain_weights = get_pretrain_weights(
                    pretrain_weights,
                    self.__class__.__name__,
                    save_dir,
                    backbone_name=backbone_name)
        if pretrain_weights is not None:
            load_pretrain_weights(
                self.net, pretrain_weights, model_name=self.model_name)

    def get_model_info(self):
        info = dict()
        info['version'] = paddlex.__version__
        info['Model'] = self.__class__.__name__
        info['_Attributes'] = {'model_type': self.model_type}
        if 'self' in self.init_params:
            del self.init_params['self']
        if '__class__' in self.init_params:
            del self.init_params['__class__']
        if 'model_name' in self.init_params:
            del self.init_params['model_name']
        if 'params' in self.init_params:
            del self.init_params['params']

        info['_init_params'] = self.init_params

        info['_Attributes']['num_classes'] = self.num_classes
        info['_Attributes']['labels'] = self.labels

        try:
            primary_metric_key = list(self.eval_metrics.keys())[0]
            primary_metric_value = float(self.eval_metrics[primary_metric_key])
            info['_Attributes']['eval_metrics'] = {
                primary_metric_key: primary_metric_value
            }
        except:
            pass

        if hasattr(self, 'test_transforms'):
            if self.test_transforms is not None:
                info['Transforms'] = list()
                for op in self.test_transforms.transforms:
                    name = op.__class__.__name__
                    if name.startswith('Arrange'):
                        continue
                    attr = op.__dict__
                    info['Transforms'].append({name: attr})
        info['completed_epochs'] = self.completed_epochs
        return info

    def get_pruning_info(self):
        info = dict()
        info['pruner'] = self.pruner.__class__.__name__
        info['pruning_ratios'] = self.pruning_ratios
        info['pruner_inputs'] = self.pruner.inputs
        return info

    def save_model(self, save_dir):
        if not osp.isdir(save_dir):
            if osp.exists(save_dir):
                os.remove(save_dir)
            os.makedirs(save_dir)
        model_info = self.get_model_info()
        model_info['status'] = self.status
        paddle.save(self.net.state_dict(),
                    os.path.join(save_dir, 'model.pdparams'))
        paddle.save(self.optimizer.state_dict(),
                    os.path.join(save_dir, 'model.pdopt'))

        with open(
                osp.join(save_dir, 'model.yml'), encoding='utf-8',
                mode='w') as f:
            yaml.dump(model_info, f)

        # 评估结果保存
        if hasattr(self, 'eval_details'):
            with open(osp.join(save_dir, 'eval_details.json'), 'w') as f:
                json.dump(self.eval_details, f)

        if self.status == 'Pruned' and self.pruner is not None:
            pruning_info = self.get_pruning_info()
            with open(
                    osp.join(save_dir, 'prune.yml'), encoding='utf-8',
                    mode='w') as f:
                yaml.dump(pruning_info, f)

        # 模型保存成功的标志
        open(osp.join(save_dir, '.success'), 'w').close()
        logging.info("Model saved in {}.".format(save_dir))

    def build_data_loader(self, dataset, batch_size, mode='train'):
        batch_size_each_card = get_single_card_bs(batch_size=batch_size)
        if mode == 'eval':
            batch_size = batch_size_each_card
            total_steps = math.ceil(dataset.num_samples * 1.0 / batch_size)
            logging.info(
                "Start to evaluate(total_samples={}, total_steps={})...".
                format(dataset.num_samples, total_steps))
        if dataset.num_samples < batch_size:
            raise Exception(
                'The volume of datset({}) must be larger than batch size({}).'
                .format(dataset.num_samples, batch_size))

        # TODO detection eval阶段需做判断
        batch_sampler = DistributedBatchSampler(
            dataset,
            batch_size=batch_size_each_card,
            shuffle=dataset.shuffle,
            drop_last=mode == 'train')

        shm_size = _get_shared_memory_size_in_M()
        if shm_size is None or shm_size < 1024.:
            use_shared_memory = False
        else:
            use_shared_memory = True

        loader = DataLoader(
            dataset,
            batch_sampler=batch_sampler,
            collate_fn=dataset.batch_transforms,
            num_workers=dataset.num_workers,
            return_list=True,
            use_shared_memory=use_shared_memory)

        return loader

    def train_loop(self,
                   num_epochs,
                   train_dataset,
                   train_batch_size,
                   eval_dataset=None,
                   save_interval_epochs=1,
                   log_interval_steps=10,
                   save_dir='output',
                   early_stop=False,
                   early_stop_patience=5,
                   use_vdl=True):
        arrange_transforms(
            model_type=self.model_type,
            transforms=train_dataset.transforms,
            mode='train')

        nranks = paddle.distributed.get_world_size()
        local_rank = paddle.distributed.get_rank()
        if nranks > 1:
            # Initialize parallel environment if not done.
            if not paddle.distributed.parallel.parallel_helper._is_parallel_ctx_initialized(
            ):
                paddle.distributed.init_parallel_env()
                ddp_net = paddle.DataParallel(self.net)
            else:
                ddp_net = paddle.DataParallel(self.net)

        if use_vdl:
            from visualdl import LogWriter
            vdl_logdir = osp.join(save_dir, 'vdl_log')
            log_writer = LogWriter(vdl_logdir)
        # task_id: 目前由PaddleX GUI赋值
        # 用于在VisualDL日志中注明所属任务id
        task_id = getattr(paddlex, "task_id", "")

        thresh = .0001
        if early_stop:
            earlystop = EarlyStop(early_stop_patience, thresh)

        self.train_data_loader = self.build_data_loader(
            train_dataset, batch_size=train_batch_size, mode='train')

        if eval_dataset is not None:
            self.test_transforms = copy.deepcopy(eval_dataset.transforms)

        start_epoch = self.completed_epochs
        train_step_time = SmoothedValue(log_interval_steps)
        train_step_each_epoch = math.floor(train_dataset.num_samples /
                                           train_batch_size)
        train_total_step = train_step_each_epoch * (num_epochs - start_epoch)
        if eval_dataset is not None:
            eval_batch_size = train_batch_size
            eval_epoch_time = 0

        best_accuracy_key = ""
        best_accuracy = -1.0
        best_model_epoch = -1
        current_step = 0
        for i in range(start_epoch, num_epochs):
            self.net.train()
            if callable(
                    getattr(self.train_data_loader.dataset, 'set_epoch',
                            None)):
                self.train_data_loader.dataset.set_epoch(i)
            train_avg_metrics = TrainingStats()
            step_time_tic = time.time()

            for step, data in enumerate(self.train_data_loader()):
                if nranks > 1:
                    outputs = self.run(ddp_net, data, mode='train')
                else:
                    outputs = self.run(self.net, data, mode='train')
                loss = outputs['loss']
                loss.backward()
                self.optimizer.step()
                self.optimizer.clear_grad()
                lr = self.optimizer.get_lr()
                if isinstance(self.optimizer._learning_rate,
                              paddle.optimizer.lr.LRScheduler):
                    self.optimizer._learning_rate.step()

                train_avg_metrics.update(outputs)
                outputs['lr'] = lr
                step_time_toc = time.time()
                train_step_time.update(step_time_toc - step_time_tic)
                step_time_tic = step_time_toc
                current_step += 1

                # 每间隔log_interval_steps，输出loss信息
                if current_step % log_interval_steps == 0 and local_rank == 0:
                    if use_vdl:
                        for k, v in outputs.items():
                            log_writer.add_scalar(
                                '{}-Metrics/Training(Step): {}'.format(
                                    task_id, k), v, current_step)

                    # 估算剩余时间
                    avg_step_time = train_step_time.avg()
                    eta = avg_step_time * (train_total_step - current_step)
                    if eval_dataset is not None:
                        eval_num_epochs = math.ceil(
                            (num_epochs - i - 1) / save_interval_epochs)
                        if eval_epoch_time == 0:
                            eta += avg_step_time * math.ceil(
                                eval_dataset.num_samples / eval_batch_size)
                        else:
                            eta += eval_epoch_time * eval_num_epochs

                    logging.info(
                        "[TRAIN] Epoch={}/{}, Step={}/{}, {}, time_each_step={}s, eta={}"
                        .format(i + 1, num_epochs, step + 1,
                                train_step_each_epoch,
                                dict2str(outputs),
                                round(avg_step_time, 2), seconds_to_hms(eta)))

            logging.info('[TRAIN] Epoch {} finished, {} .'
                         .format(i + 1, train_avg_metrics.log()))
            self.completed_epochs += 1

            # 每间隔save_interval_epochs, 在验证集上评估和对模型进行保存
            eval_epoch_tic = time.time()
            if (i + 1) % save_interval_epochs == 0 or i == num_epochs - 1:
                if eval_dataset is not None and eval_dataset.num_samples > 0:
                    self.eval_metrics = self.evaluate(
                        eval_dataset,
                        batch_size=eval_batch_size,
                        return_details=False)
                    # 保存最优模型
                    if local_rank == 0:
                        logging.info('[EVAL] Finished, Epoch={}, {} .'.format(
                            i + 1, dict2str(self.eval_metrics)))
                        best_accuracy_key = list(self.eval_metrics.keys())[0]
                        current_accuracy = self.eval_metrics[best_accuracy_key]
                        if current_accuracy > best_accuracy:
                            best_accuracy = current_accuracy
                            best_model_epoch = i + 1
                            best_model_dir = osp.join(save_dir, "best_model")
                            self.save_model(save_dir=best_model_dir)
                        if best_model_epoch > 0:
                            logging.info(
                                'Current evaluated best model in eval_dataset is epoch_{}, {}={}'
                                .format(best_model_epoch, best_accuracy_key,
                                        best_accuracy))
                    eval_epoch_time = time.time() - eval_epoch_tic

                current_save_dir = osp.join(save_dir, "epoch_{}".format(i + 1))
                if local_rank == 0:
                    self.save_model(save_dir=current_save_dir)

                    if eval_dataset is not None and early_stop:
                        if earlystop(current_accuracy):
                            break

    def analyze_sensitivity(self,
                            dataset,
                            batch_size=8,
                            criterion='l1_norm',
                            save_dir='output'):
        """

        Args:
            dataset(paddlex.dataset): Dataset used for evaluation during sensitivity analysis.
            batch_size(int, optional): Batch size used in evaluation. Defaults to 8.
            criterion({'l1_norm', 'fpgm'}, optional): Pruning criterion. Defaults to 'l1_norm'.
            save_dir(str, optional): The directory to save sensitivity file of the model. Defaults to 'output'.

        """
        if self.__class__.__name__ in ['FasterRCNN', 'MaskRCNN']:
            raise Exception("{} does not support pruning currently!".format(
                self.__class__.__name__))

        assert criterion in ['l1_norm', 'fpgm'], \
            "Pruning criterion {} is not supported. Please choose from ['l1_norm', 'fpgm']"
        arrange_transforms(
            model_type=self.model_type,
            transforms=dataset.transforms,
            mode='eval')
        if self.model_type == 'detector':
            self.net.eval()
        else:
            self.net.train()
        inputs = _pruner_template_input(
            sample=dataset[0], model_type=self.model_type)
        if criterion == 'l1_norm':
            self.pruner = L1NormFilterPruner(self.net, inputs=inputs)
        else:
            self.pruner = FPGMFilterPruner(self.net, inputs=inputs)

        if not osp.isdir(save_dir):
            os.makedirs(save_dir)
        sen_file = osp.join(save_dir, 'model.sensi.data')
        logging.info('Sensitivity analysis of model parameters starts...')
        self.pruner.sensitive(
            eval_func=partial(_pruner_eval_fn, self, dataset, batch_size),
            sen_file=sen_file)
        logging.info(
            'Sensitivity analysis is complete. The result is saved at {}.'.
            format(sen_file))

    def prune(self, pruned_flops, save_dir=None):
        """

        Args:
            pruned_flops(float): Ratio of FLOPs to be pruned.
            save_dir(None or str, optional): If None, the pruned model will not be saved
            Otherwise, the pruned model will be saved at save_dir. Defaults to None.

        """
        if self.status == "Pruned":
            raise Exception(
                "A pruned model cannot be done model pruning again!")
        pre_pruning_flops = flops(self.net, self.pruner.inputs)
        logging.info("Pre-pruning FLOPs: {}. Pruning starts...".format(
            pre_pruning_flops))
        skip_vars = []
        for param in self.net.parameters():
            if param.shape[0] <= 8:
                skip_vars.append(param.name)
        _, self.pruning_ratios = sensitive_prune(self.pruner, pruned_flops,
                                                 skip_vars)
        post_pruning_flops = flops(self.net, self.pruner.inputs)
        logging.info("Pruning is complete. Post-pruning FLOPs: {}".format(
            post_pruning_flops))
        logging.warning("Pruning the model may hurt its performance, "
                        "retraining is highly recommended")
        self.status = 'Pruned'

        if save_dir is not None:
            self.save_model(save_dir)
            logging.info("Pruned model is saved at {}".format(save_dir))

    def _export_inference_model(self, save_dir, image_shape=[-1, -1]):
        save_dir = osp.join(save_dir, 'inference_model')
        self.net.eval()
        self.test_inputs = self.get_test_inputs(image_shape)
        static_net = paddle.jit.to_static(
            self.net, input_spec=self.test_inputs)
        paddle.jit.save(static_net, osp.join(save_dir, 'model'))

        if self.status == 'Pruned':
            pruning_info = self.get_pruning_info()
            with open(
                    osp.join(save_dir, 'prune.yml'), encoding='utf-8',
                    mode='w') as f:
                yaml.dump(pruning_info, f)

        model_info = self.get_model_info()
        model_info['status'] = 'Infer'
        with open(
                osp.join(save_dir, 'model.yml'), encoding='utf-8',
                mode='w') as f:
            yaml.dump(model_info, f)

        # 模型保存成功的标志
        open(osp.join(save_dir, '.success'), 'w').close()
        logging.info("The model for the inference deployment is saved in {}.".
                     format(save_dir))
