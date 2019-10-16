# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Main function to train various object detection models."""

from __future__ import absolute_import
from __future__ import division
# from __future__ import google_type_annotations
from __future__ import print_function

from absl import app
from absl import flags
from absl import logging
import functools
import os
import pprint
import tensorflow.compat.v2 as tf

from official.modeling.hyperparams import params_dict
from official.modeling.training import distributed_executor as executor
from official.vision.detection.configs import factory as config_factory
from official.vision.detection.dataloader import input_reader
from official.vision.detection.dataloader import mode_keys as ModeKeys
from official.vision.detection.executor.detection_executor import DetectionDistributedExecutor
from official.vision.detection.modeling import factory as model_factory

executor.initialize_common_flags()

flags.DEFINE_string(
    'mode',
    default='train',
    help='Mode to run: `train`, `eval` or `train_and_eval`.')

flags.DEFINE_string(
    'model', default='retinanet',
    help='Model to run: `retinanet` or `shapemask`.')

flags.DEFINE_string('training_file_pattern', None,
                    'Location of the train data.')

flags.DEFINE_string('eval_file_pattern', None, 'Location of ther eval data')


FLAGS = flags.FLAGS


def run_executor(params, train_input_fn=None, eval_input_fn=None):
  """Runs Retinanet model on distribution strategy defined by the user."""

  model_builder = model_factory.model_generator(params)

  if FLAGS.mode == 'train':

    def _model_fn(params):
      return model_builder.build_model(params, mode=ModeKeys.TRAIN)

    builder = executor.ExecutorBuilder(
        strategy_type=params.strategy_type,
        strategy_config=params.strategy_config)
    num_workers = (builder.strategy.num_replicas_in_sync + 7) / 8
    is_multi_host = (num_workers > 1)
    if is_multi_host:
      train_input_fn = functools.partial(
          train_input_fn,
          batch_size=params.train.batch_size //
          builder.strategy.num_replicas_in_sync)

    dist_executor = builder.build_executor(
        class_ctor=DetectionDistributedExecutor,
        params=params,
        is_multi_host=is_multi_host,
        model_fn=_model_fn,
        loss_fn=model_builder.build_loss_fn,
        predict_post_process_fn=model_builder.post_processing,
        trainable_variables_filter=model_builder
        .make_filter_trainable_variables_fn())

    return dist_executor.train(
        train_input_fn=train_input_fn,
        model_dir=params.model_dir,
        iterations_per_loop=params.train.iterations_per_loop,
        total_steps=params.train.total_steps,
        init_checkpoint=model_builder.make_restore_checkpoint_fn(),
        save_config=True)
  elif FLAGS.mode == 'eval':

    def _model_fn(params):
      return model_builder.build_model(params, mode=ModeKeys.PREDICT_WITH_GT)

    builder = executor.ExecutorBuilder(
        strategy_type=params.strategy_type,
        strategy_config=params.strategy_config)
    dist_executor = builder.build_executor(
        class_ctor=DetectionDistributedExecutor,
        params=params,
        model_fn=_model_fn,
        loss_fn=model_builder.build_loss_fn,
        predict_post_process_fn=model_builder.post_processing,
        trainable_variables_filter=model_builder
        .make_filter_trainable_variables_fn())

    results = dist_executor.evaluate_from_model_dir(
        model_dir=params.model_dir,
        eval_input_fn=eval_input_fn,
        eval_metric_fn=model_builder.eval_metrics,
        eval_timeout=params.eval.eval_timeout,
        min_eval_interval=params.eval.min_eval_interval,
        total_steps=params.train.total_steps)
    for k, v in results.items():
      logging.info('Final eval metric %s: %f', k, v)
    return results
  else:
    raise ValueError('Mode not found: %s.' % FLAGS.mode)


def main(argv):
  del argv  # Unused.

  params = config_factory.config_generator(FLAGS.model)

  params = params_dict.override_params_dict(
      params, FLAGS.config_file, is_strict=True)

  params = params_dict.override_params_dict(
      params, FLAGS.params_override, is_strict=True)
  params.override(
      {
          'strategy_type': FLAGS.strategy_type,
          'model_dir': FLAGS.model_dir,
          'strategy_config': executor.strategy_flags_dict(),
      },
      is_strict=False)
  params.validate()
  params.lock()
  pp = pprint.PrettyPrinter()
  params_str = pp.pformat(params.as_dict())
  logging.info('Model Parameters: {}'.format(params_str))

  train_input_fn = None
  eval_input_fn = None
  training_file_pattern = FLAGS.training_file_pattern or params.train.train_file_pattern
  eval_file_pattern = FLAGS.eval_file_pattern or params.eval.eval_file_pattern
  if not training_file_pattern and not eval_file_pattern:
    raise ValueError('Must provide at least one of training_file_pattern and '
                     'eval_file_pattern.')

  if training_file_pattern:
    # Use global batch size for single host.
    train_input_fn = input_reader.InputFn(
        file_pattern=training_file_pattern,
        params=params,
        mode=input_reader.ModeKeys.TRAIN,
        batch_size=params.train.batch_size)

  if eval_file_pattern:
    eval_input_fn = input_reader.InputFn(
        file_pattern=eval_file_pattern,
        params=params,
        mode=input_reader.ModeKeys.PREDICT_WITH_GT,
        batch_size=params.eval.batch_size,
        num_examples=params.eval.eval_samples)
  return run_executor(
      params, train_input_fn=train_input_fn, eval_input_fn=eval_input_fn)


if __name__ == '__main__':
  assert tf.version.VERSION.startswith('2.')
  app.run(main)
