# phonetics/extraction/generate_pairs.py
"""
Generate positive pairs and training triplets from SQLite database.

Architecture: SQLite-driven streaming with parallel similarity computation.
- No Python dict for toponyms (uses SQLite directly)
- Multiprocessing for phonetic similarity (CPU-bound bottleneck)
- Deduplication via SQLite unique constraint
- Pair candidates generated via SQL JOIN
- Quota management in main thread (thread-safe)

This script runs AFTER rebuild_toponyms_index.py and implements the
Data Selection and Curation Criteria from the paper.
"""

import argparse
import json
import logging
import multiprocessing as mp
import random
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("Error: pyarrow package required")
    sys.exit(1)

try:
    from rapidfuzz.distance import Levenshtein
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

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

DEFAULT_SCRIPT_PAIR_QUOTA = 100_000
ISORTHOGRAPH_QUOTA_FRACTION = 0.2
MIN_PHONETIC_SIMILARITY = 0.35
BATCH_SIZE = 10000  # Rows per batch insert
SIMILARITY_BATCH_SIZE = 5000  # Rows per parallel similarity batch
DEFAULT_NUM_WORKERS = max(1, mp.cpu_count() - 2)  # Leave 2 cores for system


# =============================================================================
# PHONETIC SIMILARITY (Lightweight, no global state)
# =============================================================================

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
    Uses RapidFuzz + length pre-filter.
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

    # Length pre-filter
    length_diff = abs(len1 - len2)
    if max_len > 0 and length_diff / max_len > (1 - MIN_PHONETIC_SIMILARITY):
        return 0.0

    # Substring check
    if min_len >= 3 and (norm1 in norm2 or norm2 in norm1):
        return 0.85

    # Levenshtein distance
    if RAPIDFUZZ_AVAILABLE:
        distance = Levenshtein.distance(norm1, norm2)
    else:
        # Fallback Python implementation
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


def compute_similarities_batch(rows: List[Tuple]) -> List[Tuple]:
    """
    Compute phonetic similarities for a batch of candidate pairs.

    This function runs in worker processes. It receives raw rows and returns
    rows augmented with similarity scores and derived fields.

    Input rows: (place_id, namespace, a, b, name_a, name_b, script_a, script_b, lang_a, lang_b)
    Output: (place_id, namespace, a, b, script_a, script_b, is_cross_script, sim) for passing pairs
    """
    results = []

    for row in rows:
        place_id, namespace, a, b, name_a, name_b, script_a, script_b, lang_a, lang_b = row

        # Normalize scripts
        script_a = script_a or 'OTHER'
        script_b = script_b or 'OTHER'
        is_cross_script = (script_a != script_b)

        # Guard: identical names in same script → similarity = 1.0 (skip Levenshtein)
        if script_a == script_b and name_a == name_b:
            sim = 1.0
        else:
            sim = phonetic_similarity(name_a, name_b)

        # Different thresholds for same-script vs cross-script
        if is_cross_script:
            min_sim = MIN_PHONETIC_SIMILARITY  # 0.35
        else:
            min_sim = 0.6  # Higher bar for same-script

        if sim >= min_sim:
            results.append((place_id, namespace, a, b, script_a, script_b, is_cross_script, sim))

    return results


# =============================================================================
# QUOTA TRACKING (In-memory, small footprint)
# =============================================================================

