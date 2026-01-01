#!/usr/bin/env python3
"""
Embedding Similarity Evaluation for Phonetic Model

Samples random toponyms from Elasticsearch and finds their k-nearest neighbours
using cosine similarity on the embedding field. Generates LaTeX tables suitable
for inclusion in academic papers.

Runs two separate test series:
1. Toponyms WITH IPA (phonetically grounded embeddings)
2. Toponyms WITHOUT IPA (character-only embeddings)

Usage:
    python -m testing.evaluate_embeddings \
        --es-host localhost:9200 \
        --index toponyms \
        --samples 10 \
        --neighbours 15 \
        --output article/embedding-evaluation.tex
"""

import argparse
import random
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

from elasticsearch import Elasticsearch

from processing.settings import ES_HOST


@dataclass
class Toponym:
    """Represents a toponym document from ES."""
    doc_id: str
    name: str
    lang: str
    ipa: Optional[str]
    embedding: List[float]

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
% Generated by phonetics.evaluate_embeddings
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

    args = parser.parse_args()

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