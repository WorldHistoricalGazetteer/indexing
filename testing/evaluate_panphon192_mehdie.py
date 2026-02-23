#!/usr/bin/env python3
"""
PanPhon192 Baseline Evaluation on MEHDIE Benchmark.

Computes PanPhon192 cosine similarity as a baseline, producing Recall@K and MRR
metrics comparable to the existing Levenshtein, Jaro-Winkler, and Symphonym
results in the paper.

PanPhon192 is the raw articulatory feature representation used as *input* to
Symphonym's Teacher network. Comparing it against the full trained Symphonym
system demonstrates the value added by neural training beyond the phonetic
features themselves.

Usage:
    python -m testing.evaluate_panphon192_mehdie
    python -m testing.evaluate_panphon192_mehdie --coverage-only
    python -m testing.evaluate_panphon192_mehdie --output testing/results/panphon192_mehdie_results.json
"""

import argparse
import csv
import json
import sys
import unicodedata
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# Suppress noisy warnings before imports
warnings.filterwarnings("ignore", category=UserWarning, module='epitran')
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
warnings.filterwarnings("ignore", message=".*tokenizer class.*")
warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent.parent))

from testing.mehdie_benchmark import MEHDIEBenchmark

# ============================================================================
# Script detection for MEHDIE toponyms
# ============================================================================

def detect_script_mehdie(text: str) -> str:
    """Detect whether a MEHDIE toponym is Arabic, Hebrew, or Latin."""
    ar_count = he_count = la_count = 0
    for ch in text:
        if ch.isalpha():
            name = unicodedata.name(ch, '')
            if 'ARABIC' in name:
                ar_count += 1
            elif 'HEBREW' in name:
                he_count += 1
            elif 'LATIN' in name:
                la_count += 1
    if ar_count > he_count and ar_count > la_count:
        return 'ar'
    elif he_count > ar_count and he_count > la_count:
        return 'he'
    elif la_count > 0:
        return 'la'
    return 'unknown'


# ============================================================================
# PanPhon192 computation — reuses the exact logic from
# phonetics/extraction/rebuild_toponyms_index.py IPAConverter.to_embedding()
# ============================================================================

class PanPhon192Computer:
    """
    Computes PanPhon192 vectors: name → IPA → PanPhon features → 8-bin pooling → 192-dim.

    G2P routing:
      - Arabic (ar) → Epitran (ara-Arab)
      - Hebrew (he) → Phonikud
    """

    def __init__(self):
        self._ft = None
        self._epitran_cache = {}
        self._phonikud = None
        self._init_panphon()

    def _init_panphon(self):
        import panphon
        self._ft = panphon.FeatureTable()
        print(f"PanPhon FeatureTable loaded")

    def _get_epitran(self, code: str):
        if code not in self._epitran_cache:
            try:
                import epitran
                self._epitran_cache[code] = epitran.Epitran(code)
            except Exception as e:
                print(f"  Warning: Failed to load Epitran({code}): {e}")
                self._epitran_cache[code] = None
        return self._epitran_cache[code]

    def _get_phonikud(self):
        if self._phonikud is None:
            try:
                import phonikud as phonikud_module
                self._phonikud = phonikud_module
                print(f"Phonikud loaded for Hebrew G2P")
            except ImportError:
                print("  Warning: Phonikud not available. Hebrew IPA will fail.")
                self._phonikud = False
        return self._phonikud if self._phonikud is not False else None

    def to_ipa(self, text: str, lang: str) -> Optional[str]:
        """Convert text to IPA using the appropriate G2P backend."""
        if lang == 'he':
            phonikud = self._get_phonikud()
            if phonikud:
                try:
                    ipa = phonikud.phonemize(text)
                    return ipa if ipa and ipa.strip() else None
                except Exception:
                    return None
            return None

        if lang == 'ar':
            epi = self._get_epitran('ara-Arab')
            if epi:
                try:
                    ipa = epi.transliterate(text)
                    return ipa if ipa and ipa.strip() else None
                except Exception:
                    return None
            return None

        if lang == 'la':
            # Romanised variants in MEHDIE datasets (e.g. "Rekem", "Jaljulya")
            # are scholarly transliterations. Route through English Epitran as
            # a reasonable phonetic approximation — these follow broadly English
            # grapheme-phoneme conventions.
            epi = self._get_epitran('eng-Latn')
            if epi:
                try:
                    ipa = epi.transliterate(text)
                    return ipa if ipa and ipa.strip() else None
                except Exception:
                    return None
            return None

        # Fallback: unsupported language
        return None

    def to_embedding(self, ipa: str) -> Optional[np.ndarray]:
        """
        Compute 192-dimensional PanPhon embedding using 8-bin position pooling.

        Exact replication of IPAConverter.to_embedding() from
        phonetics/extraction/rebuild_toponyms_index.py.
        """
        if not ipa:
            return None
        try:
            segments = self._ft.word_fts(ipa)
            if not segments:
                return None

            num_segments = len(segments)
            num_bins = 8
            features_per_bin = 24

            # Initialize bins: 8 bins × 24 features each
            bins = [[0.0] * features_per_bin for _ in range(num_bins)]
            bin_counts = [0] * num_bins

            # Assign each segment to a bin based on position
            for seg_idx, seg in enumerate(segments):
                position = seg_idx / num_segments
                bin_idx = min(int(position * num_bins), num_bins - 1)

                features = seg.numeric()
                for i, val in enumerate(features):
                    bins[bin_idx][i] += val
                bin_counts[bin_idx] += 1

            # Compute mean for each bin (zero-padded bins stay zero)
            embedding = []
            for bin_idx in range(num_bins):
                if bin_counts[bin_idx] > 0:
                    bin_avg = [v / bin_counts[bin_idx] for v in bins[bin_idx]]
                else:
                    bin_avg = [0.0] * features_per_bin
                embedding.extend(bin_avg)

            vec = np.array(embedding, dtype=np.float32)

            # Reject zero vectors (no phonetic content)
            if np.linalg.norm(vec) == 0:
                return None

            # L2 normalise for cosine similarity via dot product
            vec = vec / np.linalg.norm(vec)
            return vec

        except Exception:
            return None

    def compute(self, text: str, lang: str) -> Tuple[Optional[str], Optional[np.ndarray]]:
        """Full pipeline: text → IPA → PanPhon192 vector. Returns (ipa, vec)."""
        ipa = self.to_ipa(text, lang)
        if ipa is None:
            return None, None
        vec = self.to_embedding(ipa)
        return ipa, vec


