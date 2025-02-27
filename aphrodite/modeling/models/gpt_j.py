# coding=utf-8
# Adapted from
# https://github.com/huggingface/transformers/blob/v4.28.0/src/transformers/models/gptj/modeling_gptj.py
# Copyright 2023 The PygmalionAI team.
# Copyright 2023 The vLLM team.
# Copyright 2021 The EleutherAI and HuggingFace Teams. All rights reserved.
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
"""Inference-only GPT-J model compatible with HuggingFace weights.

The input of the model is flattened to a 1D tensor of tokens. The model uses
InputMetadata to extract the original 2D shape of the input.
"""
from typing import List, Optional, Tuple

import torch
from torch import nn
from transformers import GPTJConfig

from aphrodite.modeling.metadata import InputMetadata
from aphrodite.modeling.layers.activation import get_act_fn
from aphrodite.modeling.layers.attention import PagedAttentionWithRoPE
from aphrodite.modeling.layers.sampler import Sampler
from aphrodite.modeling.layers.quantized_linear import ParallelLinear
from aphrodite.modeling.quantization_utils import QuantizationConfig
from aphrodite.modeling.hf_downloader import (hf_model_weights_iterator,
                                              load_tensor_parallel_weights,
                                              convert_pyslice_to_tensor,
                                              get_parallel_weight)
from aphrodite.modeling.megatron.parallel_state import (
    get_tensor_model_parallel_rank, get_tensor_model_parallel_world_size)
from aphrodite.modeling.megatron.tensor_parallel import (
    VocabParallelEmbedding)
from aphrodite.common.sequence import SamplerOutput

KVCache = Tuple[torch.Tensor, torch.Tensor]


class GPTJAttention(nn.Module):

    def __init__(self,
                 config: GPTJConfig,
                 quant_config: Optional[QuantizationConfig] = None):
        super().__init__()
        self.total_num_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.head_size = self.hidden_size // self.total_num_heads

        self.qkv_proj = ParallelLinear.column(config.hidden_size,
                                              3 * config.hidden_size,
                                              bias=False,
                                              gather_output=False,
                                              perform_initialization=False,
                                              quant_config=quant_config)
        self.out_proj = ParallelLinear.row(config.hidden_size,
                                           config.hidden_size,
                                           bias=False,
                                           input_is_parallel=True,
                                           perform_initialization=False,
                                           quant_config=quant_config)

        tp_world_size = get_tensor_model_parallel_world_size()
        assert self.total_num_heads % tp_world_size == 0
        self.num_heads = self.total_num_heads // tp_world_size

        scaling = self.head_size**-0.5
        assert getattr(config, "rotary", True)
        assert config.rotary_dim % 2 == 0
        rope_theta = getattr(config, "rope_theta", 10000)
        max_position_embeddings = getattr(config, "max_position_embeddings",
                                          8192)
        self.attn = PagedAttentionWithRoPE(
            self.num_heads,
            self.head_size,
            scaling,
            config.rotary_dim,
            base=rope_theta,
            max_position=max_position_embeddings,
            is_neox_style=False)
        self.warmup = False

    def forward(
        self,
        position_ids: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
        cache_event: Optional[torch.cuda.Event],
    ) -> torch.Tensor:
        qkv, _ = self.qkv_proj(hidden_states)
        q, k, v = qkv.chunk(chunks=3, dim=-1)
        k_cache, v_cache = kv_cache
        attn_output = self.attn(position_ids, q, k, v, k_cache, v_cache,
                                input_metadata, cache_event)
        attn_output, _ = self.out_proj(attn_output)
        return attn_output


