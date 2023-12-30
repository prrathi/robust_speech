"""
Variations of the PGD attack (https://arxiv.org/abs/1706.06083)
"""

import numpy as np
import torch
import torch.nn as nn
import pdb
from torch_stoi import NegSTOILoss
import string
import time

import robust_speech as rs
from robust_speech.adversarial.attacks.attacker import Attacker
from robust_speech.adversarial.utils import (
    l2_clamp_or_normalize,
    linf_clamp,
    rand_assign,
)
from speechbrain.dataio.dataio import merge_char, split_word
from speechbrain.utils.edit_distance import count_ops, op_table


def reverse_bound_from_rel_bound(batch, rel, order=2):
    """From a relative eps bound, reconstruct the absolute bound for the given batch"""
    wavs, wav_lens = batch.sig
    wav_lens = [int(wavs.size(1) * r) for r in wav_lens]
    epss = []
    for i in range(len(wavs)):
        eps = torch.norm(wavs[i, : wav_lens[i]], p=order) / rel
        epss.append(eps)
    return torch.tensor(epss).to(wavs.device)


def pgd_loop(
    batch,
    asr_brain,
    nb_iter,
    eps,
    eps_iter,
    delta_init=None,
    minimize=False,
    order=np.inf,
    clip_min=None,
    clip_max=None,
    l1_sparsity=None,
    lambda_stoi=0,
    sample_rate=16000,
    tokenizer=None,
    log=None
):
    """
    Iteratively maximize the loss over the input.

    Arguments
    ---------
    asr_brain: rs.adversarial.brain.ASRBrain
       brain object.
    eps: float
       maximum distortion.
    nb_iter: int
       number of iterations.
    eps_iter: float
       attack step size.
    rand_init: (optional bool)
       random initialization.
    clip_min: (optional) float
       mininum value per input dimension.
    clip_max: (optional) float
       maximum value per input dimension.
    order: (optional) int
       the order of maximum distortion (inf or 2).
    targeted: bool
       if the attack is targeted.
    l1_sparsity: optional float
       sparsity value for L1 projection.
          - if None, then perform regular L1 projection.
          - if float value, then perform sparse L1 descent from
          Algorithm 1 in https://arxiv.org/pdf/1904.13000v1.pdf


    Returns
    -------
    tensor containing the perturbed input.
    """
    start = time.time()
    wav_init, wav_lens = batch.sig
    loss_func = NegSTOILoss(sample_rate=sample_rate).cuda()
    if delta_init is not None:
        delta = delta_init
    else:
        delta = torch.zeros_like(wav_init)
    if isinstance(eps_iter, torch.Tensor):
        assert eps_iter.dim() == 1
        eps_iter = eps_iter.unsqueeze(1)
    delta.requires_grad_()
    print(log)
    print("ID: " + str(batch.id[0]), file=open(log, 'a'))
    for nbi in range(nb_iter):
        print(time.time() - start)
        batch.sig = wav_init + delta, wav_lens

        predictions = asr_brain.compute_forward(batch, rs.Stage.ATTACK)
        pred_tokens = predictions[1]
        predicted_words = [tokenizer.decode(t).strip().upper().translate(
            str.maketrans('', '', string.punctuation)) for t in pred_tokens]
        predicted_words = [wrd.split(" ") for wrd in predicted_words]
        target_words = [wrd.upper().translate(str.maketrans(
            '', '', string.punctuation)).split(" ") for wrd in batch.wrd]
        ops = count_ops(op_table(target_words[0], predicted_words[0]))
        wer = 100.0 * sum(ops.values()) / max(1, len(target_words[0]))
        print(str(nbi) + ": " + str(wer), file=open(log, 'a'))

        loss = asr_brain.compute_objectives(
            predictions, batch, rs.Stage.ATTACK)
        if lambda_stoi != 0:
            loss2 = loss - lambda_stoi * loss_func(wav_init, wav_init+delta)
            if minimize:
                loss2 = -loss2
            loss2.backward(inputs = delta)
        else:
            if minimize:
                loss = -loss
            loss.backward(inputs = delta)
        print(time.time() - start)
        if order == np.inf:
            grad_sign = delta.grad.data.sign()
            delta.data = delta.data + eps_iter * grad_sign
            delta.data = linf_clamp(delta.data, eps)
            delta.data = (
                torch.clamp(wav_init.data + delta.data, clip_min, clip_max)
                - wav_init.data
            )

        elif order == 2:
            grad = delta.grad.data
            grad = l2_clamp_or_normalize(grad)
            delta.data = delta.data + eps_iter * grad
            delta.data = (
                torch.clamp(wav_init.data + delta.data, clip_min, clip_max)
                - wav_init.data
            )
            if eps is not None:
                delta.data = l2_clamp_or_normalize(delta.data, eps)
            print(time.time() - start)
        else:
            raise NotImplementedError(
                "PGD attack only supports order=2 or order=np.inf"
            )
        delta.grad.data.zero_()

        # print(loss)
    if isinstance(eps_iter, torch.Tensor):
        eps_iter = eps_iter.squeeze(1)
    wav_adv = torch.clamp(wav_init + delta, clip_min, clip_max)
    return wav_adv


            



