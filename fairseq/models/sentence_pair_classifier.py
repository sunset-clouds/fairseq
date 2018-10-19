# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from fairseq.tasks.language_modeling import LanguageModelingTask
from fairseq.modules import (
    ElmoTokenEmbedder, MultiheadAttention
)
from . import (
    BaseFairseqModel, register_model, register_model_architecture,
)

from fairseq import options
from fairseq import utils

from fairseq.models.transformer import (
    Embedding, LayerNorm, Linear, PositionalEmbedding,
)


class AttentionLayer(nn.Module):
    def __init__(self, dim, dropout):
        super().__init__()

        self.prem_hyp_attn = MultiheadAttention(dim, 16, 0, add_zero_attn=True)

        self.ln_h = nn.LayerNorm(dim)

        self.qh_ffn = nn.Sequential(
            nn.Linear(dim, dim * 2),
            nn.ReLU(),
            nn.Linear(dim * 2, dim),
        )
        self.dropout = nn.Dropout(dropout)

    def forward(self, q, kv, key_mask):
        enc_h, _ = self.prem_hyp_attn(
            query=q,
            key=kv,
            value=kv,
            key_padding_mask=key_mask,
            need_weights=False,
        )
        enc_h += q
        enc_h = self.ln_h(enc_h)
        enc_h = self.dropout(enc_h)

        enc_h = enc_h + self.qh_ffn(enc_h)
        return enc_h


@register_model('sentence_pair_classifier')
class SentencePairClassifier(BaseFairseqModel):
    def __init__(self, args, embedding):
        super().__init__()

        self.embedding = embedding

        self.class_queries = nn.Parameter(torch.Tensor(args.num_labels, args.model_dim))
        self.embedding_dropout = nn.Dropout(args.embedding_dropout)
        self.qh_attn = MultiheadAttention(args.model_dim, 16, 0, add_zero_attn=True)
        self.ln_q = nn.LayerNorm(args.model_dim)
        self.last_dropout = nn.Dropout(args.last_dropout)
        self.proj = torch.nn.Linear(args.model_dim, 1, bias=True)

        self.pq_layers = nn.ModuleList([
            AttentionLayer(args.model_dim, args.dropout),
            AttentionLayer(args.model_dim, args.dropout),
        ])


        self.reset_parameters()

    def reset_parameters(self):
        torch.nn.init.normal_(self.class_queries)
        torch.nn.init.xavier_uniform_(self.proj.weight)
        torch.nn.init.constant_(self.proj.bias, 0)

    def forward(self, sentence1, sentence2):

        premise_pad_mask = sentence1.eq(self.embedding.padding_idx)
        hypothesis_pad_mask = sentence2.eq(self.embedding.padding_idx)
        if not premise_pad_mask.any():
            premise_pad_mask = None
        else:
            premise_pad_mask = torch.cat(
                [premise_pad_mask.new_zeros(premise_pad_mask.size(0), 1), premise_pad_mask], dim=1)

        if not hypothesis_pad_mask.any():
            hypothesis_pad_mask = None
        else:
            hypothesis_pad_mask = torch.cat(
                [hypothesis_pad_mask.new_zeros(hypothesis_pad_mask.size(0), 1), hypothesis_pad_mask], dim=1)

        embedded_premise = self.embedding(sentence1).transpose(0, 1)
        embedded_hypothesis = self.embedding(sentence2).transpose(0, 1)

        q = self.class_queries.unsqueeze(1).expand(self.class_queries.shape[0], embedded_hypothesis.shape[1],
                                                   self.class_queries.shape[1])

        enc_h = embedded_hypothesis
        for layer in self.pq_layers:
            enc_h = layer(enc_h, embedded_premise, premise_pad_mask)

        enc_q, _ = self.qh_attn(
            query=q,
            key=enc_h,
            value=enc_h,
            key_padding_mask=hypothesis_pad_mask,
            need_weights=False,
        )


        x = self.ln_q(enc_q)
        x = self.last_dropout(x)

        x = self.proj(x).squeeze(-1).t()

        return x

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        parser.add_argument('--elmo-path', metavar='PATH', help='path to elmo model')
        parser.add_argument('--model-dim', type=int, metavar='N', help='decoder input dimension')
        parser.add_argument('--embedding-dropout', type=float, metavar='D', help='dropout after embedding')
        parser.add_argument('--last-dropout', type=float, metavar='D', help='dropout before projection')
        parser.add_argument('--dropout', type=float, metavar='D', help='model dropout')

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""

        # make sure all arguments are present in older models
        base_architecture(args)

        dictionary = task.dictionary

        if args.elmo_path is not None:
            task = LanguageModelingTask(args, dictionary, dictionary)
            models, _ = utils.load_ensemble_for_inference([args.elmo_path], task, {'remove_head': True})
            assert len(models) == 1, 'ensembles are currently not supported for elmo embeddings'

            embedding = ElmoTokenEmbedder(
                models[0],
                dictionary.eos(),
                dictionary.pad(),
                add_bos=True,
                remove_bos=False,
                remove_eos=False,
                combine_tower_states=True,
                projection_dim=args.model_dim,
                add_final_predictive=True,
                add_final_context=True,
                final_dropout=args.embedding_dropout,
                weights_dropout=0,
                tune_lm=False,
                apply_softmax=True,
                layer_norm=False,
                affine_layer_norm=False,
                channelwise_weights=False,
                scaled_sigmoid=False,
                individual_norms=False,
                channelwise_norm=False,
                init_gamma=1.0,
                ltn=True,
                ltn_dims=3,
                train_gamma=True,
            )
        else:
            embedding = nn.Embedding(len(dictionary), args.model_dim, dictionary.pad())

        return SentencePairClassifier(args, embedding)


