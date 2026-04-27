"""Batch 1 staging contracts for manifests and derived patch artefacts.

This module centralizes lightweight schema contracts used by the staged pipeline.
It is intentionally dependency-free so both orchestrators and workers can import
it without pulling heavy runtime modules.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any


STAGE_NAMES = (
    "extract",
    "patch",
    "h3",
    "ccode",
    "toponyms",
    "tiles",
    "index",
)

STAGE_STATUSES = (
    "pending",
    "running",
    "completed",
    "failed",
    "skipped",
)


@dataclass(slots=True)
class StageState:
    status: str = "pending"
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    metrics: dict[str, Any] | None = None


@dataclass(slots=True)
class Manifest:
    run_id: str
    namespace: str
    generated_at: str
    source_version: str | None
    artefacts: dict[str, str]
    stages: dict[str, StageState]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_manifest(run_id: str, namespace: str, source_version: str | None = None) -> Manifest:
    """Create a default manifest object for a namespace run."""
    return Manifest(
        run_id=run_id,
        namespace=namespace,
        generated_at=utc_now_iso(),
        source_version=source_version,
        artefacts={},
        stages={name: StageState() for name in STAGE_NAMES},
    )


def manifest_to_dict(manifest: Manifest) -> dict[str, Any]:
    """Serialize dataclass manifest to plain dict for json dump."""
    data = asdict(manifest)
    data["contract_version"] = 1
    return data


def validate_stage_status(status: str) -> None:
    if status not in STAGE_STATUSES:
        raise ValueError(f"Invalid stage status '{status}'. Expected one of {STAGE_STATUSES}")


# Geometry reference contract (for staged rows pointing into geometry blob store)
GEOMETRY_REF_REQUIRED_FIELDS = (
    "place_id",
    "geometry_index",
    "geom_ref",
)


# CCode patch record contract (applied after H3 prefilter stage)
CCODE_PATCH_REQUIRED_FIELDS = (
    "place_id",
    "ccodes",
    "source",
)


def validate_required_fields(record: dict[str, Any], required_fields: tuple[str, ...]) -> None:
    missing = [f for f in required_fields if f not in record]
    if missing:
        raise ValueError(f"Record missing required fields: {missing}")


# ---------------------------------------------------------------------------
# Dataset-status contract (Master Plan Appendix E.2 item 3)
# ---------------------------------------------------------------------------

DATASET_STATUSES = ("published", "pending")
"""Permitted values for the top-level ``dataset_status`` field on every staged place doc.

``published`` content is visible to all users; ``pending`` content lives in the same
indices but is hidden from off-scope users by a discovery-time filter (see
Master Plan §7.2 / §7.4).
"""

STAGED_PLACE_REQUIRED_TOPLEVEL_FIELDS = (
    "place_id",
    "namespace",
    "dataset_status",
    "dataset_id",
)
"""Fields that every staged place document must carry at the top level.

