#!/usr/bin/env python3
"""Boundary completion stage (staged pipeline, no Elasticsearch).

This stage assembles full relation boundary geometry from OSM/OHM PBF and writes
namespace-scoped boundary patches to staged artefacts.

Two modes:

* **Standalone mode** (legacy): processes every boundary relation in the PBF
  in a single task. Writes
  ``{STAGED_BASE_DIR}/{namespace}/boundary/places.boundary.jsonl``.

* **Shard mode**: planner writes a ``shard_map.json`` assigning relation IDs
  to N shards. Each Slurm array task invokes this script with
  ``--shard-id I --shard-map PATH`` and processes only its assigned
  relations, writing
  ``{STAGED_BASE_DIR}/{namespace}/boundary/places.boundary.shard_<I>.jsonl``.
  The companion ``processing.boundary_stage_finalize`` concatenates the
  per-shard outputs into the canonical ``places.boundary.jsonl`` once the
  array completes.

Each patch record carries ``place_id`` and an ``update_doc`` payload that is
merged later by ``processing.boundary_merge`` into staged place snapshots before
H3/ccode processing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from processing.geom_store import GeomStoreWriter, configure_module_writer
from processing.osm_boundary_geometry import (
    BoundaryPassProcessor,
    _ProgressReporter,
    _find_osmium,
    _require_osmium,
    prefilter_boundaries,
)
from processing.settings import (
    DATA_DIR,
    GEOM_STORE_STAGING_DIR,
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


def _load_shard_relation_ids(shard_map_path: Path, shard_id: int) -> set[int]:
    """Return the set of relation IDs assigned to a given shard."""
    payload = json.loads(shard_map_path.read_text(encoding="utf-8"))
    for shard in payload.get("shards", []):
        if shard.get("shard_id") == shard_id:
            return {int(r) for r in shard.get("relation_ids", [])}
    raise KeyError(
        f"shard_id={shard_id} not found in shard map {shard_map_path} "
        f"(available shards: {[s.get('shard_id') for s in payload.get('shards', [])]})"
    )


def _extract_shard_subset_pbf(
    *,
    source_pbf: Path,
    relation_ids: set[int],
    out_dir: Path,
    namespace: str,
    shard_id: int,
) -> Path:
    """Use ``osmium getid -r`` to project ``source_pbf`` to just ``relation_ids``.

    This drops the prefiltered PBF down to only the relations this shard
    owns plus their referenced ways and nodes. The result is small (KBs to
    MBs for small shards), so the subsequent osmium area assembly does
    only the work this shard's relations require.
    """
    osmium_tool, osmium_env = _find_osmium()
    if not osmium_tool:
        raise RuntimeError(
            "osmium-tool not available; required for shard PBF subset extraction"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    id_list_path = out_dir / f"{namespace}_shard_{shard_id}_ids.txt"
    subset_pbf = out_dir / f"{namespace}_shard_{shard_id}.osm.pbf"

    with id_list_path.open("w", encoding="utf-8") as fh:
        for rid in sorted(relation_ids):
            fh.write(f"r{rid}\n")

    started = time.time()
    print(
        f"  Subsetting PBF to shard {shard_id}: "
        f"{len(relation_ids):,} relations + referenced ways/nodes"
    )
    try:
        result = subprocess.run(
            [
                osmium_tool, "getid",
                str(source_pbf),
                "-i", str(id_list_path),
                "-r",  # add referenced (member ways + their nodes)
                "-o", str(subset_pbf),
                "--overwrite",
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=osmium_env,
            timeout=3600,
        )
    finally:
        try:
            id_list_path.unlink()
        except FileNotFoundError:
            pass

    if result.returncode != 0 or not subset_pbf.exists():
        raise RuntimeError(
            f"osmium getid failed (exit={result.returncode}) for shard {shard_id}"
        )
    elapsed = time.time() - started
    size_mb = subset_pbf.stat().st_size / 1e6
    print(f"  Shard subset PBF: {size_mb:.1f} MB ({elapsed:.0f}s)")
    return subset_pbf


def run_boundary_stage(
    *,
    run_id: str,
    namespace: str,
    pbf_file: Path,
    manifest_path: Path | None = None,
    max_areas: int | None = None,
    shard_id: int | None = None,
    shard_map_path: Path | None = None,
    prefiltered_pbf: Path | None = None,
) -> dict[str, Any]:
    """Assemble boundary geometry and emit staged boundary patches.

    This function does not touch Elasticsearch.

    When ``shard_id`` and ``shard_map_path`` are both provided, the function
    runs in shard mode: it subsets the (prefiltered) PBF to just the
    relations assigned to ``shard_id`` and writes its output to a
    shard-suffixed JSONL. The companion
    ``processing.boundary_stage_finalize`` concatenates per-shard outputs
    into the canonical ``places.boundary.jsonl`` once the full array
    completes.
    """
    if namespace not in {"osm", "ohm"}:
        raise ValueError("Boundary stage currently supports only 'osm' and 'ohm'")

    if not pbf_file.exists():
        raise FileNotFoundError(f"PBF file not found: {pbf_file}")

    is_shard = shard_id is not None and shard_map_path is not None
    if (shard_id is None) != (shard_map_path is None):
        raise ValueError("--shard-id and --shard-map must be used together")

    boundary_dir = Path(STAGED_BASE_DIR) / namespace / "boundary"
    boundary_dir.mkdir(parents=True, exist_ok=True)
    patch_filename = (
        f"places.boundary.shard_{shard_id}.jsonl" if is_shard else "places.boundary.jsonl"
    )
    patch_path = boundary_dir / patch_filename

    # In shard mode, only the finalize step flips the manifest stage status —
    # individual shard tasks should not race on the manifest write.
    if not is_shard and manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "boundary", "running")

    script_id = "boundary-stage" if not is_shard else f"boundary-stage-shard-{shard_id}"

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id=script_id,
        status="running",
        stage="boundary",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="boundary_stage" if not is_shard else "boundary_stage_shard",
        status="running",
        namespace=namespace,
        stage="boundary",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
        slurm_array_task_id=os.environ.get("SLURM_ARRAY_TASK_ID"),
    )

    extracted_count = 0
    failed_count = 0
    areas_seen = 0

    scratch = os.environ.get("SLURM_SCRATCH") or os.environ.get("TMPDIR")
    filter_dir = scratch if (scratch and os.path.isdir(scratch)) else os.environ.get("TMPDIR", "/tmp")

    # Optional pre-filter to speed up area assembly. If the planner already
    # produced a prefiltered PBF on shared storage (passed via
    # ``prefiltered_pbf``), reuse it and skip the in-worker prefilter — this
    # avoids 32 simultaneous workers each doing a 2 h+ prefilter pass on the
    # 92 GB OSM PBF, which was the cause of the SMP-array timeouts.
    filtered_pbf_path: str | None = None
    shard_subset_path: Path | None = None
    processing_pbf = str(pbf_file)
    if prefiltered_pbf is not None and prefiltered_pbf.exists():
        print(f"Reusing planner-produced prefiltered PBF: {prefiltered_pbf} "
              f"({prefiltered_pbf.stat().st_size / 1e9:.1f} GB) — skipping in-worker prefilter")
        processing_pbf = str(prefiltered_pbf)
    else:
        filtered_path = os.path.join(filter_dir, f"{namespace}_boundary_stage_filtered.osm.pbf")
        prefiltered = prefilter_boundaries(pbf_file, filtered_path)
        if prefiltered:
            processing_pbf = prefiltered
            filtered_pbf_path = prefiltered

    # In shard mode, hold the assigned relation IDs so the area-iteration
    # loop can drop osmium-leaked siblings (see comment at iteration site).
    shard_relation_ids: set[int] | None = None
    if is_shard:
        shard_relation_ids = _load_shard_relation_ids(shard_map_path, shard_id)
        if not shard_relation_ids:
            print(f"  Shard {shard_id} has no relation_ids — exiting cleanly")
        else:
            shard_subset_path = _extract_shard_subset_pbf(
                source_pbf=Path(processing_pbf),
                relation_ids=shard_relation_ids,
                out_dir=Path(filter_dir),
                namespace=namespace,
                shard_id=shard_id,
            )
            processing_pbf = str(shard_subset_path)

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

    # Per-shard geom_store staging file. Concurrent shards must not write to
    # the same ``*.bin`` so each shard gets its own namespace key, picked up
    # by ``consolidate_geom_store`` alongside the per-authority files. In
    # non-shard mode there's a single writer for the namespace.
    if is_shard:
        geom_writer_ns = f"{namespace}_boundary_shard_{shard_id}"
    else:
        geom_writer_ns = f"{namespace}_boundary"
    geom_writer = GeomStoreWriter(GEOM_STORE_STAGING_DIR, geom_writer_ns)
    configure_module_writer(geom_writer)

    def signal_handler(sig, frame):  # pragma: no cover - runtime interruption path
        out.flush()
        out.close()
        configure_module_writer(None)
        geom_writer.close()
        if not is_shard and manifest_path and manifest_path.exists():
            update_namespace_stage_status(manifest_path, namespace, "boundary", "failed", error="signal")
        raise SystemExit(130)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    started = time.time()
    leaked_areas_skipped = 0
    try:
        idx_type = "flex_mem"
        fp = osmium.FileProcessor(processing_pbf).with_locations(idx_type).with_areas()
        with _ProgressReporter(processor, interval=30):
            for obj in fp:
                if not (isinstance(obj, osmium.osm.Area) and not obj.from_way()):
                    continue
                # ``osmium getid -r`` follows relation→relation member refs
                # (subarea, admin_centre, label, etc.), so the per-shard
                # subset PBF contains many relations that belong to other
                # shards. Without this filter every shard re-assembles the
                # leaked siblings, multiplying the work and producing
                # duplicate boundary patches across shards.
                if shard_relation_ids is not None and obj.orig_id() not in shard_relation_ids:
                    leaked_areas_skipped += 1
                    continue
                processor.process_area(obj)
                areas_seen += 1
                if max_areas is not None and areas_seen >= max_areas:
                    break
    except Exception:
        failed_count += 1
        raise
    finally:
        out.close()
        configure_module_writer(None)
        geom_writer.close()
        if filtered_pbf_path and os.path.exists(filtered_pbf_path):
            try:
                os.remove(filtered_pbf_path)
            except OSError:
                pass
        if shard_subset_path is not None and shard_subset_path.exists():
            try:
                shard_subset_path.unlink()
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
    if is_shard:
        metrics["shard_id"] = shard_id
        metrics["leaked_areas_skipped"] = leaked_areas_skipped

    # Shard tasks should not race on the per-namespace manifest stage status —
    # the finalize step is responsible for flipping `boundary` to "completed".
    if not is_shard and manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "boundary", "completed", metrics=metrics)

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id=script_id,
        status="completed",
        stage="boundary",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="boundary_stage" if not is_shard else "boundary_stage_shard",
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
    parser.add_argument("--shard-id", type=int,
                        help="Process only this shard's relations (requires --shard-map)")
    parser.add_argument("--shard-map",
                        help="Path to shard_map.json from boundary_shard_planner")
    parser.add_argument("--prefiltered-pbf",
                        help="Path to a pre-built boundary-only PBF (from "
                             "boundary_shard_planner --keep-prefilter). "
                             "When set, skips the in-worker tags-filter pass.")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(runs_dir=STAGED_RUNS_DIR, run_id=args.run_id)
    )

    pbf_file = Path(args.pbf_file) if args.pbf_file else _default_pbf_for(args.namespace)

    shard_map_path = Path(args.shard_map) if args.shard_map else None
    prefiltered_pbf = Path(args.prefiltered_pbf) if args.prefiltered_pbf else None

    metrics = run_boundary_stage(
        run_id=args.run_id,
        namespace=args.namespace,
        pbf_file=pbf_file,
        manifest_path=manifest_path if manifest_path.exists() else None,
        max_areas=args.max_areas,
        shard_id=args.shard_id,
        shard_map_path=shard_map_path,
        prefiltered_pbf=prefiltered_pbf,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


