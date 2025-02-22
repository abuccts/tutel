# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import TYPE_CHECKING, Any, Optional, Tuple, Union, cast

import torch
from torch import Tensor

from .jit_compiler import IS_HIP_EXTENSION
from ..jit_kernels import sparse as jit_kernel
from ..jit_kernels.gating import fast_cumsum_sub_one

class GatingEncoder(torch.autograd.Function):
    @staticmethod
    def forward(ctx: Any, config: Any, reshaped_input: Tensor, *gates_):
        ctx.reshaped_input = reshaped_input
        ctx.config = config
        if gates_:
          ctx.gates_h2 = [x.view(-1, 1).repeat(1, 2) if x.dtype == torch.float16 else x for x in gates_]
        else:
          ctx.gates_h2 = [ctx.config.ones_helper] * len(ctx.config.indices_)

        dispatched_input = torch.zeros([ctx.config.num_global_experts * ctx.config.capacity, ctx.config.model_dim], dtype=reshaped_input.dtype, device=reshaped_input.device)
        for g, i, l in zip(ctx.gates_h2, ctx.config.indices_, ctx.config.locations_):
          ctx.config.func_fwd(g, i, l, reshaped_input, dispatched_input, extra=[ctx.config.indices_[0].size(0), ctx.config.aligned_dim, ctx.config.capacity])
        return dispatched_input

    @staticmethod
    def backward(ctx: Any, dispatched_input: Tensor):
        dispatched_input = dispatched_input.contiguous()
        last_result = None
        for g, i, l in zip(ctx.gates_h2, ctx.config.indices_, ctx.config.locations_):
          grad_data = torch.empty(ctx.reshaped_input.shape, dtype=dispatched_input.dtype, device=dispatched_input.device)
          ctx.config.func_bwd_data(g, i, l, grad_data, dispatched_input, extra=[ctx.config.indices_[0].size(0), ctx.config.aligned_dim, ctx.config.capacity])
          last_result = grad_data if last_result is None else last_result + grad_data

        grad_gates = []
        if id(ctx.gates_h2[0]) != id(ctx.config.ones_helper):
          for i, l in zip(ctx.config.indices_, ctx.config.locations_):
            grad_gates1_s = torch.empty([ctx.config.sample_size,], dtype=dispatched_input.dtype, device=dispatched_input.device)
            ctx.config.func_bwd_gate(grad_gates1_s, i, l, ctx.reshaped_input, dispatched_input, extra=[ctx.config.indices_[0].size(0), ctx.config.aligned_dim, ctx.config.capacity])
            grad_gates.append(grad_gates1_s)
        return (None, last_result, *grad_gates)


class GatingDecoder(torch.autograd.Function):
    @staticmethod
    def forward(ctx: Any, config: Any, expert_output: Tensor, *gates_):
        ctx.expert_output = expert_output
        ctx.config = config
        if gates_:
          ctx.gates_h2 = [x.view(-1, 1).repeat(1, 2) if x.dtype == torch.float16 else x for x in gates_]
        else:
          ctx.gates_h2 = [ctx.config.ones_helper] * len(ctx.config.indices_)

        last_result = None
        for g, i, l in zip(ctx.gates_h2, ctx.config.indices_, ctx.config.locations_):
          single_output = torch.empty([config.sample_size, config.model_dim], dtype=expert_output.dtype, device=expert_output.device)
          config.func_bwd_data(g, i, l, single_output, expert_output, extra=[ctx.config.indices_[0].size(0), ctx.config.aligned_dim, ctx.config.capacity])
          last_result = single_output if last_result is None else last_result + single_output
        return last_result

    @staticmethod
    def backward(ctx: Any, combined_output: Tensor):
        combined_output = combined_output.contiguous()
        grad_expert_output = torch.zeros(ctx.expert_output.shape, dtype=combined_output.dtype, device=combined_output.device)
        for g, i, l in zip(ctx.gates_h2, ctx.config.indices_, ctx.config.locations_):
          ctx.config.func_fwd(g, i, l, combined_output, grad_expert_output, extra=[ctx.config.indices_[0].size(0), ctx.config.aligned_dim, ctx.config.capacity])

        grad_gates = []
        if id(ctx.gates_h2[0]) != id(ctx.config.ones_helper):
          for i, l in zip(ctx.config.indices_, ctx.config.locations_):
            grad_gates1_s = torch.empty([ctx.config.sample_size,], dtype=combined_output.dtype, device=combined_output.device)
            ctx.config.func_bwd_gate(grad_gates1_s, i, l, combined_output, ctx.expert_output, extra=[ctx.config.indices_[0].size(0), ctx.config.aligned_dim, ctx.config.capacity])
            grad_gates.append(grad_gates1_s)
        return (None, grad_expert_output, *grad_gates)


