#!/usr/bin/env python3
"""
Embedding Similarity Evaluation for Phonetic Model

Samples random toponyms from Elasticsearch and finds their k-nearest neighbours
using cosine similarity on the embedding field. Generates LaTeX tables suitable
for inclusion in academic papers.

Runs two separate test series:
1. Toponyms WITH IPA (phonetically grounded embeddings)
2. Toponyms WITHOUT IPA (character-only embeddings)

Optional cross-script mode samples toponyms from specific scripts and highlights
neighbours from different scripts, demonstrating cross-lingual phonetic matching.

Usage:
    python -m testing.evaluate_embeddings \
        --output article/embedding-evaluation.tex \
        --samples 10 \
        --neighbours 15 \
        --es-host localhost:9200 \
        --index toponyms

    # Cross-script evaluation
    python -m testing.evaluate_embeddings \
        --output article/cross-script-evaluation.tex \
        --cross-script \
        --samples 5 \
        --neighbours 15
"""

import argparse
import random
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Set

from elasticsearch import Elasticsearch

from processing.settings import ES_HOST


# Script detection ranges (Unicode blocks)
SCRIPT_RANGES = {
    'latin': [(0x0000, 0x024F), (0x1E00, 0x1EFF), (0x2C60, 0x2C7F)],
    'cyrillic': [(0x0400, 0x04FF), (0x0500, 0x052F), (0x2DE0, 0x2DFF)],
    'greek': [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
    'arabic': [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF)],
    'hebrew': [(0x0590, 0x05FF)],
    'cjk': [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3000, 0x303F)],
    'devanagari': [(0x0900, 0x097F)],
    'thai': [(0x0E00, 0x0E7F)],
    'georgian': [(0x10A0, 0x10FF)],
    'armenian': [(0x0530, 0x058F)],
}


def detect_script(text: str) -> str:
    """Detect the primary script of a text string."""
    if not text:
        return 'unknown'

    script_counts: Dict[str, int] = {}

    for char in text:
        if char.isspace() or not char.isalpha():
            continue

        code = ord(char)

        for script_name, ranges in SCRIPT_RANGES.items():
            for start, end in ranges:
                if start <= code <= end:
                    script_counts[script_name] = script_counts.get(script_name, 0) + 1
                    break

    if not script_counts:
        return 'latin'  # Default assumption

    return max(script_counts, key=script_counts.get)


@dataclass
class Toponym:
    """Represents a toponym document from ES."""
    doc_id: str
    name: str
    lang: str
    ipa: Optional[str]
    embedding: List[float]
    script: str = field(default='')

    def __post_init__(self):
        if not self.script:
            self.script = detect_script(self.name)

    @property
    def has_ipa(self) -> bool:
        return self.ipa is not None and len(self.ipa) > 0


@dataclass
class Neighbour:
    """A nearest neighbour result."""
    rank: int
    score: float
    name: str
    lang: str
    ipa: Optional[str]
    script: str = field(default='')

    def __post_init__(self):
        if not self.script:
            self.script = detect_script(self.name)


