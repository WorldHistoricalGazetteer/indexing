#!/usr/bin/env python3
"""Merge staged H3 patches into namespace snapshots.

Reads:
  - {STAGED_BASE_DIR}/{namespace}/boundary_merged/places.parquet|jsonl  (osm/ohm)
    or {STAGED_BASE_DIR}/{namespace}/extract/places.parquet|jsonl       (others)
  - {STAGED_BASE_DIR}/{namespace}/h3/places.h3.jsonl

Writes:
  - {STAGED_BASE_DIR}/{namespace}/h3_merged/places.parquet
  - {STAGED_BASE_DIR}/{namespace}/h3_merged/places.jsonl

H3 fields are merged per-geometry, addressed by ``geometry_index`` carried on
both sides. Re-running the merge over the same patches is idempotent.

This stage runs after Batch 6 (H3 derivation) and before Batch 7 (ccode
enrichment). It does not access Elasticsearch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import pyarrow.json as paj
import pyarrow.parquet as pq

from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import write_runtime_history_event, write_stage_event
from processing.staging_contract import (
    H3_PATCH_REQUIRED_FIELDS,
    H3_PATCH_GEOMETRY_REQUIRED_FIELDS,
    validate_required_fields,
)
from processing.staging_orchestrator import update_namespace_stage_status


# OSM/OHM run boundary completion before H3, so their H3 source is the
# boundary-merged snapshot. gn/wd run a Phase 3 update merge before H3 (the
# auxiliary toponyms/geoshape patches), so their source is update_merged/.
# Everything else reads the extract directly.
BOUNDARY_REQUIRED_NAMESPACES = frozenset({"osm", "ohm"})

from processing.staging_contract import UPDATE_PATCH_NAMESPACES  # noqa: E402


def _source_dir(namespace: str) -> Path:
    base = Path(STAGED_BASE_DIR) / namespace
    if namespace in BOUNDARY_REQUIRED_NAMESPACES:
        return base / "boundary_merged"
    if namespace in UPDATE_PATCH_NAMESPACES:
        update_merged = base / "update_merged"
        if update_merged.exists():
            return update_merged
    return base / "extract"


def _iter_source_docs(namespace: str) -> Iterable[dict[str, Any]]:
    src_dir = _source_dir(namespace)
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
        f"No H3-merge source found for namespace '{namespace}' in {src_dir}"
    )


def _load_h3_patches(namespace: str) -> dict[str, dict[int, dict[str, Any]]]:
    """Return ``{place_id: {geometry_index: {h3_centroid, h3_cover}}}``."""
    patch_path = Path(STAGED_BASE_DIR) / namespace / "h3" / "places.h3.jsonl"
    if not patch_path.exists():
        raise FileNotFoundError(f"H3 patch file not found: {patch_path}")

    patches: dict[str, dict[int, dict[str, Any]]] = {}
    with patch_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            try:
                validate_required_fields(rec, H3_PATCH_REQUIRED_FIELDS)
            except ValueError:
                continue
            place_id = rec["place_id"]
            geom_updates: dict[int, dict[str, Any]] = {}
            for entry in rec.get("geometries") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    validate_required_fields(entry, H3_PATCH_GEOMETRY_REQUIRED_FIELDS)
                except ValueError:
                    continue
                geom_updates[int(entry["geometry_index"])] = {
                    "h3_centroid": entry["h3_centroid"],
                    "h3_cover": entry["h3_cover"],
                }
            if geom_updates:
                patches[place_id] = geom_updates
    return patches


def _apply_patch_to_doc(
    doc: dict[str, Any],
    geom_updates: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Return (merged_doc, applied_count) — patch is authoritative for h3_*."""
    geometries = doc.get("geometries") or []
    if not isinstance(geometries, list):
        return doc, 0

    applied = 0
    new_geometries: list[Any] = []
    for idx, geom in enumerate(geometries):
        if not isinstance(geom, dict):
            new_geometries.append(geom)
            continue
        gidx = geom.get("geometry_index", idx)
        update = geom_updates.get(gidx)
        if update is None:
            new_geometries.append(geom)
            continue
        merged_geom = dict(geom)
        merged_geom["h3_centroid"] = update["h3_centroid"]
        merged_geom["h3_cover"] = update["h3_cover"]
        new_geometries.append(merged_geom)
        applied += 1

    if not applied:
        return doc, 0

    merged = dict(doc)
    merged["geometries"] = new_geometries
    return merged, applied


def _normalize_for_parquet(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert empty nested-list fields to None for stable Parquet inference."""
    normalized = dict(doc)
    for key in ("geometries", "toponyms", "types", "relations"):
        value = normalized.get(key)
        if isinstance(value, list) and len(value) == 0:
            normalized[key] = None
    return normalized


def run_h3_merge(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "h3_merge", "running")

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="h3-merge",
        status="running",
        stage="h3_merge",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="h3_merge",
        status="running",
        namespace=namespace,
        stage="h3_merge",
    )

    patches = _load_h3_patches(namespace)

    out_dir = Path(STAGED_BASE_DIR) / namespace / "h3_merged"
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "places.parquet"
    jsonl_path = out_dir / "places.jsonl"

    docs_seen = 0
    docs_updated = 0
    geometry_updates_applied = 0
    docs_written = 0

    with jsonl_path.open("w", encoding="utf-8") as out_jsonl:
        for doc in _iter_source_docs(namespace):
            docs_seen += 1
            place_id = doc.get("place_id")
            geom_updates = patches.pop(place_id, None) if place_id else None
            if geom_updates:
                doc, applied = _apply_patch_to_doc(doc, geom_updates)
                if applied:
                    docs_updated += 1
                    geometry_updates_applied += applied

            doc = _normalize_for_parquet(doc)
            out_jsonl.write(json.dumps(doc, ensure_ascii=True) + "\n")
            docs_written += 1

    table = paj.read_json(str(jsonl_path))
    pq.write_table(table, str(parquet_path))

    metrics = {
        "docs_seen": docs_seen,
        "docs_updated": docs_updated,
        "geometry_updates_applied": geometry_updates_applied,
        "docs_written": docs_written,
        "patches_unmatched": len(patches),
        "parquet_path": str(parquet_path),
        "jsonl_path": str(jsonl_path),
    }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "h3_merge", "completed", metrics=metrics
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="h3-merge",
        status="completed",
        stage="h3_merge",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="h3_merge",
        status="completed",
        namespace=namespace,
        stage="h3_merge",
        details=metrics,
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge H3 patches into staged snapshot")
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--namespace", required=True, help="Namespace")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
            runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
        )
    )

    metrics = run_h3_merge(
        run_id=args.run_id,
        namespace=args.namespace,
        manifest_path=manifest_path if manifest_path.exists() else None,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
