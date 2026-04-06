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
ELASTIC_PASS_FILE = f"{IX1_BASE}/es/config/elastic.password"

# ES index names (may be dated aliases like toponyms_20260317)
TOPONYMS_INDEX = os.getenv("TOPONYMS_INDEX", "toponyms_*")
PLACES_INDEX = os.getenv("PLACES_INDEX", "places_*")
CLUSTERS_INDEX = os.getenv("CLUSTERS_INDEX", "clusters")
BOUNDARIES_INDEX = os.getenv("BOUNDARIES_INDEX", "boundaries")

# Symphonym model
SYMPHONYM_MODEL_DIR = os.getenv("SYMPHONYM_MODEL_DIR", "")  # empty = auto-detect
SYMPHONYM_DATA_VERSION = os.getenv("SYMPHONYM_DATA_VERSION", "7")


def get_elastic_password() -> str | None:
    """Read the elastic superuser password if it exists."""
    try:
        return Path(ELASTIC_PASS_FILE).read_text().strip()
    except FileNotFoundError:
        return None

