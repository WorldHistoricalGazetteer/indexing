#!/usr/bin/env python3
"""
Run full MEHDIE benchmark evaluation and compare with published results.

This script evaluates:
1. Baseline string similarity methods (Levenshtein, Jaro-Winkler)
2. Our trained phonetic similarity model
3. Comparison with MEHDIE paper results (orthographic + phonetic methods)

Reference:
    Sagi et al. (2025) "Utilizing phonetic similarity for cross-source and
    cross-language toponym matching: a benchmark and prototype"
    Language Resources and Evaluation, 59:2427-2451

Usage:
    python run_mehdie_evaluation.py \
        --testsets /path/to/mehdie-testsets \
        --model /path/to/phonetic_phase3.pt \
        --output results/mehdie_evaluation.json
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
    create_model_similarity_fn,
    levenshtein_similarity,
    jaro_winkler_similarity,
)

# =============================================================================
# Published results from MEHDIE paper (Sagi et al., 2025)
# Values extracted from Figures 2-4, Section 5.1.2
# =============================================================================
#
# IMPORTANT: The paper uses F-5 as primary metric (recall 5x more important
# than precision). This reflects their user preference for high recall with
# tolerance for low precision.
#
# From Figure 4 (best F-5 by dataset pair and method, threshold 0.9):
# - Phonetic method uses Hamming feature distance over IPA representations
# - Orthographic method uses Jaro distance over transliterated variants
#
# Note: The paper reports that BOTH methods should be used in combination
# for best results, as they capture different matches.

MEHDIE_PAPER_RESULTS = {
    # Pairing 2 in paper: Yaqut Al-Sham (687) ↔ Kima Al-Sham (1899), 30 matches
    'testset7-YaqutSham_KimaSham': {
        'num_matches': 30,
        'orthographic': {'f5': 0.67, 'threshold': 0.9},
        'phonetic': {'f5': 0.77, 'threshold': 0.9},
    },
    # Pairing 3 in paper: Kima Al-Sham (1899) ↔ Thuraya Al-Sham (291), 21 matches
    'testset8-KimaShamThurayyaSham': {
        'num_matches': 21,
        'orthographic': {'f5': 0.65, 'threshold': 0.9},
        'phonetic': {'f5': 0.68, 'threshold': 0.9},
    },
    # Pairing 4 in paper: Tudela (306) ↔ Althurayya (2241), 18 matches
    'testset9-TudelaThurayya': {
        'num_matches': 18,
        'orthographic': {'f5': 0.92, 'threshold': 0.9},  # Orthographic better here
        'phonetic': {'f5': 0.70, 'threshold': 0.9},
    },
    # Pairing 1 in paper: Yaqut Andalus/Magreb (484) ↔ Kima Andalus/Magreb (559), 28 matches
    'testset10-YaqutAndalusMagrebKima-KimaMagrebAndalusMapped': {
        'num_matches': 28,
        'orthographic': {'f5': 0.75, 'threshold': 0.9},
        'phonetic': {'f5': 0.77, 'threshold': 0.9},
    },
    # Pairing 5 in paper: Damast (447) ↔ Tudela (306), 32 matches
    'testset11-DamastTudela': {
        'num_matches': 32,
        'orthographic': {'f5': 0.78, 'threshold': 0.9},
        'phonetic': {'f5': 0.88, 'threshold': 0.9},  # Best phonetic result
    },
}

# Average results from Figure 2 (across all dataset pairs)
MEHDIE_AVERAGE_RESULTS = {
    'phonetic_0.95': {'precision': 0.53, 'recall': 0.10, 'f1': 0.17, 'f5': 0.11},
    'phonetic_0.9': {'precision': 0.08, 'recall': 0.55, 'f1': 0.14, 'f5': 0.32},
    'phonetic_0.85': {'precision': 0.04, 'recall': 0.85, 'f1': 0.08, 'f5': 0.58},
    'orthographic_0.9': {'precision': 0.32, 'recall': 0.22, 'f1': 0.26, 'f5': 0.22},
    'orthographic_0.8': {'precision': 0.15, 'recall': 0.36, 'f1': 0.21, 'f5': 0.30},
    'orthographic_0.7': {'precision': 0.08, 'recall': 0.50, 'f1': 0.14, 'f5': 0.32},
}


def load_model(model_path: str, device: str = 'cuda'):
    """Load trained model and vocabularies."""
    from phonetics.models import HybridPhoneticModel, PhoneticEncoder, CharEncoder
    from phonetics.vocab import CharVocab, LangVocab

    checkpoint = torch.load(model_path, map_location=device)

    # Load vocabularies
    vocab_dir = Path(model_path).parent
    base_name = Path(model_path).stem

    char_vocab_path = vocab_dir / f'{base_name}_char_vocab.pkl'
    lang_vocab_path = vocab_dir / f'{base_name}_lang_vocab.pkl'

    if not char_vocab_path.exists() or not lang_vocab_path.exists():
        raise FileNotFoundError(
            f"Vocabulary files not found. Expected:\n"
            f"  {char_vocab_path}\n"
            f"  {lang_vocab_path}"
        )

    char_vocab = CharVocab.load(char_vocab_path)
    lang_vocab = LangVocab.load(lang_vocab_path)

    # Reconstruct model
    phonetic_encoder = PhoneticEncoder()
    char_encoder = CharEncoder(
        vocab_size=checkpoint['char_vocab_size'],
        num_langs=checkpoint['num_langs']
    )
    model = HybridPhoneticModel(phonetic_encoder, char_encoder)
    model.load_state_dict(checkpoint['model_state'])
    model = model.to(device)
    model.eval()

    return model, char_vocab, lang_vocab


def run_evaluation(
        testsets_dir: str,
        model_path: str = None,
        output_path: str = None,
        thresholds: list = None,
        device: str = 'cuda'
):
    """Run full MEHDIE benchmark evaluation."""

    if thresholds is None:
        # Match paper's threshold range
        thresholds = [0.7, 0.8, 0.85, 0.9, 0.95]

    print("=" * 80)
    print("MEHDIE BENCHMARK EVALUATION")
    print("=" * 80)
    print(f"Testsets directory: {testsets_dir}")
    print(f"Model: {model_path or 'None (baselines only)'}")
    print(f"Thresholds: {thresholds}")
    print(f"Device: {device}")
    print()

    # Load benchmark
    benchmark = MEHDIEBenchmark(testsets_dir)

    if not benchmark.testsets:
        print("ERROR: No testsets loaded. Check testsets directory.")
        return None

    # Define methods to evaluate
    methods = {
        'Levenshtein': levenshtein_similarity,
        'Jaro-Winkler': jaro_winkler_similarity,
    }

    # Add trained model if provided
    if model_path:
        print(f"\nLoading model from {model_path}...")
        model, char_vocab, lang_vocab = load_model(model_path, device)
        methods['OurModel'] = create_model_similarity_fn(
            model, char_vocab, lang_vocab, device
        )
        print("Model loaded successfully.")

    # Run evaluation
    print("\n" + "=" * 80)
    print("RUNNING EVALUATION")
    print("=" * 80)

    all_results = benchmark.compare_methods(methods, thresholds)

    # Print comparison table (now using F-5 as primary metric)
    print_f5_comparison(benchmark, all_results)

    # Compare with MEHDIE paper results
    print("\n" + "=" * 80)
    print("COMPARISON WITH MEHDIE PAPER RESULTS (F-5 metric)")
    print("=" * 80)
    print("\nNote: Paper uses F-5 which weights recall 5x more than precision.")
    print("This reflects user preference for finding all matches over precision.\n")

    comparison_data = []

    for testset_name in benchmark.testsets.keys():
        paper_results = MEHDIE_PAPER_RESULTS.get(testset_name, {})

        row = {'testset': testset_name}

        # Paper results
        if paper_results:
            row['num_matches'] = paper_results.get('num_matches', 'N/A')
            row['MEHDIE_orthographic_f5'] = paper_results.get('orthographic', {}).get('f5', 'N/A')
            row['MEHDIE_phonetic_f5'] = paper_results.get('phonetic', {}).get('f5', 'N/A')
            # Best of the two methods (paper recommends using both)
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
    print(f"{'Testset':<30} {'GT':>4} {'MEHDIE-O':>10} {'MEHDIE-P':>10} {'Ours':>10} {'Δ':>8}")
    print("-" * 80)

    deltas = []
    for row in comparison_data:
        testset = row['testset'].replace('testset', 'TS').split('-')[0]
        num_matches = row.get('num_matches', '?')
        mehdie_orth = row.get('MEHDIE_orthographic_f5', 'N/A')
        mehdie_phon = row.get('MEHDIE_phonetic_f5', 'N/A')
        mehdie_best = row.get('MEHDIE_best_f5', 'N/A')
        our_f5 = row.get('OurModel_f5', row.get('Jaro-Winkler_f5', 'N/A'))

        if isinstance(mehdie_best, float) and isinstance(our_f5, float):
            delta = our_f5 - mehdie_best
            deltas.append(delta)
            delta_str = f"{delta:+.3f}"
        else:
            delta_str = "N/A"

        mehdie_orth_str = f"{mehdie_orth:.2f}" if isinstance(mehdie_orth, float) else str(mehdie_orth)
        mehdie_phon_str = f"{mehdie_phon:.2f}" if isinstance(mehdie_phon, float) else str(mehdie_phon)
        our_str = f"{our_f5:.3f}" if isinstance(our_f5, float) else str(our_f5)

        print(
            f"{testset:<30} {num_matches:>4} {mehdie_orth_str:>10} {mehdie_phon_str:>10} {our_str:>10} {delta_str:>8}")

    if deltas:
        avg_delta = np.mean(deltas)
        print("-" * 80)
        print(f"{'AVERAGE DELTA vs MEHDIE best':<30} {'':<4} {'':<10} {'':<10} {'':<10} {avg_delta:+8.3f}")

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
            'model_path': model_path,
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
    parser = argparse.ArgumentParser(
        description='Run MEHDIE benchmark evaluation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run baselines only
    python run_mehdie_evaluation.py --testsets /path/to/mehdie-testsets

    # Run with trained model
    python run_mehdie_evaluation.py \\
        --testsets /path/to/mehdie-testsets \\
        --model /path/to/phonetic_phase3.pt \\
        --output results/evaluation.json

    # Run with CPU (for baselines or if no GPU available)
    python run_mehdie_evaluation.py \\
        --testsets /path/to/mehdie-testsets \\
        --device cpu
        """
    )

    parser.add_argument(
        '--testsets', required=True,
        help='Path to MEHDIE testsets directory'
    )
    parser.add_argument(
        '--model',
        help='Path to trained model checkpoint (Phase 3 recommended)'
    )
    parser.add_argument(
        '--output',
        help='Path to save results JSON'
    )
    parser.add_argument(
        '--thresholds', type=float, nargs='+',
        default=[0.7, 0.8, 0.85, 0.9, 0.95],
        help='Similarity thresholds to evaluate (default matches paper range)'
    )
    parser.add_argument(
        '--device', default='cuda',
        help='Device for model inference (cuda/cpu)'
    )

    args = parser.parse_args()

    # Check CUDA availability
    if args.device == 'cuda' and not torch.cuda.is_available():
        print("CUDA not available, falling back to CPU")
        args.device = 'cpu'

    run_evaluation(
        testsets_dir=args.testsets,
        model_path=args.model,
        output_path=args.output,
        thresholds=args.thresholds,
        device=args.device
    )


if __name__ == '__main__':
    main()