// Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <time.h>

#include "model_deploy/ppseg/include/seg_postprocess.h"

namespace PaddleDeploy {

bool SegPostprocess::Init(const YAML::Node& yaml_config) {
  return true;
}

void SegPostprocess::RestoreSegMap(const ShapeInfo& shape_info,
                                   cv::Mat* label_mat,
                                   cv::Mat*  score_mat,
                                   SegResult* result) {
  int ori_h = shape_info.shapes[0][1];
  int ori_w = shape_info.shapes[0][0];
  result->label_map.Resize({ori_h, ori_w});
  result->score_map.Resize({ori_h, ori_w});

  for (int j = shape_info.transforms.size() - 1; j > 0; --j) {
    std::vector<int> last_shape = shape_info.shapes[j - 1];
    std::vector<int> cur_shape = shape_info.shapes[j];
    if (shape_info.transforms[j] == "Resize" ||
        shape_info.transforms[j] == "ResizeByShort" ||
        shape_info.transforms[j] == "ResizeByLong") {
      if (last_shape[0] != label_mat->cols ||
            last_shape[1] != label_mat->rows) {
        cv::resize(*label_mat, *label_mat,
                cv::Size(last_shape[0], last_shape[1]),
                0, 0, cv::INTER_NEAREST);
        cv::resize(*score_mat, *score_mat,
                cv::Size(last_shape[0], last_shape[1]),
                0, 0, cv::INTER_LINEAR);
      }
    } else if (shape_info.transforms[j] == "Padding") {
      if (last_shape[0] < label_mat->cols || last_shape[1] < label_mat->rows) {
        *label_mat = (*label_mat)(cv::Rect(0, 0, last_shape[0], last_shape[1]));
        *score_mat = (*score_mat)(cv::Rect(0, 0, last_shape[0], last_shape[1]));
      }
    }
  }
  result->label_map.data.assign(
    label_mat->begin<uint8_t>(), label_mat->end<uint8_t>());
  result->score_map.data.assign(
    score_mat->begin<float>(), score_mat->end<float>());
}

bool SegPostprocess::Run(const std::vector<DataBlob>& outputs,
                         const std::vector<ShapeInfo>& shape_infos,
                         std::vector<Result>* results, int thread_num) {
  results->clear();
  int batch_size = shape_infos.size();
  results->resize(batch_size);

  // tricks for PaddleX, which segmentation model has two outputs
  int index = 0;
  if (outputs.size() == 2) {
    index = 1;
  }
  std::vector<int> score_map_shape = outputs[index].shape;
  int score_map_size = std::accumulate(score_map_shape.begin() + 1,
                    score_map_shape.end(), 1, std::multiplies<int>());
  const float* score_map_data =
        reinterpret_cast<const float*>(outputs[index].data.data());
  int num_map_pixels = score_map_shape[2] * score_map_shape[3];

  for (int i = 0; i < batch_size; ++i) {
    (*results)[i].model_type = "seg";
    (*results)[i].seg_result = new SegResult();
    const float* current_start_ptr = score_map_data + i * score_map_size;
    cv::Mat ori_score_mat(score_map_shape[1],
            score_map_shape[2] * score_map_shape[3],
            CV_32FC1, const_cast<float*>(current_start_ptr));
    ori_score_mat = ori_score_mat.t();
    cv::Mat score_mat(score_map_shape[2], score_map_shape[3], CV_32FC1);
    cv::Mat label_mat(score_map_shape[2], score_map_shape[3], CV_8UC1);
    for (int j = 0; j < ori_score_mat.rows; ++j) {
      double max_value;
      cv::Point max_id;
      minMaxLoc(ori_score_mat.row(j), 0, &max_value, 0, &max_id);
      score_mat.at<float>(j) = max_value;
      label_mat.at<uchar>(j) = max_id.x;
    }
    RestoreSegMap(shape_infos[i], &label_mat,
                &score_mat, (*results)[i].seg_result);
  }
  return true;
}

}  //  namespace PaddleDeploy
