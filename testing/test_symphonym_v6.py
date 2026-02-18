#!/usr/bin/env python3
"""
Comprehensive Symphonym v6 Test Suite

Tests the staging ES index with v6 embeddings across multiple dimensions:
- Script coverage and distribution
- Cross-script similarity performance
- Embedding quality metrics
- KNN retrieval accuracy
- Known toponym pair validation

Outputs a JSON report and formatted summary for inclusion in the paper.

Usage:
    python testing/test_symphonym_v6.py
    sbatch -p smp -c 2 -t 1:00:00 --wrap="python testing/test_symphonym_v6.py"
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from elasticsearch import Elasticsearch
from tqdm import tqdm

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# =============================================================================
# ELASTICSEARCH HOST DETECTION
# =============================================================================

def get_es_host():
    """Read ES host from staging environment file."""
    staging_info_file = "/ix1/ishi/esinfo/es-staging.env"

    if not Path(staging_info_file).exists():
        raise EnvironmentError(
            f"Staging ES info file not found: {staging_info_file}\n"
            "Make sure staging ES is running: es -staging-start"
        )

    node = None
    port = None

    with open(staging_info_file) as f:
        for line in f:
            if line.startswith("ES_NODE="):
                node = line.strip().split("=", 1)[1]
            elif line.startswith("ES_PORT="):
                port = line.strip().split("=", 1)[1]

    if not node or not port:
        raise EnvironmentError(
            f"Could not parse ES_NODE or ES_PORT from {staging_info_file}"
        )

    return f"http://{node}:{port}"


# =============================================================================
# TEST DATA: Known Cross-Script Pairs
# =============================================================================

# Format: (name1, lang1, name2, lang2, expected_similarity, description)
CROSS_SCRIPT_PAIRS = [
    # === NON-LATIN TO NON-LATIN PAIRS (most critical for demonstrating cross-script capability) ===

    # Cyrillic-Arabic
    ("Москва", "ru", "موسكو", "ar", 0.75, "Cyrillic-Arabic: Moscow"),
    ("Киев", "uk", "كييف", "ar", 0.70, "Cyrillic-Arabic: Kyiv"),

    # Cyrillic-Greek
    ("Афины", "ru", "Αθήνα", "el", 0.70, "Cyrillic-Greek: Athens"),

    # Cyrillic-Hebrew
    ("Иерусалим", "ru", "ירושלים", "he", 0.65, "Cyrillic-Hebrew: Jerusalem"),

    # Greek-Arabic
    ("Αθήνα", "el", "أثينا", "ar", 0.70, "Greek-Arabic: Athens"),

    # Arabic-Hebrew (both abjads, challenging)
    ("القدس", "ar", "ירושלים", "he", 0.60, "Arabic-Hebrew: Jerusalem"),

    # CJK-Cyrillic
    ("北京", "zh", "Пекин", "ru", 0.75, "CJK-Cyrillic: Beijing"),
    ("东京", "zh", "Токио", "ru", 0.70, "CJK-Cyrillic: Tokyo"),

    # CJK-Arabic
    ("上海", "zh", "شانغهاي", "ar", 0.70, "CJK-Arabic: Shanghai"),

    # Hangul-Cyrillic
    ("서울", "ko", "Сеул", "ru", 0.75, "Hangul-Cyrillic: Seoul"),

    # === LATIN-INVOLVED PAIRS (for comparison, but not primary showcase) ===

    # Latin-Cyrillic
    ("London", "en", "Лондон", "ru", 0.85, "Latin-Cyrillic: London"),
    ("Moscow", "en", "Москва", "ru", 0.85, "Latin-Cyrillic: Moscow"),

    # Latin-Greek
    ("Athens", "en", "Αθήνα", "el", 0.75, "Latin-Greek: Athens"),

    # Latin-Arabic
    ("Damascus", "en", "دمشق", "ar", 0.75, "Latin-Arabic: Damascus"),
    ("Cairo", "en", "القاهرة", "ar", 0.70, "Latin-Arabic: Cairo"),

    # Latin-Hebrew
    ("Jerusalem", "en", "ירושלים", "he", 0.70, "Latin-Hebrew: Jerusalem"),

    # Latin-CJK
    ("Beijing", "en", "北京", "zh", 0.80, "Latin-CJK: Beijing"),
    ("Tokyo", "en", "東京", "ja", 0.80, "Latin-CJK: Tokyo"),

    # Latin-Hangul
    ("Seoul", "en", "서울", "ko", 0.80, "Latin-Hangul: Seoul"),

    # === NEGATIVE EXAMPLES (unrelated toponyms across scripts) ===
    ("Лондон", "ru", "東京", "ja", 0.30, "Unrelated: London vs Tokyo (Cyrillic-CJK)"),
    ("Αθήνα", "el", "القاهرة", "ar", 0.30, "Unrelated: Athens vs Cairo (Greek-Arabic)"),
    ("北京", "zh", "תל אביב", "he", 0.30, "Unrelated: Beijing vs Tel Aviv (CJK-Hebrew)"),
]

# Diacritic variants (should be VERY HIGH similarity)
DIACRITIC_PAIRS = [
    ("Zurich", "de", "Zürich", "de", 0.90, "Umlaut variant"),
    ("Krakow", "pl", "Kraków", "pl", 0.90, "Polish diacritic"),
    ("Sao Paulo", "pt", "São Paulo", "pt", 0.90, "Portuguese tilde"),
    ("Bogota", "es", "Bogotá", "es", 0.90, "Spanish accent"),
]


# =============================================================================
# ELASTICSEARCH QUERY HELPERS
# =============================================================================

def get_index_stats(es: Elasticsearch, index: str = 'toponyms') -> dict:
    """Get basic index statistics."""
    stats = es.count(index=index)
    total_docs = stats['count']

    # Count documents with embeddings
    with_embedding = es.count(
        index=index,
        body={'query': {'exists': {'field': 'embedding'}}}
    )['count']

    return {
        'total_toponyms': total_docs,
        'with_embedding': with_embedding,
        'embedding_coverage_pct': (with_embedding / total_docs * 100) if total_docs > 0 else 0
    }


def get_script_distribution(es: Elasticsearch, index: str = 'toponyms') -> dict:
    """Get distribution of toponyms by script."""
    aggs = es.search(
        index=index,
        size=0,
        body={
            'aggs': {
                'by_script': {
                    'terms': {'field': 'script', 'size': 30}
                }
            }
        }
    )

    distribution = {}
    for bucket in aggs['aggregations']['by_script']['buckets']:
        distribution[bucket['key']] = bucket['doc_count']

    return distribution


def get_embedding_coverage_by_script(es: Elasticsearch, index: str = 'toponyms') -> dict:
    """Get embedding coverage percentage by script."""
    # Get all scripts
    scripts = get_script_distribution(es, index)

    coverage = {}
    for script in scripts.keys():
        total = es.count(
            index=index,
            body={'query': {'term': {'script': script}}}
        )['count']

        with_emb = es.count(
            index=index,
            body={
                'query': {
                    'bool': {
                        'must': [
                            {'term': {'script': script}},
                            {'exists': {'field': 'embedding'}}
                        ]
                    }
                }
            }
        )['count']

        coverage[script] = {
            'total': total,
            'with_embedding': with_emb,
            'coverage_pct': (with_emb / total * 100) if total > 0 else 0
        }

    return coverage


def find_toponym(es: Elasticsearch, name: str, lang: str = None, index: str = 'toponyms') -> dict:
    """Find a toponym by name and optional language."""
    query = {
        'bool': {
            'must': [
                {'term': {'name.keyword': name}}
            ]
        }
    }

    if lang:
        query['bool']['must'].append({'term': {'lang': lang}})

    # Also require embedding
    query['bool']['must'].append({'exists': {'field': 'embedding'}})

    result = es.search(
        index=index,
        body={'query': query, 'size': 1}
    )

    if result['hits']['total']['value'] > 0:
        return result['hits']['hits'][0]['_source']
    return None


def cosine_similarity(vec1: list, vec2: list) -> float:
    """Compute cosine similarity between two int8 embedding vectors."""
    # Convert int8 to float and dequantize
    v1 = np.array(vec1, dtype=np.float32) / 127.0
    v2 = np.array(vec2, dtype=np.float32) / 127.0

    # Cosine similarity
    dot = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return float(dot / (norm1 * norm2))


def knn_search(es: Elasticsearch, embedding: list, k: int = 100, index: str = 'toponyms') -> list:
    """Perform KNN search with an embedding vector and aggregate by name.

    Returns list of dicts with:
        - name: the toponym string
        - variants: list of (lang, script) tuples for this name
        - max_score: highest score for any variant of this name
    """
    result = es.search(
        index=index,
        body={
            'knn': {
                'field': 'embedding',
                'query_vector': embedding,
                'k': k,
                'num_candidates': k * 10
            },
            '_source': ['name', 'lang', 'script'],
            'size': k
        }
    )

    # Aggregate results by name
    by_name = defaultdict(lambda: {'variants': [], 'max_score': 0})

    for hit in result['hits']['hits']:
        name = hit['_source']['name']
        lang = hit['_source'].get('lang')
        script = hit['_source'].get('script')
        score = hit['_score']

        by_name[name]['variants'].append((lang, script))
        by_name[name]['max_score'] = max(by_name[name]['max_score'], score)

    # Convert to sorted list
    aggregated = []
    for name, data in by_name.items():
        aggregated.append({
            'name': name,
            'variants': data['variants'],
            'max_score': data['max_score']
        })

    # Sort by max score descending
    aggregated.sort(key=lambda x: x['max_score'], reverse=True)

    return aggregated


# =============================================================================
# TEST FUNCTIONS
# =============================================================================

def test_cross_script_pairs(es: Elasticsearch, pairs: list, index: str = 'toponyms') -> dict:
    """Test known cross-script pairs for similarity."""
    results = []

    logger.info(f"Testing {len(pairs)} cross-script pairs...")

    for name1, lang1, name2, lang2, expected, description in tqdm(pairs, desc="Cross-script pairs"):
        # Find both toponyms
        doc1 = find_toponym(es, name1, lang1, index)
        doc2 = find_toponym(es, name2, lang2, index)

        if not doc1:
            results.append({
                'name1': name1, 'lang1': lang1,
                'name2': name2, 'lang2': lang2,
                'expected': expected,
                'actual': None,
                'status': 'MISSING_1',
                'description': description
            })
            continue

        if not doc2:
            results.append({
                'name1': name1, 'lang1': lang1,
                'name2': name2, 'lang2': lang2,
                'expected': expected,
                'actual': None,
                'status': 'MISSING_2',
                'description': description
            })
            continue

        # Compute similarity
        similarity = cosine_similarity(doc1['embedding'], doc2['embedding'])

        # Determine pass/fail
        status = 'PASS' if similarity >= expected else 'FAIL'

        results.append({
            'name1': name1, 'lang1': lang1,
            'name2': name2, 'lang2': lang2,
            'expected': expected,
            'actual': round(similarity, 4),
            'status': status,
            'description': description
        })

    # Summary stats
    total = len(results)
    passed = sum(1 for r in results if r['status'] == 'PASS')
    failed = sum(1 for r in results if r['status'] == 'FAIL')
    missing = sum(1 for r in results if r['status'].startswith('MISSING'))

    return {
        'results': results,
        'summary': {
            'total': total,
            'passed': passed,
            'failed': failed,
            'missing': missing,
            'pass_rate': (passed / (total - missing) * 100) if (total - missing) > 0 else 0
        }
    }


def test_knn_retrieval(es: Elasticsearch, index: str = 'toponyms') -> dict:
    """Test KNN retrieval with similarity threshold analysis.

    Instead of checking top-K ranking, verify that:
    1. Expected cross-script variants appear above similarity thresholds
    2. Higher-scoring results are legitimate near-synonyms
    3. Anomalous results are flagged as potential data quality issues
    """
    test_cases = [
        {
            "query": "Beijing",
            "lang": "en",
            "expected_variants": [
                {"name": "北京", "min_similarity": 0.80, "type": "CJK"},
                {"name": "Пекин", "min_similarity": 0.70, "type": "Cyrillic"},
                {"name": "بكين", "min_similarity": 0.65, "type": "Arabic"}
            ],
            "description": "Multi-script international city name"
        },
        {
            "query": "Athens",
            "lang": "en",
            "expected_variants": [
                {"name": "Αθήνα", "min_similarity": 0.75, "type": "Greek"},
                {"name": "Афины", "min_similarity": 0.70, "type": "Cyrillic"},
                {"name": "أثينا", "min_similarity": 0.65, "type": "Arabic"}
            ],
            "description": "Ancient city with multiple script variants"
        },
        {
            "query": "Jerusalem",
            "lang": "en",
            "expected_variants": [
                {"name": "ירושלים", "min_similarity": 0.70, "type": "Hebrew"},
                {"name": "القدس", "min_similarity": 0.65, "type": "Arabic"},
                {"name": "Ιερουσαλήμ", "min_similarity": 0.65, "type": "Greek"}
            ],
            "description": "Multi-cultural city with diverse name variants"
        }
    ]

    results = []

    logger.info(f"Testing KNN retrieval with threshold analysis on {len(test_cases)} cases...")

    for case in tqdm(test_cases, desc="KNN retrieval"):
        # Find query toponym
        query_doc = find_toponym(es, case["query"], case["lang"], index)

        if not query_doc:
            results.append({
                'query': f"{case['query']} ({case['lang']})",
                'status': 'MISSING_QUERY',
                'variants_found': [],
                'high_scoring_results': [],
                'data_quality_issues': [],
                'description': case["description"]
            })
            continue

        # Perform KNN search with large k to get comprehensive results
        knn_results = knn_search(es, query_doc['embedding'], k=200, index=index)

        # Build name -> similarity map for easier lookup
        name_to_info = {item['name']: item for item in knn_results}

        # Check for expected variants with thresholds
        # Build set of expected names for fast lookup
        expected_names = {v["name"] for v in case["expected_variants"]}

        variants_found = []
        for variant in case["expected_variants"]:
            if variant["name"] in name_to_info:
                info = name_to_info[variant["name"]]
                # Convert ES score to cosine similarity (approximate)
                # ES returns higher scores for more similar items, normalize to 0-1 range
                similarity = info['max_score'] / (knn_results[0]['max_score'] if knn_results else 1.0)

                variants_found.append({
                    "name": variant["name"],
                    "type": variant["type"],
                    "similarity": round(similarity, 4),
                    "threshold": variant["min_similarity"],
                    "status": "PASS" if similarity >= variant["min_similarity"] else "BELOW_THRESHOLD",
                    "langs": [f"{lang}:{script}" for lang, script in info['variants'][:5]]
                })
            else:
                variants_found.append({
                    "name": variant["name"],
                    "type": variant["type"],
                    "similarity": None,
                    "threshold": variant["min_similarity"],
                    "status": "NOT_FOUND",
                    "langs": []
                })

        # Analyze high-scoring results (excluding exact query match)
        query_lower = case["query"].lower()
        high_scoring = [item for item in knn_results[:20] if item['name'].lower() != query_lower]

        high_scoring_results = []
        data_quality_issues = []

        for item in high_scoring[:15]:
            similarity = item['max_score'] / (knn_results[0]['max_score'] if knn_results else 1.0)

            variant_strs = [f"{lang}:{script}" for lang, script in item['variants'][:3]]
            if len(item['variants']) > 3:
                variant_strs.append(f"+{len(item['variants'])-3} more")

            result_entry = {
                "name": item['name'],
                "similarity": round(similarity, 4),
                "variants": variant_strs,
                "is_expected": item['name'] in expected_names
            }
            high_scoring_results.append(result_entry)

            # Flag potential data quality issues
            name_len = len(item['name'])
            has_latin = any(c.isascii() and c.isalpha() for c in item['name'])

            # Issue 1: Suspiciously long names (>50 chars) with high similarity
            if similarity > 0.6 and name_len > 50:
                data_quality_issues.append({
                    "name": item['name'],
                    "similarity": round(similarity, 4),
                    "issue_type": "suspiciously_long",
                    "details": f"Name length: {name_len} characters",
                    "langs": variant_strs[:2]
                })

            # Issue 2: Non-Latin names tagged as both en and nl (unlikely combination)
            langs_in_variants = [lang for lang, _ in item['variants']]
            if not has_latin and "en" in langs_in_variants and "nl" in langs_in_variants:
                data_quality_issues.append({
                    "name": item['name'],
                    "similarity": round(similarity, 4),
                    "issue_type": "implausible_lang_tags",
                    "details": "Non-Latin name tagged as both en and nl",
                    "langs": variant_strs[:2]
                })

            # Issue 3: Names containing obvious data errors (e.g., "School", "Station" in unexpected languages)
            problematic_words = ["School", "Station", "Hospital", "University", "College", "F P ", "Pry "]
            if any(word in item['name'] for word in problematic_words):
                is_expected = item['name'] in expected_names
                if similarity > 0.5 and not is_expected:
                    data_quality_issues.append({
                        "name": item['name'],
                        "similarity": round(similarity, 4),
                        "issue_type": "institutional_name_anomaly",
                        "details": "Contains institution keywords (likely OSM data quality issue)",
                        "langs": variant_strs[:2]
                    })

        # Determine overall status
        passed_variants = sum(1 for v in variants_found if v["status"] == "PASS")
        total_expected = len(case["expected_variants"])

        if passed_variants == total_expected:
            status = "PASS"
        elif passed_variants > 0:
            status = "PARTIAL"
        else:
            status = "FAIL"

        results.append({
            'query': f"{case['query']} ({case['lang']})",
            'status': status,
            'variants_found': variants_found,
            'high_scoring_results': high_scoring_results[:10],
            'data_quality_issues': data_quality_issues,
            'description': case["description"]
        })

    passed = sum(1 for r in results if r["status"] == "PASS")
    partial = sum(1 for r in results if r["status"] == "PARTIAL")

    return {
        'results': results,
        'summary': {
            'total': len(results),
            'passed': passed,
            'partial': partial,
            'pass_rate': (passed / len(results) * 100) if results else 0
        }
    }


def test_embedding_statistics(es: Elasticsearch, index: str = 'toponyms', sample_size: int = 10000) -> dict:
    """Compute embedding quality statistics on a sample."""
    logger.info(f"Sampling {sample_size} embeddings for statistics...")

    # Random sample with embeddings
    result = es.search(
        index=index,
        body={
            'query': {
                'function_score': {
                    'query': {'exists': {'field': 'embedding'}},
                    'random_score': {}
                }
            },
            '_source': ['embedding'],
            'size': sample_size
        }
    )

    embeddings = []
    for hit in result['hits']['hits']:
        emb = np.array(hit['_source']['embedding'], dtype=np.float32) / 127.0
        embeddings.append(emb)

    embeddings = np.array(embeddings)

    # Compute statistics
    norms = np.linalg.norm(embeddings, axis=1)

    # Pairwise similarities (sample for efficiency)
    n_pairs = min(1000, len(embeddings))
    sample_indices = np.random.choice(len(embeddings), n_pairs, replace=False)
    sample_embs = embeddings[sample_indices]

    similarities = []
    for i in range(len(sample_embs)):
        for j in range(i + 1, len(sample_embs)):
            sim = np.dot(sample_embs[i], sample_embs[j]) / (
                np.linalg.norm(sample_embs[i]) * np.linalg.norm(sample_embs[j])
            )
            similarities.append(sim)

    similarities = np.array(similarities)

    return {
        'sample_size': len(embeddings),
        'embedding_dim': embeddings.shape[1],
        'norm_stats': {
            'mean': float(norms.mean()),
            'std': float(norms.std()),
            'min': float(norms.min()),
            'max': float(norms.max())
        },
        'pairwise_similarity_stats': {
            'mean': float(similarities.mean()),
            'std': float(similarities.std()),
            'min': float(similarities.min()),
            'max': float(similarities.max()),
            'median': float(np.median(similarities))
        }
    }


# =============================================================================
# MAIN TEST RUNNER
# =============================================================================

def run_all_tests(es_host: str, index: str = 'toponyms', output_file: str = None):
    """Run all tests and generate report."""
    logger.info("="*70)
    logger.info("SYMPHONYM v6 TEST SUITE")
    logger.info("="*70)
    logger.info(f"ES Host: {es_host}")
    logger.info(f"Index: {index}")
    logger.info("")

    es = Elasticsearch(es_host, request_timeout=60)

    # Check connection
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {es_host}")
        sys.exit(1)

    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'es_host': es_host,
        'index': index,
        'tests': {}
    }

    # Test 1: Index Statistics
    logger.info("Test 1: Index Statistics")
    stats = get_index_stats(es, index)
    report['tests']['index_stats'] = stats
    logger.info(f"  Total toponyms: {stats['total_toponyms']:,}")
    logger.info(f"  With embeddings: {stats['with_embedding']:,} ({stats['embedding_coverage_pct']:.1f}%)")
    logger.info("")

    # Test 2: Script Distribution
    logger.info("Test 2: Script Distribution")
    script_dist = get_script_distribution(es, index)
    report['tests']['script_distribution'] = script_dist
    for script, count in sorted(script_dist.items(), key=lambda x: -x[1])[:10]:
        logger.info(f"  {script:15s}: {count:,}")
    logger.info("")

    # Test 3: Embedding Coverage by Script
    logger.info("Test 3: Embedding Coverage by Script")
    coverage = get_embedding_coverage_by_script(es, index)
    report['tests']['embedding_coverage_by_script'] = coverage
    for script, data in sorted(coverage.items(), key=lambda x: -x[1]['coverage_pct']):
        logger.info(f"  {script:15s}: {data['coverage_pct']:5.1f}% ({data['with_embedding']:,} / {data['total']:,})")
    logger.info("")

    # Test 4: Cross-Script Pair Similarity
    logger.info("Test 4: Cross-Script Pair Similarity")
    pair_results = test_cross_script_pairs(es, CROSS_SCRIPT_PAIRS, index)
    report['tests']['cross_script_pairs'] = pair_results
    logger.info(f"  Pass rate: {pair_results['summary']['pass_rate']:.1f}% ({pair_results['summary']['passed']}/{pair_results['summary']['total'] - pair_results['summary']['missing']})")
    logger.info(f"  Failed: {pair_results['summary']['failed']}, Missing: {pair_results['summary']['missing']}")
    logger.info("")

    # Test 5: Diacritic Variants
    logger.info("Test 5: Diacritic Variant Similarity")
    diacritic_results = test_cross_script_pairs(es, DIACRITIC_PAIRS, index)
    report['tests']['diacritic_variants'] = diacritic_results
    logger.info(f"  Pass rate: {diacritic_results['summary']['pass_rate']:.1f}% ({diacritic_results['summary']['passed']}/{diacritic_results['summary']['total'] - diacritic_results['summary']['missing']})")
    logger.info("")

    # Test 6: KNN Retrieval
    logger.info("Test 6: KNN Retrieval Accuracy")
    knn_results = test_knn_retrieval(es, index)
    report['tests']['knn_retrieval'] = knn_results
    logger.info(f"  Pass rate: {knn_results['summary']['pass_rate']:.1f}% ({knn_results['summary']['passed']}/{knn_results['summary']['total']})")
    logger.info("")

    # Test 7: Embedding Statistics
    logger.info("Test 7: Embedding Quality Statistics")
    emb_stats = test_embedding_statistics(es, index)
    report['tests']['embedding_statistics'] = emb_stats
    logger.info(f"  Sample size: {emb_stats['sample_size']:,}")
    logger.info(f"  Norm: {emb_stats['norm_stats']['mean']:.4f} ± {emb_stats['norm_stats']['std']:.4f}")
    logger.info(f"  Pairwise similarity: {emb_stats['pairwise_similarity_stats']['mean']:.4f} ± {emb_stats['pairwise_similarity_stats']['std']:.4f}")
    logger.info("")

    # Save report
    if output_file:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)
        logger.info(f"Report saved to: {output_path}")

    logger.info("="*70)
    logger.info("TEST SUITE COMPLETE")
    logger.info("="*70)

    return report


def main():
    parser = argparse.ArgumentParser(description="Symphonym v6 comprehensive test suite")
    parser.add_argument('--es-host', default=None,
                        help='Elasticsearch host (defaults to staging ES from /ix1/ishi/esinfo/es-staging.env)')
    parser.add_argument('--index', default='toponyms', help='Index name')
    parser.add_argument('--output', default='testing/symphonym_v6_test_report.json',
                        help='Output JSON report file')

    args = parser.parse_args()

    # Determine ES host
    if args.es_host:
        es_host = args.es_host
    else:
        try:
            es_host = get_es_host()
        except EnvironmentError as e:
            logger.error(str(e))
            sys.exit(1)

    run_all_tests(es_host, args.index, args.output)


if __name__ == '__main__':
    main()

