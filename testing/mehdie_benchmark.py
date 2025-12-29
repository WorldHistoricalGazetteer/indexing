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

    def get_all_name_pairs(self, testset: TestSet) -> List[Tuple[str, str, str, str]]:
        """
        Get all possible name pairs between two datasets.

        Returns list of (id1, id2, name1, name2) tuples.
        """
        pairs = []
        for id1, record1 in testset.dataset1.items():
            for id2, record2 in testset.dataset2.items():
                # For each pair of records, compare all name variants
                for name1 in record1['all_names']:
                    for name2 in record2['all_names']:
                        if name1 and name2:
                            pairs.append((id1, id2, name1, name2))
        return pairs

    def evaluate_model(
            self,
            similarity_fn: Callable[[str, str], float],
            thresholds: List[float] = None,
            testset_names: Optional[List[str]] = None,
            verbose: bool = True
    ) -> Dict[str, List[EvaluationResult]]:
        """
        Evaluate a similarity function against the benchmark.

        Args:
            similarity_fn: Function (name1, name2) -> similarity score [0, 1]
            thresholds: List of similarity thresholds to evaluate
            testset_names: Specific testsets to evaluate (None = all)
            verbose: Print progress

        Returns:
            Dict mapping testset name to list of EvaluationResults per threshold
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


def create_model_similarity_fn(model, char_vocab, lang_vocab, device='cuda'):
    """
    Create a similarity function from a trained HybridPhoneticModel.

    Args:
        model: Trained HybridPhoneticModel
        char_vocab: CharVocab instance
        lang_vocab: LangVocab instance
        device: Device to run inference on

    Returns:
        Function (name1, name2) -> similarity score [0, 1]
    """
    import torch.nn.functional as F

    model = model.to(device)
    model.eval()

    # Cache for embeddings
    embedding_cache = {}

    def get_embedding(name: str) -> torch.Tensor:
        if name in embedding_cache:
            return embedding_cache[name]

        # Detect language based on script
        lang = detect_language(name)

        # Encode
        char_ids = torch.tensor([char_vocab.encode(name)], dtype=torch.long, device=device)
        lang_ids = torch.tensor([lang_vocab.encode(lang)], dtype=torch.long, device=device)
        lengths = torch.tensor([len(name)], dtype=torch.long, device=device)

        with torch.no_grad():
            emb = model.encode_char_only(char_ids, lang_ids, lengths)

        embedding_cache[name] = emb
        return emb

    def similarity_fn(name1: str, name2: str) -> float:
        if not name1 or not name2:
            return 0.0

        emb1 = get_embedding(name1)
        emb2 = get_embedding(name2)

        # Cosine similarity
        sim = F.cosine_similarity(emb1, emb2).item()

        # Convert from [-1, 1] to [0, 1]
        return (sim + 1) / 2

    return similarity_fn


def detect_language(text: str) -> str:
    """Detect language based on Unicode script."""
    if not text:
        return 'unknown'

    # Check first non-space character
    for char in text:
        if char.isspace():
            continue
        code = ord(char)

        # Hebrew: U+0590–U+05FF
        if 0x0590 <= code <= 0x05FF:
            return 'heb'

        # Arabic: U+0600–U+06FF, U+0750–U+077F, U+08A0–U+08FF
        if (0x0600 <= code <= 0x06FF or
                0x0750 <= code <= 0x077F or
                0x08A0 <= code <= 0x08FF):
            return 'ara'

        # Syriac: U+0700–U+074F
        if 0x0700 <= code <= 0x074F:
            return 'syr'

        # Latin
        if code < 0x0250:
            return 'lat'

    return 'unknown'


# =============================================================================
# Baseline similarity functions for comparison
# =============================================================================

def levenshtein_similarity(s1: str, s2: str) -> float:
    """Normalized Levenshtein similarity."""
    if not s1 or not s2:
        return 0.0

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
    """Jaro-Winkler string similarity."""
    if not s1 or not s2:
        return 0.0

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

    parser = argparse.ArgumentParser(description='MEHDIE Benchmark Evaluation')
    parser.add_argument('testsets_dir', help='Path to MEHDIE testsets directory')
    parser.add_argument('--model', help='Path to trained model checkpoint')
    parser.add_argument('--baselines', action='store_true',
                        help='Run baseline methods (Levenshtein, Jaro-Winkler)')
    parser.add_argument('--thresholds', type=float, nargs='+',
                        default=[0.7, 0.8, 0.85, 0.9, 0.95],
                        help='Similarity thresholds to evaluate')
    parser.add_argument('--latex', action='store_true',
                        help='Output results in LaTeX format')

    args = parser.parse_args()

    # Load benchmark
    benchmark = MEHDIEBenchmark(args.testsets_dir)

    methods = {}

    # Add baselines if requested
    if args.baselines:
        methods['Levenshtein'] = levenshtein_similarity
        methods['Jaro-Winkler'] = jaro_winkler_similarity

    # Add trained model if provided
    if args.model:
        # Import model classes
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent))

        from phonetics.models import HybridPhoneticModel, PhoneticEncoder, CharEncoder
        from phonetics.vocab import CharVocab, LangVocab

        # Load checkpoint
        checkpoint = torch.load(args.model, map_location='cpu')

        # Load vocabularies
        vocab_dir = Path(args.model).parent
        base_name = Path(args.model).stem
        char_vocab = CharVocab.load(vocab_dir / f'{base_name}_char_vocab.pkl')
        lang_vocab = LangVocab.load(vocab_dir / f'{base_name}_lang_vocab.pkl')

        # Reconstruct model
        phonetic_encoder = PhoneticEncoder()
        char_encoder = CharEncoder(
            vocab_size=checkpoint['char_vocab_size'],
            num_langs=checkpoint['num_langs']
        )
        model = HybridPhoneticModel(phonetic_encoder, char_encoder)
        model.load_state_dict(checkpoint['model_state'])

        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        methods['PhoneticModel'] = create_model_similarity_fn(
            model, char_vocab, lang_vocab, device
        )

    if not methods:
        print("No methods to evaluate. Use --baselines and/or --model")
        exit(1)

    # Run evaluation
    if len(methods) == 1:
        name, fn = list(methods.items())[0]
        results = benchmark.evaluate_model(fn, args.thresholds)
        benchmark.print_results(results, latex=args.latex)
    else:
        all_results = benchmark.compare_methods(methods, args.thresholds)
        benchmark.print_comparison(all_results)