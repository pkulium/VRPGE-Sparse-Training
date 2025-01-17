import os
import pathlib
import shutil
import math
import torch
import torch.nn as nn
from args import args as parser_args
from typing import Union, Dict, Tuple
import numpy as np
import random

TensorType = Union[torch.Tensor, np.ndarray]
N, M = 2, 4

DEBUG = False
def save_checkpoint(state, is_best, filename="checkpoint.pth", save=False, finetune=False):
    filename = pathlib.Path(filename)
    if not filename.parent.exists():
        os.makedirs(filename.parent)
    torch.save(state, filename)
    if is_best:
        if finetune:
            shutil.copyfile(filename, str(filename.parent / "model_best_finetune.pth"))
        else:
            shutil.copyfile(filename, str(filename.parent / "model_best.pth"))
        if not save:
            os.remove(filename)

def get_lr(optimizer):
    return optimizer.param_groups[0]["lr"]

def freeze_model_weights(model):
    print("=> Freezing model weights")
    for n, m in model.named_modules():
        if hasattr(m, "weight") and m.weight is not None:
            print(f"==> No gradient to {n}.weight")
            m.weight.requires_grad = False
            if m.weight.grad is not None:
                print(f"==> Setting gradient of {n}.weight to None")
                m.weight.grad = None

            if hasattr(m, "bias") and m.bias is not None:
                print(f"==> No gradient to {n}.bias")
                m.bias.requires_grad = False

                if m.bias.grad is not None:
                    print(f"==> Setting gradient of {n}.bias to None")
                    m.bias.grad = None

def freeze_model_subnet(model):
    print("=> Freezing model subnet")
    for n, m in model.named_modules():
        if hasattr(m, "scores"):
            m.scores.requires_grad = False
            print(f"==> No gradient to {n}.scores")
            if m.scores.grad is not None:
                print(f"==> Setting gradient of {n}.scores to None")
                m.scores.grad = None

def fix_model_subnet(model):
    for n, m in model.named_modules():
        if hasattr(m, "scores"):
            if m.prune:
                m.fix_subnet()
                m.train_weights = True

def unfreeze_model_weights(model):
    print("=> Unfreezing model weights")
    for n, m in model.named_modules():
        if hasattr(m, "weight") and m.weight is not None:
            print(f"==> Gradient to {n}.weight")
            m.weight.requires_grad = True
            if hasattr(m, "bias") and m.bias is not None:
                print(f"==> Gradient to {n}.bias")
                m.bias.requires_grad = True

def unfreeze_model_subnet(model):
    print("=> Unfreezing model subnet")
    for n, m in model.named_modules():
        if hasattr(m, "scores"):
            print(f"==> Gradient to {n}.scores")
            m.scores.requires_grad = True

def set_model_prune_rate(model, prune_rate):
    print(f"==> Setting prune rate of network to {prune_rate}")
    for n, m in model.named_modules():
        if hasattr(m, "set_prune_rate"):
            m.set_prune_rate(prune_rate)
            print(f"==> Setting prune rate of {n} to {prune_rate}")

def solve_v(x):
    k = x.nelement() * parser_args.prune_rate
    def f(v):
        return (x - v).clamp(0, 1).sum() - k
    if f(0) < 0:
        return 0, 0
    a, b = 0, x.max()
    itr = 0
    while (1):
        itr += 1
        v = (a + b) / 2
        obj = f(v)
        if abs(obj) < 1e-3 or itr > 20:
            break
        if obj < 0:
            b = v
        else:
            a = v
    v = max(0, v)
    return v, itr


def solve_v_total(model, total):
    k = total * parser_args.prune_rate
    a, b = 0, 0
    for n, m in model.named_modules():
        if hasattr(m, "scores") and m.prune:
            b = max(b, m.scores.max())
    def f(v):
        s = 0
        for n, m in model.named_modules():
            if hasattr(m, "scores") and m.prune:
                s += (m.scores - v).clamp(0, 1).sum()
        return s - k
    if f(0) < 0:
        return 0, 0
    itr = 0
    while (1):
        itr += 1
        v = (a + b) / 2
        obj = f(v)
        if abs(obj) < 1e-3 or itr > 20:
            break
        if obj < 0:
            b = v
        else:
            a = v
    v = max(0, v)
    return v, itr


def constrainScore(model, args, v_meter, max_score_meter):
    for n, m in model.named_modules():
        if hasattr(m, "scores"):
            if args.center:
                m.scores.clamp_(-0.5, 0.5)
            else:
                max_score_meter.update(m.scores.max())
                v, itr = solve_v(m.scores)
                v_meter.update(v)
                m.scores.sub_(v).clamp_(0, 1)

def constrainScoreByWhole(model, v_meter, max_score_meter):
    total = 0
    for n, m in model.named_modules():
        if hasattr(m, "scores"):
            if m.prune:
                total += m.scores.nelement()
                max_score_meter.update(m.scores.max())
    v, itr = solve_v_total(model, total)
    v_meter.update(v)
    for n, m in model.named_modules():
        if hasattr(m, "scores"):
            if m.prune:
                m.scores.sub_(v).clamp_(0, 1)

