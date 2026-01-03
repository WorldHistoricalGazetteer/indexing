# phonetics/extraction/rebuild_toponyms_index.py
"""
Rebuild the ES toponyms index from the places index.
Optimized for speed and robustness against garbage data.
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
    from elasticsearch import Elasticsearch, helpers
except ImportError:
    print("Error: elasticsearch package required. Install with: pip install elasticsearch")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None
    print("Warning: tqdm not available. Progress bars disabled.")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import (
    Script, detect_script, get_primary_namespace
)

from processing.settings import ES_HOST, IX1_BASE

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Suppress noisy Elasticsearch HTTP logs (PUT/POST/GET)
logging.getLogger("elastic_transport").setLevel(logging.WARNING)
logging.getLogger("elasticsearch").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent.parent.parent / 'schemas' / 'toponyms.json'

# --- CONSTANTS ---
MAX_ID_BYTES = 450  # ES limit is 512. Leave room for safety.
MAX_NAME_LEN = 200  # If a name is longer than this chars, it's a description, skip it.


def create_sqlite_db(db_path: str) -> sqlite3.Connection:
    """
    Create SQLite database for toponym aggregation.

    OPTIMIZATION: We do NOT create indexes on toponym_namespaces yet.
    We create them after bulk insertion to speed up the process.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)

    # Performance Tuning for Bulk Loading
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=OFF')      # Riskier but much faster for temp scratch DBs
    conn.execute('PRAGMA cache_size=-4000000')  # Use up to 4GB RAM for cache
    conn.execute('PRAGMA temp_store=MEMORY')    # Store temp tables in RAM

    conn.executescript('''
        CREATE TABLE IF NOT EXISTS toponyms (
            toponym_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            lang TEXT,
            lang_variant TEXT,
            script TEXT
        );

        -- Heap table for speed (no PK yet)
        CREATE TABLE IF NOT EXISTS toponym_namespaces (
            toponym_id TEXT NOT NULL,
            namespace TEXT NOT NULL
        );
    ''')

    conn.commit()
    return conn


def optimize_db_after_load(conn: sqlite3.Connection):
    """
    Create indexes AFTER loading data.
    This prevents re-balancing the B-Tree on every insert.
    """
    logger.info("Optimizing SQLite database (Creating Indices)...")

    # Create the index needed for the GROUP BY query
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tn_id ON toponym_namespaces(toponym_id)')

    # Analyze to help the query optimizer
    conn.execute('ANALYZE')
    conn.commit()
    logger.info("Database optimized.")


