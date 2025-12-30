"""
HDF5-backed Dataset classes for streaming training.

v3: OptimizedPhase2Dataset loads metadata into RAM, reads features from disk (local NVMe).
v4: Added set_mined_negatives() support for Stage B curriculum training.
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
    """
    Phase 3 Dataset with curriculum hard negatives.

    Supports two negative mining stages:
    - Stage A: Orthographically close, phonetically distant (computed on-the-fly)
    - Stage B: Model-mined hard negatives (set via set_mined_negatives())
    """

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

        # Stage B: mined negatives (set via set_mined_negatives)
        self._mined_negatives: Optional[Dict[int, List[int]]] = None

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

    def set_mined_negatives(self, mined_negatives: Dict[int, List[int]]):
        """
        Set model-mined hard negatives for Stage B training.

        Args:
            mined_negatives: Dict mapping anchor_idx -> list of hard negative indices
        """
        self._mined_negatives = mined_negatives
        self.negative_stage = 'B'
        total = sum(len(v) for v in mined_negatives.values())
        print(f"  Set {total:,} mined negatives for {len(mined_negatives):,} anchors")

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
        """Stage A: Find orthographically close but phonetically distant negative."""
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

    def _get_stage_b_negative(self, source_idx: int, anchor_idx: int, anchor_cluster: int) -> int:
        """Stage B: Use model-mined hard negatives."""
        if self._mined_negatives and anchor_idx in self._mined_negatives:
            negatives = self._mined_negatives[anchor_idx]
            if negatives:
                return random.choice(negatives)
        # Fallback to random if no mined negative available
        return self._random_negative(source_idx, anchor_cluster, {anchor_idx})

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        source_idx, pair_idx = self.combined_indices[idx]
        f = self._get_file(source_idx)
        items = f['items']
        phon_count = self.source_phon_counts[source_idx]
        pairs = f['pairs_with_phonetic'] if pair_idx < phon_count else f['pairs_without_phonetic']
        local_idx = pair_idx if pair_idx < phon_count else pair_idx - phon_count
        anchor_idx, positive_idx = int(pairs['anchor_idx'][local_idx]), int(pairs['positive_idx'][local_idx])
        anchor_cluster = int(items['cluster_id'][anchor_idx])

        # Select negative based on stage
        if self.negative_stage == 'B':
            negative_idx = self._get_stage_b_negative(source_idx, anchor_idx, anchor_cluster)
        elif self.negative_stage == 'A':
            negative_idx = self._get_stage_a_negative(source_idx, anchor_idx, anchor_cluster)
        else:
            negative_idx = self._random_negative(source_idx, anchor_cluster, {anchor_idx, positive_idx})

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

    Loads all data into RAM at startup. Supports sharing loaded data between
    train/val splits to avoid double memory usage.
    """

    def __init__(self, hdf5_paths: List[str], oversample_factors: List[int],
                 split: str = 'train', train_ratio: float = 0.9, seed: int = 42,
                 shared_data: Optional[Dict] = None):
        """
        Args:
            shared_data: If provided, reuse already-loaded arrays instead of loading again.
                         Pass the train dataset's get_shared_data() to the val dataset.
        """
        self.rng = np.random.default_rng(seed if split == 'train' else seed + 1)

        # Either use shared data or load fresh
        if shared_data is not None:
            print(f"OptimizedPhase2Dataset ({split}): Reusing shared data from train set...")
            self._char_ids = shared_data['char_ids']
            self._char_lengths = shared_data['char_lengths']
            self._lang_ids = shared_data['lang_ids']
            self._features = shared_data['features']
            self._feat_lengths = shared_data['feat_lengths']
            self._total_items = shared_data['total_items']
        else:
            # Load EVERYTHING into memory
            self._char_ids: Dict[int, np.ndarray] = {}
            self._char_lengths: Dict[int, np.ndarray] = {}
            self._lang_ids: Dict[int, np.ndarray] = {}
            self._features: Dict[int, np.ndarray] = {}
            self._feat_lengths: Dict[int, np.ndarray] = {}
            self._total_items: Dict[int, int] = {}

            total_bytes = 0
            print(f"OptimizedPhase2Dataset ({split}): Loading ALL data into RAM...")

            for source_idx, path in enumerate(hdf5_paths):
                with h5py.File(path, 'r') as f:
                    total_items = f.attrs['total_items']
                    self._total_items[source_idx] = total_items

                    # Load ALL arrays into memory
                    print(f"  Source {source_idx}: Loading {total_items:,} items...", end=" ", flush=True)
                    self._char_ids[source_idx] = f['items/char_ids'][:]
                    self._char_lengths[source_idx] = f['items/char_lengths'][:]
                    self._lang_ids[source_idx] = f['items/lang_ids'][:]
                    self._features[source_idx] = f['features/data'][:]
                    self._feat_lengths[source_idx] = f['features/lengths'][:]

                    source_bytes = (
                        self._char_ids[source_idx].nbytes +
                        self._char_lengths[source_idx].nbytes +
                        self._lang_ids[source_idx].nbytes +
                        self._features[source_idx].nbytes +
                        self._feat_lengths[source_idx].nbytes
                    )
                    total_bytes += source_bytes
                    print(f"{source_bytes/1024**3:.2f} GB")

            print(f"  Total memory: {total_bytes/1024**3:.2f} GB", flush=True)

        # Build index for this split
        self.combined_indices = []

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            total_items = self._total_items[source_idx]

            indices = np.arange(total_items)
            self.rng.shuffle(indices)
            split_idx = int(total_items * train_ratio)
            source_indices = indices[:split_idx] if split == 'train' else indices[split_idx:]

            # Apply oversampling
            for _ in range(factor):
                for item_idx in source_indices:
                    self.combined_indices.append((source_idx, item_idx))

            print(f"  {split} Source {source_idx}: {len(source_indices):,} items × {factor}x = {len(source_indices)*factor:,}")

        self.rng.shuffle(self.combined_indices)
        print(f"  {split} total: {len(self.combined_indices):,} samples", flush=True)

    def get_shared_data(self) -> Dict:
        """Return data arrays for sharing with val dataset."""
        return {
            'char_ids': self._char_ids,
            'char_lengths': self._char_lengths,
            'lang_ids': self._lang_ids,
            'features': self._features,
            'feat_lengths': self._feat_lengths,
            'total_items': self._total_items,
        }

    def __len__(self):
        return len(self.combined_indices)

    def __getitem__(self, idx: int) -> Dict[str, np.ndarray]:
        source_idx, item_idx = self.combined_indices[idx]

        # ALL data from memory - ZERO disk I/O
        char_len = int(self._char_lengths[source_idx][item_idx])
        char_ids = self._char_ids[source_idx][item_idx, :char_len].copy()
        lang_id = self._lang_ids[source_idx][item_idx]

        feat_len = int(self._feat_lengths[source_idx][item_idx])
        features = self._features[source_idx][item_idx, :feat_len, :].copy()

        return {
            'char_ids': char_ids,
            'lang_id': lang_id,
            'phonetic_features': features,
        }


