#!/usr/bin/env python3
"""
v4 Training Data Generation Pipeline

Generates training data for all three phases of Symphonym training:
- Phase 1: Teacher training with phonetic features (triplets)
- Phase 2: Student alignment (all toponyms with PanPhon embeddings)
- Phase 3: Hard negative fine-tuning (triplets from ES similarity)

Key differences from v3:
- Uses ES KNN for PanPhon similarity queries (not Python computation)
- Balanced sampling by script+language pair across all phases
- Hard negatives selected via ES KNN (same script, similar embedding, different place)
"""

import argparse
import json
import logging
import random
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import hdbscan

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(x, **kwargs):
        return x

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import scan
except ImportError:
    print("ERROR: elasticsearch package required")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.getLogger("elastic_transport").setLevel(logging.WARNING)
logging.getLogger("elasticsearch").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Constants
TRAINING_NAMESPACES = ['gn', 'wd', 'tgn']

# Fallback threshold for edge cases (n=2 toponyms where HDBSCAN can't work)
# This is only used when exactly 2 toponyms exist in a place
PAIR_SIMILARITY_THRESHOLD = 0.5  # Generous threshold for the n=2 edge case

MAX_TOPONYMS_PER_PLACE = 50  # Cap to prevent combinatorial explosion (O(n²) pairs)
KNN_CANDIDATES = 100  # Number of candidates for KNN queries
ES_BATCH_SIZE = 500  # Batch size for ES bulk operations
ES_PARALLEL_WORKERS = 8  # Reduced from 16 to prevent ES request storms
MSEARCH_BATCH_SIZE = 100  # Number of queries per _msearch request

# ============================================================================
# ES RESILIENCE PARAMETERS
# ============================================================================
# Elasticsearch can become overloaded during heavy KNN operations. These settings
# implement exponential backoff with retry to prevent job failures.
ES_MAX_RETRIES = 5  # Maximum retry attempts per request
ES_INITIAL_BACKOFF = 1.0  # Initial backoff in seconds
ES_MAX_BACKOFF = 60.0  # Maximum backoff in seconds
ES_BACKOFF_FACTOR = 2.0  # Exponential backoff multiplier
ES_FAILURE_THRESHOLD = 0.1  # Abort if >10% of ES calls fail after retries

# ============================================================================
# PHASE 1 NEGATIVE SAMPLING
# ============================================================================
# Phase 1 negatives should mostly respect script to avoid teaching easy shortcuts.
# If negatives are script-agnostic, the Teacher learns "different script = different"
# rather than learning fine-grained phonetic discrimination.
PHASE1_SAME_SCRIPT_NEGATIVE_RATIO = 0.8  # 80% same-script, 20% any-script

# ============================================================================
# REPRODUCIBILITY
# ============================================================================
# Fixed random seed for reproducible train/val splits across runs.
# Note: Phase 2 uses deterministic zlib.crc32 hash of toponym_id for splitting,
# which is inherently reproducible. Phases 1 and 3 use this seed for shuffling
# and sampling operations.
RANDOM_SEED = 42

# ============================================================================
# UNIFIED BIN-BALANCING PARAMETERS
# ============================================================================
# Script-language stratification ensures adequate representation of under-resourced
# languages within well-resourced scripts (e.g., Latin-Swahili vs Latin-English)

# Target samples per script+language bin (capping for over-represented bins)
TARGET_SAMPLES_PER_BIN = 50000

# Minimum bin size to include (bins smaller than this are dropped to prevent
# severe overfitting from extreme oversampling)
MIN_BIN_SIZE = 1000

# Maximum oversampling factor (small bins can be oversampled up to this factor)
# Setting to 5 means a bin with 2000 samples can contribute up to 10000
MAX_OVERSAMPLE_FACTOR = 5

# Validation/test split ratios
VAL_RATIO = 0.1
TEST_RATIO = 0.1  # Only used in Phase 2


def apply_bin_balancing(
    samples_by_bin: Dict[str, List],
    target_per_bin: int = TARGET_SAMPLES_PER_BIN,
    min_bin_size: int = MIN_BIN_SIZE,
    max_oversample: int = MAX_OVERSAMPLE_FACTOR,
) -> Tuple[List, Dict[str, int]]:
    """
    Apply unified bin-balancing algorithm across all phases.

    Strategy:
    1. Drop bins below MIN_BIN_SIZE (would require extreme oversampling)
    2. Cap bins above TARGET_SAMPLES_PER_BIN
    3. Oversample bins between MIN_BIN_SIZE and TARGET_SAMPLES_PER_BIN
       (up to MAX_OVERSAMPLE_FACTOR to prevent severe repetition)

    Args:
        samples_by_bin: Dict mapping bin_key (script:lang) to list of samples
        target_per_bin: Target number of samples per bin
        min_bin_size: Minimum samples required (else drop bin)
        max_oversample: Maximum oversampling factor

    Returns:
        Tuple of (balanced_samples list, stats dict)
    """
    balanced = []
    stats = {
        'bins_total': len(samples_by_bin),
        'bins_dropped': 0,
        'bins_capped': 0,
        'bins_oversampled': 0,
        'bins_unchanged': 0,
        'dropped_bins': [],
        'samples_by_bin': {},
    }

    for bin_key, samples in samples_by_bin.items():
        bin_size = len(samples)

        # 1. Drop bins below minimum threshold
        if bin_size < min_bin_size:
            stats['bins_dropped'] += 1
            stats['dropped_bins'].append((bin_key, bin_size))
            continue

        # 2. Cap over-represented bins
        if bin_size >= target_per_bin:
            selected = random.sample(samples, target_per_bin)
            stats['bins_capped'] += 1
            stats['samples_by_bin'][bin_key] = target_per_bin

        # 3. Oversample under-represented bins (with limit)
        elif bin_size < target_per_bin:
            # Calculate how many samples we can reasonably add
            max_samples = min(target_per_bin, bin_size * max_oversample)

            if max_samples > bin_size:
                # Use random.choices for oversampling (with replacement)
                selected = random.choices(samples, k=max_samples)
                stats['bins_oversampled'] += 1
            else:
                selected = samples
                stats['bins_unchanged'] += 1

            stats['samples_by_bin'][bin_key] = len(selected)
        else:
            selected = samples
            stats['bins_unchanged'] += 1
            stats['samples_by_bin'][bin_key] = bin_size

        balanced.extend(selected)

    return balanced, stats


def unpack_embedding(blob: bytes) -> Optional[List[float]]:
    """Unpack 192-dim embedding from binary blob."""
    if not blob:
        return None
    try:
        return list(struct.unpack(f'{len(blob)//4}f', blob))
    except:
        return None


def get_script_lang_key(script: str, lang: Optional[str]) -> str:
    """Create a canonical script+language key for binning."""
    lang_part = lang.split('-')[0] if lang else 'und'
    return f"{script}:{lang_part}"