class ScriptPairQuotaTracker:
    """
    Lightweight quota tracker. Only tracks counts, not pairs.
    """
    def __init__(self, quota: int):
        self.quota = quota
        self.isorthograph_quota = int(quota * ISORTHOGRAPH_QUOTA_FRACTION)
        self.counts: Dict[str, int] = {}
        self.isorthograph_counts: Dict[str, int] = {}

    def _key(self, s1: str, s2: str) -> str:
        return '-'.join(sorted([s1, s2]))

    def can_accept(self, s1: str, s2: str, is_isorthograph: bool) -> bool:
        key = self._key(s1, s2)
        count = self.counts.get(key, 0)
        if count >= self.quota:
            return False
        if is_isorthograph:
            iso_count = self.isorthograph_counts.get(key, 0)
            if iso_count >= self.isorthograph_quota:
                return False
        return True

    def accept(self, s1: str, s2: str, is_isorthograph: bool):
        key = self._key(s1, s2)
        self.counts[key] = self.counts.get(key, 0) + 1
        if is_isorthograph:
            self.isorthograph_counts[key] = self.isorthograph_counts.get(key, 0) + 1

    def get_stats(self) -> Dict:
        total = sum(self.counts.values())
        cross = sum(v for k, v in self.counts.items() if k.count('-') == 1 and k.split('-')[0] != k.split('-')[1])
        return {
            'total_pairs': total,
            'cross_script_pairs': cross,
            'same_script_pairs': total - cross,
            'isorthograph_pairs': sum(self.isorthograph_counts.values()),
            'by_script_pair': dict(self.counts),
            'quota_per_pair': self.quota,
        }


# =============================================================================
# SQLITE PAIR CANDIDATE TABLE
# =============================================================================

def create_staging_db(db_path: Path) -> sqlite3.Connection:
    """Create staging database for pair candidates with deduplication."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-2000000")  # 2GB cache
    conn.execute("PRAGMA temp_store=MEMORY")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pair_candidates (
            a TEXT NOT NULL,
            b TEXT NOT NULL,
            place_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            script_pair TEXT NOT NULL,
            is_cross_script INTEGER NOT NULL,
            similarity REAL NOT NULL,
            PRIMARY KEY (a, b)
        )
    """)
    conn.commit()
    return conn


# =============================================================================
# STREAMING PAIR GENERATOR (SQL-driven)
# =============================================================================

def stream_candidate_pairs(
    source_conn: sqlite3.Connection,
    namespaces: List[str],
    limit: Optional[int] = None,
) -> Iterator[Tuple[str, str, str, str, str, str, str, str, str, str]]:
    """
    Stream candidate pairs directly from SQLite via JOIN.

    Uses window function to cap toponyms per place (MAX 50) to prevent
    combinatorial explosion on places like "London".

    Yields: (place_id, namespace, toponym_a, toponym_b, name_a, name_b, script_a, script_b, lang_a, lang_b)

    This avoids loading any toponyms into Python memory.
    """
    namespace_filter = ' OR '.join([f"ta.place_id GLOB '{ns}:*'" for ns in namespaces])

    # SQL with window function to cap at 50 toponyms per place
    # This prevents combinatorial explosion (50 toponyms = 1225 pairs max)
    # Same-script cross-language pairs are sampled at 20% in SQL to reduce bridge traffic
    query = f"""
        WITH ranked AS (
            SELECT
                ta.place_id,
                ta.toponym_id,
                ROW_NUMBER() OVER (
                    PARTITION BY ta.place_id
                    ORDER BY ta.toponym_id
                ) AS rn
            FROM toponym_attestations ta
            WHERE ({namespace_filter})
        )
        SELECT 
            r1.place_id,
            substr(r1.place_id, 1, instr(r1.place_id, ':') - 1) as namespace,
            t1.toponym_id as a,
            t2.toponym_id as b,
            t1.name as name_a,
            t2.name as name_b,
            t1.script as script_a,
            t2.script as script_b,
            t1.lang as lang_a,
            t2.lang as lang_b
        FROM ranked r1
        JOIN ranked r2
            ON r1.place_id = r2.place_id
            AND r1.toponym_id < r2.toponym_id
        JOIN toponyms t1 ON r1.toponym_id = t1.toponym_id
        JOIN toponyms t2 ON r2.toponym_id = t2.toponym_id
        WHERE r1.rn <= 50
          AND r2.rn <= 50
          AND (
              -- Keep all cross-script pairs
              t1.script != t2.script
              -- For same-script: keep if same language OR 20% sample of cross-language
              OR t1.lang = t2.lang
              OR (ABS(RANDOM()) % 100) < 20
          )
    """

    if limit:
        query += f" LIMIT {limit}"

    cursor = source_conn.cursor()
    cursor.execute(query)

    for row in cursor:
        yield row

    cursor.close()


