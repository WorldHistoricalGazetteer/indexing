"""
MEHDIE Benchmark Evaluation Framework

Evaluates phonetic similarity models against the MEHDIE benchmark testsets
for cross-lingual historical toponym matching.

Reference:
    Sagi et al. (2025) "Utilizing phonetic similarity for cross-source and
    cross-language toponym matching: a benchmark and prototype"
    Language Resources and Evaluation, 59:2427-2451
    https://doi.org/10.1007/s10579-025-09812-9

Testsets (from paper Table 2):
    - testset7:  YaqutSham ↔ KimaSham (30 matches)
    - testset8:  KimaSham ↔ ThurayyaSham (21 matches)
    - testset9:  Tudela ↔ Thurayya (18 matches)
    - testset10: YaqutAndalusMagreb ↔ KimaMagrebAndalus (28 matches)
    - testset11: Damast ↔ Tudela (32 matches)

Primary Metric: F-5 (recall weighted 5x over precision)
    The paper notes users prefer high recall and tolerate low precision,
    as they want to find all potential matches for manual review.

Usage:
    from testing.mehdie_benchmark import MEHDIEBenchmark

    benchmark = MEHDIEBenchmark('/path/to/mehdie-testsets')
    results = benchmark.evaluate_model(model, thresholds=[0.7, 0.8, 0.9])
    benchmark.print_results(results)
"""

import csv
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Callable
from pathlib import Path

import numpy as np
import torch

# Try to import anyascii
try:
    from anyascii import anyascii
except ImportError:
    def anyascii(text): return text


@dataclass
class TestSet:
    """A single MEHDIE testset with two datasets and ground truth matches."""
    name: str
    dataset1_path: str
    dataset2_path: str
    ground_truth_path: str
    dataset1_name: str = ""
    dataset2_name: str = ""

    # Loaded data
    dataset1: Dict[str, dict] = field(default_factory=dict)
    dataset2: Dict[str, dict] = field(default_factory=dict)
    ground_truth: List[Tuple[str, str]] = field(default_factory=list)

    def __post_init__(self):
        self.dataset1_name = Path(self.dataset1_path).stem
        self.dataset2_name = Path(self.dataset2_path).stem


@dataclass
class EvaluationResult:
    """Results for a single testset at a specific threshold."""
    testset_name: str
    threshold: float
    precision: float
    recall: float
    f1: float
    f5: float
    true_positives: int
    false_positives: int
    false_negatives: int
    total_ground_truth: int
    total_predictions: int


