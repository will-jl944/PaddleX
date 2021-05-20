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

from __future__ import absolute_import
import os.path as osp
from collections import OrderedDict
import numpy as np
import paddle
from paddle import to_tensor
import paddle.nn.functional as F
from paddle.static import InputSpec
from paddlex.utils import logging, TrainingStats
from paddlex.cv.models.base import BaseModel
from PaddleClas.ppcls.modeling import architectures
from PaddleClas.ppcls.modeling.loss import CELoss
from paddlex.cv.transforms import arrange_transforms

__all__ = [
    "ResNet18", "ResNet34", "ResNet50", "ResNet101", "ResNet152",
    "ResNet18_vd", "ResNet34_vd", "ResNet50_vd", "ResNet50_vd_ssld",
    "ResNet101_vd", "ResNet101_vd_ssld", "ResNet152_vd", "ResNet200_vd",
    "AlexNet", "DarkNet53", "MobileNetV1", "MobileNetV2", "MobileNetV3_small",
    "MobileNetV3_large", "DenseNet121", "DenseNet161", "DenseNet169",
    "DenseNet201", "DenseNet264", "HRNet_W18_C", "HRNet_W30_C", "HRNet_W32_C",
    "HRNet_W40_C", "HRNet_W44_C", "HRNet_W48_C", "HRNet_W64_C", "Xception41",
    "Xception65", "Xception71", "ShuffleNetV2", "ShuffleNetV2_swish"
]


