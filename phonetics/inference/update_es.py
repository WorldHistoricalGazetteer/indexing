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

    Persistent cache (Batch 9): A DuckDB cache at ``--cache-db`` (default
    ``settings.SYMPHONYM_CACHE_DB``) is consulted before any GPU work. Hits
    keyed on ``(toponym_id, embedding_version, sha256(checkpoint))`` are
    written straight to the output Parquet; misses go to the GPU and are
    appended to the cache as they are computed. A model-version bump or a
    checkpoint-file change flips the hash and forces a full recompute.
    Pass ``--no-cache`` to bypass the cache entirely (always-recompute).
    """
    import duckdb
    import os
    import shutil

    from phonetics.inference.symphonym_cache import (
        cache_size_for,
        compute_checkpoint_hash,
        insert_many,
        load_hits,
        open_cache,
    )

    duckdb_path = Path(args.input_file)
    if not duckdb_path.exists():
        logger.error(f"DuckDB database not found: {args.input_file}")
        sys.exit(1)

    # ─── Multi-GPU sharding ────────────────────────────────────────────
    # When ``--num-shards N`` (with N>1) is set, this process owns the
    # subset of toponyms whose ``hash(toponym_id) %% N == shard_id``. Each
    # shard writes its own Parquet (``<output>.shard_<id>.parquet``) and
    # they're concatenated post-array. The shared cache is bypassed for
    # sharded runs to avoid concurrent-writer DuckDB lock contention; a
    # one-shot post-array job can populate the cache from the merged
    # Parquet if needed.
    shard_id = getattr(args, "shard_id", 0)
    num_shards = getattr(args, "num_shards", 1)
    if num_shards < 1:
        logger.error(f"--num-shards must be ≥1, got {num_shards}")
        sys.exit(1)
    if not (0 <= shard_id < num_shards):
        logger.error(f"--shard-id {shard_id} out of range for --num-shards {num_shards}")
        sys.exit(1)
    sharded = num_shards > 1
    if sharded:
        logger.info(f"Sharded mode: shard {shard_id} of {num_shards}")

    final_output = Path(args.output_file)
    if sharded:
        # Suffix the output so concurrent shards don't clobber each other.
        final_output = final_output.with_suffix(
            f".shard_{shard_id}{final_output.suffix}"
        )
        logger.info(f"Sharded output file: {final_output}")

    # Check if output already exists (checkpoint)
    if final_output.exists():
        logger.info(f"✓ Output file already exists: {final_output}")
        logger.info(f"  Skipping compute step (checkpoint found)")
        logger.info(f"  To recompute, delete the file first: rm {final_output}")
        return

    # ─── Version preflight + cache load ────────────────────────────────
    use_cache = not getattr(args, "no_cache", False)
    # In sharded mode we still read the cache (DuckDB supports concurrent
    # readers and this is the whole point of pre-hydrating from production
    # ES) but skip cache writes — multiple shard processes appending to
    # the same DuckDB file would lock-contend. Misses just don't enrich
    # the cache; a separate post-merge ingest can populate them.
    sharded_cache_writes_disabled = sharded and use_cache
    cache_conn = None
    cache_hits: dict[str, bytes] = {}
    checkpoint_hash = compute_checkpoint_hash(args.checkpoint)
    logger.info(f"Checkpoint hash: {checkpoint_hash[:12]}…  (full sha256 stored)")
    logger.info(f"Embedding version: {args.embedding_version}")

    if use_cache:
        from processing.settings import SYMPHONYM_CACHE_DB
        cache_db = Path(getattr(args, "cache_db", None) or SYMPHONYM_CACHE_DB)
        if sharded_cache_writes_disabled:
            # DuckDB's default mode acquires an exclusive file lock — so 4
            # parallel shards racing to open the same cache fail with
            # "Conflicting lock is held in PID -50" (only one wins).
            # Read-only mode supports concurrent readers across processes
            # AND skips schema DDL (which the file already has from prior
            # writer runs). The cache file MUST exist; the recovery path
            # has already populated it from the migrated /ix1 cache + any
            # previous compute runs.
            logger.info(
                f"Symphonym cache (READ-ONLY in sharded mode): {cache_db}"
            )
            cache_conn = open_cache(cache_db, read_only=True)
        else:
            logger.info(f"Symphonym cache: {cache_db}")
            cache_conn = open_cache(cache_db)
        before_n = cache_size_for(
            cache_conn,
            model_version=args.embedding_version,
            checkpoint_hash=checkpoint_hash,
        )
        logger.info(
            f"Cache rows for current (version={args.embedding_version}, hash="
            f"{checkpoint_hash[:12]}…): {before_n:,}"
        )
        cache_hits = load_hits(
            cache_conn,
            model_version=args.embedding_version,
            checkpoint_hash=checkpoint_hash,
        )
        logger.info(f"Loaded {len(cache_hits):,} cache hits into memory")
    else:
        logger.info("Cache disabled (--no-cache); recomputing every embedding")

    logger.info(f"Loading model from {args.checkpoint}...")
    encoder = ToponymEncoder.from_checkpoint(
        args.checkpoint,
        args.vocab_dir,
        device=args.device
    )

    logger.info(f"Connecting to DuckDB at {duckdb_path}...")
    conn = duckdb.connect(str(duckdb_path), read_only=True)

    # Build shared WHERE clause; sharding adds a hash predicate so that
    # disjoint subsets of toponyms are owned by each shard worker.
    where_clauses = ["name IS NOT NULL", "TRIM(name) != ''"]
    if sharded:
        # DuckDB ``hash(x)`` returns a uint64; modulo over num_shards gives
        # a uniform distribution across shards.
        where_clauses.append(f"(hash(toponym_id) % {num_shards}) = {shard_id}")
    where_sql = " AND ".join(where_clauses)

    total_rows = conn.execute(
        f"SELECT COUNT(*) FROM toponyms WHERE {where_sql}"
    ).fetchone()[0]
    if sharded:
        logger.info(
            f"Toponyms in shard {shard_id}/{num_shards}: {total_rows:,} "
            f"(approx 1/{num_shards} of corpus)"
        )
    else:
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
    cache_hit_count = 0
    cache_miss_count = 0
    start_time = time.time()

    # Stream from DuckDB in batches using fetchmany instead of fetchall.
    # ``where_sql`` already includes the shard filter (when sharded).
    batch_size = args.batch_size
    cursor = conn.execute(
        f'''SELECT toponym_id, name, lang, script
            FROM toponyms
            WHERE {where_sql}
            ORDER BY toponym_id'''
    )

    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            # Partition the batch into (cache hits) + (cache misses → GPU).
            hit_batch: list[dict] = []
            miss_batch: list[dict] = []
            for row in rows:
                toponym_id, name, lang, script = row
                if not name or not name.strip():
                    continue
                emb_bytes = cache_hits.get(toponym_id) if use_cache else None
                if emb_bytes is not None:
                    # Cache stores raw int8 byte patterns (written via
                    # ``np.int8.tobytes()`` in the miss path below).
                    # ``list(bytes)`` would yield unsigned 0..255, which the
                    # int8 Parquet schema rejects (e.g. 245 → "Value 245 too
                    # large"). Reinterpret as int8 to round-trip the sign.
                    emb_int8 = np.frombuffer(emb_bytes, dtype=np.int8).tolist()
                    # The key MUST be 'doc_id' — that is the output schema's
                    # field name, and the column the index stage joins on.
                    # ``pa.Table.from_pylist`` with an explicit schema does not
                    # reject unknown keys: it silently writes null for every
                    # field the dict does not supply. Writing 'toponym_id' here
                    # produced 67,878,740 rows with a null doc_id — every cache
                    # hit — which the index stage then could not join, leaving
                    # 93% of the toponyms index without an embedding while
                    # compute, index and bulk all reported success.
                    hit_batch.append({
                        'doc_id': toponym_id, 'embedding': emb_int8,
                    })
                else:
                    miss_batch.append({
                        'toponym_id': toponym_id, 'name': name,
                        'lang': lang, 'script': script,
                    })

            if hit_batch:
                # Hits go straight to the output Parquet, no GPU traffic.
                writer.write_table(pa.Table.from_pylist(hit_batch, schema=out_schema))
                processed += len(hit_batch)
                cache_hit_count += len(hit_batch)

            if miss_batch:
                # GPU pass for cache misses.
                miss_quantised = _process_batch(
                    encoder, miss_batch, out_schema, writer,
                )
                processed += len(miss_batch)
                cache_miss_count += len(miss_batch)
                # Append the freshly-computed embeddings to the persistent
                # cache so the next compute run hits them. Skipped in
                # sharded mode — concurrent DuckDB writers from sibling
                # shards would lock-contend; a separate post-merge job
                # can ingest the merged Parquet into the cache instead.
                if use_cache and cache_conn is not None and not sharded_cache_writes_disabled:
                    # ``miss_quantised`` is an int8 numpy array. Direct
                    # ``bytes(arr.tolist())`` rejects negative values
                    # (Python's ``bytes`` requires uint8 in [0,255]) — use
                    # ``tobytes()`` which preserves the int8 byte pattern
                    # verbatim, matching how ``load_hits`` reads it back.
                    try:
                        pairs = [
                            (b['toponym_id'], miss_quantised[i].tobytes())
                            for i, b in enumerate(miss_batch)
                        ]
                        insert_many(
                            cache_conn, pairs,
                            model_version=args.embedding_version,
                            checkpoint_hash=checkpoint_hash,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"cache insert failed (non-fatal): {exc}"
                        )

            if processed and processed % 50000 == 0:
                elapsed = time.time() - start_time
                rate = processed / elapsed
                eta = (total_rows - processed) / rate if rate > 0 else 0
                logger.info(
                    f"Computed {processed:,} / {total_rows:,} ({rate:.1f} doc/s, "
                    f"ETA: {eta/60:.1f}m, hits={cache_hit_count:,}, "
                    f"misses={cache_miss_count:,})"
                )

        writer.close()
        conn.close()
        if cache_conn is not None:
            try:
                cache_conn.close()
            except Exception:
                pass

        # Move from scratch to final destination if needed
        if slurm_job_id and working_output != final_output:
            logger.info(f"Moving {working_output} -> {final_output}...")
            final_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(working_output), str(final_output))
            logger.info(f"✓ Move complete - embeddings safely stored at {final_output}")
        else:
            logger.info(f"✓ Embeddings written to {final_output}")

        elapsed = time.time() - start_time
        logger.info(
            f"Computation complete. {processed:,} embeddings saved in "
            f"{elapsed/60:.1f}m  (cache: {cache_hit_count:,} hits, "
            f"{cache_miss_count:,} misses)"
        )

    except Exception as e:
        logger.error(f"Error during compute: {e}")
        # Clean up incomplete file
        writer.close()
        conn.close()
        if cache_conn is not None:
            try:
                cache_conn.close()
            except Exception:
                pass
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
    p_compute.add_argument('--cache-db',
                           help='Override the persistent Symphonym cache DuckDB path '
                                '(default: settings.SYMPHONYM_CACHE_DB)')
    p_compute.add_argument('--no-cache', action='store_true',
                           help='Bypass the Symphonym cache (always recompute)')
    p_compute.add_argument('--shard-id', type=int, default=0,
                           help='This shard\'s 0-based index when running '
                                'a sharded multi-GPU array (default: 0)')
    p_compute.add_argument('--num-shards', type=int, default=1,
                           help='Total shards in a sharded multi-GPU array '
                                '(default: 1 = no sharding). When >1, this '
                                'task processes only toponyms whose '
                                'hash(toponym_id) %% num_shards == shard_id, '
                                'and the output filename is suffixed with '
                                '.shard_<id>.parquet.')
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

    # Persistent cross-run wall-time history so submit_index_slurm /
    # submit_batch9_slurm can size --time appropriately on subsequent runs.
    # Telemetry only — never blocks the underlying job.
    import os
    import time as _time
    from datetime import datetime as _dt, timezone as _tz
    _started_at = _dt.now(_tz.utc)
    _started_mono = _time.monotonic()
    try:
        args.func(args)
        _status = "completed"
    except SystemExit:
        _status = "failed"
        raise
    except Exception:
        _status = "failed"
        raise
    finally:
        _wall = _time.monotonic() - _started_mono
        try:
            from processing.stage_writers import record_script_wall_time
            record_script_wall_time(
                namespace="toponyms",
                script_id=f"update-es-{args.mode}",
                run_id=os.environ.get("WHG_RUN_ID", "ad-hoc"),
                started_at=_started_at.isoformat(),
                finished_at=_dt.now(_tz.utc).isoformat(),
                wall_seconds=_wall,
                status=_status,
                slurm_job_id=os.environ.get("SLURM_JOB_ID"),
                extra={"embedding_version": args.embedding_version},
            )
        except Exception:
            pass


if __name__ == '__main__':
    main()