class MEHDIEBenchmark:
    """
    Benchmark evaluation against MEHDIE testsets.

    Evaluates a phonetic similarity model's ability to match toponyms
    across different scripts (Hebrew, Arabic) and sources.
    """

    # Testset configurations matching the paper
    TESTSET_CONFIGS = {
        'testset7-YaqutSham_KimaSham': {
            'dataset1': 'YaqutSham.tsv',
            'dataset2': 'KimaSham.tsv',
            'ground_truth': 'em.tsv'
        },
        'testset8-KimaShamThurayyaSham': {
            'dataset1': 'KimaSham.tsv',
            'dataset2': 'ThurayaSham_295.tsv',
            'ground_truth': 'em.tsv'
        },
        'testset9-TudelaThurayya': {
            'dataset1': 'Tudela_257.tsv',
            'dataset2': 'althurayya_283.tsv',
            'ground_truth': 'em.tsv'
        },
        'testset10-YaqutAndalusMagrebKima-KimaMagrebAndalusMapped': {
            'dataset1': 'Yaqut_306.tsv',
            'dataset2': 'Kima_303.tsv',
            'ground_truth': 'em.tsv'
        },
        'testset11-DamastTudela': {
            'dataset1': 'damast_315.tsv',
            'dataset2': 'Tudela_257.tsv',
            'ground_truth': 'em.tsv'
        }
    }

    def __init__(self, testsets_dir: str):
        """
        Initialize benchmark with path to MEHDIE testsets.

        Args:
            testsets_dir: Path to directory containing testset folders
        """
        self.testsets_dir = Path(testsets_dir)
        self.testsets: Dict[str, TestSet] = {}
        self._load_testsets()

    def _load_testsets(self):
        """Load all available testsets."""
        for folder_name, config in self.TESTSET_CONFIGS.items():
            folder_path = self.testsets_dir / folder_name

            if not folder_path.exists():
                print(f"Warning: Testset folder not found: {folder_path}")
                continue

            dataset1_path = folder_path / config['dataset1']
            dataset2_path = folder_path / config['dataset2']
            ground_truth_path = folder_path / config['ground_truth']

            if not all(p.exists() for p in [dataset1_path, dataset2_path, ground_truth_path]):
                print(f"Warning: Missing files in {folder_name}")
                continue

            testset = TestSet(
                name=folder_name,
                dataset1_path=str(dataset1_path),
                dataset2_path=str(dataset2_path),
                ground_truth_path=str(ground_truth_path)
            )

            # Load data
            testset.dataset1 = self._load_dataset(dataset1_path)
            testset.dataset2 = self._load_dataset(dataset2_path)
            testset.ground_truth = self._load_ground_truth(ground_truth_path)

            self.testsets[folder_name] = testset
            print(f"Loaded {folder_name}: {len(testset.dataset1)} × {len(testset.dataset2)} "
                  f"places, {len(testset.ground_truth)} ground truth matches")

    def _load_dataset(self, path: Path) -> Dict[str, dict]:
        """Load a dataset TSV file."""
        dataset = {}
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                record_id = row.get('id', '')
                if record_id:
                    # Get all name variants
                    title = row.get('title', '')
                    variants_str = row.get('variants', '')

                    # Parse variants (may be semicolon or colon separated)
                    variants = []
                    if variants_str:
                        # Handle both separators
                        for sep in [';', ':']:
                            if sep in variants_str:
                                variants.extend(variants_str.split(sep))
                                break
                        else:
                            variants = [variants_str]

                    # Clean variants
                    variants = [v.strip() for v in variants if v.strip()]

                    # Add title if not in variants
                    all_names = [title] + [v for v in variants if v != title]

                    dataset[record_id] = {
                        'id': record_id,
                        'title': title,
                        'variants': variants,
                        'all_names': all_names,
                        'lat': row.get('lat', ''),
                        'lon': row.get('lon', ''),
                        'raw': row
                    }
        return dataset

    def _load_ground_truth(self, path: Path) -> List[Tuple[str, str]]:
        """Load ground truth matches from em.tsv."""
        matches = []
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                judgement = row.get('judgement', '').lower()
                if judgement == 'true':
                    id1 = row.get('id_1', '')
                    id2 = row.get('id_2', '')
                    if id1 and id2:
                        matches.append((id1, id2))
        return matches

    def evaluate_model(
            self,
            similarity_fn: Callable[[str, str], float],
            thresholds: List[float] = None,
            testset_names: Optional[List[str]] = None,
            verbose: bool = True
    ) -> Dict[str, List[EvaluationResult]]:
        """
        Evaluate a similarity function against the benchmark.
        Passes RAW strings to similarity_fn.
        """
        if thresholds is None:
            # Default thresholds matching paper range
            thresholds = [0.7, 0.8, 0.85, 0.9, 0.95]

        results = {}

        testsets_to_eval = testset_names or list(self.testsets.keys())

        for testset_name in testsets_to_eval:
            if testset_name not in self.testsets:
                print(f"Warning: Testset {testset_name} not found")
                continue

            testset = self.testsets[testset_name]

            if verbose:
                print(f"\nEvaluating {testset_name}...")

            # Compute similarity scores for all record pairs
            # We match at record level: if ANY name pair exceeds threshold, it's a match
            pair_scores = {}  # (id1, id2) -> max similarity score

            total_comparisons = len(testset.dataset1) * len(testset.dataset2)
            if verbose:
                print(f"  Computing {total_comparisons:,} pairwise similarities...")

            for id1, record1 in testset.dataset1.items():
                for id2, record2 in testset.dataset2.items():
                    max_score = 0.0
                    for name1 in record1['all_names']:
                        for name2 in record2['all_names']:
                            if name1 and name2:
                                # PASS RAW NAMES: let similarity_fn handle romanization/detection
                                score = similarity_fn(name1, name2)
                                max_score = max(max_score, score)
                    pair_scores[(id1, id2)] = max_score

            # Ground truth as set for fast lookup
            gt_set = set(testset.ground_truth)

            # Evaluate at each threshold
            testset_results = []
            for threshold in thresholds:
                # Predictions at this threshold
                predictions = {
                    pair for pair, score in pair_scores.items()
                    if score >= threshold
                }

                # Calculate metrics
                tp = len(predictions & gt_set)
                fp = len(predictions - gt_set)
                fn = len(gt_set - predictions)

                precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
                recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

                f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

                # F-5 emphasizes recall 5x more than precision (paper's primary metric)
                beta = 5
                f5 = (1 + beta ** 2) * precision * recall / (beta ** 2 * precision + recall) \
                    if (beta ** 2 * precision + recall) > 0 else 0.0

                result = EvaluationResult(
                    testset_name=testset_name,
                    threshold=threshold,
                    precision=precision,
                    recall=recall,
                    f1=f1,
                    f5=f5,
                    true_positives=tp,
                    false_positives=fp,
                    false_negatives=fn,
                    total_ground_truth=len(gt_set),
                    total_predictions=len(predictions)
                )
                testset_results.append(result)

                if verbose:
                    print(f"  θ={threshold:.2f}: P={precision:.3f} R={recall:.3f} "
                          f"F1={f1:.3f} F5={f5:.3f} (TP={tp}, FP={fp}, FN={fn})")

            results[testset_name] = testset_results

        return results

    def print_results(self, results: Dict[str, List[EvaluationResult]],
                      latex: bool = False):
        """Print results in a formatted table."""
        if latex:
            self._print_latex_table(results)
        else:
            self._print_ascii_table(results)

    def _print_ascii_table(self, results: Dict[str, List[EvaluationResult]]):
        """Print ASCII formatted results table."""
        print("\n" + "=" * 100)
        print("MEHDIE BENCHMARK RESULTS")
        print("=" * 100)

        for testset_name, testset_results in results.items():
            print(f"\n{testset_name}")
            print("-" * 90)
            print(f"{'Threshold':>10} {'Precision':>10} {'Recall':>10} {'F1':>10} "
                  f"{'F5':>10} {'TP':>6} {'FP':>6} {'FN':>6}")
            print("-" * 90)

            for r in testset_results:
                print(f"{r.threshold:>10.2f} {r.precision:>10.3f} {r.recall:>10.3f} "
                      f"{r.f1:>10.3f} {r.f5:>10.3f} {r.true_positives:>6} "
                      f"{r.false_positives:>6} {r.false_negatives:>6}")

        # Summary: best F-5 per testset (matching paper's primary metric)
        print("\n" + "=" * 100)
        print("SUMMARY (Best F-5 per testset)")
        print("=" * 100)
        print(f"{'Testset':<50} {'θ':>6} {'P':>8} {'R':>8} {'F5':>8}")
        print("-" * 100)

        for testset_name, testset_results in results.items():
            best = max(testset_results, key=lambda r: r.f5)
            short_name = testset_name.replace('testset', 'TS')
            print(f"{short_name:<50} {best.threshold:>6.2f} {best.precision:>8.3f} "
                  f"{best.recall:>8.3f} {best.f5:>8.3f}")

    def _print_latex_table(self, results: Dict[str, List[EvaluationResult]]):
        """Print LaTeX formatted results table."""
        print("\n% LaTeX table")
        print("\\begin{table}[h]")
        print("\\centering")
        print("\\begin{tabular}{lccccc}")
        print("\\toprule")
        print("Testset & $\\theta$ & Precision & Recall & F-1 & F-5 \\\\")
        print("\\midrule")

        for testset_name, testset_results in results.items():
            best = max(testset_results, key=lambda r: r.f5)
            short_name = testset_name.split('-')[0].replace('testset', 'TS')
            print(f"{short_name} & {best.threshold:.2f} & {best.precision:.3f} & "
                  f"{best.recall:.3f} & {best.f1:.3f} & {best.f5:.3f} \\\\")

        print("\\bottomrule")
        print("\\end{tabular}")
        print("\\caption{MEHDIE benchmark results (F-5 is primary metric)}")
        print("\\label{tab:mehdie-results}")
        print("\\end{table}")

    def compare_methods(
            self,
            methods: Dict[str, Callable[[str, str], float]],
            thresholds: List[float] = None,
            verbose: bool = True
    ) -> Dict[str, Dict[str, List[EvaluationResult]]]:
        """
        Compare multiple similarity methods.

        Args:
            methods: Dict mapping method name to similarity function
            thresholds: Thresholds to evaluate
            verbose: Print progress

        Returns:
            Dict mapping method name to results dict
        """
        if thresholds is None:
            thresholds = [0.7, 0.8, 0.85, 0.9, 0.95]

        all_results = {}

        for method_name, similarity_fn in methods.items():
            if verbose:
                print(f"\n{'=' * 60}")
                print(f"Evaluating method: {method_name}")
                print('=' * 60)

            all_results[method_name] = self.evaluate_model(
                similarity_fn, thresholds, verbose=verbose
            )

        return all_results

    def print_comparison(self, all_results: Dict[str, Dict[str, List[EvaluationResult]]]):
        """Print comparison table across methods (using F-5 as primary metric)."""
        print("\n" + "=" * 120)
        print("METHOD COMPARISON (Best F-5 per testset)")
        print("=" * 120)

        methods = list(all_results.keys())
        testsets = list(next(iter(all_results.values())).keys())

        # Header
        header = f"{'Testset':<40}"
        for method in methods:
            header += f" {method:>15}"
        print(header)
        print("-" * 120)

        # Rows
        for testset in testsets:
            row = f"{testset:<40}"
            for method in methods:
                best = max(all_results[method][testset], key=lambda r: r.f5)
                row += f" {best.f5:>15.3f}"
            print(row)

        # Average
        print("-" * 120)
        row = f"{'AVERAGE':<40}"
        for method in methods:
            avg_f5 = np.mean([
                max(all_results[method][ts], key=lambda r: r.f5).f5
                for ts in testsets
            ])
            row += f" {avg_f5:>15.3f}"
        print(row)

    def evaluate_ranking(
            self,
            similarity_fn: Callable[[str, str], float],
            testset_names: Optional[List[str]] = None,
            verbose: bool = True,
            embedding_cache: Optional[Dict[str, 'torch.Tensor']] = None,
    ) -> Dict[str, dict]:
        """
        Evaluate using ranking metrics (Recall@K, MRR) rather than thresholds.

        For each ground truth pair (id1, id2), we query with all names from id1
        and rank all records in dataset2 by maximum similarity. We then check
        where id2 appears in the ranking.

        This is the appropriate evaluation for embedding-based retrieval systems
        like Symphonym.

        Args:
            similarity_fn: Function that computes similarity between two strings
            testset_names: Optional list of testsets to evaluate
            verbose: Print progress
            embedding_cache: Optional pre-computed embeddings dict (name -> tensor)
                            If provided, uses fast matrix operations

        Returns:
            Dict mapping testset name to metrics dict containing:
            - recall_at_1, recall_at_5, recall_at_10, recall_at_20
            - mrr (Mean Reciprocal Rank)
            - mean_rank
        """
        results = {}

        testsets_to_eval = testset_names or list(self.testsets.keys())

        for testset_name in testsets_to_eval:
            if testset_name not in self.testsets:
                print(f"Warning: Testset {testset_name} not found")
                continue

            testset = self.testsets[testset_name]

            if verbose:
                print(f"\nEvaluating {testset_name} (ranking mode)...")

            # Use fast matrix computation if embeddings are cached
            if embedding_cache is not None:
                query_rankings = self._compute_rankings_fast(
                    testset, embedding_cache, verbose
                )
            else:
                query_rankings = self._compute_rankings_slow(
                    testset, similarity_fn, verbose
                )

            # Now evaluate: for each ground truth pair, find rank of correct answer
            ranks = []
            reciprocal_ranks = []

            # Group ground truth by query (id1)
            gt_by_query = {}
            for id1, id2 in testset.ground_truth:
                if id1 not in gt_by_query:
                    gt_by_query[id1] = set()
                gt_by_query[id1].add(id2)

            for id1, correct_ids in gt_by_query.items():
                if id1 not in query_rankings:
                    continue

                ranking = query_rankings[id1]

                # Find rank of first correct answer
                for rank, (id2, score) in enumerate(ranking, start=1):
                    if id2 in correct_ids:
                        ranks.append(rank)
                        reciprocal_ranks.append(1.0 / rank)
                        break
                else:
                    # Correct answer not found (shouldn't happen)
                    ranks.append(len(ranking) + 1)
                    reciprocal_ranks.append(0.0)

            # Compute metrics
            if ranks:
                recall_at_1 = sum(1 for r in ranks if r <= 1) / len(ranks)
                recall_at_5 = sum(1 for r in ranks if r <= 5) / len(ranks)
                recall_at_10 = sum(1 for r in ranks if r <= 10) / len(ranks)
                recall_at_20 = sum(1 for r in ranks if r <= 20) / len(ranks)
                mrr = np.mean(reciprocal_ranks)
                mean_rank = np.mean(ranks)
            else:
                recall_at_1 = recall_at_5 = recall_at_10 = recall_at_20 = 0.0
                mrr = 0.0
                mean_rank = float('inf')

            results[testset_name] = {
                'recall_at_1': recall_at_1,
                'recall_at_5': recall_at_5,
                'recall_at_10': recall_at_10,
                'recall_at_20': recall_at_20,
                'mrr': mrr,
                'mean_rank': mean_rank,
                'num_queries': len(gt_by_query),
                'corpus_size': len(testset.dataset2),
            }

            if verbose:
                print(f"  Queries: {len(gt_by_query)}, Corpus: {len(testset.dataset2)}")
                print(f"  Recall@1:  {recall_at_1:.3f}")
                print(f"  Recall@5:  {recall_at_5:.3f}")
                print(f"  Recall@10: {recall_at_10:.3f}")
                print(f"  Recall@20: {recall_at_20:.3f}")
                print(f"  MRR:       {mrr:.3f}")
                print(f"  Mean Rank: {mean_rank:.1f}")

        return results

    def _compute_rankings_fast(
            self,
            testset: TestSet,
            embedding_cache: Dict[str, 'torch.Tensor'],
            verbose: bool = True
    ) -> Dict[str, List[Tuple[str, float]]]:
        """
        Compute rankings using fast matrix operations on pre-computed embeddings.
        """
        import torch
        import torch.nn.functional as F

        if verbose:
            print(f"  Computing rankings (fast matrix mode)...")

        # Build name-to-embedding lookup and record-to-names mapping
        # For each record, get all its name embeddings

        # Dataset2: build matrix of all name embeddings with record mapping
        d2_names = []
        d2_record_ids = []
        d2_name_to_idx = {}

        for id2, record2 in testset.dataset2.items():
            for name in record2['all_names']:
                if name and name in embedding_cache:
                    idx = len(d2_names)
                    d2_names.append(name)
                    d2_record_ids.append(id2)
                    if name not in d2_name_to_idx:
                        d2_name_to_idx[name] = []
                    d2_name_to_idx[name].append(idx)

        if not d2_names:
            return {}

        # Stack embeddings into matrix
        d2_embeddings = torch.stack([embedding_cache[n] for n in d2_names])
        # Normalize for cosine similarity
        d2_embeddings = F.normalize(d2_embeddings, p=2, dim=1)

        query_rankings = {}

        # Process each query record
        for id1, record1 in testset.dataset1.items():
            # Get embeddings for all names in this record
            q_names = [n for n in record1['all_names'] if n and n in embedding_cache]
            if not q_names:
                continue

            q_embeddings = torch.stack([embedding_cache[n] for n in q_names])
            q_embeddings = F.normalize(q_embeddings, p=2, dim=1)

            # Compute all similarities at once: [num_query_names, num_d2_names]
            sim_matrix = torch.mm(q_embeddings, d2_embeddings.t())

            # For each d2 record, get max similarity across all name pairs
            record_scores = {}
            for idx, id2 in enumerate(d2_record_ids):
                # Max similarity from any query name to this d2 name
                max_sim = sim_matrix[:, idx].max().item()
                if id2 not in record_scores:
                    record_scores[id2] = max_sim
                else:
                    record_scores[id2] = max(record_scores[id2], max_sim)

            # Sort by score descending
            ranking = sorted(record_scores.items(), key=lambda x: -x[1])
            query_rankings[id1] = ranking

        return query_rankings

    def _compute_rankings_slow(
            self,
            testset: TestSet,
            similarity_fn: Callable[[str, str], float],
            verbose: bool = True
    ) -> Dict[str, List[Tuple[str, float]]]:
        """
        Compute rankings using pairwise similarity function (slow fallback).
        """
        if verbose:
            print(f"  Computing similarities for {len(testset.dataset1)} queries (slow mode)...")

        query_rankings = {}

        for id1, record1 in testset.dataset1.items():
            scores = []
            for id2, record2 in testset.dataset2.items():
                # Max similarity across all name pairs
                max_score = 0.0
                for name1 in record1['all_names']:
                    for name2 in record2['all_names']:
                        if name1 and name2:
                            score = similarity_fn(name1, name2)
                            max_score = max(max_score, score)
                scores.append((id2, max_score))

            # Sort by score descending
            scores.sort(key=lambda x: -x[1])
            query_rankings[id1] = scores

        return query_rankings

    def compare_methods_ranking(
            self,
            methods: Dict[str, Callable[[str, str], float]],
            verbose: bool = True,
            embedding_caches: Optional[Dict[str, Dict[str, 'torch.Tensor']]] = None,
    ) -> Dict[str, Dict[str, dict]]:
        """
        Compare multiple methods using ranking metrics.

        Args:
            methods: Dict mapping method name to similarity function
            verbose: Print progress
            embedding_caches: Optional dict mapping method name to its embedding cache

        Returns:
            Dict mapping method name to results from evaluate_ranking
        """
        all_results = {}

        if embedding_caches is None:
            embedding_caches = {}

        for method_name, similarity_fn in methods.items():
            if verbose:
                print(f"\n{'=' * 60}")
                print(f"Evaluating method: {method_name}")
                print('=' * 60)

            cache = embedding_caches.get(method_name)
            all_results[method_name] = self.evaluate_ranking(
                similarity_fn, verbose=verbose, embedding_cache=cache
            )

        return all_results


