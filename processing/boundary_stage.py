#!/usr/bin/env python3
"""Boundary completion stage (staged pipeline, no Elasticsearch).

This stage assembles full relation boundary geometry from OSM/OHM PBF and writes
namespace-scoped boundary patches to staged artefacts.

Output:
  {STAGED_BASE_DIR}/{namespace}/boundary/places.boundary.jsonl

Each patch record carries ``place_id`` and an ``update_doc`` payload that is
merged later by ``processing.boundary_merge`` into staged place snapshots before
H3/ccode processing.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from processing.osm_boundary_geometry import (
    BoundaryPassProcessor,
    _ProgressReporter,
    _require_osmium,
    prefilter_boundaries,
)
from processing.settings import (
    DATA_DIR,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import write_runtime_history_event, write_stage_event
from processing.staging_orchestrator import update_namespace_stage_status


def _strip_h3_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove H3 fields from boundary patch payload.

    H3 is computed later in Batch 6 from final geometry snapshots, so boundary
    stage must not emit any H3-derived fields.
    """
    clean = dict(doc)
    clean.pop("h3_centroid", None)
    clean.pop("h3_cover", None)
    return clean


def _default_pbf_for(namespace: str) -> Path:
    if namespace == "osm":
        return Path(DATA_DIR) / "authorities" / "osm" / "planet-latest.osm.pbf"
    if namespace == "ohm":
        return Path(DATA_DIR) / "authorities" / "ohm" / "planet-latest.osm.pbf"
    raise ValueError(f"Unsupported namespace: {namespace}")


def run_boundary_stage(
    *,
    run_id: str,
    namespace: str,
    pbf_file: Path,
    manifest_path: Path | None = None,
    max_areas: int | None = None,
) -> dict[str, Any]:
    """Assemble boundary geometry and emit staged boundary patches.

    This function does not touch Elasticsearch.
    """
    if namespace not in {"osm", "ohm"}:
        raise ValueError("Boundary stage currently supports only 'osm' and 'ohm'")

    if not pbf_file.exists():
        raise FileNotFoundError(f"PBF file not found: {pbf_file}")

    boundary_dir = Path(STAGED_BASE_DIR) / namespace / "boundary"
    boundary_dir.mkdir(parents=True, exist_ok=True)
    patch_path = boundary_dir / "places.boundary.jsonl"

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "boundary", "running")

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="boundary-stage",
        status="running",
        stage="boundary",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="boundary_stage",
        status="running",
        namespace=namespace,
        stage="boundary",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
        slurm_array_task_id=os.environ.get("SLURM_ARRAY_TASK_ID"),
    )

    extracted_count = 0
    failed_count = 0
    areas_seen = 0

    # Optional pre-filter to speed up area assembly.
    filtered_pbf_path: str | None = None
    processing_pbf = str(pbf_file)
    scratch = os.environ.get("SLURM_SCRATCH") or os.environ.get("TMPDIR")
    filter_dir = scratch if (scratch and os.path.isdir(scratch)) else os.environ.get("TMPDIR", "/tmp")
    filtered_path = os.path.join(filter_dir, f"{namespace}_boundary_stage_filtered.osm.pbf")
    prefiltered = prefilter_boundaries(pbf_file, filtered_path)
    if prefiltered:
        processing_pbf = prefiltered
        filtered_pbf_path = prefiltered

    osmium = _require_osmium()

    out = patch_path.open("w", encoding="utf-8")

    def add_patch(place_id: str, update_doc: dict[str, Any], upsert_doc: dict[str, Any]) -> None:
        nonlocal extracted_count
        payload = {
            "place_id": place_id,
            "update_doc": _strip_h3_fields(update_doc),
            "upsert_doc": _strip_h3_fields(upsert_doc),
            "source": "boundary_stage",
        }
        out.write(json.dumps(payload, ensure_ascii=True) + "\n")
        extracted_count += 1

    processor = BoundaryPassProcessor(add_patch, namespace)

    def signal_handler(sig, frame):  # pragma: no cover - runtime interruption path
        out.flush()
        out.close()
        if manifest_path and manifest_path.exists():
            update_namespace_stage_status(manifest_path, namespace, "boundary", "failed", error="signal")
        raise SystemExit(130)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    started = time.time()
    try:
        idx_type = "flex_mem"
        fp = osmium.FileProcessor(processing_pbf).with_locations(idx_type).with_areas()
        with _ProgressReporter(processor, interval=30):
            for obj in fp:
                if isinstance(obj, osmium.osm.Area) and not obj.from_way():
                    processor.process_area(obj)
                    areas_seen += 1
                    if max_areas is not None and areas_seen >= max_areas:
                        break
    except Exception:
        failed_count += 1
        raise
    finally:
        out.close()
        if filtered_pbf_path and os.path.exists(filtered_pbf_path):
            try:
                os.remove(filtered_pbf_path)
            except OSError:
                pass

    elapsed = time.time() - started
    metrics = {
        "areas_seen": processor.areas_seen,
        "patches_written": extracted_count,
        "tag_rejected": processor.tag_rejected,
        "geom_errors": processor.geom_errors,
        "elapsed_seconds": round(elapsed, 1),
        "patch_path": str(patch_path),
    }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "boundary", "completed", metrics=metrics)

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="boundary-stage",
        status="completed",
        stage="boundary",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="boundary_stage",
        status="completed",
        namespace=namespace,
        stage="boundary",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
        slurm_array_task_id=os.environ.get("SLURM_ARRAY_TASK_ID"),
        details=metrics,
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run staged boundary completion for osm/ohm")
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--namespace", required=True, choices=["osm", "ohm"], help="Namespace")
    parser.add_argument("--pbf-file", help="Override PBF path")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    parser.add_argument("--max-areas", type=int, help="Optional area cap for smoke testing")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(runs_dir=STAGED_RUNS_DIR, run_id=args.run_id)
    )

    pbf_file = Path(args.pbf_file) if args.pbf_file else _default_pbf_for(args.namespace)

    metrics = run_boundary_stage(
        run_id=args.run_id,
        namespace=args.namespace,
        pbf_file=pbf_file,
        manifest_path=manifest_path if manifest_path.exists() else None,
        max_areas=args.max_areas,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


