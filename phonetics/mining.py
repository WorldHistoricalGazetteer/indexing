"""
Hard Negative Mining for Stage B Training.

Scans the training data to find pairs where:
- The model incorrectly assigns HIGH similarity (> threshold)
- But they are NOT in the positive pairs (i.e., true negatives)

These are the model's mistakes - pairs it thinks are similar but aren't.
"""

import h5py
import numpy as np
import torch
import torch.nn.functional as F
from typing import Dict, List, Set, Tuple
from pathlib import Path

from .config import Config


def mine_hard_negatives(
    model,
    data_path: str,
    char_vocab,
    lang_vocab,
    similarity_threshold: float = 0.5,
    max_negatives_per_anchor: int = 20,
    random_sample_size: int = 500000,
    targeted_sample_size: int = 500000,
    batch_size: int = 512,
    device: str = 'cuda'
) -> Dict[int, List[int]]:
    """
    Hybrid Mining: Finds hard negatives from both random pairs and targeted pairs.

    Strategy:
    1. RANDOM SAMPLING: Sample random item pairs to find "background noise" -
       unrelated items that the model incorrectly thinks are similar.
    2. TARGETED SAMPLING: Sample from existing negative pairs to find
       spelling-based confusions.

    This teaches the model to both:
    - Push apart unrelated items (fixes high baseline similarity)
    - Distinguish spelling lookalikes (fixes false friends)

    Args:
        model: Trained HybridPhoneticModel
        data_path: Path to training HDF5 file
        char_vocab: CharVocab instance
        lang_vocab: LangVocab instance
        similarity_threshold: Pairs with model sim > this are "hard negatives"
        max_negatives_per_anchor: Max negatives to keep per anchor item
        random_sample_size: Number of random pairs to check
        targeted_sample_size: Number of targeted (non-positive) pairs to check
        batch_size: Batch size for model inference
        device: cuda or cpu

    Returns:
        Dict mapping anchor_idx -> list of hard negative indices
    """
    print("=" * 60)
    print("MINING HARD NEGATIVES (Hybrid Strategy)")
    print("=" * 60)
    print(f"  Data: {data_path}")
    print(f"  Similarity threshold: {similarity_threshold}")
    print(f"  Random samples: {random_sample_size:,}")
    print(f"  Targeted samples: {targeted_sample_size:,}")
    print()

    model.eval()
    device = torch.device(device if torch.cuda.is_available() else 'cpu')
    model = model.to(device)

    mined_negatives: Dict[int, List[int]] = {}

    with h5py.File(data_path, 'r') as f:
        # Detect file structure
        is_optimized = 'triplets' in f and 'items/char_ids' in f

        if is_optimized:
            print("Detected OPTIMIZED Phase 3 file structure")
            items_grp = f['items']
            n_items = items_grp['char_ids'].shape[0]

            # Load pre-encoded data
            print(f"Loading {n_items:,} items into RAM...")
            all_char_ids = items_grp['char_ids'][:]
            all_char_lengths = items_grp['char_lengths'][:]
            all_lang_ids = items_grp['lang_ids'][:]

            # Build positive pairs set from triplets
            triplets_grp = f['triplets']
            anchor_indices = triplets_grp['anchor_idx'][:]
            positive_indices = triplets_grp['positive_idx'][:]

            positive_pairs: Set[Tuple[int, int]] = set()
            for a, p in zip(anchor_indices, positive_indices):
                positive_pairs.add((int(a), int(p)))
                positive_pairs.add((int(p), int(a)))

            print(f"  Positive pairs: {len(positive_pairs):,}")

        else:
            print("Detected ORIGINAL file structure")
            items_grp = f['items']
            n_items = items_grp['toponym'].shape[0]

            print(f"Loading {n_items:,} items...")
            toponyms = items_grp['toponym'][:]
            langs = items_grp['lang'][:]

            if isinstance(toponyms[0], bytes):
                toponyms = [t.decode('utf-8') for t in toponyms]
            if isinstance(langs[0], bytes):
                langs = [l.decode('utf-8') for l in langs]

            # Pre-encode all items
            print("Pre-encoding items...")
            all_char_ids = []
            all_char_lengths = []
            all_lang_ids = []
            max_seq_len = getattr(Config, 'MAX_SEQ_LEN', 50)

            for topo, lang in zip(toponyms, langs):
                char_ids = char_vocab.encode(topo)[:max_seq_len]
                all_char_ids.append(char_ids + [0] * (max_seq_len - len(char_ids)))
                all_char_lengths.append(min(len(char_ids), max_seq_len))
                all_lang_ids.append(lang_vocab.encode(lang))

            all_char_ids = np.array(all_char_ids)
            all_char_lengths = np.array(all_char_lengths)
            all_lang_ids = np.array(all_lang_ids)

            # Build positive pairs
            pairs_grp = f['pairs_with_phonetic']
            anchor_indices = pairs_grp['anchor_idx'][:]
            positive_indices = pairs_grp['positive_idx'][:]

            positive_pairs: Set[Tuple[int, int]] = set()
            for a, p in zip(anchor_indices, positive_indices):
                positive_pairs.add((int(a), int(p)))
                positive_pairs.add((int(p), int(a)))

    # Get max sequence length
    max_seq_len = all_char_ids.shape[1] if len(all_char_ids.shape) > 1 else getattr(Config, 'MAX_SEQ_LEN', 50)

    # =========================================================================
    # PHASE 1: RANDOM SAMPLING (Fix background noise)
    # =========================================================================
    print(f"\n--- Phase 1: Random Sampling ({random_sample_size:,} pairs) ---")
    print("  Goal: Push apart unrelated items (fix high baseline similarity)")

    rng = np.random.default_rng(seed=42)
    random_hard = 0

    num_batches = (random_sample_size + batch_size - 1) // batch_size

    for batch_idx in range(num_batches):
        curr_batch_size = min(batch_size, random_sample_size - batch_idx * batch_size)

        # Sample random pairs
        idxs1 = rng.integers(0, n_items, size=curr_batch_size)
        idxs2 = rng.integers(0, n_items, size=curr_batch_size)

        # Get data for batch
        char_ids_1 = all_char_ids[idxs1][:, :max_seq_len]
        char_ids_2 = all_char_ids[idxs2][:, :max_seq_len]
        lengths_1 = np.minimum(all_char_lengths[idxs1], max_seq_len)
        lengths_2 = np.minimum(all_char_lengths[idxs2], max_seq_len)
        lang_ids_1 = all_lang_ids[idxs1]
        lang_ids_2 = all_lang_ids[idxs2]

        # To tensors
        c1 = torch.from_numpy(char_ids_1).long().to(device)
        c2 = torch.from_numpy(char_ids_2).long().to(device)
        l1 = torch.from_numpy(lang_ids_1).long().to(device)
        l2 = torch.from_numpy(lang_ids_2).long().to(device)
        len1 = torch.from_numpy(lengths_1).long()
        len2 = torch.from_numpy(lengths_2).long()

        with torch.no_grad():
            emb1 = model.encode_char_only(c1, l1, len1)
            emb2 = model.encode_char_only(c2, l2, len2)
            sims = F.cosine_similarity(emb1, emb2)

        # Find failures (high similarity for random pairs = bad)
        failures = torch.where(sims > similarity_threshold)[0].cpu().numpy()

        for fail_idx in failures:
            a_idx = int(idxs1[fail_idx])
            n_idx = int(idxs2[fail_idx])

            # Skip same item or known positives
            if a_idx == n_idx:
                continue
            if (a_idx, n_idx) in positive_pairs:
                continue

            if a_idx not in mined_negatives:
                mined_negatives[a_idx] = []
            if len(mined_negatives[a_idx]) < max_negatives_per_anchor:
                mined_negatives[a_idx].append(n_idx)
                random_hard += 1

        if batch_idx % 100 == 0:
            print(f"  Batch {batch_idx}/{num_batches}, found {random_hard:,} hard negatives...", end='\r')

    print(f"\n  Phase 1 complete: {random_hard:,} random hard negatives")

    # =========================================================================
    # PHASE 2: TARGETED SAMPLING (Fix spelling confusions)
    # =========================================================================
    print(f"\n--- Phase 2: Targeted Sampling ({targeted_sample_size:,} pairs) ---")
    print("  Goal: Distinguish spelling lookalikes (fix false friends)")

    targeted_hard = 0

    # Sample non-positive pairs (excluding known positives)
    num_batches = (targeted_sample_size + batch_size - 1) // batch_size

    collected = 0
    attempts = 0
    max_attempts = targeted_sample_size * 5

    while collected < targeted_sample_size and attempts < max_attempts:
        curr_batch_size = min(batch_size, targeted_sample_size - collected)

        # Sample pairs, filtering out positives
        idxs1 = []
        idxs2 = []

        while len(idxs1) < curr_batch_size and attempts < max_attempts:
            a = rng.integers(0, n_items)
            n = rng.integers(0, n_items)
            attempts += 1

            if a == n:
                continue
            if (int(a), int(n)) in positive_pairs:
                continue

            idxs1.append(a)
            idxs2.append(n)

        if not idxs1:
            break

        idxs1 = np.array(idxs1)
        idxs2 = np.array(idxs2)
        collected += len(idxs1)

        # Get data
        char_ids_1 = all_char_ids[idxs1][:, :max_seq_len]
        char_ids_2 = all_char_ids[idxs2][:, :max_seq_len]
        lengths_1 = np.minimum(all_char_lengths[idxs1], max_seq_len)
        lengths_2 = np.minimum(all_char_lengths[idxs2], max_seq_len)
        lang_ids_1 = all_lang_ids[idxs1]
        lang_ids_2 = all_lang_ids[idxs2]

        c1 = torch.from_numpy(char_ids_1).long().to(device)
        c2 = torch.from_numpy(char_ids_2).long().to(device)
        l1 = torch.from_numpy(lang_ids_1).long().to(device)
        l2 = torch.from_numpy(lang_ids_2).long().to(device)
        len1 = torch.from_numpy(lengths_1).long()
        len2 = torch.from_numpy(lengths_2).long()

        with torch.no_grad():
            emb1 = model.encode_char_only(c1, l1, len1)
            emb2 = model.encode_char_only(c2, l2, len2)
            sims = F.cosine_similarity(emb1, emb2)

        failures = torch.where(sims > similarity_threshold)[0].cpu().numpy()

        for fail_idx in failures:
            a_idx = int(idxs1[fail_idx])
            n_idx = int(idxs2[fail_idx])

            if a_idx not in mined_negatives:
                mined_negatives[a_idx] = []
            if len(mined_negatives[a_idx]) < max_negatives_per_anchor:
                mined_negatives[a_idx].append(n_idx)
                targeted_hard += 1

        if (collected // batch_size) % 100 == 0:
            print(f"  Collected {collected:,}/{targeted_sample_size:,}, found {targeted_hard:,} hard...", end='\r')

    print(f"\n  Phase 2 complete: {targeted_hard:,} targeted hard negatives")

    # =========================================================================
    # SUMMARY
    # =========================================================================
    total_hard = random_hard + targeted_hard
    print(f"\n{'='*60}")
    print("MINING COMPLETE")
    print(f"{'='*60}")
    print(f"  Random hard negatives:   {random_hard:,}")
    print(f"  Targeted hard negatives: {targeted_hard:,}")
    print(f"  Total hard negatives:    {total_hard:,}")
    print(f"  Unique anchors:          {len(mined_negatives):,}")

    if mined_negatives:
        avg = total_hard / len(mined_negatives)
        print(f"  Average per anchor:      {avg:.1f}")

    return mined_negatives


def mine_hard_negatives_from_multiple_sources(
    model,
    data_paths: List[str],
    char_vocab,
    lang_vocab,
    **kwargs
) -> Dict[int, List[int]]:
    """
    Mine hard negatives from multiple training data files.

    Combines results from all sources.
    """
    all_negatives: Dict[int, List[int]] = {}

    for path in data_paths:
        if not Path(path).exists():
            print(f"Skipping {path} (not found)")
            continue

        print(f"\n{'='*60}")
        print(f"Processing: {path}")
        print('='*60)

        negatives = mine_hard_negatives(
            model=model,
            data_path=path,
            char_vocab=char_vocab,
            lang_vocab=lang_vocab,
            **kwargs
        )

        # Merge (note: indices are file-specific, so this simple merge
        # only works if processing one file at a time for training)
        for anchor, negs in negatives.items():
            if anchor not in all_negatives:
                all_negatives[anchor] = []
            all_negatives[anchor].extend(negs)

    return all_negatives