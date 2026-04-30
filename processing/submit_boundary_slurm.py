#!/usr/bin/env python3
"""Submit the sharded boundary-stage Slurm chain for OSM/OHM namespaces.

Up to four jobs are submitted with ``afterok`` dependencies:

1. **Planner** (single task) — runs ``processing.boundary_shard_planner``
   to scan the prefiltered PBF and write ``shard_map.json``.
2. **Regular workers** (Slurm array) — one task per regular shard,
   each runs ``processing.boundary_stage --shard-id N --shard-map ...``
   against its own LPT-packed subset of relations. Wall budget tuned to
   the median shard.
3. **Mega workers** (Slurm array, optional) — one task per dedicated
   mega shard (each containing a single mega-relation), with a longer
   wall budget. Skipped if ``--mega-shard-count 0``.
4. **Finalizer** (single task) — concatenates all per-shard JSONLs
   into ``places.boundary.jsonl`` and flips the manifest stage to
   completed. Depends on both worker arrays.

Per-namespace prerequisite: ``extract`` stage must be ``completed`` in the
run manifest. The PBF path is ``DATA_DIR/authorities/{ns}/planet-latest.osm.pbf``
unless overridden with ``--pbf-file``.

Usage::

    python -m processing.submit_boundary_slurm --run-id <RUN_ID> --namespace ohm
    python -m processing.submit_boundary_slurm --run-id <RUN_ID> --namespace osm \\
        --shard-count 22 --mega-shard-count 10
    python -m processing.submit_boundary_slurm --run-id <RUN_ID> --namespace ohm --dry-run
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

from processing.boundary_shard_planner import (  # noqa: E402
    COST_PROXY_NODE_COUNT,
    _COST_PROXIES,
)
from processing.settings import (  # noqa: E402
    DATA_DIR,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_orchestrator import load_run_manifest  # noqa: E402


# Defaults tuned to OHM scale; OSM is ~10× larger by member-way count and
# routes the heaviest relations to dedicated mega shards.
_DEFAULT_SHARD_COUNT = {
    "ohm": 16,
    "osm": 22,
}
_DEFAULT_MEGA_SHARD_COUNT = {
    "ohm": 0,
    "osm": 10,
}
_DEFAULT_REGULAR_WALL_HOURS = {
    "ohm": 8,
    "osm": 24,
}
# Mega shards each hold the costliest single relations (continent-scale
# admin, oceans). Wall budget is tuned to a single mega-relation's
# expected assembly time — generous because the worst-shard wall is now
# the long pole.
_DEFAULT_MEGA_WALL_HOURS = {
    "ohm": 12,
    "osm": 96,
}
_PLANNER_WALL_HOURS = {
    # OSM planet PBF (~92 GB) prefilter alone is multi-hour; previous runs
    # took 5 h 11 m → 6 h timeout, so allow 12 h of headroom for SMP node
    # variance and the keep-prefilter copy to /vast.
    "ohm": 4,
    "osm": 12,
}
_FINALIZE_WALL_HOURS = 1


def _default_pbf_for(namespace: str) -> Path:
    return Path(DATA_DIR) / "authorities" / namespace / "planet-latest.osm.pbf"


def _hours_to_slurm_time(hours: int) -> str:
    days, h = divmod(hours, 24)
    if days:
        return f"{days}-{h:02d}:00:00"
    return f"{h:02d}:00:00"


def _submit(sbatch_path: Path, *, depend_on: list[str] | None, dry_run: bool) -> str | None:
    if dry_run:
        print(f"\n--- DRY RUN: {sbatch_path.name} ---")
        print(sbatch_path.read_text(encoding="utf-8"))
        print("--- END ---")
        return None

    cmd = ["sbatch", "--parsable"]
    if depend_on:
        joined = ":".join(depend_on)
        cmd.append(f"--dependency=afterok:{joined}")
    cmd.append(str(sbatch_path))

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"sbatch failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)

    job_id = next((tok for tok in result.stdout.split() if tok.isdigit()),
                  result.stdout.strip().split()[-1])
    print(f"  Submitted: {job_id}  ({sbatch_path.name})")
    return job_id


def _build_planner_sbatch(
    *,
    run_id: str,
    namespace: str,
    pbf_file: Path,
    shard_count: int,
    mega_shard_count: int,
    cost_proxy: str,
    shard_map_path: Path,
    prefiltered_pbf_path: Path,
    work_dir: Path,
    log_dir: Path,
) -> Path:
    log_prefix = log_dir / f"whg-boundary-planner-{run_id}-%j"
    body = f"""#!/bin/bash
