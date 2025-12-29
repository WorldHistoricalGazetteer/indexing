"""
HDF5-backed Dataset classes for streaming training.

v3: OptimizedPhase2Dataset now loads ALL data into RAM for zero disk I/O
"""

import random
from collections import defaultdict
from typing import Dict, List, Optional

import h5py
import numpy as np
import torch
from torch.utils.data import Dataset

from .config import Config


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


class MultiSourcePhase1Dataset(Dataset):
    """Phase 1 Dataset combining multiple HDF5 sources with oversampling."""

    def __init__(self, hdf5_paths: List[str], oversample_factors: List[int],
                 split: str = 'train', train_ratio: float = 0.9,
                 subsample_pairs: int = Config.SUBSAMPLE_PAIRS, seed: int = 42):
        self.hdf5_paths = hdf5_paths
        self.oversample_factors = oversample_factors
        self.split = split
        self.rng = np.random.default_rng(seed if split == 'train' else seed + 1)
        self._file_handles: Dict[int, h5py.File] = {}
        self.combined_indices = []
        self.source_pair_counts = []
        self.source_phonetic_indices = []
        self.source_phonetic_set = []
        self.source_cluster_maps = []
        self.source_cluster_ids_excl = []
        self.source_feature_keys = []

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                total_pairs = f.attrs['pairs_with_phonetic']
                max_pairs = min(total_pairs, subsample_pairs)
                all_indices = np.arange(total_pairs)
                self.rng.shuffle(all_indices)
                sampled_indices = all_indices[:max_pairs].tolist()
                split_idx = int(max_pairs * train_ratio)
                source_indices = sampled_indices[:split_idx] if split == 'train' else sampled_indices[split_idx:]
                self.source_pair_counts.append(len(source_indices))
                for _ in range(factor):
                    for pair_idx in source_indices:
                        self.combined_indices.append((source_idx, pair_idx))
                feature_keys = set(f['features'].keys())
                self.source_feature_keys.append(feature_keys)
                items = f['items']
                phonetic_indices = []
                cluster_to_items = defaultdict(list)
                for idx in range(f.attrs['total_items']):
                    if items['has_phonetic'][idx] and str(idx) in feature_keys:
                        phonetic_indices.append(idx)
                        cluster_to_items[int(items['cluster_id'][idx])].append(idx)
                self.source_phonetic_indices.append(phonetic_indices)
                self.source_phonetic_set.append(set(phonetic_indices))
                self.source_cluster_maps.append(dict(cluster_to_items))
                cluster_ids = list(cluster_to_items.keys())
                self.source_cluster_ids_excl.append({c: [x for x in cluster_ids if x != c] for c in cluster_ids})
        self.rng.shuffle(self.combined_indices)
        print(f"MultiSourcePhase1Dataset ({split}): {len(self.combined_indices):,} samples", flush=True)

    def _get_file(self, source_idx: int) -> h5py.File:
        if source_idx not in self._file_handles:
            self._file_handles[source_idx] = h5py.File(self.hdf5_paths[source_idx], 'r')
        return self._file_handles[source_idx]

    def __len__(self): return len(self.combined_indices)

    def __getitem__(self, idx: int) -> Dict[str, np.ndarray]:
        source_idx, pair_idx = self.combined_indices[idx]
        f = self._get_file(source_idx)
        pairs = f['pairs_with_phonetic']
        items = f['items']
        anchor_idx = int(pairs['anchor_idx'][pair_idx])
        positive_idx = int(pairs['positive_idx'][pair_idx])
        anchor_cluster = int(items['cluster_id'][anchor_idx])
        other_clusters = self.source_cluster_ids_excl[source_idx].get(anchor_cluster, [])
        if other_clusters:
            neg_cluster = self.rng.choice(other_clusters)
            cluster_items = self.source_cluster_maps[source_idx][neg_cluster]
            negative_idx = cluster_items[self.rng.integers(len(cluster_items))]
        else:
            available = self.source_phonetic_set[source_idx] - {anchor_idx, positive_idx}
            negative_idx = self.rng.choice(list(available)) if available else positive_idx
        return {
            'anchor_features': f['features'][str(anchor_idx)][:] if str(anchor_idx) in self.source_feature_keys[source_idx] else np.array([], dtype=np.float32),
            'positive_features': f['features'][str(positive_idx)][:] if str(positive_idx) in self.source_feature_keys[source_idx] else np.array([], dtype=np.float32),
            'negative_features': f['features'][str(negative_idx)][:] if str(negative_idx) in self.source_feature_keys[source_idx] else np.array([], dtype=np.float32),
        }

    def __del__(self):
        for f in self._file_handles.values():
            try: f.close()
            except: pass


