#!/usr/bin/env python
"""Submit a Slurm array job for staged tile generation (Batch 10).

Tile generation reads only staged snapshots and the geometry store — it does
not touch Elasticsearch — so this submission can run in parallel with other
post-extract work and **before** the global barrier (Batch 8).

The array is **bucket-driven**: each task owns exactly one tile bucket
(``osm_admin``, ``ohm_admin``, ``osm_misc``, ``po``, ``clio``, ``nl``) and
streams its full list of contributing namespaces from staged snapshots. This
keeps each output ``.geojsonl`` / ``.mbtiles`` owned by a single writer and
correctly handles the mixed ``osm_misc`` bucket (one task that streams both
``osm`` and ``ohm`` misc-boundary records).

Per-bucket prerequisites:

* ``osm_admin`` / ``osm_misc``  — every contributing OSM/OHM namespace must
  have ``boundary_merge`` complete (full polygons present in the snapshot).
* ``ohm_admin``                 — same gate, restricted to ``ohm``.
* ``po`` / ``clio`` / ``nl``    — the namespace's ``extract`` must be complete.

Usage::

    python -m processing.submit_tiles_slurm --run-id <RUN_ID> [--dry-run]
    python -m processing.submit_tiles_slurm --run-id <RUN_ID> --depend-on <JOB>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO = Path(os.environ.get("WHG_REPO", str(_REPO_ROOT)))
_CONDA_ENV = os.environ.get("CONDA_ENV", "whg")
_CONDA_SH = os.environ.get(
    "CONDA_SH",
    "/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh",
)

sys.path.insert(0, str(_REPO))

from processing.generate_tiles import TILE_BUCKETS  # noqa: E402
from processing.settings import (  # noqa: E402
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import estimate_wall_time_seconds  # noqa: E402
from processing.staging_orchestrator import load_run_manifest  # noqa: E402


# Tile generation is IO-bound on the geom store reads and CPU-bound on
# tippecanoe; 12 h is a generous default for the largest namespace (osm)
# when the runtime-history file is empty (first run).
_DEFAULT_WALL_SECONDS = 12 * 3_600
_QOS = "htc-htc-s"  # ≤ 1 day tier; tile generation should never need longer


def _estimate_wall_for_buckets(buckets: list[str]) -> int:
    """Take the max of the per-(namespace, 'tiles') estimates across all
    contributing namespaces. Falls back to ``_DEFAULT_WALL_SECONDS`` when no
    namespace has wall-time history."""
    contributors: set[str] = set()
    for bucket in buckets:
        contributors.update(TILE_BUCKETS.get(bucket, ()))
    if not contributors:
        return _DEFAULT_WALL_SECONDS
    estimates = [
        estimate_wall_time_seconds(ns, "tiles", default=_DEFAULT_WALL_SECONDS)
        for ns in contributors
    ]
    return max(estimates)


def _seconds_to_slurm_time(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _required_stage_for(namespace: str) -> str:
    """Per-namespace prerequisite stage that must be completed before tiles."""
    if namespace in ("osm", "ohm"):
        return "boundary_merge"
    return "extract"


def _eligible_buckets(manifest: dict) -> list[str]:
    """Return tile buckets whose every contributing namespace is ready.

    Skips a bucket entirely (rather than partially) when any contributing
    namespace's prerequisite stage isn't complete — partial buckets would
    misrepresent the world (e.g. ``osm_misc`` without ``ohm`` would be
    silently incomplete).

    Buckets whose ``tiles`` stage is already complete *for every contributing
    namespace* are skipped for idempotent reruns.
    """
    eligible: list[str] = []
    ns_data = manifest.get("namespaces", {})
    for bucket, contributors in TILE_BUCKETS.items():
        ready = True
        already_done = True
        for ns in contributors:
            stages = ns_data.get(ns, {}).get("stages", {})
            if stages.get(_required_stage_for(ns)) != "completed":
                ready = False
                break
            if stages.get("tiles") != "completed":
                already_done = False
        if ready and not already_done:
            eligible.append(bucket)
    return eligible


def _write_array_map(buckets: list[str], work_dir: Path) -> Path:
    array_map = {str(i): bucket for i, bucket in enumerate(buckets)}
    map_path = work_dir / "tiles_array_map.json"
    map_path.write_text(json.dumps(array_map, indent=2), encoding="utf-8")
    return map_path


def _build_sbatch_script(
    *,
    run_id: str,
    buckets: list[str],
    manifest_path: Path,
    array_map_path: Path,
    depend_on: str | None,
    output_dir: Path | None,
) -> str:
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-tiles-{run_id}-%A_%a"

    array_end = len(buckets) - 1
    slurm_time = _seconds_to_slurm_time(_estimate_wall_for_buckets(buckets))

    output_arg = ""
    if output_dir is not None:
        output_arg = f" --output-dir {output_dir}"

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-tiles-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --array=0-{array_end}",
        f"#SBATCH --time={slurm_time}",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={_QOS}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=4",
        "#SBATCH --mem=16G",
    ]
    if depend_on:
        lines.append(f"#SBATCH --dependency=afterok:{depend_on}")
    lines.extend([
        "",
        "set -eo pipefail",
        f"source {_CONDA_SH}",
        f"conda activate {_CONDA_ENV}",
        f"cd {_REPO}",
        "",
        f"BUCKET=$(python -c \"import json; d=json.load(open('{array_map_path}')); print(d[str($SLURM_ARRAY_TASK_ID)])\")",
        "echo \"Array task $SLURM_ARRAY_TASK_ID → bucket: $BUCKET\"",
        "",
        "python -m processing.generate_tiles \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        f"    --bucket \"$BUCKET\"{output_arg}",
    ])
    return "\n".join(lines) + "\n"


def submit(
    *,
    run_id: str,
    manifest_path: Path,
    depend_on: str | None = None,
    dry_run: bool = False,
    output_dir: Path | None = None,
) -> str | None:
    manifest = load_run_manifest(manifest_path)
    buckets = _eligible_buckets(manifest)

    if not buckets:
        print("No tile buckets eligible (prerequisites incomplete or already done).")
        return None

    print(f"Buckets to generate ({len(buckets)}): {', '.join(buckets)}")

    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    array_map_path = _write_array_map(buckets, work_dir)

    sbatch_text = _build_sbatch_script(
        run_id=run_id,
        buckets=buckets,
        manifest_path=manifest_path,
        array_map_path=array_map_path,
        depend_on=depend_on,
        output_dir=output_dir,
    )

    sbatch_path = work_dir / "tiles_array.sbatch"
    sbatch_path.write_text(sbatch_text, encoding="utf-8")
    print(f"\nSbatch script written to: {sbatch_path}")

    if dry_run:
        print("\n--- DRY RUN: sbatch script contents ---")
        print(sbatch_text)
        print("--- END ---")
        return None

    result = subprocess.run(
        ["sbatch", "-M", os.environ.get("WHG_SLURM_CLUSTER", "htc"), str(sbatch_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"sbatch failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)

    job_id = next((tok for tok in result.stdout.split() if tok.isdigit()), result.stdout.strip().split()[-1])
    print(f"Submitted Slurm array job: {job_id}  ({len(buckets)} tasks)")
    return job_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit staged tile-generation Slurm array job"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument(
        "--depend-on",
        help="Slurm job ID this array should wait on (afterok)",
    )
    parser.add_argument("--output-dir", help="Override default tile output directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print sbatch but do not submit")
    args = parser.parse_args()

    if args.manifest_path:
        manifest_path = Path(args.manifest_path)
    else:
        manifest_path = Path(
            STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
                runs_dir=STAGED_RUNS_DIR,
                run_id=args.run_id,
            )
        )

    if not manifest_path.exists():
        print(f"Run manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    submit(
        run_id=args.run_id,
        manifest_path=manifest_path,
        depend_on=args.depend_on,
        dry_run=args.dry_run,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )


if __name__ == "__main__":
    main()
