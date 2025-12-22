"""
HDF5-backed Dataset classes for streaming training.

Replaces in-memory Datasets with on-disk HDF5 access.
Memory footprint stays constant regardless of dataset size.
"""

import h5py
import torch
import random
import numpy as np
from torch.utils.data import Dataset
from collections import defaultdict
from typing import Dict, List


class StreamingPhase1Dataset(Dataset):
    """Phase 1 Dataset with HDF5 streaming."""

    def __init__(self, hdf5_path: str, split: str = 'train', train_ratio: float = 0.9):
        self.hdf5_path = hdf5_path
        self.split = split

        with h5py.File(hdf5_path, 'r') as f:
            total_pairs = f.attrs['pairs_with_phonetic']
            split_idx = int(total_pairs * train_ratio)

            if split == 'train':
                self.pair_indices = list(range(split_idx))
            else:
                self.pair_indices = list(range(split_idx, total_pairs))

            items = f['items']
            self.phonetic_item_indices = []

            for idx in range(f.attrs['total_items']):
                if items['has_phonetic'][idx]:
                    self.phonetic_item_indices.append(idx)

            self.cluster_to_items = defaultdict(list)
            for idx in self.phonetic_item_indices:
                cluster_id = int(items['cluster_id'][idx])
                self.cluster_to_items[cluster_id].append(idx)

            self.cluster_ids = list(self.cluster_to_items.keys())

        print(f"StreamingPhase1Dataset ({split}): {len(self.pair_indices):,} pairs")

    def __len__(self) -> int:
        return len(self.pair_indices)

    def _load_features(self, f: h5py.File, item_idx: int) -> np.ndarray:
        feature_key = str(item_idx)
        if feature_key in f['features']:
            return f['features'][feature_key][:]
        return np.array([])

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        with h5py.File(self.hdf5_path, 'r') as f:
            pairs = f['pairs_with_phonetic']
            items = f['items']

            pair_idx = self.pair_indices[idx]
            anchor_idx = int(pairs['anchor_idx'][pair_idx])
            positive_idx = int(pairs['positive_idx'][pair_idx])

            anchor_cluster = int(items['cluster_id'][anchor_idx])
            neg_cluster = random.choice([c for c in self.cluster_ids if c != anchor_cluster])
            negative_idx = random.choice(self.cluster_to_items[neg_cluster])

            anchor_features = self._load_features(f, anchor_idx)
            positive_features = self._load_features(f, positive_idx)
            negative_features = self._load_features(f, negative_idx)

        return {
            'anchor_features': torch.tensor(anchor_features, dtype=torch.float32),
            'positive_features': torch.tensor(positive_features, dtype=torch.float32),
            'negative_features': torch.tensor(negative_features, dtype=torch.float32),
        }


class StreamingPhase2Dataset(Dataset):
    """Phase 2 Dataset with HDF5 streaming."""

    def __init__(self, hdf5_path: str, char_vocab, lang_vocab, split: str = 'train', train_ratio: float = 0.9):
        self.hdf5_path = hdf5_path
        self.char_vocab = char_vocab
        self.lang_vocab = lang_vocab
        self.split = split

        with h5py.File(hdf5_path, 'r') as f:
            items = f['items']
            self.item_indices = []

            for idx in range(f.attrs['total_items']):
                if items['has_phonetic'][idx]:
                    self.item_indices.append(idx)

            random.shuffle(self.item_indices)
            split_idx = int(len(self.item_indices) * train_ratio)

            if split == 'train':
                self.item_indices = self.item_indices[:split_idx]
            else:
                self.item_indices = self.item_indices[split_idx:]

        print(f"StreamingPhase2Dataset ({split}): {len(self.item_indices):,} items")

    def __len__(self) -> int:
        return len(self.item_indices)

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        with h5py.File(self.hdf5_path, 'r') as f:
            items = f['items']
            item_idx = self.item_indices[idx]

            romanized = items['romanized'][item_idx]
            lang = items['lang'][item_idx]

            feature_key = str(item_idx)
            phonetic_features = f['features'][feature_key][:]

        char_ids = self.char_vocab.encode(romanized)
        lang_id = self.lang_vocab.encode(lang)

        return {
            'char_ids': torch.tensor(char_ids, dtype=torch.long),
            'lang_id': torch.tensor(lang_id, dtype=torch.long),
            'phonetic_features': torch.tensor(phonetic_features, dtype=torch.float32),
        }


