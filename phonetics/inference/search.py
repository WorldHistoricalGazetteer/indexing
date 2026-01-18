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

    said

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
try:
    from processing.settings import ES_HOST
except EnvironmentError:
    print("Elasticsearch is not running. Skipping ES connection.")
    ES_HOST = None

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

    if ES_HOST:
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
    # Test 1: Cross-script equivalents (CORE GOAL)
    # Script-specific thresholds based on information density:
    #   - High (>0.85): Vowel-rich scripts, transliterations
    #   - Medium (>0.75): Greek (vowel loss, diacritics collapse)
    #   - Low (>0.65): Unpointed Hebrew, Arabic partial vowels
    # ---------------------------------------------------------------------------
    print("\n[Test 1] Cross-script equivalents (script-aware thresholds)")
    print("-" * 50)

    # Format: (name1, lang1, name2, lang2, description, threshold, script_pair)
    cross_script_pairs = [
        # HIGH THRESHOLD (>0.85): Vowel-rich scripts / transliterations
        ("London", "en", "Лондон", "ru", "Latin/Cyrillic", 0.85),
        ("Moscow", "en", "Москва", "ru", "Latin/Cyrillic", 0.85),
        ("Paris", "en", "Париж", "ru", "Latin/Cyrillic", 0.85),
        ("Berlin", "en", "Берлин", "ru", "Latin/Cyrillic", 0.85),
        ("Kiev", "en", "Київ", "uk", "Latin/Ukrainian", 0.85),
        ("Warsaw", "en", "Варшава", "ru", "Latin/Cyrillic", 0.85),
        ("Prague", "en", "Прага", "ru", "Latin/Cyrillic", 0.85),
        # Chinese (romanized forms have clear phonetic mapping)
        ("Beijing", "en", "北京", "zh", "Latin/Chinese", 0.85),
        ("Shanghai", "en", "上海", "zh", "Latin/Chinese", 0.85),
        ("Guangzhou", "en", "广州", "zh", "Latin/Chinese", 0.85),
        ("Nanjing", "en", "南京", "zh", "Latin/Chinese", 0.85),
        # Korean (clear vowel encoding)
        ("Seoul", "en", "서울", "ko", "Latin/Korean", 0.85),
        ("Busan", "en", "부산", "ko", "Latin/Korean", 0.85),
        ("Incheon", "en", "인천", "ko", "Latin/Korean", 0.85),
        # Devanagari (vowel-rich)
        ("Delhi", "en", "दिल्ली", "hi", "Latin/Devanagari", 0.85),
        ("Mumbai", "en", "मुंबई", "hi", "Latin/Devanagari", 0.85),
        ("Kolkata", "en", "कोलकाता", "hi", "Latin/Devanagari", 0.85),
        # Georgian (vowel-rich)
        ("Tbilisi", "en", "თბილისი", "ka", "Latin/Georgian", 0.85),
        # TRANSLITERATIONS (should be very high)
        ("Moskva", None, "Москва", "ru", "Translit/Cyrillic", 0.85),
        ("Parizh", None, "Париж", "ru", "Translit/Cyrillic", 0.85),

        # MEDIUM THRESHOLD (>0.75): Greek (diacritics collapse, vowel ambiguity)
        # Note: These are semantic equivalences, not pure phonetic matches
        ("Athens", "en", "Αθήνα", "el", "Latin/Greek (semantic)", 0.75),
        ("Thessaloniki", "en", "Θεσσαλονίκη", "el", "Latin/Greek (semantic)", 0.75),
        # Greek transliterations should be higher
        ("Athina", None, "Αθήνα", "el", "Translit/Greek", 0.85),

        # MEDIUM THRESHOLD (>0.75): Arabic (long vowels encoded, shorts often missing)
        ("London", "en", "لندن", "ar", "Latin/Arabic", 0.75),
        ("Damascus", "en", "دمشق", "ar", "Latin/Arabic", 0.75),
        ("Beirut", "en", "بيروت", "ar", "Latin/Arabic", 0.75),

        # LOW THRESHOLD (>0.65): Unpointed Hebrew (vowels completely ambiguous)
        # Model must guess from consonant skeleton only
        ("Jerusalem", "en", "ירושלים", "he", "Latin/Hebrew (unpointed)", 0.65),
        ("Haifa", "en", "חיפה", "he", "Latin/Hebrew (unpointed)", 0.65),
        # Hebrew transliterations with explicit vowels should be higher
        ("Yerushalayim", None, "ירושלים", "he", "Translit/Hebrew", 0.75),
    ]

    cross_script_pass = 0
    script_results = {}  # Track per-script performance
    rank_results = []  # Track ranking performance

    for n1, lang1, n2, lang2, desc, threshold in cross_script_pairs:
        # Encode with language hints for ambiguous scripts
        e1 = encoder.encode(n1, lang=lang1) if lang1 else encoder.encode(n1)
        e2 = encoder.encode(n2, lang=lang2) if lang2 else encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()

        status = "✓" if sim > threshold else "✗"
        if sim > threshold:
            cross_script_pass += 1

        # Track by script pair
        script_key = desc.split()[0]  # e.g., "Latin/Greek"
        if script_key not in script_results:
            script_results[script_key] = []
        script_results[script_key].append((sim, threshold))

        # For rank-based metric: store for later evaluation
        rank_results.append((n1, n2, lang1, lang2, sim, desc))

        # Show threshold inline
        print(f"  {status} {n1:18} vs {n2:18}: {sim:.4f} (>{threshold:.2f}) {desc}")

    print(f"  Overall pass rate: {cross_script_pass}/{len(cross_script_pairs)}")

    # Show per-script summary
    print("\n  Per-script summary:")
    for script_key, results in sorted(script_results.items()):
        sims = [s for s, _ in results]
        avg_sim = sum(sims) / len(sims)
        passed = sum(1 for s, t in results if s > t)
        print(f"    {script_key:30} avg={avg_sim:.3f} pass={passed}/{len(results)}")

    # Rank-based evaluation: Would the correct match be retrieved in top-k?
    print("\n  Rank-based retrieval test:")
    print("    (Tests if correct cross-script match would rank in top-k among distractors)")

    # Create a large multi-script pool of distractor names for ranking test
    # This ensures the model is tested against diverse phonetic and orthographic patterns
    distractor_pool = [
        # Latin script - Europe
        "Paris", "Madrid", "Rome", "Vienna", "Berlin", "Prague", "Warsaw", "Athens",
        "Brussels", "Amsterdam", "Stockholm", "Copenhagen", "Oslo", "Helsinki",
        "Dublin", "Lisbon", "Budapest", "Bucharest", "Sofia", "Zagreb", "Belgrade",
        "Milan", "Naples", "Venice", "Munich", "Hamburg", "Lyon", "Marseille",
        "Barcelona", "Seville", "Valencia", "Krakow", "Gdansk", "Riga", "Vilnius",

        # Latin script - Americas
        "Toronto", "Montreal", "Vancouver", "Chicago", "Boston", "Seattle", "Miami",
        "Lima", "Bogota", "Santiago", "Caracas", "Quito", "Montevideo", "Asuncion",
        "Mexico", "Guadalajara", "Havana", "Panama", "Kingston", "Nassau",

        # Latin script - Asia/Pacific
        "Tokyo", "Sydney", "Melbourne", "Brisbane", "Auckland", "Wellington",
        "Singapore", "Jakarta", "Manila", "Bangkok", "Hanoi", "Saigon",
        "Kuala Lumpur", "Yangon", "Phnom Penh", "Vientiane", "Dili",

        # Latin script - Africa
        "Cairo", "Lagos", "Nairobi", "Johannesburg", "Cape Town", "Casablanca",
        "Tunis", "Algiers", "Tripoli", "Addis Ababa", "Dakar", "Accra", "Kinshasa",
        "Luanda", "Maputo", "Harare", "Lusaka", "Kampala", "Dar es Salaam",

        # Arabic script
        "دمشق", "بيروت", "بغداد", "الرياض", "دبي", "القاهرة", "الدوحة", "عمان",
        "الكويت", "صنعاء", "طرابلس", "الجزائر", "تونس", "الرباط", "مسقط",

        # Cyrillic script
        "Москва", "Санкт-Петербург", "Киев", "Минск", "Варшава", "София", "Белград",
        "Тбилиси", "Ереван", "Баку", "Ташкент", "Алматы", "Бишкек", "Душанбе",

        # Greek script
        "Αθήνα", "Θεσσαλονίκη", "Πάτρα", "Ηράκλειο", "Λάρισα", "Βόλος",

        # Hebrew script
        "ירושלים", "תל אביב", "חיפה", "באר שבע", "נצרת", "אילת",

        # CJK scripts
        "北京", "上海", "广州", "深圳", "南京", "杭州", "成都", "重庆", "西安", "武汉",
        "東京", "大阪", "京都", "名古屋", "横浜", "神戸", "福岡", "札幌",
        "서울", "부산", "인천", "대구", "대전", "광주", "울산",

        # Devanagari script
        "दिल्ली", "मुंबई", "कोलकाता", "चेन्नई", "बेंगलुरु", "हैदराबाद", "अहमदाबाद",

        # Thai script
        "กรุงเทพ", "เชียงใหม่", "ภูเก็ต", "พัทยา", "นครราชสีมา",

        # Georgian script
        "თბილისი", "ბათუმი", "ქუთაისი", "რუსთავი",

        # Armenian script
        "Երևան", "Գյումրի", "Վանաձոր",

        # Bengali script
        "ঢাকা", "চট্টগ্রাম", "খুলনা", "রাজশাহী",

        # Hangul script (additional)
        "평양", "개성", "원산", "함흥",

        # Tamil script
        "சென்னை", "கோயம்புத்தூர்", "மதுரை",

        # Telugu script
        "హైదరాబాద్", "విజయవాడ", "విశాఖపట్నం",

        # Kannada script
        "ಬೆಂಗಳೂರು", "ಮೈಸೂರು", "ಮಂಗಳೂರು",

        # Malayalam script
        "തിരുവനന്തപുരം", "കൊച്ചി", "കോഴിക്കോട്",

        # Gujarati script
        "અમદાવાદ", "સુરત", "વડોદરા",
    ]

    rank_at_1 = 0
    rank_at_5 = 0
    rank_at_10 = 0
    rank_at_20 = 0
    rank_at_50 = 0
    mrr_sum = 0.0

    import random

    for n1, n2, lang1, lang2, sim, desc in rank_results:
        # Create candidate set: target + 99 randomly sampled distractors from diverse pool
        # This creates a challenging 100-way ranking task with multi-script distractors
        available_distractors = [d for d in distractor_pool if d != n1 and d != n2]
        sampled_distractors = random.sample(available_distractors, min(99, len(available_distractors)))
        candidates = [n2] + sampled_distractors

        # Encode query
        query_emb = encoder.encode(n1, lang=lang1) if lang1 else encoder.encode(n1)

        # Encode all candidates with appropriate language hints
        candidate_embs = []
        for cand in candidates:
            if cand == n2:
                # Use correct language for target
                emb = encoder.encode(cand, lang=lang2) if lang2 else encoder.encode(cand)
            else:
                # Distractors use no language hint
                emb = encoder.encode(cand)
            candidate_embs.append(emb)

        candidate_embs = torch.stack(candidate_embs)

        # Compute similarities and rank
        sims = encoder.similarity(query_emb, candidate_embs)
        ranks = torch.argsort(sims, descending=True)

        # Find rank of correct answer (index 0)
        target_rank = (ranks == 0).nonzero(as_tuple=True)[0].item() + 1

        if target_rank == 1:
            rank_at_1 += 1
        if target_rank <= 5:
            rank_at_5 += 1
        if target_rank <= 10:
            rank_at_10 += 1
        if target_rank <= 20:
            rank_at_20 += 1
        if target_rank <= 50:
            rank_at_50 += 1
        mrr_sum += 1.0 / target_rank

    total_pairs = len(rank_results)
    print(f"    (100-way ranking: 1 target + 99 multi-script distractors per query)")
    print(f"    Recall@1:  {rank_at_1}/{total_pairs} ({100*rank_at_1/total_pairs:.1f}%)")
    print(f"    Recall@5:  {rank_at_5}/{total_pairs} ({100*rank_at_5/total_pairs:.1f}%)")
    print(f"    Recall@10: {rank_at_10}/{total_pairs} ({100*rank_at_10/total_pairs:.1f}%)")
    print(f"    Recall@20: {rank_at_20}/{total_pairs} ({100*rank_at_20/total_pairs:.1f}%)")
    print(f"    Recall@50: {rank_at_50}/{total_pairs} ({100*rank_at_50/total_pairs:.1f}%)")
    print(f"    MRR: {mrr_sum/total_pairs:.3f}")

    # ---------------------------------------------------------------------------
    # Test 2: Historical variant spellings (should be > 0.75)
    # Same place, different historical orthography - phonetically similar
    # These test the model's ability to handle historical spelling variations
    # ---------------------------------------------------------------------------
    print("\n[Test 2] Historical variant spellings (expected: > 0.75)")
    print("-" * 50)
    print("  Note: Same place, historical orthographic variations")

    historical_pairs = [
        ("Hampstead", "Hamsted", "dropped p"),
        ("Christchurch", "Cristechurch", "i/h variation"),
        ("Edinburgh", "Edinborough", "burgh/borough"),
        ("Shrewsbury", "Shrowesbury", "vowel shift"),
        ("Worcester", "Worcestre", "medieval -re"),
        ("Gloucester", "Glocester", "ou/o variation"),
        ("Leicester", "Leycester", "ei/ey variation"),
        ("Warwick", "Warwike", "ck/ke variation"),
        ("Lincoln", "Lincolne", "silent -e"),
        ("Norwich", "Norwiche", "silent -e"),
        ("Canterbury", "Canterburie", "y/ie variation"),
        ("Durham", "Duresme", "medieval French"),
        ("York", "Yorke", "silent -e"),
        ("Bath", "Bathe", "silent -e"),
    ]

    historical_pass = 0
    for n1, n2, desc in historical_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "✓" if sim > 0.75 else "✗"
        if sim > 0.75:
            historical_pass += 1
        print(f"  {status} {n1:18} vs {n2:18}: {sim:.4f}  ({desc})")
    print(f"  Pass rate: {historical_pass}/{len(historical_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 3: Phonetically similar cross-language variants (should be > 0.70)
    # Same script, different language, but phonetically similar
    # These SHOULD match despite different languages - tests phonetic vs orthographic
    # ---------------------------------------------------------------------------
    print("\n[Test 3] Phonetically similar cross-language variants (expected: > 0.70)")
    print("-" * 50)
    print("  Note: Different languages, but phonetically similar (should match)")

    similar_crosslang_pairs = [
        ("London", "Londyn", "en/pl - phonetically close"),
        ("Moscow", "Moscou", "en/fr - phonetically close"),
        ("Moscow", "Mosca", "en/it - phonetically close"),
        ("Rome", "Roma", "en/it - phonetically close"),
        ("Naples", "Napoli", "en/it - phonetically close"),
        ("Venice", "Venezia", "en/it - minor shift"),
        ("Milan", "Milano", "en/it - phonetically close"),
        ("Lisbon", "Lisboa", "en/pt - phonetically close"),
        ("Seville", "Sevilla", "en/es - phonetically close"),
        ("Copenhagen", "København", "en/da - phonetically close"),
        ("Warsaw", "Warszawa", "en/pl - phonetically close"),
    ]

    similar_crosslang_pass = 0
    for n1, n2, desc in similar_crosslang_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "✓" if sim > 0.70 else "✗"
        if sim > 0.70:
            similar_crosslang_pass += 1
        print(f"  {status} {n1:18} vs {n2:18}: {sim:.4f}  ({desc})")
    print(f"  Pass rate: {similar_crosslang_pass}/{len(similar_crosslang_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 4: Phonetically distinct exonyms (should be < 0.50)
    # Same place, different languages - phonetically DIFFERENT
    # These should NOT match because the model is phonetic, not geographic
    # Note: Excludes pairs like "Moscow/Moscou" which ARE phonetically similar
    # ---------------------------------------------------------------------------
    print("\n[Test 4] Phonetically distinct exonyms (expected: < 0.50)")
    print("-" * 50)
    print("  Note: Same place geographically, but phonetically very different")

    exonym_pairs = [
        # Vowel-shift exonyms (marginal phonetic difference)
        ("London", "Londres", "en/fr - vowel shift"),
        ("London", "Londra", "en/it - vowel shift"),
        ("Munich", "München", "en/de - umlaut difference"),

        # Complete phonetic divergence (should definitely NOT match)
        ("Vienna", "Wien", "en/de - completely different"),
        ("Florence", "Firenze", "en/it - different consonants"),
        ("Prague", "Praha", "en/cs - different phonetics"),
        ("Germany", "Deutschland", "en/de - completely different"),
        ("Finland", "Suomi", "en/fi - completely different"),
        ("Greece", "Hellas", "en/el - completely different"),
        ("Japan", "Nihon", "en/ja - completely different"),
        ("Egypt", "Misr", "en/ar - completely different"),
        ("Hungary", "Magyarország", "en/hu - completely different"),
        ("Albania", "Shqipëria", "en/sq - completely different"),
        ("Georgia", "საქართველო", "en/ka - completely different"),
    ]

    exonym_pass = 0
    for n1, n2, desc in exonym_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        # More lenient for vowel-shift cases, strict for complete divergence
        threshold = 0.50 if "vowel shift" in desc or "umlaut" in desc else 0.40
        status = "✓" if sim < threshold else "✗"
        if sim < threshold:
            exonym_pass += 1
        print(f"  {status} {n1:18} vs {n2:18}: {sim:.4f} (<{threshold:.2f}) {desc}")
    print(f"  Pass rate: {exonym_pass}/{len(exonym_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 5: Unrelated pairs (should be < 0.50)
    # Completely different places - should have low similarity
    # ---------------------------------------------------------------------------
    print("\n[Test 5] Unrelated pairs (expected: < 0.50)")
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
        ("Dublin", "Hanoi"),
        ("Amsterdam", "Nairobi"),
        ("Stockholm", "Jakarta"),
        ("Brussels", "Manila"),
        ("Vienna", "Santiago"),
        ("Prague", "Taipei"),
        ("Warsaw", "Johannesburg"),
    ]

    unrelated_pass = 0
    for n1, n2 in unrelated_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "✓" if sim < 0.50 else "✗"
        if sim < 0.50:
            unrelated_pass += 1
        print(f"  {status} {n1:18} vs {n2:18}: {sim:.4f}")
    print(f"  Pass rate: {unrelated_pass}/{len(unrelated_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 6: Diacritic/minor variants (expected: > 0.90)
    # Same name, minor orthographic differences - validates robustness
    # ---------------------------------------------------------------------------
    print("\n[Test 6] Diacritic/minor variants (expected: > 0.90)")
    print("-" * 50)

    diacritic_pairs = [
        ("Zurich", "Zürich"),
        ("Gdansk", "Gdańsk"),
        ("Krakow", "Kraków"),
        ("Malmo", "Malmö"),
        ("Sao Paulo", "São Paulo"),
        ("Bogota", "Bogotá"),
        ("Poznan", "Poznań"),
        ("Chisinau", "Chișinău"),
        ("Brasov", "Brașov"),
        ("Timisoara", "Timișoara"),
    ]

    diacritic_pass = 0
    for n1, n2 in diacritic_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "✓" if sim > 0.90 else "✗"
        if sim > 0.90:
            diacritic_pass += 1
        print(f"  {status} {n1:18} vs {n2:18}: {sim:.4f}")
    print(f"  Pass rate: {diacritic_pass}/{len(diacritic_pairs)}")

    # ---------------------------------------------------------------------------
    # Test 7: Exonym pairs (informational - no pass/fail)
    # These are genuinely different names for same place
    # ---------------------------------------------------------------------------
    print("\n[Test 7] Exonym pairs (informational - no pass/fail)")
    print("-" * 50)
    print("  Note: These are genuinely different names for same place")

    confusable_pairs = [
        ("Vienna", "Hanoi", "similar vowel patterns"),
        ("Mali", "Bali", "rhyming"),
        ("Chad", "Tchad", "same place!"),
        ("China", "Ghana", "similar structure"),
        ("Peru", "Beirut", "shared sounds"),
        ("Niger", "Nigeria", "substring"),
        ("Guinea", "Guyana", "similar"),
        ("Austria", "Australia", "near-homophone"),
        ("Iran", "Iraq", "similar"),
        ("Sweden", "Sudan", "similar start"),
        ("Slovakia", "Slovenia", "confusable"),
        ("Dominica", "Dominican Republic", "substring"),
        ("Georgia", "Georgia", "country vs US state"),
        ("Springfield", "Springfield", "many places"),
    ]

    for n1, n2, desc in confusable_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        status = "⚠" if sim > 0.75 else " "
        print(f"  {status} {n1:18} vs {n2:18}: {sim:.4f}  ({desc})")

    # ---------------------------------------------------------------------------
    # Test 8: Short names (edge case)
    # Very short names are harder to distinguish
    # ---------------------------------------------------------------------------
    print("\n[Test 8] Short names (edge case)")
    print("-" * 50)
    print("  Note: Very short names are harder to distinguish")

    short_pairs = [
        ("Ur", "Or", "2 chars"),
        ("Po", "Pa", "2 chars"),
        ("Goa", "Gao", "3 chars"),
        ("Boa", "Bua", "3 chars similar"),
        ("Rome", "Rame", "4 chars"),
        ("Lima", "Lama", "4 chars"),
        ("Oslo", "Aslo", "4 chars anagram-ish"),
        ("Bern", "Bonn", "4 chars similar"),
    ]

    for n1, n2, desc in short_pairs:
        e1, e2 = encoder.encode(n1), encoder.encode(n2)
        sim = encoder.similarity(e1, e2).item()
        print(f"  ? {n1:18} vs {n2:18}: {sim:.4f}  ({desc})")

    # ---------------------------------------------------------------------------
    # Test 9: Embedding space statistics
    # Technical validation of embedding properties
    # ---------------------------------------------------------------------------
    print("\n[Test 9] Embedding space statistics")
    print("-" * 50)

    test_names = [
        "London", "Paris", "Berlin", "Tokyo", "Moscow", "Beijing",
        "Cairo", "Sydney", "Mumbai", "Lagos", "Lima", "Toronto",
        "Londres", "Sar-e Tanōr", "München", "Москва",
        "東京", "서울", "北京", "Αθήνα", "القاهرة", "ירושלים",
        "दिल्ली", "กรุงเทพ", "თბილისი"
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
    total_core = (cross_script_pass + historical_pass + similar_crosslang_pass +
                  exonym_pass + unrelated_pass + diacritic_pass)
    total_tests = (len(cross_script_pairs) + len(historical_pairs) +
                   len(similar_crosslang_pairs) + len(exonym_pairs) +
                   len(unrelated_pairs) + len(diacritic_pairs))
    print(f"  Core tests passed: {total_core}/{total_tests} ({100*total_core/total_tests:.1f}%)")
    print(f"    - Cross-script (script-aware):     {cross_script_pass}/{len(cross_script_pairs)}")
    print(f"    - Historical variants:             {historical_pass}/{len(historical_pairs)}")
    print(f"    - Phonetically similar X-lang:     {similar_crosslang_pass}/{len(similar_crosslang_pairs)}")
    print(f"    - Phonetically distinct (reject):  {exonym_pass}/{len(exonym_pairs)}")
    print(f"    - Unrelated:                       {unrelated_pass}/{len(unrelated_pairs)}")
    print(f"    - Diacritics:                      {diacritic_pass}/{len(diacritic_pairs)}")
    print("\n  Testing methodology improvements:")
    print("    ✓ Script-specific thresholds (vowel-poor scripts get lower thresholds)")
    print("    ✓ Language hints for ambiguous scripts (Greek, Hebrew, Arabic)")
    print("    ✓ Separated phonetic equivalence from semantic equivalence")
    print("    ✓ Rank-based metrics (Recall@k, MRR) complement absolute thresholds")
    print("    ✓ Per-script performance distributions logged")
    print("=" * 70 + "\n")

    ##############################################

    if ES_HOST:
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