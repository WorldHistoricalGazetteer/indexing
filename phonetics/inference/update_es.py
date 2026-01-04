"""
Update Elasticsearch toponyms index with phonetic embeddings.

This script loads a trained model and updates all toponyms in the
ES index with their computed embeddings.

Usage:
    python -m phonetics.inference.update_es \
        --checkpoint /path/to/final_model.pt \
        --vocab-dir /path/to/data/v2/vocab \
        --es-host localhost:9200 \
        --batch-size 500

Options:
    --checkpoint      Path to trained model checkpoint
    --vocab-dir       Directory containing vocabulary JSON files
    --es-host         Elasticsearch host (default: localhost:9200)
    --index           Toponyms index name (default: toponyms)
    --batch-size      Encoding batch size (default: 500)
    --embedding-version  Version number for embeddings (default: 2)
    --device          Device to use: cpu or cuda (default: cuda if available)
    --subset          Optional file with toponym IDs to update (one per line)
"""

import argparse
import logging
import sys
from pathlib import Path

import torch

from processing.settings import ES_HOST

try:
    from elasticsearch import Elasticsearch
except ImportError:
    print("Error: elasticsearch package required")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.inference.encoder import ToponymEncoder, ESIndexUpdater

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='Update ES toponyms index with phonetic embeddings'
    )
    parser.add_argument(
        '--checkpoint',
        type=str,
        required=True,
        help='Path to trained model checkpoint'
    )
    parser.add_argument(
        '--vocab-dir',
        type=str,
        required=True,
        help='Directory containing vocabulary JSON files'
    )
    parser.add_argument(
        '--es-host',
        type=str,
        default=ES_HOST,
        help='Elasticsearch host'
    )
    parser.add_argument(
        '--index',
        type=str,
        default='toponyms',
        help='Toponyms index name'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=500,
        help='Encoding batch size'
    )
    parser.add_argument(
        '--scroll-size',
        type=int,
        default=1000,
        help='ES scroll batch size'
    )
    parser.add_argument(
        '--embedding-version',
        type=int,
        default=2,
        help='Version number for embeddings'
    )
    parser.add_argument(
        '--device',
        type=str,
        default='cuda' if torch.cuda.is_available() else 'cpu',
        help='Device to use (cpu or cuda)'
    )
    parser.add_argument(
        '--subset',
        type=str,
        default=None,
        help='Optional file with toponym IDs to update (one per line)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Test mode: encode but do not update ES'
    )
    parser.add_argument(
        '--force-update',
        action='store_true',
        help='Re-process all documents, even those already at current version'
    )

    args = parser.parse_args()

    # Validate paths
    if not Path(args.checkpoint).exists():
        logger.error(f"Checkpoint not found: {args.checkpoint}")
        sys.exit(1)

    if not Path(args.vocab_dir).exists():
        logger.error(f"Vocab directory not found: {args.vocab_dir}")
        sys.exit(1)

    # Connect to ES
    es = Elasticsearch(args.es_host)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    logger.info(f"Connected to Elasticsearch at {args.es_host}")

    # Check index exists
    if not es.indices.exists(index=args.index):
        logger.error(f"Index '{args.index}' does not exist")
        sys.exit(1)

    # Load encoder
    logger.info(f"Loading model from {args.checkpoint}")
    logger.info(f"Using device: {args.device}")

    encoder = ToponymEncoder.from_checkpoint(
        args.checkpoint,
        args.vocab_dir,
        device=args.device,
    )

    logger.info(f"Model loaded: embed_dim={encoder.embed_dim}")

    # Dry run: just test encoding
    if args.dry_run:
        logger.info("Dry run mode: testing encoding...")

        # Fetch a few samples
        response = es.search(
            index=args.index,
            body={
                "size": 5,
                "query": {"match_all": {}},
                "_source": ["name", "lang", "script"]
            }
        )

        for hit in response['hits']['hits']:
            name = hit['_source'].get('name', '')
            lang = hit['_source'].get('lang')

            embedding = encoder.encode(name, lang=lang)
            logger.info(f"  {name} ({lang}): shape={embedding.shape}, norm={embedding.norm():.4f}")

        logger.info("Dry run complete")
        return

    # Create updater
    updater = ESIndexUpdater(
        encoder=encoder,
        es_client=es,
        index=args.index,
        embedding_version=args.embedding_version,
    )

    # Update subset or all
    if args.subset:
        logger.info(f"Updating subset from {args.subset}")
        with open(args.subset, 'r') as f:
            toponym_ids = [line.strip() for line in f if line.strip()]

        logger.info(f"Loaded {len(toponym_ids)} toponym IDs")

        stats = updater.update_subset(
            toponym_ids=toponym_ids,
            batch_size=args.batch_size,
        )
    else:
        logger.info("Updating toponyms...")

        stats = updater.update_all(
            batch_size=args.batch_size,
            scroll_size=args.scroll_size,
            show_progress=True,
            force_update=args.force_update,
        )

    # Summary
    logger.info("=" * 60)
    logger.info("Update complete!")
    logger.info(f"  Processed: {stats['processed']:,}")
    logger.info(f"  Updated:   {stats['updated']:,}")
    logger.info(f"  Errors:    {stats['errors']:,}")


if __name__ == '__main__':
    main()