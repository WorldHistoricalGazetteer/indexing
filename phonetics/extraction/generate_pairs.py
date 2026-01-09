# extractions/generate_pairs.py
"""
Generate positive pairs and training triplets from extracted Parquet data.

This script runs AFTER extract_to_parquet.py and:
1. Scans the ES places index to find co-located toponyms
2. Generates positive pairs with phonetic similarity filtering (>= 0.35)
3. Creates triplets for Phase 1 (random negatives) and Phase 3 (hard negatives)
4. Saves curated test pairs for evaluation

Default namespaces: gn (GeoNames), wd (Wikidata), tgn (Getty TGN)

Usage:
    python -m phonetics.extraction.generate_pairs \
        --es-host "http://localhost:9200" \
        --data-dir /ix1/whcdh/models/phonetic/data/v3 \
        --namespaces gn wd tgn
"""

import argparse
import json
import logging
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from processing.settings import ES_HOST

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


def build_lang_index(toponym_index: Dict[str, Dict]) -> Dict[str, List[str]]:
    logger.info("Building language index for fast negative sampling...")
    lang_map = defaultdict(list)
    for tid, data in toponym_index.items():
        lang = data.get('lang')
        if lang:
            lang_map[lang].append(tid)
    return lang_map


def load_toponym_index(data_dir: Path) -> Dict[str, Dict]:
    logger.info("Loading toponym index from Parquet...")

    # Training data is in 'training/' subdirectory with hive partitioning by script
    training_path = data_dir / 'training'
    if not training_path.exists():
        # Fallback to 'toponyms/' for backward compatibility
        training_path = data_dir / 'toponyms'

    dataset = ds.dataset(training_path, format='parquet', partitioning='hive')

    # Optimization: Use Pandas for faster loading if available
    try:
        table = dataset.to_table(columns=[
            'toponym_id', 'name', 'name_normalized', 'script', 'lang'
        ])
        df = table.to_pandas()
        # Drop duplicates based on ID to ensure unique index
        df = df.drop_duplicates(subset=['toponym_id'])
        # Vectorized dictionary creation
        index = df.set_index('toponym_id').to_dict(orient='index')
    except Exception:
        # Fallback to pure PyArrow iteration
        table = dataset.to_table(columns=[
            'toponym_id', 'name', 'name_normalized', 'script', 'lang'
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
    query = {
        "query": {"terms": {"namespace": namespaces}},
        "_source": ["namespace", "toponyms"]
    }

    for doc in scan(es, index=index, query=query, scroll='20m', size=batch_size):
        place_id = doc['_id']
        source = doc['_source']
        namespace = source.get('namespace', 'other')
        toponyms = source.get('toponyms', [])

        if len(toponyms) >= min_toponyms:
            yield place_id, namespace, toponyms


def normalize_for_comparison(name: str) -> str:
    if not name:
        return ""
    if anyascii:
        normalized = anyascii(name).lower()
    else:
        normalized = name.lower()
    return ''.join(c for c in normalized if c.isalnum())


def phonetic_similarity(name1: str, name2: str) -> float:
    norm1 = normalize_for_comparison(name1)
    norm2 = normalize_for_comparison(name2)

    if not norm1 or not norm2: return 0.0
    if norm1 == norm2: return 1.0

    min_len = min(len(norm1), len(norm2))
    if min_len >= 3 and (norm1 in norm2 or norm2 in norm1):
        return 0.85

    len1, len2 = len(norm1), len(norm2)
    max_len = max(len1, len2)
    if max_len > 3 * min_len: return 0.0

    if len1 < len2:
        return phonetic_similarity(name2, name1)

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


MIN_PHONETIC_SIMILARITY = 0.35


def generate_pairs_from_place(
        toponyms: List[Dict],
        toponym_index: Dict[str, Dict],
        max_pairs: int = 50,
) -> List[Dict]:
    pairs = []
    valid_tops = []

    for top in toponyms:
        toponym_id = top.get('toponym_id', '')
        if not toponym_id: continue

        # Handle ID normalization
        if '@' in toponym_id:
            at_pos = toponym_id.rfind('@')
            name = toponym_id[:at_pos]
            lang_part = toponym_id[at_pos + 1:]
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

    # Safety Cap
    MAX_TOPONYMS_PER_PLACE = 50
    if len(valid_tops) > MAX_TOPONYMS_PER_PLACE:
        valid_tops = random.sample(valid_tops, MAX_TOPONYMS_PER_PLACE)

    for i, (id1, script1) in enumerate(valid_tops):
        for id2, script2 in valid_tops[i + 1:]:
            name1 = toponym_index[id1].get('name', '')
            name2 = toponym_index[id2].get('name', '')

            sim = phonetic_similarity(name1, name2)
            if sim < MIN_PHONETIC_SIMILARITY: continue

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
        lang_index: Dict[str, List[str]],
        num_negatives: int = 5,
) -> List[Tuple[str, str]]:
    if anchor_id not in toponym_index: return []

    anchor_data = toponym_index[anchor_id]
    anchor_name = anchor_data['name_normalized']
    anchor_lang = anchor_data.get('lang')
    negatives = []

    # 1. Orthographic Negatives
    prefix = anchor_name[:2] if len(anchor_name) >= 2 else ""
    candidates = prefix_index.get(prefix, [])
    if len(candidates) > 1000:
        candidates = random.sample(candidates, 1000)

    for cand_id in candidates:
        if cand_id == anchor_id or cand_id in positive_ids: continue
        negatives.append((cand_id, 'ortho_close'))
        if len(negatives) >= num_negatives: break

    # 2. Same-Language Random Negatives
    if len(negatives) < num_negatives and anchor_lang:
        same_lang_candidates = lang_index.get(anchor_lang, [])
        if same_lang_candidates:
            needed = num_negatives - len(negatives)
            samples = random.sample(same_lang_candidates, min(len(same_lang_candidates), needed * 2))
            for tid in samples:
                if tid not in positive_ids and tid != anchor_id:
                    negatives.append((tid, 'random'))
                    if len(negatives) >= num_negatives: break

    return negatives


def _write_batch(output_dir: Path, records: List[Dict], part_num: int, schema: pa.Schema):
    """Generic function to write a batch of records (pairs or triplets) to Parquet."""
    output_path = output_dir / f"part-{part_num:04d}.parquet"
    table = pa.Table.from_pylist(records, schema=schema)
    pq.write_table(table, output_path, compression='snappy')


def generate_pairs(
        es: Elasticsearch,
        data_dir: Path,
        namespaces: List[str],
        output_dir: Path,
        batch_size: int = 50000,
        limit: Optional[int] = None,
) -> int:
    logger.info("Generating positive pairs...")
    toponym_index = load_toponym_index(data_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pairs_buffer = []
    total_pairs = 0
    part_num = 0

    query = {"query": {"terms": {"namespace": namespaces}}}
    total_places = es.count(index='places', body=query)['count']
    if limit: total_places = min(total_places, limit)

    iterator = scan_places_for_pairs(es, namespaces)
    if tqdm: iterator = tqdm(iterator, total=total_places, desc="Scanning places")

    places_processed = 0
    for place_id, namespace, toponyms in iterator:
        pairs = generate_pairs_from_place(toponyms, toponym_index)
        for pair in pairs:
            pair['namespace'] = namespace
            pair['place_id'] = place_id
            pairs_buffer.append(pair)

        total_pairs += len(pairs)
        places_processed += 1

        if len(pairs_buffer) >= batch_size:
            _write_batch(output_dir, pairs_buffer, part_num, PAIRS_SCHEMA)
            part_num += 1
            pairs_buffer = []

        if limit and places_processed >= limit: break

    if pairs_buffer:
        _write_batch(output_dir, pairs_buffer, part_num, PAIRS_SCHEMA)

    logger.info(f"Generated {total_pairs:,} pairs from {places_processed:,} places")
    return total_pairs


def generate_triplets(
        data_dir: Path,
        output_dir: Path,
        phase: str = 'phase1',
        negatives_per_pair: int = 1,
        batch_size: int = 100000,
        limit: Optional[int] = None,
) -> int:
    logger.info(f"Generating triplets for {phase}...")
    toponym_index = load_toponym_index(data_dir)
    all_ids = list(toponym_index.keys())

    prefix_index = {}
    lang_index = {}
    if phase == 'phase3':
        prefix_index = build_prefix_index(toponym_index)
        lang_index = build_lang_index(toponym_index)

    dataset = ds.dataset(data_dir / 'pairs', format='parquet')
    output_dir.mkdir(parents=True, exist_ok=True)
    buffer = []
    part_num = 0
    total_triplets = 0

    logger.info("Building adjacency list for negative filtering...")
    adj = defaultdict(set)
    for batch in dataset.to_batches(columns=['anchor_id', 'positive_id']):
        anchors = batch['anchor_id']
        positives = batch['positive_id']
        for a, p in zip(anchors, positives):
            a_str = a.as_py()
            p_str = p.as_py()
            adj[a_str].add(p_str)
            adj[p_str].add(a_str)

    logger.info("Generating triplets...")
    for batch in dataset.to_batches(columns=['anchor_id', 'positive_id']):
        anchors = batch['anchor_id']
        positives = batch['positive_id']

        for i in range(len(anchors)):
            anchor = anchors[i].as_py()
            positive = positives[i].as_py()
            forbidden = adj[anchor]

            if phase == 'phase1':
                for _ in range(negatives_per_pair):
                    neg = random.choice(all_ids)
                    attempts = 0
                    while (neg == anchor or neg in forbidden) and attempts < 10:
                        neg = random.choice(all_ids)
                        attempts += 1
                    if attempts < 10:
                        buffer.append({
                            'anchor_id': anchor,
                            'positive_id': positive,
                            'negative_id': neg,
                            'negative_type': 'random'
                        })
            else:
                negs = find_hard_negatives(
                    anchor, forbidden, toponym_index,
                    prefix_index, lang_index,
                    negatives_per_pair
                )
                for neg_id, neg_type in negs:
                    buffer.append({
                        'anchor_id': anchor,
                        'positive_id': positive,
                        'negative_id': neg_id,
                        'negative_type': neg_type
                    })

            if len(buffer) >= batch_size:
                _write_batch(output_dir, buffer, part_num, TRIPLETS_SCHEMA)
                part_num += 1
                total_triplets += len(buffer)
                buffer = []

            if limit and total_triplets >= limit: break
        if limit and total_triplets >= limit: break

    if buffer:
        _write_batch(output_dir, buffer, part_num, TRIPLETS_SCHEMA)
        total_triplets += len(buffer)

    logger.info(f"Finished {phase}. Generated {total_triplets:,} triplets.")
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
        {'query': 'Christiania', 'target': 'Oslo@no', 'type': 'historical'},  # Completely different roots (Semantic test)

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
    parser.add_argument('--es-host', default=ES_HOST)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--namespaces', nargs='+', default=['gn', 'wd', 'tgn'],
                        help='Namespaces to use for pair generation (default: gn wd tgn)')
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