def escape_latex(text: str) -> str:
    """Escape special LaTeX characters."""
    if not text:
        return ""
    replacements = [
        ('\\', r'\textbackslash{}'),
        ('&', r'\&'),
        ('%', r'\%'),
        ('$', r'\$'),
        ('#', r'\#'),
        ('_', r'\_'),
        ('{', r'\{'),
        ('}', r'\}'),
        ('~', r'\textasciitilde{}'),
        ('^', r'\textasciicircum{}'),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def format_ipa(ipa: Optional[str]) -> str:
    """Format IPA for LaTeX output."""
    if not ipa:
        return r"\textemdash"
    # Use textipa command defined in the paper's preamble
    return rf"\textipa{{{escape_latex(ipa)}}}"


def get_random_toponyms(
        es: Elasticsearch,
        index: str,
        n: int,
        with_ipa: bool,
        seed: int = None
) -> List[Toponym]:
    """
    Sample n random toponyms from ES, filtered by IPA presence.

    Uses function_score with random_score for true random sampling.
    """
    if seed is not None:
        random.seed(seed)

    # Build query based on IPA filter
    if with_ipa:
        filter_clause = {
            "bool": {
                "must": [
                    {"exists": {"field": "embedding"}},
                    {"exists": {"field": "ipa_cached"}}
                ],
                "must_not": [
                    {"term": {"ipa_cached": ""}}
                ]
            }
        }
    else:
        filter_clause = {
            "bool": {
                "must": [
                    {"exists": {"field": "embedding"}}
                ],
                "should": [
                    {"bool": {"must_not": {"exists": {"field": "ipa_cached"}}}},
                    {"term": {"ipa_cached": ""}}
                ],
                "minimum_should_match": 1
            }
        }

    # Use random_score for random sampling
    query = {
        "size": n,
        "query": {
            "function_score": {
                "query": {"bool": {"filter": filter_clause}},
                "random_score": {"seed": seed or random.randint(0, 2 ** 31)},
                "boost_mode": "replace"
            }
        },
        "_source": ["name", "lang", "ipa_cached", "embedding"]
    }

    resp = es.search(index=index, body=query)

    toponyms = []
    for hit in resp['hits']['hits']:
        source = hit['_source']
        toponyms.append(Toponym(
            doc_id=hit['_id'],
            name=source.get('name', ''),
            lang=source.get('lang', 'und'),
            ipa=source.get('ipa_cached'),
            embedding=source.get('embedding', [])
        ))

    return toponyms


def get_toponyms_by_script(
        es: Elasticsearch,
        index: str,
        script: str,
        n: int,
        seed: int = None
) -> List[Toponym]:
    """
    Sample n random toponyms from ES that are primarily in the specified script.

    Uses a regex filter on the name field to match script-specific character ranges.
    """
    if seed is not None:
        random.seed(seed)

    # Build regex patterns for each script
    script_patterns = {
        'cyrillic': '[А-Яа-яЁёҐґЄєІіЇї]',
        'greek': '[Α-Ωα-ωάέήίόύώ]',
        'arabic': '[\u0600-\u06FF]',
        'hebrew': '[\u0590-\u05FF]',
        'cjk': '[\u4E00-\u9FFF]',
        'devanagari': '[\u0900-\u097F]',
        'thai': '[\u0E00-\u0E7F]',
        'georgian': '[\u10A0-\u10FF]',
        'armenian': '[\u0530-\u058F]',
    }

    if script not in script_patterns:
        print(f"Warning: No regex pattern for script '{script}', falling back to random sampling")
        return get_random_toponyms(es, index, n, with_ipa=True, seed=seed)

    pattern = script_patterns[script]

    query = {
        "size": n,
        "query": {
            "function_score": {
                "query": {
                    "bool": {
                        "must": [
                            {"exists": {"field": "embedding"}},
                            {"regexp": {"name": f".*{pattern}.*"}}
                        ]
                    }
                },
                "random_score": {"seed": seed or random.randint(0, 2 ** 31)},
                "boost_mode": "replace"
            }
        },
        "_source": ["name", "lang", "ipa_cached", "embedding"]
    }

    resp = es.search(index=index, body=query)

    toponyms = []
    for hit in resp['hits']['hits']:
        source = hit['_source']
        toponyms.append(Toponym(
            doc_id=hit['_id'],
            name=source.get('name', ''),
            lang=source.get('lang', 'und'),
            ipa=source.get('ipa_cached'),
            embedding=source.get('embedding', [])
        ))

    return toponyms


def find_neighbours(
        es: Elasticsearch,
        index: str,
        toponym: Toponym,
        k: int = 15
) -> List[Neighbour]:
    """
    Find k nearest neighbours using ES kNN search.

    Uses cosine similarity as configured in the index mapping.
    """
    if not toponym.embedding:
        return []

    # kNN query - ES 8.x syntax
    query = {
        "size": k + 1,  # +1 to account for self-match
        "knn": {
            "field": "embedding",
            "query_vector": toponym.embedding,
            "k": k + 1,
            "num_candidates": 100
        },
        "_source": ["name", "lang", "ipa_cached"]
    }

    resp = es.search(index=index, body=query)

    neighbours = []
    rank = 0

    for hit in resp['hits']['hits']:
        # Skip self-match
        if hit['_id'] == toponym.doc_id:
            continue

        rank += 1
        if rank > k:
            break

        source = hit['_source']
        neighbours.append(Neighbour(
            rank=rank,
            score=hit['_score'],
            name=source.get('name', ''),
            lang=source.get('lang', 'und'),
            ipa=source.get('ipa_cached')
        ))

    return neighbours


def generate_latex_table(
        toponym: Toponym,
        neighbours: List[Neighbour],
        table_id: int,
        series: str  # "ipa" or "noipa"
) -> str:
    """Generate a LaTeX table for one toponym and its neighbours."""

    label = f"tab:embed-{series}-{table_id}"
    ipa_status = "with IPA" if toponym.has_ipa else "without IPA"

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\small",
        rf"\caption{{Nearest neighbours for \textbf{{{escape_latex(toponym.name)}}} [{toponym.lang}] ({ipa_status})}}",
        rf"\label{{{label}}}",
        r"\begin{tabular}{rlllp{4cm}}",
        r"\toprule",
        r"\textbf{Rank} & \textbf{Score} & \textbf{Name} & \textbf{Lang} & \textbf{IPA} \\",
        r"\midrule",
    ]

    # Query toponym row (highlighted)
    query_ipa = format_ipa(toponym.ipa)
    lines.append(
        rf"\rowcolor{{gray!20}} Q & --- & {escape_latex(toponym.name)} & {toponym.lang} & {query_ipa} \\"
    )
    lines.append(r"\midrule")

    # Neighbour rows
    for n in neighbours:
        ipa_cell = format_ipa(n.ipa)
        lines.append(
            rf"{n.rank} & {n.score:.4f} & {escape_latex(n.name)} & {n.lang} & {ipa_cell} \\"
        )

    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ])

    return "\n".join(lines)


