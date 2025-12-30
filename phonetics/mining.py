"""
Hard negative mining for Stage B curriculum training.
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
    similarity_threshold: float = 0.60,
    max_negatives_per_anchor: int = 50,
    random_sample_size: int = 5_000_000,
    device: str = 'cuda'
) -> Dict[int, List[int]]:
    """
    Hybrid Mining: Finds hard negatives from random pairs AND Stage A triplets.

    Strategy:
    1. RANDOM SAMPLING: Sample random item pairs to find "background noise" -
       unrelated items that the model incorrectly thinks are similar.
    2. TRIPLET SCANNING: Scan the pre-computed Stage A triplets to find
       spelling lookalikes the model still confuses.

    Args:
        model: Trained HybridPhoneticModel
        data_path: Path to training HDF5 file (optimized Phase 3 format)
        char_vocab: CharVocab instance (unused for optimized format, kept for API)
        lang_vocab: LangVocab instance (unused for optimized format, kept for API)
        similarity_threshold: Pairs with model sim > this are "hard negatives"
        max_negatives_per_anchor: Max negatives to keep per anchor item
        random_sample_size: Number of random pairs to check
        device: cuda or cpu

    Returns:
        Dict mapping anchor_idx -> list of hard negative indices
    """
    print("=" * 60)
    print("MINING HARD NEGATIVES (Corrected Hybrid Strategy)")
    print("=" * 60)
    print(f"  Data: {data_path}")
    print(f"  Similarity threshold: {similarity_threshold}")
    print(f"  Random samples: {random_sample_size:,}")
    print(f"  (Phase 2 will scan ALL Stage A triplets)")
    print()

    model.eval()
    device = torch.device(device if torch.cuda.is_available() else 'cpu')
    model = model.to(device)

    mined_negatives: Dict[int, List[int]] = {}
    total_random = 0
    total_targeted = 0

    with h5py.File(data_path, 'r') as f:
        # Check for optimized format
        if 'triplets' not in f or 'items/char_ids' not in f:
            raise ValueError("This mining function requires optimized Phase 3 HDF5 format")

        # Load all data into RAM
        n_items = f['items']['char_ids'].shape[0]
        print(f"Loading {n_items:,} items into RAM...")

        all_char_ids = f['items']['char_ids'][:]
        all_char_lengths = f['items']['char_lengths'][:]
        all_lang_ids = f['items']['lang_ids'][:]

        # Load Stage A triplets (anchor, positive, negative)
        # The negative_idx contains the spelling lookalikes we need to check
        print("Loading Stage A triplets for targeted mining...")
        trip_anchor_idxs = f['triplets']['anchor_idx'][:]
        trip_negative_idxs = f['triplets']['negative_idx'][:]
        num_triplets = len(trip_anchor_idxs)
        print(f"  Triplets to scan: {num_triplets:,}")

        # Build positive pairs set (for filtering random samples)
        trip_positive_idxs = f['triplets']['positive_idx'][:]
        positive_pairs: Set[Tuple[int, int]] = set()
        for a, p in zip(trip_anchor_idxs, trip_positive_idxs):
            positive_pairs.add((int(a), int(p)))
            positive_pairs.add((int(p), int(a)))
        print(f"  Positive pairs: {len(positive_pairs):,}")

    max_seq_len = all_char_ids.shape[1]
    batch_size = 8192  # Larger batches for speed

    # =========================================================================
    # PHASE 1: RANDOM SAMPLING (Fix background noise)
    # =========================================================================
    print(f"\n--- Phase 1: Random Sampling ({random_sample_size:,} pairs) ---")
    print("  Goal: Push apart unrelated items (fix high baseline similarity)")

    rng = np.random.default_rng(seed=42)
    num_batches = (random_sample_size + batch_size - 1) // batch_size

    for batch_idx in range(num_batches):
        curr_batch_size = min(batch_size, random_sample_size - batch_idx * batch_size)

        # Vectorized random selection
        idxs1 = rng.integers(0, n_items, size=curr_batch_size)
        idxs2 = rng.integers(0, n_items, size=curr_batch_size)

        # Prepare batch
        c1 = torch.from_numpy(all_char_ids[idxs1]).long().to(device)
        l1 = torch.from_numpy(all_lang_ids[idxs1]).long().to(device)
        len1 = torch.from_numpy(np.minimum(all_char_lengths[idxs1], max_seq_len)).long()

        c2 = torch.from_numpy(all_char_ids[idxs2]).long().to(device)
        l2 = torch.from_numpy(all_lang_ids[idxs2]).long().to(device)
        len2 = torch.from_numpy(np.minimum(all_char_lengths[idxs2], max_seq_len)).long()

        with torch.no_grad():
            emb1 = model.encode_char_only(c1, l1, len1)
            emb2 = model.encode_char_only(c2, l2, len2)
            sims = F.cosine_similarity(emb1, emb2)

        # Find failures (similarity > threshold for random pairs = model error)
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
                total_random += 1

        if batch_idx % 50 == 0:
            print(f"  Batch {batch_idx}/{num_batches}, found {total_random:,} hard negatives...", end='\r')

        # Early stopping if we find enough
        if total_random > 500_000:
            print(f"\n  Hit 500k random limit at batch {batch_idx}. Stopping Phase 1.")
            break

    print(f"\n  Phase 1 complete: {total_random:,} random hard negatives")

    # =========================================================================
    # PHASE 2: TRIPLET SCANNING (Fix spelling confusions)
    # =========================================================================
    print(f"\n--- Phase 2: Triplet Scanning ({num_triplets:,} Stage A pairs) ---")
    print("  Goal: Find spelling lookalikes the model still confuses")

    # Scan the ACTUAL Stage A triplets - these are (anchor, negative) pairs
    # where negative is orthographically close but phonetically different

    for batch_start in range(0, num_triplets, batch_size):
        batch_end = min(batch_start + batch_size, num_triplets)

        # Get indices from the TRIPLET arrays (not random!)
        idxs1 = trip_anchor_idxs[batch_start:batch_end]
        idxs2 = trip_negative_idxs[batch_start:batch_end]

        # Prepare batch
        c1 = torch.from_numpy(all_char_ids[idxs1]).long().to(device)
        l1 = torch.from_numpy(all_lang_ids[idxs1]).long().to(device)
        len1 = torch.from_numpy(np.minimum(all_char_lengths[idxs1], max_seq_len)).long()

        c2 = torch.from_numpy(all_char_ids[idxs2]).long().to(device)
        l2 = torch.from_numpy(all_lang_ids[idxs2]).long().to(device)
        len2 = torch.from_numpy(np.minimum(all_char_lengths[idxs2], max_seq_len)).long()

        with torch.no_grad():
            emb1 = model.encode_char_only(c1, l1, len1)
            emb2 = model.encode_char_only(c2, l2, len2)
            sims = F.cosine_similarity(emb1, emb2)

        # Find failures (model thinks spelling lookalikes are similar = error)
        failures = torch.where(sims > similarity_threshold)[0].cpu().numpy()

        for fail_idx in failures:
            a_idx = int(idxs1[fail_idx])
            n_idx = int(idxs2[fail_idx])

            if a_idx not in mined_negatives:
                mined_negatives[a_idx] = []
            # Allow extra negatives for targeted cases (harder)
            if len(mined_negatives[a_idx]) < (max_negatives_per_anchor + 20):
                mined_negatives[a_idx].append(n_idx)
                total_targeted += 1

        if (batch_start // batch_size) % 50 == 0:
            print(f"  Scanned {batch_end:,}/{num_triplets:,}, found {total_targeted:,} hard negatives...", end='\r')

    print(f"\n  Phase 2 complete: {total_targeted:,} targeted hard negatives")

    # =========================================================================
    # SUMMARY
    # =========================================================================
    total_hard = total_random + total_targeted
    print(f"\n{'='*60}")
    print("MINING COMPLETE")
    print(f"{'='*60}")
    print(f"  Random hard negatives:   {total_random:,}")
    print(f"  Targeted hard negatives: {total_targeted:,}")
    print(f"  Total hard negatives:    {total_hard:,}")
    print(f"  Unique anchors:          {len(mined_negatives):,}")

    if mined_negatives:
        avg = total_hard / len(mined_negatives)
        print(f"  Average per anchor:      {avg:.1f}")

    return mined_negatives