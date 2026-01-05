# phonetics/inference/update_es.py
"""
Multi-stage inference pipeline for populating Elasticsearch embeddings.

Modes:
  1. extract:  Dump toponyms from ES -> Parquet (Network I/O bound)
  2. compute:  Calculate embeddings GPU -> Parquet (Compute bound)
  3. push:     Bulk update embeddings -> ES (Network I/O bound)

Usage:
    python -m phonetics.inference.update_es extract --output-file /scr/job/data.parquet ...
    python -m phonetics.inference.update_es compute --input-file /scr/job/data.parquet ...
    python -m phonetics.inference.update_es push    --input-file /scr/job/embeddings.parquet ...
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
# STEP 1: EXTRACT (ES -> Parquet)
# =============================================================================
def run_extract(args):
    logger.info(f"Connecting to {args.es_host}...")
    es = Elasticsearch(args.es_host, request_timeout=60)

    # Define query based on update mode
    if args.force:
        query = {"query": {"match_all": {}}}
        logger.info("Mode: Force update (extracting ALL documents)")
    else:
        # Only fetch docs where version is missing OR version != current
        query = {
            "query": {
                "bool": {
                    "must_not": [
                        {"term": {"embedding_version": args.embedding_version}}
                    ]
                }
            }
        }
        logger.info(f"Mode: Incremental (skipping version {args.embedding_version})")

    # Count
    total = es.count(index=args.index, body=query)['count']
    if total == 0:
        logger.info("No documents need updating.")
        return

    logger.info(f"Extracting {total:,} documents to {args.output_file}")

    # Output Schema
    schema = pa.schema([
        ('doc_id', pa.string()),
        ('name', pa.string()),
        ('lang', pa.string()),
        ('script', pa.string()),
    ])

    query["_source"] = ["name", "lang", "script"]

    # Scroll Scan
    scan_iter = helpers.scan(
        es,
        index=args.index,
        query=query,
        scroll='60m',
        size=args.scroll_size
    )

    writer = None
    batch_buffer = []
    count = 0

    for doc in scan_iter:
        src = doc['_source']
        batch_buffer.append({
            'doc_id': doc['_id'],
            'name': src.get('name', ''),
            'lang': src.get('lang'),
            'script': src.get('script', 'OTHER')
        })

        if len(batch_buffer) >= args.batch_size:
            table = pa.Table.from_pylist(batch_buffer, schema=schema)
            if writer is None:
                writer = pq.ParquetWriter(args.output_file, schema, compression='snappy')
            writer.write_table(table)
            count += len(batch_buffer)
            batch_buffer = []

            if count % 100000 == 0:
                logger.info(f"Extracted {count:,} / {total:,}...")

    # Flush remaining
    if batch_buffer:
        table = pa.Table.from_pylist(batch_buffer, schema=schema)
        if writer is None:
            writer = pq.ParquetWriter(args.output_file, schema, compression='snappy')
        writer.write_table(table)
        count += len(batch_buffer)

    if writer:
        writer.close()

    logger.info(f"Extraction complete. Saved {count:,} records.")


# =============================================================================
# STEP 2: COMPUTE (Parquet -> GPU -> Parquet)
# =============================================================================
def run_compute(args):
    if not Path(args.input_file).exists():
        logger.error(f"Input file not found: {args.input_file}")
        sys.exit(1)

    logger.info(f"Loading model from {args.checkpoint}...")
    encoder = ToponymEncoder.from_checkpoint(
        args.checkpoint,
        args.vocab_dir,
        device=args.device
    )

    logger.info(f"Reading from {args.input_file}...")
    parquet_file = pq.ParquetFile(args.input_file)

    # Output Schema
    out_schema = pa.schema([
        ('doc_id', pa.string()),
        ('embedding', pa.list_(pa.float32())),
    ])

    writer = pq.ParquetWriter(args.output_file, out_schema, compression='snappy')

    total_rows = parquet_file.metadata.num_rows
    processed = 0
    start_time = time.time()

    # Iterate over row groups (chunks)
    for batch in parquet_file.iter_batches(batch_size=args.batch_size):
        df = batch.to_pandas()

        # Prepare inputs for encoder
        doc_ids = df['doc_id'].tolist()

        # Zip name/lang safely
        names = df['name'].fillna('').tolist()
        langs = df['lang'].where(df['lang'].notnull(), None).tolist()

        # Filter out empty names
        valid_indices = [i for i, n in enumerate(names) if n.strip()]
        if len(valid_indices) < len(names):
            logger.warning(f"Skipping {len(names) - len(valid_indices)} empty names")

        doc_ids = [doc_ids[i] for i in valid_indices]
        names = [names[i] for i in valid_indices]
        langs = [langs[i] for i in valid_indices]

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
        if processed % 10000 == 0:
            elapsed = time.time() - start_time
            rate = processed / elapsed
            logger.info(f"Computed {processed:,} / {total_rows:,} ({rate:.1f} doc/s)")

    writer.close()
    logger.info(f"Computation complete. Embeddings saved to {args.output_file}")


# =============================================================================
# STEP 3: PUSH (Parquet -> ES)
# =============================================================================
def run_push(args):
    if not Path(args.input_file).exists():
        logger.error(f"Input file not found: {args.input_file}")
        sys.exit(1)

    logger.info(f"Connecting to {args.es_host}...")
    es = Elasticsearch(args.es_host, request_timeout=60, max_retries=3)

    parquet_file = pq.ParquetFile(args.input_file)
    total_rows = parquet_file.metadata.num_rows
    logger.info(f"Pushing {total_rows:,} embeddings to index '{args.index}'...")

    def generate_actions():
        for batch in parquet_file.iter_batches(batch_size=args.batch_size):
            df = batch.to_pandas()
            ids = df['doc_id'].tolist()
            embs = df['embedding'].tolist()

            for doc_id, emb in zip(ids, embs):
                yield {
                    '_op_type': 'update',
                    '_index': args.index,
                    '_id': doc_id,
                    'doc': {
                        'embedding': list(emb),  # ensure python list
                        'embedding_version': args.embedding_version
                    }
                }

    # Parallel Bulk for speed
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

        if (success_count + error_count) % 50000 == 0:
            logger.info(f"Pushed {success_count:,} docs...")

    # Refresh to make searchable
    es.indices.refresh(index=args.index)
    logger.info(f"Push complete. Success: {success_count:,}, Errors: {error_count:,}")

    # Create snapshot
    logger.info("Creating snapshot...")
    create_checkpoint_snapshot(es, 'toponym_embeddings')
    logger.info("...done.")


# =============================================================================
# MAIN CLI
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Multi-stage ES embedding update")
    subparsers = parser.add_subparsers(dest='mode', required=True)

    # --- SHARED ARGS ---
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument('--es-host', default=ES_HOST)
    parent_parser.add_argument('--index', default='toponyms')
    parent_parser.add_argument('--embedding-version', type=int, default=2)
    parent_parser.add_argument('--batch-size', type=int, default=2000)

    # --- EXTRACT ---
    p_extract = subparsers.add_parser('extract', parents=[parent_parser])
    p_extract.add_argument('--output-file', required=True)
    p_extract.add_argument('--scroll-size', type=int, default=5000)
    p_extract.add_argument('--force', action='store_true', help="Ignore existing version")
    p_extract.set_defaults(func=run_extract)

    # --- COMPUTE ---
    p_compute = subparsers.add_parser('compute', parents=[parent_parser])
    p_compute.add_argument('--input-file', required=True)
    p_compute.add_argument('--output-file', required=True)
    p_compute.add_argument('--checkpoint', required=True)
    p_compute.add_argument('--vocab-dir', required=True)
    p_compute.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    p_compute.set_defaults(func=run_compute)

    # --- PUSH ---
    p_push = subparsers.add_parser('push', parents=[parent_parser])
    p_push.add_argument('--input-file', required=True)
    p_push.set_defaults(func=run_push)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()