def generate_cross_script_table(
        toponym: Toponym,
        neighbours: List[Neighbour],
        table_id: int
) -> str:
    """Generate a LaTeX table highlighting cross-script matches."""

    label = f"tab:cross-script-{table_id}"

    # Count scripts in neighbours
    script_counts: Dict[str, int] = {}
    for n in neighbours:
        script_counts[n.script] = script_counts.get(n.script, 0) + 1

    cross_script_count = sum(1 for n in neighbours if n.script != toponym.script)

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\small",
        rf"\caption{{Cross-script neighbours for \textbf{{{escape_latex(toponym.name)}}} [{toponym.lang}, {toponym.script}] "
        rf"({cross_script_count}/{len(neighbours)} cross-script)}}",
        rf"\label{{{label}}}",
        r"\begin{tabular}{rllllp{3.5cm}}",
        r"\toprule",
        r"\textbf{Rank} & \textbf{Score} & \textbf{Name} & \textbf{Lang} & \textbf{Script} & \textbf{IPA} \\",
        r"\midrule",
    ]

    # Query toponym row (highlighted)
    query_ipa = format_ipa(toponym.ipa)
    lines.append(
        rf"\rowcolor{{gray!20}} Q & --- & {escape_latex(toponym.name)} & {toponym.lang} & {toponym.script} & {query_ipa} \\"
    )
    lines.append(r"\midrule")

    # Neighbour rows - highlight cross-script matches
    for n in neighbours:
        ipa_cell = format_ipa(n.ipa)
        if n.script != toponym.script:
            # Highlight cross-script matches
            lines.append(
                rf"\rowcolor{{blue!10}} {n.rank} & {n.score:.4f} & {escape_latex(n.name)} & {n.lang} & {n.script} & {ipa_cell} \\"
            )
        else:
            lines.append(
                rf"{n.rank} & {n.score:.4f} & {escape_latex(n.name)} & {n.lang} & {n.script} & {ipa_cell} \\"
            )

    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ])

    return "\n".join(lines)


def generate_summary_table(
        results: List[Tuple[Toponym, List[Neighbour]]],
        series: str
) -> str:
    """Generate a summary statistics table for a series."""

    if not results:
        return ""

    # Calculate statistics
    all_scores = []
    same_lang_counts = []
    has_ipa_counts = []

    for toponym, neighbours in results:
        scores = [n.score for n in neighbours]
        all_scores.extend(scores)

        same_lang = sum(1 for n in neighbours if n.lang == toponym.lang)
        same_lang_counts.append(same_lang)

        with_ipa = sum(1 for n in neighbours if n.ipa)
        has_ipa_counts.append(with_ipa)

    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
    min_score = min(all_scores) if all_scores else 0
    max_score = max(all_scores) if all_scores else 0

    avg_same_lang = sum(same_lang_counts) / len(same_lang_counts) if same_lang_counts else 0
    avg_with_ipa = sum(has_ipa_counts) / len(has_ipa_counts) if has_ipa_counts else 0

    k = len(results[0][1]) if results and results[0][1] else 15

    series_label = "With IPA" if series == "ipa" else "Without IPA"

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        rf"\caption{{Summary statistics for {series_label} series ($n={len(results)}$, $k={k}$)}}",
        rf"\label{{tab:embed-summary-{series}}}",
        r"\begin{tabular}{lr}",
        r"\toprule",
        r"\textbf{Metric} & \textbf{Value} \\",
        r"\midrule",
        rf"Mean similarity score & {avg_score:.4f} \\",
        rf"Min similarity score & {min_score:.4f} \\",
        rf"Max similarity score & {max_score:.4f} \\",
        r"\midrule",
        rf"Avg. same-language neighbours & {avg_same_lang:.1f} / {k} \\",
        rf"Avg. neighbours with IPA & {avg_with_ipa:.1f} / {k} \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ]

    return "\n".join(lines)


