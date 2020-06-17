import os
import gc
import random
import weakref
import atexit
import warnings
import itertools
import math
from abc import ABCMeta, abstractmethod
from copy import deepcopy
import tempfile
import time
import sys
from contextlib import contextmanager
import pathlib
import logging

import numpy as np
import tensorflow as tf
from tensorflow.data import Dataset
from tensorflow.compat.v1 import logging as tf_logging

from sklearn.model_selection import train_test_split
import joblib

from finetune.util import list_transpose
from finetune.encoding.input_encoder import EncodedOutput
from finetune.config import all_gpus, assert_valid_config, get_default_config
from finetune.saver import Saver, InitializeHook
from finetune.errors import FinetuneError
from finetune.model import get_model_fn, PredictMode
from finetune.util.download import download_data_if_required
from finetune.util.shapes import shape_list
from finetune.util.timing import ProgressBar
from finetune.util.in_memory_finetune import make_in_memory_finetune_hooks
from finetune.util.indico_estimator import IndicoEstimator
from finetune.util.gpu_info import gpu_info

from finetune.input_pipeline import BasePipeline, InputMode
from sklearn.utils import shuffle as dataset_shuffle
from finetune.util.input_utils import InputMode, validation_settings, wrap_tqdm, Chunker, has_targets, batch_dataset

from finetune.base import BaseModel
from finetune.encoding.target_encoders import (
    SequenceLabelingEncoder,
    SequenceMultiLabelingEncoder,
)
from finetune.nn.target_blocks import sequence_labeler
from finetune.nn.crf import sequence_decode
from finetune.encoding.sequence_encoder import (
    finetune_to_indico_sequence,
)
from finetune.encoding.input_encoder import get_spacy
from finetune.encoding.input_encoder import tokenize_context
from finetune.target_models.sequence_labeling import SequencePipeline

LOGGER = logging.getLogger("finetune")


class SSLPipeline(SequencePipeline):
    def __init__(self, config, multi_label):
        super(SSLPipeline, self).__init__(config, multi_label)

    def get_dataset_from_generator(self, generator_fn, input_mode,
                                   update_hook=None, u_generator_fn=None):
        def chunked_and_tokenized_dataset(gen):
            for d in gen():
                yield from self.text_to_tokens_mask(**d)
                
        datasets = super().get_dataset_from_generator(generator_fn, input_mode,
                                                      update_hook=update_hook)
        if input_mode == InputMode.PREDICT:
            return datasets

        if u_generator_fn:
            u_data_fn = lambda: chunked_and_tokenized_dataset(u_generator_fn)
            types, shapes = self.feed_shape_type_def()
            u_types, u_shapes = types[0], shapes[0]
            u_raw_dataset = self.make_dataset_fn(
                data_fn=u_data_fn,
                tqdm_mode=tqdm_mode,
                update_hook=update_hook,
                types=u_types,
                shapes=u_shapes,
                skip_val=input_mode == InputMode.TRAIN
            )
            u_train_dataset = (
                lambda: u_raw_dataset()
                .shuffle(
                    self.config.shuffle_buffer_size,
                    seed=self.config.seed,
                    reshuffle_each_iteration=False,
                )
            )
            u_batch_dataset = batch_dataset(
                    u_train_dataset,
                    batch_size=self.config.batch_size,
                    shapes=u_shapes,
                    n_epochs=self.config.n_epochs
            )
            datasets["train_dataset"] = (lambda:
                self.combine_datasets(datasets["train_dataset"],
                                      u_batch_dataset))
        return datasets


    def get_dataset_from_list(self, data_list, input_mode, update_hook=None, u_data_list=None):
        assert input_mode == InputMode.TRAIN, "use the generator path for prediction"

        datasets = super().get_dataset_from_list(data_list, input_mode,
                                                 update_hook=update_hook)
        if u_data_list:
            u_data_list = list(u_data_list)
            u_train = dataset_shuffle(u_data_list, random_state=self.config.seed)
            tokenized_u_train = list(
                itertools.chain.from_iterable(
                    self.text_to_tokens_mask(**d) for d in u_train
                )
            )
            types, shapes = self.feed_shape_type_def()
            u_types, u_shapes = types[0], shapes[0]
            u_train_unbatched = self.make_dataset_fn(
                data_fn=lambda: tokenized_u_train,
                tqdm_mode="train",
                update_hook=update_hook,
                types=u_types,
                shapes=u_shapes
            )
            u_train_batched = batch_dataset(
                u_train_unbatched,
                batch_size=self.config.batch_size,
                shapes=u_shapes,
                n_epochs=self.config.n_epochs
            )
            x_train = datasets["train_dataset"]
            datasets["train_dataset"] = (lambda:
                                         self.combine_datasets(x_train,
                                                               u_train_batched))
        return datasets

    def combine_datasets(self, x_dataset, u_dataset):
        def map_func(X, U):
            combined = {
                **(X[0]),
                "u_tokens": U["tokens"]
            }
            if "context" in U:
                combined["u_context"] = U["context"]
            return (combined, X[1])
        zipped_dataset = tf.data.Dataset.zip((x_dataset(), u_dataset()))
        combined_dataset = zipped_dataset.map(map_func)
        return combined_dataset

