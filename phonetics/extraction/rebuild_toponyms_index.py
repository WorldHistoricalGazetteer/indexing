"""
Rebuild the ES toponyms index from the places index.

This script:
1. Deletes the existing toponyms index
2. Creates a new index with revised schema (128-dim embeddings, namespaces array)
3. Scans the places index, aggregating toponyms via SQLite intermediate
4. Bulk indexes toponyms with complete namespace lists and script detection

Usage:
    python -m phonetics.extraction.rebuild_toponyms_index \
        --es-host localhost:9200 \
        --batch-size 10000 \
        --confirm

Requirements:
    - elasticsearch
    - tqdm
"""

import argparse
import json
import logging
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterator, Dict, List, Optional, Tuple

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import scan, bulk, parallel_bulk
except ImportError:
    print("Error: elasticsearch package required. Install with: pip install elasticsearch")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None
    print("Warning: tqdm not available. Progress bars disabled.")

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import (
    Script, detect_script, get_primary_namespace, NAMESPACE_PRIORITY
)

from processing.settings import ES_HOST, IX1_BASE

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Schema path
SCHEMA_PATH = Path(__file__).parent.parent.parent / 'schemas' / 'toponyms.json'


def create_sqlite_db(db_path: str) -> sqlite3.Connection:
    """
    Create SQLite database for toponym aggregation.

    Schema:
    - toponyms: unique toponyms with metadata
    - toponym_namespaces: many-to-many relationship
    """
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=-1000000')  # 1GB cache

    conn.executescript('''
                       CREATE TABLE IF NOT EXISTS toponyms
                       (
                           toponym_id
                           TEXT
                           PRIMARY
                           KEY,
                           name
                           TEXT
                           NOT
                           NULL,
                           lang
                           TEXT,
                           lang_variant
                           TEXT,
                           script
                           TEXT
                       );

                       CREATE TABLE IF NOT EXISTS toponym_namespaces
                       (
                           toponym_id
                           TEXT
                           NOT
                           NULL,
                           namespace
                           TEXT
                           NOT
                           NULL,
                           PRIMARY
                           KEY
                       (
                           toponym_id,
                           namespace
                       )
                           );

                       CREATE INDEX IF NOT EXISTS idx_namespace
                           ON toponym_namespaces(namespace);
                       ''')

    conn.commit()
    return conn


def scan_places(
        es: Elasticsearch,
        index: str = 'places',
        batch_size: int = 1000,
) -> Iterator[Dict]:
    """
    Scan the places index and yield place documents.

    Args:
        es: Elasticsearch client
        index: Places index name
        batch_size: Scroll batch size

    Yields:
        Place documents
    """
    query = {
        "query": {"match_all": {}},
        "_source": ["namespace", "toponyms"]
    }

    for doc in scan(
            es,
            index=index,
            query=query,
            scroll='10m',
            size=batch_size,
    ):
        yield doc['_source']


