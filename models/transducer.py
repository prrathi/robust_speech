"""A RNN-Transducer ASR system with librispeech supporting adversarial attacks.
The system employs an encoder, a decoder, and an joint network
between them. Decoding is performed with beamsearch coupled with a neural
language model.

Inspired from SpeechBrain Transducer 
(https://github.com/speechbrain/speechbrain/blob/develop/recipes/LibriSpeech/ASR/transducer/train.py)
"""
import logging
import os
import sys

import speechbrain as sb
import torch
from hyperpyyaml import load_hyperpyyaml
from speechbrain.utils.distributed import run_on_main

import robust_speech as rs
from robust_speech.adversarial.brain import AdvASRBrain

logger = logging.getLogger(__name__)

# Define training procedure


class RNNTASR(AdvASRBrain):
    """
    Encoder-decoder transducer model.
    """

    def compute_forward(self, batch, stage):
        """Forward computations from the waveform batches to the output probabilities."""
        if not stage == rs.Stage.ATTACK:
            batch = batch.to(self.device)
        wavs, wav_lens = batch.sig
        tokens_with_bos, token_with_bos_lens = batch.tokens_bos
        # wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)

        if hasattr(self.hparams, "smoothing") and self.hparams.smoothing:
            wavs = self.hparams.smoothing(wavs, wav_lens)

        # Add augmentation if specified
        if stage == sb.Stage.TRAIN:
            if hasattr(self.modules, "env_corrupt"):
                wavs_noise = self.modules.env_corrupt(wavs, wav_lens)
                wavs = torch.cat([wavs, wavs_noise], dim=0)
                wav_lens = torch.cat([wav_lens, wav_lens])
                batch.sig = wavs, wav_lens
                tokens_with_bos = torch.cat(
                    [tokens_with_bos, tokens_with_bos], dim=0)
                token_with_bos_lens = torch.cat(
                    [token_with_bos_lens, token_with_bos_lens]
                )
                batch.tokens_bos = tokens_with_bos, token_with_bos_lens
            if hasattr(self.modules, "augmentation"):
                wavs = self.modules.augmentation(wavs, wav_lens)

        # Forward pass
        feats = self.hparams.compute_features(
            wavs) if self.hparams.compute_features is not None else wavs
        if stage == sb.Stage.TRAIN:
            feats = self.modules.normalize(feats, wav_lens)
        else:
            # don't update normalization outside of training!
            feats = self.modules.normalize(
                feats, wav_lens, epoch=self.modules.normalize.update_until_epoch + 1
            )
        if stage == rs.Stage.ATTACK:
            encoded = self.modules.enc(feats)
        else:
            encoded = self.modules.enc(feats.detach())
        e_in = self.modules.emb(tokens_with_bos)
        hidden, _ = self.modules.dec(e_in)
        # Joint network
        # add labelseq_dim to the encoder tensor: [B,T,H_enc] => [B,T,1,H_enc]
        # add timeseq_dim to the decoder tensor: [B,U,H_dec] => [B,1,U,H_dec]
        joint = self.modules.Tjoint(encoded.unsqueeze(2), hidden.unsqueeze(1))

        # Output layer for transducer log-probabilities
        logits = self.modules.transducer_lin(joint)
        p_transducer = self.hparams.log_softmax(logits)

        # Compute outputs
        if stage == sb.Stage.TRAIN or stage == rs.Stage.ATTACK:
            return_ctc = False
            return_ce = False
            current_epoch = self.hparams.epoch_counter.current
            if (
                hasattr(self.hparams, "ctc_cost")
                and current_epoch <= self.hparams.number_of_ctc_epochs
            ):
                return_ctc = True
                # Output layer for ctc log-probabilities
                out_ctc = self.modules.enc_lin(encoded)
                p_ctc = self.hparams.log_softmax(out_ctc)
            if (
                hasattr(self.hparams, "ce_cost")
                and current_epoch <= self.hparams.number_of_ce_epochs
            ):
                return_ce = True
                # Output layer for ctc log-probabilities
                p_ce = self.modules.dec_lin(hidden)
                p_ce = self.hparams.log_softmax(p_ce)
            if return_ce and return_ctc:
                return p_ctc, p_ce, p_transducer, wav_lens
            elif return_ctc:
                return p_ctc, p_transducer, wav_lens
            elif return_ce:
                return p_ce, p_transducer, wav_lens
            else:
                return p_transducer, wav_lens

        elif stage == sb.Stage.VALID:
            best_hyps, _, _, _ = self.hparams.valid_search(encoded)
            return p_transducer, wav_lens, best_hyps
        else:
            (
                best_hyps,
                _,
                _,
                _,
            ) = self.hparams.test_search(encoded)
            return p_transducer, wav_lens, best_hyps

    def compute_objectives(
        self, predictions, batch, stage, adv=False, targeted=False, reduction="mean"
    ):
        """Computes the loss (Transducer+(CTC+NLL)) given predictions and targets."""

        ids = batch.id
        current_epoch = self.hparams.epoch_counter.current
        tokens, token_lens = batch.tokens
        tokens_eos, token_eos_lens = batch.tokens_eos
        if hasattr(self.modules, "env_corrupt") and stage == sb.Stage.TRAIN:
            tokens_eos = torch.cat([tokens_eos, tokens_eos], dim=0)
            token_eos_lens = torch.cat([token_eos_lens, token_eos_lens], dim=0)
            tokens = torch.cat([tokens, tokens], dim=0)
            token_lens = torch.cat([token_lens, token_lens], dim=0)

        if stage == sb.Stage.TRAIN or stage == rs.Stage.ATTACK:
            if len(predictions) == 4:
                p_ctc, p_ce, p_transducer, wav_lens = predictions
                ctc_loss = self.hparams.ctc_cost(
                    p_ctc, tokens, wav_lens, token_lens, reduction=reduction
                )
                ce_loss = self.hparams.ce_cost(
                    p_ce, tokens_eos, length=token_eos_lens, reduction=reduction
                )
                loss_transducer = self.hparams.transducer_cost(
                    p_transducer, tokens, wav_lens, token_lens, reduction=reduction
                )
                loss = (
                    self.hparams.ctc_weight * ctc_loss
                    + self.hparams.ce_weight * ce_loss
                    + (1 - (self.hparams.ctc_weight + self.hparams.ce_weight))
                    * loss_transducer
                )
            elif len(predictions) == 3:
                # one of the 2 heads (CTC or CE) is still computed
                # CTC alive
                if current_epoch <= self.hparams.number_of_ctc_epochs:
                    p_ctc, p_transducer, wav_lens = predictions
                    ctc_loss = self.hparams.ctc_cost(
                        p_ctc, tokens, wav_lens, token_lens, reduction=reduction
                    )
                    loss_transducer = self.hparams.transducer_cost(
                        p_transducer, tokens, wav_lens, token_lens, reduction=reduction
                    )
                    loss = (
                        self.hparams.ctc_weight * ctc_loss
                        + (1 - self.hparams.ctc_weight) * loss_transducer
                    )
                # CE for decoder alive
                else:
                    p_ce, p_transducer, wav_lens = predictions
                    ce_loss = self.hparams.ce_cost(
                        p_ce, tokens_eos, length=token_eos_lens, reduction=reduction
                    )
                    loss_transducer = self.hparams.transducer_cost(
                        p_transducer, tokens, wav_lens, token_lens, reduction=reduction
                    )
                    loss = (
                        self.hparams.ce_weight * ce_loss
                        + (1 - self.hparams.ctc_weight) * loss_transducer
                    )
            else:
                p_transducer, wav_lens = predictions
                loss = self.hparams.transducer_cost(
                    p_transducer, tokens, wav_lens, token_lens, reduction=reduction
                )
        else:
            p_transducer, wav_lens, predicted_tokens = predictions
            loss = self.hparams.transducer_cost(
                p_transducer, tokens, wav_lens, token_lens, reduction=reduction
            )

        if stage not in [sb.Stage.TRAIN, rs.Stage.ATTACK]:
            # Decode token terms to words
            if isinstance(self.tokenizer, sb.dataio.encoder.CTCTextEncoder):
                predicted_words = [
                    self.tokenizer.decode_ndim(utt_seq) for utt_seq in predicted_tokens
                ]
                predicted_words = ["".join(s).strip().split(" ")
                                   for s in predicted_words]
            else:
                predicted_words = [
                    self.tokenizer.decode_ids(utt_seq).split(" ") for utt_seq in predicted_tokens
                ]
            target_words = [wrd.split(" ") for wrd in batch.wrd]

            if adv:
                if targeted:
                    self.adv_wer_metric_target.append(
                        ids, predicted_words, target_words
                    )
                    self.adv_cer_metric_target.append(
                        ids, predicted_words, target_words
                    )
                else:
                    self.adv_wer_metric.append(
                        ids, predicted_words, target_words)
                    self.adv_cer_metric.append(
                        ids, predicted_words, target_words)
            else:
                self.wer_metric.append(ids, predicted_words, target_words)
                self.cer_metric.append(ids, predicted_words, target_words)

        return loss
