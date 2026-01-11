# training/data_loading.py
"""
Data loading utilities for phonetic embedding training.

This module provides:
- Parquet-based dataset classes for each training phase
- Noise augmentation for typo tolerance
- Collate functions with dynamic batching

Training phases:
- Phase 1: Teacher training on phonetic features (triplet loss)
- Phase 2: Student alignment to Teacher (distillation loss + noise)
- Phase 3: Student fine-tuning with hard negatives (contrastive loss + noise)

Data is extracted using the two-pass strategy from extract_to_parquet.py:
- Pass 1 builds vocabulary from entire corpus (~67M toponyms)
- Pass 2 extracts training data from gn, wd, tgn namespaces

Vocabulary limits are read dynamically from the vocab files rather than hardcoded.
"""

import random
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable

import pandas as pd
import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
import logging

logger = logging.getLogger(__name__)

import pyarrow.parquet as pq
import pyarrow.dataset as ds

def load_vocab_limits(data_dir: Path) -> Dict[str, int]:
    """
    Load vocabulary limits from the extracted data directory.

    This reads the actual vocabulary sizes from the vocab files created
    during extraction, rather than using hardcoded values.

    Args:
        data_dir: Path to extracted data directory

    Returns:
        Dict with 'char', 'script', 'lang' limits
    """
    import json
    vocab_dir = Path(data_dir) / 'vocab'

    limits = {'char': 5000, 'script': 25, 'lang': 1000}  # Safe defaults

    try:
        # Load char vocab
        char_path = vocab_dir / 'char_vocab.json'
        if char_path.exists():
            with open(char_path) as f:
                char_data = json.load(f)
                limits['char'] = len(char_data.get('char_to_id', {}))

        # Load script vocab
        script_path = vocab_dir / 'script_vocab.json'
        if script_path.exists():
            with open(script_path) as f:
                script_data = json.load(f)
                limits['script'] = len(script_data.get('script_to_id', {}))

        # Load lang vocab
        lang_path = vocab_dir / 'lang_vocab.json'
        if lang_path.exists():
            with open(lang_path) as f:
                lang_data = json.load(f)
                limits['lang'] = len(lang_data.get('lang_to_id', {}))

        logger.info(f"Loaded vocab limits: char={limits['char']}, script={limits['script']}, lang={limits['lang']}")

    except Exception as e:
        logger.warning(f"Could not load vocab limits from {vocab_dir}: {e}. Using defaults.")

    return limits


def get_training_data_path(data_dir: Path) -> Path:
    """
    Get the path to training data, checking both new ('training') and old ('toponyms') locations.
    """
    training_path = Path(data_dir) / 'training'
    if training_path.exists():
        return training_path
    # Fallback to old path for backward compatibility
    toponyms_path = Path(data_dir) / 'toponyms'
    if toponyms_path.exists():
        return toponyms_path
    raise FileNotFoundError(f"Training data not found in {data_dir}/training or {data_dir}/toponyms")


# ============================================================================
# Noise Augmentation
# ============================================================================

# Common OCR/keyboard errors for phonetically plausible noise
KEYBOARD_ADJACENT = {
    'a': 'qwsz', 'b': 'vghn', 'c': 'xdfv', 'd': 'erfcxs', 'e': 'rdsw',
    'f': 'rtgvcd', 'g': 'tyhbvf', 'h': 'yujnbg', 'i': 'uojk', 'j': 'uiknmh',
    'k': 'ioljm', 'l': 'opk', 'm': 'njk', 'n': 'bhjm', 'o': 'iplk',
    'p': 'ol', 'q': 'wa', 'r': 'etdf', 's': 'weadzx', 't': 'ryfg',
    'u': 'yihj', 'v': 'cfgb', 'w': 'qeas', 'x': 'zsdc', 'y': 'tugh',
    'z': 'asx',
}

# OCR confusion pairs
OCR_CONFUSIONS = {
    'o': '0', '0': 'o', 'l': '1', '1': 'l', 'i': '1',
    'e': 'c', 'c': 'e', 'n': 'm', 'm': 'n', 'rn': 'm',
    'cl': 'd', 'vv': 'w', 'ii': 'u',
}