def generate_cross_script_summary(
        results: List[Tuple[Toponym, List[Neighbour]]]
) -> str:
    """Generate summary statistics for cross-script evaluation."""

    if not results:
        return ""

    all_scores = []
    cross_script_counts = []
    cross_script_scores = []
    same_script_scores = []
    scripts_found: Set[str] = set()

    for toponym, neighbours in results:
        scripts_found.add(toponym.script)

        for n in neighbours:
            all_scores.append(n.score)
            scripts_found.add(n.script)

            if n.script != toponym.script:
                cross_script_scores.append(n.score)
            else:
                same_script_scores.append(n.score)

        cross_count = sum(1 for n in neighbours if n.script != toponym.script)
        cross_script_counts.append(cross_count)

    k = len(results[0][1]) if results and results[0][1] else 15

    avg_cross = sum(cross_script_counts) / len(cross_script_counts) if cross_script_counts else 0
    avg_cross_score = sum(cross_script_scores) / len(cross_script_scores) if cross_script_scores else 0
    avg_same_score = sum(same_script_scores) / len(same_script_scores) if same_script_scores else 0

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\caption{Cross-script evaluation summary}",
        r"\label{tab:cross-script-summary}",
        r"\begin{tabular}{lr}",
        r"\toprule",
        r"\textbf{Metric} & \textbf{Value} \\",
        r"\midrule",
        rf"Query toponyms evaluated & {len(results)} \\",
        rf"Neighbours per query & {k} \\",
        rf"Scripts encountered & {len(scripts_found)} \\",
        r"\midrule",
        rf"Avg. cross-script neighbours & {avg_cross:.1f} / {k} \\",
        rf"Cross-script neighbour rate & {100 * avg_cross / k:.1f}\% \\",
        r"\midrule",
        rf"Mean score (cross-script) & {avg_cross_score:.4f} \\",
        rf"Mean score (same-script) & {avg_same_score:.4f} \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ]

    return "\n".join(lines)


def generate_comparison_table(
        ipa_results: List[Tuple[Toponym, List[Neighbour]]],
        noipa_results: List[Tuple[Toponym, List[Neighbour]]]
) -> str:
    """Generate a comparison table between IPA and non-IPA series."""

    def calc_stats(results):
        if not results:
            return {}

        all_scores = []
        top1_scores = []
        top5_scores = []

        for _, neighbours in results:
            scores = [n.score for n in neighbours]
            all_scores.extend(scores)
            if scores:
                top1_scores.append(scores[0])
                top5_scores.extend(scores[:5])

        return {
            'mean': sum(all_scores) / len(all_scores) if all_scores else 0,
            'top1_mean': sum(top1_scores) / len(top1_scores) if top1_scores else 0,
            'top5_mean': sum(top5_scores) / len(top5_scores) if top5_scores else 0,
        }

    ipa_stats = calc_stats(ipa_results)
    noipa_stats = calc_stats(noipa_results)

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\caption{Comparison of embedding quality: IPA vs non-IPA toponyms}",
        r"\label{tab:embed-comparison}",
        r"\begin{tabular}{lrr}",
        r"\toprule",
        r"\textbf{Metric} & \textbf{With IPA} & \textbf{Without IPA} \\",
        r"\midrule",
        rf"Mean similarity (all neighbours) & {ipa_stats.get('mean', 0):.4f} & {noipa_stats.get('mean', 0):.4f} \\",
        rf"Mean similarity (top-1) & {ipa_stats.get('top1_mean', 0):.4f} & {noipa_stats.get('top1_mean', 0):.4f} \\",
        rf"Mean similarity (top-5) & {ipa_stats.get('top5_mean', 0):.4f} & {noipa_stats.get('top5_mean', 0):.4f} \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ]

    return "\n".join(lines)


