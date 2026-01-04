# extractions/generate_pairs.py
"""
Generate positive pairs and training triplets from extracted Parquet data.

This script:
1. Scans the ES places index to find co-located toponyms
2. Generates positive pairs for contrastive learning
3. Creates triplets for Phase 1 (random negatives) and Phase 3 (hard negatives)
4. Saves curated test pairs for evaluation

Output:
    /data/v2/
    ├── pairs/
    │   └── part-*.parquet
    ├── triplets/
    │   ├── phase1/
    │   │   └── part-*.parquet
    │   └── phase3/
    │       └── part-*.parquet
    └── test_pairs/
        ├── cross_script.json
        └── curated.json

Usage:
    python -m phonetics.extraction.generate_pairs \
        --es-host localhost:9200 \
        --data-dir /ix1/whcdh/models/phonetic/data/v2 \
        --namespaces gn wd tgn pl iv gb
"""

import argparse
import json
import logging
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import scan
except ImportError:
    print("Error: elasticsearch package required")
    sys.exit(1)

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pyarrow.dataset as ds
except ImportError:
    print("Error: pyarrow package required")
    sys.exit(1)

try:
    from anyascii import anyascii
except ImportError:
    anyascii = None
    logger.warning("anyascii not found! Cross-script pairs will be filtered out.")

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import Script

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Parquet schemas
PAIRS_SCHEMA = pa.schema([
    ('anchor_id', pa.string()),
    ('positive_id', pa.string()),
    ('namespace', pa.string()),
    ('place_id', pa.string()),
    ('is_cross_script', pa.bool_()),
])

TRIPLETS_SCHEMA = pa.schema([
    ('anchor_id', pa.string()),
    ('positive_id', pa.string()),
    ('negative_id', pa.string()),
    ('negative_type', pa.string()),  # 'random', 'ortho_close', 'cross_script'
])


def build_prefix_index(toponym_index: Dict[str, Dict]) -> Dict[str, List[str]]:
    logger.info("Building prefix index for hard negative mining...")
    prefix_map = defaultdict(list)
    for tid, data in toponym_index.items():
        name = data.get('name_normalized', '')
        if name and len(name) >= 2:  # Use 2 chars for better recall
            prefix = name[:2]
            prefix_map[prefix].append(tid)
    return prefix_map


def load_toponym_index(data_dir: Path) -> Dict[str, Dict]:
    """
    Load toponym metadata into memory for fast lookup.

    Returns:
        Dict mapping toponym_id to metadata dict
    """
    logger.info("Loading toponym index from Parquet...")

    dataset = ds.dataset(data_dir / 'toponyms', format='parquet', partitioning='hive')
    table = dataset.to_table(columns=[
        'toponym_id', 'name', 'name_normalized', 'script', 'lang',
        'epitran_supported', 'split'
    ])

    index = {}
    for i in range(len(table)):
        row = {col: table[col][i].as_py() for col in table.column_names}
        index[row['toponym_id']] = row

    logger.info(f"Loaded {len(index):,} toponyms")
    return index


def scan_places_for_pairs(
        es: Elasticsearch,
        namespaces: List[str],
        index: str = 'places',
        batch_size: int = 1000,
        min_toponyms: int = 2,
) -> Iterator[Tuple[str, str, List[Dict]]]:
    """
    Scan places index and yield (place_id, namespace, toponyms) tuples.

    Only yields places with at least min_toponyms.
    """
    query = {
        "query": {"terms": {"namespace": namespaces}},
        "_source": ["namespace", "toponyms"]
    }

    for doc in scan(es, index=index, query=query, scroll='10m', size=batch_size):
        place_id = doc['_id']
        source = doc['_source']
        namespace = source.get('namespace', 'other')
        toponyms = source.get('toponyms', [])

        if len(toponyms) >= min_toponyms:
            yield place_id, namespace, toponyms


