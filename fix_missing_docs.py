#!/usr/bin/env python3
"""
fix_missing_docs.py
-------------------
Query DuckDB for the 5 documents that failed ES indexing due to zero-magnitude
panphon_embedding vectors, and index them to ES (without the embedding field).

These documents were rejected entirely by ES during the full rebuild because
their panphon_embedding was all zeros, which ES cosine similarity refuses.
The fix is to index them without the embedding field at all.

Usage (on remote, after sourcing staging ES environment):
    python3 fix_missing_docs.py [--db-path PATH] [--es-host URL] [--dry-run]
"""

import argparse
import json
import logging
import os
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

import duckdb

try:
    from elasticsearch import Elasticsearch
except ImportError:
    print("ERROR: elasticsearch package required.")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from phonetics.utils.script_detection import get_primary_namespace

try:
    from anyascii import anyascii
except ImportError:
    anyascii = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# The 5 document IDs that failed with zero-magnitude panphon_embedding
FAILED_IDS = [
    'Bolătău@ro',
    'aérodrome de Vanguard Landing@fr',
    'Parish of Dry Lake@en',
    'Jogaria F P@en',
    'Paso Pánuco@es',
]

DEFAULT_DB_PATH = '/ix1/ishi/data/toponyms.db'
DEFAULT_ES_HOST = os.environ.get('ES_HOST', 'http://localhost:9200')


def romanize_for_search(name: str, script: str) -> Optional[str]:
    if anyascii is None:
        return None
    if script == 'LATIN':
        return None
    romanized = anyascii(name).lower().strip()
    if romanized and romanized != name.lower():
        return romanized
    return None


def _embedding_from_packed_features(packed: bytes) -> Optional[List[float]]:
    """Reconstruct 192-dim embedding from packed PanPhon features, returns None if all-zero."""
    if not packed:
        return None
    num_floats = len(packed) // 4
    features_per_segment = 24
    if num_floats < features_per_segment or num_floats % features_per_segment != 0:
        return None
    all_features = struct.unpack(f'{num_floats}f', packed)
    num_segments = num_floats // features_per_segment
    num_bins = 8
    bins = [[0.0] * features_per_segment for _ in range(num_bins)]
    bin_counts = [0] * num_bins
    for seg_idx in range(num_segments):
        position = seg_idx / num_segments
        bin_idx = min(int(position * num_bins), num_bins - 1)
        offset = seg_idx * features_per_segment
        for i in range(features_per_segment):
            bins[bin_idx][i] += all_features[offset + i]
        bin_counts[bin_idx] += 1
    embedding = []
    for bin_idx in range(num_bins):
        if bin_counts[bin_idx] > 0:
            embedding.extend(v / bin_counts[bin_idx] for v in bins[bin_idx])
        else:
            embedding.extend([0.0] * features_per_segment)
    # Reject zero-magnitude vectors (ES cosine similarity requires non-zero magnitude)
    if not any(embedding):
        return None
    return embedding


def build_doc(row) -> dict:
    """Build an ES document from a DuckDB row, omitting zero-magnitude embeddings."""
    toponym_id, name, lang, lang_variant, script, ipa, panphon_features, namespaces_str, attestations_str = row
    namespaces = namespaces_str.split(',') if namespaces_str else []
    attestations = attestations_str.split(',') if attestations_str else []
    primary_ns = get_primary_namespace(namespaces)

    doc = {
        'toponym_id': toponym_id,
        'name': name,
        'lang': lang,
        'lang_variant': lang_variant,
        'script': script,
        'namespaces': namespaces,
        'primary_namespace': primary_ns,
        'attestations': attestations,
        'embedding': None,
        'embedding_version': None,
        'indexed_at': datetime.now(timezone.utc).isoformat(),
    }

    name_romanized = romanize_for_search(name, script)
    if name_romanized:
        doc['name_romanized'] = name_romanized

    if ipa:
        doc['ipa'] = ipa
        if panphon_features:
            embedding = _embedding_from_packed_features(panphon_features)
            if embedding:
                doc['panphon_embedding'] = embedding
            else:
                logger.info(f"  Zero-magnitude embedding omitted for {toponym_id!r} (expected)")

    return doc


