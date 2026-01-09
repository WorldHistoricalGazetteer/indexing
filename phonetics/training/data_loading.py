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

try:
    import pyarrow.parquet as pq
    import pyarrow.dataset as ds
except ImportError:
    raise ImportError("pyarrow required: pip install pyarrow")


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

class Phase1Dataset(Dataset):
    """
    Dataset for Phase 1: Teacher training on phonetic features.
    OPTIMIZED: Uses Pandas vectorization for instant loading (<10s).
    FIXED: Converts NumPy arrays to Python lists to prevent DataLoader crashes.
    LOGIC: Anchors must match split; Pos/Neg can be from any split.
    """

    def __init__(
            self,
            data_dir: Path,
            split: str = 'train',
    ):
        self.data_dir = Path(data_dir)
        print(f"Phase1Dataset: Loading data for split '{split}'...")

        # 1. Load Triplets -> Pandas (Instant Read)
        triplets_path = self.data_dir / 'triplets' / 'phase1'
        self.triplets_df = ds.dataset(triplets_path, format='parquet').to_table().to_pandas()

        # 2. Load Toponyms -> Pandas
        toponyms_path = get_training_data_path(self.data_dir)
        dataset = ds.dataset(toponyms_path, format='parquet', partitioning='hive')

        # Load only necessary columns
        table = dataset.to_table(columns=['toponym_id', 'features', 'feature_length', 'split'])
        topo_df = table.to_pandas()

        # 3. Build Cache & Sets (Vectorized)

        # A. Filter for valid features (Drop NaNs / Empty)
        valid_feats_df = topo_df[(topo_df['features'].notna()) & (topo_df['feature_length'] > 0)]

        # Ensure 'features' column contains Python lists, not NumPy arrays
        if not valid_feats_df.empty and 'features' in valid_feats_df.columns:
            first_val = valid_feats_df['features'].iloc[0]
            if isinstance(first_val, np.ndarray):
                valid_feats_df = valid_feats_df.copy()  # Avoid SettingWithCopyWarning
                valid_feats_df['features'] = valid_feats_df['features'].apply(lambda x: x.tolist())

        # B. Create Cache (Dict lookup)
        # orient='index' is optimized for creating {id: {col: val}}
        self._feature_cache = valid_feats_df.set_index('toponym_id')[['features', 'feature_length']].to_dict(
            orient='index')

        # C. Identify IDs available in cache (Any split)
        available_ids = set(self._feature_cache.keys())

        # D. Identify IDs allowed as Anchors (Strict split filtering)
        valid_anchor_ids = set(topo_df[topo_df['split'] == split]['toponym_id'])

        print(f"Phase1Dataset: Filtering {len(self.triplets_df)} raw triplets (Vectorized)...")

        # 4. Vectorized Filtering

        # Step 4a: Anchor MUST be in the correct split
        df = self.triplets_df
        mask_anchor_split = df['anchor_id'].isin(valid_anchor_ids)

        # Step 4b: ALL 3 parts must have valid features
        mask_features = (
                df['anchor_id'].isin(available_ids) &
                df['positive_id'].isin(available_ids) &
                df['negative_id'].isin(available_ids)
        )

        # Apply combined mask
        self.valid_df = df[mask_anchor_split & mask_features].reset_index(drop=True)

        print(f"Phase1Dataset: {len(self.valid_df)} valid triplets for {split}")

    def __len__(self) -> int:
        return len(self.valid_df)

    def __getitem__(self, idx: int) -> Dict:
        # Fetch row from filtered DataFrame
        row = self.valid_df.iloc[idx]

        return {
            'anchor': self._feature_cache[row['anchor_id']],
            'positive': self._feature_cache[row['positive_id']],
            'negative': self._feature_cache[row['negative_id']],
        }