def normalize_for_comparison(name: str) -> str:
    """
    Normalize a toponym for phonetic comparison.
    Romanizes via anyascii and removes non-alphanumeric characters.
    """
    if not name:
        return ""

    # Romanize if possible
    if anyascii:
        normalized = anyascii(name).lower()
    else:
        normalized = name.lower()

    # Keep only alphanumeric chars (removes hyphens, spaces, punctuation)
    # This ensures "Non-Violence" == "nonviolence"
    return ''.join(c for c in normalized if c.isalnum())


def phonetic_similarity(name1: str, name2: str) -> float:
    """
    Compute phonetic similarity using normalized Levenshtein ratio.

    Returns value between 0.0 (completely different) and 1.0 (identical).
    Used to filter out semantically equivalent but phonetically unrelated
    pairs like "Germany" / "Deutschland".
    """
    # 1. Normalize (Romanize + clean)
    norm1 = normalize_for_comparison(name1)
    norm2 = normalize_for_comparison(name2)

    if not norm1 or not norm2:
        return 0.0

    # 2. Exact Match Shortcut (Fastest)
    if norm1 == norm2:
        return 1.0

    # 3. Substring Shortcut (Fast)
    # If "York" matches "New York", we consider that phonetically relevant
    min_len = min(len(norm1), len(norm2))
    if min_len >= 3 and (norm1 in norm2 or norm2 in norm1):
        return 0.85

    # 4. Length Heuristic (Fast)
    # If one string is 3x longer than the other, they are likely different
    # e.g. "US" vs "United States"
    len1, len2 = len(norm1), len(norm2)
    max_len = max(len1, len2)
    if max_len > 3 * min_len:
        return 0.0

    # 5. Levenshtein Distance (Slow - Run only if necessary)
    if len1 < len2:
        return phonetic_similarity(name2, name1)

    # Memory optimized row-based Levenshtein
    previous_row = range(len2 + 1)
    for i, c1 in enumerate(norm1):
        current_row = [i + 1]
        for j, c2 in enumerate(norm2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    distance = previous_row[len2]

    return 1.0 - (distance / max_len)


# Minimum phonetic similarity threshold for creating pairs
# This filters out unrelated exonyms like "Germany"/"Deutschland"
# Set relatively low (0.35) because:
# - We want to catch obvious mismatches
# - But not be too aggressive (some valid pairs have low scores)
# - The model will learn finer distinctions during training
MIN_PHONETIC_SIMILARITY = 0.35


def generate_pairs_from_place(
        toponyms: List[Dict],
        toponym_index: Dict[str, Dict],
        max_pairs: int = 50,
) -> List[Dict]:
    """
    Generate all positive pairs from a place's toponyms.

    Returns list of pair dicts with cross-script detection.
    """
    pairs = []

    # Build list of valid toponym IDs with their scripts
    # ES places documents have toponym_id already defined (e.g., "Non-Violence@lb")
    valid_tops = []
    for top in toponyms:
        # Use the pre-defined toponym_id from ES
        toponym_id = top.get('toponym_id', '')

        if not toponym_id:
            continue

        # Normalize to canonical format (name@lang or name@)
        # The toponym_id from ES may have variants like "name@en-US"
        # but our index uses "name@en"
        if '@' in toponym_id:
            at_pos = toponym_id.rfind('@')
            name = toponym_id[:at_pos]
            lang_part = toponym_id[at_pos + 1:]
            # Strip variant (e.g., "en-US" -> "en")
            if '-' in lang_part:
                lang = lang_part.split('-', 1)[0]
            else:
                lang = lang_part
            canonical_id = f"{name}@{lang}" if lang else f"{name}@"
        else:
            canonical_id = f"{toponym_id}@"

        if canonical_id in toponym_index:
            script = toponym_index[canonical_id].get('script', 'OTHER')
            valid_tops.append((canonical_id, script))

    # SAFETY CAP: If a place has too many names, sample them down
    # to prevent O(N^2) explosion on super-nodes (like 'United States')
    MAX_TOPONYMS_PER_PLACE = 50
    if len(valid_tops) > MAX_TOPONYMS_PER_PLACE:
        valid_tops = random.sample(valid_tops, MAX_TOPONYMS_PER_PLACE)

    # Generate all pairs with phonetic similarity filtering
    for i, (id1, script1) in enumerate(valid_tops):
        for id2, script2 in valid_tops[i + 1:]:
            # Get names for similarity check
            name1 = toponym_index[id1].get('name', '')
            name2 = toponym_index[id2].get('name', '')

            # Filter semantic translations (Germany != Deutschland)
            sim = phonetic_similarity(name1, name2)
            if sim < MIN_PHONETIC_SIMILARITY:
                continue

            is_cross_script = script1 != script2
            pairs.append({
                'anchor_id': id1,
                'positive_id': id2,
                'is_cross_script': is_cross_script,
            })

    if len(pairs) > max_pairs:
        pairs = random.sample(pairs, max_pairs)

    return pairs


def find_hard_negatives(
        anchor_id: str,
        positive_ids: Set[str],
        toponym_index: Dict[str, Dict],
        prefix_index: Dict[str, List[str]],
        num_negatives: int = 5,
) -> List[Tuple[str, str]]:
    """
    Find hard negatives for a given anchor.

    Hard negative types:
    - 'ortho_close': Similar orthography but different place
    - 'cross_script': Same name in different script
    - 'random': Random negative from same language

    Returns list of (negative_id, negative_type) tuples.
    """
    anchor_data = toponym_index.get(anchor_id)
    if not anchor_data:
        return []

    anchor_name = anchor_data['name_normalized']
    anchor_script = anchor_data.get('script', 'OTHER')
    anchor_lang = anchor_data.get('lang', '')

    negatives = []

    # Optimized Candidate Search
    anchor_prefix = anchor_name[:2] if len(anchor_name) >= 2 else ""
    raw_candidates = prefix_index.get(anchor_prefix, [])

    # Sample if too many to check (speed optimization)
    if len(raw_candidates) > 1000:
        raw_candidates = random.sample(raw_candidates, 1000)

    candidates = []
    for tid in raw_candidates:
        if tid in positive_ids or tid == anchor_id:
            continue
        candidates.append((tid, 'ortho_close'))

    # Sample from candidates
    if candidates:
        negatives.extend(random.sample(candidates, min(len(candidates), num_negatives // 2)))

    # Add random negatives from same language
    if anchor_lang:
        same_lang = [
            (tid, 'random') for tid, tdata in toponym_index.items()
            if tdata.get('lang') == anchor_lang
               and tid not in positive_ids
               and tid != anchor_id
        ]
        if same_lang:
            negatives.extend(random.sample(same_lang, min(len(same_lang), num_negatives - len(negatives))))

    return negatives[:num_negatives]


def generate_pairs(
        es: Elasticsearch,
        data_dir: Path,
        namespaces: List[str],
        output_dir: Path,
        batch_size: int = 50000,
        limit: Optional[int] = None,
) -> int:
    """
    Generate positive pairs from places index.

    Returns number of pairs generated.
    """
    logger.info("Generating positive pairs...")

    toponym_index = load_toponym_index(data_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    pairs_buffer = []
    total_pairs = 0
    part_num = 0

    # Count places
    query = {"query": {"terms": {"namespace": namespaces}}}
    total_places = es.count(index='places', body=query)['count']
    if limit:
        total_places = min(total_places, limit)

    iterator = scan_places_for_pairs(es, namespaces)
    if tqdm:
        iterator = tqdm(iterator, total=total_places, desc="Scanning places")

    places_processed = 0
    for place_id, namespace, toponyms in iterator:
        pairs = generate_pairs_from_place(toponyms, toponym_index)

        for pair in pairs:
            pair['namespace'] = namespace
            pair['place_id'] = place_id
            pairs_buffer.append(pair)

        total_pairs += len(pairs)
        places_processed += 1

        # Flush buffer
        if len(pairs_buffer) >= batch_size:
            _write_pairs_batch(output_dir, pairs_buffer, part_num, PAIRS_SCHEMA)
            part_num += 1
            pairs_buffer = []

        if limit and places_processed >= limit:
            break

    # Final flush
    if pairs_buffer:
        _write_pairs_batch(output_dir, pairs_buffer, part_num, PAIRS_SCHEMA)

    logger.info(f"Generated {total_pairs:,} pairs from {places_processed:,} places")
    return total_pairs


def _write_pairs_batch(output_dir: Path, records: List[Dict], part_num: int, schema: pa.Schema):
    """Write a batch of pairs to Parquet."""
    output_path = output_dir / f"part-{part_num:04d}.parquet"
    table = pa.Table.from_pylist(records, schema=schema)
    pq.write_table(table, output_path, compression='snappy')


def generate_triplets(
        data_dir: Path,
        output_dir: Path,
        phase: str = 'phase1',
        negatives_per_pair: int = 1,
        batch_size: int = 100000,
        limit: Optional[int] = None,
) -> int:
    """
    Generate training triplets from pairs.

    Args:
        data_dir: Base data directory
        output_dir: Output directory for triplets
        phase: 'phase1' (random negatives) or 'phase3' (hard negatives)
        negatives_per_pair: Number of negatives per positive pair
        batch_size: Batch size for writing
        limit: Limit number of triplets for testing

    Returns:
        Number of triplets generated
    """
    logger.info(f"Generating triplets for {phase}...")

    toponym_index = load_toponym_index(data_dir)
    all_toponym_ids = list(toponym_index.keys())

    prefix_index = build_prefix_index(toponym_index)

    # Load pairs
    pairs_dataset = ds.dataset(data_dir / 'pairs', format='parquet')
    pairs_table = pairs_dataset.to_table(columns=['anchor_id', 'positive_id'])

    output_dir.mkdir(parents=True, exist_ok=True)

    triplets_buffer = []
    total_triplets = 0
    part_num = 0

    # Build positive sets per anchor for hard negative mining
    anchor_positives: Dict[str, Set[str]] = defaultdict(set)
    for i in range(len(pairs_table)):
        anchor = pairs_table['anchor_id'][i].as_py()
        positive = pairs_table['positive_id'][i].as_py()
        anchor_positives[anchor].add(positive)
        anchor_positives[positive].add(anchor)  # Symmetric

    iterator = range(len(pairs_table))
    if tqdm:
        iterator = tqdm(iterator, desc=f"Generating {phase} triplets")

    for i in iterator:
        anchor_id = pairs_table['anchor_id'][i].as_py()
        positive_id = pairs_table['positive_id'][i].as_py()

        positives_for_anchor = anchor_positives[anchor_id]

        if phase == 'phase1':
            # Random negatives
            for _ in range(negatives_per_pair):
                negative_id = random.choice(all_toponym_ids)
                while negative_id in positives_for_anchor or negative_id == anchor_id:
                    negative_id = random.choice(all_toponym_ids)

                triplets_buffer.append({
                    'anchor_id': anchor_id,
                    'positive_id': positive_id,
                    'negative_id': negative_id,
                    'negative_type': 'random',
                })
        else:
            # Hard negatives for phase3
            hard_negs = find_hard_negatives(
                anchor_id, positives_for_anchor, toponym_index,
                prefix_index,
                num_negatives=negatives_per_pair
            )

            for negative_id, neg_type in hard_negs:
                triplets_buffer.append({
                    'anchor_id': anchor_id,
                    'positive_id': positive_id,
                    'negative_id': negative_id,
                    'negative_type': neg_type,
                })

        total_triplets += len(triplets_buffer) - (total_triplets % batch_size)

        # Flush buffer
        if len(triplets_buffer) >= batch_size:
            _write_pairs_batch(output_dir, triplets_buffer, part_num, TRIPLETS_SCHEMA)
            part_num += 1
            triplets_buffer = []

        if limit and total_triplets >= limit:
            break

    # Final flush
    if triplets_buffer:
        _write_pairs_batch(output_dir, triplets_buffer, part_num, TRIPLETS_SCHEMA)
        total_triplets += len(triplets_buffer)

    logger.info(f"Generated {total_triplets:,} triplets")
    return total_triplets


def generate_curated_test_pairs(
        data_dir: Path,
        output_dir: Path,
) -> int:
    """
    Generate curated test pairs for evaluation.

    Creates:
    - cross_script.json: Pairs of same place name in different scripts
    - historical_variants.json: Known historical spelling variants
    """
    logger.info("Generating curated test pairs...")

    toponym_index = load_toponym_index(data_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find cross-script pairs
    cross_script_pairs = []

    # Group by name (case-insensitive)
    name_groups: Dict[str, List[str]] = defaultdict(list)
    for tid, tdata in toponym_index.items():
        name_key = tdata['name_normalized']
        name_groups[name_key].append(tid)

    # Find groups with multiple scripts
    for name, tids in name_groups.items():
        if len(tids) < 2:
            continue

        scripts = set(toponym_index[tid]['script'] for tid in tids)
        if len(scripts) > 1:
            # Multi-script group
            for i, tid1 in enumerate(tids):
                for tid2 in tids[i + 1:]:
                    if toponym_index[tid1]['script'] != toponym_index[tid2]['script']:
                        cross_script_pairs.append({
                            'id1': tid1,
                            'id2': tid2,
                            'name': name,
                            'scripts': [toponym_index[tid1]['script'], toponym_index[tid2]['script']],
                        })

    # Save cross-script pairs
    with open(output_dir / 'cross_script.json', 'w') as f:
        json.dump(cross_script_pairs, f, indent=2)

    logger.info(f"Generated {len(cross_script_pairs)} cross-script test pairs")

    # Curated historical variants (manually defined examples)
    curated_pairs = [
        # --- 1. Historical & Archaic Forms (The "Deep Time" Test) ---
        {'query': 'Lundenwic', 'target': 'London@en', 'type': 'historical'},
        {'query': 'Eboracum', 'target': 'York@en', 'type': 'historical'},
        {'query': 'Constantinople', 'target': 'Istanbul@en', 'type': 'historical'},
        {'query': 'Akyab', 'target': 'Sittwe@en', 'type': 'historical'},
        {'query': 'Peking', 'target': 'Beijing@en', 'type': 'historical'},  # Legacy Romanization
        {'query': 'Bombay', 'target': 'Mumbai@en', 'type': 'historical'},  # Colonial renaming
        {'query': 'Danzig', 'target': 'Gdańsk@pl', 'type': 'historical'},  # German/Polish shift
        {'query': 'Christiania', 'target': 'Oslo@no', 'type': 'historical'},
        # Completely different roots (Semantic test)

        # --- 2. Typos & OCR Noise (The "Robustness" Test) ---
        {'query': 'Londn', 'target': 'London@en', 'type': 'typo'},
        {'query': 'Munchin', 'target': 'Munich@en', 'type': 'typo'},
        {'query': 'Amstedram', 'target': 'Amsterdam@en', 'type': 'typo'},
        {'query': 'Filidelphia', 'target': 'Philadelphia@en', 'type': 'typo'},
        {'query': 'Manhatten', 'target': 'Manhattan@en', 'type': 'typo'},  # Common vowel swap
        {'query': 'Glascow', 'target': 'Glasgow@en', 'type': 'typo'},  # c/g confusion
        {'query': 'San Fransisco', 'target': 'San Francisco@en', 'type': 'typo'},  # s/c confusion
        {'query': 'Edinborough', 'target': 'Edinburgh@en', 'type': 'typo'},  # Phonetic spelling of silent letters

        # --- 3. Cross-Script (The "Universal" Test) ---
        {'query': 'Москва', 'target': 'Moscow@en', 'type': 'script_variant'},
        {'query': 'Київ', 'target': 'Kyiv@en', 'type': 'script_variant'},
        {'query': 'Αθήνα', 'target': 'Athens@en', 'type': 'script_variant'},
        {'query': 'القاهرة', 'target': 'Cairo@en', 'type': 'script_variant'},
        {'query': 'ירושלים', 'target': 'Jerusalem@en', 'type': 'script_variant'},
        {'query': 'Тбилиси', 'target': 'Tbilisi@en', 'type': 'script_variant'},  # Cyrillic (Georgian capital)
        {'query': 'بغداد', 'target': 'Baghdad@en', 'type': 'script_variant'},  # Arabic
        {'query': 'मुंबई', 'target': 'Mumbai@en', 'type': 'script_variant'},  # Devanagari
        {'query': 'Σπάρτη', 'target': 'Sparta@en', 'type': 'script_variant'},  # Greek

        # --- 4. CJK Romanization Pipeline (The "AnyAscii" Test) ---
        {'query': '北京', 'target': 'Beijing@en', 'type': 'cjk_pipeline'},
        {'query': '東京', 'target': 'Tokyo@en', 'type': 'cjk_pipeline'},
        {'query': '上海', 'target': 'Shanghai@en', 'type': 'cjk_pipeline'},  # Chinese Hanzi
        {'query': '大阪', 'target': 'Osaka@en', 'type': 'cjk_pipeline'},  # Japanese Kanji

        # --- 5. Hard Cross-Lingual (The "Cognate" Test) ---
        {'query': 'München', 'target': 'Munich@en', 'type': 'translation'},
        {'query': 'Köln', 'target': 'Cologne@en', 'type': 'translation'},
        {'query': 'Praha', 'target': 'Prague@en', 'type': 'translation'},
        {'query': 'Wien', 'target': 'Vienna@en', 'type': 'translation'},
        {'query': 'Firenze', 'target': 'Florence@en', 'type': 'translation'},  # Italian
        {'query': 'Sevilla', 'target': 'Seville@en', 'type': 'translation'},  # Spanish
        {'query': 'Warszawa', 'target': 'Warsaw@en', 'type': 'translation'},  # Polish
        {'query': 'København', 'target': 'Copenhagen@en', 'type': 'translation'},  # Danish
    ]

    with open(output_dir / 'curated.json', 'w') as f:
        json.dump(curated_pairs, f, indent=2)

    logger.info(f"Saved {len(curated_pairs)} curated test pairs")

    return len(cross_script_pairs) + len(curated_pairs)


def main():
    parser = argparse.ArgumentParser(description='Generate pairs and triplets')
    parser.add_argument('--es-host', default='localhost:9200')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--namespaces', nargs='+', default=['gn', 'pl', 'iv'])
    parser.add_argument('--batch-size', type=int, default=50000)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--skip-pairs', action='store_true', help='Skip pair generation')
    parser.add_argument('--skip-triplets', action='store_true', help='Skip triplet generation')
    args = parser.parse_args()

    es = Elasticsearch(args.es_host)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    # Generate pairs
    if not args.skip_pairs:
        generate_pairs(
            es, args.data_dir, args.namespaces,
            args.data_dir / 'pairs',
            args.batch_size, args.limit
        )

    # Generate triplets
    if not args.skip_triplets:
        generate_triplets(
            args.data_dir,
            args.data_dir / 'triplets' / 'phase1',
            phase='phase1',
            limit=args.limit
        )

        generate_triplets(
            args.data_dir,
            args.data_dir / 'triplets' / 'phase3',
            phase='phase3',
            limit=args.limit
        )

    # Generate curated test pairs
    generate_curated_test_pairs(args.data_dir, args.data_dir / 'test_pairs')

    logger.info("Done!")


if __name__ == '__main__':
    main()