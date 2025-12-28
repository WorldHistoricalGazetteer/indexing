"""
HDF5-backed Dataset classes for streaming training.

Replaces in-memory Datasets with on-disk HDF5 access.
Memory footprint stays constant regardless of dataset size.

v2 Changes:
- StreamingPhase3Dataset now supports curriculum hard negatives:
  - Stage A: Orthographically close, phonetically distant
  - Stage B: Model-mined false positives (optional second pass)
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


# =============================================================================
# Multi-Source Dataset Classes
# =============================================================================

class MultiSourcePhase1Dataset(Dataset):
    """
    Phase 1 Dataset combining multiple HDF5 sources with oversampling.

    Each source can have a different oversample factor, allowing historical
    sources (Index Villaris, Pleiades) to be represented proportionally
    despite having fewer pairs than GeoNames.

    File handles are cached per worker process for performance.
    """

    def __init__(
        self,
        hdf5_paths: List[str],
        oversample_factors: List[int],
        split: str = 'train',
        train_ratio: float = 0.9,
        subsample_pairs: int = Config.SUBSAMPLE_PAIRS
    ):
        self.hdf5_paths = hdf5_paths
        self.oversample_factors = oversample_factors
        self.split = split

        # File handle cache (populated lazily per worker)
        self._file_handles: Dict[int, h5py.File] = {}

        # Build combined index: list of (source_idx, pair_idx_in_source)
        self.combined_indices = []

        # Per-source metadata
        self.source_pair_counts = []
        self.source_phonetic_indices = []  # List of phonetic item indices per source
        self.source_cluster_maps = []  # cluster_to_items per source

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                total_pairs = f.attrs['pairs_with_phonetic']

                # Subsample if needed
                max_pairs = min(total_pairs, subsample_pairs)
                all_indices = list(range(total_pairs))
                random.shuffle(all_indices)
                sampled_indices = all_indices[:max_pairs]

                # Split train/val
                split_idx = int(max_pairs * train_ratio)
                if split == 'train':
                    source_indices = sampled_indices[:split_idx]
                else:
                    source_indices = sampled_indices[split_idx:]

                self.source_pair_counts.append(len(source_indices))

                # Apply oversampling
                for _ in range(factor):
                    for pair_idx in source_indices:
                        self.combined_indices.append((source_idx, pair_idx))

                # Build phonetic item indices for this source
                items = f['items']
                phonetic_indices = []
                cluster_to_items = defaultdict(list)

                for idx in range(f.attrs['total_items']):
                    if items['has_phonetic'][idx]:
                        feature_key = str(idx)
                        if feature_key in f['features']:
                            feat_shape = f['features'][feature_key].shape
                            if feat_shape[0] > 0:
                                phonetic_indices.append(idx)
                                cluster_id = int(items['cluster_id'][idx])
                                cluster_to_items[cluster_id].append(idx)

                self.source_phonetic_indices.append(phonetic_indices)
                self.source_cluster_maps.append(dict(cluster_to_items))

        # Shuffle combined indices
        random.shuffle(self.combined_indices)

        print(f"MultiSourcePhase1Dataset ({split}):")
        for i, (path, count, factor) in enumerate(zip(hdf5_paths, self.source_pair_counts, oversample_factors)):
            effective = count * factor
            print(f"  Source {i}: {count:,} pairs × {factor}x = {effective:,} effective")
        print(f"  Total: {len(self.combined_indices):,} samples per epoch", flush=True)

    def _get_file(self, source_idx: int) -> h5py.File:
        """Get cached file handle for source, opening if needed."""
        if source_idx not in self._file_handles:
            self._file_handles[source_idx] = h5py.File(
                self.hdf5_paths[source_idx], 'r', swmr=True
            )
        return self._file_handles[source_idx]

    def __len__(self) -> int:
        return len(self.combined_indices)

    def _load_features(self, f: h5py.File, item_idx: int) -> np.ndarray:
        feature_key = str(item_idx)
        if feature_key in f['features']:
            return f['features'][feature_key][:]
        return np.array([])

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        source_idx, pair_idx = self.combined_indices[idx]
        f = self._get_file(source_idx)

        pairs = f['pairs_with_phonetic']
        items = f['items']

        anchor_idx = int(pairs['anchor_idx'][pair_idx])
        positive_idx = int(pairs['positive_idx'][pair_idx])

        # Get negative from same source
        anchor_cluster = int(items['cluster_id'][anchor_idx])
        cluster_map = self.source_cluster_maps[source_idx]
        cluster_ids = list(cluster_map.keys())

        # Find clusters different from anchor
        other_clusters = [c for c in cluster_ids if c != anchor_cluster]

        if other_clusters:
            # Normal case: sample from a different cluster
            neg_cluster = random.choice(other_clusters)
            negative_idx = random.choice(cluster_map[neg_cluster])
        else:
            # Edge case: only one cluster in this source
            available = [i for i in self.source_phonetic_indices[source_idx]
                        if i != anchor_idx and i != positive_idx]
            if available:
                negative_idx = random.choice(available)
            else:
                negative_idx = positive_idx

        anchor_features = self._load_features(f, anchor_idx)
        positive_features = self._load_features(f, positive_idx)
        negative_features = self._load_features(f, negative_idx)

        return {
            'anchor_features': torch.tensor(anchor_features, dtype=torch.float32),
            'positive_features': torch.tensor(positive_features, dtype=torch.float32),
            'negative_features': torch.tensor(negative_features, dtype=torch.float32),
        }

    def __del__(self):
        """Close any open file handles."""
        for f in self._file_handles.values():
            try:
                f.close()
            except:
                pass


class MultiSourcePhase2Dataset(Dataset):
    """
    Phase 2 Dataset combining multiple HDF5 sources with oversampling.

    Provides (character sequence, phonetic features) pairs for
    Student-Teacher alignment training.

    File handles are cached per worker process for performance.
    """

    def __init__(
        self,
        hdf5_paths: List[str],
        oversample_factors: List[int],
        char_vocab,
        lang_vocab,
        split: str = 'train',
        train_ratio: float = 0.9
    ):
        self.hdf5_paths = hdf5_paths
        self.oversample_factors = oversample_factors
        self.char_vocab = char_vocab
        self.lang_vocab = lang_vocab
        self.split = split

        # File handle cache (populated lazily per worker)
        self._file_handles: Dict[int, h5py.File] = {}

        # Build combined index: list of (source_idx, item_idx_in_source)
        self.combined_indices = []
        self.source_item_counts = []

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                items = f['items']
                item_indices = []

                for idx in range(f.attrs['total_items']):
                    if items['has_phonetic'][idx]:
                        item_indices.append(idx)

                random.shuffle(item_indices)
                split_idx = int(len(item_indices) * train_ratio)

                if split == 'train':
                    source_indices = item_indices[:split_idx]
                else:
                    source_indices = item_indices[split_idx:]

                self.source_item_counts.append(len(source_indices))

                # Apply oversampling
                for _ in range(factor):
                    for item_idx in source_indices:
                        self.combined_indices.append((source_idx, item_idx))

        random.shuffle(self.combined_indices)

        print(f"MultiSourcePhase2Dataset ({split}):")
        for i, (path, count, factor) in enumerate(zip(hdf5_paths, self.source_item_counts, oversample_factors)):
            effective = count * factor
            print(f"  Source {i}: {count:,} items × {factor}x = {effective:,} effective")
        print(f"  Total: {len(self.combined_indices):,} samples per epoch", flush=True)

    def _get_file(self, source_idx: int) -> h5py.File:
        """Get cached file handle for source, opening if needed."""
        if source_idx not in self._file_handles:
            self._file_handles[source_idx] = h5py.File(
                self.hdf5_paths[source_idx], 'r', swmr=True
            )
        return self._file_handles[source_idx]

    def __len__(self) -> int:
        return len(self.combined_indices)

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        source_idx, item_idx = self.combined_indices[idx]
        f = self._get_file(source_idx)

        items = f['items']

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

    def __del__(self):
        """Close any open file handles."""
        for f in self._file_handles.values():
            try:
                f.close()
            except:
                pass


class MultiSourcePhase3Dataset(Dataset):
    """
    Phase 3 Dataset combining multiple HDF5 sources with oversampling
    and curriculum hard negative support.

    Each source maintains its own cluster structure for negative sampling,
    ensuring negatives come from the same source as the anchor/positive.

    File handles are cached per worker process for performance.
    """

    def __init__(
        self,
        hdf5_paths: List[str],
        oversample_factors: List[int],
        char_vocab,
        lang_vocab,
        split: str = 'train',
        train_ratio: float = 0.9,
        subsample_pairs: int = Config.SUBSAMPLE_PAIRS,
        negative_stage: str = 'A'
    ):
        self.hdf5_paths = hdf5_paths
        self.oversample_factors = oversample_factors
        self.char_vocab = char_vocab
        self.lang_vocab = lang_vocab
        self.split = split
        self.negative_stage = negative_stage

        # File handle cache (populated lazily per worker)
        self._file_handles: Dict[int, h5py.File] = {}

        # Build combined index: list of (source_idx, pair_idx_in_source, is_phonetic)
        self.combined_indices = []
        self.source_pair_counts = []

        # Per-source metadata for negative sampling
        self.source_cluster_maps = []
        self.source_first_char_indices = []
        self.source_item_romanized = []
        self.source_item_ipa = []
        self.source_phon_counts = []
        self.source_all_items = []  # All item indices per source for fallback

        for source_idx, (path, factor) in enumerate(zip(hdf5_paths, oversample_factors)):
            with h5py.File(path, 'r') as f:
                phon_count = f.attrs['pairs_with_phonetic']
                no_phon_count = f.attrs.get('pairs_without_phonetic', 0)
                total_pairs = phon_count + no_phon_count

                self.source_phon_counts.append(phon_count)

                max_pairs = min(total_pairs, subsample_pairs)
                all_indices = list(range(total_pairs))
                random.shuffle(all_indices)
                sampled_indices = all_indices[:max_pairs]

                split_idx = int(len(sampled_indices) * train_ratio)
                if split == 'train':
                    source_indices = sampled_indices[:split_idx]
                else:
                    source_indices = sampled_indices[split_idx:]

                self.source_pair_counts.append(len(source_indices))

                # Apply oversampling
                for _ in range(factor):
                    for pair_idx in source_indices:
                        self.combined_indices.append((source_idx, pair_idx))

                # Build per-source metadata for negative sampling
                items = f['items']
                total_items = f.attrs['total_items']

                cluster_to_items = defaultdict(list)
                first_char_index = defaultdict(list)
                item_romanized = {}
                item_ipa = {}
                all_item_indices = []

                for idx in range(total_items):
                    cluster_id = int(items['cluster_id'][idx])
                    cluster_to_items[cluster_id].append(idx)
                    all_item_indices.append(idx)

                    romanized = items['romanized'][idx]
                    if isinstance(romanized, bytes):
                        romanized = romanized.decode('utf-8')
                    item_romanized[idx] = romanized

                    if romanized:
                        first_char = romanized[0].lower()
                        first_char_index[first_char].append(idx)

                    ipa = items['ipa'][idx]
                    if isinstance(ipa, bytes):
                        ipa = ipa.decode('utf-8')
                    item_ipa[idx] = ipa

                self.source_cluster_maps.append(dict(cluster_to_items))
                self.source_first_char_indices.append(dict(first_char_index))
                self.source_item_romanized.append(item_romanized)
                self.source_item_ipa.append(item_ipa)
                self.source_all_items.append(all_item_indices)

        random.shuffle(self.combined_indices)

        print(f"MultiSourcePhase3Dataset ({split}):")
        for i, (path, count, factor) in enumerate(zip(hdf5_paths, self.source_pair_counts, oversample_factors)):
            effective = count * factor
            print(f"  Source {i}: {count:,} pairs × {factor}x = {effective:,} effective")
        print(f"  Total: {len(self.combined_indices):,} samples per epoch", flush=True)
        print(f"  Negative stage: {negative_stage}", flush=True)

    def _get_file(self, source_idx: int) -> h5py.File:
        """Get cached file handle for source, opening if needed."""
        if source_idx not in self._file_handles:
            self._file_handles[source_idx] = h5py.File(
                self.hdf5_paths[source_idx], 'r', swmr=True
            )
        return self._file_handles[source_idx]

    def __len__(self) -> int:
        return len(self.combined_indices)

    def _random_negative(self, source_idx: int, anchor_cluster: int, exclude_indices: set = None) -> int:
        """Select random negative from different cluster within same source."""
        if exclude_indices is None:
            exclude_indices = set()

        cluster_map = self.source_cluster_maps[source_idx]
        cluster_ids = list(cluster_map.keys())
        other_clusters = [c for c in cluster_ids if c != anchor_cluster]

        if other_clusters:
            # Normal case: sample from a different cluster
            neg_cluster = random.choice(other_clusters)
            return random.choice(cluster_map[neg_cluster])
        else:
            # Edge case: only one cluster in this source
            # Sample any item from this source not in exclude set
            all_items = self.source_all_items[source_idx]
            available = [i for i in all_items if i not in exclude_indices]
            if available:
                return random.choice(available)
            # Fallback: return any item (will have zero loss)
            return random.choice(all_items)

    def _get_stage_a_negative(self, source_idx: int, anchor_idx: int, anchor_cluster: int) -> int:
        """Stage A: Find orthographically close but phonetically distant negative."""
        anchor_rom = self.source_item_romanized[source_idx].get(anchor_idx, '')
        anchor_ipa = self.source_item_ipa[source_idx].get(anchor_idx, '')

        if not anchor_rom:
            return self._random_negative(source_idx, anchor_cluster, {anchor_idx})

        first_char = anchor_rom[0].lower()
        first_char_index = self.source_first_char_indices[source_idx]
        candidates = list(first_char_index.get(first_char, []))

        if len(candidates) < 10:
            for offset in [-1, 1, -2, 2]:
                adj_char = chr(ord(first_char) + offset)
                candidates.extend(first_char_index.get(adj_char, []))

        if len(candidates) < 5:
            return self._random_negative(source_idx, anchor_cluster, {anchor_idx})

        random.shuffle(candidates)
        best_neg_idx = None
        best_score = -float('inf')

        # Use cached cluster info instead of reading from file
        cluster_map = self.source_cluster_maps[source_idx]

        for neg_idx in candidates[:50]:
            # Get cluster from cached data
            neg_cluster = None
            for cid, items in cluster_map.items():
                if neg_idx in items:
                    neg_cluster = cid
                    break

            if neg_cluster is None or neg_cluster == anchor_cluster:
                continue

            neg_rom = self.source_item_romanized[source_idx].get(neg_idx, '')
            if not neg_rom:
                continue

            edit_dist = _levenshtein_distance(anchor_rom.lower(), neg_rom.lower())

            if edit_dist > Config.STAGE_A_EDIT_DISTANCE_MAX:
                continue

            neg_ipa = self.source_item_ipa[source_idx].get(neg_idx, '')

            if anchor_ipa and neg_ipa:
                ipa_edit = _levenshtein_distance(anchor_ipa, neg_ipa)
                max_ipa_len = max(len(anchor_ipa), len(neg_ipa), 1)
                phon_dist = ipa_edit / max_ipa_len

                if phon_dist < Config.STAGE_A_PHONETIC_DISTANCE_MIN:
                    continue

                score = phon_dist - (edit_dist * 0.1)
            else:
                score = -edit_dist

            if score > best_score:
                best_score = score
                best_neg_idx = neg_idx

        if best_neg_idx is not None:
            return best_neg_idx

        return self._random_negative(source_idx, anchor_cluster, {anchor_idx})

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        source_idx, pair_idx = self.combined_indices[idx]
        phon_count = self.source_phon_counts[source_idx]
        f = self._get_file(source_idx)

        items = f['items']

        if pair_idx < phon_count:
            pairs = f['pairs_with_phonetic']
            local_idx = pair_idx
        else:
            pairs = f['pairs_without_phonetic']
            local_idx = pair_idx - phon_count

        anchor_idx = int(pairs['anchor_idx'][local_idx])
        positive_idx = int(pairs['positive_idx'][local_idx])
        anchor_cluster = int(items['cluster_id'][anchor_idx])

        if self.negative_stage == 'A':
            negative_idx = self._get_stage_a_negative(source_idx, anchor_idx, anchor_cluster)
        else:
            negative_idx = self._random_negative(source_idx, anchor_cluster, {anchor_idx, positive_idx})

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

    def __del__(self):
        """Close any open file handles."""
        for f in self._file_handles.values():
            try:
                f.close()
            except:
                pass