"""Batch 5 namespace materialization starter.

Builds deterministic per-namespace snapshot manifests from staged extract artefacts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from processing.settings import STAGED_BASE_DIR, STAGED_MANIFEST_FILENAME


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

    Requires at minimum `staged/{namespace}/extract/places.jsonl`.
    """
    base = Path(staged_base_dir)
    ns_dir = base / namespace
    extract_dir = ns_dir / extract_stage

    places_jsonl = extract_dir / "places.jsonl"
    snapshot_meta = extract_dir / "places.snapshot.json"
    events_jsonl = extract_dir / "events.jsonl"

    if not places_jsonl.exists():
        raise FileNotFoundError(f"Missing staged extract artefact: {places_jsonl}")

    artefacts: dict[str, dict[str, Any]] = {}
    for name, path in (
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

    payload: dict[str, Any] = {
        "contract_version": 1,
        "run_id": run_id,
        "namespace": namespace,
        "stage": extract_stage,
        "status": "completed",
        "artefacts": artefacts,
    }

    # Write stage-scoped materialization manifest
    stage_manifest = extract_dir / "materialized.snapshot.manifest.json"
    _atomic_write_json(stage_manifest, payload)

    # Mirror to namespace root canonical manifest file for controller/barrier usage.
    root_manifest = ns_dir / manifest_filename
    _atomic_write_json(root_manifest, payload)

    return payload

