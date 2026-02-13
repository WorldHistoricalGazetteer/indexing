#!/usr/bin/env python3
"""
Training Data Generator - Main class for generating Symphonym training data.

OPTIMISATIONS over v5:
- _LocalManifest: Single ES scan materialised to Parquet, reused across phases.
  Eliminates duplicate full-index scans for positive pairs and negative candidates.
- _IntegerAdjacency: Integer-encoded edge set (~60% memory reduction vs string tuples).
  Replaces Set[Tuple[str, str]] adjacency used in Phase 1 and Phase 3.
- Manifest-derived negative sampling: Phase 1 builds ids_by_script from cached
  manifest instead of a second full ES scan.

PUBLIC API: Unchanged from v5.
"""

import gc
import json
import pickle
import random
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import parquet as writer
from elasticsearch.helpers import scan

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(x, **kwargs):
        return x

from phonetics.extraction.constants import (
    ES_PARALLEL_WORKERS, MAX_OVERSAMPLE_FACTOR, MAX_TOPONYMS_PER_PLACE,
    MIN_BIN_SIZE, MSEARCH_BATCH_SIZE, PARQUET_BATCH_SIZE,
    PHASE1_SAME_SCRIPT_NEGATIVE_RATIO, RANDOM_SEED, TARGET_SAMPLES_PER_BIN,
    apply_bin_balancing, get_script_lang_key, logger
)
from phonetics.extraction.es_knn_helper import ESKNNHelper
from phonetics.extraction.streaming_writer import TripletStreamingWriter, MultiSplitStreamingWriter


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _LocalManifest:
    """
    Single scan materialised to local Parquet, reused across phases.

    Contains: toponym_id, script, lang, attestations for all toponyms
    with IPA/PanPhon in training namespaces.

    Build sources (in priority order):
    1. Cached Parquet on disk (instant reload)
    2. DuckDB (seconds — local columnar scan)
    3. ES scan (minutes — fallback if no DuckDB available)

    Eliminates:
    - Full scan in generate_positive_pairs() (attestations + script + lang)
    - Full scan in generate_phase1_triplets_streaming() (negative candidate loading)
    """

    def __init__(self, es, index: str, namespaces: List[str], path: Path,
                 db_path: Optional[str] = None):
        self.es = es
        self.index = index
        self.namespaces = namespaces
        self.path = path
        self.db_path = db_path
        self._loaded = False

        # Lazily populated on ensure()
        self.ids: List[str] = []
        self.scripts: List[str] = []
        self.langs: List[str] = []
        self.attestations: List[List[str]] = []

        # Derived indexes, built on ensure()
        self.ids_by_script: Dict[str, List[str]] = {}
        self.all_ids: List[str] = []

    def ensure(self):
        """Load manifest from cache, or build from DuckDB/ES."""
        if self._loaded:
            return

        if self.path.exists():
            logger.info(f"Loading cached manifest from {self.path}")
        elif self.db_path and Path(self.db_path).exists():
            self._build_from_duckdb()
        else:
            if self.db_path:
                logger.info(f"DuckDB not found at {self.db_path}, falling back to ES scan")
            self._build_from_es()

        table = pq.read_table(self.path)
        self.ids = table['toponym_id'].to_pylist()
        self.scripts = table['script'].to_pylist()
        self.langs = table['lang'].to_pylist()
        self.attestations = table['attestations'].to_pylist()

        # Build derived indexes for negative sampling
        self.all_ids = self.ids
        ids_by_script: Dict[str, List[str]] = defaultdict(list)
        for tid, script in zip(self.ids, self.scripts):
            if script:
                ids_by_script[script].append(tid)
        self.ids_by_script = dict(ids_by_script)

        logger.info(
            f"Manifest loaded: {len(self.ids):,} toponyms, "
            f"{len(self.ids_by_script)} scripts"
        )
        self._loaded = True

    def _build_from_duckdb(self):
        """Build manifest from DuckDB (seconds, no network)."""
        import duckdb

        logger.info(f"Building manifest from DuckDB: {self.db_path}")

        conn = duckdb.connect(self.db_path, read_only=True)

        ns_sql = ','.join(f"'{ns}'" for ns in self.namespaces)

        # Check if toponym_attestations table exists and has data
        try:
            att_count = conn.execute("SELECT COUNT(*) FROM toponym_attestations").fetchone()[0]
            if att_count == 0:
                logger.warning("toponym_attestations table is empty, falling back to ES")
                conn.close()
                self._build_from_es()
                return
            logger.info(f"Found {att_count:,} attestation records in DuckDB")
        except Exception as e:
            logger.warning(f"Could not query toponym_attestations: {e}, falling back to ES")
            conn.close()
            self._build_from_es()
            return

        result = conn.execute(f'''
            SELECT t.toponym_id,
                   t.script,
                   t.lang,
                   LIST(DISTINCT ta.place_id) as attestations
            FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            LEFT JOIN toponym_attestations ta ON t.toponym_id = ta.toponym_id
            WHERE t.ipa IS NOT NULL
              AND tn.namespace IN ({ns_sql})
            GROUP BY t.toponym_id, t.script, t.lang
        ''').arrow()

        conn.close()

        if len(result) == 0:
            logger.warning("DuckDB query returned no results, falling back to ES")
            self._build_from_es()
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(result, self.path, compression='zstd', row_group_size=128_000)
        logger.info(f"Manifest written from DuckDB: {len(result):,} rows -> {self.path}")

    def _build_from_es(self):
        """Build manifest from ES scan (fallback)."""
        logger.info("Building local toponym manifest (ES scan fallback)...")

        ns_filter = [{"term": {"namespaces": ns}} for ns in self.namespaces]
        query = {
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "panphon_embedding"}}],
                    "should": ns_filter,
                    "minimum_should_match": 1,
                }
            },
            "_source": ["script", "lang", "attestations"],
        }

        rows = []
        empty_attestations_count = 0
        for hit in tqdm(
            scan(self.es, index=self.index, query=query, scroll='5m', size=5000),
            desc="Scanning ES for manifest"
        ):
            src = hit['_source']
            attestations = src.get('attestations', [])
            if not attestations:
                empty_attestations_count += 1
            rows.append({
                'toponym_id': hit['_id'],
                'script': src.get('script', 'UNKNOWN'),
                'lang': src.get('lang', ''),
                'attestations': attestations,
            })

        logger.info(f"Scanned {len(rows):,} toponyms from ES")
        logger.info(f"Toponyms with empty attestations: {empty_attestations_count:,} ({empty_attestations_count/len(rows)*100:.1f}%)")

        table = pa.Table.from_pylist(rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, self.path, compression='zstd', row_group_size=128_000)
        logger.info(f"Manifest written from ES: {len(rows):,} rows -> {self.path}")