def pgd_loop_with_return_delta(
    batch,
    asr_brain,
    nb_iter,
    eps,
    eps_iter,
    delta_init=None,
    minimize=False,
    order=np.inf,
    clip_min=None,
    clip_max=None,
    l1_sparsity=None,
    existing_perturbation=None,
):
    """
    Iteratively maximize the loss over the input.

    Arguments
    ---------
    asr_brain: rs.adversarial.brain.ASRBrain
       brain object.
    eps: float
       maximum distortion.
    nb_iter: int
       number of iterations.
    eps_iter: float
       attack step size.
    rand_init: (optional bool)
       random initialization.
    clip_min: (optional) float
       mininum value per input dimension.
    clip_max: (optional) float
       maximum value per input dimension.
    order: (optional) int
       the order of maximum distortion (inf or 2).
    targeted: bool
       if the attack is targeted.
    l1_sparsity: optional float
       sparsity value for L1 projection.
          - if None, then perform regular L1 projection.
          - if float value, then perform sparse L1 descent from
          Algorithm 1 in https://arxiv.org/pdf/1904.13000v1.pdf


    Returns
    -------
    tensor containing the perturbed input.
    Also, perturbed vector is returned.
    """
    wav_init, wav_lens = batch.sig
    if delta_init is not None:
        delta = delta_init
    else:
        delta = torch.zeros_like(wav_init)
    if isinstance(eps_iter, torch.Tensor):
        assert eps_iter.dim() == 1
        eps_iter = eps_iter.unsqueeze(1)
    delta.requires_grad_()
    if existing_perturbation is None:
        for _ in range(nb_iter):
            batch.sig = wav_init + delta, wav_lens
            predictions = asr_brain.compute_forward(batch, rs.Stage.ATTACK)
            loss = asr_brain.compute_objectives(
                predictions, batch, rs.Stage.ATTACK)
            if minimize:
                loss = -loss
            loss.backward()
            if order == np.inf:
                grad_sign = delta.grad.data.sign()
                delta.data = delta.data + eps_iter * grad_sign
                delta.data = linf_clamp(delta.data, eps)
                delta.data = (
                    torch.clamp(wav_init.data + delta.data, clip_min, clip_max)
                    - wav_init.data
                )

            elif order == 2:
                grad = delta.grad.data
                grad = l2_clamp_or_normalize(grad)
                delta.data = delta.data + eps_iter * grad
                delta.data = (
                    torch.clamp(wav_init.data + delta.data, clip_min, clip_max)
                    - wav_init.data
                )
                if eps is not None:
                    delta.data = l2_clamp_or_normalize(delta.data, eps)
            else:
                raise NotImplementedError(
                    "PGD attack only supports order=2 or order=np.inf"
                )
            delta.grad.data.zero_()
    else:
        wav_length = wav_init.shape[1]
        perturb_length = existing_perturbation.shape[1]
        if perturb_length >= wav_length:
            delta = existing_perturbation[:, :wav_length]
        else:
            import torch.nn.functional as F
            delta = F.pad(existing_perturbation,
                          (0, wav_length - perturb_length), mode='constant')

        if order == np.inf:
            delta.data = linf_clamp(delta.data, eps)
            delta.data = (
                torch.clamp(wav_init.data + delta.data, clip_min, clip_max)
                - wav_init.data
            )
        elif order == 2:
            delta.data = (
                torch.clamp(wav_init.data + delta.data, clip_min, clip_max)
                - wav_init.data
            )
            if eps is not None:
                delta.data = l2_clamp_or_normalize(delta.data, eps)
    if isinstance(eps_iter, torch.Tensor):
        eps_iter = eps_iter.squeeze(1)

    wav_adv = torch.clamp(wav_init + delta, clip_min, clip_max)
    return wav_adv, delta


