#!/usr/bin/env python3
"""
Constants and utility functions for training data generation.

Part of phonetics.extraction package.
"""

import logging
import random
import struct
import time
import zlib
from collections import Counter
from typing import Dict, List, Optional, Tuple

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.getLogger("elastic_transport").setLevel(logging.WARNING)
logging.getLogger("elasticsearch").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ============================================================================
# TRAINING DATA CONSTANTS
# ============================================================================
TRAINING_NAMESPACES = ['gn', 'wd', 'tgn']

# Fallback threshold for edge cases (n=2 toponyms where HDBSCAN can't work)
PAIR_SIMILARITY_THRESHOLD = 0.5

MAX_TOPONYMS_PER_PLACE = 50  # Cap to prevent combinatorial explosion
KNN_CANDIDATES = 100
ES_BATCH_SIZE = 500
ES_PARALLEL_WORKERS = 8
MSEARCH_BATCH_SIZE = 500

# ============================================================================
# ES RESILIENCE PARAMETERS
# ============================================================================
ES_MAX_RETRIES = 5
ES_INITIAL_BACKOFF = 1.0
ES_MAX_BACKOFF = 60.0
ES_BACKOFF_FACTOR = 2.0
ES_FAILURE_THRESHOLD = 0.1

# ============================================================================
# PHASE 1 NEGATIVE SAMPLING
# ============================================================================
PHASE1_SAME_SCRIPT_NEGATIVE_RATIO = 0.8

# ============================================================================
# REPRODUCIBILITY
# ============================================================================
RANDOM_SEED = 42

# ============================================================================
# BIN-BALANCING PARAMETERS
# ============================================================================
TARGET_SAMPLES_PER_BIN = 50000
MIN_BIN_SIZE = 1000
MAX_OVERSAMPLE_FACTOR = 5
VAL_RATIO = 0.1
TEST_RATIO = 0.1

# ============================================================================
# MEMORY-EFFICIENT STREAMING PARAMETERS
# ============================================================================
PARQUET_BATCH_SIZE = 50000  # Rows per batch for streaming writes


def es_retry_with_backoff(func, *args, **kwargs):
    """Execute an ES operation with exponential backoff retry."""
    last_exception = None
    backoff = ES_INITIAL_BACKOFF

    for attempt in range(ES_MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if attempt < ES_MAX_RETRIES - 1:
                logger.debug(f"ES request failed (attempt {attempt + 1}/{ES_MAX_RETRIES}): {e}")
                time.sleep(backoff)
                backoff = min(backoff * ES_BACKOFF_FACTOR, ES_MAX_BACKOFF)
            else:
                logger.warning(f"ES request failed after {ES_MAX_RETRIES} attempts: {e}")

    return None


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


def apply_bin_balancing(
    samples_by_bin: Dict[str, List],
    target_per_bin: int = TARGET_SAMPLES_PER_BIN,
    min_bin_size: int = MIN_BIN_SIZE,
    max_oversample: int = MAX_OVERSAMPLE_FACTOR,
) -> Tuple[List, Dict[str, int]]:
    """
    Apply unified bin-balancing algorithm across all phases.

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

        if bin_size < min_bin_size:
            stats['bins_dropped'] += 1
            stats['dropped_bins'].append((bin_key, bin_size))
            continue

        if bin_size >= target_per_bin:
            selected = random.sample(samples, target_per_bin)
            stats['bins_capped'] += 1
            stats['samples_by_bin'][bin_key] = target_per_bin
        elif bin_size < target_per_bin:
            max_samples = min(target_per_bin, bin_size * max_oversample)
            if max_samples > bin_size:
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