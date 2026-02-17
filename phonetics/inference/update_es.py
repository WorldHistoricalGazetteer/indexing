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

import numpy as np
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
# QUANTIZATION UTILITIES
# =============================================================================
def quantize_embeddings_to_bytes(embeddings: np.ndarray) -> np.ndarray:
    """
    Quantize float32 embeddings to int8 (byte) range for ES storage.

    Args:
        embeddings: (N, D) array of L2-normalized float32 embeddings

    Returns:
        (N, D) array of int8 values in range [-128, 127]
    """
    # L2-normalized embeddings are in [-1, 1]
    # Scale to [-127, 127] and round to int8
    quantized = np.round(embeddings * 127.0).astype(np.int8)
    return quantized


def dequantize_embeddings_from_bytes(quantized: np.ndarray) -> np.ndarray:
    """
    Convert int8 embeddings back to approximate float32.

    Args:
        quantized: (N, D) array of int8 values

    Returns:
        (N, D) array of float32 values
    """
    return quantized.astype(np.float32) / 127.0


# =============================================================================
# STEP 1: COMPUTE (DuckDB -> GPU -> Parquet)
# =============================================================================
def run_compute(args):
    """
    Compute embeddings from DuckDB database (ALL toponyms).

    Changed in v6: Processes entire corpus, not just training subset.
    Saves embeddings as int8 (byte) for efficient ES storage.
    Uses /scratch for intermediate writes, then moves to final destination.

    Checkpointing: If output file already exists, skips processing (assumes complete).
    To restart from scratch, delete the output file first.
    """
    import duckdb
    import os
    import shutil

    duckdb_path = Path(args.input_file)
    if not duckdb_path.exists():
        logger.error(f"DuckDB database not found: {args.input_file}")
        sys.exit(1)

    final_output = Path(args.output_file)

    # Check if output already exists (checkpoint)
    if final_output.exists():
        logger.info(f"✓ Output file already exists: {final_output}")
        logger.info(f"  Skipping compute step (checkpoint found)")
        logger.info(f"  To recompute, delete the file first: rm {final_output}")
        return

    logger.info(f"Loading model from {args.checkpoint}...")
    encoder = ToponymEncoder.from_checkpoint(
        args.checkpoint,
        args.vocab_dir,
        device=args.device
    )

    logger.info(f"Connecting to DuckDB at {duckdb_path}...")
    conn = duckdb.connect(str(duckdb_path), read_only=True)

    # Get total count (all toponyms, not just training subset)
    total_rows = conn.execute("SELECT COUNT(*) FROM toponyms WHERE name IS NOT NULL AND TRIM(name) != ''").fetchone()[0]
    logger.info(f"Total toponyms to process: {total_rows:,}")

    # Use scratch for intermediate writes if in Slurm job
    slurm_job_id = os.environ.get('SLURM_JOB_ID')

    if slurm_job_id:
        scratch_dir = Path(f"/scratch/slurm-{slurm_job_id}")
        scratch_dir.mkdir(parents=True, exist_ok=True)
        working_output = scratch_dir / final_output.name
        logger.info(f"Using scratch for intermediate writes: {working_output}")
    else:
        working_output = final_output
        final_output.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Writing directly to: {working_output}")

    # Output Schema - store as int8 bytes for efficiency
    out_schema = pa.schema([
        ('doc_id', pa.string()),
        ('embedding', pa.list_(pa.int8())),  # byte storage
    ])

    writer = pq.ParquetWriter(working_output, out_schema, compression='snappy')

    processed = 0
    start_time = time.time()

    # Stream from DuckDB in batches using fetchmany instead of fetchall
    batch_size = args.batch_size
    cursor = conn.execute('''
        SELECT toponym_id, name, lang, script
        FROM toponyms
        WHERE name IS NOT NULL AND TRIM(name) != ''
        ORDER BY toponym_id
    ''')

    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            batch_buffer = []
            for row in rows:
                toponym_id, name, lang, script = row
                if not name or not name.strip():
                    continue

                batch_buffer.append({
                    'toponym_id': toponym_id,
                    'name': name,
                    'lang': lang,
                    'script': script
                })

            if batch_buffer:
                # Process batch
                _process_batch(encoder, batch_buffer, out_schema, writer)
                processed += len(batch_buffer)

                if processed % 50000 == 0:
                    elapsed = time.time() - start_time
                    rate = processed / elapsed
                    eta = (total_rows - processed) / rate if rate > 0 else 0
                    logger.info(f"Computed {processed:,} / {total_rows:,} ({rate:.1f} doc/s, ETA: {eta/60:.1f}m)")

        writer.close()
        conn.close()

        # Move from scratch to final destination if needed
        if slurm_job_id and working_output != final_output:
            logger.info(f"Moving {working_output} -> {final_output}...")
            final_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(working_output), str(final_output))
            logger.info(f"✓ Move complete - embeddings safely stored at {final_output}")
        else:
            logger.info(f"✓ Embeddings written to {final_output}")

        elapsed = time.time() - start_time
        logger.info(f"Computation complete. {processed:,} embeddings saved in {elapsed/60:.1f}m")

    except Exception as e:
        logger.error(f"Error during compute: {e}")
        # Clean up incomplete file
        writer.close()
        conn.close()
        if working_output.exists():
            logger.warning(f"Removing incomplete output: {working_output}")
            working_output.unlink()
        raise