``dataset_status`` and ``dataset_id`` are populated by ``write_staged_place_doc`` if
absent (defaulting to ``'published'`` / ``<namespace>``); the ``whg`` namespace requires
an explicit ``dataset_id`` (sub-namespaced per Dataset/Collection, e.g. ``'whg:1234'``).
"""


# ---------------------------------------------------------------------------
# Per-gazetteer aggregate contract (Master Plan §1.4.1, Appendix E.2 items 1–2)
# ---------------------------------------------------------------------------

# Global gazetteers are exempt from per-gazetteer H3 coverage compaction — their
# coverage is assumed worldwide and the inventory carries the sentinel "global"
# instead of an enumerated cell list. Browser intersection tests shortcut on the
# sentinel without enumerating cells.
GLOBAL_COVERAGE_NAMESPACES = frozenset({
    "osm",
    "ohm",
    "wd",
    "gn",
    "po",
    "tgn",
})

H3_COVERAGE_GLOBAL_SENTINEL = "global"

# Aggregate file paths under ``{STAGED_BASE_DIR}/_aggregates/``. Two files per
# non-global namespace; one file (with the sentinel) for global namespaces.
AGGREGATE_H3_COVERAGE_FILENAME_TEMPLATE = "{namespace}.h3_coverage.json"
AGGREGATE_TEMPORAL_EXTENT_FILENAME_TEMPLATE = "{namespace}.temporal_extent.json"


# H3 coverage file shape:
#   non-global: {"namespace": "<ns>", "coverage": ["8a2a..."], "compacted": true}
#   global:     {"namespace": "<ns>", "coverage": "global"}
# Produced by Batch 6 (per-gazetteer H3 derivation + coverage compaction).
H3_COVERAGE_REQUIRED_FIELDS = ("namespace", "coverage")


# Temporal-extent file shape:
#   {"namespace": "<ns>", "record_count": <int>, "temporal_extent": [start, end]}
# Where start/end are calendar years (int) or null. Produced by Batch 9 alongside
# corpus-wide toponym dedup + Symphonym embedding generation.
TEMPORAL_EXTENT_REQUIRED_FIELDS = ("namespace", "record_count", "temporal_extent")


def validate_h3_coverage_aggregate(record: dict[str, Any]) -> None:
    """Validate a per-gazetteer H3 coverage aggregate file payload."""
    validate_required_fields(record, H3_COVERAGE_REQUIRED_FIELDS)
    coverage = record["coverage"]
    namespace = record["namespace"]
    if namespace in GLOBAL_COVERAGE_NAMESPACES:
        if coverage != H3_COVERAGE_GLOBAL_SENTINEL:
            raise ValueError(
                f"Global namespace '{namespace}' must use sentinel "
                f"'{H3_COVERAGE_GLOBAL_SENTINEL}', got {coverage!r}"
            )
    else:
        if not isinstance(coverage, list) or not all(
            isinstance(c, str) for c in coverage
        ):
            raise ValueError(
                f"Non-global namespace '{namespace}' coverage must be a list of "
                f"H3 cell strings, got {type(coverage).__name__}"
            )


def validate_temporal_extent_aggregate(record: dict[str, Any]) -> None:
    """Validate a per-gazetteer temporal-extent aggregate file payload."""
    validate_required_fields(record, TEMPORAL_EXTENT_REQUIRED_FIELDS)
    extent = record["temporal_extent"]
    if (
        not isinstance(extent, list)
        or len(extent) != 2
        or not all((e is None) or isinstance(e, int) for e in extent)
    ):
        raise ValueError(
            f"temporal_extent must be a 2-element [start, end] list of int|None, "
            f"got {extent!r}"
        )
    if not isinstance(record["record_count"], int) or record["record_count"] < 0:
        raise ValueError(
            f"record_count must be a non-negative int, got {record['record_count']!r}"
        )


# ---------------------------------------------------------------------------
# SQLite hard-link database schema (Batch 12)
# ---------------------------------------------------------------------------
# Documented here so Batch 12 consumers (clustering/sqlite_overlay.py,
# clustering/harvest/hard_links_staged.py, clustering/harvest/loc_links.py,
# clustering/harvest/contributor_replay.py) share a single source of truth for
# the schema. The actual database is built on CRC and shipped to Pitt via
# atomic-swap; the gateway opens it read-only.

HARD_LINK_RELATION_TYPES = (
    "sameAs",
    "exactMatch",
    "closeMatch",
    "distinct",
)

HARD_LINK_SOURCE_CATEGORIES = ("authority", "contributor")

HARD_LINK_SQLITE_SCHEMA = """
CREATE TABLE hard_link_assertions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_a TEXT NOT NULL,
    place_b TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    source_category TEXT NOT NULL,
    source_id TEXT NOT NULL,
    asserted_at TEXT,
    justification TEXT,
    CHECK (place_a < place_b),
    CHECK (relation_type IN ('sameAs', 'exactMatch', 'closeMatch', 'distinct')),
    CHECK (source_category IN ('authority', 'contributor')),
    UNIQUE (place_a, place_b, relation_type, source_id)
);
CREATE INDEX ix_hla_place_a ON hard_link_assertions(place_a);
CREATE INDEX ix_hla_place_b ON hard_link_assertions(place_b);
CREATE INDEX ix_hla_source ON hard_link_assertions(source_category, source_id);
"""
"""DDL for the Pitt-side hard-link SQLite database.

Inserts must use ``INSERT OR IGNORE`` for idempotency; the ``UNIQUE`` constraint
deduplicates assertions made by multiple sources covering the same relation.

``source_id`` conventions:
    - Authority harvest:  ``'wikidata'``, ``'tgn'``, ``'osm'``, ``'ohm'``, ``'loc'``, ...
    - Contributor replay: ``'contributor:<user_id>'`` (legacy v3.2 rows append
      ``':legacy_v3_2'``, see Batch 13b).

``place_a < place_b`` is enforced canonical ordering so we never store both
directions of a symmetric relation.

WAL mode and a large page cache are required during bulk insert; configure via
``PRAGMA journal_mode=WAL; PRAGMA cache_size=-200000;`` before harvest.
"""

HARD_LINK_REQUIRED_FIELDS = (
    "place_a",
    "place_b",
    "relation_type",
    "source_category",
    "source_id",
)


def validate_hard_link_row(row: dict[str, Any]) -> None:
    """Validate a hard-link row before SQLite insert."""
    validate_required_fields(row, HARD_LINK_REQUIRED_FIELDS)
    if row["relation_type"] not in HARD_LINK_RELATION_TYPES:
        raise ValueError(
            f"relation_type must be one of {HARD_LINK_RELATION_TYPES}, "
            f"got {row['relation_type']!r}"
        )
    if row["source_category"] not in HARD_LINK_SOURCE_CATEGORIES:
        raise ValueError(
            f"source_category must be one of {HARD_LINK_SOURCE_CATEGORIES}, "
            f"got {row['source_category']!r}"
        )
    if row["place_a"] >= row["place_b"]:
        raise ValueError(
            f"place_a must be lexicographically less than place_b "
            f"(got {row['place_a']!r}, {row['place_b']!r})"
        )

