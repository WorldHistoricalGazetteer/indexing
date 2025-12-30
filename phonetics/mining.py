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


def mine_hard_negatives(
    model,
    data_path: str,
    char_vocab,
    lang_vocab,
    similarity_threshold: float = 0.7,
    max_negatives_per_anchor: int = 10,
    sample_size: int = 50000,
    batch_size: int = 512,
    device: str = 'cuda'
) -> Dict[int, List[int]]:
    """
    Mine hard negatives from training data.

    Strategy:
    1. Load all items from the HDF5 file
    2. Build a set of known positive pairs
    3. Sample random item pairs that are NOT positives
    4. Find pairs where model similarity > threshold (model's mistakes)

    Args:
        model: Trained HybridPhoneticModel
        data_path: Path to training HDF5 file (e.g., training_data_gn.h5)
        char_vocab: CharVocab instance
        lang_vocab: LangVocab instance
        similarity_threshold: Pairs with model sim > this are "hard negatives"
        max_negatives_per_anchor: Max negatives to keep per anchor item
        sample_size: Number of random negative pairs to evaluate
        batch_size: Batch size for model inference
        device: cuda or cpu

    Returns:
        Dict mapping anchor_idx -> list of hard negative indices
    """
    print("=" * 60)
    print("MINING HARD NEGATIVES")
    print("=" * 60)
    print(f"  Data: {data_path}")
    print(f"  Similarity threshold: {similarity_threshold}")
    print(f"  Sample size: {sample_size:,}")
    print()

    model.eval()
    device = torch.device(device if torch.cuda.is_available() else 'cpu')
    model = model.to(device)

    with h5py.File(data_path, 'r') as f:
        # --- Load items ---
        items_grp = f['items']
        n_items = items_grp['toponym'].shape[0]

        print(f"Loading {n_items:,} items...")

        toponyms = items_grp['toponym'][:]
        langs = items_grp['lang'][:]

        # Decode bytes to strings if necessary
        if isinstance(toponyms[0], bytes):
            toponyms = [t.decode('utf-8') for t in toponyms]
        if isinstance(langs[0], bytes):
            langs = [l.decode('utf-8') for l in langs]

        # --- Build positive pairs set ---
        pairs_grp = f['pairs_with_phonetic']
        anchor_indices = pairs_grp['anchor_idx'][:]
        positive_indices = pairs_grp['positive_idx'][:]

        print(f"Building positive pairs set from {len(anchor_indices):,} pairs...")

        positive_pairs: Set[Tuple[int, int]] = set()
        for a, p in zip(anchor_indices, positive_indices):
            # Store both directions
            positive_pairs.add((int(a), int(p)))
            positive_pairs.add((int(p), int(a)))

        print(f"  Positive pairs (bidirectional): {len(positive_pairs):,}")

    # --- Pre-encode all items ---
    print(f"\nPre-encoding all {n_items:,} items...")

    all_embeddings = []

    for start in range(0, n_items, batch_size):
        end = min(start + batch_size, n_items)

        batch_toponyms = toponyms[start:end]
        batch_langs = langs[start:end]

        # Encode characters
        char_ids_list = []
        lang_ids_list = []
        lengths = []

        for topo, lang in zip(batch_toponyms, batch_langs):
            char_ids = char_vocab.encode(topo)
            lang_id = lang_vocab.encode(lang)

            char_ids_list.append(char_ids)
            lang_ids_list.append(lang_id)
            lengths.append(len(topo))

        # Pad sequences (use 0 as padding, standard convention)
        max_len = max(len(ids) for ids in char_ids_list)
        pad_id = 0  # Standard padding index
        padded_char_ids = []
        for ids in char_ids_list:
            padded = ids + [pad_id] * (max_len - len(ids))
            padded_char_ids.append(padded)

        char_ids_t = torch.tensor(padded_char_ids, dtype=torch.long, device=device)
        lang_ids_t = torch.tensor(lang_ids_list, dtype=torch.long, device=device)
        lengths_t = torch.tensor(lengths, dtype=torch.long)

        with torch.no_grad():
            emb = model.encode_char_only(char_ids_t, lang_ids_t, lengths_t)
            all_embeddings.append(emb.cpu())

        if (start // batch_size) % 100 == 0:
            print(f"  Encoded {end:,}/{n_items:,}...", end='\r')

    all_embeddings = torch.cat(all_embeddings, dim=0)  # (n_items, embed_dim)
    print(f"\n  Embeddings shape: {all_embeddings.shape}")

    # --- Sample random negative pairs ---
    print(f"\nSampling {sample_size:,} random negative pairs...")

    rng = np.random.default_rng(seed=42)

    negative_candidates = []
    attempts = 0
    max_attempts = sample_size * 10

    while len(negative_candidates) < sample_size and attempts < max_attempts:
        # Sample random pairs
        batch_anchors = rng.integers(0, n_items, size=batch_size)
        batch_others = rng.integers(0, n_items, size=batch_size)

        for a, o in zip(batch_anchors, batch_others):
            if a == o:
                continue
            if (int(a), int(o)) in positive_pairs:
                continue

            negative_candidates.append((int(a), int(o)))

            if len(negative_candidates) >= sample_size:
                break

        attempts += batch_size

    print(f"  Collected {len(negative_candidates):,} negative candidates")

    # --- Find hard negatives (high model similarity) ---
    print(f"\nFinding hard negatives (model sim > {similarity_threshold})...")

    mined_negatives: Dict[int, List[int]] = {}
    total_hard = 0

    for start in range(0, len(negative_candidates), batch_size):
        end = min(start + batch_size, len(negative_candidates))
        batch_pairs = negative_candidates[start:end]

        anchor_ids = [p[0] for p in batch_pairs]
        other_ids = [p[1] for p in batch_pairs]

        emb_anchors = all_embeddings[anchor_ids]
        emb_others = all_embeddings[other_ids]

        # Cosine similarity
        sims = F.cosine_similarity(emb_anchors, emb_others)

        # Find failures (high similarity = model thinks they're similar but they're not)
        hard_mask = sims > similarity_threshold
        hard_indices = torch.where(hard_mask)[0]

        for idx in hard_indices:
            a_id = anchor_ids[idx]
            n_id = other_ids[idx]

            if a_id not in mined_negatives:
                mined_negatives[a_id] = []

            if len(mined_negatives[a_id]) < max_negatives_per_anchor:
                mined_negatives[a_id].append(n_id)
                total_hard += 1

        if (start // batch_size) % 50 == 0:
            print(f"  Processed {end:,}/{len(negative_candidates):,}, found {total_hard:,} hard negatives...", end='\r')

    print(f"\n\nMining complete:")
    print(f"  Total hard negatives: {total_hard:,}")
    print(f"  Unique anchors with negatives: {len(mined_negatives):,}")

    if mined_negatives:
        avg_per_anchor = total_hard / len(mined_negatives)
        print(f"  Average negatives per anchor: {avg_per_anchor:.1f}")

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