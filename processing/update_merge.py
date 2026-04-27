#!/usr/bin/env python3
"""Batch 4c Phase 3 — collapse update patches into the staged snapshot.

Reads ``staged/{namespace}/extract/places.parquet|jsonl`` plus
``staged/{namespace}/update_patch/places.update.jsonl`` and writes
``staged/{namespace}/update_merged/places.parquet|jsonl``. This is the
generic merger consumed by both ``geonames-toponyms`` (toponyms + relations
patch) and ``wikidata-geoshapes`` (geometry-replacement patch).

Patch row shape (only ``place_id`` is required; every other field is
optional and only fields present on a patch row are merged)::

    {
      "place_id": "<ns>:<id>",
      "title": "<optional new title — overwrites>",
      "toponyms_to_add": [
        {"toponym_id": "London@en", "timespans": [...]}, ...
      ],
      "relations_to_add": [
        {"relation_type": "sameAs", "related_place_id": "wd:Q84",
         "label": "Wikidata"}, ...
      ],
      "geometries_to_replace": [<enriched geom_entry>, ...],
      "h3_centroid": "<cell>",   # only set when geometries_to_replace present
      "h3_cover":    ["<cell>", ...]
    }

Merge semantics:

* ``title`` overwrites the source title when present (matches the legacy
  GN-toponyms ES script that updated ``title`` only for preferred names).
* ``toponyms_to_add`` are appended de-duplicated by ``toponym_id`` (existing
  toponyms with the same ``toponym_id`` win; the patch never overwrites).
* ``relations_to_add`` are appended de-duplicated by
  ``(relation_type, related_place_id)``.
* ``geometries_to_replace`` overwrites the entire ``geometries`` array
  (matches the legacy WD-geoshapes ES script which did
  ``ctx._source.geometries[0] = params.new_geom``). Top-level ``h3_centroid``
  / ``h3_cover`` from the patch are preserved.

Idempotent: re-running the merger over the same source + patch produces
byte-identical output.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pyarrow.json as paj
import pyarrow.parquet as pq

from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import (
    record_script_wall_time,
    write_runtime_history_event,
    write_stage_event,
)
from processing.staging_contract import (
    UPDATE_MERGED_DIRNAME,
    UPDATE_PATCH_FILENAME,
    UPDATE_PATCH_REQUIRED_FIELDS,
    has_update_patch,
    validate_required_fields,
)
from processing.staging_orchestrator import update_namespace_stage_status


def _extract_dir(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / namespace / "extract"


def _patch_path(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / namespace / "update_patch" / UPDATE_PATCH_FILENAME


def _merged_dir(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / namespace / UPDATE_MERGED_DIRNAME


def _iter_extract_docs(namespace: str) -> Iterator[dict[str, Any]]:
    src_dir = _extract_dir(namespace)
    parquet_path = src_dir / "places.parquet"
    jsonl_path = src_dir / "places.jsonl"

    if parquet_path.exists():
        parquet = pq.ParquetFile(parquet_path)
        for batch in parquet.iter_batches(batch_size=2000):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row
        return
    if jsonl_path.exists():
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
        return
    raise FileNotFoundError(
        f"No staged extract for namespace '{namespace}' in {src_dir}"
    )


def _load_patches(namespace: str) -> dict[str, dict[str, Any]]:
    """Return ``{place_id: merged_patch}`` (later patch rows shallow-update earlier).

    The patch file may contain multiple rows for the same ``place_id`` — we
    fold them by appending lists and overwriting scalar fields.
    """
    patches: dict[str, dict[str, Any]] = {}
    patch_path = _patch_path(namespace)
    if not patch_path.exists():
        return patches

    with patch_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                validate_required_fields(rec, UPDATE_PATCH_REQUIRED_FIELDS)
            except ValueError:
                continue

            place_id = rec["place_id"]
            existing = patches.setdefault(place_id, {})
            for field in ("title", "geometries_to_replace", "h3_centroid", "h3_cover"):
                if field in rec:
                    existing[field] = rec[field]
            for field in ("toponyms_to_add", "relations_to_add"):
                if field not in rec:
                    continue
                value = rec[field]
                if not isinstance(value, list):
                    continue
                existing.setdefault(field, []).extend(value)
    return patches


def _dedupe_toponyms(
    existing: list[Any] | None, additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append additions, skipping any whose ``toponym_id`` already exists."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for entry in existing or []:
        if isinstance(entry, dict):
            tid = entry.get("toponym_id")
            if isinstance(tid, str):
                seen.add(tid)
            merged.append(entry)
    for entry in additions:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("toponym_id")
        if not isinstance(tid, str) or tid in seen:
            continue
        seen.add(tid)
        merged.append(entry)
    return merged


def _dedupe_relations(
    existing: list[Any] | None, additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append additions, skipping any whose ``(relation_type, related_place_id)``
    already exists."""
    def _key(rel: dict[str, Any]) -> tuple[str, str] | None:
        rt = rel.get("relation_type")
        rp = rel.get("related_place_id")
        if isinstance(rt, str) and isinstance(rp, str):
            return (rt, rp)
        return None

    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for entry in existing or []:
        if isinstance(entry, dict):
            key = _key(entry)
            if key is not None:
                seen.add(key)
            merged.append(entry)
    for entry in additions:
        if not isinstance(entry, dict):
            continue
        key = _key(entry)
        if key is None or key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def _apply_patch(doc: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (merged_doc, changed)."""
    merged = dict(doc)
    changed = False

    if "title" in patch and patch["title"]:
        if merged.get("title") != patch["title"]:
            merged["title"] = patch["title"]
            changed = True

    if "toponyms_to_add" in patch and patch["toponyms_to_add"]:
        new_toponyms = _dedupe_toponyms(merged.get("toponyms"), patch["toponyms_to_add"])
        if new_toponyms != (merged.get("toponyms") or []):
            merged["toponyms"] = new_toponyms
            changed = True

    if "relations_to_add" in patch and patch["relations_to_add"]:
        new_relations = _dedupe_relations(merged.get("relations"), patch["relations_to_add"])
        if new_relations != (merged.get("relations") or []):
            merged["relations"] = new_relations
            changed = True

    if "geometries_to_replace" in patch and patch["geometries_to_replace"] is not None:
        merged["geometries"] = patch["geometries_to_replace"]
        if "h3_centroid" in patch:
            merged["h3_centroid"] = patch["h3_centroid"]
        if "h3_cover" in patch:
            merged["h3_cover"] = patch["h3_cover"]
        changed = True

    return merged, changed


def _normalize_for_parquet(doc: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(doc)
    for key in ("geometries", "toponyms", "types", "relations"):
        value = normalized.get(key)
        if isinstance(value, list) and len(value) == 0:
            normalized[key] = None
    return normalized


def run_update_merge(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path | None = None,
    slurm_job_id: str | None = None,
) -> dict[str, Any]:
    if not has_update_patch(namespace):
        # Defensive: callers shouldn't invoke this for namespaces that don't
        # have a Phase 3 patch. Treat as a no-op rather than failing.
        return {
            "namespace": namespace,
            "skipped": True,
            "reason": "namespace not in UPDATE_PATCH_NAMESPACES",
        }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "update_merge", "running")
    write_stage_event(
        run_id=run_id, namespace=namespace, script_id="update-merge",
        status="running", stage="update_merge",
    )
    write_runtime_history_event(
        run_id=run_id, event="update_merge", status="running",
        namespace=namespace, stage="update_merge", slurm_job_id=slurm_job_id,
    )

    started = datetime.now(timezone.utc)
    patches = _load_patches(namespace)

    out_dir = _merged_dir(namespace)
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "places.parquet"
    jsonl_path = out_dir / "places.jsonl"

    docs_seen = 0
    docs_changed = 0
    docs_written = 0

    with jsonl_path.open("w", encoding="utf-8") as out_jsonl:
        for doc in _iter_extract_docs(namespace):
            docs_seen += 1
            place_id = doc.get("place_id")
            patch = patches.pop(place_id, None) if place_id else None
            if patch:
                doc, changed = _apply_patch(doc, patch)
                if changed:
                    docs_changed += 1
            doc = _normalize_for_parquet(doc)
            out_jsonl.write(json.dumps(doc, ensure_ascii=True) + "\n")
            docs_written += 1

    table = paj.read_json(str(jsonl_path))
    pq.write_table(table, str(parquet_path))

    finished = datetime.now(timezone.utc)
    metrics = {
        "namespace": namespace,
        "docs_seen": docs_seen,
        "docs_changed": docs_changed,
        "docs_written": docs_written,
        "patches_unmatched": len(patches),
        "parquet_path": str(parquet_path),
        "jsonl_path": str(jsonl_path),
        "wall_seconds": round((finished - started).total_seconds(), 1),
    }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "update_merge", "completed", metrics=metrics,
        )
    write_stage_event(
        run_id=run_id, namespace=namespace, script_id="update-merge",
        status="completed", stage="update_merge", metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id, event="update_merge", status="completed",
        namespace=namespace, stage="update_merge", details=metrics,
    )
    try:
        record_script_wall_time(
            namespace=namespace, script_id="update-merge", run_id=run_id,
            started_at=started.isoformat(), finished_at=finished.isoformat(),
            wall_seconds=metrics["wall_seconds"], status="completed",
            slurm_job_id=slurm_job_id,
            extra={"docs_changed": docs_changed},
        )
    except Exception:
        pass
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge Phase 3 update patches into the staged snapshot"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--manifest-path")
    args = parser.parse_args()

    if args.manifest_path:
        manifest_path = Path(args.manifest_path)
    else:
        manifest_path = Path(
            STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
                runs_dir=STAGED_RUNS_DIR, run_id=args.run_id,
            )
        )

    import os
    metrics = run_update_merge(
        run_id=args.run_id, namespace=args.namespace,
        manifest_path=manifest_path if manifest_path.exists() else None,
        slurm_job_id=os.getenv("SLURM_JOB_ID"),
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