def es_retry_with_backoff(func, *args, **kwargs):
    """
    Execute an ES operation with exponential backoff retry.

    Implements resilience against ES overload by:
    1. Retrying failed requests up to ES_MAX_RETRIES times
    2. Using exponential backoff between retries
    3. Logging warnings for retries

    Returns:
        Result of func(*args, **kwargs), or None if all retries fail
    """
    last_exception = None
    backoff = ES_INITIAL_BACKOFF

    for attempt in range(ES_MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if attempt < ES_MAX_RETRIES - 1:
                logger.debug(f"ES request failed (attempt {attempt + 1}/{ES_MAX_RETRIES}): {e}")
                logger.debug(f"Backing off for {backoff:.1f}s...")
                time.sleep(backoff)
                backoff = min(backoff * ES_BACKOFF_FACTOR, ES_MAX_BACKOFF)
            else:
                logger.warning(f"ES request failed after {ES_MAX_RETRIES} attempts: {e}")

    return None


class ESKNNHelper:
    """
    Helper class for ES KNN operations with retry/throttle support.

    Features:
    - LRU-bounded embedding cache (MAX_CACHE_SIZE entries)
    - Exponential backoff retry on ES failures
    - Failure rate tracking with abort threshold
    - Batched operations via _msearch and mget
    """

    # Maximum number of embeddings to cache (each ~768 bytes for 192 floats)
    # 100K embeddings ≈ 77MB cache
    MAX_CACHE_SIZE = 100000

    def __init__(self, es: Elasticsearch, index: str = "toponyms"):
        self.es = es
        self.index = index
        self._embedding_cache: Dict[str, List[float]] = {}
        self._cache_order: List[str] = []  # Track insertion order for LRU eviction

        # Failure tracking
        self._total_requests = 0
        self._failed_requests = 0

    def _record_request(self, success: bool):
        """Record request outcome for failure rate tracking."""
        self._total_requests += 1
        if not success:
            self._failed_requests += 1

    def get_failure_rate(self) -> float:
        """Get current failure rate."""
        if self._total_requests == 0:
            return 0.0
        return self._failed_requests / self._total_requests

    def check_failure_threshold(self):
        """Check if failure rate exceeds threshold, raise if so."""
        if self._total_requests > 100:  # Only check after sufficient samples
            rate = self.get_failure_rate()
            if rate > ES_FAILURE_THRESHOLD:
                raise RuntimeError(
                    f"ES failure rate ({rate:.1%}) exceeds threshold ({ES_FAILURE_THRESHOLD:.0%}). "
                    f"Aborting to prevent data quality issues. "
                    f"({self._failed_requests}/{self._total_requests} requests failed)"
                )

    def reset_failure_tracking(self):
        """Reset failure counters (call between phases)."""
        self._total_requests = 0
        self._failed_requests = 0

    def _cache_embedding(self, toponym_id: str, embedding: List[float]):
        """Add embedding to cache with LRU eviction."""
        if toponym_id in self._embedding_cache:
            return  # Already cached

        # Evict oldest entries if cache is full
        while len(self._embedding_cache) >= self.MAX_CACHE_SIZE:
            oldest = self._cache_order.pop(0)
            self._embedding_cache.pop(oldest, None)

        self._embedding_cache[toponym_id] = embedding
        self._cache_order.append(toponym_id)

    def clear_cache(self):
        """Clear embedding cache (call between phases to free memory)."""
        self._embedding_cache.clear()
        self._cache_order.clear()

    def get_embedding(self, toponym_id: str) -> Optional[List[float]]:
        """Get embedding for a toponym from ES with retry support."""
        if toponym_id in self._embedding_cache:
            return self._embedding_cache[toponym_id]

        def _fetch():
            doc = self.es.get(index=self.index, id=toponym_id, _source=['panphon_embedding'])
            return doc['_source'].get('panphon_embedding')

        emb = es_retry_with_backoff(_fetch)
        self._record_request(emb is not None)

        if emb:
            self._cache_embedding(toponym_id, emb)
        return emb

    def find_similar_in_place(
        self,
        place_id: str,
        toponym_ids: List[str],
    ) -> List[List[str]]:
        """
        Cluster toponyms within a place using HDBSCAN density-based clustering.

        Uses PanPhon embeddings to find natural phonetic clusters without
        arbitrary similarity thresholds. HDBSCAN automatically determines
        the number of clusters based on local density structure.

        Handles edge cases:
        - 0 toponyms: returns []
        - 1 toponym: returns [[tid]]
        - 2 toponyms: uses simple cosine similarity check (HDBSCAN needs ≥3)
        - N toponyms: uses HDBSCAN with allow_single_cluster=True

        Returns:
            List of clusters, where each cluster is a list of toponym_ids
        """
        n = len(toponym_ids)

        # Edge case: no toponyms
        if n == 0:
            return []

        # Edge case: single toponym
        if n == 1:
            return [toponym_ids]

        # Get embeddings for all toponyms in this place
        embeddings = {}
        for tid in toponym_ids:
            emb = self.get_embedding(tid)
            if emb:
                embeddings[tid] = emb

        ids = list(embeddings.keys())
        n_with_emb = len(ids)

        # Edge case: 0 or 1 toponym with valid embeddings
        if n_with_emb == 0:
            return []
        if n_with_emb == 1:
            return [ids]

        # Edge case: exactly 2 toponyms - HDBSCAN needs ≥3 points
        if n_with_emb == 2:
            vec1 = np.array(embeddings[ids[0]])
            vec2 = np.array(embeddings[ids[1]])
            # Compute cosine similarity
            norm1, norm2 = np.linalg.norm(vec1), np.linalg.norm(vec2)
            if norm1 > 0 and norm2 > 0:
                cos_sim = np.dot(vec1, vec2) / (norm1 * norm2)
                if cos_sim >= PAIR_SIMILARITY_THRESHOLD:
                    return [ids]  # Similar enough - one cluster
            return [[ids[0]], [ids[1]]]  # Different clusters

        # Main case: ≥3 toponyms - use HDBSCAN

        vectors = np.array([embeddings[tid] for tid in ids])

        try:
            # Precompute cosine distance matrix (1 - cosine_similarity)
            # This is more compatible across HDBSCAN versions than metric='cosine'
            from sklearn.metrics.pairwise import cosine_distances
            distance_matrix = cosine_distances(vectors)

            # Use cluster_selection_epsilon=0.2 (cosine distance) to merge clusters
            # where members are within ~0.8 cosine similarity of each other.
            # This creates larger, more meaningful clusters for phonetically similar names.
            clusterer = hdbscan.HDBSCAN(
                min_cluster_size=2,
                min_samples=2,  # Require at least 2 points in core neighborhood (better denoising)
                metric='precomputed',  # Use precomputed distance matrix
                cluster_selection_epsilon=0.2,  # Merge clusters within cosine distance 0.2 (sim >= 0.8)
                allow_single_cluster=True  # Critical: allows all points in one cluster
            )
            labels = clusterer.fit_predict(distance_matrix)
        except Exception as e:
            logger.warning(f"HDBSCAN failed for place {place_id}: {e}, returning single cluster")
            return [ids]

        # Group by cluster label
        clusters_dict: Dict[int, List[str]] = defaultdict(list)
        noise_points = []

        for tid, label in zip(ids, labels):
            if label >= 0:
                clusters_dict[label].append(tid)
            else:
                noise_points.append(tid)

        result = list(clusters_dict.values())

        # Each noise point becomes its own singleton cluster
        for tid in noise_points:
            result.append([tid])

        return result


    def find_hard_negative(
        self,
        anchor_id: str,
        anchor_embedding: List[float],
        anchor_script: str,
        adjacency: Set[Tuple[str, str]],
        k: int = 20,
    ) -> Optional[str]:
        """
        Find a hard negative using ES KNN.

        Queries for toponyms with:
        - Same script as anchor
        - High embedding similarity (via KNN)
        - Different place (not adjacent)
        """
        try:
            # KNN query with script filter
            query = {
                "size": k,
                "knn": {
                    "field": "panphon_embedding",
                    "query_vector": anchor_embedding,
                    "k": k,
                    "num_candidates": KNN_CANDIDATES,
                    "filter": {
                        "term": {"script": anchor_script}
                    }
                },
                "_source": False
            }

            results = self.es.search(index=self.index, body=query)

            for hit in results['hits']['hits']:
                candidate_id = hit['_id']
                # Check not the anchor and not adjacent
                if candidate_id != anchor_id and (anchor_id, candidate_id) not in adjacency:
                    return candidate_id

            return None

        except Exception as e:
            logger.debug(f"KNN hard negative search failed: {e}")
            return None

    def batch_get_embeddings(self, toponym_ids: List[str]) -> Dict[str, List[float]]:
        """Get embeddings for multiple toponyms efficiently using mget with retry."""
        result = {}

        # Filter out already cached
        to_fetch = [tid for tid in toponym_ids if tid not in self._embedding_cache]

        # Add cached ones to result
        for tid in toponym_ids:
            if tid in self._embedding_cache:
                result[tid] = self._embedding_cache[tid]

        if not to_fetch:
            return result

        # Batch fetch from ES with retry
        def _mget():
            return self.es.mget(
                index=self.index,
                body={"ids": to_fetch},
                _source=['panphon_embedding']
            )

        docs = es_retry_with_backoff(_mget)
        self._record_request(docs is not None)

        if docs:
            for doc in docs.get('docs', []):
                if doc.get('found') and '_source' in doc:
                    emb = doc['_source'].get('panphon_embedding')
                    if emb:
                        self._cache_embedding(doc['_id'], emb)
                        result[doc['_id']] = emb


        return result

    def find_hard_negatives_batch(
        self,
        anchors: List[Dict],
        adjacency: Set[Tuple[str, str]],
        k: int = 20,
        stochastic: bool = True,
    ) -> List[Optional[str]]:
        """
        Find hard negatives for multiple anchors using ES _msearch.

        This reduces network round-trips by 50-100x compared to individual queries.

        Args:
            anchors: List of dicts with keys: 'anchor_id', 'embedding', 'script', optionally 'sample_idx'
            adjacency: Set of adjacent (anchor, candidate) pairs to exclude
            k: Number of candidates to retrieve per anchor
            stochastic: If True, randomly select from valid candidates (for oversampling diversity)

        Returns:
            List of hard negative toponym_ids (or None if not found), same order as anchors
        """
        if not anchors:
            return []

        # Build _msearch body
        bodies = []
        for a in anchors:
            bodies.append({"index": self.index})
            bodies.append({
                "size": k,
                "knn": {
                    "field": "panphon_embedding",
                    "query_vector": a['embedding'],
                    "k": k,
                    "num_candidates": KNN_CANDIDATES,
                    "filter": {
                        "term": {"script": a['script']}
                    }
                },
                "_source": False
            })

        def _msearch():
            return self.es.msearch(body=bodies)['responses']

        responses = es_retry_with_backoff(_msearch)
        self._record_request(responses is not None)

        if responses is None:
            return [None] * len(anchors)

        # Process responses
        results = []
        for i, response in enumerate(responses):
            anchor_id = anchors[i]['anchor_id']
            hard_neg = None

            if 'hits' in response and 'hits' in response['hits']:
                # Collect all valid candidates (not self, not adjacent)
                valid_candidates = []
                for hit in response['hits']['hits']:
                    candidate_id = hit['_id']
                    if candidate_id != anchor_id and (anchor_id, candidate_id) not in adjacency:
                        valid_candidates.append(candidate_id)

                if valid_candidates:
                    if stochastic and len(valid_candidates) > 1:
                        # Use sample_idx if provided (for oversampled pairs) to get different negatives
                        sample_idx = anchors[i].get('sample_idx', 0)
                        # Deterministic but varied selection based on anchor + sample index
                        seed = RANDOM_SEED + (zlib.crc32(anchor_id.encode('utf-8')) & 0xffffffff) + sample_idx
                        rng = random.Random(seed)
                        hard_neg = rng.choice(valid_candidates)
                    else:
                        # Take the first (most similar) candidate
                        hard_neg = valid_candidates[0]

            results.append(hard_neg)

        return results


class TrainingDataGenerator:
    """Generates training data for all phases from DuckDB + ES."""

    def __init__(
        self,
        es: Elasticsearch,
        db_path: str,
        output_dir: Path,
        scratch_dir: Path,
        training_namespaces: List[str],
        force_regenerate: bool = False,
    ):
        self.es = es
        self.db_path = db_path
        self.output_dir = output_dir
        self.scratch_dir = scratch_dir
        self.training_namespaces = training_namespaces
        self.force_regenerate = force_regenerate

        # ES KNN helper for similarity queries
        self.knn = ESKNNHelper(es, index="toponyms")

        # DuckDB connection is now optional - we read from ES directly
        # Keep db_path for reference but don't require connection
        self.conn = None
        if db_path and Path(db_path).exists():
            try:
                self.conn = duckdb.connect(db_path, read_only=True)
                logger.info(f"Connected to DuckDB at {db_path} (optional, for fallback)")
            except Exception as e:
                logger.warning(f"Could not connect to DuckDB: {e} (will use ES only)")

        # Statistics
        self.stats = {
            'phase1': {'pairs': 0, 'triplets': 0, 'by_bin': {}},
            'phase2': {'samples': 0, 'by_bin': {}},
            'phase3': {'triplets': 0, 'by_bin': {}},
        }

    def _check_phase_complete(self, phase: str) -> bool:
        """
        Check if a phase's output files already exist (checkpoint detection).

        Returns True if the phase can be skipped (outputs exist and force_regenerate is False).
        """
        # If force regeneration is enabled, always return False
        if self.force_regenerate:
            return False

        if phase == 'pairs':
            pairs_file = self.output_dir / 'pairs' / 'positive_pairs.parquet'
            return pairs_file.exists()
        elif phase == 'phase1':
            train_file = self.output_dir / 'triplets' / 'phase1' / 'train.parquet'
            val_file = self.output_dir / 'triplets' / 'phase1' / 'val.parquet'
            return train_file.exists() and val_file.exists()
        elif phase == 'phase2':
            # Phase 2 data is stored in training/split={train,val,test}/data.parquet
            train_file = self.output_dir / 'training' / 'split=train' / 'data.parquet'
            val_file = self.output_dir / 'training' / 'split=val' / 'data.parquet'
            return train_file.exists() and val_file.exists()
        elif phase == 'phase3':
            train_file = self.output_dir / 'triplets' / 'phase3' / 'train.parquet'
            val_file = self.output_dir / 'triplets' / 'phase3' / 'val.parquet'
            return train_file.exists() and val_file.exists()
        return False

    def _load_pairs_from_checkpoint(self) -> Dict[str, List[Tuple]]:
        """Load positive pairs from checkpoint Parquet file."""
        pairs_file = self.output_dir / 'pairs' / 'positive_pairs.parquet'
        logger.info(f"Loading pairs from checkpoint: {pairs_file}")

        table = pq.read_table(pairs_file)
        df = table.to_pandas()

        pairs_by_bin: Dict[str, List[Tuple]] = defaultdict(list)
        for _, row in df.iterrows():
            # We don't store similarity in the checkpoint, use 1.0 as placeholder
            pairs_by_bin[row['bin']].append((row['anchor'], row['positive'], 1.0))

        logger.info(f"Loaded {len(df):,} pairs across {len(pairs_by_bin)} bins")
        return pairs_by_bin

    def generate_all(self):
        """Generate training data for all phases with checkpoint support.

        IMPORTANT: Order matters! Phase 2 data (training/ with features) must be
        generated BEFORE Phase 1 triplets, because Phase 1 training needs to look up
        features from the training data. Similarly, Phase 3 must come last as it
        depends on ES KNN queries against the same data.

        Generation order:
        1. Positive pairs (clustering co-located toponyms)
        2. Phase 2 samples (all toponyms with features - needed by Phase 1 & 3 training)
        3. Phase 1 triplets (random negatives - IDs only, features from Phase 2)
        4. Phase 3 triplets (hard negatives via ES KNN)
        """
        logger.info("=" * 60)
        logger.info("GENERATING TRAINING DATA FOR ALL PHASES")
        logger.info("=" * 60)

        # Step 1: Generate positive pairs from co-located toponyms
        logger.info("\n" + "=" * 60)
        logger.info("STEP 1: GENERATE POSITIVE PAIRS")
        logger.info("=" * 60)

        if self._check_phase_complete('pairs'):
            logger.info("✓ Positive pairs checkpoint found, loading from disk...")
            pairs_by_bin = self._load_pairs_from_checkpoint()
        else:
            pairs_by_bin = self.generate_positive_pairs()

        # Step 2: Generate Phase 2 samples FIRST (all toponyms with embeddings)
        # CRITICAL: This must happen BEFORE Phase 1 triplets because Phase 1 training
        # needs to look up features from the training/ directory
        logger.info("\n" + "=" * 60)
        logger.info("STEP 2: GENERATE PHASE 2 SAMPLES (training data with features)")
        logger.info("=" * 60)

        if self._check_phase_complete('phase2'):
            logger.info("✓ Phase 2 checkpoint found, skipping...")
            # Count samples from existing files
            training_dir = self.output_dir / 'training'
            total_samples = 0
            for split in ['train', 'val', 'test']:
                split_file = training_dir / f'split={split}' / 'data.parquet'
                if split_file.exists():
                    total_samples += pq.read_table(split_file).num_rows
            self.stats['phase2']['samples'] = total_samples
        else:
            self.generate_phase2_samples()

        # Clear embedding cache and reset failure tracking between phases
        logger.info("Clearing embedding cache and resetting ES failure tracking...")
        self.knn.clear_cache()
        self.knn.reset_failure_tracking()

        # Step 3: Generate Phase 1 triplets (random negatives)
        # These are IDs only - Phase 1 training joins them to features from Phase 2 data
        logger.info("\n" + "=" * 60)
        logger.info("STEP 3: GENERATE PHASE 1 TRIPLETS")
        logger.info("=" * 60)

        if self._check_phase_complete('phase1'):
            logger.info("✓ Phase 1 checkpoint found, skipping...")
            # Load stats if available
            phase1_train = pq.read_table(self.output_dir / 'triplets' / 'phase1' / 'train.parquet')
            phase1_val = pq.read_table(self.output_dir / 'triplets' / 'phase1' / 'val.parquet')
            self.stats['phase1']['triplets'] = len(phase1_train) + len(phase1_val)
        else:
            self.generate_phase1_triplets(pairs_by_bin)

        # Reset failure tracking before Phase 3 (heavy ES usage)
        self.knn.reset_failure_tracking()

        # Step 4: Generate Phase 3 triplets (hard negatives from ES)
        logger.info("\n" + "=" * 60)
        logger.info("STEP 4: GENERATE PHASE 3 TRIPLETS")
        logger.info("=" * 60)

        if self._check_phase_complete('phase3'):
            logger.info("✓ Phase 3 checkpoint found, skipping...")
            # Load stats if available
            phase3_train = pq.read_table(self.output_dir / 'triplets' / 'phase3' / 'train.parquet')
            phase3_val = pq.read_table(self.output_dir / 'triplets' / 'phase3' / 'val.parquet')
            self.stats['phase3']['triplets'] = len(phase3_train) + len(phase3_val)
        else:
            self.generate_phase3_triplets(pairs_by_bin)

        # Save statistics
        stats_path = self.output_dir / 'training_stats.json'
        with open(stats_path, 'w') as f:
            json.dump(self.stats, f, indent=2)
        logger.info(f"Statistics saved to {stats_path}")

        return self.stats

    def generate_positive_pairs(self) -> Dict[str, List[Tuple]]:
        """
        Generate positive pairs from co-located toponyms using ES KNN clustering.

        For each place with multiple toponyms:
        1. Use ES KNN to find similar toponyms within the place (cosine similarity >= threshold)
        2. Build clusters using union-find on similar pairs
        3. Generate pairs only within clusters
        4. Store in bins by script+language pair

        This handles places with multiple phonetically-distinct name variants:
        - E.g., "London" (English), "Londres" (French), "Лондон" (Russian) might form
          one cluster, while "Lundúnir" (Icelandic) forms another if sufficiently different.

        Note on deduplication: We do NOT deduplicate pairs globally across places.
        The same toponym pair may appear in multiple authorities (e.g., GeoNames + Wikidata
        both have London/Londres). This is intentional:
        - Duplicate positives across different places are not harmful
        - Global deduplication couples unrelated places and uses unbounded memory
        - We dedupe within each place's cluster to avoid true duplicates

        Uses ThreadPoolExecutor to parallelize ES KNN queries across places.

        NOTE: Reads from ES toponyms index (not DuckDB) to get toponyms with panphon_embedding.

        Returns:
            Dict mapping script+lang key to list of (toponym_id_a, toponym_id_b, similarity) tuples
        """
        pairs_by_bin: Dict[str, List[Tuple]] = defaultdict(list)

        # Statistics
        cluster_stats = {
            'places_processed': 0,
            'places_with_clusters': 0,
            'total_clusters': 0,
            'singleton_clusters': 0,
            'multi_clusters': 0,  # Places with >1 cluster
            'cluster_sizes': Counter(),
            'duplicate_pairs_within_place': 0,  # Track place-local duplicates
        }

        # Query ES for toponyms with PanPhon embeddings in training namespaces
        # Group by attestations (place_id)
        logger.info("Querying ES for toponyms with PanPhon embeddings...")

        # Build ES query - scroll through all toponyms with embeddings in training namespaces
        ns_filter = [{"term": {"namespaces": ns}} for ns in self.training_namespaces]

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"exists": {"field": "panphon_embedding"}}
                    ],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            },
            "_source": ["attestations", "script", "lang"]
        }

        # Use scroll API to get all matching toponyms
        places: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        total_attestations = 0

        for hit in tqdm(scan(self.es, index="toponyms", query=query, scroll='5m', size=5000),
                       desc="Scanning ES for toponyms"):
            toponym_id = hit['_id']
            source = hit['_source']
            script = source.get('script', 'UNKNOWN')
            lang = source.get('lang', '')
            attestations = source.get('attestations', [])

            # Add this toponym to each place it appears in
            for place_id in attestations:
                places[place_id].append((toponym_id, script, lang))
                total_attestations += 1

        logger.info(f"Found {total_attestations:,} toponym attestations from ES")
        logger.info(f"Grouped into {len(places):,} places")

        # Filter to places with multiple toponyms and cap per place
        places_with_multiple = {}
        for p, t in places.items():
            if len(t) >= 2:
                if len(t) > MAX_TOPONYMS_PER_PLACE:
                    t = random.sample(t, MAX_TOPONYMS_PER_PLACE)
                places_with_multiple[p] = t

        logger.info(f"Places with ≥2 toponyms: {len(places_with_multiple):,}")

        # Helper function to process a single place (for parallel execution)
        def process_place(place_id: str, toponyms_info: List[Tuple]) -> Tuple[str, List, Dict]:
            """Process a single place and return clusters with metadata."""
            id_to_info = {t[0]: (t[1], t[2]) for t in toponyms_info}
            toponym_ids = list(id_to_info.keys())

            clusters = self.knn.find_similar_in_place(
                place_id=place_id,
                toponym_ids=toponym_ids,
            )

            return place_id, clusters, id_to_info

        # Parallel ES KNN clustering using ThreadPoolExecutor
        logger.info(f"Running parallel ES KNN clustering with {ES_PARALLEL_WORKERS} workers...")
        total_pairs = 0

        with ThreadPoolExecutor(max_workers=ES_PARALLEL_WORKERS) as executor:
            # Submit all tasks
            futures = {
                executor.submit(process_place, pid, info): pid
                for pid, info in places_with_multiple.items()
            }

            # Process results as they complete
            iterator = as_completed(futures)
            iterator = tqdm(iterator, total=len(futures), desc="Parallel ES KNN clustering")

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

                # Place-local deduplication (pairs can repeat across places, but not within)
                seen_in_place: Set[Tuple[str, str]] = set()

                for cluster in clusters:
                    cluster_stats['cluster_sizes'][len(cluster)] += 1

                    if len(cluster) < 2:
                        cluster_stats['singleton_clusters'] += 1
                        continue

                    # Generate pairs within this cluster
                    for i, id_a in enumerate(cluster):
                        for id_b in cluster[i+1:]:
                            script_a, lang_a = id_to_info[id_a]
                            script_b, lang_b = id_to_info[id_b]

                            # Place-local deduplication only
                            pair_key = tuple(sorted([id_a, id_b]))
                            if pair_key in seen_in_place:
                                cluster_stats['duplicate_pairs_within_place'] += 1
                                continue
                            seen_in_place.add(pair_key)

                            # Determine bin key (use script+lang pair)
                            key_a = get_script_lang_key(script_a, lang_a)
                            key_b = get_script_lang_key(script_b, lang_b)
                            bin_key = tuple(sorted([key_a, key_b]))
                            bin_key_str = f"{bin_key[0]}|{bin_key[1]}"

                            # Similarity was already checked by ES KNN (>= threshold)
                            pairs_by_bin[bin_key_str].append((id_a, id_b, 0.0))
                            total_pairs += 1

                # Periodically check ES failure rate
                if cluster_stats['places_processed'] % 10000 == 0:
                    self.knn.check_failure_threshold()

        # Final ES failure rate check and logging
        failure_rate = self.knn.get_failure_rate()
        logger.info(f"ES failure rate: {failure_rate:.2%} ({self.knn._failed_requests}/{self.knn._total_requests} requests)")
        self.knn.check_failure_threshold()

        logger.info(f"Generated {total_pairs:,} positive pairs")
        logger.info(f"Distributed across {len(pairs_by_bin)} script+language bins")

        # Log clustering statistics
        logger.info("Clustering statistics:")
        logger.info(f"  Places processed: {cluster_stats['places_processed']:,}")
        logger.info(f"  Places with ≥2 toponyms: {cluster_stats['places_with_clusters']:,}")
        logger.info(f"  Total clusters formed: {cluster_stats['total_clusters']:,}")
        logger.info(f"  Places with multiple clusters: {cluster_stats['multi_clusters']:,}")
        logger.info(f"  Singleton clusters (no pairs): {cluster_stats['singleton_clusters']:,}")
        if cluster_stats['duplicate_pairs_within_place'] > 0:
            logger.info(f"  Duplicate pairs within places (skipped): {cluster_stats['duplicate_pairs_within_place']:,}")
        logger.info(f"  Cluster size distribution:")
        for size, count in sorted(cluster_stats['cluster_sizes'].items())[:10]:
            logger.info(f"    Size {size}: {count:,} clusters")

        # Log bin distribution
        bin_sizes = [(k, len(v)) for k, v in pairs_by_bin.items()]
        bin_sizes.sort(key=lambda x: -x[1])
        logger.info("Top 10 bins:")
        for bin_key, count in bin_sizes[:10]:
            logger.info(f"  {bin_key}: {count:,}")

        self.stats['phase1']['pairs'] = total_pairs
        self.stats['phase1']['by_bin'] = {k: len(v) for k, v in pairs_by_bin.items()}
        self.stats['phase1']['clustering'] = {
            'places_with_multiple_clusters': cluster_stats['multi_clusters'],
            'total_clusters': cluster_stats['total_clusters'],
            'singleton_clusters': cluster_stats['singleton_clusters'],
        }

        # Save pairs to Parquet
        pairs_dir = self.output_dir / 'pairs'
        pairs_dir.mkdir(exist_ok=True)

        all_pairs = []
        for bin_key, pairs in pairs_by_bin.items():
            for id_a, id_b, sim in pairs:
                all_pairs.append({
                    'anchor': id_a,
                    'positive': id_b,
                    'bin': bin_key,
                })

        if all_pairs:
            table = pa.Table.from_pylist(all_pairs)
            pq.write_table(table, pairs_dir / 'positive_pairs.parquet')
            logger.info(f"Saved pairs to {pairs_dir / 'positive_pairs.parquet'}")

        return pairs_by_bin

    def generate_phase1_triplets(self, pairs_by_bin: Dict[str, List[Tuple]]):
        """
        Generate Phase 1 triplets with random negatives.

        For each positive pair (anchor, positive), sample a random negative
        that is NOT in the same place (not adjacent).

        IMPORTANT: This generates SELF-CONTAINED triplets that include all data
        needed for training (panphon features). Training does NOT need to look up
        features from any other data source.

        Uses unified bin-balancing algorithm:
        - Caps over-represented script+language bins
        - Oversamples under-represented bins (up to MAX_OVERSAMPLE_FACTOR)
        - Drops bins below MIN_BIN_SIZE
        """
        logger.info("=" * 60)
        logger.info("PHASE 1: Generating triplets with random negatives")
        logger.info("=" * 60)

        logger.info("Building adjacency set...")
        adjacency: Set[Tuple[str, str]] = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                adjacency.add((id_a, id_b))
                adjacency.add((id_b, id_a))

        logger.info(f"Adjacency set has {len(adjacency):,} edges")

        # Pre-load toponym info AND features for all anchors/positives
        # This is CRITICAL - Phase 1 triplets must be self-contained
        logger.info("Pre-loading toponym info and features for all anchors/positives...")
        all_anchor_ids = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                all_anchor_ids.add(id_a)
                all_anchor_ids.add(id_b)

        # Batch query for all anchor info AND features from ES using mget
        # We need: script, lang, panphon_embedding (the 192-dim features)
        toponym_data_map = {}  # toponym_id -> {script, lang, features, feature_length}
        anchor_list = list(all_anchor_ids)
        batch_size = 5000
        logger.info(f"Loading toponym data from ES for {len(anchor_list):,} toponyms...")

        for i in range(0, len(anchor_list), batch_size):
            batch = anchor_list[i:i+batch_size]
            docs = self.es.mget(index="toponyms", body={"ids": batch},
                               _source=['script', 'lang', 'panphon_embedding'])
            for doc in docs.get('docs', []):
                if doc.get('found') and '_source' in doc:
                    toponym_id = doc['_id']
                    source = doc['_source']
                    embedding = source.get('panphon_embedding')

                    # Only include if has valid embedding
                    if embedding and len(embedding) > 0:
                        toponym_data_map[toponym_id] = {
                            'script': source.get('script', 'UNKNOWN'),
                            'lang': source.get('lang', ''),
                            'features': embedding,
                            'feature_length': len(embedding) // 24,  # 24 = panphon feature dim
                        }

            if (i + batch_size) % 50000 < batch_size:
                logger.info(f"  Loaded {min(i + batch_size, len(anchor_list)):,} / {len(anchor_list):,}...")

        logger.info(f"Loaded data for {len(toponym_data_map):,} unique toponyms with valid features")

        # Get all toponym IDs for negative sampling, grouped by script
        # This enables script-aware negative sampling to avoid teaching easy shortcuts
        logger.info("Loading toponym IDs for negative sampling (grouped by script) from ES...")

        all_ids = []
        ids_by_script: Dict[str, List[str]] = defaultdict(list)

        # Use ES scan to get all toponym IDs with panphon_embedding
        neg_query = {
            "query": {
                "exists": {"field": "panphon_embedding"}
            },
            "_source": ["script"]
        }

        for hit in tqdm(scan(self.es, index="toponyms", query=neg_query, scroll='5m', size=10000),
                       desc="Loading negative candidates from ES"):
            toponym_id = hit['_id']
            script = hit['_source'].get('script', 'UNKNOWN')
            all_ids.append(toponym_id)
            if script:
                ids_by_script[script].append(toponym_id)

        logger.info(f"Loaded {len(all_ids):,} candidate negatives across {len(ids_by_script)} scripts")
        logger.info(f"Phase 1 negative sampling: {PHASE1_SAME_SCRIPT_NEGATIVE_RATIO:.0%} same-script, {1-PHASE1_SAME_SCRIPT_NEGATIVE_RATIO:.0%} any-script")

        # Apply unified bin-balancing to pairs
        logger.info(f"Applying bin-balancing (target={TARGET_SAMPLES_PER_BIN}, min={MIN_BIN_SIZE}, max_oversample={MAX_OVERSAMPLE_FACTOR}x)...")

        balanced_pairs, balance_stats = apply_bin_balancing(
            pairs_by_bin,
            target_per_bin=TARGET_SAMPLES_PER_BIN,
            min_bin_size=MIN_BIN_SIZE,
            max_oversample=MAX_OVERSAMPLE_FACTOR,
        )

        # Log balancing results
        logger.info(f"Bin balancing results:")
        logger.info(f"  Total bins: {balance_stats['bins_total']}")
        logger.info(f"  Bins dropped (< {MIN_BIN_SIZE}): {balance_stats['bins_dropped']}")
        logger.info(f"  Bins capped (> {TARGET_SAMPLES_PER_BIN}): {balance_stats['bins_capped']}")
        logger.info(f"  Bins oversampled: {balance_stats['bins_oversampled']}")
        logger.info(f"  Bins unchanged: {balance_stats['bins_unchanged']}")

        if balance_stats['dropped_bins']:
            logger.info(f"  Dropped bins (top 10):")
            for bin_key, size in balance_stats['dropped_bins'][:10]:
                logger.info(f"    {bin_key}: {size} samples")

        logger.info(f"Balanced pairs: {len(balanced_pairs):,}")

        # Generate triplets from balanced pairs
        # CRITICAL: We must include the actual features in each triplet so training is self-contained
        # We already have features for anchors/positives in toponym_data_map
        # For negatives, we need to batch-fetch features as we generate triplets

        logger.info("Generating triplets (with stochastic negative sampling)...")

        # First pass: select negatives and collect all negative IDs we need
        logger.info("  Pass 1: Selecting negatives...")
        triplet_specs = []  # List of (anchor, positive, negative, bin_key) tuples
        negative_ids_needed = set()

        for triplet_idx, pair in enumerate(balanced_pairs):
            anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)

            # Skip if anchor or positive don't have features
            if anchor not in toponym_data_map or positive not in toponym_data_map:
                continue

            anchor_data = toponym_data_map[anchor]
            script = anchor_data['script']
            lang = anchor_data['lang']

            # Use seeded RNG for reproducible negative sampling
            seed = RANDOM_SEED + (zlib.crc32(anchor.encode('utf-8')) & 0xffffffff) + triplet_idx
            rng = random.Random(seed)

            # Find a negative that's not adjacent
            for _ in range(10):  # Max attempts
                use_same_script = rng.random() < PHASE1_SAME_SCRIPT_NEGATIVE_RATIO

                if use_same_script and script and script in ids_by_script and len(ids_by_script[script]) > 0:
                    negative = rng.choice(ids_by_script[script])
                else:
                    negative = rng.choice(all_ids)

                if (anchor, negative) not in adjacency and (positive, negative) not in adjacency:
                    bin_key = get_script_lang_key(script, lang)
                    triplet_specs.append((anchor, positive, negative, bin_key))
                    # Track negative for feature fetching (unless already in toponym_data_map)
                    if negative not in toponym_data_map:
                        negative_ids_needed.add(negative)
                    break

        logger.info(f"  Selected {len(triplet_specs):,} triplets, need features for {len(negative_ids_needed):,} negatives")

        # Batch fetch features for negatives that aren't already loaded
        if negative_ids_needed:
            logger.info("  Pass 2: Loading features for negatives...")
            neg_list = list(negative_ids_needed)

            for i in range(0, len(neg_list), batch_size):
                batch = neg_list[i:i+batch_size]
                docs = self.es.mget(index="toponyms", body={"ids": batch},
                                   _source=['script', 'lang', 'panphon_embedding'])
                for doc in docs.get('docs', []):
                    if doc.get('found') and '_source' in doc:
                        toponym_id = doc['_id']
                        source = doc['_source']
                        embedding = source.get('panphon_embedding')

                        if embedding and len(embedding) > 0:
                            toponym_data_map[toponym_id] = {
                                'script': source.get('script', 'UNKNOWN'),
                                'lang': source.get('lang', ''),
                                'features': embedding,
                                'feature_length': len(embedding) // 24,
                            }

                if (i + batch_size) % 100000 < batch_size:
                    logger.info(f"    Loaded {min(i + batch_size, len(neg_list)):,} / {len(neg_list):,}...")

        # Build final triplets with embedded features
        logger.info("  Pass 3: Building triplets with features...")
        triplets = []
        skipped_missing_features = 0

        for anchor, positive, negative, bin_key in triplet_specs:
            # Verify all three have features
            if anchor not in toponym_data_map or \
               positive not in toponym_data_map or \
               negative not in toponym_data_map:
                skipped_missing_features += 1
                continue

            anchor_data = toponym_data_map[anchor]
            positive_data = toponym_data_map[positive]
            negative_data = toponym_data_map[negative]

            triplets.append({
                'anchor_id': anchor,
                'positive_id': positive,
                'negative_id': negative,
                'bin': bin_key,
                # Embed the actual features (self-contained triplet)
                'anchor_features': anchor_data['features'],
                'anchor_feature_length': anchor_data['feature_length'],
                'positive_features': positive_data['features'],
                'positive_feature_length': positive_data['feature_length'],
                'negative_features': negative_data['features'],
                'negative_feature_length': negative_data['feature_length'],
            })

        if skipped_missing_features > 0:
            logger.info(f"  Skipped {skipped_missing_features:,} triplets due to missing features")

        logger.info(f"Generated {len(triplets):,} Phase 1 triplets (self-contained with features)")
        self.stats['phase1']['triplets'] = len(triplets)
        self.stats['phase1']['balance_stats'] = balance_stats

        # Save to Parquet in directory structure expected by training
        triplets_dir = self.output_dir / 'triplets' / 'phase1'
        triplets_dir.mkdir(parents=True, exist_ok=True)

        if triplets:
            # Use deterministic split based on anchor_id hash (reproducible across runs)
            # crc32 % 10: 0 = val, 1-9 = train (90/10 split)
            train_triplets = []
            val_triplets = []

            for triplet in triplets:
                hash_val = (zlib.crc32(triplet['anchor_id'].encode('utf-8')) & 0xffffffff) % 10
                if hash_val == 0:
                    val_triplets.append(triplet)
                else:
                    train_triplets.append(triplet)

            # Shuffle within each split (with fixed seed for reproducibility)
            rng = random.Random(RANDOM_SEED)
            rng.shuffle(train_triplets)
            rng.shuffle(val_triplets)

            train_table = pa.Table.from_pylist(train_triplets)
            val_table = pa.Table.from_pylist(val_triplets)

            pq.write_table(train_table, triplets_dir / 'train.parquet')
            pq.write_table(val_table, triplets_dir / 'val.parquet')

            logger.info(f"Saved {len(train_triplets):,} train, {len(val_triplets):,} val triplets (deterministic split)")

    def generate_phase2_samples(self):
        """
        Generate Phase 2 samples: balanced toponyms with PanPhon embeddings.

        Phase 2 trains the Student to mimic Teacher outputs. We use the unified
        bin-balancing algorithm to ensure balanced representation across
        script+language pairs.

        Uses unified bin-balancing algorithm:
        - Groups samples by script+language bin
        - Caps over-represented bins (e.g., LATIN:en)
        - Oversamples under-represented bins (up to MAX_OVERSAMPLE_FACTOR)
        - Drops bins below MIN_BIN_SIZE

        Exports data in the format expected by Phase2Dataset:
        - toponym_id, name, script, lang
        - char_ids (list of int)
        - features (list of float), feature_length (int)
        - split ('train'/'val'/'test'), epitran_supported (bool)

        NOTE: Reads from ES toponyms index (not DuckDB) to get toponyms with panphon_embedding.
        """
        logger.info("=" * 60)
        logger.info("PHASE 2: GENERATING BALANCED TRAINING SAMPLES (from ES)")
        logger.info("=" * 60)

        # Create output directory (training/ is expected by data_loading.py)
        training_dir = self.output_dir / 'training'
        training_dir.mkdir(exist_ok=True)

        # Load char vocabulary for char_ids conversion
        import json
        vocab_path = self.output_dir / 'vocab' / 'char_vocab.json'
        if not vocab_path.exists():
            logger.error(f"Char vocabulary not found at {vocab_path}")
            return

        with open(vocab_path) as f:
            char_vocab = json.load(f)
        char_to_id = char_vocab.get('char_to_id', {})
        unk_id = char_to_id.get('<UNK>', 1)
        logger.info(f"Loaded char vocabulary with {len(char_to_id)} characters")

        # Query ES for coverage statistics
        ns_filter = [{"term": {"namespaces": ns}} for ns in self.training_namespaces]

        # Count total in training namespaces
        total_query = {
            "query": {
                "bool": {
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            }
        }
        total_count = self.es.count(index="toponyms", body=total_query)['count']

        # Count with panphon_embedding
        with_emb_query = {
            "query": {
                "bool": {
                    "must": [
                        {"exists": {"field": "panphon_embedding"}}
                    ],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            }
        }
        with_features = self.es.count(index="toponyms", body=with_emb_query)['count']

        without_features = total_count - with_features
        coverage_pct = (with_features / total_count * 100) if total_count > 0 else 0

        logger.info(f"Found {with_features:,} toponyms with PanPhon embeddings (from ES)")
        logger.info(f"  Total in training namespaces: {total_count:,}")
        logger.info(f"  Without embeddings (excluded): {without_features:,}")
        logger.info(f"  PanPhon coverage: {coverage_pct:.1f}%")

        # ============================================================
        # MEMORY-EFFICIENT TWO-PASS APPROACH (using ES)
        # Pass 1: Count samples per bin using ES aggregation
        # Pass 2: Stream from ES and sample based on bin quotas
        # ============================================================

        # Pass 1: Count bin sizes using ES aggregation
        logger.info("Pass 1: Counting samples per script+language bin (from ES)...")
        bin_counts: Counter = Counter()

        # Use ES composite aggregation to get script+lang counts
        agg_query = {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"exists": {"field": "panphon_embedding"}}
                    ],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            },
            "aggs": {
                "script_lang": {
                    "composite": {
                        "size": 10000,  # Large enough to get all combinations
                        "sources": [
                            {"script": {"terms": {"field": "script"}}},
                            {"lang": {"terms": {"field": "lang"}}}
                        ]
                    }
                }
            }
        }

        # Use composite aggregation to handle pagination
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

        logger.info(f"Found {len(bin_counts)} script+language bins")

        # Log top bins
        logger.info("Top 10 bins (before balancing):")
        for bin_key, count in bin_counts.most_common(10):
            logger.info(f"  {bin_key}: {count:,}")

        # Calculate sampling probabilities for each bin
        logger.info(f"Calculating sampling quotas (target={TARGET_SAMPLES_PER_BIN}, min={MIN_BIN_SIZE}, max_oversample={MAX_OVERSAMPLE_FACTOR}x)...")

        bin_quotas = {}  # bin_key -> (target_count, sampling_prob)
        dropped_bins = []
        stats = {'bins_total': len(bin_counts), 'bins_dropped': 0, 'bins_capped': 0,
                 'bins_oversampled': 0, 'bins_unchanged': 0}

        for bin_key, count in bin_counts.items():
            if count < MIN_BIN_SIZE:
                dropped_bins.append((bin_key, count))
                stats['bins_dropped'] += 1
                continue

            if count >= TARGET_SAMPLES_PER_BIN:
                # Cap: sample TARGET_SAMPLES_PER_BIN from count
                target = TARGET_SAMPLES_PER_BIN
                prob = TARGET_SAMPLES_PER_BIN / count
                stats['bins_capped'] += 1
            else:
                # Oversample up to MAX_OVERSAMPLE_FACTOR
                max_target = min(TARGET_SAMPLES_PER_BIN, count * MAX_OVERSAMPLE_FACTOR)
                if max_target > count:
                    target = max_target
                    prob = max_target / count  # > 1.0 means oversample
                    stats['bins_oversampled'] += 1
                else:
                    target = count
                    prob = 1.0
                    stats['bins_unchanged'] += 1

            bin_quotas[bin_key] = (target, prob)

        logger.info(f"Bin balancing plan:")
        logger.info(f"  Bins dropped (< {MIN_BIN_SIZE}): {stats['bins_dropped']}")
        logger.info(f"  Bins capped (> {TARGET_SAMPLES_PER_BIN}): {stats['bins_capped']}")
        logger.info(f"  Bins oversampled: {stats['bins_oversampled']}")
        logger.info(f"  Bins unchanged: {stats['bins_unchanged']}")

        if dropped_bins:
            logger.info(f"  Dropped bins:")
            for bin_key, size in dropped_bins[:20]:
                logger.info(f"    {bin_key}: {size} samples")

        # Create split directories
        for split_name in ['train', 'val', 'test']:
            split_dir = training_dir / f'split={split_name}'
            split_dir.mkdir(exist_ok=True)

        # Set up incremental Parquet writers (one per split)
        # This writes data in batches without holding everything in memory
        writers = {}
        write_buffers = {'train': [], 'val': [], 'test': []}
        WRITE_BATCH_SIZE = 50000  # Write to disk every 50K samples
        schema = None  # Will be set from first sample

        # Track samples per bin and per split (counts only, not data)
        bin_sample_counts: Counter = Counter()
        split_counts = {'train': 0, 'val': 0, 'test': 0}
        processed = 0

        def flush_buffer(split_name: str):
            """Write buffered samples to Parquet and clear buffer."""
            nonlocal schema, writers
            buffer = write_buffers[split_name]
            if not buffer:
                return

            split_dir = training_dir / f'split={split_name}'

            table = pa.Table.from_pylist(buffer)
            if schema is None:
                schema = table.schema

            # Use append mode if file exists
            parquet_path = split_dir / 'data.parquet'
            if split_name not in writers:
                writers[split_name] = pq.ParquetWriter(str(parquet_path), schema)

            writers[split_name].write_table(table)

            write_buffers[split_name] = []

        # Pass 2: Stream from ES using scroll API
        logger.info("Pass 2: Streaming from ES, sampling, and writing incrementally to Parquet...")

        es_query = {
            "query": {
                "bool": {
                    "must": [
                        {"exists": {"field": "panphon_embedding"}}
                    ],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            },
            "_source": ["name", "script", "lang", "ipa", "panphon_embedding"]
        }

        for hit in tqdm(scan(self.es, index="toponyms", query=es_query, scroll='5m', size=5000),
                       total=with_features, desc="Streaming from ES"):
            toponym_id = hit['_id']
            source = hit['_source']
            name = source.get('name', '')
            script = source.get('script', 'UNKNOWN')
            lang = source.get('lang', '')
            panphon_features = source.get('panphon_embedding', [])

            bin_key = get_script_lang_key(script, lang)

            # Skip dropped bins
            if bin_key not in bin_quotas:
                continue

            target, prob = bin_quotas[bin_key]

            # Reservoir sampling with oversampling support
            # For prob > 1.0 (oversampling), we may include the same item multiple times
            # For prob < 1.0 (capping), we sample with that probability

            # Use seeded RNG for reproducibility
            rng = random.Random(RANDOM_SEED + (zlib.crc32(toponym_id.encode('utf-8')) & 0xffffffff))

            if prob >= 1.0:
                # Oversampling: include at least once, maybe more
                num_copies = int(prob)
                if rng.random() < (prob - num_copies):
                    num_copies += 1
            else:
                # Capping: include with probability
                num_copies = 1 if rng.random() < prob else 0

            if num_copies == 0:
                continue

            # Features are already a list from ES (not binary blob)
            features = panphon_features
            if not features:
                continue

            # Convert name to char_ids
            char_ids = [char_to_id.get(c, unk_id) for c in name]

            # Determine split using stable hash (reproducible across runs)
            hash_val = (zlib.crc32(toponym_id.encode('utf-8')) & 0xffffffff) % 10
            if hash_val == 0:
                split = 'test'
            elif hash_val == 1:
                split = 'val'
            else:
                split = 'train'

            # Build sample dict
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

            # Add copies to buffer (for oversampling)
            for _ in range(num_copies):
                write_buffers[split].append(sample.copy())
                bin_sample_counts[bin_key] += 1
                split_counts[split] += 1

            # Flush buffers when they get large
            for split_name in ['train', 'val', 'test']:
                if len(write_buffers[split_name]) >= WRITE_BATCH_SIZE:
                    flush_buffer(split_name)

            processed += 1
            if processed % 500000 == 0:
                total_sampled = sum(split_counts.values())
                logger.info(f"  Processed {processed:,} toponyms, sampled {total_sampled:,}...")

        # Flush any remaining samples in buffers
        for split_name in ['train', 'val', 'test']:
            flush_buffer(split_name)

        # Close all writers
        for writer in writers.values():
            writer.close()

        # Update stats
        total_samples = sum(split_counts.values())
        self.stats['phase2']['samples'] = total_samples
        self.stats['phase2']['balance_stats'] = stats
        self.stats['phase2']['by_split'] = dict(split_counts)

        logger.info(f"Sampled {total_samples:,} total samples (incremental write)")
        logger.info(f"Split distribution:")
        for split_name, count in split_counts.items():
            logger.info(f"  {split_name}: {count:,}")

        logger.info(f"Phase 2 export complete: {total_samples:,} total samples")


    def generate_phase3_triplets(self, pairs_by_bin: Dict[str, List[Tuple]]):
        """
        Generate Phase 3 triplets with hard negatives from ES KNN.

        Hard negatives are toponyms that:
        - Have the same script as the anchor
        - Are phonetically similar (high PanPhon cosine similarity via KNN)
        - But refer to DIFFERENT places (not in adjacency set)

        This teaches the model to discriminate between similar-sounding
        but geographically distinct names.

        Uses unified bin-balancing algorithm and batched _msearch for efficiency.
        """
        logger.info("=" * 60)
        logger.info("PHASE 3: GENERATING TRIPLETS WITH HARD NEGATIVES")
        logger.info("=" * 60)

        logger.info("Building adjacency set for Phase 3...")
        adjacency: Set[Tuple[str, str]] = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                adjacency.add((id_a, id_b))
                adjacency.add((id_b, id_a))

        logger.info(f"Adjacency set has {len(adjacency):,} edges")

        # Apply unified bin-balancing to pairs FIRST (before generating triplets)
        logger.info(f"Applying bin-balancing to pairs (target={TARGET_SAMPLES_PER_BIN}, min={MIN_BIN_SIZE}, max_oversample={MAX_OVERSAMPLE_FACTOR}x)...")

        balanced_pairs, balance_stats = apply_bin_balancing(
            pairs_by_bin,
            target_per_bin=TARGET_SAMPLES_PER_BIN,
            min_bin_size=MIN_BIN_SIZE,
            max_oversample=MAX_OVERSAMPLE_FACTOR,
        )

        # Log balancing results
        logger.info(f"Bin balancing results:")
        logger.info(f"  Total bins: {balance_stats['bins_total']}")
        logger.info(f"  Bins dropped (< {MIN_BIN_SIZE}): {balance_stats['bins_dropped']}")
        logger.info(f"  Bins capped (> {TARGET_SAMPLES_PER_BIN}): {balance_stats['bins_capped']}")
        logger.info(f"  Bins oversampled: {balance_stats['bins_oversampled']}")
        logger.info(f"  Bins unchanged: {balance_stats['bins_unchanged']}")

        logger.info(f"Balanced pairs for hard negative mining: {len(balanced_pairs):,}")

        # Collect unique anchor IDs from balanced pairs
        logger.info("Collecting unique anchor IDs from balanced pairs...")
        unique_anchors = set()
        for pair in balanced_pairs:
            anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)
            unique_anchors.add(anchor)
            unique_anchors.add(positive)

        logger.info(f"Found {len(unique_anchors):,} unique toponyms in balanced pairs")

        # Load anchor info ONLY for toponyms we need from ES (not entire corpus!)
        logger.info("Loading anchor info for needed toponyms only (from ES)...")
        anchor_info = {}  # toponym_id -> (script, lang)

        anchor_list = list(unique_anchors)
        batch_size = 5000
        for i in range(0, len(anchor_list), batch_size):
            batch = anchor_list[i:i+batch_size]
            docs = self.es.mget(index="toponyms", body={"ids": batch}, _source=['script', 'lang'])
            for doc in docs.get('docs', []):
                if doc.get('found') and '_source' in doc:
                    toponym_id = doc['_id']
                    source = doc['_source']
                    anchor_info[toponym_id] = (source.get('script', 'UNKNOWN'), source.get('lang', ''))

            if (i + batch_size) % 50000 < batch_size:
                logger.info(f"  Loaded {min(i + batch_size, len(anchor_list)):,} / {len(anchor_list):,}...")

        logger.info(f"Loaded info for {len(anchor_info):,} toponyms")

        # Use balanced pairs (already capped/oversampled appropriately)
        all_pairs_to_process = []
        for pair in balanced_pairs:
            anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)
            if anchor in anchor_info:
                script, lang = anchor_info[anchor]
                bin_key = get_script_lang_key(script, lang)
                all_pairs_to_process.append((anchor, positive, bin_key))

        logger.info(f"Processing {len(all_pairs_to_process):,} balanced pairs for hard negatives...")

        # Batch fetch embeddings from ES (using mget)
        all_anchors = list(set(p[0] for p in all_pairs_to_process))
        logger.info(f"Pre-fetching embeddings for {len(all_anchors):,} anchors...")

        # Fetch in batches to avoid memory issues
        anchor_embeddings = {}
        for i in range(0, len(all_anchors), 5000):
            batch = all_anchors[i:i+5000]
            batch_embs = self.knn.batch_get_embeddings(batch)
            anchor_embeddings.update(batch_embs)
            if (i + 5000) % 50000 < 5000:
                logger.info(f"  Fetched {min(i + 5000, len(all_anchors)):,} / {len(all_anchors):,} embeddings")

        logger.info(f"Fetched {len(anchor_embeddings):,} embeddings total")

        # Process in batches using _msearch for hard negative mining
        # STOCHASTIC OVERSAMPLING: Track sample_idx so oversampled pairs get different hard negatives
        triplets = []
        failed_lookups = 0

        # Build batches for _msearch
        batches = []
        current_batch = []

        for sample_idx, (anchor, positive, bin_key) in enumerate(all_pairs_to_process):
            if anchor not in anchor_embeddings:
                failed_lookups += 1
                continue

            script, lang = anchor_info[anchor]
            current_batch.append({
                'anchor_id': anchor,
                'positive_id': positive,
                'embedding': anchor_embeddings[anchor],
                'script': script,
                'bin': bin_key,
                'sample_idx': sample_idx,  # Enables stochastic hard negative selection
            })

            if len(current_batch) >= MSEARCH_BATCH_SIZE:
                batches.append(current_batch)
                current_batch = []

        if current_batch:
            batches.append(current_batch)

        logger.info(f"Processing {len(batches)} batches of {MSEARCH_BATCH_SIZE} queries each...")

        # Process batches with progress bar
        iterator = batches
        iterator = tqdm(iterator, desc="Batched hard negative mining (_msearch)")

        batches_processed = 0
        for batch in iterator:
            # Use batched _msearch
            hard_negs = self.knn.find_hard_negatives_batch(
                anchors=batch,
                adjacency=adjacency,
                k=20,
            )

            # Create triplets from results
            for item, hard_neg in zip(batch, hard_negs):
                if hard_neg:
                    triplets.append({
                        'anchor_id': item['anchor_id'],
                        'positive_id': item['positive_id'],
                        'negative_id': hard_neg,
                        'negative_type': 'hard',  # Required by training code
                        'bin': item['bin'],
                    })

            # Periodically check ES failure rate
            batches_processed += 1
            if batches_processed % 100 == 0:
                self.knn.check_failure_threshold()

        # Final ES failure rate check and logging for Phase 3
        failure_rate = self.knn.get_failure_rate()
        logger.info(f"Phase 3 ES failure rate: {failure_rate:.2%} ({self.knn._failed_requests}/{self.knn._total_requests} requests)")
        self.knn.check_failure_threshold()

        if failed_lookups > 0:
            logger.warning(f"Failed to find embedding for {failed_lookups:,} anchors")

        logger.info(f"Generated {len(triplets):,} Phase 3 triplets")
        self.stats['phase3']['triplets'] = len(triplets)
        self.stats['phase3']['balance_stats'] = balance_stats

        # Save to Parquet in directory structure expected by training
        triplets_dir = self.output_dir / 'triplets' / 'phase3'
        triplets_dir.mkdir(parents=True, exist_ok=True)

        if triplets:
            # Use deterministic split based on anchor_id hash (reproducible across runs)
            # crc32 % 10: 0 = val, 1-9 = train (90/10 split)
            train_triplets = []
            val_triplets = []

            for triplet in triplets:
                hash_val = (zlib.crc32(triplet['anchor_id'].encode('utf-8')) & 0xffffffff) % 10
                if hash_val == 0:
                    val_triplets.append(triplet)
                else:
                    train_triplets.append(triplet)

            # Shuffle within each split (with fixed seed for reproducibility)
            rng = random.Random(RANDOM_SEED)
            rng.shuffle(train_triplets)
            rng.shuffle(val_triplets)

            train_table = pa.Table.from_pylist(train_triplets)
            val_table = pa.Table.from_pylist(val_triplets)

            pq.write_table(train_table, triplets_dir / 'train.parquet')
            pq.write_table(val_table, triplets_dir / 'val.parquet')

            logger.info(f"Saved {len(train_triplets):,} train, {len(val_triplets):,} val triplets (deterministic split)")