def apply_character_noise(
        text: str,
        noise_prob: float = 0.3,
        max_edits: int = 2,
        use_keyboard: bool = True,
) -> str:
    """
    Apply realistic character-level noise to text.

    Noise types:
    - Deletion: Remove a character
    - Insertion: Duplicate a character or insert space
    - Substitution: Replace with adjacent key or OCR confusion
    - Transposition: Swap adjacent characters

    Args:
        text: Input text
        noise_prob: Probability of applying any noise
        max_edits: Maximum number of edits to apply
        use_keyboard: Use keyboard adjacency for substitutions

    Returns:
        Noisy text
    """
    if random.random() > noise_prob:
        return text

    if len(text) < 2:
        return text

    chars = list(text)

    # Number of edits: 1 for short words, up to max_edits for longer
    num_edits = 1 if len(text) < 6 else random.randint(1, min(max_edits, len(text) // 3))

    for _ in range(num_edits):
        if len(chars) < 2:
            break

        edit_type = random.choice(['delete', 'insert', 'substitute', 'transpose'])
        pos = random.randint(0, len(chars) - 1)

        if edit_type == 'delete' and len(chars) > 2:
            chars.pop(pos)

        elif edit_type == 'insert':
            # Duplicate the character at position
            chars.insert(pos, chars[pos])

        elif edit_type == 'substitute':
            char = chars[pos].lower()
            if use_keyboard and char in KEYBOARD_ADJACENT:
                # Use keyboard-adjacent character
                adjacent = KEYBOARD_ADJACENT[char]
                new_char = random.choice(adjacent)
                # Preserve case
                if chars[pos].isupper():
                    new_char = new_char.upper()
                chars[pos] = new_char
            elif char in OCR_CONFUSIONS:
                # Use OCR confusion
                new_char = OCR_CONFUSIONS[char]
                if chars[pos].isupper():
                    new_char = new_char.upper()
                chars[pos] = new_char

        elif edit_type == 'transpose' and pos < len(chars) - 1:
            chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]

    return ''.join(chars)


# ============================================================================
# Dataset Classes
# ============================================================================

class Phase1DatasetEnriched(Dataset):
    """
    FAST Dataset for Phase 1: Uses pre-enriched triplets with embedded features.

    This avoids the expensive join at load time by reading triplet files that
    already contain anchor/positive/negative features. Loading is instant (~10s)
    instead of ~30 minutes.

    Use this if triplets were generated with --enrich-triplets flag.
    Falls back to Phase1Dataset if enriched triplets not found.
    """

    def __init__(
            self,
            data_dir: Path,
            split: str = 'train',
    ):
        self.data_dir = Path(data_dir)
        self.split = split

        # Check for enriched triplets
        enriched_path = self.data_dir / 'triplets' / 'phase1_enriched'
        if not enriched_path.exists():
            print(f"Phase1DatasetEnriched: Enriched triplets not found, falling back to Phase1Dataset", flush=True)
            # Delegate to standard loader
            self._delegate = Phase1Dataset(data_dir, split)
            self._use_delegate = True
            return

        self._use_delegate = False
        print(f"Phase1DatasetEnriched: Loading enriched triplets for split '{split}'...", flush=True)

        # Load enriched triplets directly - they already contain features!
        dataset = ds.dataset(enriched_path, format='parquet')

        # Filter by split and ensure features exist
        df = dataset.to_table().to_pandas()

        # Filter to split
        mask = df['split'] == split
        self.triplets_df = df[mask].reset_index(drop=True)

        print(f"Phase1DatasetEnriched: {len(self.triplets_df):,} triplets for {split}", flush=True)

    def __len__(self) -> int:
        if self._use_delegate:
            return len(self._delegate)
        return len(self.triplets_df)

    def __getitem__(self, idx: int) -> Dict:
        if self._use_delegate:
            return self._delegate[idx]

        row = self.triplets_df.iloc[idx]

        # Features are already embedded in the triplet
        anchor_features = row['anchor_features']
        positive_features = row['positive_features']
        negative_features = row['negative_features']

        # Convert numpy arrays to lists if needed
        if hasattr(anchor_features, 'tolist'):
            anchor_features = anchor_features.tolist()
        if hasattr(positive_features, 'tolist'):
            positive_features = positive_features.tolist()
        if hasattr(negative_features, 'tolist'):
            negative_features = negative_features.tolist()

        return {
            'anchor': {
                'features': anchor_features,
                'feature_length': row['anchor_feature_length'],
            },
            'positive': {
                'features': positive_features,
                'feature_length': row['positive_feature_length'],
            },
            'negative': {
                'features': negative_features,
                'feature_length': row['negative_feature_length'],
            },
        }


class Phase1Dataset(Dataset):
    """
    Dataset for Phase 1: Teacher training on phonetic features.

    MEMORY-OPTIMIZED: Only loads features for toponyms that appear in triplets,
    not the entire 57M+ toponym corpus. This reduces memory from ~100GB to ~10GB.

    Uses SQLite for random-access feature lookup instead of loading all into RAM.
    """

    def __init__(
            self,
            data_dir: Path,
            split: str = 'train',
    ):
        self.data_dir = Path(data_dir)
        self.split = split
        print(f"Phase1Dataset: Loading data for split '{split}'...", flush=True)

        # 1. Load Triplets -> Pandas (small: ~500MB for 5M triplets)
        triplets_path = self.data_dir / 'triplets' / 'phase1'
        self.triplets_df = ds.dataset(triplets_path, format='parquet').to_table().to_pandas()
        print(f"Phase1Dataset: Loaded {len(self.triplets_df):,} triplets", flush=True)

        # 2. Get unique toponym IDs from triplets (only ~10-15M unique, not 57M)
        needed_ids = set(self.triplets_df['anchor_id'].unique()) | \
                     set(self.triplets_df['positive_id'].unique()) | \
                     set(self.triplets_df['negative_id'].unique())
        print(f"Phase1Dataset: {len(needed_ids):,} unique toponym IDs needed", flush=True)

        # 3. Load ONLY the needed toponyms from Parquet
        toponyms_path = get_training_data_path(self.data_dir)
        dataset = ds.dataset(toponyms_path, format='parquet', partitioning='hive')

        # Use PyArrow filter to only load needed rows
        import pyarrow.compute as pc
        needed_ids_list = list(needed_ids)

        # Load in chunks to avoid memory spike
        print(f"Phase1Dataset: Loading features for needed toponyms...", flush=True)
        self._feature_cache = {}
        valid_anchor_ids = set()

        # Process in batches using scanner
        scanner = dataset.scanner(
            columns=['toponym_id', 'features', 'feature_length', 'split'],
            batch_size=100000
        )

        loaded_count = 0
        for batch in scanner.to_batches():
            batch_df = batch.to_pandas()

            # Filter to only needed IDs
            mask = batch_df['toponym_id'].isin(needed_ids)
            filtered = batch_df[mask]

            if len(filtered) == 0:
                continue

            # Track valid anchor IDs (must be in correct split)
            split_mask = filtered['split'] == split
            valid_anchor_ids.update(filtered[split_mask]['toponym_id'].tolist())

            # Filter for valid features
            valid_mask = filtered['features'].notna() & (filtered['feature_length'] > 0)
            valid_rows = filtered[valid_mask]

            # Add to cache
            for _, row in valid_rows.iterrows():
                tid = row['toponym_id']
                features = row['features']
                if isinstance(features, np.ndarray):
                    features = features.tolist()
                self._feature_cache[tid] = {
                    'features': features,
                    'feature_length': row['feature_length']
                }

            loaded_count += len(valid_rows)
            if loaded_count % 500000 == 0:
                print(f"  Loaded {loaded_count:,} toponyms with features...", flush=True)

        print(f"Phase1Dataset: Cached {len(self._feature_cache):,} toponyms with valid features", flush=True)

        # 4. Filter triplets - anchor must be in split, all must have features
        available_ids = set(self._feature_cache.keys())

        print(f"Phase1Dataset: Filtering triplets...", flush=True)
        df = self.triplets_df

        # Vectorized filtering
        mask_anchor_split = df['anchor_id'].isin(valid_anchor_ids)
        mask_features = (
            df['anchor_id'].isin(available_ids) &
            df['positive_id'].isin(available_ids) &
            df['negative_id'].isin(available_ids)
        )

        self.valid_df = df[mask_anchor_split & mask_features].reset_index(drop=True)

        # Free memory
        del self.triplets_df

        print(f"Phase1Dataset: {len(self.valid_df):,} valid triplets for {split}", flush=True)

    def __len__(self) -> int:
        return len(self.valid_df)

    def __getitem__(self, idx: int) -> Dict:
        row = self.valid_df.iloc[idx]

        return {
            'anchor': self._feature_cache[row['anchor_id']],
            'positive': self._feature_cache[row['positive_id']],
            'negative': self._feature_cache[row['negative_id']],
        }


class Phase2Dataset(Dataset):
    """
    Dataset for Phase 2: Student-Teacher alignment.

    MEMORY-OPTIMIZED: Uses PyArrow predicate pushdown to filter at the
    Parquet level before loading into memory. This avoids loading 57M rows
    just to filter down to ~23M.

    The filtered Arrow table stays in Arrow format (columnar, compressed)
    which is much more memory-efficient than Python dicts.
    """

    def __init__(
            self,
            data_dir: Path,
            split: str = 'train',
            require_features: bool = True,
            vocab_limits: Optional[Dict[str, int]] = None,
    ):
        self.data_dir = Path(data_dir)
        self.split = split
        self.require_features = require_features
        print(f"Phase2Dataset: Loading {split} data (Predicate Pushdown)...", flush=True)

        # Load vocab limits dynamically if not provided
        if vocab_limits is None:
            vocab_limits = load_vocab_limits(self.data_dir)
        self.vocab_limits = vocab_limits

        # Load dataset with filter pushdown
        toponyms_path = get_training_data_path(self.data_dir)
        dataset = ds.dataset(toponyms_path, format='parquet', partitioning='hive')

        import pyarrow.compute as pc

        # Build filter expression (pushed down to Parquet reader)
        filter_expr = pc.field('split') == split

        if require_features:
            # Filter for valid features - these filters are pushed to Parquet scan
            filter_expr = filter_expr & pc.is_valid(pc.field('features'))
            filter_expr = filter_expr & (pc.field('feature_length') > 0)
            filter_expr = filter_expr & (pc.field('epitran_supported') == True)

        # Note: 'script' is a string field (e.g., "LATIN"), not an integer ID
        # No need for bounds checking - all script names are valid

        # Scan with filter pushdown - only matching rows are read from disk
        print(f"Phase2Dataset: Scanning with predicate pushdown...", flush=True)

        self._table = dataset.to_table(
            columns=[
                'toponym_id', 'name', 'script', 'lang',
                'char_ids',
                'features', 'feature_length'
            ],
            filter=filter_expr
        )

        self._count = len(self._table)
        print(f"Phase2Dataset: {self._count:,} valid samples for {split}", flush=True)

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, idx: int) -> Dict:
        """Get item from PyArrow table."""
        row = {
            'toponym_id': self._table['toponym_id'][idx].as_py(),
            'name': self._table['name'][idx].as_py(),
            'script': self._table['script'][idx].as_py(),
            'lang': self._table['lang'][idx].as_py() or '',
            'char_ids': self._table['char_ids'][idx].as_py(),
            'features': self._table['features'][idx].as_py(),
            'feature_length': self._table['feature_length'][idx].as_py(),
        }

        # Convert numpy arrays to lists if needed
        if hasattr(row['features'], 'tolist'):
            row['features'] = row['features'].tolist()
        if hasattr(row['char_ids'], 'tolist'):
            row['char_ids'] = row['char_ids'].tolist()

        return row