class _IntegerAdjacency:
    """
    Memory-efficient adjacency set using integer-encoded edges.

    Replaces Set[Tuple[str, str]] which stores two ~30-byte Python string
    objects per edge. This stores a single 64-bit integer per edge.

    For 10M edges: ~300MB (string tuples) -> ~80MB (int set).

    The encoding is opaque — callers use add(a, b) and __contains__((a, b)).
    """

    __slots__ = ('_id_to_int', '_edges')

    def __init__(self):
        self._id_to_int: Dict[str, int] = {}
        self._edges: set = set()

    def _enc(self, tid: str) -> int:
        idx = self._id_to_int.get(tid)
        if idx is None:
            idx = len(self._id_to_int)
            self._id_to_int[tid] = idx
        return idx

    def add(self, a: str, b: str):
        ia = self._enc(a)
        ib = self._enc(b)
        if ia > ib:
            ia, ib = ib, ia
        self._edges.add((ia << 32) | ib)

    def __contains__(self, pair) -> bool:
        """Support `(a, b) in adjacency` syntax for drop-in compatibility."""
        a, b = pair
        ia = self._id_to_int.get(a)
        ib = self._id_to_int.get(b)
        if ia is None or ib is None:
            return False
        if ia > ib:
            ia, ib = ib, ia
        return ((ia << 32) | ib) in self._edges

    def __len__(self) -> int:
        return len(self._edges)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class TrainingDataGenerator:
    """Generates training data for all phases from ES."""

    def __init__(
            self,
            es,
            db_path: str,
            output_dir: Path,
            scratch_dir: Path,
            training_namespaces: List[str],
            force_regenerate: bool = False,
            skip_to_phase3: bool = False,
            resume_from_pass2: bool = False,
    ):
        self.es = es
        self.db_path = db_path
        self.output_dir = output_dir
        self.scratch_dir = scratch_dir
        self.training_namespaces = training_namespaces
        self.force_regenerate = force_regenerate
        self.skip_to_phase3 = skip_to_phase3
        self.resume_from_pass2 = resume_from_pass2

        self.knn = ESKNNHelper(es, index="toponyms")

        # DuckDB connection is optional
        self.conn = None

        # Local manifest — single scan reused across phases
        # Prefers DuckDB (seconds) over ES scan (minutes) when db_path is available
        self._manifest = _LocalManifest(
            es,
            index="toponyms",
            namespaces=training_namespaces,
            path=scratch_dir / "toponym_manifest.parquet",
            db_path=db_path,
        )

        self.stats = {
            'phase1': {'pairs': 0, 'triplets': 0, 'by_bin': {}},
            'phase2': {'samples': 0, 'by_bin': {}},
            'phase3': {'triplets': 0, 'by_bin': {}},
        }

    def _check_phase_complete(self, phase: str) -> bool:
        """Check if a phase's output files already exist and are readable."""
        if self.force_regenerate:
            return False

        try:
            if phase == 'pairs':
                pairs_file = self.output_dir / 'pairs' / 'positive_pairs.parquet'
                if not pairs_file.exists():
                    return False
                # Validate it's readable
                pq.read_schema(pairs_file)
                return True

            elif phase == 'phase1':
                train = self.output_dir / 'triplets' / 'phase1' / 'train.parquet'
                val = self.output_dir / 'triplets' / 'phase1' / 'val.parquet'
                if not (train.exists() and val.exists()):
                    return False
                # Validate both files are readable
                pq.read_schema(train)
                pq.read_schema(val)
                return True

            elif phase == 'phase2':
                train = self.output_dir / 'training' / 'split=train' / 'data.parquet'
                val = self.output_dir / 'training' / 'split=val' / 'data.parquet'
                if not (train.exists() and val.exists()):
                    return False
                # Validate both files are readable
                pq.read_schema(train)
                pq.read_schema(val)
                return True

            elif phase == 'phase3':
                train = self.output_dir / 'triplets' / 'phase3' / 'train.parquet'
                val = self.output_dir / 'triplets' / 'phase3' / 'val.parquet'
                if not (train.exists() and val.exists()):
                    return False
                # Validate both files are readable
                pq.read_schema(train)
                pq.read_schema(val)
                return True

        except Exception as e:
            logger.warning(f"Checkpoint validation failed for {phase}: {e}")
            logger.warning(f"Will regenerate {phase}")
            return False

        return False

    def _check_phase3_has_text_fields(self) -> bool:
        """Check if Phase 3 triplets have the text fields needed for Student training."""
        train_path = self.output_dir / 'triplets' / 'phase3' / 'train.parquet'
        if not train_path.exists():
            return False

        schema = pq.read_schema(train_path)
        required_fields = ['anchor_name', 'anchor_script', 'anchor_lang',
                           'positive_name', 'positive_script', 'positive_lang',
                           'negative_name', 'negative_script', 'negative_lang']

        existing_fields = set(schema.names)
        return all(f in existing_fields for f in required_fields)

    def _augment_phase3_with_text_fields(self):
        """
        Augment existing Phase 3 triplets with text fields for Student training.

        Uses chunked processing to avoid loading entire files into memory.
        Reads in batches, fetches metadata, writes to new file, then swaps.
        """
        logger.info("=" * 60)
        logger.info("AUGMENTING PHASE 3 WITH TEXT FIELDS")
        logger.info("=" * 60)

        phase3_dir = self.output_dir / 'triplets' / 'phase3'

        for split in ['train', 'val']:
            parquet_path = phase3_dir / f'{split}.parquet'
            if not parquet_path.exists():
                logger.warning(f"Phase 3 {split} file not found, skipping")
                continue

            logger.info(f"Processing {split}...")

            # First pass: collect all unique toponym IDs without loading full data
            logger.info("  Pass 1: Collecting unique toponym IDs...")
            all_ids = set()
            parquet_file = pq.ParquetFile(parquet_path)
            total_rows = parquet_file.metadata.num_rows

            for batch in parquet_file.iter_batches(batch_size=50000,
                                                   columns=['anchor_id', 'positive_id', 'negative_id']):
                df_batch = batch.to_pandas()
                all_ids.update(df_batch['anchor_id'].tolist())
                all_ids.update(df_batch['positive_id'].tolist())
                all_ids.update(df_batch['negative_id'].tolist())

            logger.info(f"  Found {len(all_ids):,} unique toponym IDs in {total_rows:,} triplets")

            # Second pass: batch fetch text metadata from ES
            logger.info("  Pass 2: Fetching text metadata from ES...")
            raw_metadata = self._batch_mget(
                list(all_ids),
                source_fields=['name', 'script', 'lang'],
                desc="Fetching text metadata",
            )
            text_metadata = {}
            for tid, source in raw_metadata.items():
                text_metadata[tid] = {
                    'name': source.get('name', ''),
                    'script': source.get('script', 'UNKNOWN'),
                    'lang': source.get('lang', ''),
                }

            logger.info(f"  Fetched metadata for {len(text_metadata):,} toponyms")
            del all_ids  # Free memory

            # Third pass: stream through data, add text fields, write to new file
            logger.info("  Pass 3: Writing augmented file...")

            temp_path = phase3_dir / f'{split}_augmented_temp.parquet'

            def get_text_field(toponym_id, field):
                meta = text_metadata.get(toponym_id, {})
                return meta.get(field, '' if field != 'script' else 'UNKNOWN')

            writer = None
            rows_written = 0

            for batch in parquet_file.iter_batches(batch_size=50000):
                df_batch = batch.to_pandas()

                # Add text columns
                df_batch['anchor_name'] = df_batch['anchor_id'].apply(lambda x: get_text_field(x, 'name'))
                df_batch['anchor_script'] = df_batch['anchor_id'].apply(lambda x: get_text_field(x, 'script'))
                df_batch['anchor_lang'] = df_batch['anchor_id'].apply(lambda x: get_text_field(x, 'lang'))
                df_batch['positive_name'] = df_batch['positive_id'].apply(lambda x: get_text_field(x, 'name'))
                df_batch['positive_script'] = df_batch['positive_id'].apply(lambda x: get_text_field(x, 'script'))
                df_batch['positive_lang'] = df_batch['positive_id'].apply(lambda x: get_text_field(x, 'lang'))
                df_batch['negative_name'] = df_batch['negative_id'].apply(lambda x: get_text_field(x, 'name'))
                df_batch['negative_script'] = df_batch['negative_id'].apply(lambda x: get_text_field(x, 'script'))
                df_batch['negative_lang'] = df_batch['negative_id'].apply(lambda x: get_text_field(x, 'lang'))

                # Convert to pyarrow table
                batch_table = pa.Table.from_pandas(df_batch, preserve_index=False)

                # Initialize writer with schema from first batch
                if writer is None:
                    writer = pq.ParquetWriter(temp_path, batch_table.schema)

                writer.write_table(batch_table)
                rows_written += len(df_batch)

                if rows_written % 200000 < 50000:
                    logger.info(f"    Written {rows_written:,} / {total_rows:,}...")

            if writer:
                writer.close()

            del text_metadata  # Free memory

            # Swap files
            backup_path = phase3_dir / f'{split}_backup_no_text.parquet'
            if not backup_path.exists():
                logger.info(f"  Backing up original to {backup_path.name}")
                parquet_path.rename(backup_path)
            else:
                # Backup already exists from previous run, just remove original
                parquet_path.unlink()

            temp_path.rename(parquet_path)
            logger.info(f"  ✓ {split} augmented: {rows_written:,} triplets")

        logger.info("Phase 3 augmentation complete")

    def _load_pairs_from_checkpoint(self) -> Dict[str, List[Tuple]]:
        """Load positive pairs from checkpoint Parquet file."""
        pairs_file = self.output_dir / 'pairs' / 'positive_pairs.parquet'
        logger.info(f"Loading pairs from checkpoint: {pairs_file}")

        table = pq.read_table(pairs_file)
        df = table.to_pandas()

        pairs_by_bin: Dict[str, List[Tuple]] = defaultdict(list)
        for _, row in df.iterrows():
            pairs_by_bin[row['bin']].append((row['anchor'], row['positive'], 1.0))

        logger.info(f"Loaded {len(df):,} pairs across {len(pairs_by_bin)} bins")
        return pairs_by_bin

    def _build_adjacency(self, pairs_by_bin: Dict[str, List[Tuple]]) -> _IntegerAdjacency:
        """Build integer-encoded adjacency from pairs. Reusable across phases."""
        adjacency = _IntegerAdjacency()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                adjacency.add(id_a, id_b)
        return adjacency

    def _batch_mget(
        self,
        ids: List[str],
        source_fields: List[str],
        batch_size: int = 5000,
        desc: str = "Fetching",
    ) -> Dict[str, dict]:
        """
        Batch mget with progress logging and failure tracking.

        Returns dict mapping toponym_id -> _source dict for found documents.
        Periodically checks ES failure threshold to abort early on flaky connections.
        """
        results = {}
        mget_failures = 0

        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            try:
                docs = self.es.mget(index="toponyms", body={"ids": batch},
                                    _source=source_fields)
                for doc in docs.get('docs', []):
                    if doc.get('found') and '_source' in doc:
                        results[doc['_id']] = doc['_source']
            except Exception as e:
                mget_failures += 1
                logger.warning(f"mget batch failed ({len(batch)} ids): {e}")

            if (i + batch_size) % 50000 < batch_size:
                logger.info(f"  {desc}: {min(i + batch_size, len(ids)):,} / {len(ids):,}...")
                if mget_failures > 0:
                    failure_pct = mget_failures / ((i // batch_size) + 1)
                    logger.warning(f"  mget failure rate: {failure_pct:.1%} ({mget_failures} batches)")
                    if failure_pct > 0.1:
                        raise RuntimeError(
                            f"mget failure rate ({failure_pct:.1%}) too high — "
                            f"aborting to prevent silent data loss"
                        )

        if mget_failures > 0:
            logger.warning(f"Total mget failures: {mget_failures} batches out of {(len(ids) + batch_size - 1) // batch_size}")

        return results

    def generate_all(self):
        """Generate training data for all phases."""
        logger.info("=" * 60)
        logger.info("GENERATING TRAINING DATA FOR ALL PHASES")
        logger.info("=" * 60)

        # If skip_to_phase3 is set, force skip Phase 1 and Phase 2
        if self.skip_to_phase3:
            logger.info("🔄 SKIP TO PHASE 3 mode enabled")
            logger.info("  Phase 1 & 2 will be skipped")

        # Check what needs to be done
        need_phase1 = not self._check_phase_complete('phase1') and not self.skip_to_phase3
        need_phase3_generate = not self._check_phase_complete('phase3')
        need_phase3_augment = self._check_phase_complete('phase3') and not self._check_phase3_has_text_fields()

        # Step 1: Load/generate positive pairs (only if needed for generation)
        logger.info("\n" + "=" * 60)
        logger.info("STEP 1: POSITIVE PAIRS")
        logger.info("=" * 60)

        pairs_by_bin = None
        if need_phase1 or need_phase3_generate:
            if self._check_phase_complete('pairs'):
                logger.info("✓ Positive pairs checkpoint found, loading...")
                pairs_by_bin = self._load_pairs_from_checkpoint()
            else:
                if self.skip_to_phase3:
                    logger.warning("⚠ Skip-to-phase3 set but no pairs checkpoint found!")
                    logger.info("  Attempting to load from pairs/positive_pairs.parquet...")
                    if (self.output_dir / 'pairs' / 'positive_pairs.parquet').exists():
                        pairs_by_bin = self._load_pairs_from_checkpoint()
                    else:
                        logger.error("  FATAL: No pairs found, cannot skip to Phase 3")
                        raise FileNotFoundError("pairs/positive_pairs.parquet required for --skip-to-phase3")
                else:
                    pairs_by_bin = self.generate_positive_pairs()
        else:
            logger.info("✓ Pairs not needed (Phase 1 & 3 complete), skipping...")

        # Step 2: Generate Phase 2 samples
        logger.info("\n" + "=" * 60)
        logger.info("STEP 2: GENERATE PHASE 2 SAMPLES")
        logger.info("=" * 60)

        if self._check_phase_complete('phase2') or self.skip_to_phase3:
            if self.skip_to_phase3:
                logger.info("🔄 Skipping Phase 2 (skip-to-phase3 mode)")
            else:
                logger.info("✓ Phase 2 checkpoint found, skipping...")
            training_dir = self.output_dir / 'training'
            total_samples = 0
            for split in ['train', 'val', 'test']:
                split_file = training_dir / f'split={split}' / 'data.parquet'
                if split_file.exists():
                    total_samples += pq.read_table(split_file).num_rows
            self.stats['phase2']['samples'] = total_samples
        else:
            self.generate_phase2_samples()

        # Clear cache between phases
        logger.info("Clearing embedding cache...")
        self.knn.clear_cache()
        self.knn.reset_failure_tracking()
        gc.collect()

        # Step 3: Generate Phase 1 triplets (MEMORY-EFFICIENT STREAMING)
        logger.info("\n" + "=" * 60)
        logger.info("STEP 3: GENERATE PHASE 1 TRIPLETS (streaming)")
        logger.info("=" * 60)

        if self._check_phase_complete('phase1') or self.skip_to_phase3:
            if self.skip_to_phase3:
                logger.info("🔄 Skipping Phase 1 (skip-to-phase3 mode)")
            else:
                logger.info("✓ Phase 1 checkpoint found, skipping...")
            phase1_train = pq.read_table(self.output_dir / 'triplets' / 'phase1' / 'train.parquet')
            phase1_val = pq.read_table(self.output_dir / 'triplets' / 'phase1' / 'val.parquet')
            self.stats['phase1']['triplets'] = len(phase1_train) + len(phase1_val)
        else:
            self.generate_phase1_triplets_streaming(pairs_by_bin)

        # Clear memory before Phase 3
        gc.collect()
        self.knn.reset_failure_tracking()

        # Step 4: Generate Phase 3 triplets
        logger.info("\n" + "=" * 60)
        logger.info("STEP 4: GENERATE PHASE 3 TRIPLETS")
        logger.info("=" * 60)

        if self._check_phase_complete('phase3'):
            logger.info("✓ Phase 3 checkpoint found")

            # Check if text fields exist (needed for Student training)
            if not self._check_phase3_has_text_fields():
                logger.info("⚠ Phase 3 missing text fields, augmenting...")
                self._augment_phase3_with_text_fields()
            else:
                logger.info("✓ Phase 3 has text fields, skipping...")

            phase3_train = pq.read_table(self.output_dir / 'triplets' / 'phase3' / 'train.parquet')
            phase3_val = pq.read_table(self.output_dir / 'triplets' / 'phase3' / 'val.parquet')
            self.stats['phase3']['triplets'] = len(phase3_train) + len(phase3_val)
        else:
            self.generate_phase3_triplets_streaming(pairs_by_bin)

        # Save statistics
        stats_path = self.output_dir / 'training_stats.json'
        with open(stats_path, 'w') as f:
            json.dump(self.stats, f, indent=2)
        logger.info(f"Statistics saved to {stats_path}")

        return self.stats

    def generate_positive_pairs(self) -> Dict[str, List[Tuple]]:
        """
        Generate positive pairs from co-located toponyms.

        Uses _LocalManifest to avoid a full ES scan — attestations, script, and
        lang are read from the cached Parquet manifest instead.
        """
        pairs_by_bin: Dict[str, List[Tuple]] = defaultdict(list)

        cluster_stats = {
            'places_processed': 0,
            'places_with_clusters': 0,
            'total_clusters': 0,
            'singleton_clusters': 0,
            'multi_clusters': 0,
            'cluster_sizes': Counter(),
        }

        # Use manifest instead of ES scan
        logger.info("Loading toponym manifest for pair generation...")
        self._manifest.ensure()

        logger.info("Grouping toponyms by place...")
        places: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        total_attestations = 0

        for tid, script, lang, atts in zip(
            self._manifest.ids,
            self._manifest.scripts,
            self._manifest.langs,
            self._manifest.attestations,
        ):
            for place_id in atts:
                places[place_id].append((tid, script, lang))
                total_attestations += 1

        logger.info(f"Found {total_attestations:,} toponym attestations")
        logger.info(f"Grouped into {len(places):,} places")

        # Diagnostic: Check if attestations are mostly empty
        if total_attestations == 0 and len(self._manifest.ids) > 0:
            logger.error(f"CRITICAL: All {len(self._manifest.ids):,} toponyms have empty attestations!")
            logger.error("This means the 'attestations' field in ES is not populated.")
            logger.error("You must run the rebuild_toponyms_index script without --resume to populate attestations.")
            logger.error("Or delete the DuckDB file to force a full rebuild from the places index.")
            raise ValueError("Cannot generate training data without attestations (place back-references)")

        # Filter and cap
        places_with_multiple = {}
        for p, t in places.items():
            if len(t) >= 2:
                if len(t) > MAX_TOPONYMS_PER_PLACE:
                    t = random.sample(t, MAX_TOPONYMS_PER_PLACE)
                places_with_multiple[p] = t

        logger.info(f"Places with ≥2 toponyms: {len(places_with_multiple):,}")

        def process_place(place_id: str, toponyms_info: List[Tuple]) -> Tuple[str, List, Dict]:
            id_to_info = {t[0]: (t[1], t[2]) for t in toponyms_info}
            toponym_ids = list(id_to_info.keys())
            clusters = self.knn.find_similar_in_place(place_id=place_id, toponym_ids=toponym_ids)
            return place_id, clusters, id_to_info

        logger.info(f"Running parallel ES KNN clustering...")
        total_pairs = 0

        with ThreadPoolExecutor(max_workers=ES_PARALLEL_WORKERS) as executor:
            futures = {
                executor.submit(process_place, pid, info): pid
                for pid, info in places_with_multiple.items()
            }

            iterator = tqdm(as_completed(futures), total=len(futures), desc="Clustering")

            for future in iterator:
                try:
                    place_id, clusters, id_to_info = future.result()
                except Exception as e:
                    logger.debug(f"Place processing failed: {e}")
                    continue

                cluster_stats['places_processed'] += 1
                cluster_stats['places_with_clusters'] += 1
                cluster_stats['total_clusters'] += len(clusters)

                if len(clusters) > 1:
                    cluster_stats['multi_clusters'] += 1

                seen_in_place: Set[Tuple[str, str]] = set()

                for cluster in clusters:
                    cluster_stats['cluster_sizes'][len(cluster)] += 1

                    if len(cluster) < 2:
                        cluster_stats['singleton_clusters'] += 1
                        continue

                    for i, id_a in enumerate(cluster):
                        for id_b in cluster[i + 1:]:
                            script_a, lang_a = id_to_info[id_a]
                            script_b, lang_b = id_to_info[id_b]

                            pair_key = tuple(sorted([id_a, id_b]))
                            if pair_key in seen_in_place:
                                continue
                            seen_in_place.add(pair_key)

                            key_a = get_script_lang_key(script_a, lang_a)
                            key_b = get_script_lang_key(script_b, lang_b)
                            bin_key = tuple(sorted([key_a, key_b]))
                            bin_key_str = f"{bin_key[0]}|{bin_key[1]}"

                            pairs_by_bin[bin_key_str].append((id_a, id_b, 0.0))
                            total_pairs += 1

                if cluster_stats['places_processed'] % 10000 == 0:
                    self.knn.check_failure_threshold()

        failure_rate = self.knn.get_failure_rate()
        logger.info(f"ES failure rate: {failure_rate:.2%}")
        self.knn.check_failure_threshold()

        logger.info(f"Generated {total_pairs:,} positive pairs")
        logger.info(f"Distributed across {len(pairs_by_bin)} bins")

        # Log statistics
        logger.info("Clustering statistics:")
        logger.info(f"  Places processed: {cluster_stats['places_processed']:,}")
        logger.info(f"  Total clusters: {cluster_stats['total_clusters']:,}")

        bin_sizes = [(k, len(v)) for k, v in pairs_by_bin.items()]
        bin_sizes.sort(key=lambda x: -x[1])
        logger.info("Top 10 bins:")
        for bin_key, count in bin_sizes[:10]:
            logger.info(f"  {bin_key}: {count:,}")

        self.stats['phase1']['pairs'] = total_pairs
        self.stats['phase1']['by_bin'] = {k: len(v) for k, v in pairs_by_bin.items()}

        # Save pairs
        pairs_dir = self.output_dir / 'pairs'
        pairs_dir.mkdir(exist_ok=True)

        all_pairs = []
        for bin_key, pairs in pairs_by_bin.items():
            for id_a, id_b, sim in pairs:
                all_pairs.append({'anchor': id_a, 'positive': id_b, 'bin': bin_key})

        if all_pairs:
            table = pa.Table.from_pylist(all_pairs)
            pq.write_table(table, pairs_dir / 'positive_pairs.parquet')
            logger.info(f"Saved pairs to {pairs_dir / 'positive_pairs.parquet'}")

        return pairs_by_bin

    def generate_phase2_samples(self):
        """Generate Phase 2 samples with streaming writes."""
        logger.info("=" * 60)
        logger.info("PHASE 2: GENERATING BALANCED TRAINING SAMPLES")
        logger.info("=" * 60)

        training_dir = self.output_dir / 'training'
        training_dir.mkdir(exist_ok=True)

        # Load char vocabulary
        vocab_path = self.output_dir / 'vocab' / 'char_vocab.json'
        if not vocab_path.exists():
            logger.error(f"Char vocabulary not found at {vocab_path}")
            return

        with open(vocab_path) as f:
            char_vocab = json.load(f)
        char_to_id = char_vocab.get('char_to_id', {})
        unk_id = char_to_id.get('<UNK>', 1)
        logger.info(f"Loaded char vocabulary with {len(char_to_id)} characters")

        ns_filter = [{"term": {"namespaces": ns}} for ns in self.training_namespaces]

        # Count with embedding
        with_emb_query = {
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "panphon_embedding"}}],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            }
        }
        with_features = self.es.count(index="toponyms", body=with_emb_query)['count']
        logger.info(f"Found {with_features:,} toponyms with PanPhon embeddings")

        # Count bin sizes using ES aggregation
        logger.info("Pass 1: Counting samples per bin...")
        bin_counts: Counter = Counter()

        agg_query = {
            "size": 0,
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "panphon_embedding"}}],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            },
            "aggs": {
                "script_lang": {
                    "composite": {
                        "size": 10000,
                        "sources": [
                            {"script": {"terms": {"field": "script"}}},
                            {"lang": {"terms": {"field": "lang"}}}
                        ]
                    }
                }
            }
        }

        after_key = None
        while True:
            if after_key:
                agg_query["aggs"]["script_lang"]["composite"]["after"] = after_key

            result = self.es.search(index="toponyms", body=agg_query)
            buckets = result['aggregations']['script_lang']['buckets']

            if not buckets:
                break

            for bucket in buckets:
                script = bucket['key']['script'] or 'UNKNOWN'
                lang = bucket['key']['lang'] or ''
                count = bucket['doc_count']
                bin_key = get_script_lang_key(script, lang)
                bin_counts[bin_key] = count

            after_key = result['aggregations']['script_lang'].get('after_key')
            if not after_key:
                break

        logger.info(f"Found {len(bin_counts)} bins")

        # Calculate sampling quotas
        bin_quotas = {}
        dropped_bins = []
        stats = {'bins_total': len(bin_counts), 'bins_dropped': 0, 'bins_capped': 0,
                 'bins_oversampled': 0, 'bins_unchanged': 0}

        for bin_key, count in bin_counts.items():
            if count < MIN_BIN_SIZE:
                dropped_bins.append((bin_key, count))
                stats['bins_dropped'] += 1
                continue

            if count >= TARGET_SAMPLES_PER_BIN:
                target = TARGET_SAMPLES_PER_BIN
                prob = TARGET_SAMPLES_PER_BIN / count
                stats['bins_capped'] += 1
            else:
                max_target = min(TARGET_SAMPLES_PER_BIN, count * MAX_OVERSAMPLE_FACTOR)
                if max_target > count:
                    target = max_target
                    prob = max_target / count
                    stats['bins_oversampled'] += 1
                else:
                    target = count
                    prob = 1.0
                    stats['bins_unchanged'] += 1

            bin_quotas[bin_key] = (target, prob)

        logger.info(
            f"Bin balancing: dropped={stats['bins_dropped']}, capped={stats['bins_capped']}, oversampled={stats['bins_oversampled']}")

        # Use streaming writer
        writer = MultiSplitStreamingWriter(training_dir, batch_size=PARQUET_BATCH_SIZE)

        # Stream from ES — Phase 2 needs name, ipa, panphon_embedding which
        # the manifest doesn't store, so this scan is unavoidable
        logger.info("Pass 2: Streaming and writing...")

        es_query = {
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "panphon_embedding"}}],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            },
            "_source": ["name", "script", "lang", "ipa", "panphon_embedding"]
        }

        processed = 0
        for hit in tqdm(scan(self.es, index="toponyms", query=es_query, scroll='5m', size=5000),
                        total=with_features, desc="Streaming"):
            toponym_id = hit['_id']
            source = hit['_source']
            name = source.get('name', '')
            script = source.get('script', 'UNKNOWN')
            lang = source.get('lang', '')
            panphon_features = source.get('panphon_embedding', [])

            bin_key = get_script_lang_key(script, lang)

            if bin_key not in bin_quotas:
                continue

            target, prob = bin_quotas[bin_key]

            rng = random.Random(RANDOM_SEED + (zlib.crc32(toponym_id.encode('utf-8')) & 0xffffffff))

            if prob >= 1.0:
                num_copies = int(prob)
                if rng.random() < (prob - num_copies):
                    num_copies += 1
            else:
                num_copies = 1 if rng.random() < prob else 0

            if num_copies == 0:
                continue

            if not panphon_features:
                continue

            # Convert to numpy float32 for efficient Parquet storage
            features = np.array(panphon_features, dtype=np.float32)

            char_ids = [char_to_id.get(c, unk_id) for c in name]

            hash_val = (zlib.crc32(toponym_id.encode('utf-8')) & 0xffffffff) % 10
            if hash_val == 0:
                split = 'test'
            elif hash_val == 1:
                split = 'val'
            else:
                split = 'train'

            sample = {
                'toponym_id': toponym_id,
                'name': name,
                'script': script,
                'lang': lang or '',
                'char_ids': char_ids,
                'features': features,
                'feature_length': len(features) // 24 if len(features) >= 24 else len(features),
                'epitran_supported': True,
                'split': split,
            }

            for _ in range(num_copies):
                writer.add_sample(split, sample.copy())

            processed += 1
            if processed % 500000 == 0:
                logger.info(f"  Processed {processed:,}...")

        writer.close_all()
        split_counts = writer.get_counts()

        total_samples = sum(split_counts.values())
        self.stats['phase2']['samples'] = total_samples
        self.stats['phase2']['balance_stats'] = stats
        self.stats['phase2']['by_split'] = split_counts

        logger.info(f"Sampled {total_samples:,} total samples")
        for split_name, count in split_counts.items():
            logger.info(f"  {split_name}: {count:,}")

    def generate_phase1_triplets_streaming(self, pairs_by_bin: Dict[str, List[Tuple]]):
        """
        Generate Phase 1 triplets with STREAMING writes to avoid OOM.

        Uses _IntegerAdjacency for memory-efficient edge storage and
        _LocalManifest for negative candidate sampling (no second ES scan).
        """
        logger.info("=" * 60)
        logger.info("PHASE 1: Streaming triplet generation (memory-efficient)")
        logger.info("=" * 60)

        # Build integer-encoded adjacency (reused if Phase 3 runs after)
        logger.info("Building adjacency set...")
        adjacency = self._build_adjacency(pairs_by_bin)
        logger.info(f"Adjacency set has {len(adjacency):,} edges")

        # Collect all unique toponym IDs we need features for
        logger.info("Collecting unique toponym IDs...")
        all_anchor_ids = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                all_anchor_ids.add(id_a)
                all_anchor_ids.add(id_b)

        # Load toponym data with features using NUMPY arrays (60-70% memory savings)
        # Also load attestations for negative validation
        logger.info(f"Loading toponym data for {len(all_anchor_ids):,} toponyms...")
        toponym_data_map = {}
        attestation_map = {}  # toponym_id -> Set[attestation_id]

        raw_data = self._batch_mget(
            list(all_anchor_ids),
            source_fields=['script', 'lang', 'panphon_embedding', 'attestations'],
            desc="Loading toponym data",
        )

        for toponym_id, source in raw_data.items():
            embedding = source.get('panphon_embedding')
            if embedding and len(embedding) > 0:
                toponym_data_map[toponym_id] = {
                    'script': source.get('script', 'UNKNOWN'),
                    'lang': source.get('lang', ''),
                    'features': np.array(embedding, dtype=np.float32),
                    'feature_length': len(embedding) // 24,
                }
                attestation_map[toponym_id] = set(source.get('attestations', []))

        del raw_data
        logger.info(f"Loaded data for {len(toponym_data_map):,} toponyms")

        # Load negative candidates from manifest (no second ES scan)
        logger.info("Loading negative candidates from manifest...")
        self._manifest.ensure()
        all_ids = self._manifest.all_ids
        ids_by_script = self._manifest.ids_by_script

        logger.info(f"Negative candidate pool: {len(all_ids):,} IDs, {len(ids_by_script)} scripts")

        # Apply bin balancing (reduced targets to optimize for quality over quantity)
        logger.info("Applying bin-balancing...")
        logger.info(f"  Target: {TARGET_SAMPLES_PER_BIN:,} per bin (reduced from 50k)")
        logger.info(f"  Min bin: {MIN_BIN_SIZE:,} (reduced from 1k)")
        logger.info(f"  Max oversample: {MAX_OVERSAMPLE_FACTOR}x (reduced from 5x)")
        balanced_pairs, balance_stats = apply_bin_balancing(
            pairs_by_bin,
            target_per_bin=TARGET_SAMPLES_PER_BIN,
            min_bin_size=MIN_BIN_SIZE,
            max_oversample=MAX_OVERSAMPLE_FACTOR,
        )
        logger.info(f"Balanced pairs: {len(balanced_pairs):,}")

        # Set up streaming writer
        triplets_dir = self.output_dir / 'triplets' / 'phase1'
        triplets_dir.mkdir(parents=True, exist_ok=True)
        writer = TripletStreamingWriter(triplets_dir, batch_size=PARQUET_BATCH_SIZE)

        # THREE-PASS APPROACH for memory-efficient attestation validation:
        # Pass 1a: Generate candidate negatives (adjacency check only)
        # Pass 1b: Batch fetch attestations for all unique candidates
        # Pass 1c: Validate against attestations, select final negatives
        # Pass 2: Batch fetch negative features
        # Pass 3: Stream triplets to Parquet

        logger.info("Pass 1a: Generating candidate negatives...")
        pending_triplets = []  # (anchor, positive, [candidates], bin_key, anchor_attestations)
        all_candidates = set()

        for triplet_idx, pair in enumerate(tqdm(balanced_pairs, desc="Generating candidates")):
            anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)

            if anchor not in toponym_data_map or positive not in toponym_data_map:
                continue

            anchor_data = toponym_data_map[anchor]
            script = anchor_data['script']
            lang = anchor_data['lang']
            anchor_attestations = attestation_map.get(anchor, set())

            # Deterministic negative sampling
            seed = RANDOM_SEED + (zlib.crc32(anchor.encode('utf-8')) & 0xffffffff) + triplet_idx
            rng = random.Random(seed)

            # Generate candidate negatives (adjacency check only, attestation check deferred)
            candidates = []
            for _ in range(50):
                use_same_script = rng.random() < PHASE1_SAME_SCRIPT_NEGATIVE_RATIO

                if use_same_script and script and script in ids_by_script and len(ids_by_script[script]) > 0:
                    candidate = rng.choice(ids_by_script[script])
                else:
                    candidate = rng.choice(all_ids)

                # Quick check: skip if in adjacency (known positive pairs)
                if (anchor, candidate) in adjacency or (positive, candidate) in adjacency:
                    continue

                candidates.append(candidate)
                all_candidates.add(candidate)

                if len(candidates) >= 10:  # Keep top 10 candidates per triplet
                    break

            if candidates:
                bin_key = get_script_lang_key(script, lang)
                pending_triplets.append((anchor, positive, candidates, bin_key, anchor_attestations))

        logger.info(
            f"Generated candidates for {len(pending_triplets):,} triplets, {len(all_candidates):,} unique candidates")

        # Pass 1b: Batch fetch attestations for all candidates
        logger.info("Pass 1b: Fetching attestations for candidates...")
        raw_attestations = self._batch_mget(
            list(all_candidates),
            source_fields=['attestations'],
            desc="Fetching candidate attestations",
        )
        candidate_attestations_map = {
            tid: set(source.get('attestations', []))
            for tid, source in raw_attestations.items()
        }
        del raw_attestations
        logger.info(f"Fetched attestations for {len(candidate_attestations_map):,} candidates")

        # Pass 1c: Validate candidates and select final negatives
        logger.info("Pass 1c: Validating candidates...")
        triplet_specs = []  # (anchor, positive, negative, bin_key)
        negatives_to_fetch = set()
        skipped_no_negative = 0

        for anchor, positive, candidates, bin_key, anchor_attestations in tqdm(pending_triplets, desc="Validating"):
            negative = None
            for candidate in candidates:
                candidate_attestations = candidate_attestations_map.get(candidate, set())

                # Valid if no attestation overlap
                if anchor_attestations.isdisjoint(candidate_attestations):
                    negative = candidate
                    break

            if negative is None:
                skipped_no_negative += 1
                continue

            triplet_specs.append((anchor, positive, negative, bin_key))

            # Track negatives that need feature fetching
            if negative not in toponym_data_map:
                negatives_to_fetch.add(negative)

        # Free memory
        del pending_triplets, all_candidates, candidate_attestations_map

        logger.info(
            f"Selected {len(triplet_specs):,} triplets, need features for {len(negatives_to_fetch):,} negatives")
        if skipped_no_negative > 0:
            logger.info(f"Skipped {skipped_no_negative:,} pairs (no valid negative found)")

        # Pass 2: Batch fetch negative features
        negative_features = {}  # negative_id -> {features, feature_length}
        if negatives_to_fetch:
            logger.info("Pass 2: Batch fetching negative features...")
            raw_neg = self._batch_mget(
                list(negatives_to_fetch),
                source_fields=['panphon_embedding'],
                desc="Fetching negative features",
            )
            for tid, source in raw_neg.items():
                emb = source.get('panphon_embedding')
                if emb and len(emb) > 0:
                    negative_features[tid] = {
                        'features': np.array(emb, dtype=np.float32),
                        'feature_length': len(emb) // 24,
                    }
            del raw_neg
            logger.info(f"Fetched features for {len(negative_features):,} negatives")

        # Pass 3: Stream triplets to Parquet
        logger.info("Pass 3: Streaming triplets to Parquet...")
        triplet_count = 0
        skipped_missing_features = 0

        for anchor, positive, negative, bin_key in tqdm(triplet_specs, desc="Writing triplets"):
            anchor_data = toponym_data_map[anchor]
            positive_data = toponym_data_map[positive]

            # Get negative features
            if negative in toponym_data_map:
                negative_data = toponym_data_map[negative]
            elif negative in negative_features:
                negative_data = negative_features[negative]
            else:
                skipped_missing_features += 1
                continue

            triplet = {
                'anchor_id': anchor,
                'positive_id': positive,
                'negative_id': negative,
                'bin': bin_key,
                'anchor_features': anchor_data['features'],
                'anchor_feature_length': anchor_data['feature_length'],
                'positive_features': positive_data['features'],
                'positive_feature_length': positive_data['feature_length'],
                'negative_features': negative_data['features'],
                'negative_feature_length': negative_data['feature_length'],
            }

            split_hash = (zlib.crc32(anchor.encode('utf-8')) & 0xffffffff) % 10
            writer.add_triplet(triplet, split_hash)
            triplet_count += 1

        # Close writer
        train_count, val_count = writer.close()

        total_skipped = skipped_no_negative + skipped_missing_features
        logger.info(f"Generated {triplet_count:,} triplets (skipped {total_skipped:,})")
        logger.info(f"Saved {train_count:,} train, {val_count:,} val triplets")

        self.stats['phase1']['triplets'] = triplet_count
        self.stats['phase1']['balance_stats'] = balance_stats

        # Free memory
        del toponym_data_map
        del negative_features
        del triplet_specs
        gc.collect()

    def generate_phase3_triplets_streaming(self, pairs_by_bin: Dict[str, List[Tuple]]):
        """
        Generate Phase 3 triplets with hard negatives using streaming writes.

        Uses _IntegerAdjacency for memory-efficient edge storage.
        Reuses toponym_data_map embeddings for KNN queries instead of
        fetching them separately via batch_get_embeddings.

        Supports resumption from phase3_partial/ checkpoint if ES died mid-run:

            1. Before ES dies (replace "7971159" with current Job ID):
            rsync -av --progress /scratch/slurm-7971159/triplets/phase3/ /ix1/ishi/models/phonetic/data/v6/triplets/phase3_partial/

            2. Restart ES:
            es -staging-start

            3. Resume Phase 3 only:
            es -generate-training-data 6 --skip-to-phase3

        """
        logger.info("=" * 60)
        logger.info("PHASE 3: Streaming hard negative triplet generation")
        logger.info("=" * 60)

        # Initialize variables that might be used later (to avoid UnboundLocalError)
        existing_triplets = 0
        partial_dir = self.output_dir / 'triplets' / 'phase3_partial'
        balance_stats = {}

        # Check for Pass 1 completion checkpoint
        checkpoint_dir = self.output_dir / 'triplets' / 'phase3_checkpoint'
        triplet_specs_file = checkpoint_dir / 'triplet_specs.pkl'
        negatives_file = checkpoint_dir / 'negatives_to_fetch.pkl'

        resume_from_pass2 = (
            checkpoint_dir.exists()
            and triplet_specs_file.exists()
            and negatives_file.exists()
        )

        if resume_from_pass2:
            logger.info("=" * 60)
            logger.info("✓ RESUMING FROM PASS 2 (Pass 1 checkpoint found)")
            logger.info("=" * 60)
            logger.info("Loading triplet_specs and negatives_to_fetch from checkpoint...")

            with open(triplet_specs_file, 'rb') as f:
                triplet_specs = pickle.load(f)
            with open(negatives_file, 'rb') as f:
                negatives_to_fetch = pickle.load(f)

            logger.info(f"Loaded {len(triplet_specs):,} triplet specs, {len(negatives_to_fetch):,} negatives")

            # We still need toponym_data_map for writing triplets, but we can skip:
            # - Adjacency building
            # - Bin balancing
            # - ES KNN mining (Pass 1)

            # Extract unique anchors and positives from triplet_specs
            unique_toponyms = set()
            for anchor, positive, negative, _ in triplet_specs:
                unique_toponyms.add(anchor)
                unique_toponyms.add(positive)

            logger.info(f"Loading data for {len(unique_toponyms):,} toponyms (anchors + positives)...")
            toponym_data_map = {}

            raw_data = self._batch_mget(
                list(unique_toponyms),
                source_fields=['name', 'script', 'lang', 'panphon_embedding'],
                desc="Loading anchor/positive data",
            )

            for toponym_id, source in raw_data.items():
                embedding = source.get('panphon_embedding')
                if embedding and len(embedding) > 0:
                    toponym_data_map[toponym_id] = {
                        'name': source.get('name', ''),
                        'script': source.get('script', 'UNKNOWN'),
                        'lang': source.get('lang', ''),
                        'features': np.array(embedding, dtype=np.float32),
                        'feature_length': len(embedding) // 24,
                    }

            del raw_data
            logger.info(f"Loaded data for {len(toponym_data_map):,} toponyms")

            # Initialize for final checks (we skip Pass 1 entirely)
            failed_lookups = 0

            # Jump directly to Pass 2

        else:
            # Normal flow: Run Pass 1
            logger.info("Starting Pass 1: Hard negative mining via ES")

            # Initialize variables used later
            failed_lookups = 0

            # Check for partial checkpoint (mid-Pass 1 interruption)
            skip_batches = 0
            # Note: existing_triplets and partial_dir initialized at function start

            if partial_dir.exists():
                # Count existing triplets
                for split_file in partial_dir.glob('split=*/*.parquet'):
                    try:
                        existing_triplets += pq.read_table(split_file).num_rows
                    except:
                        pass

                if existing_triplets > 0:
                    logger.info(f"Found partial checkpoint: {existing_triplets:,} existing triplets")
                    logger.info(f"Resuming from batch ~{existing_triplets // MSEARCH_BATCH_SIZE}")
                    skip_batches = existing_triplets // MSEARCH_BATCH_SIZE
                else:
                    logger.info("phase3_partial/ exists but is empty, starting fresh")
            else:
                logger.info("No partial checkpoint found, starting fresh")

            # Build integer-encoded adjacency
            logger.info("Building adjacency set...")
            adjacency = self._build_adjacency(pairs_by_bin)
            logger.info(f"Adjacency set has {len(adjacency):,} edges")

            # Build per-anchor adjacency lookup for O(1) checks
            logger.info("Building per-anchor adjacency lookup...")
            adj_by_anchor = defaultdict(set)
            for pairs in pairs_by_bin.values():
                for a, b, _ in pairs:
                    adj_by_anchor[a].add(b)
                    adj_by_anchor[b].add(a)
            logger.info(f"Built adjacency lookup for {len(adj_by_anchor):,} anchors")

            # Apply bin balancing (reduced targets to optimize for quality over quantity)
            logger.info("Applying bin-balancing...")
            logger.info(f"  Target: {TARGET_SAMPLES_PER_BIN:,} per bin (reduced from 50k)")
            logger.info(f"  Min bin: {MIN_BIN_SIZE:,} (reduced from 1k)")
            logger.info(f"  Max oversample: {MAX_OVERSAMPLE_FACTOR}x (reduced from 5x)")
            balanced_pairs, balance_stats = apply_bin_balancing(
                pairs_by_bin,
                target_per_bin=TARGET_SAMPLES_PER_BIN,
                min_bin_size=MIN_BIN_SIZE,
                max_oversample=MAX_OVERSAMPLE_FACTOR,
            )
            logger.info(f"Balanced pairs: {len(balanced_pairs):,}")

            # Cap Phase 3 total pairs to reduce ES load and training time
            PHASE3_MAX_TOTAL = 10_000_000  # 10M triplets total
            if len(balanced_pairs) > PHASE3_MAX_TOTAL:
                logger.info(f"Capping Phase 3 pairs from {len(balanced_pairs):,} to {PHASE3_MAX_TOTAL:,}")
                # Take a stratified sample to preserve bin distribution
                random.seed(RANDOM_SEED)
                random.shuffle(balanced_pairs)
                balanced_pairs = balanced_pairs[:PHASE3_MAX_TOTAL]
                logger.info(f"Capped pairs: {len(balanced_pairs):,}")

            # Collect unique anchors
            unique_anchors = set()
            for pair in balanced_pairs:
                anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)
                unique_anchors.add(anchor)
                unique_anchors.add(positive)
            logger.info(f"Unique toponyms: {len(unique_anchors):,}")

            # Load anchor info AND features (for self-contained triplets)
            # Include 'name' for Student training (Phase 3 trains Student on hard negatives)
            # Include 'attestations' for negative validation
            logger.info("Loading anchor info, features, and attestations...")
            toponym_data_map = {}  # toponym_id -> {script, lang, name, features, feature_length}
            attestation_map = {}  # toponym_id -> Set[attestation_id]

            raw_data = self._batch_mget(
                list(unique_anchors),
                source_fields=['name', 'script', 'lang', 'panphon_embedding', 'attestations'],
                desc="Loading anchor data",
            )

            for toponym_id, source in raw_data.items():
                embedding = source.get('panphon_embedding')
                if embedding and len(embedding) > 0:
                    toponym_data_map[toponym_id] = {
                        'name': source.get('name', ''),
                        'script': source.get('script', 'UNKNOWN'),
                        'lang': source.get('lang', ''),
                        'features': np.array(embedding, dtype=np.float32),
                        'feature_length': len(embedding) // 24,
                    }
                    # Convert to frozenset once for faster disjoint checks
                    attestation_map[toponym_id] = frozenset(source.get('attestations', []))

            del raw_data
            logger.info(f"Loaded data for {len(toponym_data_map):,} toponyms")

            # Precompute deterministic seed per anchor (avoids repeated CRC32 in hot loop)
            logger.info("Precomputing anchor seeds...")
            anchor_seed_map = {
                tid: (RANDOM_SEED + (zlib.crc32(tid.encode('utf-8')) & 0xffffffff))
                for tid in unique_anchors
            }

            # Build pairs to process
            all_pairs_to_process = []
            for pair in balanced_pairs:
                anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)
                if anchor in toponym_data_map and positive in toponym_data_map:
                    script = toponym_data_map[anchor]['script']
                    lang = toponym_data_map[anchor]['lang']
                    bin_key = get_script_lang_key(script, lang)
                    all_pairs_to_process.append((anchor, positive, bin_key))

            logger.info(f"Processing {len(all_pairs_to_process):,} pairs...")

            # Extract embeddings for KNN from already-loaded toponym_data_map
            # This avoids a redundant batch_get_embeddings fetch from ES
            logger.info("Extracting embeddings for KNN from loaded data...")
            anchor_embeddings = {}
            for tid, data in toponym_data_map.items():
                # Convert numpy float32 back to list for KNN helper compatibility
                anchor_embeddings[tid] = data['features'].tolist()
            logger.info(f"Prepared {len(anchor_embeddings):,} embeddings for KNN")

            # Build batches for _msearch
            batches = []
            current_batch = []
            failed_lookups = 0

            for sample_idx, (anchor, positive, bin_key) in enumerate(all_pairs_to_process):
                if anchor not in anchor_embeddings:
                    failed_lookups += 1
                    continue

                anchor_data = toponym_data_map[anchor]
                current_batch.append({
                    'anchor_id': anchor,
                    'positive_id': positive,
                    'embedding': anchor_embeddings[anchor],
                    'script': anchor_data['script'],
                    'bin': bin_key,
                    'sample_idx': sample_idx,
                })

                if len(current_batch) >= MSEARCH_BATCH_SIZE:
                    batches.append(current_batch)
                    current_batch = []

            if current_batch:
                batches.append(current_batch)

            logger.info(f"Processing {len(batches):,} batches...")

            # Pass 1: Mine hard negatives from ES
            logger.info("Pass 1: Mining hard negatives (optimized)...")
            triplet_specs = []  # (anchor_id, positive_id, negative_id, bin_key)
            negatives_to_fetch = set()
            batches_processed = 0

            # Skip batches that were already processed in partial run
            batches_to_process = batches[skip_batches:] if skip_batches > 0 else batches
            logger.info(f"Processing batches {skip_batches} to {len(batches)} ({len(batches_to_process)} remaining)")

            for batch in tqdm(batches_to_process, desc="Hard negative mining", initial=skip_batches, total=len(batches)):
                # Get candidate lists with attestations from ES (return_lists=True returns raw hits)
                candidate_responses = self.knn.find_hard_negatives_batch_with_attestations(
                    anchors=batch,
                    k=20,
                )

                # High-performance local filtering with reservoir sampling
                for item, hits in zip(batch, candidate_responses):
                    if not hits:
                        continue

                    anchor_id = item['anchor_id']
                    positive_id = item['positive_id']

                    # Fast lookups (no dict construction)
                    anchor_att = attestation_map.get(anchor_id)
                    adj_set = adj_by_anchor.get(anchor_id, set())

                    # Deterministic RNG per anchor (seed precomputed)
                    seed = anchor_seed_map.get(anchor_id, RANDOM_SEED) + item['sample_idx']
                    rng = random.Random(seed)

                    # Reservoir sampling: select one negative uniformly without building list
                    selected = None
                    seen_valid = 0

                    # Iterate ES hits (each hit has _id and _source with attestations)
                    for hit in hits:
                        candidate_id = hit['_id']

                        # Skip self
                        if candidate_id == anchor_id:
                            continue

                        # O(1) adjacency check using set membership
                        if candidate_id in adj_set:
                            continue

                        # Fast attestation disjoint check (no set construction in loop)
                        # Convert candidate attestations to frozenset once per candidate
                        candidate_att = frozenset(hit.get('_source', {}).get('attestations', []))
                        if anchor_att and not anchor_att.isdisjoint(candidate_att):
                            continue

                        # Reservoir sampling: uniform selection without list allocation
                        seen_valid += 1
                        if rng.randrange(seen_valid) == 0:
                            selected = candidate_id

                    if selected:
                        # Verify anchor and positive have data
                        if anchor_id in toponym_data_map and positive_id in toponym_data_map:
                            triplet_specs.append((anchor_id, positive_id, selected, item['bin']))

                            # Track negatives needing feature fetch
                            if selected not in toponym_data_map:
                                negatives_to_fetch.add(selected)

                batches_processed += 1
                # Check ES health less frequently (every 200 batches instead of 100)
                if batches_processed % 200 == 0:
                    self.knn.check_failure_threshold()

            logger.info(f"Found {len(triplet_specs):,} triplets, need features for {len(negatives_to_fetch):,} negatives")

            # Save Pass 1 checkpoint (only in normal flow, not when resuming)
            checkpoint_dir = self.output_dir / 'triplets' / 'phase3_checkpoint'
            checkpoint_dir.mkdir(parents=True, exist_ok=True)

            triplet_specs_file = checkpoint_dir / 'triplet_specs.pkl'
            negatives_file = checkpoint_dir / 'negatives_to_fetch.pkl'

            logger.info("Saving Pass 1 checkpoint...")
            with open(triplet_specs_file, 'wb') as f:
                pickle.dump(triplet_specs, f, protocol=pickle.HIGHEST_PROTOCOL)
            with open(negatives_file, 'wb') as f:
                pickle.dump(negatives_to_fetch, f, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info(f"✓ Checkpoint saved: {len(triplet_specs):,} triplet specs, {len(negatives_to_fetch):,} negatives")

        # Set up streaming writer (both flows need this)
        triplets_dir = self.output_dir / 'triplets' / 'phase3'
        triplets_dir.mkdir(parents=True, exist_ok=True)
        writer = TripletStreamingWriter(triplets_dir, batch_size=PARQUET_BATCH_SIZE)

        # Pass 2: Batch fetch negative features (and names for Student training)
        negative_features = {}
        if negatives_to_fetch:
            logger.info("Pass 2: Batch fetching negative features...")
            raw_neg = self._batch_mget(
                list(negatives_to_fetch),
                source_fields=['name', 'script', 'lang', 'panphon_embedding'],
                desc="Fetching negative features",
            )
            for tid, source in raw_neg.items():
                emb = source.get('panphon_embedding')
                if emb and len(emb) > 0:
                    negative_features[tid] = {
                        'name': source.get('name', ''),
                        'script': source.get('script', 'UNKNOWN'),
                        'lang': source.get('lang', ''),
                        'features': np.array(emb, dtype=np.float32),
                        'feature_length': len(emb) // 24,
                    }
            del raw_neg
            logger.info(f"Fetched features for {len(negative_features):,} negatives")

        # Pass 3: Stream triplets to Parquet
        logger.info("Pass 3: Streaming triplets to Parquet...")
        triplet_count = 0
        skipped_missing = 0

        for anchor_id, positive_id, hard_neg, bin_key in tqdm(triplet_specs, desc="Writing triplets"):
            anchor_data = toponym_data_map[anchor_id]
            positive_data = toponym_data_map[positive_id]

            # Get negative features
            if hard_neg in toponym_data_map:
                negative_data = toponym_data_map[hard_neg]
            elif hard_neg in negative_features:
                negative_data = negative_features[hard_neg]
            else:
                skipped_missing += 1
                continue

            triplet = {
                'anchor_id': anchor_id,
                'positive_id': positive_id,
                'negative_id': hard_neg,
                'negative_type': 'hard',
                'bin': bin_key,
                # Text data for Student training (Phase 3 trains Student on hard negatives)
                'anchor_name': anchor_data['name'],
                'anchor_script': anchor_data['script'],
                'anchor_lang': anchor_data['lang'],
                'positive_name': positive_data['name'],
                'positive_script': positive_data['script'],
                'positive_lang': positive_data['lang'],
                'negative_name': negative_data['name'],
                'negative_script': negative_data['script'],
                'negative_lang': negative_data['lang'],
                # Phonetic features for Teacher training (optional, enables Teacher fine-tuning)
                'anchor_features': anchor_data['features'],
                'anchor_feature_length': anchor_data['feature_length'],
                'positive_features': positive_data['features'],
                'positive_feature_length': positive_data['feature_length'],
                'negative_features': negative_data['features'],
                'negative_feature_length': negative_data['feature_length'],
            }

            split_hash = (zlib.crc32(anchor_id.encode('utf-8')) & 0xffffffff) % 10
            writer.add_triplet(triplet, split_hash)
            triplet_count += 1

        # Final checks
        failure_rate = self.knn.get_failure_rate()
        logger.info(f"ES failure rate: {failure_rate:.2%}")
        self.knn.check_failure_threshold()

        if failed_lookups > 0:
            logger.warning(f"Failed embedding lookups: {failed_lookups:,}")
        if skipped_missing > 0:
            logger.warning(f"Skipped triplets (missing features): {skipped_missing:,}")

        train_count, val_count = writer.close()

        logger.info(f"Generated {triplet_count:,} Phase 3 triplets")

        # If we had a partial checkpoint, merge the files
        if existing_triplets > 0:
            logger.info(f"Merging {existing_triplets:,} partial triplets with {triplet_count:,} new triplets")
            triplets_dir = self.output_dir / 'triplets' / 'phase3'

            # Read partial files
            for split in ['train', 'val']:
                partial_split_dir = partial_dir / f'split={split}'
                final_split_dir = triplets_dir / f'split={split}'

                if partial_split_dir.exists():
                    partial_files = list(partial_split_dir.glob('*.parquet'))
                    final_files = list(final_split_dir.glob('*.parquet'))

                    if partial_files:
                        # Read all partial parquet files
                        partial_tables = [pq.read_table(f) for f in partial_files]
                        # Read all new files
                        final_tables = [pq.read_table(f) for f in final_files] if final_files else []

                        # Concatenate
                        all_tables = partial_tables + final_tables
                        merged = pa.concat_tables(all_tables)

                        # Write merged file
                        final_split_dir.mkdir(parents=True, exist_ok=True)
                        pq.write_table(
                            merged,
                            final_split_dir / 'data.parquet',
                            compression='snappy'
                        )

                        # Update counts
                        if split == 'train':
                            train_count = merged.num_rows
                        else:
                            val_count = merged.num_rows

                        logger.info(f"Merged {split}: {merged.num_rows:,} total triplets")

            # Remove partial directory after successful merge
            import shutil
            shutil.rmtree(partial_dir)
            logger.info("Removed partial checkpoint after successful merge")

        logger.info(f"Saved {train_count:,} train, {val_count:,} val triplets")

        self.stats['phase3']['triplets'] = triplet_count
        self.stats['phase3']['balance_stats'] = balance_stats

        # Free memory
        del toponym_data_map
        del anchor_embeddings
        del negative_features
        del triplet_specs
        gc.collect()