def maskNxM(
    parameter: TensorType,
    n: int,
    m: int
) -> TensorType:
    """
    Accepts either a torch.Tensor or numpy.ndarray and generates a floating point mask of 1's and 0's
    corresponding to the locations that should be retained for NxM pruning. The appropriate ranking mechanism
    should already be built into the parameter when this method is called.
    """

    if type(parameter) is torch.Tensor:
        out_neurons, in_neurons = parameter.size()

        with torch.no_grad():
            groups = parameter.reshape(out_neurons, -1, n)
            zeros = torch.zeros(1, 1, 1, device=parameter.device)
            ones = torch.ones(1, 1, 1, device=parameter.device)

            percentile = m / n
            quantiles = torch.quantile(groups, percentile, -1, keepdim=True)
            mask = torch.where(groups > quantiles, ones, zeros).reshape(out_neurons, in_neurons)
        # with torch.no_grad():
        #     groups = parameter.reshape(out_neurons, -1, n)
        #     zeros = torch.zeros_like(groups)
        #     ones = torch.ones_like(groups)

        #     percentile = m / n
        #     quantiles = torch.quantile(groups, percentile, -1, keepdim=True)
        #     initial_mask = torch.where(groups > quantiles, ones, zeros).reshape(out_neurons, in_neurons)
        #     print(f'initial_mask:{initial_mask}')
        #     # Count ones in each group 
        #     ones_count = initial_mask.sum(dim=-1)

        #     for i in range(out_neurons):
        #         shortfall = m - int(ones_count[i].item())  # Convert to int
        #         if shortfall > 0:
        #             # Find indices where the group is equal to the quantile and currently zero in the mask
        #             tie_indices = (groups[i] == quantiles[i]) & (initial_mask[i] == 0)
        #             tie_indices_list = tie_indices.nonzero(as_tuple=False).reshape(-1).tolist()

        #             # Check if the shortfall is not greater than the number of available indices
        #             # shortfall = min(shortfall, len(tie_indices_list))

        #             # Randomly select indices to fill the shortfall
        #             selected_indices = random.sample(tie_indices_list, shortfall)
        #             initial_mask[i, selected_indices] = 1

        #     # Reshape the mask back to original dimensions
        #     mask = initial_mask
    else:
        out_neurons, in_neurons = parameter.shape
        percentile = (100 * m) / n

        groups = parameter.reshape(out_neurons, -1, n)
        group_thresholds = np.percentile(groups, percentile, axis=-1, keepdims=True)
        mask = (groups > group_thresholds).astype(np.float32).reshape(out_neurons, in_neurons)

    return mask

def flatten_and_reshape(z, M):
    """
    Flatten z and reshape it into a 2D tensor with columns divisible by M.
    """
    num_elements = z.numel()
    num_rows = num_elements // M
    return z.flatten()[:num_rows * M].view(num_rows, M)

def get_n_m_sparse_matrix(w):
    with torch.no_grad():
        length = w.numel()
        group = int(length / M)
        w_tmp = w.t().detach().abs().reshape(group, M)
        index = torch.argsort(w_tmp, dim=1)[:, :int(M - N)]
        mask = torch.ones(w_tmp.shape, device=w_tmp.device)
        mask = mask.scatter_(dim=1, index=index, value=0).reshape(w.t().shape).t()
    if DEBUG:
        print(f'w:{w}')
        print(f'mask:{mask}')
    return mask

def admm_solve(z, N, M, rho=1, max_iter=100, tol=1e-4):
    z_flattened = flatten_and_reshape(z, M)
    if DEBUG:
        print(f'z_flattened:{z_flattened}')
    n, m = z_flattened.shape
    s = torch.zeros_like(z_flattened)
    W = torch.zeros_like(z_flattened)
    u = torch.zeros_like(z_flattened)

    for _ in range(max_iter):
        # Update s
        s = (z_flattened + rho * (W - u)) / (1 + rho)

        # Update W
        W_new = s + u
        scores = W_new.abs()
        mask = maskNxM(scores, M, N)
        W = mask * W_new

        # Update u
        u += s - W

        # Check for convergence
        primal_res = torch.norm(s - W)
        dual_res = torch.norm(-rho * (W - W_new))

        if primal_res < tol and dual_res < tol:
            break
    if DEBUG:
        print(f's:{s}')
        print(f'primal_res:{primal_res}')
        print(f'dual_res:{dual_res}')
    return W.view_as(z)

def constrainScoreByADMM(model, v_meter, max_score_meter):
    total = 0
    for n, m in model.named_modules():
        if hasattr(m, "scores"):
            if not m.prune:
                continue
            s = admm_solve(m.scores, N, M)
            m.scores.data = s
    for n, m in model.named_modules():
        if hasattr(m, "scores"):
            if m.prune:
                m.scores.clamp_(0, 1)