class Phase3Dataset(Dataset):
    """
    Dataset for Phase 3: Contrastive fine-tuning with hard negatives.

    MEMORY-OPTIMIZED: Only loads toponyms that appear in triplets,
    not the entire 57M+ toponym corpus.
    """

    def __init__(
            self,
            data_dir: Path,
            split: str = 'train',
            vocab_limits: Optional[Dict[str, int]] = None,
    ):
        self.data_dir = Path(data_dir)
        self.split = split
        print(f"Phase3Dataset: Loading {split} data...")

        # Load vocab limits dynamically if not provided
        if vocab_limits is None:
            vocab_limits = load_vocab_limits(self.data_dir)

        # 1. Load Triplets -> Pandas (small: ~500MB for 5M triplets)
        triplets_path = self.data_dir / 'triplets' / 'phase3'
        self.triplets_df = ds.dataset(triplets_path, format='parquet').to_table().to_pandas()
        print(f"Phase3Dataset: Loaded {len(self.triplets_df):,} triplets")

        # 2. Get unique toponym IDs from triplets
        needed_ids = set(self.triplets_df['anchor_id'].unique()) | \
                     set(self.triplets_df['positive_id'].unique()) | \
                     set(self.triplets_df['negative_id'].unique())
        print(f"Phase3Dataset: {len(needed_ids):,} unique toponym IDs needed")

        # 3. Load ONLY the needed toponyms from Parquet
        toponyms_path = get_training_data_path(self.data_dir)
        dataset = ds.dataset(toponyms_path, format='parquet', partitioning='hive')

        columns = [
            'toponym_id', 'name', 'script', 'lang',
            'char_ids', 'split'
        ]

        print(f"Phase3Dataset: Loading toponyms for needed IDs...")
        self._cache = {}

        # Process in batches using scanner
        scanner = dataset.scanner(columns=columns, batch_size=100000)

        loaded_count = 0
        for batch in scanner.to_batches():
            batch_df = batch.to_pandas()

            # Filter to only needed IDs
            mask = batch_df['toponym_id'].isin(needed_ids)
            filtered = batch_df[mask]

            if len(filtered) == 0:
                continue

            # Sanitize script IDs
            if pd.api.types.is_integer_dtype(filtered['script']):
                script_mask = (filtered['script'] >= 0) & (filtered['script'] < vocab_limits['script'])
                filtered = filtered[script_mask]

            # Sanitize char IDs
            if not filtered.empty:
                limit = vocab_limits['char']

                def is_safe_chars(ids):
                    if ids is None: return True
                    if isinstance(ids, np.ndarray):
                        if ids.size == 0: return True
                        return ids.min() >= 0 and ids.max() < limit
                    if not ids: return True
                    return min(ids) >= 0 and max(ids) < limit

                char_mask = filtered['char_ids'].apply(is_safe_chars)
                filtered = filtered[char_mask]

            # Add to cache
            for _, row in filtered.iterrows():
                tid = row['toponym_id']
                char_ids = row['char_ids']
                if isinstance(char_ids, np.ndarray):
                    char_ids = char_ids.tolist()
                lang = row['lang'] if pd.notna(row['lang']) else ''

                self._cache[tid] = {
                    'name': row['name'],
                    'script': row['script'],
                    'lang': lang,
                    'char_ids': char_ids,
                    'split': row['split']
                }

            loaded_count += len(filtered)
            if loaded_count % 500000 == 0:
                print(f"  Loaded {loaded_count:,} toponyms...")

        print(f"Phase3Dataset: Cached {len(self._cache):,} valid toponyms")

        # 4. Filter triplets
        available_ids = set(self._cache.keys())

        print(f"Phase3Dataset: Filtering triplets...")
        df = self.triplets_df

        valid_anchor_ids = {
            tid for tid, data in self._cache.items()
            if data['split'] == split
        }

        mask_valid = (
            df['anchor_id'].isin(valid_anchor_ids) &
            df['positive_id'].isin(available_ids) &
            df['negative_id'].isin(available_ids)
        )

        self.valid_df = df[mask_valid].reset_index(drop=True)

        # Free memory
        del self.triplets_df

        print(f"Phase3Dataset: {len(self.valid_df):,} valid triplets for {split}")

    def __len__(self) -> int:
        return len(self.valid_df)

    def __getitem__(self, idx: int) -> Dict:
        row = self.valid_df.iloc[idx]
        return {
            'anchor': self._cache[row['anchor_id']],
            'positive': self._cache[row['positive_id']],
            'negative': self._cache[row['negative_id']],
            'negative_type': row['negative_type'],
        }