def create_model_similarity_fn(model, char_vocab, lang_vocab, device='cuda'):
    """
    DEPRECATED: Use run_mehdie_evaluation.create_symphonym_similarity_fn instead.

    This legacy function is kept for backwards compatibility but should not be used
    with the current Symphonym model architecture.
    """
    raise NotImplementedError(
        "This function is deprecated. Use run_mehdie_evaluation.py with ToponymEncoder instead."
    )



# =============================================================================
# Baseline similarity functions for comparison
# =============================================================================

def levenshtein_similarity(s1: str, s2: str) -> float:
    """Normalized Levenshtein similarity (with internal Romanization)."""
    if not s1 or not s2:
        return 0.0

    # Internal Romanization for baselines
    s1 = anyascii(s1).lower()
    s2 = anyascii(s2).lower()

    m, n = len(s1), len(s2)
    if m < n:
        s1, s2 = s2, s1
        m, n = n, m

    if n == 0:
        return 0.0

    previous = list(range(n + 1))
    for i, c1 in enumerate(s1):
        current = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous[j + 1] + 1
            deletions = current[j] + 1
            substitutions = previous[j] + (c1 != c2)
            current.append(min(insertions, deletions, substitutions))
        previous = current

    distance = previous[n]
    max_len = max(m, n)
    return 1.0 - (distance / max_len)


