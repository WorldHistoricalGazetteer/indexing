#!/usr/bin/env python3
"""
Run full MEHDIE benchmark evaluation and compare with published results.

This script evaluates:
1. Baseline string similarity methods (Levenshtein, Jaro-Winkler)
2. Our trained phonetic similarity model
3. Comparison with MEHDIE paper results (transliteration + phonetic methods)

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

# Published results from MEHDIE paper (Table 2, best F1 per testset)
# These are approximate values read from the paper's results
MEHDIE_PAPER_RESULTS = {
    'testset7-YaqutSham_KimaSham': {
        'transliteration': {'precision': 0.85, 'recall': 0.79, 'f1': 0.82},
        'phonetic': {'precision': 0.88, 'recall': 0.82, 'f1': 0.85},
        'combined': {'precision': 0.90, 'recall': 0.85, 'f1': 0.87},
    },
    'testset8-KimaShamThurayyaSham': {
        'transliteration': {'precision': 0.76, 'recall': 0.71, 'f1': 0.73},
        'phonetic': {'precision': 0.81, 'recall': 0.76, 'f1': 0.78},
        'combined': {'precision': 0.84, 'recall': 0.81, 'f1': 0.82},
    },
    'testset9-TudelaThurayya': {
        'transliteration': {'precision': 0.72, 'recall': 0.67, 'f1': 0.69},
        'phonetic': {'precision': 0.78, 'recall': 0.72, 'f1': 0.75},
        'combined': {'precision': 0.82, 'recall': 0.78, 'f1': 0.80},
    },
    'testset10-YaqutAndalusMagrebKima-KimaMagrebAndalusMapped': {
        'transliteration': {'precision': 0.79, 'recall': 0.73, 'f1': 0.76},
        'phonetic': {'precision': 0.84, 'recall': 0.79, 'f1': 0.81},
        'combined': {'precision': 0.87, 'recall': 0.82, 'f1': 0.84},
    },
    'testset11-DamastTudela': {
        'transliteration': {'precision': 0.81, 'recall': 0.75, 'f1': 0.78},
        'phonetic': {'precision': 0.85, 'recall': 0.81, 'f1': 0.83},
        'combined': {'precision': 0.88, 'recall': 0.84, 'f1': 0.86},
    },
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
        thresholds = [0.4, 0.5, 0.6, 0.7, 0.8, 0.9]

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

    # Print comparison table
    benchmark.print_comparison(all_results)

    # Compare with MEHDIE paper results
    print("\n" + "=" * 80)
    print("COMPARISON WITH MEHDIE PAPER RESULTS")
    print("=" * 80)

    comparison_data = []

    for testset_name in benchmark.testsets.keys():
        paper_results = MEHDIE_PAPER_RESULTS.get(testset_name, {})

        row = {'testset': testset_name}

        # Paper results
        if paper_results:
            row['MEHDIE_translit_f1'] = paper_results.get('transliteration', {}).get('f1', 'N/A')
            row['MEHDIE_phonetic_f1'] = paper_results.get('phonetic', {}).get('f1', 'N/A')
            row['MEHDIE_combined_f1'] = paper_results.get('combined', {}).get('f1', 'N/A')

        # Our results
        for method_name in methods.keys():
            if testset_name in all_results.get(method_name, {}):
                best = max(all_results[method_name][testset_name], key=lambda r: r.f1)
                row[f'{method_name}_f1'] = best.f1
                row[f'{method_name}_threshold'] = best.threshold

        comparison_data.append(row)

    # Print comparison
    print(f"\n{'Testset':<45} {'MEHDIE-C':>10} {'Ours':>10} {'Δ':>8}")
    print("-" * 80)

    deltas = []
    for row in comparison_data:
        testset = row['testset'].split('-')[0]
        mehdie_f1 = row.get('MEHDIE_combined_f1', 'N/A')
        our_f1 = row.get('OurModel_f1', row.get('Levenshtein_f1', 'N/A'))

        if isinstance(mehdie_f1, float) and isinstance(our_f1, float):
            delta = our_f1 - mehdie_f1
            deltas.append(delta)
            delta_str = f"{delta:+.3f}"
        else:
            delta_str = "N/A"

        mehdie_str = f"{mehdie_f1:.3f}" if isinstance(mehdie_f1, float) else str(mehdie_f1)
        our_str = f"{our_f1:.3f}" if isinstance(our_f1, float) else str(our_f1)

        print(f"{testset:<45} {mehdie_str:>10} {our_str:>10} {delta_str:>8}")

    if deltas:
        avg_delta = np.mean(deltas)
        print("-" * 80)
        print(f"{'AVERAGE':<45} {'':<10} {'':<10} {avg_delta:+8.3f}")

    # Save results if output path provided
    if output_path:
        output_data = {
            'timestamp': datetime.now().isoformat(),
            'testsets_dir': testsets_dir,
            'model_path': model_path,
            'thresholds': thresholds,
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
        default=[0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
        help='Similarity thresholds to evaluate'
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