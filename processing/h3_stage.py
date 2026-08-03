#!/usr/bin/env python
"""Stage-local H3 derivation worker.

This job reads staged namespace extract artefacts and writes H3 patch records
without touching Elasticsearch. It is intended to run as a Slurm array worker
(one namespace per task).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq

from processing.helpers import (
    H3_SUBCELL_BBOX_DEG,
    bbox_maxdim_deg,
    compute_h3_fields,
    select_h3_cover_geometry,
)
from processing.settings import STAGED_BASE_DIR, STAGED_RUN_MANIFEST_FILE_TEMPLATE, STAGED_RUNS_DIR
from processing.stage_writers import record_script_wall_time, write_runtime_history_event, write_stage_event
from processing.staging_contract import UPDATE_PATCH_NAMESPACES
from processing.staging_orchestrator import load_run_manifest, update_namespace_stage_status


BOUNDARY_REQUIRED_NAMESPACES = {"osm", "ohm"}


# ---------------------------------------------------------------------------
# Cover geometry source
# ---------------------------------------------------------------------------
# h3_cover for an area feature must be computed from the feature's real polygon.
# The staged ``hull`` (the previous cover source) is dropped from parquet
# sidecars by ``staged_parquet.strip_hull_for_parquet`` for schema stability, so
# any stage reading the parquet (which ``_iter_extract_docs`` prefers) sees no
# hull and would otherwise fall back to a centroid-only cover. The authoritative
# geometry lives in the geom store, keyed by ``geom_ref`` (=
# ``"{place_id}_{geometry_index}"``); we read it from there and fall back to the
# hull / inline geom when the store is unavailable or the feature has none.
try:
    from processing.geom_store import GeomStoreReader as _GeomStoreReader
    from processing.settings import GEOM_STORE_DIR as _GEOM_STORE_DIR
except Exception:  # pragma: no cover - geom store optional at import time
    _GeomStoreReader = None
    _GEOM_STORE_DIR = None

_geom_reader = None
_geom_reader_tried = False


def get_geom_reader():
    """Lazily open a cached ``GeomStoreReader`` (or None if unavailable)."""
    global _geom_reader, _geom_reader_tried
    if not _geom_reader_tried:
        _geom_reader_tried = True
        if _GeomStoreReader is not None and _GEOM_STORE_DIR:
            try:
                _geom_reader = _GeomStoreReader(_GEOM_STORE_DIR)
            except Exception:
                _geom_reader = None
    return _geom_reader


def cover_geometry_for(geom: dict, place_id: str, geometry_index, reader) -> dict | None:
    """Return the GeoJSON geometry to polyfill for ``h3_cover``.

    Prefers the authoritative polygon from the geom store (for ``has_geom``
    features); falls back to the staged hull / inline geom (the prior
    behaviour, still correct for namespaces whose h3 reads hull-bearing JSONL).
    Returns ``None`` for point features (→ centroid-only cover, as intended).
    """
    if reader is not None and geom.get("has_geom"):
        key = geom.get("geom_ref") or f"{place_id}_{geometry_index}"
        try:
            gj = reader.get(key)
        except Exception:
            gj = None
        # Accept GeometryCollections (which carry "geometries", not
        # "coordinates") — antimeridian-spanning features are stored that way and
        # compute_h3_fields handles them. Rejecting them here would silently drop
        # back to the hull/centroid cover for those large features.
        if isinstance(gj, dict) and gj.get("type") and (gj.get("coordinates") or gj.get("geometries")):
            return gj
    return select_h3_cover_geometry(geom, geom.get("hull"))


def _extract_stage_dir(namespace: str) -> Path:
    """Resolve the snapshot directory H3 should read from.

    Preference order per namespace:

    * OSM/OHM → ``boundary_merged/`` once boundary completion is done.
    * gn/wd  → ``update_merged/`` once the Phase 3 update patch has been
      collapsed into the snapshot.
    * everything else → ``extract/``.
    """
    base = Path(STAGED_BASE_DIR) / namespace
    if namespace in BOUNDARY_REQUIRED_NAMESPACES:
        boundary_merged = base / "boundary_merged"
        if boundary_merged.exists():
            return boundary_merged
    if namespace in UPDATE_PATCH_NAMESPACES:
        update_merged = base / "update_merged"
        if update_merged.exists():
            return update_merged
    return base / "extract"


def _iter_extract_docs(namespace: str) -> Iterable[dict[str, Any]]:
    extract_dir = _extract_stage_dir(namespace)
    parquet_path = extract_dir / "places.parquet"
    jsonl_path = extract_dir / "places.jsonl"

    if parquet_path.exists():
        parquet = pq.ParquetFile(parquet_path)
        for batch in parquet.iter_batches(batch_size=500):
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

    raise FileNotFoundError(f"No staged extract found for namespace '{namespace}'")


def _build_h3_patch(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, int, int]:
    place_id = doc.get("place_id")
    geometries = doc.get("geometries") or []
    if not place_id or not isinstance(geometries, list):
        return None, 0, 0

    updates: list[dict[str, Any]] = []
    geom_seen = 0
    geom_with_h3 = 0
    reader = get_geom_reader()
    for idx, geom in enumerate(geometries):
        if not isinstance(geom, dict):
            continue
        geom_seen += 1
        rp = geom.get("repr_point") or {}
        lon = rp.get("lon")
        lat = rp.get("lat")
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            continue

        # `.get(key, default)` does not help here: a Parquet round trip
        # materialises every schema field, so geometry_index is
        # PRESENT-and-null rather than absent, and the default never
        # fires. That reached h3_merge as int(None) and killed the wd
        # chain the first time update_merge ever ran.
        gi = geom.get("geometry_index")
        if gi is None:
            gi = idx
        # Sub-cell features cannot span more than the centroid cell, so skip the
        # geom-store read + polyfill and derive the centroid-only cover directly
        # (identical output). Only features large enough to produce a multi-cell
        # cover pay the full cost.
        maxdim = bbox_maxdim_deg(geom.get("bounds"))
        if maxdim is not None and maxdim < H3_SUBCELL_BBOX_DEG:
            h3_geom = None
        else:
            h3_geom = cover_geometry_for(geom, place_id, gi, reader)
        h3_centroid, h3_cover = compute_h3_fields(
            lon=float(lon),
            lat=float(lat),
            geojson_geom=h3_geom,
        )
        if not h3_centroid:
            continue

        geom_with_h3 += 1
        updates.append(
            {
                "geometry_index": gi,
                "h3_centroid": h3_centroid,
                "h3_cover": h3_cover,
            }
        )

    if not updates:
        return None, geom_seen, geom_with_h3

    return {"place_id": place_id, "geometries": updates}, geom_seen, geom_with_h3


def _resolve_namespace(manifest: dict[str, Any], explicit_namespace: str | None, array_task_id: int | None) -> str:
    if explicit_namespace:
        return explicit_namespace

    namespaces = manifest.get("selected_namespaces", [])
    if not namespaces:
        raise ValueError("Run manifest has no selected_namespaces")

    if array_task_id is None:
        raise ValueError("Missing namespace selector: pass --namespace or --array-task-id")

    if array_task_id < 0 or array_task_id >= len(namespaces):
        raise IndexError(
            f"Array task id {array_task_id} out of range for {len(namespaces)} selected namespaces"
        )
    return namespaces[array_task_id]


def run_h3_stage(
    *,
    run_id: str,
    manifest_path: Path,
    namespace: str,
    max_docs: int | None = None,
    slurm_job_id: str | None = None,
    slurm_array_task_id: str | None = None,
) -> dict[str, Any]:
    if namespace in BOUNDARY_REQUIRED_NAMESPACES:
        manifest = load_run_manifest(manifest_path)
        boundary_merge_status = (
            manifest.get("namespaces", {})
            .get(namespace, {})
            .get("stages", {})
            .get("boundary_merge")
        )
        if boundary_merge_status != "completed":
            raise RuntimeError(
                f"boundary_merge must be completed before H3 for namespace '{namespace}' "
                f"(got: {boundary_merge_status})"
            )

    out_dir = Path(STAGED_BASE_DIR) / namespace / "h3"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "places.h3.jsonl"

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="h3-stage",
        status="running",
        stage="h3",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="h3_stage",
        status="running",
        namespace=namespace,
        stage="h3",
        slurm_job_id=slurm_job_id,
        slurm_array_task_id=slurm_array_task_id,
    )
    update_namespace_stage_status(manifest_path, namespace, "h3", "running")

    docs_seen = 0
    docs_patched = 0
    geometries_seen = 0
    geometries_with_h3 = 0

    _started_at = datetime.now(timezone.utc)

    # Single-process loop. An earlier prototype handed each doc to a
    # ``multiprocessing.Pool(spawn)`` of size ``SLURM_CPUS_PER_TASK``, but
    # benchmarking on PeriodO (po) showed parallel = serial+area-aware to
    # within 6%: the spawn-mode worker startup + per-doc pickle overhead
    # cancels out the per-doc polyfill speedup, while the single-threaded
    # parent (read-jsonl + json.dumps + write) is the throughput ceiling.
    # The area-aware ``_polyfill_adaptive`` (helpers.py) is where the real
    # 3.85× speedup comes from for polygon-heavy gazetteers.
    with out_path.open("w", encoding="utf-8") as fh:
        for doc in _iter_extract_docs(namespace):
            patch, seen, with_h3 = _build_h3_patch(doc)
            geometries_seen += seen
            geometries_with_h3 += with_h3
            docs_seen += 1
            if patch:
                fh.write(json.dumps(patch, ensure_ascii=True) + "\n")
                docs_patched += 1
            if max_docs is not None and docs_seen >= max_docs:
                break

    metrics = {
        "docs_seen": docs_seen,
        "docs_patched": docs_patched,
        "geometries_seen": geometries_seen,
        "geometries_with_h3": geometries_with_h3,
        "patch_path": str(out_path),
    }

    _finished_at = datetime.now(timezone.utc)
    _wall_seconds = (_finished_at - _started_at).total_seconds()
    try:
        record_script_wall_time(
            namespace=namespace,
            script_id="h3-stage",
            run_id=run_id,
            started_at=_started_at.isoformat(),
            finished_at=_finished_at.isoformat(),
            wall_seconds=_wall_seconds,
            status="completed",
            slurm_job_id=slurm_job_id,
            extra={"geometries_with_h3": geometries_with_h3, "docs_patched": docs_patched},
        )
    except Exception as _e:
        pass  # Non-fatal — do not abort H3 stage on history write failure

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="h3-stage",
        status="completed",
        stage="h3",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="h3_stage",
        status="completed",
        namespace=namespace,
        stage="h3",
        slurm_job_id=slurm_job_id,
        slurm_array_task_id=slurm_array_task_id,
        details=metrics,
    )
    update_namespace_stage_status(manifest_path, namespace, "h3", "completed", metrics=metrics)

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run staged H3 derivation for one namespace")
    parser.add_argument("--run-id", required=True, help="Run ID (matches staged/runs/<run_id>.json)")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    parser.add_argument("--namespace", help="Namespace to process (if omitted, derive from array task id)")
    parser.add_argument("--array-task-id", type=int, help="Zero-based array task id")
    parser.add_argument("--max-docs", type=int, help="Optional cap for smoke testing")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(runs_dir=STAGED_RUNS_DIR, run_id=args.run_id)
    )
    if not manifest_path.exists():
        raise FileNotFoundError(f"Run manifest not found: {manifest_path}")

    manifest = load_run_manifest(manifest_path)

    task_id = args.array_task_id
    if task_id is None and os.getenv("SLURM_ARRAY_TASK_ID"):
        task_id = int(os.getenv("SLURM_ARRAY_TASK_ID", "0"))

    namespace = _resolve_namespace(manifest, args.namespace, task_id)

    slurm_job_id = os.getenv("SLURM_JOB_ID")
    slurm_array_task_id = os.getenv("SLURM_ARRAY_TASK_ID")

    run_h3_stage(
        run_id=args.run_id,
        manifest_path=manifest_path,
        namespace=namespace,
        max_docs=args.max_docs,
        slurm_job_id=slurm_job_id,
        slurm_array_task_id=slurm_array_task_id,
    )


if __name__ == "__main__":
    main()