def _process_batch(encoder, batch_buffer, out_schema, writer):
    """Process a batch of toponyms and write quantized embeddings."""
    doc_ids = [b['toponym_id'] for b in batch_buffer]
    names = [b['name'] for b in batch_buffer]
    langs = [b['lang'] for b in batch_buffer]

    inputs = list(zip(names, langs))

    # Inference - returns float32 L2-normalized embeddings
    embeddings = encoder.encode_batch(inputs, batch_size=len(inputs))

    # Quantize to int8 for storage
    embeddings_np = embeddings.cpu().numpy()
    embeddings_quantized = quantize_embeddings_to_bytes(embeddings_np)

    # Write to output
    out_batch = []
    for i, doc_id in enumerate(doc_ids):
        emb_bytes = embeddings_quantized[i].tolist()  # int8 list
        out_batch.append({'doc_id': doc_id, 'embedding': emb_bytes})

    table = pa.Table.from_pylist(out_batch, schema=out_schema)
    writer.write_table(table)

    return embeddings_quantized


# =============================================================================
# STEP 2: INDEX (Create full toponyms index from DuckDB + embeddings)
# =============================================================================
def run_index(args):
    """
    Create the full toponyms index from DuckDB database + embeddings.

    Changed in v6: The embeddings file now contains ALL toponyms (not just training subset).
    Embeddings are stored as int8 bytes in ES for efficiency.

    Uses DuckDB for memory-efficient embedding lookups via temporary table in /scratch.

    Workflow:
    1. rebuild_toponyms_index.py -> DuckDB (all toponyms)
    2. Train model on training subset
    3. update_es.py compute -> embeddings for ALL toponyms (quantized to int8)
    4. update_es.py index -> ES index with ALL toponyms and embeddings
    """
    import duckdb
    from datetime import datetime, timezone
    import json
    import os
    import tempfile

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

    # Create temporary DuckDB database for embedding lookups
    logger.info(f"Building temporary embeddings index from {embeddings_path}...")
    parquet_file = pq.ParquetFile(embeddings_path)
    total_embeddings = parquet_file.metadata.num_rows
    logger.info(f"Embeddings file contains {total_embeddings:,} rows")

    # Use /scratch if in Slurm job, otherwise use temp directory
    slurm_job_id = os.environ.get('SLURM_JOB_ID')
    if slurm_job_id:
        scratch_dir = Path(f"/scratch/slurm-{slurm_job_id}")
        scratch_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = tempfile.mkdtemp(dir=scratch_dir)
    else:
        temp_dir = tempfile.mkdtemp()

    temp_db_path = Path(temp_dir) / 'embeddings_lookup.duckdb'
    logger.info(f"Creating temporary embeddings database at {temp_db_path}...")

    # Create temporary DuckDB and import embeddings Parquet
    emb_conn = duckdb.connect(str(temp_db_path))
    emb_conn.execute(f"CREATE TABLE embeddings AS SELECT * FROM read_parquet('{embeddings_path}')")
    emb_conn.execute("CREATE INDEX idx_doc_id ON embeddings(doc_id)")
    logger.info(f"✓ Temporary embeddings database ready with {total_embeddings:,} rows")

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
        """Generator that yields ES bulk actions, querying embeddings from temporary DuckDB."""
        with_embedding = 0
        without_embedding = 0

        # Query all toponyms with their namespaces and attestations
        cursor = conn.execute('''
            SELECT t.toponym_id,
                   t.name,
                   t.lang,
                   t.lang_variant,
                   t.script,
                   GROUP_CONCAT(DISTINCT tn.namespace) as namespaces,
                   GROUP_CONCAT(DISTINCT ta.place_id) as attestations
            FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            LEFT JOIN toponym_attestations ta ON t.toponym_id = ta.toponym_id
            GROUP BY t.toponym_id, t.name, t.lang, t.lang_variant, t.script
        ''')

        # Stream from DuckDB in batches
        batch_size = 1000
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            # Get all toponym_ids for this batch
            batch_ids = [row[0] for row in rows]

            # Batch lookup embeddings from temporary DuckDB
            placeholders = ','.join(['?' for _ in batch_ids])
            emb_results = emb_conn.execute(
                f"SELECT doc_id, embedding FROM embeddings WHERE doc_id IN ({placeholders})",
                batch_ids
            ).fetchall()

            # Create lookup dict for this batch
            emb_lookup = {row[0]: row[1] for row in emb_results}

            for row in rows:
                toponym_id = row[0]
                namespaces = row[5].split(',') if row[5] else []
                attestations = row[6].split(',') if row[6] else []

                # Lookup embedding from batch results
                embedding = emb_lookup.get(toponym_id)

                if embedding:
                    with_embedding += 1
                else:
                    without_embedding += 1

                doc = {
                    'name': row[1],
                    'lang': row[2] or None,
                    'lang_variant': row[3] or None,
                    'script': row[4],
                    'namespaces': namespaces,
                    'primary_namespace': namespaces[0] if namespaces else None,
                    'attestations': attestations,
                    'indexed_at': indexed_at,
                }


                # Add embedding if available (as int8 list)
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

    # Cleanup temporary DuckDB
    emb_conn.close()
    conn.close()

    import shutil
    logger.info(f"Cleaning up temporary database at {temp_dir}...")
    shutil.rmtree(temp_dir, ignore_errors=True)

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
                                       help='Compute embeddings for ALL toponyms from DuckDB')
    p_compute.add_argument('--input-file', required=True,
                           help='DuckDB database file with all toponyms')
    p_compute.add_argument('--output-file', required=True,
                           help='Output embeddings Parquet file (quantized int8)')
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
                         help='Embeddings Parquet file (for all toponyms)')
    p_index.add_argument('--schema-file', required=True,
                         help='ES index schema JSON file')
    p_index.set_defaults(func=run_index)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()


