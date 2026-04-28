#!/usr/bin/env python3
"""Finalize the sharded boundary stage.

After the boundary-stage Slurm array completes, each shard has written its
own ``places.boundary.shard_<I>.jsonl``. This module concatenates them into
the canonical ``places.boundary.jsonl`` (which
``processing.boundary_merge`` reads), aggregates per-shard metrics, and
flips the manifest's ``boundary`` stage to ``completed``.

Run:

    python -m processing.boundary_stage_finalize \\
        --run-id <RUN_ID> \\
        --namespace ohm \\
        --shard-map /path/to/shard_map.json \\
        --manifest-path /path/to/manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import write_runtime_history_event, write_stage_event
from processing.staging_orchestrator import update_namespace_stage_status


def _shard_jsonl_paths(boundary_dir: Path, shard_count: int) -> list[Path]:
    paths: list[Path] = []
    for shard_id in range(shard_count):
        p = boundary_dir / f"places.boundary.shard_{shard_id}.jsonl"
        if p.exists():
            paths.append(p)
    return paths


def run_finalize(
    *,
    run_id: str,
    namespace: str,
    shard_map_path: Path,
    manifest_path: Path | None = None,
    delete_shard_files: bool = True,
) -> dict[str, Any]:
    payload = json.loads(shard_map_path.read_text(encoding="utf-8"))
    shard_count = int(payload.get("shard_count") or len(payload.get("shards", [])))
    if shard_count <= 0:
        raise ValueError(f"Invalid shard_count in {shard_map_path}: {shard_count}")

    boundary_dir = Path(STAGED_BASE_DIR) / namespace / "boundary"
    boundary_dir.mkdir(parents=True, exist_ok=True)
    out_path = boundary_dir / "places.boundary.jsonl"

    shard_paths = _shard_jsonl_paths(boundary_dir, shard_count)
    missing = [
        i for i in range(shard_count)
        if not (boundary_dir / f"places.boundary.shard_{i}.jsonl").exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"Missing shard JSONLs for shard_ids={missing} under {boundary_dir}"
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="boundary-stage-finalize",
        status="running",
        stage="boundary",
    )

    started = time.time()
    total_records = 0
    with out_path.open("w", encoding="utf-8") as out_fh:
        for shard_path in shard_paths:
            with shard_path.open("r", encoding="utf-8") as in_fh:
                for line in in_fh:
                    if not line.strip():
                        continue
                    out_fh.write(line if line.endswith("\n") else line + "\n")
                    total_records += 1

    elapsed = time.time() - started

    if delete_shard_files:
        for shard_path in shard_paths:
            try:
                shard_path.unlink()
            except OSError:
                pass

    metrics = {
        "patch_path": str(out_path),
        "shard_count": shard_count,
        "patches_written": total_records,
        "finalize_seconds": round(elapsed, 1),
    }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "boundary", "completed", metrics=metrics
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="boundary-stage-finalize",
        status="completed",
        stage="boundary",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="boundary_stage_finalize",
        status="completed",
        namespace=namespace,
        stage="boundary",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
        details=metrics,
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Concatenate sharded boundary outputs into canonical jsonl"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--namespace", required=True, choices=["osm", "ohm"])
    parser.add_argument("--shard-map", required=True,
                        help="Path to shard_map.json from boundary_shard_planner")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    parser.add_argument("--keep-shard-files", action="store_true",
                        help="Keep per-shard jsonl files after concatenation")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
            runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
        )
    )

    metrics = run_finalize(
        run_id=args.run_id,
        namespace=args.namespace,
        shard_map_path=Path(args.shard_map),
        manifest_path=manifest_path if manifest_path.exists() else None,
        delete_shard_files=not args.keep_shard_files,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
