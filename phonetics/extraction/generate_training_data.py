#!/usr/bin/env python3
"""
v4 Training Data Generation Pipeline (Memory-Optimized)

Generates training data for all three phases of Symphonym training:
- Phase 1: Teacher training with phonetic features (triplets)
- Phase 2: Student alignment (all toponyms with PanPhon embeddings)
- Phase 3: Hard negative fine-tuning (triplets from ES similarity)

Key differences from previous versions:
- Uses STREAMING WRITES to avoid OOM on large triplet datasets
- Stores embeddings as numpy float32 arrays (60-70% memory savings)
- Incremental Parquet writing with pq.ParquetWriter
- Explicit garbage collection between phases

Usage:
    python -m generate_training_data --es-host "http://localhost:9200" \
        --output-dir "/path/to/output" \
        --training-namespaces gn wd tgn
"""

import argparse
import sys
from pathlib import Path

try:
    from elasticsearch import Elasticsearch
except ImportError:
    print("ERROR: elasticsearch package required")
    sys.exit(1)

from .constants import logger
from .generator import TrainingDataGenerator


def main():
    parser = argparse.ArgumentParser(
        description='Generate training data for Symphonym v4 (memory-optimized)'
    )
    parser.add_argument('--es-host', default='http://localhost:9200',
                        help='Elasticsearch host URL')
    parser.add_argument('--db-path', default=None,
                        help='Path to DuckDB database (optional)')
    parser.add_argument('--output-dir', required=True,
                        help='Output directory for training data')
    parser.add_argument('--scratch-dir', default='/tmp',
                        help='Scratch directory for temporary files')
    parser.add_argument('--training-namespaces', nargs='+',
                        default=['gn', 'wd', 'tgn'],
                        help='Namespaces to include in training')
    parser.add_argument('--force', action='store_true',
                        help='Force regeneration, ignoring existing checkpoints')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from checkpoints (default behavior)')

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scratch_dir = Path(args.scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # Connect to ES
    es = Elasticsearch(args.es_host)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    logger.info(f"Connected to Elasticsearch at {args.es_host}")

    if args.force:
        logger.info("Mode: FORCE (regenerating all data)")
    else:
        logger.info("Mode: RESUME (skipping existing checkpoints)")

    # Generate training data
    generator = TrainingDataGenerator(
        es=es,
        db_path=args.db_path,
        output_dir=output_dir,
        scratch_dir=scratch_dir,
        training_namespaces=args.training_namespaces,
        force_regenerate=args.force,
    )

    stats = generator.generate_all()

    logger.info("\n" + "=" * 60)
    logger.info("TRAINING DATA GENERATION COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Phase 1: {stats['phase1']['triplets']:,} triplets")
    logger.info(f"Phase 2: {stats['phase2']['samples']:,} samples")
    logger.info(f"Phase 3: {stats['phase3']['triplets']:,} triplets")


if __name__ == '__main__':
    main()