# ============================================================================
# Coverage analysis
# ============================================================================

def analyze_coverage(benchmark: MEHDIEBenchmark, computer: PanPhon192Computer):
    """
    Check G2P coverage on all MEHDIE testset toponyms.
    Returns per-name results and per-testset stats.
    """
    # name -> {lang, ipa, vec, success}
    name_results: Dict[str, dict] = {}
    # testset -> {d1_total, d1_ok, d2_total, d2_ok, failures: [...]}
    testset_stats = {}

    for ts_name, testset in benchmark.testsets.items():
        stats = {
            'd1_total': 0, 'd1_ok': 0,
            'd2_total': 0, 'd2_ok': 0,
            'failures': []
        }

        for side, dataset, side_label in [
            ('d1', testset.dataset1, 'dataset1'),
            ('d2', testset.dataset2, 'dataset2'),
        ]:
            for rec_id, record in dataset.items():
                for name in record['all_names']:
                    if not name:
                        continue

                    stats[f'{side}_total'] += 1

                    if name in name_results:
                        if name_results[name]['success']:
                            stats[f'{side}_ok'] += 1
                        else:
                            stats['failures'].append({
                                'name': name, 'side': side_label,
                                'record_id': rec_id, 'lang': name_results[name]['lang']
                            })
                        continue

                    lang = detect_script_mehdie(name)
                    ipa, vec = computer.compute(name, lang)
                    success = vec is not None

                    name_results[name] = {
                        'lang': lang, 'ipa': ipa,
                        'vec': vec, 'success': success
                    }

                    if success:
                        stats[f'{side}_ok'] += 1
                    else:
                        stats['failures'].append({
                            'name': name, 'side': side_label,
                            'record_id': rec_id, 'lang': lang
                        })

        testset_stats[ts_name] = stats

    return name_results, testset_stats


