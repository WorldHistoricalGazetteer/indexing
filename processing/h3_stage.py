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

from processing.helpers import compute_h3_fields, select_h3_cover_geometry
from processing.settings import STAGED_BASE_DIR, STAGED_RUN_MANIFEST_FILE_TEMPLATE, STAGED_RUNS_DIR
from processing.stage_writers import record_script_wall_time, write_runtime_history_event, write_stage_event
from processing.staging_contract import UPDATE_PATCH_NAMESPACES
from processing.staging_orchestrator import load_run_manifest, update_namespace_stage_status


BOUNDARY_REQUIRED_NAMESPACES = {"osm", "ohm"}


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
    for idx, geom in enumerate(geometries):
        if not isinstance(geom, dict):
            continue
        geom_seen += 1
        rp = geom.get("repr_point") or {}
        lon = rp.get("lon")
        lat = rp.get("lat")
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            continue

        h3_geom = select_h3_cover_geometry(geom, geom.get("hull"))
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
                "geometry_index": geom.get("geometry_index", idx),
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

    # Parallel polyfill: hand each doc to a worker pool sized from the Slurm
    # allocation (SLURM_CPUS_PER_TASK falls back to os.cpu_count() locally).
    # Most of the wall time is the per-doc polyfill, which is a pure-CPU
    # Python call that releases the GIL only intermittently — so processes
    # rather than threads. ``imap_unordered`` keeps the writer happy without
    # waiting for slow docs to drain.
    n_workers = int(os.environ.get(
        "SLURM_CPUS_PER_TASK",
        str(max(1, (os.cpu_count() or 1) - 1)),
    ))
    n_workers = max(1, n_workers)

    if n_workers <= 1:
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
    else:
        import multiprocessing as _mp
        # ``spawn`` is the safe default on CRC (avoids fork+threading
        # interactions with Shapely/GEOS handles inherited from the parent).
        ctx = _mp.get_context("spawn")
        with out_path.open("w", encoding="utf-8") as fh, \
                ctx.Pool(processes=n_workers) as pool:
            iterator = _iter_extract_docs(namespace)
            if max_docs is not None:
                # Cap upstream so workers don't process beyond the limit.
                def _limited(src):
                    for i, d in enumerate(src):
                        if i >= max_docs: break
                        yield d
                iterator = _limited(iterator)
            # chunksize tuned for typical doc-level work — small enough to
            # keep workers fed, large enough to amortise IPC.
            for patch, seen, with_h3 in pool.imap_unordered(
                _build_h3_patch, iterator, chunksize=64,
            ):
                geometries_seen += seen
                geometries_with_h3 += with_h3
                docs_seen += 1
                if patch:
                    fh.write(json.dumps(patch, ensure_ascii=True) + "\n")
                    docs_patched += 1

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


