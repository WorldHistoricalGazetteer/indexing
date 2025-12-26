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


class StreamingPhase1Dataset(Dataset):
    """
    Phase 1 Dataset with HDF5 streaming.
    
    Provides triplets of (anchor, positive, negative) phonetic feature sequences
    for training the Teacher (phonetic encoder).
    """

    def __init__(
        self,
        hdf5_path: str,
        split: str = 'train',
        train_ratio: float = 0.9,
        subsample_pairs: int = Config.SUBSAMPLE_PAIRS
    ):
        self.hdf5_path = hdf5_path
        self.split = split

        with h5py.File(hdf5_path, 'r') as f:
            total_pairs = f.attrs['pairs_with_phonetic']

            max_pairs = min(total_pairs, subsample_pairs)
            all_indices = list(range(total_pairs))
            random.shuffle(all_indices)
            sampled_indices = all_indices[:max_pairs]

            split_idx = int(max_pairs * train_ratio)
            if split == 'train':
                self.pair_indices = sampled_indices[:split_idx]
            else:
                self.pair_indices = sampled_indices[split_idx:]

            items = f['items']
            self.phonetic_item_indices = []

            for idx in range(f.attrs['total_items']):
                if items['has_phonetic'][idx]:
                    feature_key = str(idx)
                    if feature_key in f['features']:
                        feat_shape = f['features'][feature_key].shape
                        if feat_shape[0] > 0:
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
    """
    Phase 2 Dataset with HDF5 streaming.
    
    Provides (character sequence, phonetic features) pairs for
    Student-Teacher alignment training.
    """

    def __init__(
        self,
        hdf5_path: str,
        char_vocab,
        lang_vocab,
        split: str = 'train',
        train_ratio: float = 0.9
    ):
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
    """
    Phase 3 Dataset with HDF5 streaming and Curriculum Hard Negatives.
    
    v2 Changes - Curriculum Hard Negative Mining:
    
    Stage A (Default): Orthographically close, phonetically distant
        - anyascii edit distance is small
        - PanPhon cosine distance is large
        - Targets false friends and spelling-driven false positives
    
    Stage B (Optional): Model-mined false positives
        - Pairs with similarity > threshold but known non-identical
        - Set via set_mined_negatives() after Phase 3 pass
        - Sharpens decision boundary using model's own failure modes
    
    CRITICAL: Do not mix negative types in the same training pass.
    Each stage must REPLACE, not augment, the previous one.
    """

    def __init__(
        self,
        hdf5_path: str,
        char_vocab,
        lang_vocab,
        split: str = 'train',
        train_ratio: float = 0.9,
        subsample_pairs: int = Config.SUBSAMPLE_PAIRS,
        negative_stage: str = 'A'  # 'A' for ortho-phon, 'B' for model-mined
    ):
        self.hdf5_path = hdf5_path
        self.char_vocab = char_vocab
        self.lang_vocab = lang_vocab
        self.split = split
        self.negative_stage = negative_stage
        
        # Stage B: Model-mined hard negatives (set externally)
        self.mined_negatives: Optional[Dict[int, List[int]]] = None

        with h5py.File(hdf5_path, 'r') as f:
            phon_count = f.attrs['pairs_with_phonetic']
            no_phon_count = f.attrs['pairs_without_phonetic']
            total_pairs = phon_count + no_phon_count

            max_pairs = min(total_pairs, subsample_pairs)
            all_indices = list(range(total_pairs))
            random.shuffle(all_indices)
            sampled_indices = all_indices[:max_pairs]

            split_idx = int(len(sampled_indices) * train_ratio)
            if split == 'train':
                self.pair_indices = sampled_indices[:split_idx]
            else:
                self.pair_indices = sampled_indices[split_idx:]

            self.phon_count = phon_count

            items = f['items']
            total_items = f.attrs['total_items']

            # Build cluster index
            self.cluster_to_items = defaultdict(list)
            for idx in range(total_items):
                cluster_id = int(items['cluster_id'][idx])
                self.cluster_to_items[cluster_id].append(idx)
            self.cluster_ids = list(self.cluster_to_items.keys())

            # Build first-char index for Stage A hard negatives
            self.index_by_first_char = defaultdict(list)
            self.item_romanized = {}
            self.item_ipa = {}
            
            for idx in range(total_items):
                romanized = items['romanized'][idx]
                if isinstance(romanized, bytes):
                    romanized = romanized.decode('utf-8')
                
                self.item_romanized[idx] = romanized
                
                if romanized:
                    first_char = romanized[0].lower()
                    self.index_by_first_char[first_char].append(idx)
                
                # Store IPA for phonetic distance computation
                ipa = items['ipa'][idx]
                if isinstance(ipa, bytes):
                    ipa = ipa.decode('utf-8')
                self.item_ipa[idx] = ipa

        print(f"StreamingPhase3Dataset ({split}): {len(self.pair_indices):,} pairs")
        print(f"  Negative stage: {negative_stage}")

    def set_mined_negatives(self, mined_negatives: Dict[int, List[int]]):
        """
        Set model-mined hard negatives for Stage B.
        
        Args:
            mined_negatives: Dict mapping anchor_idx to list of hard negative indices
        """
        self.mined_negatives = mined_negatives
        self.negative_stage = 'B'
        print(f"  Stage B activated: {len(mined_negatives)} anchors with mined negatives")

    def __len__(self) -> int:
        return len(self.pair_indices)

    def _get_stage_a_negative(self, f: h5py.File, anchor_idx: int) -> int:
        """
        Stage A: Find orthographically close but phonetically distant negative.
        
        Targets false friends and spelling-driven false positives.
        """
        items = f['items']
        anchor_rom = self.item_romanized.get(anchor_idx, '')
        anchor_cluster = int(items['cluster_id'][anchor_idx])
        anchor_ipa = self.item_ipa.get(anchor_idx, '')
        
        if not anchor_rom:
            return self._random_negative(anchor_cluster)
        
        first_char = anchor_rom[0].lower()
        candidates = self.index_by_first_char.get(first_char, [])
        
        if len(candidates) < 10:
            # Fallback: use adjacent first chars
            for offset in [-1, 1, -2, 2]:
                adj_char = chr(ord(first_char) + offset)
                candidates.extend(self.index_by_first_char.get(adj_char, []))
        
        if len(candidates) < 5:
            return self._random_negative(anchor_cluster)
        
        # Sample candidates and find best hard negative
        random.shuffle(candidates)
        best_neg_idx = None
        best_score = -float('inf')
        
        for neg_idx in candidates[:50]:  # Check up to 50 candidates
            neg_cluster = int(items['cluster_id'][neg_idx])
            
            # Must be from different cluster (different place)
            if neg_cluster == anchor_cluster:
                continue
            
            neg_rom = self.item_romanized.get(neg_idx, '')
            if not neg_rom:
                continue
            
            # Compute orthographic distance (edit distance)
            edit_dist = _levenshtein_distance(anchor_rom.lower(), neg_rom.lower())
            
            # We want SMALL edit distance (orthographically similar)
            if edit_dist > Config.STAGE_A_EDIT_DISTANCE_MAX:
                continue
            
            # Compute phonetic distance if IPA available
            neg_ipa = self.item_ipa.get(neg_idx, '')
            
            if anchor_ipa and neg_ipa:
                # Simple phonetic distance: normalized edit distance on IPA
                ipa_edit = _levenshtein_distance(anchor_ipa, neg_ipa)
                max_ipa_len = max(len(anchor_ipa), len(neg_ipa), 1)
                phon_dist = ipa_edit / max_ipa_len
                
                # We want LARGE phonetic distance
                if phon_dist < Config.STAGE_A_PHONETIC_DISTANCE_MIN:
                    continue
                
                # Score: prioritize high phonetic distance with low edit distance
                score = phon_dist - (edit_dist * 0.1)
            else:
                # No IPA: use edit distance inverse as proxy
                score = -edit_dist
            
            if score > best_score:
                best_score = score
                best_neg_idx = neg_idx
        
        if best_neg_idx is not None:
            return best_neg_idx
        
        # Fallback to length-similar negative
        return self._get_length_similar_negative(items, anchor_idx, anchor_cluster, anchor_rom)

    def _get_stage_b_negative(self, anchor_idx: int, anchor_cluster: int) -> int:
        """
        Stage B: Return model-mined false positive negative.
        """
        if self.mined_negatives and anchor_idx in self.mined_negatives:
            candidates = self.mined_negatives[anchor_idx]
            if candidates:
                return random.choice(candidates)
        
        # Fallback to random negative
        return self._random_negative(anchor_cluster)

    def _get_length_similar_negative(
        self,
        items,
        anchor_idx: int,
        anchor_cluster: int,
        anchor_rom: str
    ) -> int:
        """Fallback: find length-similar negative from different cluster."""
        anchor_len = len(anchor_rom)
        first_char = anchor_rom[0].lower() if anchor_rom else ''
        
        candidates = self.index_by_first_char.get(first_char, [])
        random.shuffle(candidates)
        
        for neg_idx in candidates[:20]:
            neg_cluster = int(items['cluster_id'][neg_idx])
            if neg_cluster == anchor_cluster:
                continue
            
            neg_rom = self.item_romanized.get(neg_idx, '')
            neg_len = len(neg_rom)
            
            if abs(neg_len - anchor_len) <= 2:
                return neg_idx
        
        return self._random_negative(anchor_cluster)

    def _random_negative(self, anchor_cluster: int) -> int:
        """Select random negative from different cluster."""
        neg_cluster = random.choice([c for c in self.cluster_ids if c != anchor_cluster])
        return random.choice(self.cluster_to_items[neg_cluster])

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        pair_idx = self.pair_indices[idx]

        with h5py.File(self.hdf5_path, 'r') as f:
            items = f['items']

            if pair_idx < self.phon_count:
                pairs = f['pairs_with_phonetic']
                local_idx = pair_idx
            else:
                pairs = f['pairs_without_phonetic']
                local_idx = pair_idx - self.phon_count

            anchor_idx = int(pairs['anchor_idx'][local_idx])
            positive_idx = int(pairs['positive_idx'][local_idx])
            anchor_cluster = int(items['cluster_id'][anchor_idx])
            
            # Select negative based on curriculum stage
            if self.negative_stage == 'A':
                negative_idx = self._get_stage_a_negative(f, anchor_idx)
            elif self.negative_stage == 'B':
                negative_idx = self._get_stage_b_negative(anchor_idx, anchor_cluster)
            else:
                negative_idx = self._random_negative(anchor_cluster)

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