def estimate_candidate_pairs(
    source_conn: sqlite3.Connection,
    namespaces: List[str],
) -> int:
    """
    Estimate total candidate pairs using per-place counts.

    Much faster than exact COUNT(*) on the full JOIN.
    Accounts for the 50-toponym cap per place.
    """
    namespace_filter = ' OR '.join([f"place_id GLOB '{ns}:*'" for ns in namespaces])

    # Count toponyms per place, cap at 50, compute pairs
    query = f"""
        SELECT SUM(capped * (capped - 1) / 2)
        FROM (
            SELECT MIN(cnt, 50) as capped
            FROM (
                SELECT COUNT(*) AS cnt
                FROM toponym_attestations
                WHERE ({namespace_filter})
                GROUP BY place_id
            )
        )
    """

    cursor = source_conn.cursor()
    cursor.execute(query)
    result = cursor.fetchone()[0]
    cursor.close()
    return result or 0


# =============================================================================
# MAIN PAIR GENERATION
# =============================================================================

def generate_pairs(
    source_db: Path,
    output_dir: Path,
    namespaces: List[str],
    script_pair_quota: int = DEFAULT_SCRIPT_PAIR_QUOTA,
    limit: Optional[int] = None,
    scratch_dir: Optional[Path] = None,
    num_workers: int = DEFAULT_NUM_WORKERS,
) -> Tuple[int, Dict]:
    """
    Generate positive pairs using SQLite-driven streaming with parallel similarity.

    Architecture:
    1. Stream candidate pairs via SQL JOIN (no Python dict)
    2. Batch rows and compute phonetic similarity in parallel (ProcessPoolExecutor)
    3. Apply quota-based filtering in main thread (thread-safe)
    4. Deduplicate via INSERT OR IGNORE into staging DB
    5. Export to Parquet

    IMPORTANT: For optimal performance, ensure toponyms.db has index:
        CREATE INDEX idx_attestations_place_toponym
        ON toponym_attestations(place_id, toponym_id);
    This prevents massive disk sorts in the ROW_NUMBER() window function.
    """
    logger.info("=" * 60)
    logger.info("PAIR GENERATION (SQLite-driven streaming + parallel similarity)")
    logger.info("=" * 60)
    logger.info(f"Source DB: {source_db}")
    logger.info(f"Namespaces: {namespaces}")
    logger.info(f"Quota per script-pair: {script_pair_quota:,}")
    logger.info(f"Isorthograph sub-quota: {int(script_pair_quota * ISORTHOGRAPH_QUOTA_FRACTION):,}")
    logger.info(f"RapidFuzz available: {RAPIDFUZZ_AVAILABLE}")
    logger.info(f"Parallel workers: {num_workers}")

    # Open source database (read-only)
    source_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    source_conn.execute("PRAGMA mmap_size=2147483648")

    # Create staging database for deduplication
    if scratch_dir:
        staging_path = scratch_dir / "pairs_staging.db"
    else:
        staging_path = output_dir / "pairs_staging.db"

    staging_path.parent.mkdir(parents=True, exist_ok=True)
    if staging_path.exists():
        staging_path.unlink()

    staging_conn = create_staging_db(staging_path)

    # Quota tracker (lightweight, in-memory)
    quota = ScriptPairQuotaTracker(script_pair_quota)

    # Statistics
    stats = {
        'candidates_scanned': 0,
        'passed_similarity': 0,
        'passed_quota': 0,
        'duplicates_skipped': 0,
    }

    # Estimate candidates for progress bar (fast, not exact)
    logger.info("Estimating candidate pairs...")
    if limit:
        total_candidates = limit
    else:
        total_candidates = estimate_candidate_pairs(source_conn, namespaces)
    logger.info(f"Estimated candidate pairs: {total_candidates:,}")

    # Stream and process with parallel similarity computation
    logger.info("Streaming candidate pairs with parallel similarity...")

    db_batch = []  # Batch for DB inserts
    sim_batch = []  # Batch for parallel similarity computation
    iterator = stream_candidate_pairs(source_conn, namespaces, limit)

    if tqdm:
        pbar = tqdm(total=total_candidates, desc="Processing pairs")
    else:
        pbar = None

    def process_similarity_results(results: List[Tuple]):
        """Process results from parallel similarity computation (runs in main thread)."""
        nonlocal db_batch

        for result in results:
            place_id, namespace, a, b, script_a, script_b, is_cross_script, sim = result
            stats['passed_similarity'] += 1

            # Script pair and flags
            script_pair = '-'.join(sorted([script_a, script_b]))
            is_isorthograph = (sim > 0.99)

            # Quota check (must be in main thread - not thread-safe)
            if not quota.can_accept(script_a, script_b, is_isorthograph):
                continue

            # Accept into quota
            quota.accept(script_a, script_b, is_isorthograph)
            stats['passed_quota'] += 1

            # Normalize pair orientation
            a_id, b_id = (a, b) if a < b else (b, a)

            # Add to DB batch
            db_batch.append((a_id, b_id, place_id, namespace, script_pair, int(is_cross_script), sim))

        # Write to staging DB if batch is full
        if len(db_batch) >= BATCH_SIZE:
            cursor = staging_conn.cursor()
            cursor.executemany(
                "INSERT OR IGNORE INTO pair_candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
                db_batch
            )
            stats['duplicates_skipped'] += len(db_batch) - cursor.rowcount
            staging_conn.commit()
            db_batch = []

    if num_workers > 1:
        # Parallel processing mode
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = []

            for row in iterator:
                sim_batch.append(row)
                stats['candidates_scanned'] += 1

                if pbar:
                    pbar.update(1)

                # Submit batch for parallel processing
                if len(sim_batch) >= SIMILARITY_BATCH_SIZE:
                    futures.append(executor.submit(compute_similarities_batch, sim_batch.copy()))
                    sim_batch = []

                # Collect results periodically to avoid memory buildup
                if len(futures) >= num_workers * 2:
                    for future in as_completed(futures):
                        process_similarity_results(future.result())
                    futures = []

            # Submit remaining batch
            if sim_batch:
                futures.append(executor.submit(compute_similarities_batch, sim_batch))

            # Collect remaining results
            for future in as_completed(futures):
                process_similarity_results(future.result())
    else:
        # Single-threaded mode (for debugging or small datasets)
        for row in iterator:
            sim_batch.append(row)
            stats['candidates_scanned'] += 1

            if pbar:
                pbar.update(1)

            if len(sim_batch) >= SIMILARITY_BATCH_SIZE:
                results = compute_similarities_batch(sim_batch)
                process_similarity_results(results)
                sim_batch = []

        # Process remaining
        if sim_batch:
            results = compute_similarities_batch(sim_batch)
            process_similarity_results(results)

    if pbar:
        pbar.close()

    # Final DB batch
    if db_batch:
        cursor = staging_conn.cursor()
        cursor.executemany(
            "INSERT OR IGNORE INTO pair_candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
            db_batch
        )
        stats['duplicates_skipped'] += len(db_batch) - cursor.rowcount
        staging_conn.commit()

    # Get final count
    cursor = staging_conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM pair_candidates")
    final_count = cursor.fetchone()[0]

    logger.info(f"Pair generation complete:")
    logger.info(f"  Candidates scanned: {stats['candidates_scanned']:,}")
    logger.info(f"  Passed similarity: {stats['passed_similarity']:,}")
    logger.info(f"  Passed quota: {stats['passed_quota']:,}")
    logger.info(f"  Duplicates skipped: {stats['duplicates_skipped']:,}")
    logger.info(f"  Final unique pairs: {final_count:,}")
    logger.info(f"  Workers used: {num_workers}")
    logger.info(f"  (Same-script cross-lang pairs sampled at 20% in SQL)")

    # Export to Parquet
    logger.info("Exporting to Parquet...")
    output_dir.mkdir(parents=True, exist_ok=True)

    export_to_parquet(staging_conn, output_dir)

    # Merge quota stats
    stats.update(quota.get_stats())
    stats['unique_pairs'] = final_count

    # Save stats to PARENT directory (not in pairs/ to avoid PyArrow confusion)
    stats_path = output_dir.parent / 'pair_generation_stats.json'
    with open(stats_path, 'w') as f:
        json.dump(stats, f, indent=2)
    logger.info(f"Statistics saved to: {stats_path}")

    # Cleanup
    source_conn.close()
    staging_conn.close()

    if scratch_dir and staging_path.exists():
        staging_path.unlink()

    return final_count, stats


