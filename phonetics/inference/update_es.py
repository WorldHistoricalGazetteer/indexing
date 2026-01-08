# phonetics/inference/update_es.py
"""
Inference pipeline for populating Elasticsearch with toponym embeddings.

Modes:
  1. compute:  Load training Parquet -> GPU inference -> embeddings Parquet
  2. push:     Bulk update embeddings -> ES

The compute stage reads directly from the training Parquet files generated
by rebuild_toponyms_index.py, eliminating the need for a separate extract step.

Usage:
    python -m phonetics.inference.update_es compute --input-file /path/to/training --output-file embeddings.parquet ...
    python -m phonetics.inference.update_es push    --input-file embeddings.parquet ...
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
# STEP 2: PUSH (Parquet -> ES)
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
    parser = argparse.ArgumentParser(description="ES embedding inference pipeline (compute + push)")
    subparsers = parser.add_subparsers(dest='mode', required=True)

    # --- SHARED ARGS ---
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument('--es-host', default=ES_HOST)
    parent_parser.add_argument('--index', default='toponyms')
    parent_parser.add_argument('--embedding-version', type=int, default=2)
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

    # --- PUSH ---
    p_push = subparsers.add_parser('push', parents=[parent_parser],
                                    help='Push embeddings to ES')
    p_push.add_argument('--input-file', required=True,
                        help='Embeddings Parquet file')
    p_push.set_defaults(func=run_push)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()