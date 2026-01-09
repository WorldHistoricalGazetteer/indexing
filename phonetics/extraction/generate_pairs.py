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
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing as mp

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pyarrow.dataset as ds
except ImportError:
    print("Error: pyarrow package required")
    sys.exit(1)

try:
    from rapidfuzz.distance import Levenshtein
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    print("Warning: rapidfuzz not available, using slow Python implementation")

try:
    from anyascii import anyascii
except ImportError:
    anyascii = None

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# Try to import optional dependencies for memory-efficient deduplication
try:
    from pybloom_live import ScalableBloomFilter
    BLOOM_AVAILABLE = True
except ImportError:
    BLOOM_AVAILABLE = False

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

# Sub-quota for exact orthographic matches (isorthographs like Paris@en / Paris@fr)
# This prevents common names from consuming the entire same-script quota
ISORTHOGRAPH_QUOTA_FRACTION = 0.2  # Max 20% of quota for exact matches

# Minimum phonetic similarity for positive pairs (from .tex Section 4.3)
MIN_PHONETIC_SIMILARITY = 0.35

# Cross-script pairs are weighted higher than same-script (from .tex Section 4.1.5)
CROSS_SCRIPT_WEIGHT = 3.0  # 3x more likely to be selected when quota limited

# Parallel processing configuration
DEFAULT_NUM_WORKERS = max(1, mp.cpu_count() - 2)  # Leave 2 cores for system
WORKER_CHUNK_SIZE = 1000  # Places per worker batch

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
    - Tracks isorthograph sub-quota to prevent common names dominating
    - Tracks statistics for reporting
    """

    def __init__(self, quota_per_pair: int = DEFAULT_SCRIPT_PAIR_QUOTA):
        self.quota = quota_per_pair
        self.isorthograph_quota = int(quota_per_pair * ISORTHOGRAPH_QUOTA_FRACTION)
        self.counts: Dict[Tuple[str, str], int] = defaultdict(int)
        self.isorthograph_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        self.rejected: Dict[Tuple[str, str], int] = defaultdict(int)

    def _normalize_pair(self, script1: str, script2: str) -> Tuple[str, str]:
        """Normalize script pair to canonical order."""
        return tuple(sorted([script1, script2]))

    def can_accept(self, script1: str, script2: str) -> bool:
        """Check if we can accept another pair for this script combination."""
        key = self._normalize_pair(script1, script2)
        return self.counts[key] < self.quota

    def accept(self, script1: str, script2: str, is_isorthograph: bool = False) -> bool:
        """
        Try to accept a pair. Returns True if accepted, False if quota full.

        Args:
            script1, script2: The scripts of the pair
            is_isorthograph: If True, pair is an exact orthographic match (similarity=1.0)
        """
        key = self._normalize_pair(script1, script2)

        # Check isorthograph sub-quota first
        if is_isorthograph:
            if self.isorthograph_counts[key] >= self.isorthograph_quota:
                self.rejected[key] += 1
                return False
            self.isorthograph_counts[key] += 1

        # Check main quota
        if self.counts[key] < self.quota:
            self.counts[key] += 1
            return True
        else:
            self.rejected[key] += 1
            return False

    def should_sample(self, script1: str, script2: str, similarity: float = 0.5) -> bool:
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

        # Boost weight for non-isorthographs (more interesting pairs)
        if similarity < 0.99:
            weight *= 1.5

        # Probability decreases as quota fills
        fill_ratio = current / self.quota
        prob = weight * (1.0 - fill_ratio)

        return random.random() < prob

    def get_statistics(self) -> Dict:
        """Return statistics about pair distribution."""
        # Convert tuple keys to strings for JSON serialization
        pairs_by_combo = {f"{s1}-{s2}": count for (s1, s2), count in self.counts.items()}
        rejected_by_combo = {f"{s1}-{s2}": count for (s1, s2), count in self.rejected.items()}
        isorthographs_by_combo = {f"{s1}-{s2}": count for (s1, s2), count in self.isorthograph_counts.items()}

        stats = {
            'total_pairs': sum(self.counts.values()),
            'cross_script_pairs': sum(
                count for (s1, s2), count in self.counts.items() if s1 != s2
            ),
            'same_script_pairs': sum(
                count for (s1, s2), count in self.counts.items() if s1 == s2
            ),
            'isorthograph_pairs': sum(self.isorthograph_counts.values()),
            'pairs_by_script_combination': pairs_by_combo,
            'isorthographs_by_script_combination': isorthographs_by_combo,
            'rejected_over_quota': rejected_by_combo,
            'quota_per_pair': self.quota,
            'isorthograph_quota_per_pair': self.isorthograph_quota,
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


def ensure_indexes(conn: sqlite3.Connection):
    """Ensure required indexes exist for fast queries."""
    logger.info("Checking database indexes...")
    cursor = conn.cursor()

    # Critical index for ORDER BY place_id in scan_places_for_pairs
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_attestation_place 
        ON toponym_attestations(place_id)
    """)

    # Index for toponym lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_toponyms_id 
        ON toponyms(toponym_id)
    """)

    conn.commit()
    logger.info("Database indexes verified.")


def load_toponym_index_lightweight(data_dir: Path) -> Dict[str, Tuple[str, str, str]]:
    """
    Load toponym index from SQLite database in a memory-efficient format.

    Instead of storing full dicts, store tuples: (name, script, lang)
    This reduces memory overhead by ~60% compared to dicts.

    Returns:
        Dict mapping toponym_id -> (name, script, lang)
    """
    logger.info("Loading toponym index (lightweight)...")

    db_path = data_dir / 'toponyms.db'
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA mmap_size=2147483648")  # 2GB mmap for faster reads
    cursor = conn.cursor()

    # Only load what we need: id, name, script, lang
    # Skip name_romanized - we'll normalize on demand
    query = "SELECT toponym_id, name, script, lang FROM toponyms"
    cursor.execute(query)

    # Use tuples instead of dicts for ~60% memory reduction
    index: Dict[str, Tuple[str, str, str]] = {}
    batch_size = 100000
    rows_loaded = 0

    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break

        for tid, name, script, lang in rows:
            # Store as tuple: (name, script, lang)
            index[tid] = (name or '', script or 'OTHER', lang or '')

        rows_loaded += len(rows)
        if rows_loaded % 5000000 == 0:
            logger.info(f"  Loaded {rows_loaded:,} toponyms...")

    conn.close()
    logger.info(f"Loaded {len(index):,} toponyms")
    return index


def load_toponym_index(data_dir: Path) -> Dict[str, Dict]:
    """
    Load toponym index from SQLite database.

    For backward compatibility, returns dict format.
    Consider using load_toponym_index_lightweight for lower memory usage.
    """
    logger.info("Loading toponym index from SQLite...")

    db_path = data_dir / 'toponyms.db'
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA mmap_size=2147483648")  # 2GB mmap for faster reads
    cursor = conn.cursor()

    # Query the toponyms table (name_romanized is the normalized form)
    query = """
    SELECT toponym_id, name, name_romanized, script, lang
    FROM toponyms
    """

    cursor.execute(query)

    index = {}
    batch_size = 100000
    rows_loaded = 0

    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break

        for row in rows:
            tid, name, name_romanized, script, lang = row
            # Use name_romanized as normalized form, fall back to computing it
            name_norm = name_romanized if name_romanized else normalize_for_comparison(name) if name else ''
            index[tid] = {
                'name': name or '',
                'name_normalized': name_norm,
                'script': script or 'OTHER',
                'lang': lang,
            }

        rows_loaded += len(rows)
        if rows_loaded % 5000000 == 0:
            logger.info(f"  Loaded {rows_loaded:,} toponyms...")

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

    # Enable memory-mapped I/O for faster reads
    cursor.execute("PRAGMA mmap_size=2147483648")  # 2GB mmap

    # Build namespace filter - place_id is like 'gn:12345' so we use GLOB (faster than LIKE)
    # GLOB uses index if available, LIKE with leading wildcard doesn't
    namespace_conditions = ' OR '.join([f"ta.place_id GLOB '{ns}:*'" for ns in namespaces])

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
    """Normalize name for phonetic comparison."""
    if not name:
        return ""
    if anyascii:
        normalized = anyascii(name).lower()
    else:
        normalized = name.lower()
    return ''.join(c for c in normalized if c.isalnum())


def phonetic_similarity(name1: str, name2: str) -> float:
    """
    Calculate phonetic similarity between two names.

    Uses RapidFuzz for fast Levenshtein distance calculation.
    Pre-filters by length to skip expensive distance calculations
    when strings cannot possibly meet the similarity threshold.
    """
    norm1 = normalize_for_comparison(name1)
    norm2 = normalize_for_comparison(name2)

    if not norm1 or not norm2:
        return 0.0
    if norm1 == norm2:
        return 1.0

    len1, len2 = len(norm1), len(norm2)
    max_len = max(len1, len2)
    min_len = min(len1, len2)

    # Length pre-filter: if length difference alone exceeds threshold, skip
    # If similarity must be >= 0.35, then distance/max_len <= 0.65
    # Maximum possible distance from length alone is |len1 - len2|
    length_diff = len1 - len2 if len1 > len2 else len2 - len1
    if length_diff / max_len > (1 - MIN_PHONETIC_SIMILARITY):
        return 0.0

    # Substring check for potential high similarity
    if min_len >= 3 and (norm1 in norm2 or norm2 in norm1):
        return 0.85

    # Use RapidFuzz if available (20-100x faster than Python implementation)
    if RAPIDFUZZ_AVAILABLE:
        distance = Levenshtein.distance(norm1, norm2)
    else:
        # Fallback to Python implementation
        if len1 < len2:
            norm1, norm2 = norm2, norm1
            len1, len2 = len2, len1

        previous_row = list(range(len2 + 1))
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


def _get_toponym_data(toponym_index: Dict, toponym_id: str) -> Tuple[str, str, str]:
    """
    Extract (name, script, lang) from toponym_index entry.

    Handles both lightweight tuple format and dict format for compatibility.
    """
    data = toponym_index.get(toponym_id)
    if data is None:
        return ('', 'OTHER', '')

    if isinstance(data, tuple):
        # Lightweight format: (name, script, lang)
        return data
    else:
        # Dict format
        return (
            data.get('name', ''),
            data.get('script', 'OTHER'),
            data.get('lang', '')
        )


def generate_pairs_from_place_simple(
        toponyms: List[Dict],
        toponym_index: Dict,
) -> List[Dict]:
    """
    Generate candidate pairs from co-located toponyms within a single place.

    This is a "pure" function for parallel processing - it does NOT apply
    quota filtering (which must happen in the main thread).

    Supports both lightweight (tuple) and dict toponym_index formats.

    Returns all candidate pairs that pass phonetic similarity filtering.
    Each pair includes similarity score for quota decisions later.
    """
    valid_tops = []

    for top in toponyms:
        toponym_id = top.get('toponym_id', '')
        if not toponym_id:
            continue

        # Use toponym_id directly if it's in the index
        if toponym_id in toponym_index:
            name, script, lang = _get_toponym_data(toponym_index, toponym_id)
            valid_tops.append((toponym_id, script, name))

    # Safety Cap - limit toponyms per place to avoid combinatorial explosion
    MAX_TOPONYMS_PER_PLACE = 50
    if len(valid_tops) > MAX_TOPONYMS_PER_PLACE:
        valid_tops = random.sample(valid_tops, MAX_TOPONYMS_PER_PLACE)

    # Generate all candidate pairs
    candidate_pairs = []
    for i, (id1, script1, name1) in enumerate(valid_tops):
        for id2, script2, name2 in valid_tops[i + 1:]:
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

    # Sort by cross-script (prioritise) then by similarity (descending)
    # But put non-isorthographs before isorthographs within same-script
    candidate_pairs.sort(key=lambda p: (
        -int(p['is_cross_script']),  # Cross-script first
        int(p['similarity'] > 0.99),  # Non-isorthographs before isorthographs
        -p['similarity']  # Higher similarity first
    ))

    return candidate_pairs


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

    For parallel processing, use generate_pairs_from_place_simple instead.
    """
    candidate_pairs = generate_pairs_from_place_simple(toponyms, toponym_index)

    if not quota_manager:
        # No quota filtering, return all (up to max)
        return [{
            'anchor_id': p['anchor_id'],
            'positive_id': p['positive_id'],
            'is_cross_script': p['is_cross_script'],
            'script_pair': p['script_pair'],
            'similarity': p['similarity'],
        } for p in candidate_pairs[:max_pairs]]

    # Apply quota-based filtering
    pairs = []
    for pair in candidate_pairs:
        is_isorthograph = pair['similarity'] > 0.99

        # Check if this script-pair can accept more
        if not quota_manager.should_sample(pair['script1'], pair['script2'], pair['similarity']):
            continue
        if not quota_manager.accept(pair['script1'], pair['script2'], is_isorthograph):
            continue

        pairs.append({
            'anchor_id': pair['anchor_id'],
            'positive_id': pair['positive_id'],
            'is_cross_script': pair['is_cross_script'],
            'script_pair': pair['script_pair'],
            'similarity': pair['similarity'],
        })

        if len(pairs) >= max_pairs:
            break

    return pairs