def run_evaluation(
        es_host: str,
        index: str,
        n_samples: int,
        k_neighbours: int,
        output_path: str,
        seed: int = None
):
    """Run the full evaluation and generate LaTeX output."""

    # Normalize ES host
    if not es_host.startswith(('http://', 'https://')):
        es_host = f'http://{es_host}'

    es = Elasticsearch([es_host], request_timeout=60)

    # Check connection
    if not es.ping():
        print(f"ERROR: Cannot connect to Elasticsearch at {es_host}")
        sys.exit(1)

    print(f"Connected to Elasticsearch at {es_host}")
    print(f"Index: {index}")
    print(f"Samples per series: {n_samples}")
    print(f"Neighbours per query: {k_neighbours}")
    print()

    # Refresh index
    print("Refreshing index...")
    es.indices.refresh(index=index)

    # Get index stats
    stats = es.count(index=index, body={"query": {"match_all": {}}})
    total_docs = stats['count']

    with_ipa_count = es.count(index=index, body={
        "query": {"bool": {"must": [
            {"exists": {"field": "ipa_cached"}},
            {"bool": {"must_not": {"term": {"ipa_cached": ""}}}}
        ]}}
    })['count']

    with_embedding = es.count(index=index, body={
        "query": {"exists": {"field": "embedding"}}
    })['count']

    print(f"Index statistics:")
    print(f"  Total documents: {total_docs:,}")
    print(f"  With embeddings: {with_embedding:,}")
    print(f"  With IPA: {with_ipa_count:,}")
    print()

    latex_parts = []

    # Preamble
    latex_parts.append(r"""% =============================================================================
% EMBEDDING SIMILARITY EVALUATION
% Generated by testing.evaluate_embeddings
% =============================================================================

% Required packages (add to document preamble if not present):
% \usepackage{booktabs}
% \usepackage{colortbl}
% \usepackage{xcolor}

""")

    # Series 1: With IPA
    print("=" * 60)
    print("SERIES 1: Toponyms WITH IPA")
    print("=" * 60)

    latex_parts.append(r"\subsection*{Series 1: Toponyms with IPA}")
    latex_parts.append("")

    ipa_toponyms = get_random_toponyms(es, index, n_samples, with_ipa=True, seed=seed)
    print(f"Sampled {len(ipa_toponyms)} toponyms with IPA")

    ipa_results = []
    for i, toponym in enumerate(ipa_toponyms):
        print(f"  [{i + 1}/{len(ipa_toponyms)}] {toponym.name} ({toponym.lang})")
        neighbours = find_neighbours(es, index, toponym, k_neighbours)
        ipa_results.append((toponym, neighbours))

        table = generate_latex_table(toponym, neighbours, i + 1, "ipa")
        latex_parts.append(table)

    # Summary for IPA series
    summary_ipa = generate_summary_table(ipa_results, "ipa")
    latex_parts.append(summary_ipa)

    # Series 2: Without IPA
    print()
    print("=" * 60)
    print("SERIES 2: Toponyms WITHOUT IPA")
    print("=" * 60)

    latex_parts.append(r"\subsection*{Series 2: Toponyms without IPA}")
    latex_parts.append("")

    # Use different seed for second series
    seed2 = (seed + 12345) if seed else None
    noipa_toponyms = get_random_toponyms(es, index, n_samples, with_ipa=False, seed=seed2)
    print(f"Sampled {len(noipa_toponyms)} toponyms without IPA")

    noipa_results = []
    for i, toponym in enumerate(noipa_toponyms):
        print(f"  [{i + 1}/{len(noipa_toponyms)}] {toponym.name} ({toponym.lang})")
        neighbours = find_neighbours(es, index, toponym, k_neighbours)
        noipa_results.append((toponym, neighbours))

        table = generate_latex_table(toponym, neighbours, i + 1, "noipa")
        latex_parts.append(table)

    # Summary for no-IPA series
    summary_noipa = generate_summary_table(noipa_results, "noipa")
    latex_parts.append(summary_noipa)

    # Combined comparison table
    latex_parts.append(generate_comparison_table(ipa_results, noipa_results))

    # Write output
    latex_content = "\n".join(latex_parts)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(latex_content)

    print()
    print("=" * 60)
    print(f"Output written to: {output_path}")
    print("=" * 60)