class ASRPGDAttack(Attacker):
    """
    Implementation of the PGD attack (https://arxiv.org/abs/1706.06083)
    The attack performs nb_iter steps of size eps_iter, while always staying
    within eps from the initial point.

    Arguments
    ---------
    asr_brain: rs.adversarial.brain.ASRBrain
       brain object.
    eps: float
       maximum distortion.
    nb_iter: int
       number of iterations.
    eps_iter: float
       attack step size.
    rand_init: (optional bool)
       random initialization.
    clip_min: (optional) float
       mininum value per input dimension.
    clip_max: (optional) float
       maximum value per input dimension.
    order: (optional) int
       the order of maximum distortion (inf or 2).
    targeted: bool
       if the attack is targeted.
    train_mode_for_backward: bool
       whether to force training mode in backward passes (necessary for RNN models)
    """

    def __init__(
        self,
        asr_brain,
        eps=0.3,
        nb_iter=40,
        rel_eps_iter=0.1,
        rand_init=True,
        clip_min=None,
        clip_max=None,
        order=np.inf,
        l1_sparsity=None,
        targeted=False,
        train_mode_for_backward=True,
        lambda_stoi=0,
        sample_rate=16000
    ):
        self.clip_min = clip_min if clip_min is not None else -10
        self.clip_max = clip_max if clip_max is not None else 10
        self.eps = eps
        self.nb_iter = nb_iter
        self.rel_eps_iter = rel_eps_iter
        self.rand_init = rand_init
        self.order = order
        self.targeted = targeted
        self.asr_brain = asr_brain
        self.l1_sparsity = l1_sparsity
        self.train_mode_for_backward = train_mode_for_backward
        self.max_perturbation_len = -100
        self.lambda_stoi = lambda_stoi
        self.sample_rate = sample_rate
        assert isinstance(self.rel_eps_iter, torch.Tensor) or isinstance(
            self.rel_eps_iter, float
        )
        assert isinstance(self.eps, torch.Tensor) or isinstance(
            self.eps, float)

    def perturb(self, batch, tokenizer=None, log=None):
        """
        Compute an adversarial perturbation

        Arguments
        ---------
        batch : sb.PaddedBatch
           The input batch to perturb

        Returns
        -------
        the tensor of the perturbed batch
        """
        if self.train_mode_for_backward:
            self.asr_brain.module_train()
        else:
            self.asr_brain.module_eval()

        save_device = batch.sig[0].device
        batch = batch.to(self.asr_brain.device)
        save_input = batch.sig[0]
        wav_init = torch.clone(save_input)
        delta = torch.zeros_like(wav_init)
        delta = nn.Parameter(delta)
        if self.rand_init:
            clip_min = self.clip_min if self.clip_min is not None else -0.1
            clip_max = self.clip_max if self.clip_max is not None else 0.1
            rand_assign(delta, self.order, self.eps)
            delta.data = (
                torch.clamp(wav_init + delta.data, min=clip_min, max=clip_max)
                - wav_init
            )

        wav_adv = pgd_loop(
            batch,
            self.asr_brain,
            nb_iter=self.nb_iter,
            eps=self.eps,
            eps_iter=self.rel_eps_iter * self.eps,
            minimize=self.targeted,
            order=self.order,
            clip_min=self.clip_min,
            clip_max=self.clip_max,
            delta_init=delta,
            l1_sparsity=self.l1_sparsity,
            lambda_stoi=self.lambda_stoi,
            sample_rate=self.sample_rate,
            tokenizer=tokenizer,
            log=log
        )

        batch.sig = save_input, batch.sig[1]
        batch = batch.to(save_device)
        self.asr_brain.module_eval()
        return wav_adv.data.to(save_device)

    def perturb_and_log_return_perturbation(self, batch, mode):
        """
        Compute an adversarial perturbation 
        and return the perturbated vectors

        Arguments
        ---------
        batch : sb.PaddedBatch
           The input batch to perturb

        Returns
        -------
        the tensor of the perturbed batch
        Also, the perturbation vector is returned.
        """
        if self.train_mode_for_backward:
            self.asr_brain.module_train()
        else:
            self.asr_brain.module_eval()

        save_device = batch.sig[0].device
        batch = batch.to(self.asr_brain.device)
        save_input = batch.sig[0]
        wav_init = torch.clone(save_input)
        delta = torch.zeros_like(wav_init)
        delta = nn.Parameter(delta)
        if self.rand_init:
            clip_min = self.clip_min if self.clip_min is not None else -0.1
            clip_max = self.clip_max if self.clip_max is not None else 0.1

            rand_assign(delta, self.order, self.eps)
            delta.data = (
                torch.clamp(wav_init + delta.data, min=clip_min, max=clip_max)
                - wav_init
            )
        if mode == 'train':
            wav_adv, perturbation = pgd_loop_with_return_delta(
                batch,
                self.asr_brain,
                nb_iter=self.nb_iter,
                eps=self.eps,
                eps_iter=self.rel_eps_iter * self.eps,
                minimize=self.targeted,
                order=self.order,
                clip_min=self.clip_min,
                clip_max=self.clip_max,
                delta_init=delta,
                l1_sparsity=self.l1_sparsity,
            )
            cur_perturbation_len = perturbation.shape[1]
            if self.max_perturbation_len < cur_perturbation_len:
                # We save the longest perturbation vector
                self.max_perturbation_len = cur_perturbation_len
                self.perturbation = perturbation
        else:
            wav_adv, perturbation = pgd_loop_with_return_delta(
                batch,
                self.asr_brain,
                nb_iter=self.nb_iter,
                eps=self.eps,
                eps_iter=self.rel_eps_iter * self.eps,
                minimize=self.targeted,
                order=self.order,
                clip_min=self.clip_min,
                clip_max=self.clip_max,
                delta_init=delta,
                l1_sparsity=self.l1_sparsity,
                existing_perturbation=self.perturbation
            )

        batch.sig = save_input, batch.sig[1]
        batch = batch.to(save_device)
        self.asr_brain.module_eval()
        adv_wav, perturbation = wav_adv.data.to(
            save_device), perturbation.to(save_device)
        self.snr_metric.append(batch.id, batch, adv_wav)
        if self.save_audio_path:
            self.audio_saver.save(batch.id, batch, adv_wav)
        return adv_wav, perturbation


