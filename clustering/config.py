# clustering/config.py
"""
Clustering configuration — ES connection, PG connection (SSH tunnel),
index names, thresholds, scoring weights.

Loaded from environment variables or the project .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------
ES_INTERNAL_PORT = int(os.getenv("PROD_ES_INTERNAL_PORT", "9201"))
ES_URL = os.getenv("ES_URL", f"http://localhost:{ES_INTERNAL_PORT}")

IX1_BASE = os.getenv("IX1_BASE", "/ix1/ishi")
ELASTIC_PASS_FILE = f"{IX1_BASE}/es/config/elastic.password"

PLACES_INDEX = os.getenv("PLACES_INDEX", "places")
TOPONYMS_INDEX = os.getenv("TOPONYMS_INDEX", "toponyms")
CLUSTERS_INDEX = os.getenv("CLUSTERS_INDEX", "clusters")
CLUSTER_STATE_INDEX = os.getenv("CLUSTER_STATE_INDEX", "cluster_state")

# ---------------------------------------------------------------------------
# WHG PostgreSQL (on DigitalOcean VM, via SSH tunnel)
# ---------------------------------------------------------------------------
PG_SSH_HOST = os.getenv("PG_SSH_HOST", "whg")  # SSH config alias
PG_DB_NAME = os.getenv("PG_DB_NAME", "whgv2")
PG_DB_USER = os.getenv("PG_DB_USER", "postgres")
PG_DB_HOST = os.getenv("PG_DB_HOST", "localhost")  # local after SSH tunnel
PG_DB_PORT = int(os.getenv("PG_DB_PORT", "5432"))

# ---------------------------------------------------------------------------
# Algorithm version
# ---------------------------------------------------------------------------
ALGORITHM_VERSION = "cluster_v1.0"

# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------
BATCH_SIZE = int(os.getenv("CLUSTER_BATCH_SIZE", "1000"))
ES_BULK_CHUNK = int(os.getenv("CLUSTER_ES_BULK_CHUNK", "5000"))
SCROLL_SIZE = int(os.getenv("CLUSTER_SCROLL_SIZE", "2000"))

# ---------------------------------------------------------------------------
# Known ES namespaces (targets must be in this set for a link to be useful)
# ---------------------------------------------------------------------------
KNOWN_ES_NAMESPACES = frozenset({"gn", "wd", "osm", "tgn", "gb", "pl", "iv", "nl", "dp", "un", "whg"})

# ---------------------------------------------------------------------------
# Identity relation types (from RECON_NOTES §8.2.2)
# ---------------------------------------------------------------------------
IDENTITY_RELATION_TYPES = frozenset({"sameAs", "closeMatch", "exactMatch"})


def get_elastic_password() -> str | None:
    """Read the elastic superuser password if it exists."""
    try:
        return Path(ELASTIC_PASS_FILE).read_text().strip()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Scoring configuration
# ---------------------------------------------------------------------------
@dataclass
class ScoringConfig:
    """Weights and thresholds for composite scoring."""

    # Composite score weights (must sum to 1.0)
    weight_toponym_exact: float = 0.30
    weight_symphonym: float = 0.25
    weight_spatial: float = 0.25
    weight_type_match: float = 0.10
    weight_ccode_overlap: float = 0.10

    # Phase 2 thresholds
    threshold_exact_km: float = 50.0
    max_attestations_per_toponym: int = 500  # skip very common toponyms

    # Phase 3 thresholds
    threshold_phonetic_km: float = 25.0
    knn_k: int = 20
    knn_min_similarity: float = 0.85

    # Phase 4 thresholds
    cluster_score_threshold: float = 0.4
    max_cluster_diameter_km: float = 100.0

    # Concurrent KNN queries for Phase 3
    knn_concurrency: int = 10

    # Phase 3 place limit (0 = unlimited)
    max_phase3_places: int = 0

    # Calibration parameters
    calibration_sample_size: int = 20_000  # max positive pairs to sample
    calibration_neg_ratio: float = 1.0  # ratio of negatives to positives


@dataclass
class ClusterConfig:
    """Top-level configuration bundle."""

    es_url: str = ES_URL
    es_password: str | None = field(default_factory=get_elastic_password)
    places_index: str = PLACES_INDEX
    toponyms_index: str = TOPONYMS_INDEX
    clusters_index: str = CLUSTERS_INDEX
    state_index: str = CLUSTER_STATE_INDEX
    algorithm_version: str = ALGORITHM_VERSION
    batch_size: int = BATCH_SIZE
    bulk_chunk: int = ES_BULK_CHUNK
    scroll_size: int = SCROLL_SIZE
    scoring: ScoringConfig = field(default_factory=ScoringConfig)

    # Throttling: seconds to sleep between bulk index flushes (0 = none)
    bulk_throttle_seconds: float = float(
        os.getenv("CLUSTER_BULK_THROTTLE", "0.5")
    )
    # Max place IDs per terms query (ES has a 65536 terms limit by default)
    terms_query_max: int = int(os.getenv("CLUSTER_TERMS_MAX", "2000"))