def print_coverage_report(testset_stats: dict, benchmark: MEHDIEBenchmark, name_results: Dict[str, dict]):
    """Print formatted coverage report."""
    print("\n" + "=" * 80)
    print("G2P COVERAGE REPORT FOR MEHDIE TESTSETS")
    print("=" * 80)

    total_names = 0
    total_ok = 0

    for ts_name, stats in testset_stats.items():
        short = ts_name.replace('testset', 'TS').split('-')[0]
        d1_pct = stats['d1_ok'] / stats['d1_total'] * 100 if stats['d1_total'] else 0
        d2_pct = stats['d2_ok'] / stats['d2_total'] * 100 if stats['d2_total'] else 0
        total_names += stats['d1_total'] + stats['d2_total']
        total_ok += stats['d1_ok'] + stats['d2_ok']

        print(f"\n{short} ({ts_name}):")
        print(f"  Dataset 1: {stats['d1_ok']}/{stats['d1_total']} names with IPA ({d1_pct:.1f}%)")
        print(f"  Dataset 2: {stats['d2_ok']}/{stats['d2_total']} names with IPA ({d2_pct:.1f}%)")

        # Ground truth pair coverage
        testset = benchmark.testsets[ts_name]
        gt_both_ok = 0
        gt_q_fail = 0
        gt_c_fail = 0
        for id1, id2 in testset.ground_truth:
            rec1 = testset.dataset1.get(id1, {})
            rec2 = testset.dataset2.get(id2, {})
            any_q = any(
                name_results.get(n, {}).get('success', False)
                for n in rec1.get('all_names', []) if n
            )
            any_c = any(
                name_results.get(n, {}).get('success', False)
                for n in rec2.get('all_names', []) if n
            )
            if any_q and any_c:
                gt_both_ok += 1
            elif not any_q:
                gt_q_fail += 1
            else:
                gt_c_fail += 1
        gt_total = len(testset.ground_truth)
        gt_pct = gt_both_ok / gt_total * 100 if gt_total else 0
        print(f"  Ground truth pairs: {gt_both_ok}/{gt_total} evaluable ({gt_pct:.1f}%)"
              f" [query fail: {gt_q_fail}, candidate fail: {gt_c_fail}]")

        if stats['failures']:
            # Group failures by language
            by_lang = defaultdict(list)
            for f in stats['failures']:
                by_lang[f['lang']].append(f['name'])
            print(f"  Failures by language:")
            for lang, names in sorted(by_lang.items()):
                unique_names = list(set(names))
                print(f"    {lang}: {len(unique_names)} unique names")
                for n in unique_names[:5]:
                    print(f"      - {n!r}")
                if len(unique_names) > 5:
                    print(f"      ... and {len(unique_names)-5} more")

    overall_pct = total_ok / total_names * 100 if total_names else 0
    print(f"\n{'='*80}")
    print(f"OVERALL: {total_ok}/{total_names} names with IPA ({overall_pct:.1f}%)")
    print(f"{'='*80}")


# ============================================================================
# Ranking evaluation — mirrors MEHDIEBenchmark.evaluate_ranking() exactly
# ============================================================================

def evaluate_panphon192_ranking(
    benchmark: MEHDIEBenchmark,
    name_results: Dict[str, dict],
) -> Dict[str, dict]:
    """
    Evaluate PanPhon192 using the same ranking protocol as Symphonym.

    For each ground truth pair (id1, id2), rank all dataset2 records by
    max PanPhon192 cosine similarity across name pairs, then compute
    Recall@K and MRR.

    This uses the slow-path ranking logic from MEHDIEBenchmark._compute_rankings_slow()
    but with vectorised cosine similarity instead of a scalar function.
    """
    results = {}

    for ts_name, testset in benchmark.testsets.items():
        print(f"\nEvaluating {ts_name} (PanPhon192 ranking)...")

        # Compute rankings for each query record
        query_rankings = {}

        for id1, record1 in testset.dataset1.items():
            scores = []

            # Get valid vectors for query names
            q_vecs = []
            for name in record1['all_names']:
                if name and name in name_results and name_results[name]['success']:
                    q_vecs.append(name_results[name]['vec'])

            if not q_vecs:
                continue

            q_matrix = np.stack(q_vecs)  # (num_q, 192)

            for id2, record2 in testset.dataset2.items():
                # Get valid vectors for candidate names
                c_vecs = []
                for name in record2['all_names']:
                    if name and name in name_results and name_results[name]['success']:
                        c_vecs.append(name_results[name]['vec'])

                if not c_vecs:
                    # No valid IPA for any candidate name — assign minimum similarity
                    scores.append((id2, -1.0))
                    continue

                c_matrix = np.stack(c_vecs)  # (num_c, 192)

                # Cosine similarity matrix (already L2-normalised, so dot = cosine)
                sim_matrix = q_matrix @ c_matrix.T  # (num_q, num_c)
                max_sim = float(sim_matrix.max())
                scores.append((id2, max_sim))

            # Sort by score descending
            scores.sort(key=lambda x: -x[1])
            query_rankings[id1] = scores

        # Compute metrics — same logic as MEHDIEBenchmark.evaluate_ranking()
        ranks = []
        reciprocal_ranks = []

        gt_by_query = {}
        for id1, id2 in testset.ground_truth:
            if id1 not in gt_by_query:
                gt_by_query[id1] = set()
            gt_by_query[id1].add(id2)

        excluded = 0
        for id1, correct_ids in gt_by_query.items():
            if id1 not in query_rankings:
                excluded += 1
                continue

            ranking = query_rankings[id1]

            for rank, (id2, score) in enumerate(ranking, start=1):
                if id2 in correct_ids:
                    ranks.append(rank)
                    reciprocal_ranks.append(1.0 / rank)
                    break
            else:
                ranks.append(len(ranking) + 1)
                reciprocal_ranks.append(0.0)

        if ranks:
            recall_at_1 = sum(1 for r in ranks if r <= 1) / len(ranks)
            recall_at_5 = sum(1 for r in ranks if r <= 5) / len(ranks)
            recall_at_10 = sum(1 for r in ranks if r <= 10) / len(ranks)
            recall_at_20 = sum(1 for r in ranks if r <= 20) / len(ranks)
            mrr = float(np.mean(reciprocal_ranks))
            mean_rank = float(np.mean(ranks))
        else:
            recall_at_1 = recall_at_5 = recall_at_10 = recall_at_20 = 0.0
            mrr = 0.0
            mean_rank = float('inf')

        results[ts_name] = {
            'recall_at_1': recall_at_1,
            'recall_at_5': recall_at_5,
            'recall_at_10': recall_at_10,
            'recall_at_20': recall_at_20,
            'mrr': mrr,
            'mean_rank': mean_rank,
            'num_queries': len(gt_by_query),
            'num_evaluated': len(ranks),
            'num_excluded': excluded,
            'corpus_size': len(testset.dataset2),
        }

        print(f"  Queries: {len(gt_by_query)} (excluded: {excluded})")
        print(f"  R@1:  {recall_at_1:.3f}  R@5:  {recall_at_5:.3f}  "
              f"R@10: {recall_at_10:.3f}  R@20: {recall_at_20:.3f}")
        print(f"  MRR:  {mrr:.3f}  Mean Rank: {mean_rank:.1f}")

    return results


