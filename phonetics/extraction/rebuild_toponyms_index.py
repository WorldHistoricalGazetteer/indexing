"""
Rebuild the ES toponyms index from the places index.

This script:
1. Deletes the existing toponyms index
2. Creates a new index with revised schema (128-dim embeddings, namespaces array)
3. Scans the places index, aggregating toponyms via SQLite intermediate in SCRATCH
4. Bulk indexes toponyms with complete namespace lists and script detection
5. Saves the SQLite DB to persistent storage for training

Usage:
    python -m phonetics.extraction.rebuild_toponyms_index \
        --es-host localhost:9200 \
        --scratch-dir /scratch/slurm-$SLURM_JOB_ID \
        --confirm
"""

import argparse
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Dict, List, Optional, Tuple

from processing.utilities import create_checkpoint_snapshot

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

SCHEMA_PATH = Path(__file__).parent.parent.parent / 'schemas' / 'toponyms.json'


def create_sqlite_db(db_path: str) -> sqlite3.Connection:
    """
    Create SQLite database for toponym aggregation.
    """
    # check_same_thread=False required because parallel_bulk reads from threads
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=-1000000')  # 1GB cache

    conn.executescript('''
                       CREATE TABLE IF NOT EXISTS toponyms
                       (
                           toponym_id TEXT PRIMARY KEY,
                           name TEXT NOT NULL,
                           lang TEXT,
                           lang_variant TEXT,
                           script TEXT
                       );

                       CREATE TABLE IF NOT EXISTS toponym_namespaces
                       (
                           toponym_id TEXT NOT NULL,
                           namespace TEXT NOT NULL,
                           PRIMARY KEY (toponym_id, namespace)
                       );

                       CREATE INDEX IF NOT EXISTS idx_namespace
                           ON toponym_namespaces(namespace);
                       ''')

    conn.commit()
    return conn