class SSLLabeler(BaseModel):
    defaults = dict()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _get_input_pipeline(self):
        return SSLPipeline(
            config=self.config,
            multi_label=self.config.multi_label_sequences
        )

    def _initialize(self):
        return super()._initialize()

    def predict(self, X, per_token=False, context=None, **kwargs):
        """
        Produces a list of most likely class labels as determined by the fine-tuned model.

        :param X: A list / array of text, shape [batch]
        :param per_token: If True, return raw probabilities and labels on a per token basis
        :returns: list of class labels.
        """
        return super().predict(X, per_token=per_token, context=context, **kwargs)

    def predict_proba(self, X, context=None, **kwargs):
        """
        Produces a list of most likely class labels as determined by the fine-tuned model.

        :param X: A list / array of text, shape [batch]
        :returns: list of class labels.
        """
        return self.predict(X, context=context, **kwargs)

    def _predict_op(self, logits, **kwargs):
        pass

    def _predict_proba_op(self, logits, **kwargs):
        pass

    def featurize(self, X, **kwargs):
        """
        Embeds inputs in learned feature space. Can be called before or after calling :meth:`finetune`.

        :param Xs: An iterable of lists or array of text, shape [batch, n_inputs, tokens]
        :returns: np.array of features of shape (n_examples, embedding_size).
        """
        return super().featurize(X, **kwargs)

    def _target_model(
        self, *, config, featurizer_state, targets, n_outputs, train=False, reuse=None, **kwargs
    ):
        return sequence_labeler(
            hidden=featurizer_state["sequence_features"],
            targets=targets,
            n_targets=n_outputs,
            pad_id=config.pad_idx,
            config=config,
            train=train,
            multilabel=config.multi_label_sequences,
            reuse=reuse,
            lengths=featurizer_state["lengths"],
            use_crf=self.config.crf_sequence_labeling,
            **kwargs
        )

    def finetune(self, Xs, Us=None, Y=None, context=None, update_hook=None):
        if callable(Xs):
            assert (not Us or callable(Us)), "If X is a generator, U must also be a generator"
            datasets = self.input_pipeline.get_dataset_from_generator(
                Xs, input_mode=InputMode.TRAIN,
                update_hook=update_hook, u_generator_fn=Us
            )
        else:
            assert (not Us or not callable(Us)), "If X is a list, U must also be a list"
            x_list = self.input_pipeline.zip_list_to_dict(X=Xs, Y=Y, context=context)
            u_list = self.input_pipeline.zip_list_to_dict(X=Us, context=context)
            datasets = self.input_pipeline.get_dataset_from_list(
                x_list, input_mode=InputMode.TRAIN,
                update_hook=update_hook, u_data_list=u_list
            )
                
        if self.config.keep_best_model:
            if self.config.val_size <= 10:
                tf.compat.v1.logging.warning(
                    "Early stopping / keeping best model with a validation size of {} is likely to case undesired results".format(
                        self.config.val_size
                    )
                )

        force_build_lm = Y is None
        estimator, hooks = self.get_estimator(force_build_lm=force_build_lm)
        train_hooks = hooks.copy()

        steps_per_epoch = self._n_steps(
            n_examples=self.input_pipeline.dataset_size,
            batch_size=self.config.batch_size,
            n_gpus=max(1, len(self.resolved_gpus)),
        )
        num_steps = steps_per_epoch * self.config.n_epochs

        if self.config.val_size > 0:
            # Validation with all other tasks.
            train_hooks.append(
                tf.estimator.experimental.InMemoryEvaluatorHook(
                    estimator,
                    datasets["val_dataset"],
                    every_n_iter=self.config.val_interval,
                    steps=math.ceil(self.config.val_size / self.config.batch_size),
                )
            )
            early_stopping_interval = self.config.val_interval
        else:
            early_stopping_interval = sys.maxsize

        train_hooks.append(
            self.saver.get_saver_hook(
                estimator=estimator,
                keep_best_model=self.config.keep_best_model,
                steps_per_epoch=steps_per_epoch,
                early_stopping_steps=self.config.early_stopping_steps,
                eval_frequency=early_stopping_interval,
                cache_weights_to_file=self.config.cache_weights_to_file
            )
        )

        if self.config.in_memory_finetune is not None:
            train_hooks.extend(make_in_memory_finetune_hooks(self, estimator))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            train_input_fn_skipped = lambda: datasets["train_dataset"]().skip(self.saver.get_initial_step() * max(len(self.resolved_gpus), 1))
            estimator.train(train_input_fn_skipped, hooks=train_hooks, steps=num_steps)
        
        self._trained = True

    def _predict(self, zipped_data, **kwargs):
        raise NotImplemented()

    def _predict_proba(self, zipped_data, **kwargs):
        pass
