#!/usr/bin/env python3
"""
Run MEHDIE benchmark evaluation using the trained Symphonym model.

This script evaluates:
1. Baseline string similarity methods (Levenshtein, Jaro-Winkler)
2. Our trained phonetic similarity model (Symphonym)
3. Comparison with MEHDIE paper results

Reference:
    Sagi et al. (2025) "Utilizing phonetic similarity for cross-source and
    cross-language toponym matching: a benchmark and prototype"
    Language Resources and Evaluation, 59:2427-2451

Usage:
    # Run with auto-detected latest model
    python -m testing.run_mehdie_evaluation --testsets testing/mehdie-testsets

    # Run with specific model version
    python -m testing.run_mehdie_evaluation --testsets testing/mehdie-testsets --version v3

    # Run baselines only (no model)
    python -m testing.run_mehdie_evaluation --testsets testing/mehdie-testsets --baselines-only
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import torch

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from testing.mehdie_benchmark import (
    MEHDIEBenchmark,
    levenshtein_similarity,
    jaro_winkler_similarity,
)


# =============================================================================
# Published results from MEHDIE paper (Sagi et al., 2025)
# =============================================================================

MEHDIE_PAPER_RESULTS = {
    'testset7-YaqutSham_KimaSham': {
        'num_matches': 30,
        'orthographic': {'f5': 0.67, 'threshold': 0.9},
        'phonetic': {'f5': 0.77, 'threshold': 0.9},
    },
    'testset8-KimaShamThurayyaSham': {
        'num_matches': 21,
        'orthographic': {'f5': 0.65, 'threshold': 0.9},
        'phonetic': {'f5': 0.68, 'threshold': 0.9},
    },
    'testset9-TudelaThurayya': {
        'num_matches': 18,
        'orthographic': {'f5': 0.92, 'threshold': 0.9},
        'phonetic': {'f5': 0.70, 'threshold': 0.9},
    },
    'testset10-YaqutAndalusMagrebKima-KimaMagrebAndalusMapped': {
        'num_matches': 28,
        'orthographic': {'f5': 0.75, 'threshold': 0.9},
        'phonetic': {'f5': 0.77, 'threshold': 0.9},
    },
    'testset11-DamastTudela': {
        'num_matches': 32,
        'orthographic': {'f5': 0.78, 'threshold': 0.9},
        'phonetic': {'f5': 0.88, 'threshold': 0.9},
    },
}


def get_latest_version(base_path: str) -> str:
    """Find the latest version directory (e.g., v3 > v2 > v1)."""
    base = Path(base_path)
    if not base.exists():
        return "v1"

    versions = []
    for d in base.iterdir():
        if d.is_dir() and d.name.startswith('v'):
            try:
                versions.append((int(d.name[1:]), d.name))
            except ValueError:
                continue

    if not versions:
        return "v1"

    return max(versions, key=lambda x: x[0])[1]


def create_symphonym_similarity_fn(encoder, benchmark: 'MEHDIEBenchmark' = None):
    """
    Create a similarity function from a ToponymEncoder.

    If benchmark is provided, pre-computes all embeddings for efficiency.

    Args:
        encoder: ToponymEncoder instance
        benchmark: Optional MEHDIEBenchmark to pre-compute embeddings for

    Returns:
        Function that takes two strings and returns similarity score
    """
    embedding_cache = {}

    # Pre-compute embeddings for all names in benchmark if provided
    if benchmark is not None:
        print("  Pre-computing embeddings for all benchmark names...")
        all_names = set()
        for testset in benchmark.testsets.values():
            for record in testset.dataset1.values():
                all_names.update(record['all_names'])
            for record in testset.dataset2.values():
                all_names.update(record['all_names'])

        # Filter empty names
        all_names = [n for n in all_names if n]
        print(f"  Found {len(all_names):,} unique names to encode...")

        # Batch encode
        if all_names:
            embeddings = encoder.encode_batch(all_names, batch_size=256, show_progress=True)
            for name, emb in zip(all_names, embeddings):
                embedding_cache[name] = emb

        print(f"  Cached {len(embedding_cache):,} embeddings")

    def get_embedding(name: str):
        if name in embedding_cache:
            return embedding_cache[name]

        emb = encoder.encode(name)
        embedding_cache[name] = emb
        return emb

    def similarity_fn(name1: str, name2: str) -> float:
        if not name1 or not name2:
            return 0.0

        emb1 = get_embedding(name1)
        emb2 = get_embedding(name2)

        sim = encoder.similarity(emb1, emb2).item()
        return max(0.0, sim)

    return similarity_fn


def run_evaluation(
        testsets_dir: str,
        checkpoint_path: str = None,
        vocab_dir: str = None,
        output_path: str = None,
        thresholds: list = None,
        device: str = 'cuda',
        baselines_only: bool = False,
):
    """Run full MEHDIE benchmark evaluation."""

    if thresholds is None:
        thresholds = [0.7, 0.8, 0.85, 0.9, 0.95]

    print("=" * 80)
    print("MEHDIE BENCHMARK EVALUATION")
    print("=" * 80)
    print(f"Testsets directory: {testsets_dir}")
    print(f"Model checkpoint: {checkpoint_path or 'None (baselines only)'}")
    print(f"Thresholds: {thresholds}")
    print(f"Device: {device}")
    print()

    # Load benchmark
    benchmark = MEHDIEBenchmark(testsets_dir)

    if not benchmark.testsets:
        print("ERROR: No testsets loaded. Check testsets directory.")
        return None

    # Define methods to evaluate
    methods = {}

    # Always include baselines
    methods['Levenshtein'] = levenshtein_similarity
    methods['Jaro-Winkler'] = jaro_winkler_similarity

    # Add Symphonym model if not baselines-only
    if not baselines_only and checkpoint_path:
        print(f"\nLoading Symphonym model from {checkpoint_path}...")

        from phonetics.inference.encoder import ToponymEncoder

        encoder = ToponymEncoder.from_checkpoint(
            checkpoint_path,
            vocab_dir,
            device=device,
        )
        print(f"Model loaded (embed_dim={encoder.embed_dim})")

        # Create similarity function with pre-computed embeddings
        methods['Symphonym'] = create_symphonym_similarity_fn(encoder, benchmark)

    # Run evaluation
    print("\n" + "=" * 80)
    print("RUNNING EVALUATION")
    print("=" * 80)

    all_results = benchmark.compare_methods(methods, thresholds)

    # Print comparison table
    print_f5_comparison(benchmark, all_results)

    # Compare with MEHDIE paper results
    print("\n" + "=" * 80)
    print("COMPARISON WITH MEHDIE PAPER RESULTS (F-5 metric)")
    print("=" * 80)
    print("\nNote: Paper uses F-5 which weights recall 5x more than precision.\n")

    comparison_data = []

    for testset_name in benchmark.testsets.keys():
        paper_results = MEHDIE_PAPER_RESULTS.get(testset_name, {})

        row = {'testset': testset_name}

        # Paper results
        if paper_results:
            row['num_matches'] = paper_results.get('num_matches', 'N/A')
            row['MEHDIE_orthographic_f5'] = paper_results.get('orthographic', {}).get('f5', 'N/A')
            row['MEHDIE_phonetic_f5'] = paper_results.get('phonetic', {}).get('f5', 'N/A')
            orth_f5 = paper_results.get('orthographic', {}).get('f5', 0)
            phon_f5 = paper_results.get('phonetic', {}).get('f5', 0)
            row['MEHDIE_best_f5'] = max(orth_f5, phon_f5)

        # Our results (best F-5 across thresholds)
        for method_name in methods.keys():
            if testset_name in all_results.get(method_name, {}):
                best = max(all_results[method_name][testset_name], key=lambda r: r.f5)
                row[f'{method_name}_f5'] = best.f5
                row[f'{method_name}_threshold'] = best.threshold
                row[f'{method_name}_precision'] = best.precision
                row[f'{method_name}_recall'] = best.recall

        comparison_data.append(row)

    # Print detailed comparison
    our_method = 'Symphonym' if 'Symphonym' in methods else 'Jaro-Winkler'
    print(f"{'Testset':<30} {'GT':>4} {'MEHDIE-O':>10} {'MEHDIE-P':>10} {our_method:>12} {'Δ':>8}")
    print("-" * 80)

    deltas = []
    for row in comparison_data:
        testset = row['testset'].replace('testset', 'TS').split('-')[0]
        num_matches = row.get('num_matches', '?')
        mehdie_orth = row.get('MEHDIE_orthographic_f5', 'N/A')
        mehdie_phon = row.get('MEHDIE_phonetic_f5', 'N/A')
        mehdie_best = row.get('MEHDIE_best_f5', 'N/A')
        our_f5 = row.get(f'{our_method}_f5', 'N/A')

        if isinstance(mehdie_best, float) and isinstance(our_f5, float):
            delta = our_f5 - mehdie_best
            deltas.append(delta)
            delta_str = f"{delta:+.3f}"
        else:
            delta_str = "N/A"

        mehdie_orth_str = f"{mehdie_orth:.2f}" if isinstance(mehdie_orth, float) else str(mehdie_orth)
        mehdie_phon_str = f"{mehdie_phon:.2f}" if isinstance(mehdie_phon, float) else str(mehdie_phon)
        our_str = f"{our_f5:.3f}" if isinstance(our_f5, float) else str(our_f5)

        print(f"{testset:<30} {num_matches:>4} {mehdie_orth_str:>10} {mehdie_phon_str:>10} {our_str:>12} {delta_str:>8}")

    if deltas:
        avg_delta = np.mean(deltas)
        print("-" * 80)
        print(f"{'AVERAGE DELTA vs MEHDIE best':<56} {avg_delta:+.3f}")

    # Print per-testset detail
    print("\n" + "=" * 80)
    print("DETAILED RESULTS BY TESTSET")
    print("=" * 80)

    for row in comparison_data:
        testset = row['testset']
        print(f"\n{testset}")
        print(f"  Ground truth matches: {row.get('num_matches', '?')}")
        print(f"  MEHDIE orthographic F-5: {row.get('MEHDIE_orthographic_f5', 'N/A')}")
        print(f"  MEHDIE phonetic F-5:     {row.get('MEHDIE_phonetic_f5', 'N/A')}")

        for method in methods.keys():
            f5 = row.get(f'{method}_f5')
            thresh = row.get(f'{method}_threshold')
            prec = row.get(f'{method}_precision')
            rec = row.get(f'{method}_recall')
            if f5 is not None:
                print(f"  {method}: F5={f5:.3f} (θ={thresh}, P={prec:.3f}, R={rec:.3f})")

    # Save results if output path provided
    if output_path:
        output_data = {
            'timestamp': datetime.now().isoformat(),
            'testsets_dir': testsets_dir,
            'checkpoint_path': checkpoint_path,
            'thresholds': thresholds,
            'primary_metric': 'F-5 (recall weighted 5x)',
            'mehdie_paper_results': MEHDIE_PAPER_RESULTS,
            'results': {},
            'comparison': comparison_data,
        }

        for method_name, method_results in all_results.items():
            output_data['results'][method_name] = {}
            for testset_name, testset_results in method_results.items():
                output_data['results'][method_name][testset_name] = [
                    {
                        'threshold': r.threshold,
                        'precision': r.precision,
                        'recall': r.recall,
                        'f1': r.f1,
                        'f5': r.f5,
                        'tp': r.true_positives,
                        'fp': r.false_positives,
                        'fn': r.false_negatives,
                    }
                    for r in testset_results
                ]

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(output_data, f, indent=2)
        print(f"\nResults saved to: {output_path}")

    return all_results


def print_f5_comparison(benchmark, all_results):
    """Print comparison table using F-5 as primary metric."""
    print("\n" + "=" * 100)
    print("METHOD COMPARISON (Best F-5 per testset)")
    print("=" * 100)

    methods = list(all_results.keys())
    testsets = list(next(iter(all_results.values())).keys())

    # Header
    header = f"{'Testset':<40}"
    for method in methods:
        header += f" {method:>15}"
    print(header)
    print("-" * 100)

    # Rows
    for testset in testsets:
        row = f"{testset.split('-')[0]:<40}"
        for method in methods:
            best = max(all_results[method][testset], key=lambda r: r.f5)
            row += f" {best.f5:>15.3f}"
        print(row)

    # Average
    print("-" * 100)
    row = f"{'AVERAGE':<40}"
    for method in methods:
        avg_f5 = np.mean([
            max(all_results[method][ts], key=lambda r: r.f5).f5
            for ts in testsets
        ])
        row += f" {avg_f5:>15.3f}"
    print(row)


def main():
    # Determine default paths based on latest version
    checkpoints_base = '/ix1/whcdh/models/phonetic/checkpoints'
    data_base = '/ix1/whcdh/models/phonetic/data'

    parser = argparse.ArgumentParser(
        description='Run MEHDIE benchmark evaluation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run with auto-detected latest model
    python -m testing.run_mehdie_evaluation --testsets testing/mehdie-testsets

    # Run with specific version
    python -m testing.run_mehdie_evaluation --testsets testing/mehdie-testsets --version v3

    # Run baselines only
    python -m testing.run_mehdie_evaluation --testsets testing/mehdie-testsets --baselines-only

    # Save results to file
    python -m testing.run_mehdie_evaluation --testsets testing/mehdie-testsets --output results/mehdie.json
        """
    )

    parser.add_argument(
        '--testsets', required=True,
        help='Path to MEHDIE testsets directory'
    )
    parser.add_argument(
        '--version',
        help='Model version to use (e.g., v3). If not specified, uses latest.'
    )
    parser.add_argument(
        '--checkpoint',
        help='Path to model checkpoint (overrides --version)'
    )
    parser.add_argument(
        '--vocab-dir',
        help='Path to vocab directory (overrides --version)'
    )
    parser.add_argument(
        '--output',
        help='Path to save results JSON'
    )
    parser.add_argument(
        '--thresholds', type=float, nargs='+',
        default=[0.7, 0.8, 0.85, 0.9, 0.95],
        help='Similarity thresholds to evaluate'
    )
    parser.add_argument(
        '--device', default='cuda',
        help='Device for model inference (cuda/cpu)'
    )
    parser.add_argument(
        '--baselines-only', action='store_true',
        help='Run only baseline methods (no neural model)'
    )

    args = parser.parse_args()

    # Check CUDA availability
    if args.device == 'cuda' and not torch.cuda.is_available():
        print("CUDA not available, falling back to CPU")
        args.device = 'cpu'

    # Determine checkpoint and vocab paths
    if args.checkpoint:
        checkpoint_path = args.checkpoint
        vocab_dir = args.vocab_dir or str(Path(args.checkpoint).parent.parent / 'data' / Path(args.checkpoint).parent.name / 'vocab')
    elif not args.baselines_only:
        version = args.version or get_latest_version(checkpoints_base)
        checkpoint_path = f'{checkpoints_base}/{version}/phase3_best.pt'
        vocab_dir = f'{data_base}/{version}/vocab'

        # Check if checkpoint exists
        if not Path(checkpoint_path).exists():
            print(f"WARNING: Checkpoint not found at {checkpoint_path}")
            print("Falling back to baselines only.")
            args.baselines_only = True
            checkpoint_path = None
            vocab_dir = None
    else:
        checkpoint_path = None
        vocab_dir = None

    run_evaluation(
        testsets_dir=args.testsets,
        checkpoint_path=checkpoint_path,
        vocab_dir=vocab_dir,
        output_path=args.output,
        thresholds=args.thresholds,
        device=args.device,
        baselines_only=args.baselines_only,
    )


if __name__ == '__main__':
    main()