def extract_toponyms_to_sqlite(
        es: Elasticsearch,
        conn: sqlite3.Connection,
        places_index: str = 'places',
        batch_size: int = 1000,
        limit: Optional[int] = None,
) -> Tuple[int, int]:
    """
    Extract toponyms from places index into SQLite.

    Args:
        es: Elasticsearch client
        conn: SQLite connection
        places_index: Name of places index
        batch_size: Batch size for SQLite inserts
        limit: Optional limit on number of places to process

    Returns:
        Tuple of (places_processed, toponyms_extracted)
    """
    # Get total count
    total = es.count(index=places_index)['count']
    if limit:
        total = min(total, limit)

    logger.info(f"Scanning {total:,} places from '{places_index}' index")

    places_processed = 0
    toponyms_extracted = 0

    toponym_batch = []
    namespace_batch = []

    iterator = scan_places(es, places_index, batch_size)
    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Extracting toponyms")

    for place in iterator:
        namespace = place.get('namespace', 'other')
        toponyms = place.get('toponyms', [])

        for top in toponyms:
            # Build toponym_id
            name = top.get('toponym', '').strip()
            lang = top.get('lang', '').strip() or None

            if not name:
                continue

            toponym_id = f"{name}@{lang}" if lang else f"{name}@"

            # Detect script
            script, _ = detect_script(name)

            toponym_batch.append((
                toponym_id,
                name,
                lang,
                top.get('lang_variant'),
                script.value,
            ))

            namespace_batch.append((toponym_id, namespace))
            toponyms_extracted += 1

        places_processed += 1

        # Batch insert
        if len(toponym_batch) >= batch_size:
            _insert_batch(conn, toponym_batch, namespace_batch)
            toponym_batch = []
            namespace_batch = []

        if limit and places_processed >= limit:
            break

    # Final batch
    if toponym_batch:
        _insert_batch(conn, toponym_batch, namespace_batch)

    conn.commit()

    # Get actual unique toponym count
    unique_count = conn.execute('SELECT COUNT(*) FROM toponyms').fetchone()[0]

    logger.info(f"Extracted {toponyms_extracted:,} toponyms from {places_processed:,} places")
    logger.info(f"Unique toponyms: {unique_count:,}")

    return places_processed, unique_count


def _insert_batch(
        conn: sqlite3.Connection,
        toponym_batch: List[Tuple],
        namespace_batch: List[Tuple],
):
    """Insert a batch of toponyms and namespaces."""
    conn.executemany(
        '''INSERT
        OR IGNORE INTO toponyms 
           (toponym_id, name, lang, lang_variant, script)
           VALUES (?, ?, ?, ?, ?)''',
        toponym_batch
    )
    conn.executemany(
        '''INSERT
        OR IGNORE INTO toponym_namespaces
           (toponym_id, namespace)
           VALUES (?, ?)''',
        namespace_batch
    )


def aggregate_namespaces(conn: sqlite3.Connection) -> Iterator[Dict]:
    """
    Aggregate toponyms with their namespace lists.

    Yields toponym documents ready for ES indexing.
    """
    cursor = conn.execute('''
                          SELECT t.toponym_id,
                                 t.name,
                                 t.lang,
                                 t.lang_variant,
                                 t.script,
                                 GROUP_CONCAT(tn.namespace) as namespaces
                          FROM toponyms t
                                   JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
                          GROUP BY t.toponym_id
                          ''')

    for row in cursor:
        toponym_id, name, lang, lang_variant, script, namespaces_str = row

        namespaces = list(set(namespaces_str.split(',')))
        primary_ns = get_primary_namespace(namespaces)

        yield {
            'toponym_id': toponym_id,
            'name': name,
            'lang': lang,
            'lang_variant': lang_variant,
            'script': script,
            'namespaces': namespaces,
            'primary_namespace': primary_ns,
            'embedding': None,  # Populated after training
            'embedding_version': None,
            'indexed_at': datetime.utcnow().isoformat(),
        }


def bulk_index_toponyms(
        es: Elasticsearch,
        conn: sqlite3.Connection,
        index: str = 'toponyms',
        batch_size: int = 5000,
) -> int:
    """
    Bulk index aggregated toponyms to ES.

    Args:
        es: Elasticsearch client
        conn: SQLite connection with aggregated data
        index: Target index name
        batch_size: Bulk indexing batch size

    Returns:
        Number of documents indexed
    """
    # Get count for progress bar
    total = conn.execute('SELECT COUNT(*) FROM toponyms').fetchone()[0]

    logger.info(f"Bulk indexing {total:,} toponyms to '{index}'")

    def generate_actions():
        for doc in aggregate_namespaces(conn):
            yield {
                '_index': index,
                '_id': doc['toponym_id'],
                '_source': doc,
            }

    indexed = 0
    errors = 0

    # Use parallel_bulk generator
    iterator = parallel_bulk(
        es,
        generate_actions(),
        thread_count=4,  # usually plenty for local indexing
        chunk_size=batch_size,
        queue_size=4,
        raise_on_error=False
    )

    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Indexing")

    for success, info in iterator:
        if success:
            indexed += 1
        else:
            errors += 1

    logger.info(f"Indexed {indexed:,} documents ({errors} errors)")

    return indexed