# ============================================================================
# Collate Functions
# ============================================================================

def pad_features(features_list: List[List[float]], feature_dim: int = 24) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Pad phonetic features to same length.

    Returns:
        Tuple of (features [B, L, D], lengths [B])
    """
    # Reshape to [L, D]
    reshaped = []
    lengths = []
    for feat in features_list:
        if feat:
            L = len(feat) // feature_dim
            arr = torch.tensor(feat, dtype=torch.float32).view(L, feature_dim)
            reshaped.append(arr)
            lengths.append(L)
        else:
            reshaped.append(torch.zeros(1, feature_dim))
            lengths.append(1)

    # Pad
    max_len = max(lengths)
    batch_size = len(reshaped)

    padded = torch.zeros(batch_size, max_len, feature_dim)
    for i, arr in enumerate(reshaped):
        padded[i, :arr.shape[0], :] = arr

    return padded, torch.tensor(lengths, dtype=torch.long)


def pad_char_ids(char_ids_list: List[List[int]]) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Pad character ID sequences to same length.

    Returns:
        Tuple of (char_ids [B, L], lengths [B])
    """
    lengths = [len(ids) for ids in char_ids_list]
    max_len = max(lengths)

    padded = torch.zeros(len(char_ids_list), max_len, dtype=torch.long)
    for i, ids in enumerate(char_ids_list):
        padded[i, :len(ids)] = torch.tensor(ids, dtype=torch.long)

    return padded, torch.tensor(lengths, dtype=torch.long)