def main():
    parser = argparse.ArgumentParser(
        description='Generate training data for Symphonym v4'
    )
    parser.add_argument('--es-host', default='http://localhost:9200',
                        help='Elasticsearch host URL')
    parser.add_argument('--db-path', default=None,
                        help='Path to DuckDB database (optional, for fallback/reference)')
    parser.add_argument('--output-dir', required=True,
                        help='Output directory for training data')
    parser.add_argument('--scratch-dir', default='/tmp',
                        help='Scratch directory for temporary files')
    parser.add_argument('--training-namespaces', nargs='+',
                        default=['gn', 'wd', 'tgn'],
                        help='Namespaces to include in training')
    parser.add_argument('--force', action='store_true',
                        help='Force regeneration, ignoring existing checkpoints')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from checkpoints (default behavior, explicit for clarity)')

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scratch_dir = Path(args.scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # Connect to ES
    es = Elasticsearch(args.es_host)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    logger.info(f"Connected to Elasticsearch at {args.es_host}")

    # Checkpoint mode
    if args.force:
        logger.info("Mode: FORCE (ignoring checkpoints, regenerating all data)")
    else:
        logger.info("Mode: RESUME (will skip phases with existing checkpoints)")

    # Generate training data
    generator = TrainingDataGenerator(
        es=es,
        db_path=args.db_path,
        output_dir=output_dir,
        scratch_dir=scratch_dir,
        training_namespaces=args.training_namespaces,
        force_regenerate=args.force,
    )

    stats = generator.generate_all()

    logger.info("\n" + "=" * 60)
    logger.info("TRAINING DATA GENERATION COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Phase 1: {stats['phase1']['triplets']:,} triplets")
    logger.info(f"Phase 2: {stats['phase2']['samples']:,} samples")
    logger.info(f"Phase 3: {stats['phase3']['triplets']:,} triplets")


if __name__ == '__main__':
    main()