class ASRL2PGDAttack(ASRPGDAttack):
    """
    PGD Attack with order=L2
    Arguments
    ---------
    asr_brain: rs.adversarial.brain.ASRBrain
       brain object.
    eps: float
       maximum distortion.
    nb_iter: int
       number of iterations.
    eps_iter: float
       attack step size.
    rand_init: (optional bool)
       random initialization.
    clip_min: (optional) float
       mininum value per input dimension.
    clip_max: (optional) float
       maximum value per input dimension.
    targeted: bool
       if the attack is targeted.
    train_mode_for_backward: bool
       whether to force training mode in backward passes (necessary for RNN models)
    """

    def __init__(
        self,
        asr_brain,
        eps=0.3,
        nb_iter=40,
        rel_eps_iter=0.1,
        rand_init=True,
        clip_min=None,
        clip_max=None,
        targeted=False,
        train_mode_for_backward=True,
        lambda_stoi=0,
        sample_rate=16000
    ):
        order = 2
        super(ASRL2PGDAttack, self).__init__(
            asr_brain=asr_brain,
            eps=eps,
            nb_iter=nb_iter,
            rel_eps_iter=rel_eps_iter,
            rand_init=rand_init,
            clip_min=clip_min,
            clip_max=clip_max,
            targeted=targeted,
            train_mode_for_backward=train_mode_for_backward,
            order=order,
            lambda_stoi=lambda_stoi,
            sample_rate=sample_rate 
        )