@register_model('finetuning_sentence_pair_classifier')
class FinetuningSentencePairClassifier(BaseFairseqModel):
    def __init__(self, args, language_model, eos_idx):
        super().__init__()

        self.language_model = language_model
        self.eos_idx = eos_idx

        self.last_dropout = nn.Dropout(args.last_dropout)
        self.proj = torch.nn.Linear(args.model_dim * 2, 2, bias=True)

        self.reset_parameters()

    def reset_parameters(self):
        torch.nn.init.constant_(self.proj.weight, 0)
        torch.nn.init.constant_(self.proj.bias, 0)

    def forward(self, sentence1, sentence2):
        bos_block = src_tokens.new_full((src_tokens.size(0), 1), self.eos_idx)
        src_tokens = torch.cat([bos_block, src_tokens], dim=1)

        x, _ = self.language_model(src_tokens)

        eos_idxs = src_tokens.eq(self.eos_idx)
        x = x[eos_idxs].view(src_tokens.size(0), 1, -1)  # assume only 2 eoses per sample

        x = self.last_dropout(x)
        x = self.proj(x).squeeze(-1)

        return x

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        parser.add_argument('--lm-path', metavar='PATH', help='path to elmo model')
        parser.add_argument('--model-dim', type=int, metavar='N', help='decoder input dimension')
        parser.add_argument('--last-dropout', type=float, metavar='D', help='dropout before projection')

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""

        # make sure all arguments are present in older models
        base_architecture(args)

        dictionary = task.dictionary

        assert args.lm_path is not None

        task = LanguageModelingTask(args, dictionary, dictionary)
        models, _ = utils.load_ensemble_for_inference([args.lm_path], task, {'remove_head': True})
        assert len(models) == 1, 'ensembles are currently not supported for elmo embeddings'

        return FinetuningSentencePairClassifier(args, models[0], dictionary.eos())


@register_model_architecture('sentence_pair_classifier', 'sentence_pair_classifier')
def base_architecture(args):
    args.model_dim = getattr(args, 'model_dim', 2048)
    args.embedding_dropout = getattr(args, 'embedding_dropout', 0.5)
    args.dropout = getattr(args, 'dropout', 0.3)
    args.last_dropout = getattr(args, 'last_dropout', 0.3)


@register_model_architecture('finetuning_sentence_pair_classifier', 'finetuning_sentence_pair_classifier')
def base_architecture(args):
    args.model_dim = getattr(args, 'model_dim', 1024)
    args.last_dropout = getattr(args, 'last_dropout', 0.3)
