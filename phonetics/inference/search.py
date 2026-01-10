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

from phonetics.utils.script_detection import detect_script
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


def get_latest_version(base_path: str) -> str:
    """Find the latest version directory (e.g., v3 > v2 > v1)."""
    base = Path(base_path)
    if not base.exists():
        return "v1"  # fallback

    versions = []
    for d in base.iterdir():
        if d.is_dir() and d.name.startswith('v'):
            try:
                versions.append((int(d.name[1:]), d.name))
            except ValueError:
                continue

    if not versions:
        return "v1"  # fallback

    return max(versions, key=lambda x: x[0])[1]


def main():
    # Determine latest version for defaults
    checkpoints_base = '/ix1/whcdh/models/phonetic/checkpoints'
    data_base = '/ix1/whcdh/models/phonetic/data'
    latest_version = get_latest_version(checkpoints_base)

    parser = argparse.ArgumentParser(
        description='Interactive phonetic similarity search'
    )
    parser.add_argument('--checkpoint', type=str,
                        default=f'{checkpoints_base}/{latest_version}/phase3_best.pt',
                        help='Path to model checkpoint')
    parser.add_argument('--vocab-dir', type=str,
                        default=f'{data_base}/{latest_version}/vocab',
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

    # ===========================================================================
    # DEBUGGING: Comprehensive embedding quality tests
    # ===========================================================================

    print("\n" + "=" * 70)
    print("EMBEDDING QUALITY DIAGNOSTICS")
    print("=" * 70)

    # ---------------------------------------------------------------------------
    # Test 1: Cross-script equivalents (CORE GOAL - should be > 0.85)
    # These are the primary use case: same place, different scripts
    # ---------------------------------------------------------------------------
    print("\n[Test 1] Cross-script equivalents (expected: > 0.85)")
    print("-" * 50)

    cross_script_pairs = [
        ("London", "Лондон", "Latin/Cyrillic"),
        ("Moscow", "Москва", "Latin/Cyrillic"),
        ("Paris", "Париж", "Latin/Cyrillic"),
        ("Athens", "Αθήνα", "Latin/Greek"),
        ("Beijing", "北京", "Latin/Chinese"),
        ("Seoul", "서울", "Latin/Korean"),
        ("London", "لندن", "Latin/Arabic"),
        ("Moskva", "Москва", "Transliteration/Cyrillic"),
        ("Athina", "Αθήνα", "Transliteration/Greek"),
        ("Parizh", "Париж", "Transliteration/Cyrillic"),
    ]

    cross_script_pass = 0
    for n1, n2, desc in cross_script_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "✓" if sim > 0.85 else "✗"
        if sim > 0.85:
            cross_script_pass += 1
        print(f"  {status} {n1:15} vs {n2:15}: {sim:.4f}  ({desc})")
    print(f"  Pass rate: {cross_script_pass}/{len(cross_script_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 2: Same-script different languages (HARDER - threshold > 0.70)
    # These are phonetically different despite referring to same place
    # ---------------------------------------------------------------------------
    print("\n[Test 2] Same-script cross-language (expected: > 0.70)")
    print("-" * 50)
    print("  Note: Lower threshold - these have genuinely different pronunciations")

    same_script_pairs = [
        ("London", "Londres", "en/fr - different vowels"),
        ("London", "Londra", "en/it - different ending"),
        ("Moscow", "Moscou", "en/fr - similar"),
        ("Munich", "München", "en/de - umlaut"),
        ("Vienna", "Wien", "en/de - very different"),
        ("Rome", "Roma", "en/it - similar"),
    ]

    same_script_pass = 0
    for n1, n2, desc in same_script_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "✓" if sim > 0.70 else "✗"
        if sim > 0.70:
            same_script_pass += 1
        print(f"  {status} {n1:15} vs {n2:15}: {sim:.4f}  ({desc})")
    print(f"  Pass rate: {same_script_pass}/{len(same_script_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 3: Unrelated pairs (should be < 0.50)
    # ---------------------------------------------------------------------------
    print("\n[Test 3] Unrelated pairs (expected: < 0.50)")
    print("-" * 50)

    unrelated_pairs = [
        ("London", "Tokyo"),
        ("Paris", "Beijing"),
        ("Berlin", "Cairo"),
        ("Moscow", "Sydney"),
        ("Rome", "Seoul"),
        ("Munich", "Bangkok"),
        ("Athens", "Lima"),
        ("Madrid", "Oslo"),
    ]

    unrelated_pass = 0
    for n1, n2 in unrelated_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "✓" if sim < 0.50 else "✗"
        if sim < 0.50:
            unrelated_pass += 1
        print(f"  {status} {n1:15} vs {n2:15}: {sim:.4f}")
    print(f"  Pass rate: {unrelated_pass}/{len(unrelated_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 4: Diacritic variants (should be > 0.90)
    # Same name, minor orthographic differences
    # ---------------------------------------------------------------------------
    print("\n[Test 4] Diacritic/minor variants (expected: > 0.90)")
    print("-" * 50)

    diacritic_pairs = [
        ("Zurich", "Zürich"),
        ("Gdansk", "Gdańsk"),
        ("Krakow", "Kraków"),
        ("Malmo", "Malmö"),
        ("Sao Paulo", "São Paulo"),
        ("Bogota", "Bogotá"),
    ]

    diacritic_pass = 0
    for n1, n2 in diacritic_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "✓" if sim > 0.90 else "✗"
        if sim > 0.90:
            diacritic_pass += 1
        print(f"  {status} {n1:15} vs {n2:15}: {sim:.4f}")
    print(f"  Pass rate: {diacritic_pass}/{len(diacritic_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 5: Exonym pairs - genuinely different names (expected: 0.50-0.85)
    # These refer to the same place but have very different forms
    # ---------------------------------------------------------------------------
    print("\n[Test 5] Exonym pairs (expected: 0.50-0.85, informational)")
    print("-" * 50)
    print("  Note: These are genuinely different names for same place")

    exonym_pairs = [
        ("Cologne", "Köln", "en/de"),
        ("Munich", "Muenchen", "en/de-ascii"),
        ("Nuremberg", "Nürnberg", "en/de"),
        ("Florence", "Firenze", "en/it"),
        ("Prague", "Praha", "en/cs"),
        ("Warsaw", "Warszawa", "en/pl"),
        ("Tokyo", "東京", "en/ja"),
    ]

    for n1, n2, desc in exonym_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        in_range = 0.50 <= sim <= 0.85
        status = "~" if in_range else ("↑" if sim > 0.85 else "↓")
        print(f"  {status} {n1:15} vs {n2:15}: {sim:.4f}  ({desc})")

    # ---------------------------------------------------------------------------
    # Test 6: Phonetically similar but unrelated (potential false positives)
    # ---------------------------------------------------------------------------
    print("\n[Test 6] Phonetically similar unrelated (watch for false positives)")
    print("-" * 50)
    print("  Note: High similarity here is expected due to phonetic similarity")

    phonetic_similar = [
        ("Vienna", "Hanoi", "both have similar vowel patterns"),
        ("Mali", "Bali", "rhyming names"),
        ("Chad", "Tchad", "same place, different spelling"),
        ("China", "Ghana", "similar structure"),
        ("Peru", "Teru", "similar ending"),
    ]

    for n1, n2, desc in phonetic_similar:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        print(f"  ? {n1:15} vs {n2:15}: {sim:.4f}  ({desc})")

    # ---------------------------------------------------------------------------
    # Test 7: Embedding space statistics
    # ---------------------------------------------------------------------------
    print("\n[Test 7] Embedding space statistics")
    print("-" * 50)

    test_names = [
        "London", "Paris", "Berlin", "Tokyo", "Moscow", "Beijing",
        "Cairo", "Sydney", "Mumbai", "Lagos", "Lima", "Toronto",
        "Londres", "Londinium", "Sar-e Tanōr", "München", "Москва",
        "東京", "서울", "北京", "Αθήνα", "القاهرة"
    ]

    embs = encoder.encode_batch(test_names)

    print(f"  Embedding shape: {embs.shape}")
    print(f"  Mean: {embs.mean().item():.6f}")
    print(f"  Std:  {embs.std().item():.6f}")
    print(f"  Min:  {embs.min().item():.6f}")
    print(f"  Max:  {embs.max().item():.6f}")
    print(f"  Per-dim std (mean): {embs.std(dim=0).mean().item():.6f}")
    print(f"  L2 norms (should be ~1.0): {embs.norm(dim=1).mean().item():.4f} ± {embs.norm(dim=1).std().item():.4f}")

    # Pairwise similarity distribution
    n = len(test_names)
    sims = []
    for i in range(n):
        for j in range(i + 1, n):
            sims.append(encoder.similarity(embs[i], embs[j]).item())

    sims = torch.tensor(sims)
    print(f"\n  Pairwise similarity distribution (n={len(sims)}):")
    print(f"    Mean: {sims.mean().item():.4f}")
    print(f"    Std:  {sims.std().item():.4f}")
    print(f"    Min:  {sims.min().item():.4f}")
    print(f"    Max:  {sims.max().item():.4f}")
    print(f"    Median: {sims.median().item():.4f}")

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total_core = cross_script_pass + same_script_pass + unrelated_pass + diacritic_pass
    total_tests = len(cross_script_pairs) + len(same_script_pairs) + len(unrelated_pairs) + len(diacritic_pairs)
    print(f"  Core tests passed: {total_core}/{total_tests} ({100*total_core/total_tests:.1f}%)")
    print(f"    - Cross-script:     {cross_script_pass}/{len(cross_script_pairs)}")
    print(f"    - Same-script:      {same_script_pass}/{len(same_script_pairs)}")
    print(f"    - Unrelated:        {unrelated_pass}/{len(unrelated_pairs)}")
    print(f"    - Diacritics:       {diacritic_pass}/{len(diacritic_pairs)}")
    print("=" * 70 + "\n")

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