class ASRLinfPGDAttack(ASRPGDAttack):
    """
    PGD Attack with order=Linf
    Arguments
    ---------
    asr_brain: rs.adversarial.brain.ASRBrain
       brain object.
    eps: float
       maximum distortion.
    nb_iter: int
       number of iterations.
    eps_iter: float
       attack step size.
    rand_init: (optional bool)
       random initialization.
    clip_min: (optional) float
       mininum value per input dimension.
    clip_max: (optional) float
       maximum value per input dimension.
    targeted: bool
       if the attack is targeted.
    train_mode_for_backward: bool
       whether to force training mode in backward passes (necessary for RNN models)
    """

    def __init__(
        self,
        asr_brain,
        eps=0.001,
        nb_iter=40,
        rel_eps_iter=0.1,
        rand_init=True,
        clip_min=None,
        clip_max=None,
        targeted=False,
        train_mode_for_backward=True,
    ):
        order = np.inf
        super(ASRLinfPGDAttack, self).__init__(
            asr_brain,
            eps=eps,
            nb_iter=nb_iter,
            rel_eps_iter=rel_eps_iter,
            rand_init=rand_init,
            clip_min=clip_min,
            clip_max=clip_max,
            targeted=targeted,
            train_mode_for_backward=train_mode_for_backward,
            order=order,
        )


class SNRPGDAttack(ASRL2PGDAttack):
    """
    PGD Attack with order=L2, bounded with Signal-Noise Ratio instead of L2 norm

    Arguments
    ---------
    asr_brain: rs.adversarial.brain.ASRBrain
       brain object.
    snr: float
       maximum distortion.
    nb_iter: int
       number of iterations.
    rel_eps_iter: float
       attack step size, relative to the radius.
    rand_init: (optional bool)
       random initialization.
    clip_min: (optional) float
       mininum value per input dimension.
    clip_max: (optional) float
       maximum value per input dimension.
    targeted: bool
       if the attack is targeted.
    train_mode_for_backward: bool
       whether to force training mode in backward passes (necessary for RNN models)
    """

    def __init__(
        self,
        asr_brain,
        # eps=1.0,
        snr=40,
        nb_iter=40,
        rel_eps_iter=0.1,
        rand_init=True,
        clip_min=None,
        clip_max=None,
        targeted=False,
        train_mode_for_backward=True,
        lambda_stoi=0,
        sample_rate=16000
    ):
        super(SNRPGDAttack, self).__init__(
            asr_brain=asr_brain,
            eps=1.0,
            nb_iter=nb_iter,
            rel_eps_iter=rel_eps_iter,
            rand_init=rand_init,
            clip_min=clip_min,
            clip_max=clip_max,
            targeted=targeted,
            train_mode_for_backward=train_mode_for_backward,
            lambda_stoi=lambda_stoi,
            sample_rate=sample_rate
        )
        assert isinstance(snr, int)
        self.rel_eps = torch.pow(torch.tensor(10.0), float(snr) / 20)

    def perturb(self, batch, tokenizer=None, log=None):
        """
        Compute an adversarial perturbation

        Arguments
        ---------
        batch : sb.PaddedBatch
           The input batch to perturb

        Returns
        -------
        the tensor of the perturbed batch
        """
        save_device = batch.sig[0].device
        batch = batch.to(self.asr_brain.device)
        self.eps = reverse_bound_from_rel_bound(batch, self.rel_eps, order=2)
        res = super(SNRPGDAttack, self).perturb(batch, tokenizer=tokenizer, log=log)
        self.eps = 1.0
        batch.to(save_device)
        return res.to(save_device)

    def perturb_and_log_return_perturbation(self, batch, mode):
        """
        Compute an adversarial perturbation 
        and return the perturbated vectors

        Arguments
        ---------
        batch : sb.PaddedBatch
           The input batch to perturb

        Returns
        -------
        the tensor of the perturbed batch
        Also, the perturbation vector is returned.
        """
        if self.train_mode_for_backward:
            self.asr_brain.module_train()
        else:
            self.asr_brain.module_eval()

        save_device = batch.sig[0].device
        batch = batch.to(self.asr_brain.device)
        save_input = batch.sig[0]
        self.eps = reverse_bound_from_rel_bound(batch, self.rel_eps, order=2)

        wav_init = torch.clone(save_input)
        delta = torch.zeros_like(wav_init)
        delta = nn.Parameter(delta)
        if self.rand_init:
            clip_min = self.clip_min if self.clip_min is not None else -0.1
            clip_max = self.clip_max if self.clip_max is not None else 0.1

            rand_assign(delta, self.order, self.eps)
            delta.data = (
                torch.clamp(wav_init + delta.data, min=clip_min, max=clip_max)
                - wav_init
            )
        if mode == 'train':
            wav_adv, perturbation = pgd_loop_with_return_delta(
                batch,
                self.asr_brain,
                nb_iter=self.nb_iter,
                eps=self.eps,
                eps_iter=self.rel_eps_iter * self.eps,
                minimize=self.targeted,
                order=self.order,
                clip_min=self.clip_min,
                clip_max=self.clip_max,
                delta_init=delta,
                l1_sparsity=self.l1_sparsity,
            )
            cur_perturbation_len = perturbation.shape[1]
            if self.max_perturbation_len < cur_perturbation_len:
                # We save the longest perturbation vector
                self.max_perturbation_len = cur_perturbation_len
                self.perturbation = perturbation
        else:
            wav_adv, perturbation = pgd_loop_with_return_delta(
                batch,
                self.asr_brain,
                nb_iter=self.nb_iter,
                eps=self.eps,
                eps_iter=self.rel_eps_iter * self.eps,
                minimize=self.targeted,
                order=self.order,
                clip_min=self.clip_min,
                clip_max=self.clip_max,
                delta_init=delta,
                l1_sparsity=self.l1_sparsity,
                existing_perturbation=self.perturbation
            )

        batch.sig = save_input, batch.sig[1]
        self.eps = 1.0
        batch = batch.to(save_device)
        self.asr_brain.module_eval()
        adv_wav, perturbation = wav_adv.data.to(
            save_device), perturbation.to(save_device)
        self.snr_metric.append(batch.id, batch, adv_wav)
        if self.save_audio_path:
            self.audio_saver.save(batch.id, batch, adv_wav)
        return adv_wav, perturbation