class BaseClassifier(BaseModel):
    """Parent class of all classification models.
    Args:
        model_name (str, optional): Name of classification model. Defaults to 'ResNet50'.
        num_classes (int, optional): The number of target classes. Defaults to 1000.
    """

    def __init__(self, model_name='ResNet50', num_classes=1000, **params):
        self.init_params = locals()
        self.init_params.update(params)
        del self.init_params['params']
        super(BaseClassifier, self).__init__('classifier')
        if not hasattr(architectures, model_name):
            raise Exception("ERROR: There's no model named {}.".format(
                model_name))

        self.model_name = model_name
        self.labels = None
        self.num_classes = num_classes
        for k, v in params.items():
            setattr(self, k, v)
        self.net = self.build_net(**params)

    def build_net(self, **params):
        with paddle.utils.unique_name.guard():
            net = architectures.__dict__[self.model_name](
                class_dim=self.num_classes, **params)
        return net

    def get_test_inputs(self, image_shape):
        input_spec = [
            InputSpec(
                shape=[None, 3] + image_shape, name='image', dtype='float32')
        ]
        return input_spec

    def run(self, net, inputs, mode):
        net_out = net(inputs[0])
        softmax_out = F.softmax(net_out)
        if mode == 'test':
            outputs = OrderedDict([('prediction', softmax_out)])

        elif mode == 'eval':
            labels = to_tensor(inputs[1].numpy().astype('int64').reshape(-1,
                                                                         1))
            acc1 = paddle.metric.accuracy(softmax_out, label=labels)
            k = min(5, self.num_classes)
            acck = paddle.metric.accuracy(softmax_out, label=labels, k=k)
            # multi cards eval
            if paddle.distributed.get_world_size() > 1:
                acc1 = paddle.distributed.all_reduce(
                    acc1, op=paddle.distributed.ReduceOp.
                    SUM) / paddle.distributed.get_world_size()
                acck = paddle.distributed.all_reduce(
                    acck, op=paddle.distributed.ReduceOp.
                    SUM) / paddle.distributed.get_world_size()

            outputs = OrderedDict([('acc1', acc1), ('acc{}'.format(k), acck),
                                   ('prediction', softmax_out)])

        else:
            # mode == 'train'
            labels = to_tensor(inputs[1].numpy().astype('int64').reshape(-1,
                                                                         1))
            loss = CELoss(class_dim=self.num_classes)
            loss = loss(net_out, inputs[1])
            acc1 = paddle.metric.accuracy(softmax_out, label=labels, k=1)
            k = min(5, self.num_classes)
            acck = paddle.metric.accuracy(softmax_out, label=labels, k=k)

            outputs = OrderedDict([('loss', loss), ('acc1', acc1),
                                   ('acc{}'.format(k), acck)])

        return outputs

    def default_optimizer(self, parameters, learning_rate, warmup_steps,
                          warmup_start_lr, lr_decay_epochs, lr_decay_gamma,
                          num_steps_each_epoch):
        boundaries = [b * num_steps_each_epoch for b in lr_decay_epochs]
        values = [
            learning_rate * (lr_decay_gamma**i)
            for i in range(len(lr_decay_epochs) + 1)
        ]
        scheduler = paddle.optimizer.lr.PiecewiseDecay(boundaries, values)
        if warmup_steps > 0:
            if warmup_steps > lr_decay_epochs[0] * num_steps_each_epoch:
                logging.error(
                    "In function train(), parameters should satisfy: "
                    "warmup_steps <= lr_decay_epochs[0]*num_samples_in_train_dataset",
                    exit=False)
                logging.error(
                    "See this doc for more information: "
                    "https://github.com/PaddlePaddle/PaddleX/blob/develop/docs/appendix/parameters.md#notice",
                    exit=False)
                logging.error(
                    "warmup_steps should less than {} or lr_decay_epochs[0] greater than {}, "
                    "please modify 'lr_decay_epochs' or 'warmup_steps' in train function".
                    format(lr_decay_epochs[0] * num_steps_each_epoch,
                           warmup_steps // num_steps_each_epoch))

            scheduler = paddle.optimizer.lr.LinearWarmup(
                learning_rate=scheduler,
                warmup_steps=warmup_steps,
                start_lr=warmup_start_lr,
                end_lr=learning_rate)
        optimizer = paddle.optimizer.Momentum(
            scheduler,
            momentum=.9,
            weight_decay=paddle.regularizer.L2Decay(coeff=1e-04),
            parameters=parameters)
        return optimizer

    def train(self,
              num_epochs,
              train_dataset,
              train_batch_size=64,
              eval_dataset=None,
              optimizer=None,
              save_interval_epochs=1,
              log_interval_steps=10,
              save_dir='output',
              pretrain_weights='IMAGENET',
              learning_rate=.025,
              warmup_steps=0,
              warmup_start_lr=0.0,
              lr_decay_epochs=(30, 60, 90),
              lr_decay_gamma=0.1,
              early_stop=False,
              early_stop_patience=5,
              use_vdl=True):
        """
        Train the model.
        Args:
            num_epochs(int): The number of epochs.
            train_dataset(paddlex.dataset): Training dataset.
            train_batch_size(int, optional): Total batch size among all cards used in training. Defaults to 64.
            eval_dataset(paddlex.dataset, optional):
                Evaluation dataset. If None, the model will not be evaluated during training process. Defaults to None.
            optimizer(paddle.optimizer.Optimizer or None, optional):
                Optimizer used for training. If None, a default optimizer is used. Defaults to None.
            save_interval_epochs(int, optional): Epoch interval for saving the model. Defaults to 1.
            log_interval_steps(int, optional): Step interval for printing training information. Defaults to 10.
            save_dir(str, optional): Directory to save the model. Defaults to 'output'.
            pretrain_weights(str or None, optional):
                None or name/path of pretrained weights. If None, no pretrained weights will be loaded. Defaults to 'IMAGENET'.
            learning_rate(float, optional): Learning rate for training. Defaults to .025.
            warmup_steps(int, optional): The number of steps of warm-up training. Defaults to 0.
            warmup_start_lr(float, optional): Start learning rate of warm-up training. Defaults to 0..
            lr_decay_epochs(List[int] or Tuple[int], optional):
                Epoch milestones for learning rate decay. Defaults to (20, 60, 90).
            lr_decay_gamma(float, optional): Gamma coefficient of learning rate decay, default .1.
            early_stop(bool, optional): Whether to adopt early stop strategy. Defaults to False.
            early_stop_patience(int, optional): Early stop patience. Defaults to 5.
            use_vdl(bool, optional): Whether to use VisualDL to monitor the training process. Defaults to True.

        """
        self.labels = train_dataset.labels

        # build optimizer if not defined
        if optimizer is None:
            num_steps_each_epoch = len(train_dataset) // train_batch_size
            self.optimizer = self.default_optimizer(
                parameters=self.net.parameters(),
                learning_rate=learning_rate,
                warmup_steps=warmup_steps,
                warmup_start_lr=warmup_start_lr,
                lr_decay_epochs=lr_decay_epochs,
                lr_decay_gamma=lr_decay_gamma,
                num_steps_each_epoch=num_steps_each_epoch)
        else:
            self.optimizer = optimizer

        # initiate weights
        if pretrain_weights is not None and not osp.exists(pretrain_weights):
            if pretrain_weights not in ['IMAGENET']:
                logging.warning(
                    "Path of pretrain_weights('{}') does not exist!".format(
                        pretrain_weights))
                logging.warning(
                    "Pretrain_weights is forcibly set to 'IMAGENET'. "
                    "If don't want to use pretrain weights, "
                    "set pretrain_weights to be None.")
                pretrain_weights = 'IMAGENET'
        pretrained_dir = osp.join(save_dir, 'pretrain')
        self.net_initialize(
            pretrain_weights=pretrain_weights, save_dir=pretrained_dir)

        # start train loop
        self.train_loop(
            num_epochs=num_epochs,
            train_dataset=train_dataset,
            train_batch_size=train_batch_size,
            eval_dataset=eval_dataset,
            save_interval_epochs=save_interval_epochs,
            log_interval_steps=log_interval_steps,
            save_dir=save_dir,
            early_stop=early_stop,
            early_stop_patience=early_stop_patience,
            use_vdl=use_vdl)

    def evaluate(self, eval_dataset, batch_size=1, return_details=False):
        """
        Evaluate the model.
        Args:
            eval_dataset(paddlex.dataset): Evaluation dataset.
            batch_size(int, optional): Total batch size among all cards used for evaluation. Defaults to 1.
            return_details(bool, optional): Whether to return evaluation details. Defaults to False.

        Returns:
            collections.OrderedDict with key-value pairs: {"acc1": `top 1 accuracy`, "acc5": `top 5 accuracy`}.

        """
        # 给transform添加arrange操作
        arrange_transforms(
            model_type=self.model_type,
            transforms=eval_dataset.transforms,
            mode='eval')

        self.net.eval()
        nranks = paddle.distributed.get_world_size()
        local_rank = paddle.distributed.get_rank()
        if nranks > 1:
            # Initialize parallel environment if not done.
            if not paddle.distributed.parallel.parallel_helper._is_parallel_ctx_initialized(
            ):
                paddle.distributed.init_parallel_env()
        self.eval_data_loader = self.build_data_loader(
            eval_dataset, batch_size=batch_size, mode='eval')
        eval_metrics = TrainingStats()
        eval_details = None
        if return_details:
            eval_details = list()

        with paddle.no_grad():
            for step, data in enumerate(self.eval_data_loader()):
                outputs = self.run(self.net, data, mode='eval')
                if return_details:
                    eval_details.append(outputs['prediction'].numpy())
                outputs.pop('prediction')
                eval_metrics.update(outputs)
        if return_details:
            return eval_metrics.get(), eval_details
        else:
            return eval_metrics.get()

    def predict(self, img_file, transforms=None, topk=1):
        """
        Do inference.
        Args:
            img_file(List[np.ndarray or str], str or np.ndarray): img_file(list or str or np.array)：
                Image path or decoded image data in a BGR format, which also could constitute a list,
                meaning all images to be predicted as a mini-batch.
            transforms(paddlex.transforms.Compose or None, optional):
                Transforms for inputs. If None, the transforms for evaluation process will be used. Defaults to None.
            topk(int, optional): Keep topk results in prediction. Defaults to 1.

        Returns:
            If img_file is a string or np.array, the result is a dict with key-value pairs:
            {"category_id": `category_id`, "category": `category`, "score": `score`}.
            If img_file is a list, the result is a list composed of dicts with the corresponding fields:
            category_id(int): the predicted category ID
            category(str): category name
            score(float): confidence

        """
        if transforms is None and not hasattr(self, 'test_transforms'):
            raise Exception("transforms need to be defined, now is None.")
        if transforms is None:
            transforms = self.test_transforms
        true_topk = min(self.num_classes, topk)
        if isinstance(img_file, (str, np.ndarray)):
            images = [img_file]
        else:
            images = img_file
        im = self._preprocess(images, transforms, self.model_type)
        self.net.eval()
        with paddle.no_grad():
            outputs = self.run(self.net, im, mode='test')
        prediction = outputs['prediction'].numpy()
        prediction = self._postprocess(prediction, true_topk, self.labels)
        if isinstance(img_file, (str, np.ndarray)):
            prediction = prediction[0]

        return prediction

    def _preprocess(self, images, transforms, model_type):
        arrange_transforms(
            model_type=model_type, transforms=transforms, mode='test')
        batch_im = list()
        for im in images:
            sample = {'image': im}
            batch_im.append(transforms(sample))

        batch_im = to_tensor(batch_im)

        return batch_im,

    def _postprocess(self, results, true_topk, labels):
        preds = list()
        for i, pred in enumerate(results):
            pred_label = np.argsort(pred)[::-1][:true_topk]
            preds.append([{
                'category_id': l,
                'category': labels[l],
                'score': results[i][l]
            } for l in pred_label])

        return preds


class ResNet18(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet18, self).__init__(
            model_name='ResNet18', num_classes=num_classes)


class ResNet34(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet34, self).__init__(
            model_name='ResNet34', num_classes=num_classes)


class ResNet50(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet50, self).__init__(
            model_name='ResNet50', num_classes=num_classes)


class ResNet101(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet101, self).__init__(
            model_name='ResNet101', num_classes=num_classes)


class ResNet152(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet152, self).__init__(
            model_name='ResNet152', num_classes=num_classes)


class ResNet18_vd(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet18_vd, self).__init__(
            model_name='ResNet18_vd', num_classes=num_classes)


class ResNet34_vd(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet34_vd, self).__init__(
            model_name='ResNet34_vd', num_classes=num_classes)


class ResNet50_vd(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet50_vd, self).__init__(
            model_name='ResNet50_vd', num_classes=num_classes)


class ResNet50_vd_ssld(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet50_vd_ssld, self).__init__(
            model_name='ResNet50_vd',
            num_classes=num_classes,
            lr_mult_list=[.1, .1, .2, .2, .3])
        self.model_name = 'ResNet50_vd_ssld'


class ResNet101_vd(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet101_vd, self).__init__(
            model_name='ResNet101_vd', num_classes=num_classes)


class ResNet101_vd_ssld(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet101_vd_ssld, self).__init__(
            model_name='ResNet101_vd_ssld',
            num_classes=num_classes,
            lr_mult_list=[.1, .1, .2, .2, .3])
        self.model_name = 'ResNet101_vd_ssld'


class ResNet152_vd(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet152_vd, self).__init__(
            model_name='ResNet152_vd', num_classes=num_classes)


class ResNet200_vd(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ResNet200_vd, self).__init__(
            model_name='ResNet200_vd', num_classes=num_classes)


class AlexNet(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(AlexNet, self).__init__(
            model_name='AlexNet', num_classes=num_classes)

    def get_test_inputs(self, image_shape):
        if image_shape == [-1, -1]:
            image_shape = [224, 224]
            logging.info('When exporting inference model for {},'.format(
                self.__class__.__name__
            ) + ' if image_shape is [-1, -1], it will be forcibly set to [224, 224]'
                         )
        input_spec = [
            InputSpec(
                shape=[None, 3] + image_shape, name='image', dtype='float32')
        ]
        return input_spec


class DarkNet53(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(DarkNet53, self).__init__(
            model_name='DarkNet53', num_classes=num_classes)


class MobileNetV1(BaseClassifier):
    def __init__(self, num_classes=1000, scale=1.0):
        supported_scale = [.25, .5, .75, 1.0]
        if scale not in supported_scale:
            logging.warning("scale={} is not supported by MobileNetV1, "
                            "scale is forcibly set to 1.0".format(scale))
            scale = 1.0
        params = {'scale': scale}
        super(MobileNetV1, self).__init__(
            model_name='MobileNetV1', num_classes=num_classes, **params)


class MobileNetV2(BaseClassifier):
    def __init__(self, num_classes=1000, scale=1.0):
        supported_scale = [.25, .5, .75, 1.0, 1.5, 2.0]
        if scale not in supported_scale:
            logging.warning("scale={} is not supported by MobileNetV2, "
                            "scale is forcibly set to 1.0".format(scale))
            scale = 1.0
        params = {'scale': scale}
        super(MobileNetV2, self).__init__(
            model_name='MobileNetV2', num_classes=num_classes, **params)


class MobileNetV3_small(BaseClassifier):
    def __init__(self, num_classes=1000, scale=1.0):
        supported_scale = [.35, .5, .75, 1.0, 1.25]
        if scale not in supported_scale:
            logging.warning("scale={} is not supported by MobileNetV3_small, "
                            "scale is forcibly set to 1.0".format(scale))
            scale = 1.0
        params = {'scale': scale}
        super(MobileNetV3_small, self).__init__(
            model_name='MobileNetV3_small', num_classes=num_classes, **params)


class MobileNetV3_large(BaseClassifier):
    def __init__(self, num_classes=1000, scale=1.0):
        supported_scale = [.35, .5, .75, 1.0, 1.25]
        if scale not in supported_scale:
            logging.warning("scale={} is not supported by MobileNetV3_large, "
                            "scale is forcibly set to 1.0".format(scale))
            scale = 1.0
        params = {'scale': scale}
        super(MobileNetV3_large, self).__init__(
            model_name='MobileNetV3_large', num_classes=num_classes, **params)


class DenseNet121(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(DenseNet121, self).__init__(
            model_name='DenseNet121', num_classes=num_classes)


class DenseNet161(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(DenseNet161, self).__init__(
            model_name='DenseNet161', num_classes=num_classes)


class DenseNet169(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(DenseNet169, self).__init__(
            model_name='DenseNet169', num_classes=num_classes)


class DenseNet201(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(DenseNet201, self).__init__(
            model_name='DenseNet201', num_classes=num_classes)


class DenseNet264(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(DenseNet264, self).__init__(
            model_name='DenseNet264', num_classes=num_classes)


class HRNet_W18_C(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(HRNet_W18_C, self).__init__(
            model_name='HRNet_W18_C', num_classes=num_classes)


class HRNet_W30_C(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(HRNet_W30_C, self).__init__(
            model_name='HRNet_W30_C', num_classes=num_classes)


class HRNet_W32_C(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(HRNet_W32_C, self).__init__(
            model_name='HRNet_W32_C', num_classes=num_classes)


class HRNet_W40_C(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(HRNet_W40_C, self).__init__(
            model_name='HRNet_W40_C', num_classes=num_classes)


class HRNet_W44_C(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(HRNet_W44_C, self).__init__(
            model_name='HRNet_W44_C', num_classes=num_classes)


class HRNet_W48_C(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(HRNet_W48_C, self).__init__(
            model_name='HRNet_W48_C', num_classes=num_classes)


class HRNet_W64_C(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(HRNet_W64_C, self).__init__(
            model_name='HRNet_W64_C', num_classes=num_classes)


class Xception41(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(Xception41, self).__init__(
            model_name='Xception41', num_classes=num_classes)


class Xception65(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(Xception65, self).__init__(
            model_name='Xception65', num_classes=num_classes)


class Xception71(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(Xception71, self).__init__(
            model_name='Xception71', num_classes=num_classes)


class ShuffleNetV2(BaseClassifier):
    def __init__(self, num_classes=1000, scale=1.0):
        supported_scale = [.25, .33, .5, 1.0, 1.5, 2.0]
        if scale not in supported_scale:
            logging.warning("scale={} is not supported by ShuffleNetV2, "
                            "scale is forcibly set to 1.0".format(scale))
            scale = 1.0
        params = {'scale': scale}
        super(ShuffleNetV2, self).__init__(
            model_name='ShuffleNetV2', num_classes=num_classes, **params)

    def get_test_inputs(self, image_shape):
        if image_shape == [-1, -1]:
            image_shape = [224, 224]
            logging.info('When exporting inference model for {},'.format(
                self.__class__.__name__
            ) + ' if image_shape is [-1, -1], it will be forcibly set to [224, 224]'
                         )
        input_spec = [
            InputSpec(
                shape=[None, 3] + image_shape, name='image', dtype='float32')
        ]
        return input_spec


class ShuffleNetV2_swish(BaseClassifier):
    def __init__(self, num_classes=1000):
        super(ShuffleNetV2_swish, self).__init__(
            model_name='ShuffleNetV2_x1_5', num_classes=num_classes)

    def get_test_inputs(self, image_shape):
        if image_shape == [-1, -1]:
            image_shape = [224, 224]
            logging.info('When exporting inference model for {},'.format(
                self.__class__.__name__
            ) + ' if image_shape is [-1, -1], it will be forcibly set to [224, 224]'
                         )
        input_spec = [
            InputSpec(
                shape=[None, 3] + image_shape, name='image', dtype='float32')
        ]
        return input_spec