def export_to_parquet(conn: sqlite3.Connection, output_dir: Path):
    """Export pair_candidates table to Parquet files."""
    schema = pa.schema([
        ('anchor_id', pa.string()),
        ('positive_id', pa.string()),
        ('namespace', pa.string()),
        ('place_id', pa.string()),
        ('is_cross_script', pa.bool_()),
        ('script_pair', pa.string()),
    ])

    cursor = conn.cursor()
    cursor.execute("SELECT a, b, namespace, place_id, is_cross_script, script_pair FROM pair_candidates")

    batch = []
    part_num = 0

    for row in cursor:
        a, b, namespace, place_id, is_cross_script, script_pair = row
        batch.append({
            'anchor_id': a,
            'positive_id': b,
            'namespace': namespace,
            'place_id': place_id,
            'is_cross_script': bool(is_cross_script),
            'script_pair': script_pair,
        })

        if len(batch) >= 100000:
            table = pa.Table.from_pylist(batch, schema=schema)
            pq.write_table(table, output_dir / f"part-{part_num:04d}.parquet", compression='snappy')
            part_num += 1
            batch = []

    if batch:
        table = pa.Table.from_pylist(batch, schema=schema)
        pq.write_table(table, output_dir / f"part-{part_num:04d}.parquet", compression='snappy')

    logger.info(f"Exported {part_num + 1} Parquet files")