class Phase2Dataset(Dataset):
    """
    Dataset for Phase 2: Student-Teacher alignment.
    OPTIMIZED: Uses Pandas for instant loading.
    SAFE: Filters out IDs that exceed model vocabulary limits.
    """

    def __init__(
            self,
            data_dir: Path,
            split: str = 'train',
            require_features: bool = True,
            vocab_limits: Optional[Dict[str, int]] = None,
    ):
        self.data_dir = Path(data_dir)
        print(f"Phase2Dataset: Loading {split} data (Vectorized)...")

        # Load vocab limits dynamically if not provided
        if vocab_limits is None:
            vocab_limits = load_vocab_limits(self.data_dir)

        # 1. Load Toponyms -> Pandas
        toponyms_path = get_training_data_path(self.data_dir)
        dataset = ds.dataset(toponyms_path, format='parquet', partitioning='hive')

        columns = [
            'toponym_id', 'name', 'script', 'lang',
            'char_ids', 'char_length',
            'features', 'feature_length', 'epitran_supported',
            'split'
        ]

        df = dataset.to_table(columns=columns).to_pandas()

        # 2. Vectorized Filtering
        mask_split = (df['split'] == split)

        if require_features:
            mask_features = (
                    df['features'].notna() &
                    (df['feature_length'] > 0) &
                    (df['epitran_supported'] == True)
            )
            df = df[mask_split & mask_features]
        else:
            df = df[mask_split]

        # 3. SANITIZATION (Critical Fix for CUDA Errors)
        # ---------------------------------------------------------
        print(f"Phase2Dataset: Sanitizing {len(df)} rows against vocab limits...")
        initial_count = len(df)

        # A. Ensure Script IDs are valid
        if pd.api.types.is_integer_dtype(df['script']):
            mask_script_safe = (df['script'] >= 0) & (df['script'] < vocab_limits['script'])
            df = df[mask_script_safe]

        # B. Ensure Char IDs are valid
        if not df.empty:
            # Convert features to Python lists first (prevent downstream crash)
            first_val = df['features'].iloc[0]
            if isinstance(first_val, np.ndarray):
                df['features'] = df['features'].apply(lambda x: x.tolist())

            # Check Char IDs robustly (Handle both List and NumPy)
            limit = vocab_limits['char']

            def is_safe_chars(ids):
                # Handle None/NaN
                if ids is None: return True

                # Handle NumPy Array
                if isinstance(ids, np.ndarray):
                    if ids.size == 0: return True
                    return ids.min() >= 0 and ids.max() < limit

                # Handle Python List
                if not ids: return True  # Empty list
                return min(ids) >= 0 and max(ids) < limit

            # Apply filter
            mask_chars_safe = df['char_ids'].apply(is_safe_chars)
            df = df[mask_chars_safe]

        dropped = initial_count - len(df)
        if dropped > 0:
            logger.warning(f"⚠️ Dropped {dropped} rows containing out-of-bounds IDs.")

        # 4. Final Conversion
        print(f"Phase2Dataset: converting {len(df)} rows to internal format...")

        if 'lang' in df.columns:
            df['lang'] = df['lang'].fillna('')

        self.samples = df.to_dict('records')
        print(f"Phase2Dataset: {len(self.samples)} clean samples ready for {split}")

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> Dict:
        return self.samples[idx]


class Phase3Dataset(Dataset):
    """
    Dataset for Phase 3: Contrastive fine-tuning with hard negatives.
    OPTIMIZED: Uses Pandas for instant loading.
    SAFE: Sanitizes IDs to prevent CUDA crashes.
    """

    def __init__(
            self,
            data_dir: Path,
            split: str = 'train',
            vocab_limits: Optional[Dict[str, int]] = None,
    ):
        self.data_dir = Path(data_dir)
        print(f"Phase3Dataset: Loading {split} data (Vectorized)...")

        # Load vocab limits dynamically if not provided
        if vocab_limits is None:
            vocab_limits = load_vocab_limits(self.data_dir)

        # 1. Load Triplets -> Pandas
        triplets_path = self.data_dir / 'triplets' / 'phase3'
        self.triplets_df = ds.dataset(triplets_path, format='parquet').to_table().to_pandas()

        # 2. Load Toponyms -> Pandas
        toponyms_path = get_training_data_path(self.data_dir)
        dataset = ds.dataset(toponyms_path, format='parquet', partitioning='hive')

        columns = [
            'toponym_id', 'name', 'script', 'lang',
            'char_ids', 'char_length', 'split'
        ]
        topo_df = dataset.to_table(columns=columns).to_pandas()

        # 3. SANITIZATION
        print(f"Phase3Dataset: Sanitizing {len(topo_df)} rows against vocab limits...")

        # A. Ensure Script IDs are valid
        if pd.api.types.is_integer_dtype(topo_df['script']):
            mask_script_safe = (topo_df['script'] >= 0) & (topo_df['script'] < vocab_limits['script'])
            topo_df = topo_df[mask_script_safe]

        # B. Ensure Char IDs are valid
        if not topo_df.empty:
            limit = vocab_limits['char']

            def is_safe_chars(ids):
                if ids is None: return True
                if isinstance(ids, np.ndarray):
                    if ids.size == 0: return True
                    return ids.min() >= 0 and ids.max() < limit
                if not ids: return True
                return min(ids) >= 0 and max(ids) < limit

            mask_chars_safe = topo_df['char_ids'].apply(is_safe_chars)
            topo_df = topo_df[mask_chars_safe]

        # 4. Build Cache
        if 'lang' in topo_df.columns:
            topo_df['lang'] = topo_df['lang'].fillna('')

        self._cache = topo_df.set_index('toponym_id')[
            ['name', 'script', 'lang', 'char_ids', 'char_length', 'split']
        ].to_dict(orient='index')

        available_ids = set(self._cache.keys())

        # 5. Vectorized Triplet Filtering
        print(f"Phase3Dataset: Filtering {len(self.triplets_df)} raw triplets...")

        valid_anchor_ids = set(topo_df[topo_df['split'] == split]['toponym_id'])

        df = self.triplets_df
        mask_valid = (
                df['anchor_id'].isin(valid_anchor_ids) &
                df['positive_id'].isin(available_ids) &
                df['negative_id'].isin(available_ids)
        )

        self.valid_df = df[mask_valid].reset_index(drop=True)
        print(f"Phase3Dataset: {len(self.valid_df)} valid triplets for {split}")

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
) -> DataLoader:
    """Create DataLoader for Phase 1 training."""
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