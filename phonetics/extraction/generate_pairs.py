# extractions/generate_pairs.py
"""
Generate positive pairs and training triplets from SQLite database.

This script runs AFTER rebuild_toponyms_index.py and implements the
Data Selection and Curation Criteria from the paper:

1. Script-Stratified Sampling: Quota-based sampling (default 100K pairs per script-pair)
2. Namespace Filtering: Only gn, wd, tgn namespaces
3. Phonetic Similarity Filtering: Pairs must have similarity >= 0.35
4. Cross-Script Weighting: Prioritise cross-script pairs over same-script
5. Isorthographic Retention: Keep same-string different-language pairs

Usage:
    python -m phonetics.extraction.generate_pairs \
        --data-dir /ix1/whcdh/models/phonetic/data/v3 \
        --namespaces gn wd tgn \
        --script-pair-quota 100000
"""

import argparse
import json
import logging
import random
import sqlite3
import sys
from collections import defaultdict, Counter
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

# Default quota per script-pair combination (from .tex Section 4.1)
DEFAULT_SCRIPT_PAIR_QUOTA = 100_000

# Minimum phonetic similarity for positive pairs (from .tex Section 4.3)
MIN_PHONETIC_SIMILARITY = 0.35

# Cross-script pairs are weighted higher than same-script (from .tex Section 4.1.5)
CROSS_SCRIPT_WEIGHT = 3.0  # 3x more likely to be selected when quota limited

# Known scripts for stratification
SCRIPTS = [
    'LATIN', 'CYRILLIC', 'ARABIC', 'CJK', 'GREEK', 'HEBREW',
    'DEVANAGARI', 'BENGALI', 'TAMIL', 'TELUGU', 'MALAYALAM',
    'KANNADA', 'GUJARATI', 'THAI', 'GEORGIAN', 'ARMENIAN',
    'HANGUL', 'HIRAGANA', 'KATAKANA', 'OTHER'
]


# =============================================================================
# SCRIPT-PAIR QUOTA MANAGER
# =============================================================================

class ScriptPairQuotaManager:
    """
    Manages quota-based sampling for script pairs.

    Implements criterion 1 (Script-Stratified Sampling) from the paper:
    - Maintains a quota for each script-pair combination
    - Prioritises cross-script pairs (criterion 5)
    - Tracks statistics for reporting
    """

    def __init__(self, quota_per_pair: int = DEFAULT_SCRIPT_PAIR_QUOTA):
        self.quota = quota_per_pair
        self.counts: Dict[Tuple[str, str], int] = defaultdict(int)
        self.rejected: Dict[Tuple[str, str], int] = defaultdict(int)

    def _normalize_pair(self, script1: str, script2: str) -> Tuple[str, str]:
        """Normalize script pair to canonical order."""
        return tuple(sorted([script1, script2]))

    def can_accept(self, script1: str, script2: str) -> bool:
        """Check if we can accept another pair for this script combination."""
        key = self._normalize_pair(script1, script2)
        return self.counts[key] < self.quota

    def accept(self, script1: str, script2: str) -> bool:
        """
        Try to accept a pair. Returns True if accepted, False if quota full.
        """
        key = self._normalize_pair(script1, script2)
        if self.counts[key] < self.quota:
            self.counts[key] += 1
            return True
        else:
            self.rejected[key] += 1
            return False

    def should_sample(self, script1: str, script2: str) -> bool:
        """
        Determine if a pair should be sampled based on quota fullness.

        When quotas are filling up, we use weighted random sampling
        to prioritise cross-script pairs.
        """
        key = self._normalize_pair(script1, script2)
        current = self.counts[key]

        # Always accept if under 50% of quota
        if current < self.quota * 0.5:
            return True

        # Use weighted sampling when approaching quota
        is_cross_script = script1 != script2
        weight = CROSS_SCRIPT_WEIGHT if is_cross_script else 1.0

        # Probability decreases as quota fills
        fill_ratio = current / self.quota
        prob = weight * (1.0 - fill_ratio)

        return random.random() < prob

    def get_statistics(self) -> Dict:
        """Return statistics about pair distribution."""
        stats = {
            'total_pairs': sum(self.counts.values()),
            'cross_script_pairs': sum(
                count for (s1, s2), count in self.counts.items() if s1 != s2
            ),
            'same_script_pairs': sum(
                count for (s1, s2), count in self.counts.items() if s1 == s2
            ),
            'pairs_by_script_combination': dict(self.counts),
            'rejected_over_quota': dict(self.rejected),
            'quota_per_pair': self.quota,
        }
        return stats