# Worker function for parallel processing
def _process_place_batch(args: Tuple) -> List[Tuple[str, str, List[Dict]]]:
    """
    Process a batch of places in a worker process.

    Args:
        args: Tuple of (places_batch, toponym_index)
              where places_batch is List[(place_id, namespace, toponyms)]

    Returns:
        List of (place_id, namespace, candidate_pairs) tuples
    """
    places_batch, toponym_index = args
    results = []

    for place_id, namespace, toponyms in places_batch:
        try:
            # Generate candidate pairs (no quota filtering - done in main thread)
            candidates = generate_pairs_from_place_simple(toponyms, toponym_index)
            if candidates:
                results.append((place_id, namespace, candidates))
        except Exception as e:
            # Log but don't crash the worker
            pass

    return results


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
        num_workers: int = DEFAULT_NUM_WORKERS,
) -> Tuple[int, Dict]:
    """
    Generate positive pairs from co-located toponyms.

    Uses parallel processing for phonetic similarity calculations,
    with quota management and deduplication in the main thread.

    Implements the Data Selection and Curation Criteria:
    1. Script-Stratified Sampling with quotas
    2. Namespace filtering (gn, wd, tgn)
    3. Phonetic similarity filtering (>= 0.35)
    4. Cross-script weighting/prioritisation
    5. Deduplication - each (anchor, positive) pair appears only once
    6. Isorthograph sub-quota to prevent common names dominating

    Returns:
        Tuple of (total_pairs, statistics_dict)
    """
    logger.info("Generating positive pairs with script-stratified sampling...")
    logger.info(f"  Script-pair quota: {script_pair_quota:,}")
    logger.info(f"  Isorthograph sub-quota: {int(script_pair_quota * ISORTHOGRAPH_QUOTA_FRACTION):,}")
    logger.info(f"  Namespaces: {namespaces}")
    logger.info(f"  Workers: {num_workers}")
    logger.info(f"  RapidFuzz available: {RAPIDFUZZ_AVAILABLE}")

    # Ensure indexes exist for fast queries
    ensure_indexes(conn)

    # Use lightweight index (tuples) for 112M records to save ~40% RAM
    toponym_index = load_toponym_index_lightweight(data_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize quota manager for script-stratified sampling
    quota_manager = ScriptPairQuotaManager(quota_per_pair=script_pair_quota)

    # Track seen pairs to eliminate duplicates
    if BLOOM_AVAILABLE and len(toponym_index) > 10_000_000:
        logger.info("Using Bloom filter for memory-efficient deduplication")
        seen_pairs = ScalableBloomFilter(mode=ScalableBloomFilter.LARGE_SET_GROWTH)
        use_bloom = True
    else:
        seen_pairs: Set[str] = set()
        use_bloom = False

    duplicates_skipped = 0
    pairs_buffer = []
    total_pairs = 0
    part_num = 0

    # Count total places for progress bar
    cursor = conn.cursor()
    namespace_conditions = ' OR '.join([f"place_id GLOB '{ns}:*'" for ns in namespaces])
    cursor.execute(f"SELECT COUNT(DISTINCT place_id) FROM toponym_attestations WHERE {namespace_conditions}")
    total_places = cursor.fetchone()[0]
    cursor.close()

    if limit:
        total_places = min(total_places, limit)
    logger.info(f"Scanning {total_places:,} places...")

    # Collect places into batches for parallel processing
    places_processed = 0
    places_with_pairs = 0

    def process_candidates(place_id: str, namespace: str, candidates: List[Dict]):
        """Process candidates from a place - apply quota and deduplication in main thread."""
        nonlocal duplicates_skipped, total_pairs, places_with_pairs, part_num

        new_pairs = []
        for pair in candidates:
            # Check quota (must be in main thread - not thread-safe)
            is_isorthograph = pair['similarity'] > 0.99
            if not quota_manager.should_sample(pair['script1'], pair['script2'], pair['similarity']):
                continue
            if not quota_manager.accept(pair['script1'], pair['script2'], is_isorthograph):
                continue

            # Deduplication (must be in main thread)
            aid, pid = pair['anchor_id'], pair['positive_id']
            if aid > pid:
                aid, pid = pid, aid
            pair_key = f"{aid}|{pid}"

            if pair_key in seen_pairs:
                duplicates_skipped += 1
                continue
            seen_pairs.add(pair_key)

            new_pairs.append({
                'anchor_id': pair['anchor_id'],
                'positive_id': pair['positive_id'],
                'is_cross_script': pair['is_cross_script'],
                'script_pair': pair['script_pair'],
                'namespace': namespace,
                'place_id': place_id,
            })

        if new_pairs:
            places_with_pairs += 1
            pairs_buffer.extend(new_pairs)
            total_pairs += len(new_pairs)

        # Write batch if buffer is full
        if len(pairs_buffer) >= batch_size:
            _write_batch(output_dir, pairs_buffer, part_num, PAIRS_SCHEMA)
            part_num += 1
            pairs_buffer.clear()

    if num_workers > 1:
        # Parallel processing mode
        logger.info(f"Using {num_workers} worker processes for parallel pair generation")

        # Collect places into chunks
        place_buffer = []
        iterator = scan_places_for_pairs(conn, namespaces)

        if tqdm:
            pbar = tqdm(total=total_places, desc="Scanning places")
        else:
            pbar = None

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = []

            for place_id, namespace, toponyms in iterator:
                place_buffer.append((place_id, namespace, toponyms))
                places_processed += 1

                # Submit batch when we have enough
                if len(place_buffer) >= WORKER_CHUNK_SIZE:
                    futures.append(executor.submit(
                        _process_place_batch,
                        (place_buffer.copy(), toponym_index)
                    ))
                    place_buffer.clear()

                # Collect results periodically to avoid memory buildup
                if len(futures) >= num_workers * 2:
                    for future in as_completed(futures):
                        try:
                            results = future.result()
                            for pid, ns, candidates in results:
                                process_candidates(pid, ns, candidates)
                        except Exception as e:
                            logger.warning(f"Worker error: {e}")
                    futures.clear()

                    if pbar:
                        pbar.update(places_processed - pbar.n)

                # Progress logging
                if places_processed % 100000 == 0:
                    logger.info(f"  Progress: {places_processed:,} places, {total_pairs:,} pairs")

                if limit and places_processed >= limit:
                    break

            # Submit any remaining places
            if place_buffer:
                futures.append(executor.submit(
                    _process_place_batch,
                    (place_buffer, toponym_index)
                ))

            # Collect remaining results
            for future in as_completed(futures):
                try:
                    results = future.result()
                    for pid, ns, candidates in results:
                        process_candidates(pid, ns, candidates)
                except Exception as e:
                    logger.warning(f"Worker error: {e}")

        if pbar:
            pbar.close()

    else:
        # Single-threaded mode
        iterator = scan_places_for_pairs(conn, namespaces)
        if tqdm:
            iterator = tqdm(iterator, total=total_places, desc="Scanning places")

        for place_id, namespace, toponyms in iterator:
            try:
                candidates = generate_pairs_from_place_simple(toponyms, toponym_index)
                if candidates:
                    process_candidates(place_id, namespace, candidates)

                places_processed += 1

                if places_processed % 100000 == 0:
                    logger.info(f"  Progress: {places_processed:,} places, {total_pairs:,} pairs, {duplicates_skipped:,} duplicates")

                if limit and places_processed >= limit:
                    break

            except Exception as e:
                logger.warning(f"Error processing place {place_id}: {e}")
                continue

    # Write any remaining pairs
    if pairs_buffer:
        _write_batch(output_dir, pairs_buffer, part_num, PAIRS_SCHEMA)

    # Get statistics from quota manager
    stats = quota_manager.get_statistics()
    stats['places_scanned'] = places_processed
    stats['places_with_pairs'] = places_with_pairs
    stats['duplicates_skipped'] = duplicates_skipped
    stats['unique_pairs'] = total_pairs
    stats['dedup_method'] = 'bloom_filter' if use_bloom else 'set'
    stats['num_workers'] = num_workers

    # Log summary
    logger.info(f"Pair generation complete:")
    logger.info(f"  Places scanned: {places_processed:,}")
    logger.info(f"  Places with pairs: {places_with_pairs:,}")
    logger.info(f"  Unique pairs: {total_pairs:,}")
    logger.info(f"  Duplicates skipped: {duplicates_skipped:,}")
    logger.info(f"  Isorthograph pairs: {stats.get('isorthograph_pairs', 0):,}")
    logger.info(f"  Cross-script pairs: {stats['cross_script_pairs']:,} ({100*stats['cross_script_pairs']/max(1,stats['total_pairs']):.1f}%)")
    logger.info(f"  Same-script pairs: {stats['same_script_pairs']:,}")

    # Save statistics to JSON
    stats_path = output_dir / 'pair_generation_stats.json'
    try:
        with open(stats_path, 'w') as f:
            json.dump(stats, f, indent=2, default=str)
        logger.info(f"  Statistics saved to: {stats_path}")
    except Exception as e:
        logger.error(f"Failed to save statistics: {e}")
        logger.info(f"  Stats: {stats}")

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

    pairs_dir = data_dir / 'pairs'
    if not pairs_dir.exists():
        logger.error(f"Pairs directory not found: {pairs_dir}")
        return 0

    toponym_index = load_toponym_index(data_dir)
    all_ids = list(toponym_index.keys())

    if not all_ids:
        logger.error("No toponyms found in index")
        return 0

    prefix_index = {}
    lang_index = {}
    if phase == 'phase3':
        prefix_index = build_prefix_index(toponym_index)
        lang_index = build_lang_index(toponym_index)

    try:
        dataset = ds.dataset(pairs_dir, format='parquet')
    except Exception as e:
        logger.error(f"Failed to load pairs dataset: {e}")
        return 0

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

    try:
        toponym_index = load_toponym_index(data_dir)
    except Exception as e:
        logger.error(f"Failed to load toponym index: {e}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)

    # Find cross-script pairs
    cross_script_pairs = []

    # Group by name (case-insensitive)
    name_groups: Dict[str, List[str]] = defaultdict(list)
    for tid, tdata in toponym_index.items():
        name_key = tdata.get('name_normalized') or ''
        if name_key:  # Skip empty names
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
    parser.add_argument('--num-workers', type=int, default=DEFAULT_NUM_WORKERS,
                        help=f'Number of parallel workers (default: {DEFAULT_NUM_WORKERS})')
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
                limit=args.limit,
                num_workers=args.num_workers,
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