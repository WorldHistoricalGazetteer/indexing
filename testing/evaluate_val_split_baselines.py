#!/usr/bin/env python3
# testing/evaluate_val_split_baselines.py
"""
Evaluate baseline string similarity methods on validation split.

This provides a fair comparison: Levenshtein on the same AnyAscii-romanized
data that our model sees, using the same val split.

Usage:
    python -m testing.evaluate_val_split_baselines \
        --data /path/to/training_data_gn_optimized_phase3.h5
"""

import argparse
from dataclasses import dataclass
from typing import List, Dict

import h5py
import numpy as np

# Reuse the baseline functions from mehdie_benchmark
from testing.mehdie_benchmark import levenshtein_similarity, jaro_winkler_similarity


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


def evaluate_baselines_on_val_split(
        data_path: str,
        methods: Dict[str, callable] = None,
        thresholds: List[float] = None,
        val_ratio: float = 0.1,
        seed: int = 42,
        max_pairs: int = 50000,
) -> Dict[str, List[EvalResult]]:
    """
    Evaluate baseline similarity methods on validation split.
    """
    if methods is None:
        methods = {
            'Levenshtein': levenshtein_similarity,
            'Jaro-Winkler': jaro_winkler_similarity,
        }

    if thresholds is None:
        thresholds = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95]

    print("=" * 70)
    print("VALIDATION SPLIT BASELINE EVALUATION")
    print("=" * 70)
    print(f"  Data: {data_path}")
    print(f"  Methods: {list(methods.keys())}")
    print(f"  Max pairs: {max_pairs:,}")
    print()

    rng = np.random.default_rng(seed + 1)

    with h5py.File(data_path, 'r') as f:
        # Load romanized names
        print("Loading romanized names...")
        romanized = f['items']['romanized'][:]
        # Decode if needed
        romanized = np.array([
            n.decode('utf-8') if isinstance(n, bytes) else n
            for n in romanized
        ])
        print(f"  Loaded {len(romanized):,} names")

        # Load triplet indices
        trip_anchors = f['triplets']['anchor_idx'][:]
        trip_positives = f['triplets']['positive_idx'][:]
        trip_negatives = f['triplets']['negative_idx'][:]

        n_triplets = len(trip_anchors)
        print(f"  Total triplets: {n_triplets:,}")

    # Split into train/val (matching training code)
    indices = np.arange(n_triplets)
    rng.shuffle(indices)
    split_idx = int(n_triplets * (1 - val_ratio))
    val_indices = indices[split_idx:]

    print(f"  Validation triplets: {len(val_indices):,}")

    # Subsample if needed
    if len(val_indices) > max_pairs:
        val_indices = rng.choice(val_indices, size=max_pairs, replace=False)
        print(f"  Subsampled to {len(val_indices):,} pairs")

    # Get val triplets
    val_anchors = trip_anchors[val_indices]
    val_positives = trip_positives[val_indices]
    val_negatives = trip_negatives[val_indices]

    all_results = {}

    for method_name, similarity_fn in methods.items():
        print(f"\n{'=' * 70}")
        print(f"Evaluating: {method_name}")
        print("=" * 70)

        # Compute positive pair similarities
        print("Computing positive pair similarities...")
        positive_sims = []
        for i, (a_idx, p_idx) in enumerate(zip(val_anchors, val_positives)):
            name_a = romanized[a_idx]
            name_p = romanized[p_idx]
            # Note: levenshtein_similarity does its own anyascii internally,
            # but since we're already romanized, it's effectively a no-op
            sim = similarity_fn(name_a, name_p)
            positive_sims.append(sim)

            if (i + 1) % 10000 == 0:
                print(f"  Processed {i + 1:,}/{len(val_anchors):,}...", end='\r')

        positive_sims = np.array(positive_sims)
        print(f"\n  Positive pairs: {len(positive_sims):,}")
        print(f"  Similarity range: [{positive_sims.min():.3f}, {positive_sims.max():.3f}]")
        print(f"  Mean: {positive_sims.mean():.3f}, Std: {positive_sims.std():.3f}")

        # Compute negative pair similarities
        print("Computing negative pair similarities...")
        negative_sims = []
        for i, (a_idx, n_idx) in enumerate(zip(val_anchors, val_negatives)):
            name_a = romanized[a_idx]
            name_n = romanized[n_idx]
            sim = similarity_fn(name_a, name_n)
            negative_sims.append(sim)

            if (i + 1) % 10000 == 0:
                print(f"  Processed {i + 1:,}/{len(val_anchors):,}...", end='\r')

        negative_sims = np.array(negative_sims)
        print(f"\n  Negative pairs: {len(negative_sims):,}")
        print(f"  Similarity range: [{negative_sims.min():.3f}, {negative_sims.max():.3f}]")
        print(f"  Mean: {negative_sims.mean():.3f}, Std: {negative_sims.std():.3f}")

        # Compute metrics at each threshold
        print(f"\n{'θ':>6} {'P':>8} {'R':>8} {'F1':>8} {'F5':>8} {'TP':>8} {'FP':>8}")
        print("-" * 60)

        method_results = []
        for threshold in thresholds:
            tp = np.sum(positive_sims >= threshold)
            fn = np.sum(positive_sims < threshold)
            fp = np.sum(negative_sims >= threshold)
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
            method_results.append(result)

            print(f"{threshold:>6.2f} {precision:>8.3f} {recall:>8.3f} {f1:>8.3f} "
                  f"{f5:>8.3f} {tp:>8,} {fp:>8,}")

        all_results[method_name] = method_results

        # Best F1
        best_f1 = max(method_results, key=lambda r: r.f1)
        print("-" * 60)
        print(f"Best F1: {best_f1.f1:.3f} at θ={best_f1.threshold:.2f} "
              f"(P={best_f1.precision:.3f}, R={best_f1.recall:.3f})")

    return all_results


def main():
    parser = argparse.ArgumentParser(description='Evaluate baselines on validation split')
    parser.add_argument('--data', required=True, help='Path to optimized Phase 3 HDF5')
    parser.add_argument('--max-pairs', type=int, default=50000,
                        help='Max pairs to evaluate (baselines are slow)')
    parser.add_argument('--thresholds', type=float, nargs='+',
                        default=[0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95])

    args = parser.parse_args()

    results = evaluate_baselines_on_val_split(
        data_path=args.data,
        thresholds=args.thresholds,
        max_pairs=args.max_pairs,
    )

    if results:
        print("\n" + "=" * 70)
        print("SUMMARY COMPARISON")
        print("=" * 70)
        print(f"{'Method':<20} {'Best θ':>8} {'P':>8} {'R':>8} {'F1':>8} {'F5':>8}")
        print("-" * 60)
        for method_name, method_results in results.items():
            best = max(method_results, key=lambda r: r.f1)
            print(f"{method_name:<20} {best.threshold:>8.2f} {best.precision:>8.3f} "
                  f"{best.recall:>8.3f} {best.f1:>8.3f} {best.f5:>8.3f}")


if __name__ == '__main__':
    main()