#SBATCH --job-name=whg-boundary-planner-{run_id}
#SBATCH --output={log_prefix}.out
#SBATCH --error={log_prefix}.err
#SBATCH --time={_hours_to_slurm_time(_PLANNER_WALL_HOURS[namespace])}
#SBATCH --partition=smp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G

set -eo pipefail
source {_CONDA_SH}
conda activate {_CONDA_ENV}
cd {_REPO}

python -m processing.boundary_shard_planner \\
    --pbf {pbf_file} \\
    --namespace {namespace} \\
    --shard-count {shard_count} \\
    --mega-shard-count {mega_shard_count} \\
    --cost-proxy {cost_proxy} \\
    --output {shard_map_path} \\
    --keep-prefilter {prefiltered_pbf_path}
"""
    sbatch_path = work_dir / f"boundary_planner_{namespace}.sbatch"
    sbatch_path.write_text(body, encoding="utf-8")
    return sbatch_path


def _build_worker_sbatch(
    *,
    run_id: str,
    namespace: str,
    pbf_file: Path,
    array_first: int,
    array_last: int,
    wall_hours: int,
    mem_gb: int,
    label: str,
    shard_map_path: Path,
    manifest_path: Path,
    prefiltered_pbf_path: Path,
    work_dir: Path,
    log_dir: Path,
) -> Path:
    log_prefix = log_dir / f"whg-boundary-{label}-{run_id}-%A_%a"
    body = f"""#!/bin/bash
#SBATCH --job-name=whg-boundary-{label}-{run_id}
#SBATCH --output={log_prefix}.out
#SBATCH --error={log_prefix}.err
#SBATCH --array={array_first}-{array_last}
#SBATCH --time={_hours_to_slurm_time(wall_hours)}
#SBATCH --partition=smp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem={mem_gb}G

set -eo pipefail
source {_CONDA_SH}
conda activate {_CONDA_ENV}
cd {_REPO}

echo "Shard task $SLURM_ARRAY_TASK_ID ({label}) for namespace {namespace}"
python -u -m processing.boundary_stage \\
    --run-id {run_id} \\
    --namespace {namespace} \\
    --pbf-file {pbf_file} \\
    --manifest-path {manifest_path} \\
    --shard-id $SLURM_ARRAY_TASK_ID \\
    --shard-map {shard_map_path} \\
    --prefiltered-pbf {prefiltered_pbf_path}
"""
    sbatch_path = work_dir / f"boundary_{label}_{namespace}.sbatch"
    sbatch_path.write_text(body, encoding="utf-8")
    return sbatch_path


def _build_finalize_sbatch(
    *,
    run_id: str,
    namespace: str,
    shard_map_path: Path,
    manifest_path: Path,
    work_dir: Path,
    log_dir: Path,
) -> Path:
    log_prefix = log_dir / f"whg-boundary-finalize-{run_id}-%j"
    body = f"""#!/bin/bash
#SBATCH --job-name=whg-boundary-finalize-{run_id}
#SBATCH --output={log_prefix}.out
#SBATCH --error={log_prefix}.err
#SBATCH --time={_hours_to_slurm_time(_FINALIZE_WALL_HOURS)}
#SBATCH --partition=smp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G

set -eo pipefail
source {_CONDA_SH}
conda activate {_CONDA_ENV}
cd {_REPO}

python -m processing.boundary_stage_finalize \\
    --run-id {run_id} \\
    --namespace {namespace} \\
    --shard-map {shard_map_path} \\
    --manifest-path {manifest_path}
