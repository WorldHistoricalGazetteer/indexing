# gateway/config.py
"""Gateway configuration, loaded from .env"""

import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# ES backend (internal, localhost only)
ES_INTERNAL_PORT = int(os.getenv("PROD_ES_INTERNAL_PORT", "9201"))
ES_BACKEND = f"http://localhost:{ES_INTERNAL_PORT}"

# Kibana backend (internal, localhost only)
KIBANA_PORT = int(os.getenv("KIBANA_PORT", "5601"))
KIBANA_BACKEND = f"http://localhost:{KIBANA_PORT}"

# Gateway (external-facing)
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "9200"))

# Host-based routing keywords
KIBANA_HOST_KEYWORD = "kibana"

# ES credentials (for gateway health checks and KNN queries)
IX1_BASE = os.getenv("IX1_BASE", "/ix1/ishi")
# Fast flash storage (mirrors processing.settings.IX3_BASE). Prefer this over
# /ix1 for latency-sensitive gateway-owned files — /ix1 (NFS) can be slow.
IX3_BASE = os.getenv("IX3_BASE", "/vast/ishi")
# Prefer the /vast copy (ES relocated off /ix1, 2026-07-15) so the gateway
# survives an /ix1 outage; fall back to the /ix1 original.
_vast_pass_file = f"{IX3_BASE}/es/config/elastic.password"
ELASTIC_PASS_FILE = _vast_pass_file if os.path.exists(_vast_pass_file) else f"{IX1_BASE}/es/config/elastic.password"

# ES access is ALWAYS via the stable aliases `toponyms` / `places` (never the
# dated concrete index or a `*` wildcard). This lets index cutovers (rebuilds,
# the #127 ngram reindex, etc.) be a single atomic, reversible alias re-point
# with no gateway change — and avoids a wildcard matching two indices mid-swap.
TOPONYMS_INDEX = os.getenv("TOPONYMS_INDEX", "toponyms")
PLACES_INDEX = os.getenv("PLACES_INDEX", "places")
# NB: the legacy `clusters` index enrichment was retired 2026-07-12 (clustering
# is client-side now — plan §1); CLUSTERS_INDEX is intentionally gone.

# Serving + observability
# WORKERS: more than one uvicorn worker so a single wedged request cannot take the
# whole gateway down — which is exactly what happened on 2026-08-18, when one hung
# request left the process alive, spinning, and serving nothing. Each worker loads
# its own Symphonym model (~650MB RSS), so this trades memory for survivability.
GATEWAY_WORKERS = int(os.getenv("GATEWAY_WORKERS", "2"))
# A request that takes longer than this is logged when it finishes.
SLOW_REQUEST_SECONDS = float(os.getenv("GATEWAY_SLOW_REQUEST_SECONDS", "10"))
# A request still running after this is reported by the in-flight sweeper — the case
# the access log can never show, because uvicorn logs a request only on completion.
INFLIGHT_WARN_SECONDS = float(os.getenv("GATEWAY_INFLIGHT_WARN_SECONDS", "30"))
INFLIGHT_SWEEP_SECONDS = float(os.getenv("GATEWAY_INFLIGHT_SWEEP_SECONDS", "30"))

# Symphonym model
SYMPHONYM_MODEL_DIR = os.getenv("SYMPHONYM_MODEL_DIR", "")  # empty = auto-detect
SYMPHONYM_DATA_VERSION = os.getenv("SYMPHONYM_DATA_VERSION", "7")


def get_elastic_password() -> str | None:
    """Read the elastic superuser password if it exists."""
    try:
        return Path(ELASTIC_PASS_FILE).read_text().strip()
    except FileNotFoundError:
        return None

