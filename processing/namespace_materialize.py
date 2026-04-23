"""Batch 5 namespace materialization starter.

Builds deterministic per-namespace snapshot manifests from staged extract artefacts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from processing.settings import (
    GEOM_STORE_STAGING_DIR,
    STAGED_BASE_DIR,
    STAGED_MANIFEST_FILENAME,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _line_count(path: Path) -> int:
    count = 0
    with path.open("rb") as f:
        for _ in f:
            count += 1
    return count


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def materialize_namespace_snapshot_manifest(
    *,
    namespace: str,
    run_id: str,
    staged_base_dir: str | Path = STAGED_BASE_DIR,
    extract_stage: str = "extract",
    manifest_filename: str = STAGED_MANIFEST_FILENAME,
) -> dict[str, Any]:
    """Create deterministic namespace manifest from staged extract artefacts.

    Prefers `places.parquet`; falls back to `places.jsonl` for early-stage runs.
    """
    base = Path(staged_base_dir)
    ns_dir = base / namespace
    extract_dir = ns_dir / extract_stage

    places_parquet = extract_dir / "places.parquet"
    places_jsonl = extract_dir / "places.jsonl"
    snapshot_meta = extract_dir / "places.snapshot.json"
    events_jsonl = extract_dir / "events.jsonl"

    primary_places = places_parquet if places_parquet.exists() else places_jsonl
    if not primary_places.exists():
        raise FileNotFoundError(
            f"Missing staged extract artefact: expected {places_parquet} or {places_jsonl}"
        )

    geom_store_dir = Path(GEOM_STORE_STAGING_DIR)
    geom_paths = []
    if geom_store_dir.exists():
        geom_paths.extend(sorted(geom_store_dir.glob(f"{namespace}*.bin")))
        geom_paths.extend(sorted(geom_store_dir.glob(f"{namespace}*.index.json")))

    artefacts: dict[str, dict[str, Any]] = {}
    for name, path in (
        ("places_parquet", places_parquet),
        ("places_jsonl", places_jsonl),
        ("snapshot_meta", snapshot_meta),
        ("events_jsonl", events_jsonl),
    ):
        if not path.exists():
            continue
        artefacts[name] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "lines": _line_count(path) if path.suffix == ".jsonl" else None,
        }

    for path in geom_paths:
        artefacts[f"geometry::{path.name}"] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "lines": _line_count(path) if path.suffix == ".jsonl" else None,
        }

    docs_written = None
    if snapshot_meta.exists():
        try:
            snapshot_payload = json.loads(snapshot_meta.read_text(encoding="utf-8"))
            docs_written = snapshot_payload.get("docs_written")
        except Exception:
            docs_written = None

    payload: dict[str, Any] = {
        "contract_version": 1,
        "run_id": run_id,
        "namespace": namespace,
        "stage": extract_stage,
        "status": "completed",
        "row_count": docs_written,
        "primary_places_artefact": str(primary_places),
        "artefacts": artefacts,
    }

    # Write stage-scoped materialization manifest
    stage_manifest = extract_dir / "materialized.snapshot.manifest.json"
    _atomic_write_json(stage_manifest, payload)

    # Mirror to namespace root canonical manifest file for controller/barrier usage.
    root_manifest = ns_dir / manifest_filename
    _atomic_write_json(root_manifest, payload)

    return payload

