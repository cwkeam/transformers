# coding=utf-8
# Copyright 2021 The HuggingFace Inc. team.
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
"""
Speech processor class for MCTC
"""
from contextlib import contextmanager

from ...processing_utils import ProcessorMixin


class MCTCProcessor(ProcessorMixin):
    r"""
    Constructs a MCTC processor which wraps a MCTC feature extractor and a MCTC tokenizer into a
    single processor.

    [`MCTCProcessor`] offers all the functionalities of [`MCTCFeatureExtractor`] and
    [`MCTCTokenizer`]. See the [`~MCTCProcessor.__call__`] and [`~MCTCProcessor.decode`] for more
    information.

    Args:
        feature_extractor (`MCTCFeatureExtractor`):
            An instance of [`MCTCFeatureExtractor`]. The feature extractor is a required input.
        tokenizer (`MCTCTokenizer`):
            An instance of [`MCTCTokenizer`]. The tokenizer is a required input.
    """
    feature_extractor_class = "MCTCFeatureExtractor"
    tokenizer_class = "MCTCTokenizer"

    def __init__(self, feature_extractor, tokenizer):
        super().__init__(feature_extractor, tokenizer)
        self.current_processor = self.feature_extractor

    def __call__(self, *args, **kwargs):
        """
        When used in normal mode, this method forwards all its arguments to MCTCFeatureExtractor's
        [`~MCTCFeatureExtractor.__call__`] and returns its output. If used in the context
        [`~MCTCProcessor.as_target_processor`] this method forwards all its arguments to MCTCTokenizer's
        [`~MCTCTokenizer.__call__`]. Please refer to the doctsring of the above two methods for more
        information.
        """
        return self.current_processor(*args, **kwargs)

    def batch_decode(self, *args, **kwargs):
        """
        This method forwards all its arguments to MCTCTokenizer's [`~PreTrainedTokenizer.batch_decode`]. Please
        refer to the docstring of this method for more information.
        """
        return self.tokenizer.batch_decode(*args, **kwargs)

    def decode(self, *args, **kwargs):
        """
        This method forwards all its arguments to MCTCTokenizer's [`~PreTrainedTokenizer.decode`]. Please refer
        to the docstring of this method for more information.
        """
        return self.tokenizer.decode(*args, **kwargs)

    @contextmanager
    def as_target_processor(self):
        """
        Temporarily sets the tokenizer for processing the input. Useful for encoding the labels when fine-tuning
        MCTC.
        """
        self.current_processor = self.tokenizer
        yield
        self.current_processor = self.feature_extractor