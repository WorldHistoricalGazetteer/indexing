# phonetics/inference/search.py
"""
Interactive phonetic similarity search.

Test the trained model by searching for similar toponyms.

Usage:

    First start an interactive shell:
    srun -p htc --pty bash

    Switch to the repository directory:
    cd /ix1/whcdh/elastic

    Then run:

    python -m phonetics.inference.search

    # Or with a query:
    python -m phonetics.inference.search \
        --query "Londinium" \
        --lang la
"""

import argparse
import logging
import os
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

from phonetics.inference.encoder import ToponymEncoder, search_similar

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def interactive_search(
        encoder: ToponymEncoder,
        es: Elasticsearch,
        index: str = 'toponyms',
        top_k: int = 30,
):
    """Run interactive search loop."""
    print("\n" + "=" * 60)
    print("Phonetic Similarity Search")
    print("=" * 60)
    print("Enter a toponym to find similar names.")
    print("Format: <name>[@lang]  (e.g., 'New York@en' or just 'London')")
    print("Commands: :quit, :help, :topk <n>")
    print("=" * 60 + "\n")

    while True:
        try:
            user_input = input("Query> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        # Handle commands
        if user_input.startswith(':'):
            if user_input == ':quit' or user_input == ':q':
                print("Goodbye!")
                break
            elif user_input == ':help' or user_input == ':h':
                print("\nCommands:")
                print("  :quit, :q     Exit")
                print("  :topk <n>     Set number of results (default: 30)")
                print("  :help, :h     Show this help")
                print("\nQuery format:")
                print("  <name>        Search for toponym")
                print("  <name>@<lang> Search with language hint (e.g., 'Londres@fr')")
                print()
                continue
            elif user_input.startswith(':topk'):
                try:
                    top_k = int(user_input.split()[1])
                    print(f"Set top_k = {top_k}")
                except (IndexError, ValueError):
                    print("Usage: :topk <number>")
                continue
            else:
                print(f"Unknown command: {user_input}")
                continue

        # Parse query: "name@lang" or just "name" (name can contain spaces)
        if '@' in user_input:
            # Split on last @ to handle names with @ in them (unlikely but safe)
            at_idx = user_input.rfind('@')
            query_name = user_input[:at_idx].strip()
            query_lang = user_input[at_idx + 1:].strip() or None
        else:
            query_name = user_input.strip()
            query_lang = None

        # Encode query
        try:
            query_embedding = encoder.encode(query_name, lang=query_lang)
        except Exception as e:
            print(f"Error encoding query: {e}")
            continue

        # Search
        try:
            results = search_similar(
                es_client=es,
                query_embedding=query_embedding.cpu().tolist(),
                index=index,
                top_k=top_k,
                min_score=0.85,
            )
        except Exception as e:
            print(f"Error searching: {e}")
            continue

        # Display results
        if not results:
            print("  No results found.")
            continue

        print(f"\n  Results for '{query_name}'" + (f" ({query_lang})" if query_lang else "") + ":")
        print("  " + "-" * 50)

        for i, result in enumerate(results, 1):
            name = result['name']
            lang = result.get('lang', '')
            score = result['score']
            namespaces = ', '.join(result.get('namespaces', [])[:3])

            lang_str = f"[{lang}]" if lang else ""
            print(f"  {i:2}. {name} {lang_str:6} (score: {score:.4f}) [{namespaces}]")

        print()


def single_search(
        encoder: ToponymEncoder,
        es: Elasticsearch,
        query: str,
        lang: str = None,
        index: str = 'toponyms',
        top_k: int = 10,
):
    """Run a single search query."""
    # Encode
    query_embedding = encoder.encode(query, lang=lang)

    # Search
    results = search_similar(
        es_client=es,
        query_embedding=query_embedding.cpu().tolist(),
        index=index,
        top_k=top_k,
        min_score=0.3,
    )

    # Display
    print(f"\nResults for '{query}'" + (f" ({lang})" if lang else "") + ":")
    print("-" * 60)

    if not results:
        print("No results found.")
        return

    for i, result in enumerate(results, 1):
        name = result['name']
        result_lang = result.get('lang', '')
        score = result['score']
        namespaces = ', '.join(result.get('namespaces', [])[:3])

        lang_str = f"[{result_lang}]" if result_lang else ""
        print(f"{i:2}. {name} {lang_str:6} (score: {score:.4f}) [{namespaces}]")


def main():
    parser = argparse.ArgumentParser(
        description='Interactive phonetic similarity search'
    )
    parser.add_argument('--checkpoint', type=str,
                        default='/ix1/whcdh/models/phonetic/checkpoints/v1/phase3_best.pt',
                        help='Path to model checkpoint')
    parser.add_argument('--vocab-dir', type=str,
                        default='/ix1/whcdh/models/phonetic/data/v1/vocab',
                        help='Directory containing vocab JSON files')
    parser.add_argument('--es-host', type=str, default=ES_HOST)
    parser.add_argument('--index', type=str, default='toponyms')
    parser.add_argument('--device', type=str,
                        default='cuda' if torch.cuda.is_available() else 'cpu')
    parser.add_argument('--query', type=str, default=None,
                        help='Single query (non-interactive mode)')
    parser.add_argument('--lang', type=str, default=None,
                        help='Language code for query')
    parser.add_argument('--top-k', type=int, default=10)

    args = parser.parse_args()

    # Connect to ES
    es = Elasticsearch(
        args.es_host,
        request_timeout=120,
        retry_on_timeout=True,
        max_retries=3
    )
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    # Load encoder
    logger.info(f"Loading model from {args.checkpoint}")
    encoder = ToponymEncoder.from_checkpoint(
        args.checkpoint,
        args.vocab_dir,
        device=args.device,
    )
    logger.info(f"Model loaded (embed_dim={encoder.embed_dim})")

    # DEBUGGING: Test embeddings and similarities
    emb1 = encoder.encode("Sar-e Tanōr")
    emb2 = encoder.encode("Londres")
    emb3 = encoder.encode("London")
    emb4 = encoder.encode("Londinium")

    print(f"Sar-e Tanōr vs Londres: {encoder.similarity(emb1, emb2).item():.4f}")
    print(f"London vs Londinium: {encoder.similarity(emb3, emb4).item():.4f}")
    print(f"London vs Londres: {encoder.similarity(emb3, emb2).item():.4f}")
    print(f"Londinium vs Londres: {encoder.similarity(emb4, emb2).item():.4f}")
    ##############################################

    # Run search
    if args.query:
        single_search(
            encoder=encoder,
            es=es,
            query=args.query,
            lang=args.lang,
            index=args.index,
            top_k=args.top_k,
        )
    else:
        interactive_search(
            encoder=encoder,
            es=es,
            index=args.index,
            top_k=args.top_k,
        )


if __name__ == '__main__':
    main()