def scan_places(
        es: Elasticsearch,
        index: str = 'places',
        batch_size: int = 2000,
) -> Iterator[Dict]:
    """Scan the places index and yield place documents."""
    query = {
        "query": {"match_all": {}},
        "_source": ["namespace", "toponyms"]
    }

    # Scroll 30m is safer for massive indices
    for doc in scan(
            es,
            index=index,
            query=query,
            scroll='30m',
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
    """Extract toponyms from places index into SQLite."""
    # Get total count
    try:
        total = es.count(index=places_index)['count']
    except Exception:
        total = 0

    if limit:
        total = min(total, limit)

    logger.info(f"Scanning {total:,} places from '{places_index}' index")

    places_processed = 0
    toponyms_extracted = 0

    toponym_batch = []
    namespace_batch = []

    iterator = scan_places(es, places_index, batch_size)

    if tqdm:
        # mininterval=30.0 prevents log spam (updates every 30s)
        iterator = tqdm(
            iterator,
            total=total,
            desc="Extracting",
            mininterval=30.0
        )

    for place in iterator:
        namespace = place.get('namespace', 'other')
        toponyms_list = place.get('toponyms', [])

        if not toponyms_list:
            continue

        for top in toponyms_list:

            top_id = top.get('toponym_id')
            label = top.get('label')

            # We must parse ID: "Name@Lang"
            if not top_id:
                continue

            if '@' in top_id:
                # Find the LAST @ to separate name from lang
                # (in case name contains @, though rare)
                at_pos = top_id.rfind('@')
                name = top_id[:at_pos]
                lang_part = top_id[at_pos + 1:]

                # Handle "en-GB"
                if '-' in lang_part:
                    parts = lang_part.split('-', 1)
                    lang = parts[0]
                    lang_variant = parts[1]
                else:
                    lang = lang_part
                    lang_variant = None
            else:
                # Fallback: No @ found
                name = top_id
                lang = None
                lang_variant = None

            # Fallback to label if name parsing failed somehow
            if not name and label:
                name = label

            name = name.strip()
            if not name:
                continue

            # Filter out placeholder languages like 'und' (Undetermined)
            if lang and lang.lower() in ('und', 'zxx', 'mis', 'null', 'none'):
                lang = None

            # Cleanup empty lang strings
            if not lang:
                lang = None

            # Reconstruct canonical ID
            canonical_id = f"{name}@{lang}" if lang else f"{name}@"

            # Detect script
            script, _ = detect_script(name)

            toponym_batch.append((
                canonical_id,
                name,
                lang,
                lang_variant,
                script.value,
            ))

            namespace_batch.append((canonical_id, namespace))
            toponyms_extracted += 1

        places_processed += 1

        if len(toponym_batch) >= batch_size:
            _insert_batch(conn, toponym_batch, namespace_batch)
            toponym_batch = []
            namespace_batch = []

        if limit and places_processed >= limit:
            break

    if toponym_batch:
        _insert_batch(conn, toponym_batch, namespace_batch)

    conn.commit()

    unique_count = conn.execute('SELECT COUNT(*) FROM toponyms').fetchone()[0]

    logger.info(f"Extracted {toponyms_extracted:,} occurrences from {places_processed:,} places")
    logger.info(f"Unique toponyms stored: {unique_count:,}")

    return places_processed, unique_count


def _insert_batch(
        conn: sqlite3.Connection,
        toponym_batch: List[Tuple],
        namespace_batch: List[Tuple],
):
    conn.executemany(
        '''INSERT OR IGNORE INTO toponyms 
           (toponym_id, name, lang, lang_variant, script)
           VALUES (?, ?, ?, ?, ?)''',
        toponym_batch
    )
    conn.executemany(
        '''INSERT OR IGNORE INTO toponym_namespaces
           (toponym_id, namespace)
           VALUES (?, ?)''',
        namespace_batch
    )


def aggregate_namespaces(conn: sqlite3.Connection) -> Iterator[Dict]:
    """Aggregate toponyms with their namespace lists."""
    # Use pipe | separator to avoid comma confusion
    cursor = conn.execute('''
                          SELECT t.toponym_id,
                                 t.name,
                                 t.lang,
                                 t.lang_variant,
                                 t.script,
                                 GROUP_CONCAT(tn.namespace, '|') as namespaces
                          FROM toponyms t
                                   JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
                          GROUP BY t.toponym_id
                          ''')

    for row in cursor:
        toponym_id, name, lang, lang_variant, script, namespaces_str = row

        namespaces = list(set(namespaces_str.split('|')))
        primary_ns = get_primary_namespace(namespaces)

        yield {
            'toponym_id': toponym_id,
            'name': name,
            'lang': lang,
            'lang_variant': lang_variant,
            'script': script,
            'namespaces': namespaces,
            'primary_namespace': primary_ns,
            'embedding': None,
            'embedding_version': None,
            'indexed_at': datetime.now(timezone.utc).isoformat(),
        }


def bulk_index_toponyms(
        es: Elasticsearch,
        conn: sqlite3.Connection,
        index: str = 'toponyms',
        batch_size: int = 5000,
) -> int:
    """Bulk index aggregated toponyms to ES using parallel_bulk."""
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

    iterator = parallel_bulk(
        es,
        generate_actions(),
        thread_count=4,
        chunk_size=batch_size,
        queue_size=4,
        raise_on_error=False
    )

    if tqdm:
        iterator = tqdm(
            iterator,
            total=total,
            desc="Indexing",
            mininterval=30.0  # Log friendly updates
        )

    for success, info in iterator:
        if success:
            indexed += 1
        else:
            errors += 1
            if errors <= 5:
                logger.error(f"Indexing error: {info}")

    logger.info(f"Indexed {indexed:,} documents ({errors} errors)")
    return indexed


def delete_index(es: Elasticsearch, index: str):
    if es.indices.exists(index=index):
        logger.info(f"Deleting existing index '{index}'")
        es.indices.delete(index=index)
    else:
        logger.info(f"Index '{index}' does not exist")


def create_index(es: Elasticsearch, index: str, schema_path: Path):
    logger.info(f"Creating index '{index}' from {schema_path}")
    with open(schema_path, 'r') as f:
        schema = json.load(f)
    es.indices.create(index=index, body=schema)
    logger.info(f"Index '{index}' created")


def finalize_index(es: Elasticsearch, index: str):
    logger.info(f"Finalizing index '{index}'")
    es.indices.refresh(index=index)
    count = es.count(index=index)['count']
    logger.info(f"Index '{index}' finalized with {count:,} documents")

    logger.info("Saving checkpoint snapshot...")
    create_checkpoint_snapshot(es, snapshot_name="toponyms_rebuild")
    logger.info("... done")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--es-host', default=ES_HOST)
    parser.add_argument('--places-index', default='places')
    parser.add_argument('--toponyms-index', default='toponyms')
    parser.add_argument('--schema-path', type=Path, default=SCHEMA_PATH)
    parser.add_argument('--sqlite-path', type=Path, default=f'{IX1_BASE}/data/toponyms.db')
    parser.add_argument('--scratch-dir', type=Path, default=None)
    parser.add_argument('--batch-size', type=int, default=10000)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--confirm', action='store_true')

    args = parser.parse_args()

    if not args.confirm:
        print("WARNING: This will DELETE the existing toponyms index! Run with --confirm.")
        sys.exit(1)

    if not args.schema_path.exists():
        logger.error(f"Schema file not found: {args.schema_path}")
        sys.exit(1)

    es = Elasticsearch(args.es_host)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    final_db_path = args.sqlite_path

    # Use temporary directory for building
    with tempfile.TemporaryDirectory(dir=args.scratch_dir) as temp_dir:
        temp_db_path = Path(temp_dir) / "toponyms_working.db"
        logger.info(f"Building temporary database at: {temp_db_path}")

        try:
            # 1. Create DB (with check_same_thread=False)
            conn = create_sqlite_db(str(temp_db_path))

            # 2. Extract
            places_count, toponym_count = extract_toponyms_to_sqlite(
                es, conn, args.places_index, args.batch_size, args.limit
            )

            # 3. Recreate Index
            delete_index(es, args.toponyms_index)
            create_index(es, args.toponyms_index, args.schema_path)

            # 4. Bulk Index
            indexed = bulk_index_toponyms(
                es, conn, args.toponyms_index, args.batch_size
            )

            # 5. Finalize
            finalize_index(es, args.toponyms_index)

            conn.close()

            # 6. Persist DB
            logger.info(f"Copying database to persistent storage: {final_db_path}")
            final_db_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(temp_db_path, final_db_path)

            logger.info("=" * 60)
            logger.info("Rebuild complete!")
            logger.info(f"  Places processed: {places_count:,}")
            logger.info(f"  Unique toponyms: {toponym_count:,}")
            logger.info(f"  Documents indexed: {indexed:,}")
            logger.info(f"  Database saved to: {final_db_path}")

        except Exception as e:
            logger.error(f"Process failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            sys.exit(1)

if __name__ == '__main__':
    main()