class MaxSNRPGDAttack(ASRLinfPGDAttack):
    """
    PGD Attack with order=Linf, bounded with the Max Signal-Noise Ratio instead of Linf norm

    Arguments
    ---------
    asr_brain: rs.adversarial.brain.ASRBrain
       brain object.
    snr: float
       maximum distortion.
    nb_iter: int
       number of iterations.
    eps_iter: float
       attack step size.
    rand_init: (optional bool)
       random initialization.
    clip_min: (optional) float
       mininum value per input dimension.
    clip_max: (optional) float
       maximum value per input dimension.
    targeted: bool
       if the attack is targeted.
    train_mode_for_backward: bool
       whether to force training mode in backward passes (necessary for RNN models)
    """

    def __init__(
        self,
        asr_brain,
        snr=40,
        nb_iter=40,
        rel_eps_iter=0.1,
        rand_init=True,
        clip_min=None,
        clip_max=None,
        targeted=False,
        train_mode_for_backward=True,
    ):
        super(MaxSNRPGDAttack, self).__init__(
            asr_brain=asr_brain,
            eps=1.0,
            nb_iter=nb_iter,
            rel_eps_iter=rel_eps_iter,
            rand_init=rand_init,
            clip_min=clip_min,
            clip_max=clip_max,
            targeted=targeted,
            train_mode_for_backward=train_mode_for_backward,
        )
        assert isinstance(snr, int)
        self.rel_eps = torch.pow(torch.tensor(10.0), float(snr) / 20)

    def perturb(self, batch):
        """
        Compute an adversarial perturbation

        Arguments
        ---------
        batch : sb.PaddedBatch
           The input batch to perturb

        Returns
        -------
        the tensor of the perturbed batch
        """
        save_device = batch.sig[0].device
        batch = batch.to(self.asr_brain.device)
        self.eps = reverse_bound_from_rel_bound(
            batch, self.rel_eps, order=np.inf)
        res = super(MaxSNRPGDAttack, self).perturb(batch)
        self.eps = 1.0
        batch.to(save_device)
        return res.to(save_device)