# =============================================================================
# PARQUET SCHEMAS
# =============================================================================

# Parquet schemas
PAIRS_SCHEMA = pa.schema([
    ('anchor_id', pa.string()),
    ('positive_id', pa.string()),
    ('namespace', pa.string()),
    ('place_id', pa.string()),
    ('is_cross_script', pa.bool_()),
    ('script_pair', pa.string()),  # e.g., "LATIN-CYRILLIC" for analysis
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
    """Load toponym index from SQLite database."""
    logger.info("Loading toponym index from SQLite...")

    db_path = data_dir / 'toponyms.db'
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Query the toponyms table (name_romanized is the normalized form)
    query = """
    SELECT toponym_id, name, name_romanized, script, lang
    FROM toponyms
    """

    cursor.execute(query)

    index = {}
    for row in cursor:
        tid, name, name_romanized, script, lang = row
        # Use name_romanized as normalized form, fall back to computing it
        name_norm = name_romanized if name_romanized else normalize_for_comparison(name) if name else ''
        index[tid] = {
            'name': name or '',
            'name_normalized': name_norm,
            'script': script or 'OTHER',
            'lang': lang,
        }

    conn.close()
    logger.info(f"Loaded {len(index):,} toponyms")
    return index


def scan_places_for_pairs(
        conn: sqlite3.Connection,
        namespaces: List[str],
        min_toponyms: int = 2,
) -> Iterator[Tuple[str, str, List[Dict]]]:
    """
    Scan places and yield those with multiple toponyms.

    Uses toponym_attestations to group toponyms by place_id,
    filtering to specified namespaces.
    """
    cursor = conn.cursor()

    # Build namespace filter - place_id is like 'gn:12345' so we use LIKE
    namespace_conditions = ' OR '.join([f"ta.place_id LIKE '{ns}:%'" for ns in namespaces])

    # Join toponyms with attestations to get all toponyms per place
    # Order by place_id to enable grouping
    query = f"""
    SELECT 
        ta.place_id,
        t.toponym_id,
        t.name,
        t.script,
        t.lang
    FROM toponym_attestations ta
    JOIN toponyms t ON ta.toponym_id = t.toponym_id
    WHERE {namespace_conditions}
    ORDER BY ta.place_id
    """

    cursor.execute(query)

    current_place = None
    current_namespace = None
    toponyms = []

    for row in cursor:
        place_id, toponym_id, name, script, lang = row

        # Extract namespace from place_id (e.g., 'gn:12345' -> 'gn')
        namespace = place_id.split(':')[0] if ':' in place_id else 'unknown'

        if current_place != place_id:
            # Yield previous place if it has enough toponyms
            if current_place is not None and len(toponyms) >= min_toponyms:
                yield current_place, current_namespace, toponyms

            current_place = place_id
            current_namespace = namespace
            toponyms = []

        toponyms.append({
            'toponym_id': toponym_id,
            'name': name,
            'script': script,
            'lang': lang,
        })

    # Don't forget the last place
    if current_place is not None and len(toponyms) >= min_toponyms:
        yield current_place, current_namespace, toponyms

    cursor.close()


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


def generate_pairs_from_place(
        toponyms: List[Dict],
        toponym_index: Dict[str, Dict],
        quota_manager: Optional[ScriptPairQuotaManager] = None,
        max_pairs: int = 100,
) -> List[Dict]:
    """
    Generate candidate pairs from co-located toponyms within a single place.

    Implements:
    - Phonetic similarity filtering (>= 0.35)
    - Cross-script pair identification
    - Quota-aware sampling when quota_manager provided
    """
    pairs = []
    valid_tops = []

    for top in toponyms:
        toponym_id = top.get('toponym_id', '')
        if not toponym_id: continue

        # Use toponym_id directly if it's in the index
        if toponym_id in toponym_index:
            script = toponym_index[toponym_id].get('script', 'OTHER')
            valid_tops.append((toponym_id, script))

    # Safety Cap - limit toponyms per place to avoid combinatorial explosion
    MAX_TOPONYMS_PER_PLACE = 50
    if len(valid_tops) > MAX_TOPONYMS_PER_PLACE:
        valid_tops = random.sample(valid_tops, MAX_TOPONYMS_PER_PLACE)

    # Generate all candidate pairs
    candidate_pairs = []
    for i, (id1, script1) in enumerate(valid_tops):
        for id2, script2 in valid_tops[i + 1:]:
            name1 = toponym_index[id1].get('name', '')
            name2 = toponym_index[id2].get('name', '')

            # Phonetic similarity filter
            sim = phonetic_similarity(name1, name2)
            if sim < MIN_PHONETIC_SIMILARITY:
                continue

            is_cross_script = script1 != script2
            script_pair = '-'.join(sorted([script1, script2]))

            candidate_pairs.append({
                'anchor_id': id1,
                'positive_id': id2,
                'is_cross_script': is_cross_script,
                'script1': script1,
                'script2': script2,
                'script_pair': script_pair,
                'similarity': sim,
            })

    # Sort by cross-script (prioritise) then by similarity (higher first)
    candidate_pairs.sort(key=lambda p: (-int(p['is_cross_script']), -p['similarity']))

    # Apply quota-based filtering if manager provided
    for pair in candidate_pairs:
        if quota_manager is not None:
            # Check if this script-pair can accept more
            if not quota_manager.should_sample(pair['script1'], pair['script2']):
                continue
            if not quota_manager.accept(pair['script1'], pair['script2']):
                continue

        pairs.append({
            'anchor_id': pair['anchor_id'],
            'positive_id': pair['positive_id'],
            'is_cross_script': pair['is_cross_script'],
            'script_pair': pair['script_pair'],
        })

        if len(pairs) >= max_pairs:
            break

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
        conn: sqlite3.Connection,
        data_dir: Path,
        namespaces: List[str],
        output_dir: Path,
        script_pair_quota: int = DEFAULT_SCRIPT_PAIR_QUOTA,
        batch_size: int = 50000,
        limit: Optional[int] = None,
) -> Tuple[int, Dict]:
    """
    Generate positive pairs from co-located toponyms.

    Implements the Data Selection and Curation Criteria:
    1. Script-Stratified Sampling with quotas
    2. Namespace filtering (gn, wd, tgn)
    3. Phonetic similarity filtering (>= 0.35)
    4. Cross-script weighting/prioritisation
    5. Deduplication - each (anchor, positive) pair appears only once

    Returns:
        Tuple of (total_pairs, statistics_dict)
    """
    logger.info("Generating positive pairs with script-stratified sampling...")
    logger.info(f"  Script-pair quota: {script_pair_quota:,}")
    logger.info(f"  Namespaces: {namespaces}")

    toponym_index = load_toponym_index(data_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize quota manager for script-stratified sampling
    quota_manager = ScriptPairQuotaManager(quota_per_pair=script_pair_quota)

    # Track seen pairs to eliminate duplicates
    # Key is normalized (min_id, max_id) to handle bidirectional pairs
    seen_pairs: Set[Tuple[str, str]] = set()
    duplicates_skipped = 0

    pairs_buffer = []
    total_pairs = 0
    part_num = 0

    # Count total places for progress bar
    cursor = conn.cursor()
    namespace_conditions = ' OR '.join([f"place_id LIKE '{ns}:%'" for ns in namespaces])
    cursor.execute(f"SELECT COUNT(DISTINCT place_id) FROM toponym_attestations WHERE {namespace_conditions}")
    total_places = cursor.fetchone()[0]
    cursor.close()

    if limit:
        total_places = min(total_places, limit)
    logger.info(f"Scanning {total_places:,} places...")

    iterator = scan_places_for_pairs(conn, namespaces)
    if tqdm:
        iterator = tqdm(iterator, total=total_places, desc="Scanning places")

    places_processed = 0
    places_with_pairs = 0

    for place_id, namespace, toponyms in iterator:
        pairs = generate_pairs_from_place(
            toponyms,
            toponym_index,
            quota_manager=quota_manager,
        )

        if pairs:
            new_pairs = []
            for pair in pairs:
                # Normalize pair key for deduplication (order-independent)
                pair_key = tuple(sorted([pair['anchor_id'], pair['positive_id']]))

                if pair_key in seen_pairs:
                    duplicates_skipped += 1
                    continue

                seen_pairs.add(pair_key)
                pair['namespace'] = namespace
                pair['place_id'] = place_id
                new_pairs.append(pair)

            if new_pairs:
                places_with_pairs += 1
                pairs_buffer.extend(new_pairs)
                total_pairs += len(new_pairs)

        places_processed += 1

        if len(pairs_buffer) >= batch_size:
            _write_batch(output_dir, pairs_buffer, part_num, PAIRS_SCHEMA)
            part_num += 1
            pairs_buffer = []

        if limit and places_processed >= limit:
            break

    if pairs_buffer:
        _write_batch(output_dir, pairs_buffer, part_num, PAIRS_SCHEMA)

    # Get statistics from quota manager
    stats = quota_manager.get_statistics()
    stats['places_scanned'] = places_processed
    stats['places_with_pairs'] = places_with_pairs
    stats['duplicates_skipped'] = duplicates_skipped
    stats['unique_pairs'] = len(seen_pairs)

    # Log summary
    logger.info(f"Pair generation complete:")
    logger.info(f"  Places scanned: {places_processed:,}")
    logger.info(f"  Places with pairs: {places_with_pairs:,}")
    logger.info(f"  Unique pairs: {stats['unique_pairs']:,}")
    logger.info(f"  Duplicates skipped: {duplicates_skipped:,}")
    logger.info(f"  Cross-script pairs: {stats['cross_script_pairs']:,} ({100*stats['cross_script_pairs']/max(1,stats['total_pairs']):.1f}%)")
    logger.info(f"  Same-script pairs: {stats['same_script_pairs']:,}")

    # Save statistics to JSON
    stats_path = output_dir / 'pair_generation_stats.json'
    with open(stats_path, 'w') as f:
        json.dump(stats, f, indent=2, default=str)
    logger.info(f"  Statistics saved to: {stats_path}")

    return total_pairs, stats


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
    parser = argparse.ArgumentParser(
        description='Generate pairs and triplets with script-stratified sampling',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard generation with default quotas
  python -m phonetics.extraction.generate_pairs --data-dir /path/to/data

  # Custom quota (200K pairs per script-pair)
  python -m phonetics.extraction.generate_pairs --data-dir /path/to/data --script-pair-quota 200000

  # Skip triplet generation (pairs only)
  python -m phonetics.extraction.generate_pairs --data-dir /path/to/data --skip-triplets
        """
    )
    parser.add_argument('--data-dir', type=Path, required=True,
                        help='Directory containing toponyms.db and where output will be written')
    parser.add_argument('--namespaces', nargs='+', default=['gn', 'wd', 'tgn'],
                        help='Namespaces to use for pair generation (default: gn wd tgn)')
    parser.add_argument('--script-pair-quota', type=int, default=DEFAULT_SCRIPT_PAIR_QUOTA,
                        help=f'Maximum pairs per script-pair combination (default: {DEFAULT_SCRIPT_PAIR_QUOTA:,})')
    parser.add_argument('--batch-size', type=int, default=50000,
                        help='Batch size for writing Parquet files')
    parser.add_argument('--limit', type=int, default=None,
                        help='Limit number of places to process (for testing)')
    parser.add_argument('--skip-pairs', action='store_true',
                        help='Skip pair generation')
    parser.add_argument('--skip-triplets', action='store_true',
                        help='Skip triplet generation')
    parser.add_argument('--skip-curated', action='store_true',
                        help='Skip curated test pair generation')
    args = parser.parse_args()

    # Generate pairs
    if not args.skip_pairs:
        db_path = args.data_dir / 'toponyms.db'
        if not db_path.exists():
            logger.error(f"Database not found: {db_path}")
            sys.exit(1)

        with sqlite3.connect(str(db_path)) as conn:
            total_pairs, stats = generate_pairs(
                conn, args.data_dir, args.namespaces,
                args.data_dir / 'pairs',
                script_pair_quota=args.script_pair_quota,
                batch_size=args.batch_size,
                limit=args.limit
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
    if not args.skip_curated:
        generate_curated_test_pairs(args.data_dir, args.data_dir / 'test_pairs')

    logger.info("Done!")


if __name__ == '__main__':
    main()