def print_results_table(results: Dict[str, dict]):
    """Print results in the format matching the paper's MEHDIE table."""
    print("\n" + "=" * 90)
    print("PANPHON192 MEHDIE BENCHMARK RESULTS")
    print("=" * 90)

    testset_labels = {
        'testset7-YaqutSham_KimaSham': 'TS7 (Yaqut-Kima Sham)',
        'testset8-KimaShamThurayyaSham': 'TS8 (Kima-Thurayya Sham)',
        'testset9-TudelaThurayya': 'TS9 (Tudela-Thurayya)',
        'testset10-YaqutAndalusMagrebKima-KimaMagrebAndalusMapped': 'TS10 (Yaqut-Kima Maghreb)',
        'testset11-DamastTudela': 'TS11 (Damast-Tudela)',
    }

    print(f"\n{'Method':<12} {'Testset':<28} {'R@1':>6} {'R@5':>6} {'R@10':>6} {'MRR':>6}")
    print("-" * 70)

    r1s, r5s, r10s, mrrs = [], [], [], []

    for ts_name in testset_labels:
        if ts_name not in results:
            continue
        r = results[ts_name]
        label = testset_labels[ts_name]
        r1 = r['recall_at_1'] * 100
        r5 = r['recall_at_5'] * 100
        r10 = r['recall_at_10'] * 100
        mrr_val = r['mrr'] * 100

        r1s.append(r1); r5s.append(r5); r10s.append(r10); mrrs.append(mrr_val)

        print(f"{'PanPhon192':<12} {label:<28} {r1:>6.1f} {r5:>6.1f} {r10:>6.1f} {mrr_val:>6.1f}")

    if r1s:
        print("-" * 70)
        print(f"{'PanPhon192':<12} {'Mean':<28} {np.mean(r1s):>6.1f} {np.mean(r5s):>6.1f} "
              f"{np.mean(r10s):>6.1f} {np.mean(mrrs):>6.1f}")

    # Comparison table
    print("\n\nCOMPARISON (from paper):")
    print(f"{'Method':<12} {'R@1':>6} {'R@5':>6} {'R@10':>6} {'MRR':>6}")
    print("-" * 40)
    print(f"{'Levenshtein':<12} {'81.5':>6} {'97.5':>6} {'99.4':>6} {'88.5':>6}")
    print(f"{'Jaro-Winkler':<12} {'78.5':>6} {'96.2':>6} {'97.8':>6} {'86.3':>6}")
    print(f"{'Symphonym':<12} {'85.2':>6} {'97.0':>6} {'97.6':>6} {'90.8':>6}")
    if r1s:
        print(f"{'PanPhon192':<12} {np.mean(r1s):>6.1f} {np.mean(r5s):>6.1f} "
              f"{np.mean(r10s):>6.1f} {np.mean(mrrs):>6.1f}")


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="PanPhon192 baseline evaluation on MEHDIE benchmark"
    )
    parser.add_argument(
        '--testsets', type=str,
        default='testing/mehdie-testsets',
        help='Path to MEHDIE testsets directory'
    )
    parser.add_argument(
        '--output', type=str, default=None,
        help='Path to save JSON results'
    )
    parser.add_argument(
        '--coverage-only', action='store_true',
        help='Only run coverage analysis, skip ranking evaluation'
    )
    args = parser.parse_args()

    print("=" * 80)
    print("PANPHON192 BASELINE — MEHDIE BENCHMARK EVALUATION")
    print("=" * 80)

    # Load benchmark
    testsets_path = Path(args.testsets)
    if not testsets_path.is_absolute():
        testsets_path = Path(__file__).parent.parent / args.testsets
    benchmark = MEHDIEBenchmark(str(testsets_path))

    if not benchmark.testsets:
        print("ERROR: No testsets loaded.")
        sys.exit(1)

    # Initialize PanPhon192 computer
    print("\nInitializing G2P backends...")
    computer = PanPhon192Computer()

    # Step 1: Coverage analysis
    print("\n" + "=" * 80)
    print("STEP 1: G2P COVERAGE ANALYSIS")
    print("=" * 80)
    name_results, testset_stats = analyze_coverage(benchmark, computer)
    print_coverage_report(testset_stats, benchmark, name_results)

    if args.coverage_only:
        print("\n--coverage-only flag set, stopping here.")
        return

    # Step 2: Ranking evaluation
    print("\n" + "=" * 80)
    print("STEP 2: RANKING EVALUATION")
    print("=" * 80)
    ranking_results = evaluate_panphon192_ranking(benchmark, name_results)

    # Step 3: Print results
    print_results_table(ranking_results)

    # Step 4: Failure analysis
    print("\n" + "=" * 80)
    print("FAILURE ANALYSIS: G2P failures in ground truth pairs")
    print("=" * 80)

    for ts_name, testset in benchmark.testsets.items():
        short = ts_name.replace('testset', 'TS').split('-')[0]
        gt_failures = []
        for id1, id2 in testset.ground_truth:
            rec1 = testset.dataset1.get(id1, {})
            rec2 = testset.dataset2.get(id2, {})
            names1 = rec1.get('all_names', [])
            names2 = rec2.get('all_names', [])

            any_q = any(
                name_results.get(n, {}).get('success', False)
                for n in names1 if n
            )
            any_c = any(
                name_results.get(n, {}).get('success', False)
                for n in names2 if n
            )

            if not any_q or not any_c:
                gt_failures.append({
                    'id1': id1, 'id2': id2,
                    'names1': names1[:3], 'names2': names2[:3],
                    'q_ok': any_q, 'c_ok': any_c
                })

        if gt_failures:
            print(f"\n{short}: {len(gt_failures)} ground truth pairs affected")
            for f in gt_failures[:10]:
                side = "query" if not f['q_ok'] else "candidate"
                print(f"  {f['id1']} <-> {f['id2']} ({side} failed)")
                print(f"    Query names:     {f['names1']}")
                print(f"    Candidate names: {f['names2']}")
        else:
            print(f"\n{short}: All ground truth pairs have G2P coverage ✓")

    # Save results
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        save_data = {
            'timestamp': datetime.now().isoformat(),
            'method': 'PanPhon192',
            'description': (
                'PanPhon 8-bin positional pooling (192-dim) with cosine similarity. '
                'G2P: Epitran (ara-Arab) for Arabic, Phonikud for Hebrew.'
            ),
            'coverage': {},
            'ranking_results': ranking_results,
        }
        for ts_name, stats in testset_stats.items():
            save_data['coverage'][ts_name] = {
                'd1_total': stats['d1_total'],
                'd1_ok': stats['d1_ok'],
                'd2_total': stats['d2_total'],
                'd2_ok': stats['d2_ok'],
                'failures': [
                    {'name': f['name'], 'lang': f['lang'], 'side': f['side']}
                    for f in stats['failures']
                ]
            }

        with open(output_path, 'w') as fh:
            json.dump(save_data, fh, indent=2)
        print(f"\nResults saved to: {output_path}")


if __name__ == '__main__':
    main()