def jaro_winkler_similarity(s1: str, s2: str) -> float:
    """Jaro-Winkler string similarity (with internal Romanization)."""
    if not s1 or not s2:
        return 0.0

    # Internal Romanization for baselines
    s1 = anyascii(s1).lower()
    s2 = anyascii(s2).lower()

    if s1 == s2:
        return 1.0

    len1, len2 = len(s1), len(s2)
    match_distance = max(len1, len2) // 2 - 1
    if match_distance < 0:
        match_distance = 0

    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    transpositions = 0

    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)

        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro = (matches / len1 + matches / len2 +
            (matches - transpositions / 2) / matches) / 3

    # Winkler modification
    prefix = 0
    for i in range(min(len1, len2, 4)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break

    return jaro + prefix * 0.1 * (1 - jaro)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='MEHDIE Benchmark Evaluation (Baselines Only)',
        epilog="""
For evaluation with the Symphonym model, use:
    python -m testing.run_mehdie_evaluation --testsets /path/to/mehdie-testsets
        """
    )
    parser.add_argument('testsets_dir', help='Path to MEHDIE testsets directory')
    parser.add_argument('--thresholds', type=float, nargs='+',
                        default=[0.7, 0.8, 0.85, 0.9, 0.95],
                        help='Similarity thresholds to evaluate')
    parser.add_argument('--latex', action='store_true',
                        help='Output results in LaTeX format')

    args = parser.parse_args()

    # Load benchmark
    benchmark = MEHDIEBenchmark(args.testsets_dir)

    # Run baselines
    methods = {
        'Levenshtein': levenshtein_similarity,
        'Jaro-Winkler': jaro_winkler_similarity,
    }

    # Run evaluation
    all_results = benchmark.compare_methods(methods, args.thresholds)
    benchmark.print_comparison(all_results)
