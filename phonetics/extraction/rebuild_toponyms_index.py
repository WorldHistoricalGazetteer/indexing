"""
Rebuild the ES toponyms index.
Reliability Update: Uses JSONL buffer on scratch disk to decouple SQL from HTTP.
Supports resuming from an existing SQLite database.
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
    print("Error: elasticsearch package required.")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import Script, detect_script, get_primary_namespace
from processing.settings import ES_HOST, IX1_BASE, STAGING_REPO_NAME

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.getLogger("elastic_transport").setLevel(logging.WARNING)
logging.getLogger("elasticsearch").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent.parent.parent / 'schemas' / 'toponyms.json'

# --- CONSTANTS ---
MAX_ID_BYTES = 450
MAX_NAME_LEN = 200


def create_sqlite_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=OFF')
    conn.execute('PRAGMA cache_size=-4000000')  # 4GB Cache
    conn.execute('PRAGMA temp_store=MEMORY')

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
                           NULL
                       );
                       ''')
    conn.commit()
    return conn


def optimize_db_after_load(conn: sqlite3.Connection):
    logger.info("Optimizing SQLite database...")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tn_id ON toponym_namespaces(toponym_id)')
    conn.execute('ANALYZE')
    conn.commit()
    logger.info("Database optimized.")


def scan_places(es: Elasticsearch, index: str, batch_size: int = 2000) -> Iterator[Dict]:
    query = {"query": {"match_all": {}}, "_source": ["namespace", "toponyms"]}
    for doc in helpers.scan(es, index=index, query=query, scroll='60m', size=batch_size):
        yield doc['_source']


def extract_toponyms_to_sqlite(es, conn, places_index, batch_size, limit=None):
    try:
        total = es.count(index=places_index)['count']
    except Exception:
        total = 0
    if limit:
        total = min(total, limit)

    logger.info(f"Scanning {total:,} places from '{places_index}'")
    places_processed = 0
    toponyms_extracted = 0

    iterator = scan_places(es, places_index, batch_size)
    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Extracting", mininterval=10.0)

    toponym_batch = []
    namespace_batch = []

    for place in iterator:
        namespace = place.get('namespace', 'other')
        toponyms_list = place.get('toponyms', [])

        if not toponyms_list: continue

        for top in toponyms_list:
            top_id = top.get('toponym_id')
            label = top.get('label')

            if not top_id: continue

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

            if not name and label: name = label
            if name: name = name.strip()
            if not name: continue

            # Filters
            if len(name) > MAX_NAME_LEN: continue
            if len(name.encode('utf-8')) > MAX_ID_BYTES: continue

            if lang and lang.lower() in ('und', 'zxx', 'mis', 'null', 'none'):
                lang = None

            canonical_id = f"{name}@{lang}" if lang else f"{name}@"
            script, _ = detect_script(name)

            toponym_batch.append((canonical_id, name, lang, lang_variant, script.value))
            namespace_batch.append((canonical_id, namespace))
            toponyms_extracted += 1

        places_processed += 1

        if len(toponym_batch) >= batch_size * 5:
            conn.executemany('INSERT OR IGNORE INTO toponyms VALUES (?, ?, ?, ?, ?)', toponym_batch)
            conn.executemany('INSERT INTO toponym_namespaces VALUES (?, ?)', namespace_batch)
            toponym_batch = []
            namespace_batch = []

        if limit and places_processed >= limit: break

    if toponym_batch:
        conn.executemany('INSERT OR IGNORE INTO toponyms VALUES (?, ?, ?, ?, ?)', toponym_batch)
        conn.executemany('INSERT INTO toponym_namespaces VALUES (?, ?)', namespace_batch)

    conn.commit()
    optimize_db_after_load(conn)
    return places_processed, toponyms_extracted


def dump_to_jsonl(conn: sqlite3.Connection, output_path: Path) -> int:
    """
    Step 1: Dump aggregated documents to a flat JSONL file on Scratch.
    This runs at max CPU/Disk speed without network constraints.
    """
    logger.info(f"Buffering documents to disk: {output_path}")

    cursor = conn.execute('''
                          SELECT t.toponym_id,
                                 t.name,
                                 t.lang,
                                 t.lang_variant,
                                 t.script,
                                 GROUP_CONCAT(DISTINCT tn.namespace)
                          FROM toponyms t
                                   JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
                          GROUP BY t.toponym_id
                          ''')

    count = 0
    with open(output_path, 'w', encoding='utf-8') as f:
        # Using a large buffer for write performance
        for row in cursor:
            toponym_id, name, lang, lang_variant, script, namespaces_str = row
            namespaces = namespaces_str.split(',') if namespaces_str else []
            primary_ns = get_primary_namespace(namespaces)

            doc = {
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

            f.write(json.dumps(doc) + '\n')
            count += 1

            if count % 1000000 == 0:
                logger.info(f"Buffered {count:,} documents...")

    logger.info(f"Buffering complete. Total documents: {count:,}")
    return count


def yield_from_jsonl(file_path: Path) -> Iterator[Dict]:
    """Reads the JSONL file for the ES Bulk loader."""
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line:
                yield json.loads(line)


def bulk_index_from_file(
        es: Elasticsearch,
        jsonl_path: Path,
        total_docs: int,
        index: str,
        batch_size: int = 2500
) -> int:
    """
    Step 2: Stream from JSONL to Elasticsearch.
    This is purely I/O bound and stable.
    """
    logger.info(f"Bulk indexing {total_docs:,} documents from file...")

    def generate_actions():
        for doc in yield_from_jsonl(jsonl_path):
            yield {
                '_index': index,
                '_id': doc['toponym_id'],
                '_source': doc,
            }

    indexed = 0
    errors = 0

    # Conservative Settings for Reliability
    iterator = helpers.parallel_bulk(
        es,
        generate_actions(),
        thread_count=4,  # Reduced threads
        queue_size=8,  # Reduced memory pressure
        chunk_size=batch_size,
        raise_on_error=False,
        request_timeout=120  # 2 minute timeout per batch
    )

    if tqdm:
        iterator = tqdm(iterator, total=total_docs, desc="Indexing", mininterval=5.0)

    for success, info in iterator:
        if success:
            indexed += 1
        else:
            errors += 1
            if errors <= 5:
                logger.error(f"Error: {info}")

    return indexed


# --- MAIN ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--es-host', default=ES_HOST)
    parser.add_argument('--places-index', default='places')
    parser.add_argument('--toponyms-index', default='toponyms')
    parser.add_argument('--schema-path', type=Path, default=SCHEMA_PATH)
    parser.add_argument('--sqlite-path', type=Path, default=f'{IX1_BASE}/data/toponyms.db')
    parser.add_argument('--scratch-dir', type=Path, default=None)
    parser.add_argument('--batch-size', type=int, default=2500)  # Lower default
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--resume', action='store_true', help="Resume from existing SQLite DB")

    args = parser.parse_args()

    if not args.confirm:
        print("Run with --confirm to proceed.")
        sys.exit(1)

    es = Elasticsearch(
        args.es_host,
        max_retries=5,
        retry_on_timeout=True
    )

    if not es.ping():
        logger.error(f"Cannot connect to {args.es_host}")
        sys.exit(1)

    final_db_path = args.sqlite_path
    final_db_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=args.scratch_dir) as temp_dir:
        temp_dir_path = Path(temp_dir)
        temp_db_path = temp_dir_path / "toponyms_working.db"
        jsonl_path = temp_dir_path / "buffer.jsonl"

        try:
            if args.resume and final_db_path.exists():
                logger.info(f"--- RESUMING from {final_db_path} ---")
                logger.info(f"Copying existing DB to scratch: {temp_db_path}")
                shutil.copy2(final_db_path, temp_db_path)
            else:
                # PHASE 1: EXTRACTION (ES -> SQLite)
                logger.info("--- PHASE 1: EXTRACTION ---")
                conn = create_sqlite_db(str(temp_db_path))
                places_count, _ = extract_toponyms_to_sqlite(
                    es, conn, args.places_index, args.batch_size, args.limit
                )

                # PHASE 2: CHECKPOINT DB
                logger.info("--- PHASE 2: CHECKPOINT ---")
                conn.close()
                shutil.copy2(temp_db_path, final_db_path)

            # Reopen DB from Scratch (whether resumed or just created)
            conn = create_sqlite_db(str(temp_db_path))

            # PHASE 3: BUFFER TO JSONL (SQLite -> Disk)
            # This isolates the SQL processing from the Network processing
            logger.info("--- PHASE 3: BUFFERING ---")
            total_docs = dump_to_jsonl(conn, jsonl_path)
            conn.close()  # Done with DB

            # PHASE 4: INDEXING (Disk -> ES)
            logger.info("--- PHASE 4: INDEXING ---")

            if es.indices.exists(index=args.toponyms_index):
                es.indices.delete(index=args.toponyms_index)

            with open(args.schema_path, 'r') as f:
                schema = json.load(f)

            # Ensure refresh_interval is disabled for speed
            if 'settings' not in schema: schema['settings'] = {}
            schema['settings']['refresh_interval'] = "-1"

            es.indices.create(index=args.toponyms_index, body=schema)

            indexed = bulk_index_from_file(
                es, jsonl_path, total_docs, args.toponyms_index, args.batch_size
            )

            # Finalize
            logger.info("--- FINALIZING ---")
            logger.info("Refreshing and creating snapshot...")
            es.indices.refresh(index=args.toponyms_index)
            create_checkpoint_snapshot(
                es,
                snapshot_name="rebuilt_toponyms",
                repo_name=STAGING_REPO_NAME
            )
            logger.info("...done.")

            logger.info("=" * 60)
            logger.info("SUCCESS")
            logger.info(f"Indexed: {indexed:,} / {total_docs:,}")
            logger.info(f"DB Saved: {final_db_path}")

        except Exception as e:
            logger.error(f"Process failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            sys.exit(1)


if __name__ == '__main__':
    main()