def scan_places(es: Elasticsearch, index: str = 'places', batch_size: int = 2000) -> Iterator[Dict]:
    """Scan the places index."""
    query = {
        "query": {"match_all": {}},
        "_source": ["namespace", "toponyms"]
    }

    # Scroll 60m to be safe with large datasets
    for doc in helpers.scan(
            es,
            index=index,
            query=query,
            scroll='60m',
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
    try:
        total = es.count(index=places_index)['count']
    except Exception:
        total = 0

    if limit:
        total = min(total, limit)

    logger.info(f"Scanning {total:,} places from '{places_index}' index")

    places_processed = 0
    toponyms_extracted = 0
    ignored_garbage = 0

    toponym_batch = []
    namespace_batch = []

    iterator = scan_places(es, places_index, batch_size)

    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Extracting", mininterval=10.0)

    for place in iterator:
        namespace = place.get('namespace', 'other')
        toponyms_list = place.get('toponyms', [])

        if not toponyms_list:
            continue

        for top in toponyms_list:
            top_id = top.get('toponym_id')
            label = top.get('label')

            # --- Name & Lang Parsing ---
            if not top_id:
                continue

            if '@' in top_id:
                at_pos = top_id.rfind('@')
                name = top_id[:at_pos]
                lang_part = top_id[at_pos + 1:]

                if '-' in lang_part:
                    parts = lang_part.split('-', 1)
                    lang = parts[0]
                    lang_variant = parts[1]
                else:
                    lang = lang_part
                    lang_variant = None
            else:
                name = top_id
                lang = None
                lang_variant = None

            if not name and label:
                name = label

            if name:
                name = name.strip()

            if not name:
                continue

            # --- CRITICAL FIX: GARBAGE FILTER ---
            # 1. Check Character Length (Logical check)
            if len(name) > MAX_NAME_LEN:
                ignored_garbage += 1
                continue

            # 2. Check Byte Length (Physical check for ES ID limit)
            # UTF-8 characters can be up to 4 bytes.
            if len(name.encode('utf-8')) > MAX_ID_BYTES:
                ignored_garbage += 1
                continue
            # ------------------------------------

            # Normalize Lang
            if lang and lang.lower() in ('und', 'zxx', 'mis', 'null', 'none'):
                lang = None

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

        # Insert batches
        if len(toponym_batch) >= batch_size * 5: # Buffer up slightly more before DB write
            _insert_batch(conn, toponym_batch, namespace_batch)
            toponym_batch = []
            namespace_batch = []

        if limit and places_processed >= limit:
            break

    if toponym_batch:
        _insert_batch(conn, toponym_batch, namespace_batch)

    conn.commit()

    # Optimize DB now that data is loaded
    optimize_db_after_load(conn)

    unique_count = conn.execute('SELECT COUNT(*) FROM toponyms').fetchone()[0]

    logger.info(f"Extracted {toponyms_extracted:,} occurrences from {places_processed:,} places")
    logger.info(f"Ignored {ignored_garbage:,} garbage items (too long)")
    logger.info(f"Unique toponyms stored: {unique_count:,}")

    return places_processed, unique_count


def _insert_batch(conn, toponym_batch, namespace_batch):
    conn.executemany(
        'INSERT OR IGNORE INTO toponyms VALUES (?, ?, ?, ?, ?)',
        toponym_batch
    )
    # Note: We use INSERT OR IGNORE to handle duplicate namespaces for same toponym (if any)
    # Since we removed the PK, we rely on rowid, but duplicates will be handled by GROUP BY later
    # Actually, let's use DISTINCT in the select query to be safe.
    conn.executemany(
        'INSERT INTO toponym_namespaces VALUES (?, ?)',
        namespace_batch
    )


def aggregate_namespaces(conn: sqlite3.Connection) -> Iterator[Dict]:
    """Aggregate toponyms with their namespace lists."""
    logger.info("Starting aggregation query...")

    # The DISTINCT in GROUP_CONCAT ensures we don't get 'osm|osm|osm'
    cursor = conn.execute('''
        SELECT t.toponym_id,
               t.name,
               t.lang,
               t.lang_variant,
               t.script,
               GROUP_CONCAT(DISTINCT tn.namespace) as namespaces
        FROM toponyms t
        JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
        GROUP BY t.toponym_id
    ''')

    for row in cursor:
        toponym_id, name, lang, lang_variant, script, namespaces_str = row

        # Handle edge case where GROUP_CONCAT might return None (shouldn't happen with JOIN)
        if namespaces_str:
            # Replace default comma with safe separator if needed, but here we assume comma is standard
            # for default GROUP_CONCAT.
            namespaces = namespaces_str.split(',')
        else:
            namespaces = []

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

    # Increased thread_count and queue_size for throughput
    iterator = helpers.parallel_bulk(
        es,
        generate_actions(),
        thread_count=8,
        queue_size=16,
        chunk_size=batch_size,
        raise_on_error=False
    )

    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Indexing", mininterval=5.0)

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
    parser.add_argument('--batch-size', type=int, default=5000)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--confirm', action='store_true')

    args = parser.parse_args()

    if not args.confirm:
        print("WARNING: This will DELETE the existing toponyms index! Run with --confirm.")
        sys.exit(1)

    if not args.schema_path.exists():
        logger.error(f"Schema file not found: {args.schema_path}")
        sys.exit(1)

    es = Elasticsearch(args.es_host, timeout=60) # Increased timeout
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    final_db_path = args.sqlite_path

    with tempfile.TemporaryDirectory(dir=args.scratch_dir) as temp_dir:
        temp_db_path = Path(temp_dir) / "toponyms_working.db"
        logger.info(f"Building temporary database at: {temp_db_path}")

        try:# --- STEP 1: EXTRACTION ---
            conn = create_sqlite_db(str(temp_db_path))
            places_count, toponym_count = extract_toponyms_to_sqlite(
                es, conn, args.places_index, args.batch_size, args.limit
            )

            # --- CHECKPOINT: SAVE DATABASE ---
            logger.info("Extraction complete. Closing DB to checkpoint...")
            conn.close() # Close to flush WAL and ensure integrity

            logger.info(f"Checkpointing: Copying database to {final_db_path}")
            shutil.copy2(temp_db_path, final_db_path)

            # Re-open for reading (Reuse create_sqlite_db to set PRAGMAs)
            logger.info("Re-opening temporary database for indexing...")
            conn = create_sqlite_db(str(temp_db_path))

            # --- STEP 2: RECREATE INDEX ---
            delete_index(es, args.toponyms_index)
            create_index(es, args.toponyms_index, args.schema_path)

            # --- STEP 3: BULK INDEX ---
            indexed = bulk_index_toponyms(
                es, conn, args.toponyms_index, args.batch_size
            )

            # --- STEP 4: FINALIZE ---
            finalize_index(es, args.toponyms_index)

            conn.close()

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