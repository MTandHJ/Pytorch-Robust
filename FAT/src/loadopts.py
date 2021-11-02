
from typing import TypeVar, Callable, Optional, Tuple, Dict
import numpy as np
import torch
import torchvision
import torchvision.transforms as T
import foolbox as fb

import time
from tqdm import tqdm


from models.base import AdversarialDefensiveModule
from .base import AdversaryForValid
from .config import *
from .utils import getLogger, mkdirs



class ModelNotDefineError(Exception): pass
class LossNotDefineError(Exception): pass
class OptimNotIncludeError(Exception): pass
class AttackNotIncludeError(Exception): pass
class DatasetNotIncludeError(Exception): pass


# return the num_classes of corresponding data set
def get_num_classes(dataset_type: str) -> int:
    if dataset_type in ('mnist', 'fashionmnist', 'svhn', 'cifar10'):
        return 10
    elif dataset_type in ('cifar100', ):
        return 100
    else:
        raise DatasetNotIncludeError("Dataset {0} is not included." \
                        "Refer to the following: {1}".format(dataset_type, _dataset.__doc__))


def load_model(model_type: str) -> Callable[..., torch.nn.Module]:
    """
    mnist: the model designed for MNIST dataset
    cifar: the model designed for CIFAR dataset
    resnet8|20|32|44|110|1202
    resnet18|34|50|101|50_32x4d
    preactresnet18|34|50|101
    wrn_28_10: depth-28, width-10
    wrn_34_10: depth-34, width-10
    wrn_34_20: depth-34, width-20
    """
    resnets = ['resnet8', 'resnet20', 'resnet32', 'resnet44', 
                'resnet56', 'resnet110', 'resnet1202']
    srns = ['resnet18', 'resnet34', 'resnet50', 'resnet101', 'resnext50_32x4d']
    prns = ['preactresnet18', 'preactresnet34', 'preactresnet50', 'preactresnet101']
    wrns = ['wrn_28_10', 'wrn_34_10', 'wrn_34_20']

    model: Callable[..., AdversarialDefensiveModule]
    if model_type == "mnist":
        from models.mnist import MNIST
        model = MNIST
    elif model_type == "cifar":
        from models.cifar import CIFAR
        model = CIFAR
    elif model_type in resnets:
        import models.resnet as resnet
        model = getattr(resnet, model_type)
    elif model_type in srns:
        import models.cifar_resnet as srn
        model = getattr(srn, model_type)
    elif model_type in prns:
        import models.preactresnet as prn
        model = getattr(prn, model_type)
    elif model_type in wrns:
        import models.wide_resnet as wrn
        model = getattr(wrn, model_type)
    else:
        raise ModelNotDefineError(f"model {model_type} is not defined.\n" \
                f"Refer to the following: {load_model.__doc__}\n")
    return model


def load_loss_func(loss_type: str) -> Callable:
    """
    cross_entropy: the softmax cross entropy loss
    kl_loss: kl divergence
    """
    loss_func: Callable[..., torch.Tensor]
    if loss_type == "cross_entropy":
        from .loss_zoo import cross_entropy
        loss_func = cross_entropy
    elif loss_type == "contrastive":
        from .loss_zoo import contrastive_loss
        loss_func = contrastive_loss
    elif loss_type == "kl_loss":
        from .loss_zoo import kl_divergence
        loss_func = kl_divergence
    elif loss_type == "margin_loss":
        from .loss_zoo import margin_loss
        loss_func = margin_loss
    else:
        raise LossNotDefineError(f"Loss {loss_type} is not defined.\n" \
                    f"Refer to the following: {load_loss_func.__doc__}")
    return loss_func