def delete_index(es: Elasticsearch, index: str):
    """Delete an index if it exists."""
    if es.indices.exists(index=index):
        logger.info(f"Deleting existing index '{index}'")
        es.indices.delete(index=index)
    else:
        logger.info(f"Index '{index}' does not exist")


def create_index(es: Elasticsearch, index: str, schema_path: Path):
    """Create index with schema."""
    logger.info(f"Creating index '{index}' from {schema_path}")

    with open(schema_path, 'r') as f:
        schema = json.load(f)

    es.indices.create(index=index, body=schema)
    logger.info(f"Index '{index}' created")


def finalize_index(es: Elasticsearch, index: str):
    """
    Finalize index after bulk loading.

    - Refresh to make documents searchable
    - Update settings for production use
    """
    logger.info(f"Finalizing index '{index}'")

    es.indices.refresh(index=index)

    # Get final count
    count = es.count(index=index)['count']
    logger.info(f"Index '{index}' finalized with {count:,} documents")


def main():
    parser = argparse.ArgumentParser(
        description='Rebuild ES toponyms index from places index'
    )
    parser.add_argument(
        '--es-host',
        default=ES_HOST,
        help='Elasticsearch host (default: localhost:9200)'
    )
    parser.add_argument(
        '--places-index',
        default='places',
        help='Source places index name (default: places)'
    )
    parser.add_argument(
        '--toponyms-index',
        default='toponyms',
        help='Target toponyms index name (default: toponyms)'
    )
    parser.add_argument(
        '--schema-path',
        type=Path,
        default=SCHEMA_PATH,
        help=f'Path to index schema JSON (default: {SCHEMA_PATH})'
    )
    parser.add_argument(
        '--sqlite-path',
        type=Path,
        default=f'{IX1_BASE}/data/toponyms.db',
        help=f'Path for SQLite intermediate DB (default: {IX1_BASE}/data/toponyms.db)'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=10000,
        help='Batch size for operations (default: 10000)'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='Limit number of places to process (for testing)'
    )
    parser.add_argument(
        '--confirm',
        action='store_true',
        help='Confirm destructive operation (required)'
    )

    args = parser.parse_args()

    # Safety check
    if not args.confirm:
        print("WARNING: This will DELETE the existing toponyms index!")
        print("Run with --confirm to proceed.")
        sys.exit(1)

    # Validate schema path
    if not args.schema_path.exists():
        logger.error(f"Schema file not found: {args.schema_path}")
        sys.exit(1)

    # Connect to ES
    es = Elasticsearch(args.es_host)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    logger.info(f"Connected to Elasticsearch at {args.es_host}")

    # Setup SQLite
    sqlite_path = str(args.sqlite_path)

    logger.info(f"Using SQLite database: {sqlite_path}")

    try:
        # Step 1: Extract to SQLite
        conn = create_sqlite_db(sqlite_path)
        places_count, toponym_count = extract_toponyms_to_sqlite(
            es, conn, args.places_index, args.batch_size, args.limit
        )

        # Step 2: Delete and recreate index
        delete_index(es, args.toponyms_index)
        create_index(es, args.toponyms_index, args.schema_path)

        # Step 3: Bulk index
        indexed = bulk_index_toponyms(
            es, conn, args.toponyms_index, args.batch_size
        )

        # Step 4: Finalize
        finalize_index(es, args.toponyms_index)

        conn.close()

        # Summary
        logger.info("=" * 60)
        logger.info("Rebuild complete!")
        logger.info(f"  Places processed: {places_count:,}")
        logger.info(f"  Unique toponyms: {toponym_count:,}")
        logger.info(f"  Documents indexed: {indexed:,}")

    finally:
        logger.info(f"SQLite database preserved at: {sqlite_path}")


if __name__ == '__main__':
    main()