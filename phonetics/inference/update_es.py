# phonetics/inference/update_es.py
"""
Inference pipeline for populating Elasticsearch with toponym embeddings.

Modes:
  1. compute:  Load training Parquet -> GPU inference -> embeddings Parquet
  2. index:    DuckDB + embeddings -> Full ES toponyms index (rebuild from scratch)

The compute stage reads directly from the training Parquet files generated
by rebuild_toponyms_index.py. The index stage rebuilds the entire toponyms
index from the DuckDB database (which contains all toponyms) combined with
the embeddings Parquet (which contains only training subset embeddings).

Usage:
    python -m phonetics.inference.update_es compute --input-file /path/to/training --output-file embeddings.parquet ...
    python -m phonetics.inference.update_es index --duckdb-file toponyms.db --embeddings-file embeddings.parquet ...

Workflow:
    1. rebuild_toponyms_index.py -> DuckDB (all toponyms) + Parquet (training subset)
    2. Train Student model (phases 1-3)
    3. update_es.py compute -> embeddings for training subset
    4. update_es.py index -> Full ES index (all toponyms, embeddings where available)
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import torch
import pyarrow as pa
import pyarrow.parquet as pq

from processing.settings import ES_HOST
from processing.utilities import create_checkpoint_snapshot

try:
    from elasticsearch import Elasticsearch, helpers
except ImportError:
    print("Error: elasticsearch package required")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.inference.encoder import ToponymEncoder

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# =============================================================================
# STEP 1: COMPUTE (Parquet -> GPU -> Parquet)
# =============================================================================
def run_compute(args):
    """
    Compute embeddings from Parquet input.

    Supports two input formats:
    1. Extract format: doc_id, name, lang, script (from run_extract)
    2. Training format: toponym_id, name, lang, script, char_ids (from rebuild)

    The training format is preferred as it has pre-encoded char_ids.
    """
    import pyarrow.dataset as ds

    input_path = Path(args.input_file)
    if not input_path.exists():
        logger.error(f"Input file/directory not found: {args.input_file}")
        sys.exit(1)

    logger.info(f"Loading model from {args.checkpoint}...")
    encoder = ToponymEncoder.from_checkpoint(
        args.checkpoint,
        args.vocab_dir,
        device=args.device
    )

    # Detect input format - training data is a directory with partitions
    if input_path.is_dir():
        logger.info(f"Reading training data from {args.input_file}...")
        dataset = ds.dataset(input_path, format='parquet')
        # Training format uses toponym_id
        id_column = 'toponym_id'
    else:
        logger.info(f"Reading extracted data from {args.input_file}...")
        dataset = ds.dataset(input_path, format='parquet')
        # Extract format uses doc_id
        id_column = 'doc_id'

    # Check which format
    schema_names = set(dataset.schema.names)
    if 'toponym_id' in schema_names:
        id_column = 'toponym_id'
        logger.info("Detected training Parquet format (toponym_id)")
    elif 'doc_id' in schema_names:
        id_column = 'doc_id'
        logger.info("Detected extract Parquet format (doc_id)")
    else:
        logger.error("Unknown Parquet format - expected 'toponym_id' or 'doc_id' column")
        sys.exit(1)

    # Output Schema
    out_schema = pa.schema([
        ('doc_id', pa.string()),
        ('embedding', pa.list_(pa.float32())),
    ])

    writer = pq.ParquetWriter(args.output_file, out_schema, compression='snappy')

    total_rows = dataset.count_rows()
    processed = 0
    start_time = time.time()

    # Iterate over batches
    for batch in dataset.to_batches(batch_size=args.batch_size):
        df = batch.to_pandas()

        # Prepare inputs for encoder
        doc_ids = df[id_column].tolist()

        # Zip name/lang safely
        names = df['name'].fillna('').tolist()
        langs = df['lang'].where(df['lang'].notnull(), None).tolist() if 'lang' in df.columns else [None] * len(names)

        # Filter out empty names
        valid_indices = [i for i, n in enumerate(names) if n and str(n).strip()]
        if len(valid_indices) < len(names):
            logger.debug(f"Skipping {len(names) - len(valid_indices)} empty names")

        doc_ids = [doc_ids[i] for i in valid_indices]
        names = [names[i] for i in valid_indices]
        langs = [langs[i] for i in valid_indices]

        if not doc_ids:
            continue

        inputs = list(zip(names, langs))

        # Inference
        embeddings = encoder.encode_batch(inputs, batch_size=args.batch_size)

        # Write to output
        out_batch = []
        for i, doc_id in enumerate(doc_ids):
            emb_list = embeddings[i].cpu().tolist()
            out_batch.append({'doc_id': doc_id, 'embedding': emb_list})

        table = pa.Table.from_pylist(out_batch, schema=out_schema)
        writer.write_table(table)

        processed += len(doc_ids)
        if processed % 50000 == 0:
            elapsed = time.time() - start_time
            rate = processed / elapsed
            eta = (total_rows - processed) / rate if rate > 0 else 0
            logger.info(f"Computed {processed:,} / {total_rows:,} ({rate:.1f} doc/s, ETA: {eta/60:.1f}m)")

    writer.close()
    logger.info(f"Computation complete. {processed:,} embeddings saved to {args.output_file}")


# =============================================================================
# STEP 2: INDEX (Create full toponyms index from DuckDB + embeddings)
# =============================================================================
def run_index(args):
    """
    Create the full toponyms index from DuckDB database + embeddings.

    The DuckDB database contains ALL toponyms (not just training subset).
    Embeddings are only available for training toponyms - others get null embedding.

    Workflow:
    1. rebuild_toponyms_index.py -> DuckDB (all toponyms) + Parquet (training subset)
    2. Train model on Parquet subset
    3. update_es.py compute -> embeddings for training subset
    4. update_es.py index -> ES index with ALL toponyms (embeddings where available)
    """
    import duckdb
    from datetime import datetime, timezone
    import json

    duckdb_path = Path(args.duckdb_file)
    embeddings_path = Path(args.embeddings_file)
    schema_path = Path(args.schema_file)

    if not duckdb_path.exists():
        logger.error(f"DuckDB database not found: {duckdb_path}")
        sys.exit(1)
    if not embeddings_path.exists():
        logger.error(f"Embeddings file not found: {embeddings_path}")
        sys.exit(1)
    if not schema_path.exists():
        logger.error(f"Schema file not found: {schema_path}")
        sys.exit(1)

    logger.info(f"Connecting to {args.es_host}...")
    es = Elasticsearch(args.es_host, request_timeout=120, max_retries=3)

    # Load embeddings into memory (doc_id -> embedding)
    logger.info(f"Loading embeddings from {embeddings_path}...")
    emb_table = pq.read_table(embeddings_path)
    embeddings_map = {}
    for batch in emb_table.to_batches():
        doc_ids = batch.column('doc_id').to_pylist()
        embs = batch.column('embedding').to_pylist()
        for doc_id, emb in zip(doc_ids, embs):
            embeddings_map[doc_id] = list(emb)
    logger.info(f"Loaded {len(embeddings_map):,} embeddings")

    # Load schema and create index
    logger.info(f"Creating index '{args.index}' from schema...")
    with open(schema_path) as f:
        schema = json.load(f)

    # Delete existing index if present
    if es.indices.exists(index=args.index):
        logger.info(f"Deleting existing index '{args.index}'...")
        es.indices.delete(index=args.index)

    # Disable refresh for bulk loading
    schema.setdefault('settings', {})['refresh_interval'] = '-1'
    es.indices.create(index=args.index, body=schema)
    logger.info(f"Index '{args.index}' created")

    # Connect to DuckDB
    logger.info(f"Reading toponyms from {duckdb_path}...")
    conn = duckdb.connect(str(duckdb_path), read_only=True)

    # Get total count
    total_rows = conn.execute('SELECT COUNT(*) FROM toponyms').fetchone()[0]
    logger.info(f"Total toponyms in database: {total_rows:,}")

    indexed_at = datetime.now(timezone.utc).isoformat()

    def generate_actions():
        with_embedding = 0
        without_embedding = 0

        # Query all toponyms with their namespaces and attestations
        cursor = conn.execute('''
            SELECT t.toponym_id,
                   t.name,
                   t.name_romanized,
                   t.lang,
                   t.lang_variant,
                   t.script,
                   GROUP_CONCAT(DISTINCT tn.namespace) as namespaces,
                   GROUP_CONCAT(DISTINCT ta.place_id) as attestations
            FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            LEFT JOIN toponym_attestations ta ON t.toponym_id = ta.toponym_id
            GROUP BY t.toponym_id, t.name, t.name_romanized, t.lang, t.lang_variant, t.script
        ''')

        for row in cursor.fetchall():
            toponym_id = row[0]
            namespaces = row[6].split(',') if row[6] else []
            attestations = row[7].split(',') if row[7] else []

            embedding = embeddings_map.get(toponym_id)
            if embedding:
                with_embedding += 1
            else:
                without_embedding += 1

            doc = {
                'name': row[1],
                'lang': row[3] or None,
                'lang_variant': row[4] or None,
                'script': row[5],
                'namespaces': namespaces,
                'primary_namespace': namespaces[0] if namespaces else None,
                'attestations': attestations,
                'indexed_at': indexed_at,
            }

            # Add name_romanized if present
            if row[2]:
                doc['name_romanized'] = row[2]

            # Add embedding if available
            if embedding:
                doc['embedding'] = embedding
                doc['embedding_version'] = args.embedding_version

            # Remove None values
            doc = {k: v for k, v in doc.items() if v is not None}

            yield {
                '_index': args.index,
                '_id': toponym_id,
                '_source': doc,
            }

        logger.info(f"Toponyms with embedding: {with_embedding:,}")
        logger.info(f"Toponyms without embedding: {without_embedding:,}")

    # Bulk index
    logger.info("Bulk indexing...")
    success_count = 0
    error_count = 0

    for success, info in helpers.parallel_bulk(
        es,
        generate_actions(),
        thread_count=4,
        chunk_size=args.batch_size,
        raise_on_error=False
    ):
        if success:
            success_count += 1
        else:
            error_count += 1
            if error_count < 5:
                logger.error(f"Error: {info}")

        if (success_count + error_count) % 100000 == 0:
            logger.info(f"Indexed {success_count:,} docs...")

    # Enable refresh
    logger.info("Enabling refresh...")
    es.indices.put_settings(index=args.index, body={'refresh_interval': '1s'})
    es.indices.refresh(index=args.index)

    logger.info(f"Indexing complete. Success: {success_count:,}, Errors: {error_count:,}")

    # Create snapshot
    logger.info("Creating snapshot...")
    create_checkpoint_snapshot(es, f'toponyms_v{args.embedding_version}')
    logger.info("...done.")


# =============================================================================
# MAIN CLI
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="ES embedding inference pipeline (compute + index)")
    subparsers = parser.add_subparsers(dest='mode', required=True)

    # --- SHARED ARGS ---
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument('--es-host', default=ES_HOST)
    parent_parser.add_argument('--index', default='toponyms')
    parent_parser.add_argument('--embedding-version', type=int, required=True)
    parent_parser.add_argument('--batch-size', type=int, default=2000)

    # --- COMPUTE ---
    p_compute = subparsers.add_parser('compute', parents=[parent_parser],
                                       help='Compute embeddings from training Parquet')
    p_compute.add_argument('--input-file', required=True,
                           help='Training Parquet directory or file')
    p_compute.add_argument('--output-file', required=True,
                           help='Output embeddings Parquet file')
    p_compute.add_argument('--checkpoint', required=True,
                           help='Model checkpoint path')
    p_compute.add_argument('--vocab-dir', required=True,
                           help='Vocabulary directory')
    p_compute.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    p_compute.set_defaults(func=run_compute)

    # --- INDEX (creates full index from DuckDB + embeddings) ---
    p_index = subparsers.add_parser('index', parents=[parent_parser],
                                     help='Create full toponyms index from DuckDB database + embeddings')
    p_index.add_argument('--duckdb-file', required=True,
                         help='DuckDB database with all toponyms')
    p_index.add_argument('--embeddings-file', required=True,
                         help='Embeddings Parquet file (for training subset)')
    p_index.add_argument('--schema-file', required=True,
                         help='ES index schema JSON file')
    p_index.set_defaults(func=run_index)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()