class MultiSourcePhase2Dataset(Dataset):
    """Phase 2 Dataset combining multiple HDF5 sources."""

    def __init__(self, hdf5_paths: List[str], oversample_factors: List[int],
                 char_vocab, lang_vocab, split: str = 'train', train_ratio: float = 0.9):
        self.hdf5_paths = hdf5_paths
        self.char_vocab = char_vocab
        self.lang_vocab = lang_vocab
        self._file_handles: Dict[int, h5py.File] = {}
        self.combined_indices = []
        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                items = f['items']
                item_indices = [idx for idx in range(f.attrs['total_items']) if items['has_phonetic'][idx]]
                random.shuffle(item_indices)
                split_idx = int(len(item_indices) * train_ratio)
                source_indices = item_indices[:split_idx] if split == 'train' else item_indices[split_idx:]
                for _ in range(factor):
                    for item_idx in source_indices:
                        self.combined_indices.append((source_idx, item_idx))
        random.shuffle(self.combined_indices)
        print(f"MultiSourcePhase2Dataset ({split}): {len(self.combined_indices):,} samples", flush=True)

    def _get_file(self, source_idx: int) -> h5py.File:
        if source_idx not in self._file_handles:
            self._file_handles[source_idx] = h5py.File(self.hdf5_paths[source_idx], 'r')
        return self._file_handles[source_idx]

    def __len__(self): return len(self.combined_indices)

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        source_idx, item_idx = self.combined_indices[idx]
        f = self._get_file(source_idx)
        items = f['items']
        return {
            'char_ids': torch.tensor(self.char_vocab.encode(items['romanized'][item_idx]), dtype=torch.long),
            'lang_id': torch.tensor(self.lang_vocab.encode(items['lang'][item_idx]), dtype=torch.long),
            'phonetic_features': torch.tensor(f['features'][str(item_idx)][:], dtype=torch.float32),
        }

    def __del__(self):
        for f in self._file_handles.values():
            try: f.close()
            except: pass