# =============================================================================
# TRIPLET GENERATION (Phase 1 + Phase 3)
# =============================================================================

def generate_triplets(
    data_dir: Path,
    output_dir: Path,
    phase: str = 'phase1',
    negatives_per_pair: int = 1,
    limit: Optional[int] = None,
    scratch_dir: Optional[Path] = None,
) -> int:
    """
    Generate triplets from pairs.

    Phase 1: Random negatives from the full toponym pool
    Phase 3: Hard negatives - script-matched, orthographically similar
             (same prefix but different place)

    Optimizations:
    - In-memory set for adjacency (fast lookups, ~2GB for 10M pairs)
    - rowid-based random sampling (no need to load all IDs)
    - Offset-based random selection for Phase 3 (avoid ORDER BY RANDOM)
    """
    logger.info(f"Generating triplets for {phase}...")

    pairs_dir = data_dir / 'pairs'
    if not pairs_dir.exists():
        logger.error(f"Pairs directory not found: {pairs_dir}")
        return 0

    db_path = data_dir / 'toponyms.db'
    if not db_path.exists():
        logger.error(f"Database not found: {db_path}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)

    # Connect to toponyms database
    topo_conn = sqlite3.connect(str(db_path))
    topo_conn.execute("PRAGMA mmap_size=2147483648")

    # Get max rowid for random sampling (avoids loading all IDs into memory)
    cursor = topo_conn.cursor()
    cursor.execute("SELECT MAX(rowid) FROM toponyms")
    max_rowid = cursor.fetchone()[0]
    logger.info(f"Max toponym rowid: {max_rowid:,}")

    def get_random_toponym_id() -> Optional[str]:
        """Get random toponym using rowid-based sampling (O(1) memory)."""
        for _ in range(10):  # Up to 10 attempts for gaps in rowids
            rand_rowid = random.randint(1, max_rowid)
            cursor.execute("SELECT toponym_id FROM toponyms WHERE rowid = ?", (rand_rowid,))
            result = cursor.fetchone()
            if result:
                return result[0]
        return None

    # Build adjacency set from pairs (in-memory for fast lookups)
    # This is ~2GB for 10M pairs - acceptable since we're not loading toponyms
    logger.info("Building adjacency set from pairs...")
    import pyarrow.dataset as ds
    dataset = ds.dataset(pairs_dir, format='parquet')

    adjacency: set = set()
    for batch in dataset.to_batches(columns=['anchor_id', 'positive_id']):
        for a, p in zip(batch['anchor_id'], batch['positive_id']):
            a_str, p_str = a.as_py(), p.as_py()
            adjacency.add((a_str, p_str))
            adjacency.add((p_str, a_str))

    logger.info(f"Built adjacency set with {len(adjacency):,} edges")

    def is_adjacent(anchor: str, candidate: str) -> bool:
        return (anchor, candidate) in adjacency

    # For Phase 3, build prefix index in scratch DB
    if phase == 'phase3':
        logger.info("Building prefix index for hard negative mining...")

        if scratch_dir:
            scratch_db_path = scratch_dir / f"triplet_scratch_{phase}.db"
        else:
            scratch_db_path = output_dir / f"triplet_scratch_{phase}.db"

        if scratch_db_path.exists():
            scratch_db_path.unlink()

        scratch_conn = sqlite3.connect(str(scratch_db_path))
        scratch_conn.execute("PRAGMA journal_mode=WAL")
        scratch_conn.execute("PRAGMA synchronous=OFF")
        scratch_conn.execute("PRAGMA cache_size=-500000")

        # Create prefix index with rowid for offset-based random selection
        scratch_conn.execute("""
            CREATE TABLE prefix_index (
                id INTEGER PRIMARY KEY,
                prefix TEXT NOT NULL,
                toponym_id TEXT NOT NULL,
                script TEXT NOT NULL
            )
        """)

        # Populate from toponyms table
        topo_cursor = topo_conn.cursor()
        topo_cursor.execute("SELECT toponym_id, name_romanized, script FROM toponyms WHERE name_romanized IS NOT NULL")

        prefix_batch = []
        prefix_count = 0
        for row in topo_cursor:
            tid, name_norm, script = row
            if name_norm and len(name_norm) >= 2:
                prefix = name_norm[:2].lower()
                prefix_batch.append((prefix, tid, script or 'OTHER'))

                if len(prefix_batch) >= 50000:
                    scratch_conn.executemany("INSERT INTO prefix_index (prefix, toponym_id, script) VALUES (?, ?, ?)", prefix_batch)
                    scratch_conn.commit()
                    prefix_count += len(prefix_batch)
                    prefix_batch = []

        if prefix_batch:
            scratch_conn.executemany("INSERT INTO prefix_index (prefix, toponym_id, script) VALUES (?, ?, ?)", prefix_batch)
            scratch_conn.commit()
            prefix_count += len(prefix_batch)

        # Create composite index for fast prefix+script lookups
        scratch_conn.execute("CREATE INDEX idx_prefix_script ON prefix_index(prefix, script)")
        scratch_conn.commit()
        logger.info(f"Built prefix index with {prefix_count:,} entries")

        # Cache prefix counts for offset-based random selection
        scratch_cursor = scratch_conn.cursor()
        scratch_cursor.execute("""
            SELECT prefix, script, COUNT(*), MIN(id), MAX(id) 
            FROM prefix_index 
            GROUP BY prefix, script
        """)
        prefix_ranges: Dict[Tuple[str, str], Tuple[int, int, int]] = {}
        for row in scratch_cursor:
            prefix, script, count, min_id, max_id = row
            prefix_ranges[(prefix, script)] = (count, min_id, max_id)
        logger.info(f"Cached {len(prefix_ranges):,} prefix/script ranges")

        # Load anchor info for prefix lookup
        topo_cursor.execute("SELECT toponym_id, name_romanized, script FROM toponyms WHERE name_romanized IS NOT NULL")
        anchor_info: Dict[str, Tuple[str, str]] = {}
        for row in topo_cursor:
            tid, name_norm, script = row
            if name_norm and len(name_norm) >= 2:
                anchor_info[tid] = (name_norm[:2].lower(), script or 'OTHER')
        logger.info(f"Loaded anchor info for {len(anchor_info):,} toponyms")

        def get_hard_negatives(anchor: str, num: int = 5) -> List[str]:
            """Get unique hard negatives: same prefix, same script, not adjacent."""
            if anchor not in anchor_info:
                return []

            prefix, script = anchor_info[anchor]
            key = (prefix, script)

            if key not in prefix_ranges:
                return []

            count, min_id, max_id = prefix_ranges[key]
            negatives = set()  # Use a set to ensure uniqueness

            # Increase max_attempts to account for high-density prefixes
            attempts = 0
            max_attempts = num * 10

            while len(negatives) < num and attempts < max_attempts:
                attempts += 1

                # Pick a random spot in the index
                rand_id = random.randint(min_id, max_id)

                # Seek to the nearest valid record for this prefix/script
                scratch_cursor.execute("""
                                       SELECT toponym_id
                                       FROM prefix_index
                                       WHERE id >= ?
                                         AND prefix = ?
                                         AND script = ? LIMIT 1
                                       """, (rand_id, prefix, script))

                result = scratch_cursor.fetchone()

                # If we hit the end of the ID range, wrap around to min_id
                if not result:
                    scratch_cursor.execute("""
                                           SELECT toponym_id
                                           FROM prefix_index
                                           WHERE id >= ?
                                             AND prefix = ?
                                             AND script = ? LIMIT 1
                                           """, (min_id, prefix, script))
                    result = scratch_cursor.fetchone()

                if result:
                    cand = result[0]
                    # Ensure it is not the anchor, not a positive pair, and not already picked
                    if cand != anchor and cand not in negatives and not is_adjacent(anchor, cand):
                        negatives.add(cand)

            return list(negatives)

    # Generate triplets
    schema = pa.schema([
        ('anchor_id', pa.string()),
        ('positive_id', pa.string()),
        ('negative_id', pa.string()),
        ('negative_type', pa.string()),
    ])

    buffer = []
    part_num = 0
    total_triplets = 0

    logger.info(f"Generating {phase} triplets...")

    for batch in dataset.to_batches(columns=['anchor_id', 'positive_id']):
        for i in range(len(batch)):
            anchor = batch['anchor_id'][i].as_py()
            positive = batch['positive_id'][i].as_py()

            if phase == 'phase1':
                # Phase 1: Random negatives using rowid-based sampling
                for _ in range(negatives_per_pair):
                    neg = get_random_toponym_id()
                    attempts = 0
                    while neg and (neg == anchor or is_adjacent(anchor, neg)) and attempts < 10:
                        neg = get_random_toponym_id()
                        attempts += 1

                    if neg and attempts < 10:
                        buffer.append({
                            'anchor_id': anchor,
                            'positive_id': positive,
                            'negative_id': neg,
                            'negative_type': 'random',
                        })
                        total_triplets += 1

            elif phase == 'phase3':
                # Phase 3: Hard negatives (script-matched, orthographically similar)
                hard_negs = get_hard_negatives(anchor, negatives_per_pair)

                for neg in hard_negs:
                    buffer.append({
                        'anchor_id': anchor,
                        'positive_id': positive,
                        'negative_id': neg,
                        'negative_type': 'hard_ortho',
                    })
                    total_triplets += 1

                # If not enough hard negatives, fall back to random
                if len(hard_negs) < negatives_per_pair:
                    needed = negatives_per_pair - len(hard_negs)
                    for _ in range(needed):
                        neg = get_random_toponym_id()
                        attempts = 0
                        while neg and (neg == anchor or is_adjacent(anchor, neg)) and attempts < 10:
                            neg = get_random_toponym_id()
                            attempts += 1

                        if neg and attempts < 10:
                            buffer.append({
                                'anchor_id': anchor,
                                'positive_id': positive,
                                'negative_id': neg,
                                'negative_type': 'random_fallback',
                            })
                            total_triplets += 1

            if len(buffer) >= 100000:
                table = pa.Table.from_pylist(buffer, schema=schema)
                pq.write_table(table, output_dir / f"part-{part_num:04d}.parquet", compression='snappy')
                part_num += 1
                buffer = []

            if limit and total_triplets >= limit:
                break

        if limit and total_triplets >= limit:
            break

    if buffer:
        table = pa.Table.from_pylist(buffer, schema=schema)
        pq.write_table(table, output_dir / f"part-{part_num:04d}.parquet", compression='snappy')

    # Cleanup
    topo_conn.close()
    if phase == 'phase3':
        scratch_conn.close()
        if scratch_db_path.exists():
            scratch_db_path.unlink()

    logger.info(f"Generated {total_triplets:,} triplets for {phase}")
    return total_triplets


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Generate pairs and triplets (SQLite-driven streaming)',
    )
    parser.add_argument('--data-dir', type=Path, required=True,
                        help='Directory containing toponyms.db')
    parser.add_argument('--namespaces', nargs='+', default=['gn', 'wd', 'tgn'],
                        help='Namespaces to filter')
    parser.add_argument('--script-pair-quota', type=int, default=DEFAULT_SCRIPT_PAIR_QUOTA,
                        help=f'Max pairs per script-pair (default: {DEFAULT_SCRIPT_PAIR_QUOTA:,})')
    parser.add_argument('--limit', type=int, default=None,
                        help='Limit candidates (for testing)')
    parser.add_argument('--scratch-dir', type=Path, default=None,
                        help='Fast scratch directory for staging DB')
    parser.add_argument('--num-workers', type=int, default=DEFAULT_NUM_WORKERS,
                        help=f'Number of parallel workers for similarity (default: {DEFAULT_NUM_WORKERS})')
    parser.add_argument('--skip-pairs', action='store_true',
                        help='Skip pair generation')
    parser.add_argument('--skip-triplets', action='store_true',
                        help='Skip triplet generation')
    args = parser.parse_args()

    db_path = args.data_dir / 'toponyms.db'
    if not db_path.exists():
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    if not args.skip_pairs:
        generate_pairs(
            source_db=db_path,
            output_dir=args.data_dir / 'pairs',
            namespaces=args.namespaces,
            script_pair_quota=args.script_pair_quota,
            limit=args.limit,
            scratch_dir=args.scratch_dir,
            num_workers=args.num_workers,
        )

    if not args.skip_triplets:
        # Generate Phase 1 triplets (random negatives)
        generate_triplets(
            args.data_dir,
            args.data_dir / 'triplets' / 'phase1',
            phase='phase1',
            limit=args.limit,
            scratch_dir=args.scratch_dir,
        )

        # Generate Phase 3 triplets (hard negatives)
        generate_triplets(
            args.data_dir,
            args.data_dir / 'triplets' / 'phase3',
            phase='phase3',
            limit=args.limit,
            scratch_dir=args.scratch_dir,
        )

    logger.info("Done!")


if __name__ == '__main__':
    main()

