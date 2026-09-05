# gateway/config.py
"""Gateway configuration, loaded from .env then .env.local"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Two layers, in the same order and with the same precedence as
# ``processing/settings.py`` and ``clustering/config.py``: ``.env`` holds the
# shared, committed defaults; ``.env.local`` is gitignored and holds per-host
# overrides and secrets, so ``override=True`` lets it win.
#
# This module read ONLY ``.env`` until 5 Sep 2026, and it is the gateway's sole
# source of environment: ``_common.sh`` sources both files but WITHOUT ``set -a``,
# so nothing it reads is exported and none of it reaches this process. A host that
# needed to override a gateway setting therefore had to edit the TRACKED ``.env``
# — which is what happened with ``SYMPHONYM_MODEL_DIR`` (place#242), and that edit
# then blocked ``git pull`` on the deployed checkout, because the incoming commit
# touched the same file. A deployment that cannot fast-forward is a deployment
# that silently does not happen: the staging-restore fix sat committed and
# undeployed until a peer session hit the very bug it fixes.
_repo_root = Path(__file__).parent.parent
load_dotenv(_repo_root / ".env")

# The override layer is OPTIONAL and must never be able to stop the gateway
# starting. ``load_dotenv`` does not tolerate an unreadable path — it propagates
# PermissionError — and on this deployment ``.env.local`` is mode 660
# stg135:ishi while the gateway runs as ``gazetteer``. That works today only
# because gazetteer's primary group IS ishi; a host where it is not would have
# turned this convenience into an import-time crash with no log line explaining
# it. Absent-but-optional is fine and silent; present-but-unreadable is a real
# misconfiguration and says so on stderr (i.e. into the gateway's nohup.out)
# rather than being swallowed.
_env_local = _repo_root / ".env.local"
if _env_local.exists():
    if os.access(_env_local, os.R_OK):
        load_dotenv(_env_local, override=True)
    else:
        print(
            f"WARNING: {_env_local} exists but is not readable by "
            f"uid {os.geteuid()}; per-host overrides are NOT applied.",
            file=sys.stderr,
        )

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

