# clustering/config.py
"""Configuration constants used by the surviving Batch 12 hard-link harvesters.

Everything here was previously the configuration surface of the legacy
4-phase ES-based clustering pipeline (retired by the Master Plan). Only the
identifiers actually consumed by ``hard_links_staged``, ``loc_links``, and
``contributor_replay`` (via ``pg_client``) remain.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


# ---------------------------------------------------------------------------
# WHG PostgreSQL (on DigitalOcean VM, via SSH tunnel)
# Used by clustering/pg_client.py + clustering/harvest/contributor_replay.py
# ---------------------------------------------------------------------------
PG_SSH_HOST = os.getenv("PG_SSH_HOST", "whg")  # SSH config alias
PG_DB_NAME = os.getenv("PG_DB_NAME", "whgv2")
PG_DB_USER = os.getenv("PG_DB_USER", "postgres")
PG_DB_HOST = os.getenv("PG_DB_HOST", "localhost")  # local after SSH tunnel
PG_DB_PORT = int(os.getenv("PG_DB_PORT", "5432"))


# ---------------------------------------------------------------------------
# Known WHG namespaces — hard-link targets must resolve into one of these
# for the assertion to be useful at query time.
# ---------------------------------------------------------------------------
KNOWN_ES_NAMESPACES = frozenset({"gn", "wd", "osm", "tgn", "gb", "pl", "iv", "nl", "dp", "un", "whg"})


# ---------------------------------------------------------------------------
# Identity relation types harvested into the SQLite hard_link_assertions table.
# ``distinct`` is reserved for future use (Master Plan §10.3).
# ---------------------------------------------------------------------------
IDENTITY_RELATION_TYPES = frozenset({"sameAs", "closeMatch", "exactMatch"})
