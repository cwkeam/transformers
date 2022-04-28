# coding=utf-8
# Copyright 2022 Chan Woo Kim The HuggingFace Inc. team. All rights reserved.
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
""" PyTorch mCTC model."""


import math
import os
import random
from typing import Optional

import torch
import torch.utils.checkpoint
from packaging import version
from torch import nn
from torch.nn.parameter import Parameter


from ...activations import ACT2FN
from ...deepspeed import is_deepspeed_zero3_enabled
from ...file_utils import add_code_sample_docstrings, add_start_docstrings, add_start_docstrings_to_model_forward
from ...modeling_outputs import BaseModelOutput, CausalLMOutput
from ...modeling_utils import (
    PreTrainedModel,
    apply_chunking_to_forward,
    find_pruneable_heads_and_indices,
    prune_linear_layer,
)
from ...utils import logging
from .configuration_mctc import MCTCConfig


logger = logging.get_logger(__name__)

_HIDDEN_STATES_START_POSITION = 1

_CONFIG_FOR_DOC = "MCTCConfig"
_TOKENIZER_FOR_DOC = "MCTCTokenizer"
_PROCESSOR_FOR_DOC = "MCTCProcessor"

# Base docstring
_CHECKPOINT_FOR_DOC = "mctc-large"
_EXPECTED_OUTPUT_SHAPE = [1, 292, 768]

# CTC docstring
_CTC_EXPECTED_OUTPUT = "'MISTER QUILTER IS THE APOSTLE OF THE MIDDLE CLASSES AND WE ARE GLAD TO WELCOME HIS GOSPEL'"
_CTC_EXPECTED_LOSS = 53.48


MCTC_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "mctc-large",
    # See all mCTC models at https://huggingface.co/models?filter=mctc
]


def load_tf_weights_in_mctc(model, config, tf_checkpoint_path):
    """Load tf checkpoints in a pytorch model."""
    try:
        import re

        import numpy as np
        import tensorflow as tf
    except ImportError:
        logger.error(
            "Loading a TensorFlow model in PyTorch, requires TensorFlow to be installed. Please see "
            "https://www.tensorflow.org/install/ for installation instructions."
        )
        raise
    tf_path = os.path.abspath(tf_checkpoint_path)
    logger.info(f"Converting TensorFlow checkpoint from {tf_path}")
    # Load weights from TF model
    init_vars = tf.train.list_variables(tf_path)
    names = []
    arrays = []
    for name, shape in init_vars:
        logger.info(f"Loading TF weight {name} with shape {shape}")
        array = tf.train.load_variable(tf_path, name)
        names.append(name)
        arrays.append(array)

    for name, array in zip(names, arrays):
        name = name.split("/")
        # adam_v and adam_m are variables used in AdamWeightDecayOptimizer to calculated m and v
        # which are not required for using pretrained model
        if any(
            n in ["adam_v", "adam_m", "AdamWeightDecayOptimizer", "AdamWeightDecayOptimizer_1", "global_step"]
            for n in name
        ):
            logger.info(f"Skipping {'/'.join(name)}")
            continue
        pointer = model
        for m_name in name:
            if re.fullmatch(r"[A-Za-z]+_\d+", m_name):
                scope_names = re.split(r"_(\d+)", m_name)
            else:
                scope_names = [m_name]
            if scope_names[0] == "kernel" or scope_names[0] == "gamma":
                pointer = getattr(pointer, "weight")
            elif scope_names[0] == "output_bias" or scope_names[0] == "beta":
                pointer = getattr(pointer, "bias")
            elif scope_names[0] == "output_weights":
                pointer = getattr(pointer, "weight")
            elif scope_names[0] == "squad":
                pointer = getattr(pointer, "classifier")
            else:
                try:
                    pointer = getattr(pointer, scope_names[0])
                except AttributeError:
                    logger.info(f"Skipping {'/'.join(name)}")
                    continue
            if len(scope_names) >= 2:
                num = int(scope_names[1])
                pointer = pointer[num]
        if m_name[-11:] == "_embeddings":
            pointer = getattr(pointer, "weight")
        elif m_name == "kernel":
            array = np.transpose(array)
        try:
            assert (
                pointer.shape == array.shape
            ), f"Pointer shape {pointer.shape} and array shape {array.shape} mismatched"
        except AssertionError as e:
            e.args += (pointer.shape, array.shape)
            raise
        logger.info(f"Initialize PyTorch weight {name}")
        pointer.data = torch.from_numpy(array)
    return model