class MultiSourcePhase3Dataset(Dataset):
    """Phase 3 Dataset with curriculum hard negatives."""

    def __init__(self, hdf5_paths: List[str], oversample_factors: List[int],
                 char_vocab, lang_vocab, split: str = 'train', train_ratio: float = 0.9,
                 subsample_pairs: int = Config.SUBSAMPLE_PAIRS, negative_stage: str = 'A'):
        self.hdf5_paths = hdf5_paths
        self.char_vocab = char_vocab
        self.lang_vocab = lang_vocab
        self.negative_stage = negative_stage
        self._file_handles: Dict[int, h5py.File] = {}
        self.combined_indices = []
        self.source_cluster_maps = []
        self.source_first_char_indices = []
        self.source_item_romanized = []
        self.source_item_ipa = []
        self.source_phon_counts = []
        self.source_all_items = []

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                phon_count = f.attrs['pairs_with_phonetic']
                total_pairs = phon_count + f.attrs.get('pairs_without_phonetic', 0)
                self.source_phon_counts.append(phon_count)
                max_pairs = min(total_pairs, subsample_pairs)
                all_indices = list(range(total_pairs))
                random.shuffle(all_indices)
                split_idx = int(max_pairs * train_ratio)
                source_indices = all_indices[:split_idx] if split == 'train' else all_indices[split_idx:max_pairs]
                for _ in range(factor):
                    for pair_idx in source_indices:
                        self.combined_indices.append((source_idx, pair_idx))
                items = f['items']
                total_items = f.attrs['total_items']
                cluster_to_items = defaultdict(list)
                first_char_index = defaultdict(list)
                item_romanized = {}
                item_ipa = {}
                for idx in range(total_items):
                    cluster_to_items[int(items['cluster_id'][idx])].append(idx)
                    rom = items['romanized'][idx]
                    if isinstance(rom, bytes): rom = rom.decode('utf-8')
                    item_romanized[idx] = rom
                    if rom: first_char_index[rom[0].lower()].append(idx)
                    ipa = items['ipa'][idx]
                    if isinstance(ipa, bytes): ipa = ipa.decode('utf-8')
                    item_ipa[idx] = ipa
                self.source_cluster_maps.append(dict(cluster_to_items))
                self.source_first_char_indices.append(dict(first_char_index))
                self.source_item_romanized.append(item_romanized)
                self.source_item_ipa.append(item_ipa)
                self.source_all_items.append(list(range(total_items)))
        random.shuffle(self.combined_indices)
        print(f"MultiSourcePhase3Dataset ({split}): {len(self.combined_indices):,} samples", flush=True)

    def _get_file(self, source_idx: int) -> h5py.File:
        if source_idx not in self._file_handles:
            self._file_handles[source_idx] = h5py.File(self.hdf5_paths[source_idx], 'r')
        return self._file_handles[source_idx]

    def __len__(self): return len(self.combined_indices)

    def _random_negative(self, source_idx: int, anchor_cluster: int, exclude: set) -> int:
        cluster_map = self.source_cluster_maps[source_idx]
        other = [c for c in cluster_map if c != anchor_cluster]
        if other: return random.choice(cluster_map[random.choice(other)])
        available = [i for i in self.source_all_items[source_idx] if i not in exclude]
        return random.choice(available) if available else random.choice(self.source_all_items[source_idx])

    def _get_stage_a_negative(self, source_idx: int, anchor_idx: int, anchor_cluster: int) -> int:
        anchor_rom = self.source_item_romanized[source_idx].get(anchor_idx, '')
        if not anchor_rom: return self._random_negative(source_idx, anchor_cluster, {anchor_idx})
        first_char = anchor_rom[0].lower()
        candidates = list(self.source_first_char_indices[source_idx].get(first_char, []))
        for off in [-1, 1, -2, 2]:
            candidates.extend(self.source_first_char_indices[source_idx].get(chr(ord(first_char)+off), []))
        if len(candidates) < 5: return self._random_negative(source_idx, anchor_cluster, {anchor_idx})
        random.shuffle(candidates)
        cluster_map = self.source_cluster_maps[source_idx]
        anchor_ipa = self.source_item_ipa[source_idx].get(anchor_idx, '')
        best_idx, best_score = None, -float('inf')
        for neg_idx in candidates[:50]:
            neg_cluster = next((c for c, items in cluster_map.items() if neg_idx in items), None)
            if neg_cluster == anchor_cluster: continue
            neg_rom = self.source_item_romanized[source_idx].get(neg_idx, '')
            if not neg_rom: continue
            edit = _levenshtein_distance(anchor_rom.lower(), neg_rom.lower())
            if edit > Config.STAGE_A_EDIT_DISTANCE_MAX: continue
            neg_ipa = self.source_item_ipa[source_idx].get(neg_idx, '')
            if anchor_ipa and neg_ipa:
                phon = _levenshtein_distance(anchor_ipa, neg_ipa) / max(len(anchor_ipa), len(neg_ipa), 1)
                if phon < Config.STAGE_A_PHONETIC_DISTANCE_MIN: continue
                score = phon - edit * 0.1
            else:
                score = -edit
            if score > best_score: best_score, best_idx = score, neg_idx
        return best_idx if best_idx else self._random_negative(source_idx, anchor_cluster, {anchor_idx})

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        source_idx, pair_idx = self.combined_indices[idx]
        f = self._get_file(source_idx)
        items = f['items']
        phon_count = self.source_phon_counts[source_idx]
        pairs = f['pairs_with_phonetic'] if pair_idx < phon_count else f['pairs_without_phonetic']
        local_idx = pair_idx if pair_idx < phon_count else pair_idx - phon_count
        anchor_idx, positive_idx = int(pairs['anchor_idx'][local_idx]), int(pairs['positive_idx'][local_idx])
        anchor_cluster = int(items['cluster_id'][anchor_idx])
        negative_idx = self._get_stage_a_negative(source_idx, anchor_idx, anchor_cluster) if self.negative_stage == 'A' else self._random_negative(source_idx, anchor_cluster, {anchor_idx, positive_idx})
        return {
            'anchor_char_ids': torch.tensor(self.char_vocab.encode(items['romanized'][anchor_idx]), dtype=torch.long),
            'anchor_lang_id': torch.tensor(self.lang_vocab.encode(items['lang'][anchor_idx]), dtype=torch.long),
            'positive_char_ids': torch.tensor(self.char_vocab.encode(items['romanized'][positive_idx]), dtype=torch.long),
            'positive_lang_id': torch.tensor(self.lang_vocab.encode(items['lang'][positive_idx]), dtype=torch.long),
            'negative_char_ids': torch.tensor(self.char_vocab.encode(items['romanized'][negative_idx]), dtype=torch.long),
            'negative_lang_id': torch.tensor(self.lang_vocab.encode(items['lang'][negative_idx]), dtype=torch.long),
        }

    def __del__(self):
        for f in self._file_handles.values():
            try: f.close()
            except: pass