class TutelMoeFastDispatcher:

    kernel_pool = dict()

    def __init__(self, num_global_experts, capacity, model_dim, dispatch_dtype):
        self.num_global_experts = int(num_global_experts)
        self.capacity = int(capacity)
        self.model_dim = int(model_dim)
        self.dtype = dispatch_dtype
        if IS_HIP_EXTENSION or dispatch_dtype != torch.float16:
            self.dtype = torch.float32
        self.original_dtype = dispatch_dtype
        self.aligned_dim = model_dim // (2 if self.dtype == torch.float16 else 1)
        self.is_cuda = None

    def update(self, indices_, locations_, gates_, capacity=None, is_postscore=True):
        self.indices_ = [x.to(torch.int32).view(-1) for x in indices_]
        self.locations_ = [x.to(torch.int32) for x in locations_]
        self.gates_ = [x.to(self.dtype) for x in gates_]
        self.is_postscore = is_postscore
        self.sample_size, self.capacity = int(self.indices_[0].size(0)), int(capacity) or self.capacity

        if self.is_cuda != indices_[0].is_cuda:
            self.is_cuda = indices_[0].is_cuda
            if self.is_cuda not in TutelMoeFastDispatcher.kernel_pool:
                self.func_fwd = jit_kernel.create_forward(self.dtype, indices_[0].is_cuda)
                self.func_bwd_data = jit_kernel.create_backward_data(self.dtype, indices_[0].is_cuda)
                self.func_bwd_gate = jit_kernel.create_backward_gate(self.dtype, indices_[0].is_cuda)
                self.ones_helper = torch.ones([self.sample_size, 2], dtype=self.dtype, device=self.indices_[0].device)
                TutelMoeFastDispatcher.kernel_pool[self.is_cuda] = self.func_fwd, self.func_bwd_data, self.func_bwd_gate, self.ones_helper
            else:
                self.func_fwd, self.func_bwd_data, self.func_bwd_gate, self.ones_helper = TutelMoeFastDispatcher.kernel_pool[self.is_cuda]

    def encode(self, data):
        if self.is_postscore:
            return GatingEncoder.apply(self, data.to(self.dtype)).to(self.original_dtype)
        else:
            return GatingEncoder.apply(self, data.to(self.dtype), *self.gates_).to(self.original_dtype)

    def decode(self, data):
        if self.is_postscore:
            return GatingDecoder.apply(self, data.to(self.dtype), *self.gates_).to(self.original_dtype)
        else:
            return GatingDecoder.apply(self, data.to(self.dtype)).to(self.original_dtype)

fast_dispatcher = TutelMoeFastDispatcher

def one_hot_with_dtype(data, num_classes, dtype):
    result = torch.zeros([data.size(0), num_classes], device=data.device, dtype=dtype)
    result.scatter_(1, data.unsqueeze(-1), 1)
    return result

def compute_sorted_location(x, importance_scores):
    sorted_x = x[importance_scores.argsort(dim=0)]
    sorted_cumsum = fast_cumsum_sub_one(sorted_x) * sorted_x
    return sorted_cumsum[importance_scores.argsort(dim=0).argsort(dim=0)]

def load_balance(gates, mask1, num_global_experts, fp32_gate):
    if gates.dtype == torch.float32 or fp32_gate:
        me = torch.sum(gates.float(), dim=0)
        ce = torch.sum(mask1.to(me.dtype), dim=0)
        l_loss = torch.sum(me * ce) * (num_global_experts / (gates.size(0) * gates.size(0)))
    else:
        me = torch.mean(gates, dim=0)
        ce = torch.mean(mask1.to(gates.dtype), dim=0)
        l_loss = torch.sum(me * ce) * num_global_experts
    return l_loss

def extract_critical(gates, top_k, capacity_factor=1.0, fp32_gate=False, batch_prioritized_routing=False):
    topk_indices = torch.topk(gates, top_k, dim=1).indices
    num_global_experts = gates.size(1)

    indices_s = [x.view(-1) for x in topk_indices.chunk(top_k, dim=1)]
    masks_se = [one_hot_with_dtype(x, num_classes=num_global_experts, dtype=x.dtype) for x in indices_s]
    gates_s = [(gates * x).sum(dim=1) for x in masks_se]

    l_loss = load_balance(gates, masks_se[0], num_global_experts, fp32_gate)

    if batch_prioritized_routing:
        importance_scores = -1 * gates.max(dim=1)[0]
        compute_location = lambda x: compute_sorted_location(x, importance_scores)
    else:
        compute_location = fast_cumsum_sub_one

    locations1 = compute_location(masks_se[0])

    locations_s = [torch.sum(locations1 * masks_se[0], dim=1).to(torch.int32)]

    if top_k > 1:
        acc_base = None
        for k in range(1, top_k):
            acc_base = torch.sum(masks_se[k - 1], dim=0, keepdim=True) if acc_base is None else acc_base + torch.sum(masks_se[k - 1], dim=0, keepdim=True)
            locations2 = compute_location(masks_se[k])
            locations2 += acc_base
            locations_s.append(torch.sum(locations2 * masks_se[k], dim=1).to(torch.int32))

        # Normalize Gate
        denom_s = torch.clamp(sum(gates_s), min=torch.finfo(gates_s[0].dtype).eps)
        gates_s = [x / denom_s for x in gates_s]

    indices_s = [x.to(torch.int32) for x in indices_s]

    capacity = top_k * int(capacity_factor * ((gates.size(0) + num_global_experts - 1) // num_global_experts))
    return (num_global_experts, indices_s, locations_s, gates_s, capacity), l_loss


def fast_encode(data, critial_data, is_postscore=True):
    num_global_experts = critial_data[0]
    dispatcher = TutelMoeFastDispatcher(num_global_experts, 0, data.size(-1), data.dtype)
    dispatcher.update(*critial_data[1:], is_postscore=is_postscore)
    return dispatcher.encode(data)

def fast_decode(data, critial_data, is_postscore=True):
    num_global_experts = critial_data[0]
    dispatcher = TutelMoeFastDispatcher(num_global_experts, 0, data.size(-1), data.dtype)
    dispatcher.update(*critial_data[1:], is_postscore=is_postscore)
    return dispatcher.decode(data)