# Copied from transformers.models.bart.modeling_bart._expand_mask
def _expand_mask(mask: torch.Tensor, dtype: torch.dtype, tgt_len: Optional[int] = None):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len

    expanded_mask = mask[:, None, None, :].expand(bsz, 1, tgt_len, src_len).to(dtype)

    inverted_mask = 1.0 - expanded_mask

    return inverted_mask.masked_fill(inverted_mask.bool(), torch.finfo(dtype).min)


class Conv1dSubsampler(nn.Module):
    """
    Convolutional subsampler: a stack of 1D convolution (along temporal dimension) followed by non-linear activation
    via gated linear units (https://arxiv.org/abs/1911.08460)
    """

    def __init__(self, config):
        super(Conv1dSubsampler, self).__init__()
        self.config = config
        self.glu_dim = config.conv_glu_dim

        self.dropout = nn.Dropout(config.conv_dropout)

        self.num_layers = config.num_conv_layers
        self.in_channels = config.input_feat_per_channel * config.input_channels

        if self.num_layers > 1:
            if config.conv_channels is None:
                raise ValueError(
                    "Need to specify `conv_channels` configuration in `MCTCConfig` to use multiple convolution layers."
                )

            self.mid_channels = config.conv_channels
        else:
            self.mid_channels = None

        self.out_channels = config.hidden_size * 2 # considering GLU halving
        self.kernel_size = config.conv_kernel
        self.stride = config.conv_stride

        self.conv_layers = nn.ModuleList(
            nn.Conv1d(
                self.in_channels if i == 0 else self.mid_channels[i],
                self.mid_channels[i] if i < self.num_layers - 1 else self.out_channels,
                kernel_size=k,
                stride=self.stride[i],
                padding="valid",
            )
            for i, k in enumerate(self.kernel_size)
        )

    def forward(self, input_features):
        # input_features == B x T x Features
        # -> hidden_states == B x F (channels) x T
        padding = sum([size//2 for size in self.kernel_size]) # (7, 7) -> (3, 3)
        input_features = torch.nn.functional.pad(input_features, (0,0, padding, padding), "constant", 0)
        hidden_states = input_features.transpose(1, 2).contiguous()  # -> B x F x T
        for conv in self.conv_layers:
            hidden_states = conv(hidden_states)
            hidden_states = nn.functional.glu(hidden_states, dim=self.glu_dim)
            hidden_states = self.dropout(hidden_states)

        hidden_states = hidden_states.transpose(1, 2).contiguous()  # -> B x T x F
        return hidden_states


class MCTCEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings."""

    def __init__(self, config):
        super().__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size, padding_idx=config.pad_token_id)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size)

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        # self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.LayerNorm = MCTCLayerNorm()
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        # position_ids (1, len position emb) is contiguous in memory and exported when serialized
        self.register_buffer("position_ids", torch.arange(config.max_position_embeddings).expand((1, -1)))
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")
        if version.parse(torch.__version__) > version.parse("1.6.0"):
            self.register_buffer(
                "token_type_ids",
                torch.zeros(self.position_ids.size(), dtype=torch.long, device=self.position_ids.device),
                persistent=False,
            )

    def forward(
        self, input_features=None, token_type_ids=None, position_ids=None, inputs_embeds=None, past_key_values_length=0
    ):
        if input_features is not None:
            input_shape = input_features.size()
        else:
            input_shape = inputs_embeds.size()[:-1]

        seq_length = input_shape[1]

        if position_ids is None:
            position_ids = self.position_ids[:, past_key_values_length : seq_length + past_key_values_length]

        # Setting the token_type_ids to the registered buffer in constructor where it is all zeros, which usually occurs
        # when its auto-generated, registered buffer helps users when tracing the model without passing token_type_ids, solves
        # issue #5664
        if token_type_ids is None:
            if hasattr(self, "token_type_ids"):
                buffered_token_type_ids = self.token_type_ids[:, :seq_length]
                buffered_token_type_ids_expanded = buffered_token_type_ids.expand(input_shape[0], seq_length)
                token_type_ids = buffered_token_type_ids_expanded
            else:
                token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=self.position_ids.device)

        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_features)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = inputs_embeds + token_type_embeddings
        if self.position_embedding_type == "absolute":
            position_embeddings = self.position_embeddings(position_ids)
            embeddings += position_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class MCTCSelfAttention(nn.Module):
    def __init__(self, config, position_embedding_type=None):
        super().__init__()
        if config.hidden_size % config.num_attention_heads != 0 and not hasattr(config, "embedding_size"):
            raise ValueError(
                f"The hidden size ({config.hidden_size}) is not a multiple of the number of attention "
                f"heads ({config.num_attention_heads})"
            )

        self.num_attention_heads = config.num_attention_heads
        # self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.attention_head_size = config.attention_head_dim

        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(config.hidden_size, self.all_head_size, bias=False)
        self.key = nn.Linear(config.hidden_size, self.all_head_size, bias=False)
        self.value = nn.Linear(config.hidden_size, self.all_head_size, bias=False)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

        self.position_embedding_type = position_embedding_type or getattr(
            config, "position_embedding_type", "absolute"
        )
        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            self.max_position_embeddings = config.max_position_embeddings
            self.distance_embedding = nn.Embedding(2 * config.max_position_embeddings - 1, self.attention_head_size)

        self.is_decoder = config.is_decoder

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def reshape_fortran(self, x, shape):
        if len(x.shape) > 0:
            x = x.permute(*reversed(range(len(x.shape))))
        return x.reshape(*reversed(shape)).permute(*reversed(range(len(shape))))

    def relativePositionEmbeddingRotate(self, data):
        data = data.permute(0, 2, 3, 1)

        b, d0, d1, d2 = data.shape
        
        # data = af::join(0, data, af::constant(0.0, d1, d1, d2, d3, data.type()));
        data = torch.cat((data, torch.zeros((b, d1, d1, d2))), dim=1)

        # data = af::moddims(data, af::dim4((d0 + d1) * d1, 1, d2, d3));
        # data = data.reshape()
        data = self.reshape_fortran(data, [b, (d0 + d1) * d1, 1, d2])
        # data = data.numpy().reshape((d0 + d1) * d1, 1, d2,order='F')


        # data = data.rows(0, (d1 + d0 - 1) * d1 - 1);
        data = data[:, :(d1 + d0 - 1) * d1]

        # data = af::moddims(data, af::dim4(d0 + d1 - 1, d1, d2, d3));
        data = self.reshape_fortran(data, [b, d0 + d1 - 1, d1, d2])

        n = d0 // 2
        data = data[:, n:n+d1].transpose(1, 2)

        return data.permute(0, 3, 1, 2)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        mixed_query_layer = self.query(hidden_states)
        mixed_query_layer = mixed_query_layer / math.sqrt(self.attention_head_size)


        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        is_cross_attention = encoder_hidden_states is not None

        # if is_cross_attention and past_key_value is not None:
        #     # reuse k,v, cross_attentions
        #     key_layer = past_key_value[0]
        #     value_layer = past_key_value[1]
        #     # attention_mask = encoder_attention_mask
        # elif is_cross_attention:
        #     key_layer = self.transpose_for_scores(self.key(encoder_hidden_states))
        #     value_layer = self.transpose_for_scores(self.value(encoder_hidden_states))
        #     # attention_mask = encoder_attention_mask
        # elif past_key_value is not None:
        #     key_layer = self.transpose_for_scores(self.key(hidden_states))
        #     value_layer = self.transpose_for_scores(self.value(hidden_states))
        #     key_layer = torch.cat([past_key_value[0], key_layer], dim=2)
        #     value_layer = torch.cat([past_key_value[1], value_layer], dim=2)
        # else:
        key_layer = self.transpose_for_scores(self.key(hidden_states))
        value_layer = self.transpose_for_scores(self.value(hidden_states))

        query_layer = self.transpose_for_scores(mixed_query_layer)


        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        save_dict = {}
        save_dict["pre_posemb_attention_scores"] = attention_scores.clone()

        if self.position_embedding_type == "relative_key":
            positional_embedding = self.distance_embedding.weight
            relative_position_scores = torch.einsum('lh, bche -> bcle', positional_embedding, query_layer.transpose(2,3))
            save_dict["relative_position_scores_1"] = relative_position_scores.clone()
            relative_position_scores = self.relativePositionEmbeddingRotate(relative_position_scores)
            save_dict["relative_position_scores_2"] = relative_position_scores.clone()
            attention_scores = attention_scores + relative_position_scores
            save_dict["attention_scores"] = attention_scores.clone()

            print("attention_scores", attention_scores.shape, attention_scores.sum(), attention_scores.std())

            save_dict["query_layer"] = query_layer.clone()
            save_dict["positional_embedding"] = positional_embedding.clone()
            save_dict["attention_scores"] = attention_scores.clone()

        # if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
        #     seq_length = hidden_states.size()[1]
        #     position_ids_l = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(-1, 1)
        #     position_ids_r = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(1, -1)
        #     distance = position_ids_l - position_ids_r
        #     positional_embedding = self.distance_embedding(distance + self.max_position_embeddings - 1)
        #     positional_embedding = positional_embedding.to(dtype=query_layer.dtype)  # fp16 compatibility


        #     if self.position_embedding_type == "relative_key":
        #         relative_position_scores = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
        #         attention_scores = attention_scores + relative_position_scores

            # elif self.position_embedding_type == "relative_key_query":
            #     relative_position_scores_query = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
            #     relative_position_scores_key = torch.einsum("bhrd,lrd->bhlr", key_layer, positional_embedding)
            #     attention_scores = attention_scores + relative_position_scores_query + relative_position_scores_key


        ###### attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        
        save_dict["post_posemb_attention_scores"] = attention_scores.clone()

        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in MCTCModel forward() function)
            print("attention_mask", attention_mask.shape, attention_mask.sum())
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        # ====> auto attn = dropout(softmax(scores, 1), pDropout);
        
        attention_probs = nn.functional.softmax(attention_scores, dim=-1)
        
        save_dict["attention_probs"] = attention_probs.clone()

        attention_probs = self.dropout(attention_probs)
        # attention_probs = attention_probs + 0.000013895

        # print("selfattn_attention_probs", attention_probs.shape, attention_probs.sum())

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask
            print(head_mask.shape, head_mask.sum())
        # print("selfattn_attention_probs_HEADS", attention_probs.shape, attention_probs.sum())
        # print("value_layer", value_layer.shape, value_layer.sum(), value_layer.std())

        # ====> auto result = matmul(attn.as(v.type()), v);
        context_layer = torch.matmul(attention_probs, value_layer)
        save_dict["context_layer"] = context_layer.clone()
        save_dict["value_layer"] = value_layer.clone()
        print("value_layer", value_layer.shape, value_layer.sum(), value_layer.std())
        print("context_layer", context_layer.shape, context_layer.sum(), context_layer.std())

        context_layer = context_layer.permute(0, 2, 1, 3).flatten(start_dim=-2)
        # new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        # context_layer = context_layer.view(*new_context_layer_shape)

        outputs = (context_layer, attention_scores, attention_probs, value_layer, save_dict) if output_attentions else (context_layer,)

        return outputs

class MCTCLayerNorm(nn.Module):
    def __init__(self):
        super().__init__()
        self.singleton_weight = Parameter(torch.ones(1))
        self.singleton_bias = Parameter(torch.zeros(1))
    def forward(self, hidden_states):
        return (hidden_states * self.singleton_weight) + self.singleton_bias

class MCTCSelfOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.dense = nn.Linear(config.hidden_size, config.hidden_size, bias=False)
        # self.dense.weight = self.dense.weight.transpose(0,1)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        # hidden_states = torch.matmul(self.dense.weight.clone().unsqueeze(0), hidden_states.transpose(1,2))
        # hidden_states = torch.einsum('hh, bhe -> bhe', self.dense.weight, hidden_states.transpose(-1, -2))
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        # hidden_states = hidden_states.transpose(1,2)
        print("SelfOUtput prenorm", hidden_states.shape, hidden_states.sum(), hidden_states.std())
        print("SelfOUtput input_tensor", input_tensor.shape, input_tensor.sum())
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        print("SelfOUtput postnorm", hidden_states.shape, hidden_states.sum())
        return hidden_states


class MCTCAttention(nn.Module):
    def __init__(self, config, position_embedding_type=None):
        super().__init__()
        self.self = MCTCSelfAttention(config, position_embedding_type=position_embedding_type)
        self.output = MCTCSelfOutput(config)
        self.pruned_heads = set()

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(
            heads, self.self.num_attention_heads, self.self.attention_head_size, self.pruned_heads
        )

        # Prune linear layers
        self.self.query = prune_linear_layer(self.self.query, index)
        self.self.key = prune_linear_layer(self.self.key, index)
        self.self.value = prune_linear_layer(self.self.value, index)
        self.output.dense = prune_linear_layer(self.output.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.self.num_attention_heads = self.self.num_attention_heads - len(heads)
        self.self.all_head_size = self.self.attention_head_size * self.self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        self_outputs = self.self(
            hidden_states,
            attention_mask,
            head_mask,
            encoder_hidden_states,
            encoder_attention_mask,
            past_key_value,
            output_attentions,
        )
        attention_output = self.output(self_outputs[0], hidden_states)
        # outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        
        # attention_output = self.output(self_outputs[0])
        outputs = (attention_output,) + self_outputs[1:]  # add at

        return outputs


class MCTCIntermediate(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.intermediate_size, bias=False)
        if isinstance(config.hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act
        
        # self.LayerNorm = MCTCLayerNorm()

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class MCTCOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size, bias=False)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        # self.LayerNorm = MCTCLayerNorm()
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class MCTCLayer(nn.Module):
    def __init__(self, config: MCTCConfig):
        super().__init__()

        """
        following conventions for now and adding this feed_forward_chunking utility, but not entirely sure what it does
        and what I'm supposed to be doing with this seq_len_dim. Why isn't this a config variable?
        """
        self.seq_len_dim = 1
        self.chunk_size_feed_forward = config.chunk_size_feed_forward

        self.intermediate = MCTCIntermediate(config)
        self.attention = MCTCAttention(config, position_embedding_type=config.position_embedding_type)
        self.is_decoder = config.is_decoder
        self.output = MCTCOutput(config)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        output_attentions=False,
    ):
        self_attention_outputs = self.attention(
            hidden_states, attention_mask, head_mask, output_attentions=output_attentions
        )
        attention_output = self_attention_outputs[0]
        outputs = self_attention_outputs[1:]  # add self attentions if we output attention weights

        layer_output = apply_chunking_to_forward(
            self.feed_forward_chunk, self.chunk_size_feed_forward, self.seq_len_dim, attention_output
        )

        outputs = (layer_output,) + outputs

        return outputs

    def feed_forward_chunk(self, attention_output):
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output


class MCTCPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = MCTCConfig
    load_tf_weights = load_tf_weights_in_mctc
    base_model_prefix = "mctc"
    main_input_name = "input_features"
    _keys_to_ignore_on_load_missing = [r"position_ids"]
    supports_gradient_checkpointing = True

    def _init_weights(self, module):
        """Initialize the weights"""
        std = self.config.initializer_range
        if isinstance(module, nn.Linear):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        elif isinstance(module, MCTCLayerNorm):
            module.singleton_weight.data.fill_(1.0)
            module.singleton_bias.data.zero_()
        if isinstance(module, (nn.Linear, nn.Conv1d)):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()

    def _get_feat_extract_output_lengths(self, input_lengths: torch.LongTensor):
        """
        Computes the output length of the convolutional layers
        """
        padding = 0
        dilation = 1
        for i, kernel_sz, stride in zip(
            range(self.config.num_conv_layers), self.config.conv_kernel, self.config.conv_stride
        ):
            input_lengths = ((input_lengths + 2 * padding - dilation * (kernel_sz - 1) - 1) // stride) + 1
            # input_lengths = input_lengths // self.config.conv_glu_dim
        return input_lengths

    def _get_feature_vector_attention_mask(self, feature_vector_length, attention_mask):
        # generate creates 3D attention mask, because of the shape of input_features
        # convert it to 2D if thats the case
        if len(attention_mask.shape) > 2:
            attention_mask = attention_mask[:, :, -1]

        # subsampled_lengths = attention_mask.sum(-1)
        subsampled_lengths = self._get_feat_extract_output_lengths(attention_mask.sum(-1))
        bsz = attention_mask.size()[0]
        attention_mask = torch.zeros(
            (bsz, feature_vector_length), dtype=attention_mask.dtype, device=attention_mask.device
        )

        # these two operations makes sure that all values
        # before the output lengths indices are attended to
        attention_mask[(torch.arange(bsz, device=attention_mask.device), subsampled_lengths - 1)] = 1
        attention_mask = attention_mask.flip([-1]).cumsum(-1).flip([-1]).long()
        return attention_mask

    def _set_gradient_checkpointing(self, module, value=False):
        if isinstance(module, (MCTCEncoder)):
            module.gradient_checkpointing = value


MCTC_START_DOCSTRING = r"""
    This model is a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) sub-class. Use
    it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage and
    behavior.

    Parameters:
        config ([`~MCTCConfig`]): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""

MCTC_INPUTS_DOCSTRING = r"""
    Args:
        input_features (`torch.LongTensor` of shape `({0})`):
            Indices of input sequence tokens in the vocabulary.

            Indices can be obtained using [`MCTCTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are input IDs?](../glossary#input-ids)
        attention_mask (`torch.FloatTensor` of shape `({0})`, *optional*):
            Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            [What are attention masks?](../glossary#attention-mask)
        token_type_ids (`torch.LongTensor` of shape `({0})`, *optional*):
            Segment token indices to indicate first and second portions of the inputs. Indices are selected in `[0,
            1]`:

            - 0 corresponds to a *sentence A* token,
            - 1 corresponds to a *sentence B* token.

            [What are token type IDs?](../glossary#token-type-ids)
        position_ids (`torch.LongTensor` of shape `({0})`, *optional*):
            Indices of positions of each input sequence tokens in the position embeddings. Selected in the range `[0,
            config.max_position_embeddings - 1]`.

            [What are position IDs?](../glossary#position-ids)
        head_mask (`torch.FloatTensor` of shape `(num_heads,)` or `(num_layers, num_heads)`, *optional*):
            Mask to nullify selected heads of the self-attention modules. Mask values selected in `[0, 1]`:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        inputs_embeds (`torch.FloatTensor` of shape `({0}, hidden_size)`, *optional*):
            Optionally, instead of passing `input_features` you can choose to directly pass an embedded representation.
            This is useful if you want more control over how to convert *input_features* indices into associated
            vectors than the model's internal embedding lookup matrix.
        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
            tensors for more detail.
        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~file_utils.ModelOutput`] instead of a plain tuple.
"""


class MCTCEncoder(MCTCPreTrainedModel):
    def __init__(self, config: MCTCConfig):
        super().__init__(config)

        # self.num_conv_layers = config.num_conv_layers
        # self.conv_kernel = config.conv_kernel
        # self.conv_stride = config.conv_stride
        # self.conv_dropout = config.conv_dropout
        # self.conv_glu_dim = config.conv_glu_dim

        self.hidden_dropout_prob = config.hidden_dropout_prob

        # self.layer_norm = nn.LayerNorm(config.input_feat_per_channel)
        self.layer_norm = MCTCLayerNorm()
        self.conv = Conv1dSubsampler(config)
        self.layers = nn.ModuleList([MCTCLayer(config) for _ in range(config.num_hidden_layers)])

        self.gradient_checkpointing = False

    def forward(
        self,
        input_features,
        attention_mask,
        head_mask,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=True,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        input_features = self.layer_norm(input_features)
        
        inputs_embeds = self.conv(input_features)

        # inputs_embeds = self.embed_scale * inputs_embeds

        # subsample attention mask if necessary
        if attention_mask is not None:
            attention_mask = self._get_feature_vector_attention_mask(inputs_embeds.shape[1], attention_mask)
            # padding_mask = attention_mask.ne(1).long()
        else:
            # padding_mask = torch.zeros(inputs_embeds.shape[:2], dtype=torch.long, device=inputs_embeds.device)
            pass

        hidden_states = inputs_embeds
        # embed_pos = self.embed_positions(padding_mask)
        # hidden_states = inputs_embeds + embed_pos
        hidden_states = nn.functional.dropout(hidden_states, p=self.hidden_dropout_prob, training=self.training)

        # expand attention_mask
        if attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            attention_mask = _expand_mask(attention_mask, inputs_embeds.dtype)

        encoder_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None

        # check if head_mask has a correct number of layers specified if desired
        if head_mask is not None:
            assert head_mask.size()[0] == (
                len(self.layers)
            ), f"The head_mask should be specified for {len(self.layers)} layers, but it is for {head_mask.size()[0]}."

        deepspeed_zero3_is_enabled = is_deepspeed_zero3_enabled()
        for idx, encoder_layer in enumerate(self.layers):
            if output_hidden_states:
                encoder_states = encoder_states + (hidden_states,)
            # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            dropout_probability = random.uniform(0, 1)

            skip_the_layer = True if self.training and (dropout_probability < self.config.layerdrop) else False
            if not skip_the_layer or deepspeed_zero3_is_enabled:
                # under deepspeed zero3 all gpus must run in sync
                if self.gradient_checkpointing and self.training:

                    def create_custom_forward(module):
                        def custom_forward(*inputs):
                            return module(*inputs, output_attentions)

                        return custom_forward

                    layer_outputs = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(encoder_layer),
                        hidden_states,
                        attention_mask,
                        # (head_mask[idx] if head_mask is not None else None),
                    )
                else:
                    layer_outputs = encoder_layer(
                        hidden_states=hidden_states,
                        attention_mask=attention_mask,
                        output_attentions=output_attentions,
                    )

                hidden_states = layer_outputs[0]

            if skip_the_layer:
                layer_outputs = (None, None)

            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[1],)
        # hidden_states = self.layer_norm(hidden_states)
        if output_hidden_states:
            encoder_states = encoder_states + (hidden_states,)

        if not return_dict:
            return tuple(v for v in [hidden_states, encoder_states, all_attentions] if v is not None)
        return BaseModelOutput(
            last_hidden_state=hidden_states, hidden_states=encoder_states, attentions=all_attentions
        )