class OptimizedPhase1Dataset(Dataset):
    """Ultra-fast Phase 1 using restructured HDF5."""

    def __init__(self, hdf5_paths: List[str], oversample_factors: List[int],
                 split: str = 'train', train_ratio: float = 0.9,
                 subsample_triplets: int = Config.SUBSAMPLE_PAIRS, seed: int = 42):
        self.hdf5_paths = hdf5_paths
        self.rng = np.random.default_rng(seed if split == 'train' else seed + 1)
        self._file_handles: Dict[int, h5py.File] = {}
        self._features_data: Dict[int, h5py.Dataset] = {}
        self._features_lengths: Dict[int, np.ndarray] = {}
        self._triplet_anchors: Dict[int, np.ndarray] = {}
        self._triplet_positives: Dict[int, np.ndarray] = {}
        self._triplet_negatives: Dict[int, np.ndarray] = {}
        self.combined_indices = []

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                self._features_lengths[source_idx] = f['features/lengths'][:]
                self._triplet_anchors[source_idx] = f['triplets/anchor_idx'][:]
                self._triplet_positives[source_idx] = f['triplets/positive_idx'][:]
                self._triplet_negatives[source_idx] = f['triplets/negative_idx'][:]
                total = len(self._triplet_anchors[source_idx])
            max_t = min(total, subsample_triplets)
            indices = np.arange(total)
            self.rng.shuffle(indices)
            split_idx = int(max_t * train_ratio)
            source_indices = indices[:split_idx] if split == 'train' else indices[split_idx:max_t]
            for _ in range(factor):
                for t_idx in source_indices:
                    self.combined_indices.append((source_idx, t_idx))
        self.rng.shuffle(self.combined_indices)
        print(f"OptimizedPhase1Dataset ({split}): {len(self.combined_indices):,} samples", flush=True)

    def _get_features_data(self, source_idx: int):
        if source_idx not in self._file_handles:
            self._file_handles[source_idx] = h5py.File(self.hdf5_paths[source_idx], 'r')
            self._features_data[source_idx] = self._file_handles[source_idx]['features/data']
        return self._features_data[source_idx]

    def __len__(self): return len(self.combined_indices)

    def __getitem__(self, idx: int) -> Dict[str, np.ndarray]:
        source_idx, triplet_idx = self.combined_indices[idx]
        a_idx = self._triplet_anchors[source_idx][triplet_idx]
        p_idx = self._triplet_positives[source_idx][triplet_idx]
        n_idx = self._triplet_negatives[source_idx][triplet_idx]
        feat = self._get_features_data(source_idx)
        return {
            'anchor_features': feat[a_idx, :self._features_lengths[source_idx][a_idx], :],
            'positive_features': feat[p_idx, :self._features_lengths[source_idx][p_idx], :],
            'negative_features': feat[n_idx, :self._features_lengths[source_idx][n_idx], :],
        }

    def __del__(self):
        for f in self._file_handles.values():
            try: f.close()
            except: pass


class OptimizedPhase2Dataset(Dataset):
    """
    FULLY IN-MEMORY Phase 2 Dataset - ZERO disk I/O during training.
    Loads all data (~2-3GB) into RAM at startup.
    """

    def __init__(self, hdf5_paths: List[str], oversample_factors: List[int],
                 split: str = 'train', train_ratio: float = 0.9, seed: int = 42):
        self.rng = np.random.default_rng(seed if split == 'train' else seed + 1)

        # Load EVERYTHING into memory
        self._char_ids: Dict[int, np.ndarray] = {}
        self._char_lengths: Dict[int, np.ndarray] = {}
        self._lang_ids: Dict[int, np.ndarray] = {}
        self._features: Dict[int, np.ndarray] = {}
        self._feat_lengths: Dict[int, np.ndarray] = {}
        self.combined_indices = []

        total_bytes = 0
        print(f"OptimizedPhase2Dataset ({split}): Loading ALL data into RAM...")

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                total_items = f.attrs['total_items']
                # Load ALL arrays into memory
                self._char_ids[source_idx] = f['items/char_ids'][:]
                self._char_lengths[source_idx] = f['items/char_lengths'][:]
                self._lang_ids[source_idx] = f['items/lang_ids'][:]
                self._features[source_idx] = f['features/data'][:]
                self._feat_lengths[source_idx] = f['features/lengths'][:]

                total_bytes += sum(arr.nbytes for arr in [
                    self._char_ids[source_idx], self._char_lengths[source_idx],
                    self._lang_ids[source_idx], self._features[source_idx],
                    self._feat_lengths[source_idx]
                ])

            indices = np.arange(total_items)
            self.rng.shuffle(indices)
            split_idx = int(total_items * train_ratio)
            source_indices = indices[:split_idx] if split == 'train' else indices[split_idx:]

            for _ in range(factor):
                for item_idx in source_indices:
                    self.combined_indices.append((source_idx, item_idx))

            print(f"  Source {source_idx}: {len(source_indices):,} items × {factor}x = {len(source_indices)*factor:,}")

        self.rng.shuffle(self.combined_indices)
        print(f"  Total: {len(self.combined_indices):,} samples | Memory: {total_bytes/1024**3:.2f} GB", flush=True)

    def __len__(self): return len(self.combined_indices)

    def __getitem__(self, idx: int) -> Dict[str, np.ndarray]:
        source_idx, item_idx = self.combined_indices[idx]
        char_len = int(self._char_lengths[source_idx][item_idx])
        feat_len = int(self._feat_lengths[source_idx][item_idx])
        return {
            'char_ids': self._char_ids[source_idx][item_idx, :char_len].copy(),
            'lang_id': self._lang_ids[source_idx][item_idx],
            'phonetic_features': self._features[source_idx][item_idx, :feat_len, :].copy(),
        }


