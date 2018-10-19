# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import numpy as np
import torch

from . import data_utils, FairseqDataset
from typing import List


def collate(samples, pad_idx, eos_idx):
    if len(samples) == 0:
        return {}

    return {
        'id': torch.LongTensor([s['id'] for s in samples]),
        'ntokens': sum(len(s['sentence1'] + len(s['sentence2'])) for s in samples),
        'net_input': {
            'sentence1': data_utils.collate_tokens(
                [s['sentence1'] for s in samples], pad_idx, eos_idx, left_pad=False,
            ),
            'sentence2': data_utils.collate_tokens(
                [s['sentence2'] for s in samples], pad_idx, eos_idx, left_pad=False,
            ),
        },
        'target': torch.stack([s['target'] for s in samples], dim=0),
        'nsentences': samples[0]['sentence1'].size(0),
    }


class SentencePairClassificationDataset(FairseqDataset):
    """
    A wrapper around torch.utils.data.Dataset for monolingual data.

    Args:
        dataset (torch.utils.data.Dataset): dataset to wrap
        sizes (List[int]): sentence lengths
        vocab (~fairseq.data.Dictionary): vocabulary
        shuffle (bool, optional): shuffle the elements before batching.
          Default: ``True``
    """

    def __init__(self, dataset1, dataset2, labels, sizes1, sizes2, dictionary):
        self.dataset1, self.dataset2 = dataset1, dataset2
        self.sizes1, self.sizes2 = np.array(sizes1), np.array(sizes2)
        self.labels = np.array(labels)
        self.vocab = dictionary
        self.shuffle = False

    def __getitem__(self, index):
        sent1 = self.dataset1[index]
        sent2 = self.dataset2[index]
        lbl = self.labels[index]
        return {'id': index, 'sentence1': sent1, 'sentence2': sent2, 'target': torch.LongTensor([lbl])}

    def __len__(self):
        return len(self.dataset1)

    def collater(self, samples):
        return collate(samples, self.vocab.pad(), self.vocab.eos())

    def get_dummy_batch(self, num_tokens, max_positions, tgt_len=128):
        """Return a dummy batch with a given number of tokens."""
        if isinstance(max_positions, float) or isinstance(max_positions, int):
            tgt_len = min(tgt_len, max_positions)
        bsz = num_tokens // tgt_len
        sent1 = self.vocab.dummy_sentence(tgt_len + 2)
        sent2 = self.vocab.dummy_sentence(tgt_len + 2)

        return self.collater([
            {'id': i, 'sentence1': sent1, 'sentence2': sent2, 'target': torch.LongTensor([0])}
            for i in range(bsz)
        ])

    def num_tokens(self, index):
        """Return the number of tokens in a sample. This value is used to
        enforce ``--max-tokens`` during batching."""
        return max(self.sizes1[index], self.sizes2[index])

    def size(self, index):
        """Return an example's size as a float or tuple. This value is used when
        filtering a dataset with ``--max-positions``."""
        return max(self.sizes1[index], self.sizes2[index])

    def ordered_indices(self):
        """Return an ordered list of indices. Batches will be constructed based
        on this order."""
        if self.shuffle:
            indices = np.random.permutation(len(self))
        else:
            indices = np.arange(len(self))
        indices = indices[np.argsort(self.sizes1[indices], kind='mergesort')]
        return indices[np.argsort(self.sizes2[indices], kind='mergesort')]
