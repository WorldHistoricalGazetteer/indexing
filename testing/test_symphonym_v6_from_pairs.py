#!/usr/bin/env python3
"""
Symphonym v6 Test Suite - Using Real Training Pairs

Tests the staging ES index with v6 embeddings by sampling actual
cross-script pairs from the Phase 1 training data.

This ensures we test real pairs that:
1. Actually exist in the ES index
2. Were used in training
3. Represent authentic cross-script phonetic similarity

Usage:
    python testing/test_symphonym_v6_from_pairs.py
    sbatch -p smp -c 2 -t 1:00:00 --wrap="python testing/test_symphonym_v6_from_pairs.py"
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
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
# SAMPLE TRAINING PAIRS
# =============================================================================

def sample_cross_script_pairs(pairs_file: str, n_samples: int = 50) -> list:
    """Sample cross-script pairs from the training data.

    Returns list of (name1, lang1, script1, name2, lang2, script2) tuples
    where script1 != script2
    """
    logger.info(f"Reading pairs from {pairs_file}...")

    table = pq.read_table(pairs_file)
    df = table.to_pandas()

    logger.info(f"Total pairs: {len(df):,}")

    # Parse bin into script pairs
    df['script1'] = df['bin'].str.split('|').str[0].str.split(':').str[1]
    df['script2'] = df['bin'].str.split('|').str[1].str.split(':').str[1]

    # Filter for cross-script pairs
    cross_script = df[df['script1'] != df['script2']].copy()
    logger.info(f"Cross-script pairs: {len(cross_script):,}")

    # Get language info
    cross_script['lang1'] = cross_script['bin'].str.split('|').str[0].str.split(':').str[0]
    cross_script['lang2'] = cross_script['bin'].str.split('|').str[1].str.split(':').str[0]

    # Sample diverse bins
    bin_counts = cross_script['bin'].value_counts()
    logger.info(f"Total bins: {len(bin_counts)}")

    # Sample from top bins (most common pairs) and also some rare ones
    top_bins = bin_counts.head(30).index.tolist()
    rare_bins = bin_counts.tail(20).index.tolist()
    sample_bins = top_bins + rare_bins

    sampled = []
    for bin_name in sample_bins[:n_samples]:
        bin_pairs = cross_script[cross_script['bin'] == bin_name]
        if len(bin_pairs) > 0:
            # Take first pair from each bin
            row = bin_pairs.iloc[0]
            sampled.append({
                'name1': row['name1'],
                'lang1': row['lang1'],
                'script1': row['script1'],
                'name2': row['name2'],
                'lang2': row['lang2'],
                'script2': row['script2'],
                'bin': row['bin']
            })

    logger.info(f"Sampled {len(sampled)} cross-script pairs")
    return sampled


# =============================================================================
# ELASTICSEARCH HELPERS
# =============================================================================

def find_toponym(es: Elasticsearch, name: str, lang: str = None, script: str = None, index: str = 'toponyms') -> dict:
    """Find a toponym by name, language, and script."""
    query = {
        'bool': {
            'must': [
                {'term': {'name.keyword': name}},
                {'exists': {'field': 'embedding'}}
            ]
        }
    }

    if lang:
        query['bool']['must'].append({'term': {'lang': lang}})
    if script:
        query['bool']['must'].append({'term': {'script': script}})

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


# =============================================================================
# TEST FUNCTIONS
# =============================================================================

def test_training_pairs(es: Elasticsearch, pairs: list, index: str = 'toponyms') -> dict:
    """Test cross-script pairs sampled from training data."""
    results = []

    logger.info(f"Testing {len(pairs)} training pairs...")

    for pair in tqdm(pairs, desc="Testing pairs"):
        # Find both toponyms
        doc1 = find_toponym(es, pair['name1'], pair['lang1'], pair['script1'], index)
        doc2 = find_toponym(es, pair['name2'], pair['lang2'], pair['script2'], index)

        if not doc1:
            results.append({
                'name1': pair['name1'], 'lang1': pair['lang1'], 'script1': pair['script1'],
                'name2': pair['name2'], 'lang2': pair['lang2'], 'script2': pair['script2'],
                'bin': pair['bin'],
                'similarity': None,
                'status': 'MISSING_1'
            })
            continue

        if not doc2:
            results.append({
                'name1': pair['name1'], 'lang1': pair['lang1'], 'script1': pair['script1'],
                'name2': pair['name2'], 'lang2': pair['lang2'], 'script2': pair['script2'],
                'bin': pair['bin'],
                'similarity': None,
                'status': 'MISSING_2'
            })
            continue

        # Compute similarity
        similarity = cosine_similarity(doc1['embedding'], doc2['embedding'])

        # Set threshold based on script pair difficulty
        # Non-Latin to Non-Latin: more challenging, lower threshold
        # Latin-involved: higher threshold expected
        script_pair = f"{pair['script1']}-{pair['script2']}"

        if 'LATIN' not in script_pair:
            # Non-Latin to Non-Latin pairs (most challenging)
            if 'HEBREW' in script_pair or 'ARABIC' in script_pair:
                threshold = 0.65  # Abjads are hardest
            else:
                threshold = 0.70
        else:
            # Latin-involved pairs
            if 'HEBREW' in script_pair or 'ARABIC' in script_pair:
                threshold = 0.70
            else:
                threshold = 0.75

        status = 'PASS' if similarity >= threshold else 'FAIL'

        results.append({
            'name1': pair['name1'], 'lang1': pair['lang1'], 'script1': pair['script1'],
            'name2': pair['name2'], 'lang2': pair['lang2'], 'script2': pair['script2'],
            'bin': pair['bin'],
            'similarity': round(similarity, 4),
            'threshold': threshold,
            'status': status
        })

    # Summary stats
    total = len(results)
    passed = sum(1 for r in results if r['status'] == 'PASS')
    failed = sum(1 for r in results if r['status'] == 'FAIL')
    missing = sum(1 for r in results if r['status'].startswith('MISSING'))

    # Analyze by script pair type
    by_script_pair = defaultdict(list)
    for r in results:
        if r['status'] not in ['MISSING_1', 'MISSING_2']:
            pair_key = f"{r['script1']}-{r['script2']}"
            by_script_pair[pair_key].append(r['similarity'])

    script_pair_stats = {}
    for pair_key, sims in by_script_pair.items():
        script_pair_stats[pair_key] = {
            'count': len(sims),
            'mean': float(np.mean(sims)),
            'std': float(np.std(sims)),
            'min': float(np.min(sims)),
            'max': float(np.max(sims))
        }

    return {
        'results': results,
        'summary': {
            'total': total,
            'passed': passed,
            'failed': failed,
            'missing': missing,
            'pass_rate': (passed / (total - missing) * 100) if (total - missing) > 0 else 0
        },
        'by_script_pair': script_pair_stats
    }


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


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Symphonym v6 test suite using real training pairs")
    parser.add_argument('--es-host', default=None,
                        help='Elasticsearch host (defaults to staging ES)')
    parser.add_argument('--index', default='toponyms', help='Index name')
    parser.add_argument('--pairs-file',
                        default='/ix1/ishi/models/phonetic/data/v6/pairs/positive_pairs.parquet',
                        help='Path to positive pairs parquet file')
    parser.add_argument('--n-samples', type=int, default=50,
                        help='Number of cross-script pairs to sample')
    parser.add_argument('--output', default='testing/symphonym_v6_pairs_test_report.json',
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

    logger.info("="*70)
    logger.info("SYMPHONYM v6 TEST SUITE - REAL TRAINING PAIRS")
    logger.info("="*70)
    logger.info(f"ES Host: {es_host}")
    logger.info(f"Index: {args.index}")
    logger.info(f"Pairs file: {args.pairs_file}")
    logger.info("")

    # Sample pairs
    pairs = sample_cross_script_pairs(args.pairs_file, args.n_samples)

    # Connect to ES
    es = Elasticsearch(es_host, request_timeout=60)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {es_host}")
        sys.exit(1)

    # Test pairs
    results = test_training_pairs(es, pairs, args.index)

    # Get index stats for context
    stats = get_index_stats(es, args.index)

    # Build report
    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'es_host': es_host,
        'index': args.index,
        'pairs_file': args.pairs_file,
        'n_samples': args.n_samples,
        'index_stats': stats,
        'cross_script_pairs_test': results
    }

    # Save report
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)
    logger.info(f"Report saved to: {output_path}")

    # Print summary
    logger.info("")
    logger.info("="*70)
    logger.info("TEST SUMMARY")
    logger.info("="*70)
    logger.info(f"Total pairs tested: {results['summary']['total']}")
    logger.info(f"Passed: {results['summary']['passed']}")
    logger.info(f"Failed: {results['summary']['failed']}")
    logger.info(f"Missing: {results['summary']['missing']}")
    logger.info(f"Pass rate: {results['summary']['pass_rate']:.1f}%")
    logger.info("")
    logger.info("By script pair:")
    for pair_key, stats in sorted(results['by_script_pair'].items(), key=lambda x: -x[1]['mean']):
        logger.info(f"  {pair_key:30s}: {stats['mean']:.4f} ± {stats['std']:.4f} (n={stats['count']})")
    logger.info("="*70)


if __name__ == '__main__':
    main()