class _Normalize:

    def __init__(
        self, 
        mean: Optional[Tuple]=None, 
        std: Optional[Tuple]=None
    ):
        self.set_normalizer(mean, std)

    def set_normalizer(self, mean, std):
        if mean is None or std is None:
            self.flag = False
            return 0
        self.flag = True
        mean = torch.tensor(mean)
        std = torch.tensor(std)
        self.nat_normalize = T.Normalize(
            mean=mean, std=std
        )
        self.inv_normalize = T.Normalize(
            mean=-mean/std, std=1/std
        )

    def _normalize(self, imgs: torch.Tensor, inv: bool) -> torch.Tensor:
        if not self.flag:
            return imgs
        if inv:
            normalizer = self.inv_normalize
        else:
            normalizer = self.nat_normalize
        new_imgs = [normalizer(img) for img in imgs]
        return torch.stack(new_imgs)

    def __call__(self, imgs: torch.Tensor, inv: bool = False) -> torch.Tensor:
        # normalizer will set device automatically.
        return self._normalize(imgs, inv)


def _get_normalizer(dataset_type: str) -> _Normalize:
    mean = MEANS[dataset_type]
    std = STDS[dataset_type]
    return _Normalize(mean, std)


def _dataset(
    dataset_type: str, 
    train: bool = True
) -> torch.utils.data.Dataset:
    """
    Dataset:
    mnist: MNIST
    fashionmnist: FashionMNIST
    svhn: SVHN
    cifar10: CIFAR-10
    cifar100: CIFAR-100
    """
    if dataset_type == "mnist":
        dataset = torchvision.datasets.MNIST(
            root=ROOT, train=train, download=DOWNLOAD
        )
    elif dataset_type == "fashionmnist":
        dataset = torchvision.datasets.FashionMNIST(
            root=ROOT, train=train, download=DOWNLOAD
        )
    elif dataset_type == "svhn":
        split = 'train' if train else 'test'
        dataset = torchvision.datasets.SVHN(
            root=ROOT, split=split, download=DOWNLOAD
        )
    elif dataset_type == "cifar10":
        dataset = torchvision.datasets.CIFAR10(
            root=ROOT, train=train, download=DOWNLOAD
        )
    elif dataset_type == "cifar100":
        dataset = torchvision.datasets.CIFAR100(
            root=ROOT, train=train, download=DOWNLOAD
        )
    else:
        raise DatasetNotIncludeError("Dataset {0} is not included." \
                        "Refer to the following: {1}".format(dataset_type, _dataset.__doc__))
        
    return dataset


def load_normalizer(dataset_type: str) -> _Normalize:
    normalizer = _get_normalizer(dataset_type)
    return normalizer


def _split_dataset(
    dataset: torch.utils.data.Dataset,
    ratio: float = .1, seed: int = VALIDSEED,
    shuffle: bool = True
) -> Tuple[torch.utils.data.Dataset]:
    from torch.utils.data import Subset
    datasize = len(dataset)
    indices = list(range(datasize))
    if shuffle:
        np.random.seed(seed)
        np.random.shuffle(indices)
    validsize = int(ratio * datasize)
    getLogger().info(f"[Dataset] Split the dataset into trainset({datasize-validsize}) and validset({validsize}) ...")
    train_indices, valid_indices = indices[validsize:], indices[:validsize]
    trainset = Subset(dataset, train_indices)
    validset = Subset(dataset, valid_indices)
    return trainset, validset

def load_dataset(
    dataset_type: str, 
    transforms: str ='default', 
    ratio: float = 0.1,
    seed: int = VALIDSEED,
    shuffle: bool = True,
    train: bool = True
) -> torch.utils.data.Dataset:
    from .datasets import WrapperSet
    dataset = _dataset(dataset_type, train)
    if train:
        transforms = TRANSFORMS[dataset_type] if transforms == 'default' else transforms
        getLogger().info(f"[Dataset] Apply transforms of '{transforms}' to trainset ...")
        trainset, validset = _split_dataset(dataset, ratio, seed, shuffle)
        trainset = WrapperSet(trainset, transforms=transforms)
        validset = WrapperSet(validset, transforms=TRANSFORMS['validation'])
        return trainset, validset
    else:
        getLogger().info(f"[Dataset] Apply transforms of '{transforms}' to testset ...")
        testset = WrapperSet(dataset, transforms=transforms)
        return testset


class _TQDMDataLoader(torch.utils.data.DataLoader):
    def __iter__(self):
        return iter(
            tqdm(
                super(_TQDMDataLoader, self).__iter__(), 
                leave=False, desc="վ'ᴗ' ի-"
            )
        )