class OptimizedPhase3Dataset(Dataset):
    """
    FULLY IN-MEMORY Phase 3 Dataset with pre-mined hard negatives.

    Supports sharing loaded data between train/val splits to avoid double memory usage.

    For Stage B training, call set_mined_negatives() after construction to
    override the pre-computed negatives with model-mined hard negatives.
    """

    def __init__(self, hdf5_paths: List[str], oversample_factors: List[int],
                 split: str = 'train', train_ratio: float = 0.9,
                 subsample_triplets: int = Config.SUBSAMPLE_PAIRS, seed: int = 42,
                 shared_data: Optional[Dict] = None):
        self.rng = np.random.default_rng(seed if split == 'train' else seed + 1)
        self.split = split

        # Stage B support
        self._mined_negatives: Optional[Dict[int, List[int]]] = None
        self._use_mined_negatives = False

        if shared_data is not None:
            print(f"OptimizedPhase3Dataset ({split}): Reusing shared data from train set...")
            self._triplet_anchors = shared_data['triplet_anchors']
            self._triplet_positives = shared_data['triplet_positives']
            self._triplet_negatives = shared_data['triplet_negatives']
            self._char_ids = shared_data['char_ids']
            self._char_lengths = shared_data['char_lengths']
            self._lang_ids = shared_data['lang_ids']
            self._total_triplets = shared_data['total_triplets']
            self._total_items = shared_data.get('total_items', {})
        else:
            # All loaded into memory (no features, just indices and char encodings)
            self._triplet_anchors: Dict[int, np.ndarray] = {}
            self._triplet_positives: Dict[int, np.ndarray] = {}
            self._triplet_negatives: Dict[int, np.ndarray] = {}
            self._char_ids: Dict[int, np.ndarray] = {}
            self._char_lengths: Dict[int, np.ndarray] = {}
            self._lang_ids: Dict[int, np.ndarray] = {}
            self._total_triplets: Dict[int, int] = {}
            self._total_items: Dict[int, int] = {}

            total_bytes = 0
            print(f"OptimizedPhase3Dataset ({split}): Loading data into RAM...")

            for source_idx, path in enumerate(hdf5_paths):
                with h5py.File(path, 'r') as f:
                    self._triplet_anchors[source_idx] = f['triplets/anchor_idx'][:]
                    self._triplet_positives[source_idx] = f['triplets/positive_idx'][:]
                    self._triplet_negatives[source_idx] = f['triplets/negative_idx'][:]
                    self._char_ids[source_idx] = f['items/char_ids'][:]
                    self._char_lengths[source_idx] = f['items/char_lengths'][:]
                    self._lang_ids[source_idx] = f['items/lang_ids'][:]
                    self._total_triplets[source_idx] = len(self._triplet_anchors[source_idx])
                    self._total_items[source_idx] = len(self._char_ids[source_idx])

                    total_bytes += sum(arr.nbytes for arr in [
                        self._triplet_anchors[source_idx], self._triplet_positives[source_idx],
                        self._triplet_negatives[source_idx], self._char_ids[source_idx],
                        self._char_lengths[source_idx], self._lang_ids[source_idx]
                    ])

            print(f"  Total memory: {total_bytes/1024**3:.2f} GB", flush=True)

        # Build index for this split
        self.combined_indices = []
        self.hdf5_paths = hdf5_paths  # Store for reference

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            total_triplets = self._total_triplets[source_idx]
            max_t = min(total_triplets, subsample_triplets)
            indices = np.arange(total_triplets)
            self.rng.shuffle(indices)
            split_idx = int(max_t * train_ratio)
            source_indices = indices[:split_idx] if split == 'train' else indices[split_idx:max_t]

            for _ in range(factor):
                for t_idx in source_indices:
                    self.combined_indices.append((source_idx, t_idx))

            print(f"  {split} Source {source_idx}: {len(source_indices):,} triplets × {factor}x = {len(source_indices)*factor:,}")

        self.rng.shuffle(self.combined_indices)
        print(f"  {split} total: {len(self.combined_indices):,} samples", flush=True)

    def set_mined_negatives(self, mined_negatives: Dict[int, List[int]]):
        """
        Set model-mined hard negatives for Stage B training.

        This switches the dataset from using pre-computed Stage A negatives
        to using the provided model-mined hard negatives.

        Args:
            mined_negatives: Dict mapping anchor_idx -> list of hard negative indices
        """
        self._mined_negatives = mined_negatives
        self._use_mined_negatives = True
        total = sum(len(v) for v in mined_negatives.values())
        print(f"  OptimizedPhase3Dataset ({self.split}): Set {total:,} mined negatives for {len(mined_negatives):,} anchors")

    def get_shared_data(self) -> Dict:
        """Return data arrays for sharing with val dataset."""
        return {
            'triplet_anchors': self._triplet_anchors,
            'triplet_positives': self._triplet_positives,
            'triplet_negatives': self._triplet_negatives,
            'char_ids': self._char_ids,
            'char_lengths': self._char_lengths,
            'lang_ids': self._lang_ids,
            'total_triplets': self._total_triplets,
            'total_items': self._total_items,
        }

    def __len__(self):
        return len(self.combined_indices)

    def _get_mined_negative(self, source_idx: int, anchor_idx: int) -> int:
        """Get a mined hard negative for the anchor, or random fallback.

        Note: Mined negatives are global indices from the mining data file.
        We only use them if they're valid for this source's item count.
        """
        if self._mined_negatives and anchor_idx in self._mined_negatives:
            negatives = self._mined_negatives[anchor_idx]
            if negatives:
                # Filter to valid indices for this source
                total_items = self._total_items.get(source_idx, len(self._char_ids[source_idx]))
                valid_negatives = [n for n in negatives if n < total_items]
                if valid_negatives:
                    return random.choice(valid_negatives)

        # Fallback: random item from same source
        total_items = self._total_items.get(source_idx, len(self._char_ids[source_idx]))
        return random.randint(0, total_items - 1)

    def __getitem__(self, idx: int) -> Dict[str, np.ndarray]:
        source_idx, triplet_idx = self.combined_indices[idx]

        a_idx = int(self._triplet_anchors[source_idx][triplet_idx])
        p_idx = int(self._triplet_positives[source_idx][triplet_idx])

        # Use mined negative if Stage B, otherwise use pre-computed
        if self._use_mined_negatives:
            n_idx = self._get_mined_negative(source_idx, a_idx)
        else:
            n_idx = int(self._triplet_negatives[source_idx][triplet_idx])

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