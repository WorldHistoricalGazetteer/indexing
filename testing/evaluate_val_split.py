#!/usr/bin/env python3

# testing/evaluate_val_split.py
"""
Evaluate trained model on validation split from training data.

This provides metrics (P/R/F5) on the same data distribution the model
was trained on, using held-out validation pairs.

Usage:
    python -m testing.evaluate_val_split \
        --model /path/to/final_model_b.pt \
        --data /path/to/training_data_gn_optimized_phase3.h5 \
        --gpu
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple
from dataclasses import dataclass

import h5py
import numpy as np
import torch
import torch.nn.functional as F


@dataclass
class EvalResult:
    threshold: float
    precision: float
    recall: float
    f1: float
    f5: float
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int


def load_model(model_path: str, device: str = 'cuda'):
    """Load trained model and vocabularies."""
    # Add parent to path for imports
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from phonetics.models import HybridPhoneticModel, PhoneticEncoder, CharEncoder
    from phonetics.vocab import CharVocab, LangVocab

    vocab_dir = Path(model_path).parent
    base_name = Path(model_path).stem

    char_vocab = CharVocab.load(vocab_dir / f'{base_name}_char_vocab.pkl')
    lang_vocab = LangVocab.load(vocab_dir / f'{base_name}_lang_vocab.pkl')

    checkpoint = torch.load(model_path, map_location=device)

    phonetic_encoder = PhoneticEncoder()
    char_encoder = CharEncoder(
        vocab_size=checkpoint.get('char_vocab_size', char_vocab.vocab_size),
        num_langs=checkpoint.get('num_langs', lang_vocab.next_id)
    )
    model = HybridPhoneticModel(phonetic_encoder, char_encoder)
    model.load_state_dict(checkpoint['model_state'])
    model = model.to(device)
    model.eval()

    return model, char_vocab, lang_vocab


def evaluate_on_val_split(
        model,
        data_path: str,
        device: str = 'cuda',
        thresholds: List[float] = None,
        val_ratio: float = 0.1,
        seed: int = 42,
        batch_size: int = 4096,
        max_negative_samples: int = 500000
) -> Dict[str, any]:
    """
    Evaluate model on validation split.

    Uses the same train/val split logic as training to ensure
    we're evaluating on truly held-out data.

    Args:
        model: Trained HybridPhoneticModel
        data_path: Path to optimized Phase 3 HDF5 file
        device: cuda or cpu
        thresholds: Similarity thresholds to evaluate
        val_ratio: Fraction of data used for validation (must match training)
        seed: Random seed (must match training)
        batch_size: Batch size for inference
        max_negative_samples: Max negative pairs to evaluate (for speed)

    Returns:
        Dict with results and statistics
    """
    if thresholds is None:
        thresholds = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95]

    print("=" * 70)
    print("VALIDATION SPLIT EVALUATION")
    print("=" * 70)
    print(f"  Data: {data_path}")
    print(f"  Val ratio: {val_ratio}")
    print(f"  Thresholds: {thresholds}")
    print()

    rng = np.random.default_rng(seed + 1)  # +1 to match val split in training

    with h5py.File(data_path, 'r') as f:
        # Load items
        n_items = f['items']['char_ids'].shape[0]
        print(f"Loading {n_items:,} items...")

        all_char_ids = f['items']['char_ids'][:]
        all_char_lengths = f['items']['char_lengths'][:]
        all_lang_ids = f['items']['lang_ids'][:]

        # Load triplets
        trip_anchors = f['triplets']['anchor_idx'][:]
        trip_positives = f['triplets']['positive_idx'][:]
        trip_negatives = f['triplets']['negative_idx'][:]

        n_triplets = len(trip_anchors)
        print(f"Total triplets: {n_triplets:,}")

    # Split into train/val (matching training code)
    indices = np.arange(n_triplets)
    rng.shuffle(indices)
    split_idx = int(n_triplets * (1 - val_ratio))
    val_indices = indices[split_idx:]

    print(f"Validation triplets: {len(val_indices):,}")

    # Get val triplets
    val_anchors = trip_anchors[val_indices]
    val_positives = trip_positives[val_indices]
    val_negatives = trip_negatives[val_indices]

    max_seq_len = all_char_ids.shape[1]

    # =========================================================================
    # Compute similarities for POSITIVE pairs (anchor, positive)
    # =========================================================================
    print("\nComputing positive pair similarities...")

    positive_sims = []

    for start in range(0, len(val_anchors), batch_size):
        end = min(start + batch_size, len(val_anchors))

        a_idx = val_anchors[start:end]
        p_idx = val_positives[start:end]

        c_a = torch.from_numpy(all_char_ids[a_idx]).long().to(device)
        l_a = torch.from_numpy(all_lang_ids[a_idx]).long().to(device)
        len_a = torch.from_numpy(np.minimum(all_char_lengths[a_idx], max_seq_len)).long()

        c_p = torch.from_numpy(all_char_ids[p_idx]).long().to(device)
        l_p = torch.from_numpy(all_lang_ids[p_idx]).long().to(device)
        len_p = torch.from_numpy(np.minimum(all_char_lengths[p_idx], max_seq_len)).long()

        with torch.no_grad():
            emb_a = model.encode_char_only(c_a, l_a, len_a)
            emb_p = model.encode_char_only(c_p, l_p, len_p)
            sims = F.cosine_similarity(emb_a, emb_p)
            positive_sims.extend(sims.cpu().numpy())

        if start % (batch_size * 10) == 0:
            print(f"  Processed {end:,}/{len(val_anchors):,}...", end='\r')

    positive_sims = np.array(positive_sims)
    print(f"\n  Positive pairs: {len(positive_sims):,}")
    print(f"  Similarity range: [{positive_sims.min():.3f}, {positive_sims.max():.3f}]")
    print(f"  Mean: {positive_sims.mean():.3f}, Std: {positive_sims.std():.3f}")

    # =========================================================================
    # Compute similarities for NEGATIVE pairs (anchor, negative)
    # =========================================================================
    print("\nComputing negative pair similarities...")

    # Subsample if too many
    if len(val_anchors) > max_negative_samples:
        neg_sample_idx = rng.choice(len(val_anchors), size=max_negative_samples, replace=False)
        sample_anchors = val_anchors[neg_sample_idx]
        sample_negatives = val_negatives[neg_sample_idx]
    else:
        sample_anchors = val_anchors
        sample_negatives = val_negatives

    negative_sims = []

    for start in range(0, len(sample_anchors), batch_size):
        end = min(start + batch_size, len(sample_anchors))

        a_idx = sample_anchors[start:end]
        n_idx = sample_negatives[start:end]

        c_a = torch.from_numpy(all_char_ids[a_idx]).long().to(device)
        l_a = torch.from_numpy(all_lang_ids[a_idx]).long().to(device)
        len_a = torch.from_numpy(np.minimum(all_char_lengths[a_idx], max_seq_len)).long()

        c_n = torch.from_numpy(all_char_ids[n_idx]).long().to(device)
        l_n = torch.from_numpy(all_lang_ids[n_idx]).long().to(device)
        len_n = torch.from_numpy(np.minimum(all_char_lengths[n_idx], max_seq_len)).long()

        with torch.no_grad():
            emb_a = model.encode_char_only(c_a, l_a, len_a)
            emb_n = model.encode_char_only(c_n, l_n, len_n)
            sims = F.cosine_similarity(emb_a, emb_n)
            negative_sims.extend(sims.cpu().numpy())

        if start % (batch_size * 10) == 0:
            print(f"  Processed {end:,}/{len(sample_anchors):,}...", end='\r')

    negative_sims = np.array(negative_sims)
    print(f"\n  Negative pairs: {len(negative_sims):,}")
    print(f"  Similarity range: [{negative_sims.min():.3f}, {negative_sims.max():.3f}]")
    print(f"  Mean: {negative_sims.mean():.3f}, Std: {negative_sims.std():.3f}")

    # =========================================================================
    # Compute metrics at each threshold
    # =========================================================================
    print("\n" + "=" * 70)
    print("RESULTS BY THRESHOLD")
    print("=" * 70)
    print(f"{'θ':>6} {'P':>8} {'R':>8} {'F1':>8} {'F5':>8} {'TP':>8} {'FP':>8} {'FN':>8} {'TN':>8}")
    print("-" * 70)

    results = []

    for threshold in thresholds:
        # Positive pairs above threshold = True Positives
        tp = np.sum(positive_sims >= threshold)
        # Positive pairs below threshold = False Negatives
        fn = np.sum(positive_sims < threshold)
        # Negative pairs above threshold = False Positives
        fp = np.sum(negative_sims >= threshold)
        # Negative pairs below threshold = True Negatives
        tn = np.sum(negative_sims < threshold)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        beta = 5
        f5 = (1 + beta ** 2) * precision * recall / (beta ** 2 * precision + recall) \
            if (beta ** 2 * precision + recall) > 0 else 0.0

        result = EvalResult(
            threshold=threshold,
            precision=precision,
            recall=recall,
            f1=f1,
            f5=f5,
            true_positives=int(tp),
            false_positives=int(fp),
            false_negatives=int(fn),
            true_negatives=int(tn)
        )
        results.append(result)

        print(f"{threshold:>6.2f} {precision:>8.3f} {recall:>8.3f} {f1:>8.3f} {f5:>8.3f} "
              f"{tp:>8,} {fp:>8,} {fn:>8,} {tn:>8,}")

    # Best F5
    best = max(results, key=lambda r: r.f5)
    print("-" * 70)
    print(f"Best F5: {best.f5:.3f} at θ={best.threshold:.2f} (P={best.precision:.3f}, R={best.recall:.3f})")

    # =========================================================================
    # Distribution analysis
    # =========================================================================
    print("\n" + "=" * 70)
    print("SIMILARITY DISTRIBUTION ANALYSIS")
    print("=" * 70)

    percentiles = [5, 10, 25, 50, 75, 90, 95]

    print("\nPositive pairs (should be HIGH similarity):")
    for p in percentiles:
        val = np.percentile(positive_sims, p)
        print(f"  {p:3d}th percentile: {val:.3f}")

    print("\nNegative pairs (should be LOW similarity):")
    for p in percentiles:
        val = np.percentile(negative_sims, p)
        print(f"  {p:3d}th percentile: {val:.3f}")

    # Overlap analysis
    print("\nOverlap analysis:")
    pos_below_90 = np.sum(positive_sims < 0.9) / len(positive_sims) * 100
    neg_above_70 = np.sum(negative_sims > 0.7) / len(negative_sims) * 100
    neg_above_80 = np.sum(negative_sims > 0.8) / len(negative_sims) * 100
    neg_above_90 = np.sum(negative_sims > 0.9) / len(negative_sims) * 100

    print(f"  Positive pairs with sim < 0.9: {pos_below_90:.1f}%")
    print(f"  Negative pairs with sim > 0.7: {neg_above_70:.1f}%")
    print(f"  Negative pairs with sim > 0.8: {neg_above_80:.1f}%")
    print(f"  Negative pairs with sim > 0.9: {neg_above_90:.1f}%")

    return {
        'results': results,
        'positive_sims': positive_sims,
        'negative_sims': negative_sims,
        'best_f5': best.f5,
        'best_threshold': best.threshold,
        'n_positive_pairs': len(positive_sims),
        'n_negative_pairs': len(negative_sims)
    }


def main():
    parser = argparse.ArgumentParser(description='Evaluate model on validation split')
    parser.add_argument('--model', required=True, help='Path to trained model')
    parser.add_argument('--data', required=True, help='Path to optimized Phase 3 HDF5')
    parser.add_argument('--gpu', action='store_true', help='Use GPU')
    parser.add_argument('--thresholds', type=float, nargs='+',
                        default=[0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95],
                        help='Thresholds to evaluate')
    parser.add_argument('--max-negatives', type=int, default=500000,
                        help='Max negative pairs to evaluate')

    args = parser.parse_args()

    device = 'cuda' if args.gpu and torch.cuda.is_available() else 'cpu'
    print(f"Using device: {device}")

    print("\nLoading model...")
    model, char_vocab, lang_vocab = load_model(args.model, device)
    print(f"  Char vocab: {char_vocab.vocab_size} chars")
    print(f"  Lang vocab: {lang_vocab.next_id} languages")

    results = evaluate_on_val_split(
        model=model,
        data_path=args.data,
        device=device,
        thresholds=args.thresholds,
        max_negative_samples=args.max_negatives
    )

    print("\n" + "=" * 70)
    print("EVALUATION COMPLETE")
    print("=" * 70)


if __name__ == '__main__':
    main()