def load_dataloader(
    dataset: torch.utils.data.Dataset, 
    batch_size: int, 
    train: bool = True, 
    show_progress: bool = False
) -> torch.utils.data.DataLoader:

    dataloader = _TQDMDataLoader if show_progress else torch.utils.data.DataLoader
    if train:
        loader = dataloader(
            dataset, batch_size=batch_size, shuffle=True,
            num_workers=NUM_WORKERS, pin_memory=PIN_MEMORY
        )
    else:
        loader = dataloader(
            dataset, batch_size=batch_size, shuffle=False,
            num_workers=NUM_WORKERS, pin_memory=PIN_MEMORY
        )
    return loader


def load_optimizer(
    model: torch.nn.Module, 
    optim_type: str, *,
    lr: float = 0.1, momentum: float = 0.9,
    betas: Tuple[float, float] = (0.9, 0.999),
    weight_decay: float = 1e-4,
    nesterov: bool = False,
    **kwargs: "other hyper-parameters for optimizer"
) -> torch.optim.Optimizer:
    """
    sgd: SGD
    adam: Adam
    """
    try:
        cfg = OPTIMS[optim_type]
    except KeyError:
        raise OptimNotIncludeError(f"Optim {optim_type} is not included.\n" \
                        f"Refer to the following: {load_optimizer.__doc__}")
    
    kwargs.update(lr=lr, momentum=momentum, betas=betas, 
                weight_decay=weight_decay, nesterov=nesterov)
    
    cfg.update(**kwargs) # update the kwargs needed automatically
    logger = getLogger()
    logger.info(cfg)
    if optim_type == "sgd":
        optim = torch.optim.SGD(model.parameters(), **cfg)
    elif optim_type == "adam":
        optim = torch.optim.Adam(model.parameters(), **cfg)

    return optim


class MultiLR:
    def __init__(
        self, optimizer, lr, 
        milstones=(60, 90, 110), 
        factors=(0.1, 0.01, 0.005)
    ):
        self.optimizer = optimizer
        self.base_lr = lr
        self.milstones = milstones
        self.factors = factors

        self.cur_epoch = 0
    
    def step(self):
        self.cur_epoch += 1
        for milstone, factor in zip(self.milstones, self.factors):
            if self.cur_epoch >= milstone - 1: # FAT adjusts the learning rate before each iteration
                lr = self.base_lr * factor
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = lr


def load_learning_policy(
    optimizer: torch.optim.Optimizer,
    learning_policy_type: str,
    **kwargs: "other hyper-parameters for learning scheduler"
) -> "learning policy":
    """
    default: (100, 105), 110 epochs suggested
    null:
    STD: (82, 123), 164 epochs suggested
    STD-wrn: (60, 120, 160), 200 epochs suggested
    AT: (102, 154), 200 epochs suggested
    TRADES: (75, 90, 100), 76 epochs suggested
    TRADES-M: (55, 75, 90), 100 epochs suggested
    cosine: CosineAnnealingLR, kwargs: T_max, eta_min, last_epoch
    """
    try:
        learning_policy_ = LEARNING_POLICY[learning_policy_type]
    except KeyError:
        raise NotImplementedError(f"Learning_policy {learning_policy_type} is not defined.\n" \
            f"Refer to the following: {load_learning_policy.__doc__}")

    lp_type = learning_policy_[0]
    lp_cfg = learning_policy_[1]
    lp_cfg.update(**kwargs) # update the kwargs needed automatically
    logger = getLogger()
    logger.info(f"{lp_cfg}    {lp_type}")
    try:
        learning_policy = getattr(
            torch.optim.lr_scheduler, 
            lp_type
        )(optimizer, **lp_cfg)
    except AttributeError:
        learning_policy = MultiLR(optimizer, **lp_cfg)
    
    return learning_policy


