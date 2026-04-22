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