def collate_phase1(batch: List[Dict]) -> Dict[str, torch.Tensor]:
    """
    Collate function for Phase 1 (Teacher training).

    Pads phonetic features for anchor, positive, negative.
    """
    anchors = [item['anchor']['features'] for item in batch]
    positives = [item['positive']['features'] for item in batch]
    negatives = [item['negative']['features'] for item in batch]

    anchor_feats, anchor_lens = pad_features(anchors)
    pos_feats, pos_lens = pad_features(positives)
    neg_feats, neg_lens = pad_features(negatives)

    return {
        'anchor_features': anchor_feats,
        'anchor_lengths': anchor_lens,
        'positive_features': pos_feats,
        'positive_lengths': pos_lens,
        'negative_features': neg_feats,
        'negative_lengths': neg_lens,
    }


def collate_phase2(
        batch: List[Dict],
        char_vocab: object,
        script_vocab: object,
        lang_vocab: object,
        noise_prob: float = 0.3,
        training: bool = True,
) -> Dict[str, torch.Tensor]:
    """
    Collate function for Phase 2 (Student-Teacher alignment).
    """
    # Extract data
    names = [item['name'] for item in batch]
    scripts = [item['script'] for item in batch]  # Ground truth strings
    langs = [item['lang'] for item in batch]
    features = [item['features'] for item in batch]

    # Apply noise during training
    if training and noise_prob > 0:
        names = [apply_character_noise(name, noise_prob) for name in names]

    # Re-encode names and scripts
    from phonetics.utils.script_detection import Script
    char_ids_list = []
    script_ids_list = []

    for name, script_str in zip(names, scripts):
        # 1. Encode Chars
        try:
            script_enum = Script(script_str)
        except ValueError:
            script_enum = Script.OTHER

        char_ids = char_vocab.encode(name, script_enum)
        char_ids_list.append(char_ids)

        # 2. Encode Script (Use the Enum, NOT encode_text)
        # This ensures we use the Ground Truth from Parquet
        script_ids_list.append(script_vocab.encode(script_enum))

    # Pad character sequences
    char_ids, char_lengths = pad_char_ids(char_ids_list)

    # Convert lists to tensors
    script_ids = torch.tensor(script_ids_list, dtype=torch.long)
    lang_ids = torch.tensor([lang_vocab.encode(lang) for lang in langs], dtype=torch.long)

    # Pad features (Teacher targets)
    feats, feat_lengths = pad_features(features)

    return {
        'char_ids': char_ids,
        'char_lengths': char_lengths,
        'script_ids': script_ids,
        'lang_ids': lang_ids,
        'features': feats,
        'feature_lengths': feat_lengths,
    }