class GPTJMLP(nn.Module):

    def __init__(self,
                 intermediate_size: int,
                 config: GPTJConfig,
                 quant_config: Optional[QuantizationConfig] = None):
        super().__init__()
        hidden_size = config.n_embd
        self.fc_in = ParallelLinear.column(hidden_size,
                                           intermediate_size,
                                           gather_output=False,
                                           perform_initialization=False,
                                           quant_config=quant_config)
        self.fc_out = ParallelLinear.row(intermediate_size,
                                         hidden_size,
                                         input_is_parallel=True,
                                         perform_initialization=False,
                                         quant_config=quant_config)
        self.act = get_act_fn(config.activation_function)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states, _ = self.fc_in(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states, _ = self.fc_out(hidden_states)
        return hidden_states


class GPTJBlock(nn.Module):

    def __init__(self,
                 config: GPTJConfig,
                 quant_config: Optional[QuantizationConfig] = None):
        super().__init__()
        if config.n_inner is None:
            inner_dim = 4 * config.n_embd
        else:
            inner_dim = config.n_inner
        self.ln_1 = nn.LayerNorm(config.n_embd, eps=config.layer_norm_epsilon)
        self.attn = GPTJAttention(config, quant_config)
        self.mlp = GPTJMLP(inner_dim, config, quant_config)

    def forward(
        self,
        position_ids: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
        cache_event: Optional[torch.cuda.Event],
    ) -> torch.Tensor:
        residual = hidden_states
        hidden_states = self.ln_1(hidden_states)
        attn_output = self.attn(
            position_ids=position_ids,
            hidden_states=hidden_states,
            kv_cache=kv_cache,
            input_metadata=input_metadata,
            cache_event=cache_event,
        )
        mlp_output = self.mlp(hidden_states)
        hidden_states = attn_output + mlp_output + residual
        return hidden_states


class GPTJModel(nn.Module):

    def __init__(self,
                 config: GPTJConfig,
                 quant_config: Optional[QuantizationConfig] = None):
        super().__init__()
        self.config = config
        self.embed_dim = config.n_embd
        self.wte = VocabParallelEmbedding(config.vocab_size,
                                          self.embed_dim,
                                          perform_initialization=False)
        self.h = nn.ModuleList(
            [GPTJBlock(config, quant_config) for _ in range(config.n_layer)])
        self.ln_f = nn.LayerNorm(self.embed_dim, eps=config.layer_norm_epsilon)

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_events: Optional[List[torch.cuda.Event]],
    ) -> torch.Tensor:
        hidden_states = self.wte(input_ids)
        for i in range(len(self.h)):
            if cache_events is None:
                cache_event = None
            else:
                cache_event = cache_events[i]
            layer = self.h[i]
            hidden_states = layer(
                position_ids,
                hidden_states,
                kv_caches[i],
                input_metadata,
                cache_event,
            )
        hidden_states = self.ln_f(hidden_states)
        return hidden_states


class GPTJForCausalLM(nn.Module):

    def __init__(self,
                 config: GPTJConfig,
                 quant_config: Optional[QuantizationConfig] = None):
        super().__init__()
        self.config = config
        self.quant_config = quant_config
        assert not config.tie_word_embeddings
        self.transformer = GPTJModel(config, quant_config)
        self.lm_head = ParallelLinear.column(config.n_embd,
                                             config.vocab_size,
                                             gather_output=False,
                                             perform_initialization=False,
                                             quant_config=None)
        self.sampler = Sampler(config.vocab_size)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_events: Optional[List[torch.cuda.Event]],
    ) -> SamplerOutput:
        hidden_states = self.transformer(input_ids, positions, kv_caches,
                                         input_metadata, cache_events)
        next_tokens = self.sampler(self.lm_head.weight, hidden_states,
                                   input_metadata, self.lm_head.bias)
        return next_tokens

    column_parallel_layers = ["fc_in", "lm_head"]
    row_parallel_layers = ["out_proj", "fc_out"]
    parallel_vocab_layers = ["wte", "lm_head"]

    def load_weights(self,
                     model_name_or_path: str,
                     cache_dir: Optional[str] = None,
                     load_format: str = "auto",
                     revision: Optional[str] = None):
        (column_parallel_weights, row_parallel_weights,
         ignore_weight_suffixes) = get_parallel_weight(self)
        tp_rank = get_tensor_model_parallel_rank()
        state_dict = self.state_dict()
        for name, loaded_weight in hf_model_weights_iterator(
                model_name_or_path, cache_dir, load_format, revision):
            if "attn.bias" in name or "attn.masked_bias" in name:
                continue
            if any(name.endswith(suffix) for suffix in ignore_weight_suffixes):
                continue

            is_transposed = False
            if self.quant_config is not None:
                is_transposed = self.quant_config.is_transposed(name)
            if is_transposed:
                loaded_weight = convert_pyslice_to_tensor(loaded_weight)
                loaded_weight = loaded_weight.T

            is_attention_weight = False
            for stride_id, att_weight_name in enumerate(
                ["q_proj", "k_proj", "v_proj"]):
                if att_weight_name not in name:
                    continue
                name = name.replace(att_weight_name, "qkv_proj")
                if "g_idx" in name or name not in state_dict:
                    break
                param = state_dict[name]
                if is_transposed:
                    param = param.T
                shard_size = param.shape[0] // 3
                loaded_weight = loaded_weight[shard_size * tp_rank:shard_size *
                                              (tp_rank + 1)]
                param_slice = param.data[shard_size * stride_id:shard_size *
                                         (stride_id + 1)]
                assert param_slice.shape == loaded_weight.shape
                param_slice.copy_(loaded_weight)
                is_attention_weight = True
                break
            if is_attention_weight:
                continue

            if name not in state_dict:
                continue

            param = state_dict[name]
            if is_transposed:
                param = param.T

            load_tensor_parallel_weights(param, loaded_weight, name,
                                         column_parallel_weights,
                                         row_parallel_weights, tp_rank)