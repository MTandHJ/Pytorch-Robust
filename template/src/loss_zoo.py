

from typing import Optional, Union
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.transforms.transforms import Iterable




def cross_entropy(
    outs: torch.Tensor, 
    labels: torch.Tensor, 
    reduction: str = "mean"
) -> torch.Tensor:
    """
    cross entropy with logits
    """
    return F.cross_entropy(outs, labels, reduction=reduction)

def cross_entropy_softmax(
    probs: torch.Tensor,
    labels: torch.Tensor,
    reduction: str = "mean"
) -> torch.Tensor:
    """
    cross entropy with probs
        probs: the softmax of logits
    """
    return F.nll_loss(probs.log(), labels, reduction=reduction)

def kl_divergence(
    logits: torch.Tensor, 
    targets: torch.Tensor, 
    reduction: str = "batchmean"
) -> torch.Tensor:
    # KL divergence
    assert logits.size() == targets.size()
    # targets = targets.clone().detach()
    inputs = F.log_softmax(logits, dim=-1)
    targets = F.softmax(targets, dim=-1)
    return F.kl_div(inputs, targets, reduction=reduction)

def lploss(
    x: torch.Tensor,
    p: Union[int, float, 'fro', 'nuc'] = 'fro',
    dim: Union[int, Iterable] = -1
):
    return torch.norm(x, p=p, dim=dim).mean()