def collate_phase3(
        batch: List[Dict],
        char_vocab: object,
        script_vocab: object,
        lang_vocab: object,
        noise_prob: float = 0.3,
        training: bool = True,
) -> Dict[str, torch.Tensor]:
    """
    Collate function for Phase 3 (Contrastive fine-tuning).

    Applies noise to anchors only (not positives/negatives).
    """
    from phonetics.utils.script_detection import Script

    def encode_toponyms(items: List[Dict], apply_noise: bool = False) -> Tuple[
        torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Encode a list of toponym dicts to tensors."""
        char_ids_list = []
        script_ids = []
        lang_ids = []

        for item in items:
            if not item:
                # Handle missing items
                char_ids_list.append([0])
                script_ids.append(0)
                lang_ids.append(0)
                continue

            name = item.get('name', '')
            script_str = item.get('script', 'OTHER')
            lang = item.get('lang', '')

            # Apply noise if requested
            if apply_noise and noise_prob > 0:
                name = apply_character_noise(name, noise_prob)

            try:
                script = Script(script_str)
            except ValueError:
                script = Script.OTHER

            char_ids = char_vocab.encode(name, script)
            char_ids_list.append(char_ids if char_ids else [0])

            script_ids.append(script_vocab.encode(script))
            lang_ids.append(lang_vocab.encode(lang))

        char_ids_padded, char_lengths = pad_char_ids(char_ids_list)

        return (
            char_ids_padded,
            char_lengths,
            torch.tensor(script_ids, dtype=torch.long),
            torch.tensor(lang_ids, dtype=torch.long),
        )

    # Encode anchors (with noise during training)
    anchors = [item['anchor'] for item in batch]
    anchor_chars, anchor_lens, anchor_scripts, anchor_langs = encode_toponyms(
        anchors, apply_noise=training
    )

    # Encode positives (no noise)
    positives = [item['positive'] for item in batch]
    pos_chars, pos_lens, pos_scripts, pos_langs = encode_toponyms(positives, apply_noise=False)

    # Encode negatives (no noise)
    negatives = [item['negative'] for item in batch]
    neg_chars, neg_lens, neg_scripts, neg_langs = encode_toponyms(negatives, apply_noise=False)

    return {
        'anchor_char_ids': anchor_chars,
        'anchor_char_lengths': anchor_lens,
        'anchor_script_ids': anchor_scripts,
        'anchor_lang_ids': anchor_langs,
        'positive_char_ids': pos_chars,
        'positive_char_lengths': pos_lens,
        'positive_script_ids': pos_scripts,
        'positive_lang_ids': pos_langs,
        'negative_char_ids': neg_chars,
        'negative_char_lengths': neg_lens,
        'negative_script_ids': neg_scripts,
        'negative_lang_ids': neg_langs,
    }


# ============================================================================
# DataLoader Factory
# ============================================================================

def create_phase1_dataloader(
        data_dir: Path,
        split: str = 'train',
        batch_size: int = 128,
        num_workers: int = 4,
        shuffle: bool = True,
        use_enriched: bool = True,
) -> DataLoader:
    """
    Create DataLoader for Phase 1 training.

    Args:
        use_enriched: If True, prefer Phase1DatasetEnriched (instant load).
                      Falls back to Phase1Dataset if enriched not available.
    """
    # Try enriched dataset first (instant load vs 30+ minutes)
    if use_enriched:
        enriched_path = Path(data_dir) / 'triplets' / 'phase1_enriched'
        if enriched_path.exists():
            dataset = Phase1DatasetEnriched(data_dir, split)
        else:
            print(f"Enriched triplets not found at {enriched_path}, using standard loader", flush=True)
            dataset = Phase1Dataset(data_dir, split)
    else:
        dataset = Phase1Dataset(data_dir, split)

    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=num_workers,
        collate_fn=collate_phase1,
        pin_memory=True,
    )


def create_phase2_dataloader(
        data_dir: Path,
        char_vocab: object,
        script_vocab: object,
        lang_vocab: object,
        split: str = 'train',
        batch_size: int = 128,
        num_workers: int = 4,
        shuffle: bool = True,
        noise_prob: float = 0.3,
) -> DataLoader:
    """Create DataLoader for Phase 2 training."""
    dataset = Phase2Dataset(data_dir, split)

    training = (split == 'train')

    def collate_fn(batch):
        return collate_phase2(
            batch, char_vocab, script_vocab, lang_vocab,
            noise_prob=noise_prob if training else 0.0,
            training=training,
        )

    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=num_workers,
        collate_fn=collate_fn,
        pin_memory=True,
    )


def create_phase3_dataloader(
        data_dir: Path,
        char_vocab: object,
        script_vocab: object,
        lang_vocab: object,
        split: str = 'train',
        batch_size: int = 128,
        num_workers: int = 4,
        shuffle: bool = True,
        noise_prob: float = 0.3,
) -> DataLoader:
    """Create DataLoader for Phase 3 training."""
    dataset = Phase3Dataset(data_dir, split)

    training = (split == 'train')

    def collate_fn(batch):
        return collate_phase3(
            batch, char_vocab, script_vocab, lang_vocab,
            noise_prob=noise_prob if training else 0.0,
            training=training,
        )

    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=num_workers,
        collate_fn=collate_fn,
        pin_memory=True,
    )