class StreamingPhase3Dataset(Dataset):
    """Phase 3 Dataset with HDF5 streaming and hard negative mining."""

    def __init__(self, hdf5_path: str, char_vocab, lang_vocab, split: str = 'train', train_ratio: float = 0.9):
        self.hdf5_path = hdf5_path
        self.char_vocab = char_vocab
        self.lang_vocab = lang_vocab
        self.split = split

        with h5py.File(hdf5_path, 'r') as f:
            phon_count = f.attrs['pairs_with_phonetic']
            no_phon_count = f.attrs['pairs_without_phonetic']
            total_pairs = phon_count + no_phon_count

            split_idx = int(total_pairs * train_ratio)

            if split == 'train':
                self.pair_indices = list(range(split_idx))
            else:
                self.pair_indices = list(range(split_idx, total_pairs))

            self.phonetic_pairs = [i for i in self.pair_indices if i < phon_count]
            self.non_phonetic_pairs = [i - phon_count for i in self.pair_indices if i >= phon_count]

            items = f['items']
            total_items = f.attrs['total_items']

            self.cluster_to_items = defaultdict(list)
            for idx in range(total_items):
                cluster_id = int(items['cluster_id'][idx])
                self.cluster_to_items[cluster_id].append(idx)

            self.cluster_ids = list(self.cluster_to_items.keys())

            self.index_by_first_char = defaultdict(list)
            for idx in range(total_items):
                romanized = items['romanized'][idx]
                if romanized:
                    first_char = romanized[0]
                    self.index_by_first_char[first_char].append(idx)

        print(f"StreamingPhase3Dataset ({split}): {len(self.pair_indices):,} pairs")

    def __len__(self) -> int:
        return len(self.pair_indices)

    def _get_hard_negative(self, f: h5py.File, anchor_idx: int) -> int:
        items = f['items']

        romanized = items['romanized'][anchor_idx]
        anchor_len = len(romanized)
        anchor_cluster = int(items['cluster_id'][anchor_idx])
        first_char = romanized[0] if romanized else ''

        candidates = self.index_by_first_char.get(first_char, [])

        if len(candidates) < 5:
            return self._random_negative(anchor_cluster)

        random.shuffle(candidates)
        for neg_idx in candidates[:20]:
            neg_cluster = int(items['cluster_id'][neg_idx])
            if neg_cluster == anchor_cluster:
                continue

            neg_romanized = items['romanized'][neg_idx]
            neg_len = len(neg_romanized)
            if abs(neg_len - anchor_len) <= 2:
                return neg_idx

        for neg_idx in candidates[:20]:
            if int(items['cluster_id'][neg_idx]) != anchor_cluster:
                return neg_idx

        return self._random_negative(anchor_cluster)

    def _random_negative(self, anchor_cluster: int) -> int:
        neg_cluster = random.choice([c for c in self.cluster_ids if c != anchor_cluster])
        return random.choice(self.cluster_to_items[neg_cluster])

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        pair_idx = self.pair_indices[idx]

        with h5py.File(self.hdf5_path, 'r') as f:
            items = f['items']
            phon_count = f.attrs['pairs_with_phonetic']

            if pair_idx < phon_count:
                pairs = f['pairs_with_phonetic']
                local_idx = pair_idx
            else:
                pairs = f['pairs_without_phonetic']
                local_idx = pair_idx - phon_count

            anchor_idx = int(pairs['anchor_idx'][local_idx])
            positive_idx = int(pairs['positive_idx'][local_idx])
            negative_idx = self._get_hard_negative(f, anchor_idx)

            anchor_rom = items['romanized'][anchor_idx]
            anchor_lang = items['lang'][anchor_idx]
            positive_rom = items['romanized'][positive_idx]
            positive_lang = items['lang'][positive_idx]
            negative_rom = items['romanized'][negative_idx]
            negative_lang = items['lang'][negative_idx]

        return {
            'anchor_char_ids': torch.tensor(self.char_vocab.encode(anchor_rom), dtype=torch.long),
            'anchor_lang_id': torch.tensor(self.lang_vocab.encode(anchor_lang), dtype=torch.long),
            'positive_char_ids': torch.tensor(self.char_vocab.encode(positive_rom), dtype=torch.long),
            'positive_lang_id': torch.tensor(self.lang_vocab.encode(positive_lang), dtype=torch.long),
            'negative_char_ids': torch.tensor(self.char_vocab.encode(negative_rom), dtype=torch.long),
            'negative_lang_id': torch.tensor(self.lang_vocab.encode(negative_lang), dtype=torch.long),
        }


# Example usage
if __name__ == '__main__':
    """
    # Replace Dataset initialization in training functions:

    # Old:
    train_dataset = Phase1Dataset(pairs[:split], items)

    # New:
    train_dataset = StreamingPhase1Dataset('data.h5', split='train')

    # Everything else stays the same!
    train_loader = DataLoader(
        train_dataset, batch_size=128, shuffle=True,
        collate_fn=collate_phase1, num_workers=4
    )
    """
    pass