class OptimizedPhase3Dataset(Dataset):
    """FULLY IN-MEMORY Phase 3 Dataset with pre-mined hard negatives."""

    def __init__(self, hdf5_paths: List[str], oversample_factors: List[int],
                 split: str = 'train', train_ratio: float = 0.9,
                 subsample_triplets: int = Config.SUBSAMPLE_PAIRS, seed: int = 42):
        self.rng = np.random.default_rng(seed if split == 'train' else seed + 1)

        self._triplet_anchors: Dict[int, np.ndarray] = {}
        self._triplet_positives: Dict[int, np.ndarray] = {}
        self._triplet_negatives: Dict[int, np.ndarray] = {}
        self._char_ids: Dict[int, np.ndarray] = {}
        self._char_lengths: Dict[int, np.ndarray] = {}
        self._lang_ids: Dict[int, np.ndarray] = {}
        self.combined_indices = []

        total_bytes = 0
        print(f"OptimizedPhase3Dataset ({split}): Loading ALL data into RAM...")

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                self._triplet_anchors[source_idx] = f['triplets/anchor_idx'][:]
                self._triplet_positives[source_idx] = f['triplets/positive_idx'][:]
                self._triplet_negatives[source_idx] = f['triplets/negative_idx'][:]
                self._char_ids[source_idx] = f['items/char_ids'][:]
                self._char_lengths[source_idx] = f['items/char_lengths'][:]
                self._lang_ids[source_idx] = f['items/lang_ids'][:]
                total_triplets = len(self._triplet_anchors[source_idx])

                total_bytes += sum(arr.nbytes for arr in [
                    self._triplet_anchors[source_idx], self._triplet_positives[source_idx],
                    self._triplet_negatives[source_idx], self._char_ids[source_idx],
                    self._char_lengths[source_idx], self._lang_ids[source_idx]
                ])

            max_t = min(total_triplets, subsample_triplets)
            indices = np.arange(total_triplets)
            self.rng.shuffle(indices)
            split_idx = int(max_t * train_ratio)
            source_indices = indices[:split_idx] if split == 'train' else indices[split_idx:max_t]

            for _ in range(factor):
                for t_idx in source_indices:
                    self.combined_indices.append((source_idx, t_idx))

            print(f"  Source {source_idx}: {len(source_indices):,} triplets × {factor}x = {len(source_indices)*factor:,}")

        self.rng.shuffle(self.combined_indices)
        print(f"  Total: {len(self.combined_indices):,} samples | Memory: {total_bytes/1024**3:.2f} GB", flush=True)

    def __len__(self): return len(self.combined_indices)

    def __getitem__(self, idx: int) -> Dict[str, np.ndarray]:
        source_idx, triplet_idx = self.combined_indices[idx]
        a_idx = self._triplet_anchors[source_idx][triplet_idx]
        p_idx = self._triplet_positives[source_idx][triplet_idx]
        n_idx = self._triplet_negatives[source_idx][triplet_idx]
        a_len = int(self._char_lengths[source_idx][a_idx])
        p_len = int(self._char_lengths[source_idx][p_idx])
        n_len = int(self._char_lengths[source_idx][n_idx])
        return {
            'anchor_char_ids': self._char_ids[source_idx][a_idx, :a_len].copy(),
            'anchor_lang_id': self._lang_ids[source_idx][a_idx],
            'positive_char_ids': self._char_ids[source_idx][p_idx, :p_len].copy(),
            'positive_lang_id': self._lang_ids[source_idx][p_idx],
            'negative_char_ids': self._char_ids[source_idx][n_idx, :n_len].copy(),
            'negative_lang_id': self._lang_ids[source_idx][n_idx],
        }