def _attack(attack_type: str, stepsize: float, steps: int) -> fb.attacks.Attack:
    """
    pgd-linf: \ell_{\infty} rel_stepsize=stepsize, steps=steps;
    pgd-l1: \ell_1 version;
    pgd-l2: \ell_2 version;
    pgd-kl: pgd-linf with KL divergence loss;
    pgd-softmax: pgd-linf with cross-entropy loss which takes probs as inputs
    fgsm: no hyper-parameters;
    cw-l2: stepsize=stepsize, steps=steps;
    ead: initial_stepsize=stepsize, steps=steps;
    slide: \ell_1 attack, rel_stepsize=stepsize, steps=steps;
    deepfool-linf: \ell_{\infty} version, overshoot=stepsize, steps=steps;
    deepfool-l2: \ell_2 version;
    bba-inf: \ell_{infty} version, lr=stepsize, steps=steps, overshott=1.1;
    bba-l1: \ell_1 version;
    bba-l2: \ell_2 version
    """
    attack: fb.attacks.Attack
    if attack_type == "pgd-linf":
        attack = fb.attacks.LinfPGD(
            rel_stepsize=stepsize,
            steps=steps
        )
    elif attack_type == "pgd-l2":
        attack = fb.attacks.L2PGD(
            rel_stepsize=stepsize,
            steps=steps
        )
    elif attack_type == "pgd-l1":
        attack = fb.attacks.L1PGD(
            rel_stepsize=stepsize,
            steps=steps
        )
    elif attack_type == "pgd-kl":
        from .attacks import LinfPGDKLDiv
        attack = LinfPGDKLDiv(
            rel_stepsize=stepsize,
            steps=steps
        )
    elif attack_type == "pgd-softmax":
        from .attacks import LinfPGDSoftmax
        attack = LinfPGDSoftmax(
            rel_stepsize=stepsize,
            steps=steps
        )
    elif attack_type == "fgsm":
        attack = fb.attacks.LinfFastGradientAttack(
            random_start=False
        )
    elif attack_type == "cw-l2":
        attack = fb.attacks.L2CarliniWagnerAttack(
            stepsize=stepsize,
            steps=steps
        )
    elif attack_type == "ead":
        attack = fb.attacks.EADAttack(
            initial_stepsize=stepsize,
            steps=steps
        )
    elif attack_type == "slide":
        attack = fb.attacks.SparseL1DescentAttack(
            rel_stepsize=stepsize,
            steps=steps
        )
    elif attack_type == "deepfool-linf":
        attack = fb.attacks.LinfDeepFoolAttack(
            overshoot=stepsize,
            steps=steps
        )
    elif attack_type == "deepfool-l2":
        attack = fb.attacks.L2DeepFoolAttack(
            overshoot=stepsize,
            steps=steps
        )
    elif attack_type == "bba-linf":
        attack = fb.attacks.LinfinityBrendelBethgeAttack(
            lr=stepsize,
            steps=steps
        )
    elif attack_type == "bba-l2":
        attack = fb.attacks.L2BrendelBethgeAttack(
            lr=stepsize,
            steps=steps
        )
    elif attack_type == "bba-l1":
        attack = fb.attacks.L1BrendelBethgeAttack(
            lr=stepsize,
            steps=steps
        )
    else:
        raise AttackNotIncludeError(f"Attack {attack_type} is not included.\n" \
                    f"Refer to the following: {_attack.__doc__}")
    return attack


def load_attack(
    attack_type: str, stepsize: float, steps: int
) -> fb.attacks.Attack:
    attack = _attack(attack_type, stepsize, steps)
    return attack


def load_valider(
    model: torch.nn.Module, device: torch.device, dataset_type: str
) -> AdversaryForValid:
    cfg, epsilon = VALIDER[dataset_type]
    attack = load_attack(**cfg)
    valider = AdversaryForValid(
        model=model, attacker=attack, 
        device=device, epsilon=epsilon
    )
    return valider


def generate_path(
    method: str, dataset_type: str, model:str, description: str
) -> Tuple[str, str]:
    info_path = INFO_PATH.format(
        method=method,
        dataset=dataset_type,
        model=model,
        description=description
    )
    log_path = LOG_PATH.format(
        method=method,
        dataset=dataset_type,
        model=model,
        description=description,
        time=time.strftime(TIMEFMT)
    )
    mkdirs(info_path, log_path)
    return info_path, log_path