def main():
    parser = argparse.ArgumentParser(description='Index 5 missing documents to ES from DuckDB')
    parser.add_argument('--db-path', default=DEFAULT_DB_PATH, help='Path to DuckDB database')
    parser.add_argument('--es-host', default=DEFAULT_ES_HOST, help='Elasticsearch host URL')
    parser.add_argument('--index', default='toponyms', help='ES index name')
    parser.add_argument('--dry-run', action='store_true', help='Print documents without indexing')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("FIX MISSING DOCUMENTS")
    logger.info("=" * 60)
    logger.info(f"DB path:  {args.db_path}")
    logger.info(f"ES host:  {args.es_host}")
    logger.info(f"Index:    {args.index}")
    logger.info(f"Dry run:  {args.dry_run}")
    logger.info(f"Docs:     {len(FAILED_IDS)}")
    logger.info("")

    # Connect to DuckDB
    if not Path(args.db_path).exists():
        logger.error(f"DuckDB not found: {args.db_path}")
        sys.exit(1)

    conn = duckdb.connect(args.db_path, read_only=True)

    # Connect to ES
    if not args.dry_run:
        es = Elasticsearch(args.es_host, max_retries=3, retry_on_timeout=True)
        if not es.ping():
            logger.error(f"Cannot connect to ES at {args.es_host}")
            sys.exit(1)
        logger.info("ES connection: OK")

    # Query DuckDB for each of the 5 documents
    placeholders = ','.join([f"'{tid}'" for tid in FAILED_IDS])
    query = f"""
        SELECT t.toponym_id,
               t.name,
               t.lang,
               t.lang_variant,
               t.script,
               t.ipa,
               t.panphon_features,
               GROUP_CONCAT(DISTINCT tn.namespace) as namespaces,
               GROUP_CONCAT(DISTINCT ta.place_id) as attestations
        FROM toponyms t
        JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
        LEFT JOIN toponym_attestations ta ON t.toponym_id = ta.toponym_id
        WHERE t.toponym_id IN ({placeholders})
        GROUP BY t.toponym_id, t.name, t.lang, t.lang_variant, t.script, t.ipa, t.panphon_features
    """

    rows = conn.execute(query).fetchall()
    conn.close()

    logger.info(f"Found {len(rows)} / {len(FAILED_IDS)} documents in DuckDB")
    if len(rows) < len(FAILED_IDS):
        found_ids = {row[0] for row in rows}
        missing = [tid for tid in FAILED_IDS if tid not in found_ids]
        logger.warning(f"Not found in DuckDB: {missing}")
    logger.info("")

    # Build and index each document
    indexed = 0
    for row in rows:
        toponym_id = row[0]
        logger.info(f"Processing: {toponym_id!r}")

        doc = build_doc(row)

        has_embedding = 'panphon_embedding' in doc
        logger.info(f"  ipa:               {doc.get('ipa', 'none')!r}")
        logger.info(f"  panphon_embedding: {'present' if has_embedding else 'absent (zero-magnitude, omitted)'}")
        logger.info(f"  namespaces:        {doc.get('namespaces', [])}")

        if args.dry_run:
            logger.info(f"  [DRY RUN] Would index to {args.index}")
            logger.info(f"  Document: {json.dumps({k: v for k, v in doc.items() if k != 'panphon_embedding'}, ensure_ascii=False)}")
        else:
            result = es.index(index=args.index, id=toponym_id, document=doc)
            logger.info(f"  ES result: {result['result']} (version {result['_version']})")
            indexed += 1

        logger.info("")

    # Refresh index so documents are immediately searchable
    if not args.dry_run and indexed > 0:
        es.indices.refresh(index=args.index)
        logger.info(f"Index refreshed.")

    logger.info("=" * 60)
    if args.dry_run:
        logger.info(f"DRY RUN COMPLETE - {len(rows)} documents would be indexed")
    else:
        logger.info(f"COMPLETE - {indexed} documents indexed to '{args.index}'")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()