"""
    sbatch_path = work_dir / f"boundary_finalize_{namespace}.sbatch"
    sbatch_path.write_text(body, encoding="utf-8")
    return sbatch_path


def submit(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path,
    pbf_file: Path,
    shard_count: int,
    mega_shard_count: int,
    cost_proxy: str,
    regular_wall_hours: int,
    mega_wall_hours: int,
    regular_mem_gb: int = 32,
    mega_mem_gb: int = 64,
    dry_run: bool = False,
) -> dict:
    manifest = load_run_manifest(manifest_path)
    extract_status = (
        manifest.get("namespaces", {}).get(namespace, {})
        .get("stages", {}).get("extract")
    )
    if extract_status != "completed":
        raise RuntimeError(
            f"Cannot submit boundary chain for namespace '{namespace}': "
            f"extract stage status is {extract_status!r} (expected 'completed')"
        )
    if not pbf_file.exists():
        raise FileNotFoundError(f"PBF file not found: {pbf_file}")

    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    shard_map_path = work_dir / f"boundary_shard_map_{namespace}.json"
    # Persistent prefilter shared across planner + workers. Lives next to
    # the shard map so cleanup (deleting the run work_dir) sweeps it too.
    prefiltered_pbf_path = work_dir / f"{namespace}_boundary_prefiltered.osm.pbf"

    print(f"Submitting boundary chain for namespace '{namespace}'")
    print(f"  PBF:                 {pbf_file}")
    print(f"  Cost proxy:          {cost_proxy}")
    print(f"  Regular shards:      {shard_count}  (wall: {regular_wall_hours}h, mem: {regular_mem_gb}G)")
    print(f"  Mega shards:         {mega_shard_count} (wall: {mega_wall_hours}h, mem: {mega_mem_gb}G)")
    print(f"  Shard map:           {shard_map_path}")
    print(f"  Prefiltered PBF:     {prefiltered_pbf_path}")
    print(f"  Manifest:            {manifest_path}")

    planner_sbatch = _build_planner_sbatch(
        run_id=run_id, namespace=namespace, pbf_file=pbf_file,
        shard_count=shard_count, mega_shard_count=mega_shard_count,
        cost_proxy=cost_proxy, shard_map_path=shard_map_path,
        prefiltered_pbf_path=prefiltered_pbf_path,
        work_dir=work_dir, log_dir=log_dir,
    )
    planner_jobid = _submit(planner_sbatch, depend_on=None, dry_run=dry_run)

    worker_jobids: list[str] = []
    mega_jobid: str | None = None
    regular_jobid: str | None = None

    # Mega shards always come first (shard_id 0..M-1) per planner contract.
    if mega_shard_count > 0:
        mega_sbatch = _build_worker_sbatch(
            run_id=run_id, namespace=namespace, pbf_file=pbf_file,
            array_first=0, array_last=mega_shard_count - 1,
            wall_hours=mega_wall_hours, mem_gb=mega_mem_gb,
            label="mega", shard_map_path=shard_map_path,
            manifest_path=manifest_path,
            prefiltered_pbf_path=prefiltered_pbf_path,
            work_dir=work_dir, log_dir=log_dir,
        )
        mega_jobid = _submit(
            mega_sbatch,
            depend_on=[planner_jobid] if planner_jobid else None,
            dry_run=dry_run,
        )
        if mega_jobid:
            worker_jobids.append(mega_jobid)

    # Regular shards: shard_ids M..M+R-1.
    if shard_count > 0:
        regular_sbatch = _build_worker_sbatch(
            run_id=run_id, namespace=namespace, pbf_file=pbf_file,
            array_first=mega_shard_count,
            array_last=mega_shard_count + shard_count - 1,
            wall_hours=regular_wall_hours, mem_gb=regular_mem_gb,
            label="regular", shard_map_path=shard_map_path,
            manifest_path=manifest_path,
            prefiltered_pbf_path=prefiltered_pbf_path,
            work_dir=work_dir, log_dir=log_dir,
        )
        regular_jobid = _submit(
            regular_sbatch,
            depend_on=[planner_jobid] if planner_jobid else None,
            dry_run=dry_run,
        )
        if regular_jobid:
            worker_jobids.append(regular_jobid)

    finalize_sbatch = _build_finalize_sbatch(
        run_id=run_id, namespace=namespace, shard_map_path=shard_map_path,
        manifest_path=manifest_path, work_dir=work_dir, log_dir=log_dir,
    )
    finalize_jobid = _submit(
        finalize_sbatch,
        depend_on=worker_jobids or ([planner_jobid] if planner_jobid else None),
        dry_run=dry_run,
    )

    return {
        "namespace": namespace,
        "shard_count": shard_count,
        "mega_shard_count": mega_shard_count,
        "cost_proxy": cost_proxy,
        "shard_map_path": str(shard_map_path),
        "planner_jobid": planner_jobid,
        "mega_jobid": mega_jobid,
        "regular_jobid": regular_jobid,
        "finalize_jobid": finalize_jobid,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit sharded boundary-stage Slurm chain for OSM/OHM"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--namespace", required=True, choices=["osm", "ohm"])
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument("--pbf-file", help="Override PBF path")
    parser.add_argument("--shard-count", type=int,
                        help=f"Number of regular (LPT-packed) shards "
                             f"(defaults: {_DEFAULT_SHARD_COUNT})")
    parser.add_argument("--mega-shard-count", type=int,
                        help=f"Number of dedicated mega shards "
                             f"(defaults: {_DEFAULT_MEGA_SHARD_COUNT})")
    parser.add_argument("--cost-proxy", choices=_COST_PROXIES,
                        default=COST_PROXY_NODE_COUNT,
                        help=f"Cost proxy for ranking relations "
                             f"(default: {COST_PROXY_NODE_COUNT})")
    parser.add_argument("--regular-wall-hours", type=int,
                        help=f"Wall time per regular shard "
                             f"(defaults: {_DEFAULT_REGULAR_WALL_HOURS})")
    parser.add_argument("--mega-wall-hours", type=int,
                        help=f"Wall time per mega shard "
                             f"(defaults: {_DEFAULT_MEGA_WALL_HOURS})")
    parser.add_argument("--regular-mem-gb", type=int, default=32,
                        help="Memory per regular shard task (default: 32)")
    parser.add_argument("--mega-mem-gb", type=int, default=64,
                        help="Memory per mega shard task (default: 64)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print sbatch scripts but do not submit")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
            runs_dir=STAGED_RUNS_DIR, run_id=args.run_id,
        )
    )
    if not manifest_path.exists():
        print(f"Run manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    pbf_file = Path(args.pbf_file) if args.pbf_file else _default_pbf_for(args.namespace)
    shard_count = (
        args.shard_count if args.shard_count is not None
        else _DEFAULT_SHARD_COUNT[args.namespace]
    )
    mega_shard_count = (
        args.mega_shard_count if args.mega_shard_count is not None
        else _DEFAULT_MEGA_SHARD_COUNT[args.namespace]
    )
    regular_wall_hours = (
        args.regular_wall_hours if args.regular_wall_hours is not None
        else _DEFAULT_REGULAR_WALL_HOURS[args.namespace]
    )
    mega_wall_hours = (
        args.mega_wall_hours if args.mega_wall_hours is not None
        else _DEFAULT_MEGA_WALL_HOURS[args.namespace]
    )

    result = submit(
        run_id=args.run_id,
        namespace=args.namespace,
        manifest_path=manifest_path,
        pbf_file=pbf_file,
        shard_count=shard_count,
        mega_shard_count=mega_shard_count,
        cost_proxy=args.cost_proxy,
        regular_wall_hours=regular_wall_hours,
        mega_wall_hours=mega_wall_hours,
        regular_mem_gb=args.regular_mem_gb,
        mega_mem_gb=args.mega_mem_gb,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