@add_start_docstrings(
    "The bare mCTC Model transformer outputting raw hidden-states without any specific head on top.",
    MCTC_START_DOCSTRING,
)
class MCTCModel(MCTCPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.config = config

        self.encoder = MCTCEncoder(config)

        # Initialize weights and apply final processing
        self.post_init()

    @add_start_docstrings_to_model_forward(MCTC_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @add_code_sample_docstrings(
        processor_class=_TOKENIZER_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=BaseModelOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_features,
        attention_mask=None,
        head_mask=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # shouldn't this just be use_cache=False if we know this model is
        # an encoder only model?
        # if so, modeling_bert.py line 936 also needs a similar adjustment.
        if self.config.is_decoder:
            use_cache = use_cache if use_cache is not None else self.config.use_cache
        else:
            use_cache = False

        if input_features is None:
            raise ValueError("You have to specify input_features.")

        encoder_outputs = self.encoder(
            input_features,
            attention_mask=attention_mask,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        sequence_output = encoder_outputs[0]

        if not return_dict:
            return (sequence_output,) + encoder_outputs[1:]

        return BaseModelOutput(
            last_hidden_state=sequence_output,
            hidden_states=encoder_outputs.hidden_states,
            attentions=encoder_outputs.attentions,
        )


@add_start_docstrings(
    """MCTC Model with a `language modeling` head on top for Connectionist Temporal Classification (CTC).""",
    MCTC_START_DOCSTRING,
)
class MCTCForCTC(MCTCPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.mctc = MCTCModel(config)

        if config.vocab_size is None:
            raise ValueError(
                f"You are trying to instantiate {self.__class__} with a configuration that "
                "does not define the vocabulary size of the language model head. Please "
                "instantiate the model as follows: `MCTCForCTC.from_pretrained(..., vocab_size=vocab_size)`. "
                "or define `vocab_size` of your model's configuration."
            )
        output_hidden_size = config.hidden_size

        self.ctc_head = nn.Linear(output_hidden_size, config.vocab_size)

        # Initialize weights and apply final processing
        self.post_init()

    @add_start_docstrings_to_model_forward(MCTC_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        processor_class=_PROCESSOR_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=CausalLMOutput,
        config_class=_CONFIG_FOR_DOC,
        expected_output=_CTC_EXPECTED_OUTPUT,
        expected_loss=_CTC_EXPECTED_LOSS,
    )
    def forward(
        self,
        input_features,
        attention_mask=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        labels=None,
    ):
        r"""
        labels (`torch.LongTensor` of shape `(batch_size, target_length)`, *optional*):
            Labels for connectionist temporal classification. Note that `target_length` has to be smaller or equal to
            the sequence length of the output logits. Indices are selected in `[-100, 0, ..., config.vocab_size - 1]`.
            All labels set to `-100` are ignored (masked), the loss is only computed for labels in `[0, ...,
            config.vocab_size - 1]`.
        """

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        outputs = self.mctc(
            input_features,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]

        logits = self.ctc_head(hidden_states)

        loss = None
        if labels is not None:

            if labels.max() >= self.config.vocab_size:
                raise ValueError(f"Label values must be <= vocab_size: {self.config.vocab_size}")

            # retrieve loss input_lengths from attention_mask
            attention_mask = (
                attention_mask
                if attention_mask is not None
                else torch.ones(input_features.shape[:-1], dtype=torch.long)
            )
            input_lengths = self._get_feat_extract_output_lengths(attention_mask.sum(-1)).to(torch.long)
            # assuming that padded tokens are filled with -100
            # when not being attended to
            labels_mask = labels >= 0
            target_lengths = labels_mask.sum(-1)
            flattened_targets = labels.masked_select(labels_mask)

            # ctc_loss doesn't support fp16
            log_probs = nn.functional.log_softmax(logits, dim=-1, dtype=torch.float32).transpose(0, 1)

            with torch.backends.cudnn.flags(enabled=False):
                loss = nn.functional.ctc_loss(
                    log_probs,
                    flattened_targets,
                    input_lengths,
                    target_lengths,
                    blank=self.config.pad_token_id,
                    reduction=self.config.ctc_loss_reduction,
                    zero_infinity=self.config.ctc_zero_infinity,
                )

        if not return_dict:
            output = (logits,) + outputs[_HIDDEN_STATES_START_POSITION:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutput(
            loss=loss, logits=logits, hidden_states=outputs.hidden_states, attentions=outputs.attentions
        )


# class MCTCClassificationHead(nn.Module):
#     """Head for sentence-level classification tasks."""

#     def __init__(self, config):
#         super().__init__()
#         self.dense = nn.Linear(config.hidden_size, config.hidden_size)
#         self.dropout = nn.Dropout(config.hidden_dropout_prob)
#         self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

#         self.config = config

#     def forward(self, features, **kwargs):
#         x = features[:, 0, :]  # take <s> token (equiv. to [CLS])
#         x = self.dropout(x)
#         x = self.dense(x)
#         x = ACT2FN[self.config.hidden_act](x)
#         x = self.dropout(x)
#         x = self.out_proj(x)
#         return x