def run_cross_script_evaluation(
        es_host: str,
        index: str,
        n_samples: int,
        k_neighbours: int,
        output_path: str,
        seed: int = None,
        scripts: List[str] = None
):
    """Run cross-script evaluation, sampling from specific scripts."""

    # Default scripts to evaluate
    if scripts is None:
        scripts = ['cyrillic', 'greek', 'arabic', 'hebrew', 'cjk']

    # Normalize ES host
    if not es_host.startswith(('http://', 'https://')):
        es_host = f'http://{es_host}'

    es = Elasticsearch([es_host], request_timeout=60)

    if not es.ping():
        print(f"ERROR: Cannot connect to Elasticsearch at {es_host}")
        sys.exit(1)

    print(f"Connected to Elasticsearch at {es_host}")
    print(f"Index: {index}")
    print(f"Scripts to evaluate: {scripts}")
    print(f"Samples per script: {n_samples}")
    print(f"Neighbours per query: {k_neighbours}")
    print()

    # Refresh index
    print("Refreshing index...")
    es.indices.refresh(index=index)

    latex_parts = []

    latex_parts.append(r"""% =============================================================================
% CROSS-SCRIPT EMBEDDING EVALUATION
% Generated by testing.evaluate_embeddings --cross-script
% =============================================================================

% Required packages:
% \usepackage{booktabs}
% \usepackage{colortbl}
% \usepackage{xcolor}

""")

    all_results = []

    for script in scripts:
        print("=" * 60)
        print(f"SCRIPT: {script.upper()}")
        print("=" * 60)

        latex_parts.append(rf"\subsection*{{Queries in {script.title()} script}}")
        latex_parts.append("")

        script_seed = (seed + hash(script)) if seed else None
        toponyms = get_toponyms_by_script(es, index, script, n_samples, seed=script_seed)

        if not toponyms:
            print(f"  No toponyms found for script: {script}")
            continue

        print(f"  Sampled {len(toponyms)} toponyms")

        for i, toponym in enumerate(toponyms):
            print(f"    [{i + 1}/{len(toponyms)}] {toponym.name} ({toponym.lang}, {toponym.script})")
            neighbours = find_neighbours(es, index, toponym, k_neighbours)
            all_results.append((toponym, neighbours))

            # Count cross-script neighbours
            cross_count = sum(1 for n in neighbours if n.script != toponym.script)
            print(f"      -> {cross_count}/{len(neighbours)} cross-script neighbours")

            table = generate_cross_script_table(toponym, neighbours, len(all_results))
            latex_parts.append(table)

        print()

    # Summary table
    if all_results:
        latex_parts.append(generate_cross_script_summary(all_results))

    # Write output
    latex_content = "\n".join(latex_parts)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(latex_content)

    print("=" * 60)
    print(f"Output written to: {output_path}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description='Evaluate phonetic embeddings via kNN search in Elasticsearch',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--es-host', default=ES_HOST,
                        help=f'Elasticsearch host (default: {ES_HOST})')
    parser.add_argument('--index', default='toponyms',
                        help='Index name (default: toponyms)')
    parser.add_argument('-n', '--samples', type=int, default=10,
                        help='Number of random samples per series (default: 10)')
    parser.add_argument('-k', '--neighbours', type=int, default=15,
                        help='Number of neighbours to retrieve (default: 15)')
    parser.add_argument('-o', '--output', default='article/embedding-evaluation.tex',
                        help='Output LaTeX file (default: article/embedding-evaluation.tex)')
    parser.add_argument('--seed', type=int, default=None,
                        help='Random seed for reproducibility')

    # Cross-script mode
    parser.add_argument('--cross-script', action='store_true',
                        help='Run cross-script evaluation instead of IPA/non-IPA series')
    parser.add_argument('--scripts', nargs='+',
                        default=['cyrillic', 'greek', 'arabic', 'hebrew', 'cjk'],
                        help='Scripts to evaluate in cross-script mode '
                             '(default: cyrillic greek arabic hebrew cjk)')

    args = parser.parse_args()

    if args.cross_script:
        run_cross_script_evaluation(
            es_host=args.es_host,
            index=args.index,
            n_samples=args.samples,
            k_neighbours=args.neighbours,
            output_path=args.output,
            seed=args.seed,
            scripts=args.scripts
        )
    else:
        run_evaluation(
            es_host=args.es_host,
            index=args.index,
            n_samples=args.samples,
            k_neighbours=args.neighbours,
            output_path=args.output,
            seed=args.seed
        )


if __name__ == '__main__':
    main()