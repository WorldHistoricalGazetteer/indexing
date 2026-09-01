#!/usr/bin/env python
"""Submit a Slurm array job to run staged H3 derivation for all pending namespaces.

Usage
-----
    python -m processing.submit_h3_slurm --run-id <RUN_ID> [--dry-run]

The script:
1. Reads the run manifest to discover namespaces whose H3 stage is ``pending``
   or ``failed``.
2. Queries the persistent namespace-runtime-history to estimate wall-time for
   each namespace (20 % safety margin over historical median; falls back to
   24 h on first run).
3. Writes a per-namespace wall-time file so the Slurm array wrapper can
   read it with ``array-task-id → namespace`` lookup.
4. Submits a Slurm array job that calls ``python -m processing.h3_stage``
   for each array task.

Prerequisites
-------------
* The conda env ``whg`` must be activated.
* Staged extract artefacts must exist for each namespace to be processed
  (produced by the extract/ingest phase).

Environment variables read
--------------------------
WHG_REPO      Path to the repository root (default: auto-detected from __file__)
CONDA_ENV     Conda env name (default: ``whg``)
STAGED_BASE_DIR  Override staging directory
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo / settings bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Allow override via env for remote submission
_REPO = Path(os.environ.get("WHG_REPO", str(_REPO_ROOT)))
_CONDA_ENV = os.environ.get("CONDA_ENV", "whg")
_CONDA_SH = os.environ.get(
    "CONDA_SH",
    "/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh",
)

# Import settings after we know the repo is in sys.path
sys.path.insert(0, str(_REPO))

from processing.slurm_env import CONDA_LIB_PRELOAD, SQLITE_PROBE
from processing.settings import (  # noqa: E402
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import estimate_wall_time_seconds  # noqa: E402
from processing.staging_orchestrator import (  # noqa: E402
    array_memory_gb,
    load_run_manifest,
)

# ---------------------------------------------------------------------------
# QOS tiers (htc partition)
# ---------------------------------------------------------------------------
# Wall-time thresholds → QOS selection (ascending wall-time order)
_QOS_TIERS: list[tuple[int, str]] = [
    (86_400,      "htc-htc-s"),   # ≤ 1 day
    (3 * 86_400,  "htc-htc-n"),   # ≤ 3 days
    (6 * 86_400,  "htc-htc-l"),   # ≤ 6 days
    (21 * 86_400, "htc-htc-ll"),  # ≤ 21 days (maximum)
]

# Namespaces whose real geometry arrives via the boundary chain, not the
# extract. Imported rather than redefined so the two cannot drift.
from processing.ingest_all_authorities import (  # noqa: E402
    BOUNDARY_REQUIRED_NAMESPACES as _BOUNDARY_REQUIRED,
)
from processing.staging_contract import UPDATE_PATCH_NAMESPACES  # noqa: E402

# Namespaces known to be large — allocate extra by default on first run
_LARGE_NAMESPACES = {"osm", "ohm", "gn", "wd"}
_LARGE_DEFAULT_HOURS = 48


def _select_qos(wall_seconds: int) -> tuple[str, int]:
    """Return (qos_name, wall_seconds_capped_to_tier)."""
    for cap, qos in _QOS_TIERS:
        if wall_seconds <= cap:
            return qos, wall_seconds
    # Clamp to maximum tier
    return _QOS_TIERS[-1][1], _QOS_TIERS[-1][0]


def _seconds_to_slurm_time(seconds: int) -> str:
    """Format seconds as ``D-HH:MM:SS`` for ``--time``."""
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _pending_namespaces(manifest: dict) -> list[str]:
    """Return namespaces whose H3 stage is pending or failed (skipped if completed).

    ``osm``/``ohm`` additionally require ``boundary_merge``. This used to gate on
    ``extract`` alone, which is not enough and fails **silently**: those two
    emit point fallbacks for relation multipolygons at extract time, and the
    real geometry only arrives via the boundary chain. ``h3_stage`` prefers
    ``boundary_merged/`` when it exists and quietly falls back to the extract
    when it does not — so running H3 early doesn't error, it just computes
    ``h3_cover`` from a point for every admin boundary in the planet.
    """
    pending = []
    for ns, info in manifest.get("namespaces", {}).items():
        stages = info.get("stages", {})
        h3_status = stages.get("h3", "pending")
        if h3_status not in ("pending", "failed"):
            continue
        if stages.get("extract") != "completed":
            continue
        if ns in _BOUNDARY_REQUIRED and stages.get("boundary_merge") not in (
            "completed", "skipped"
        ):
            print(
                f"  {ns}: extract is complete but boundary_merge is "
                f"'{stages.get('boundary_merge', 'pending')}' — H3 deferred, because "
                f"it would otherwise be computed from relation point fallbacks. "
                f"Run: python -m processing.submit_boundary_slurm --namespace {ns}"
            )
            continue
        # Identical hazard one stage over: h3_stage prefers update_merged/ for
        # gn/wd but falls back to extract/ when it is absent, so running early
        # does not error — it just drops the namespace's update patch. That is
        # how ~26.7M GeoNames alternate names and 58,658 Wikidata geoshapes
        # went missing from production and from this rebuild.
        if ns in UPDATE_PATCH_NAMESPACES and stages.get("update_merge") not in (
            "completed", "skipped"
        ):
            print(
                f"  {ns}: extract is complete but update_merge is "
                f"'{stages.get('update_merge', 'pending')}' — H3 deferred, because "
                f"it would otherwise silently drop the Phase 3 update patch. "
                f"Run: python -m processing.update_merge --namespace {ns}"
            )
            continue
        pending.append(ns)
    return pending


def _write_array_map(namespaces: list[str], work_dir: Path) -> Path:
    """Write a JSON map {task_id: namespace} for use by the array wrapper."""
    array_map = {str(i): ns for i, ns in enumerate(namespaces)}
    map_path = work_dir / "h3_array_map.json"
    map_path.write_text(json.dumps(array_map, indent=2), encoding="utf-8")
    return map_path


def _build_sbatch_script(
    *,
    run_id: str,
    namespaces: list[str],
    manifest_path: Path,
    array_map_path: Path,
    work_dir: Path,
    wall_seconds_per_ns: dict[str, int],
    dry_run: bool,
) -> str:
    """Build the sbatch script text for a Slurm array job.

    Each array task resolves its namespace from the array-map JSON, then looks
    up its own wall-time upper bound. Because Slurm requires a single
    ``--time`` for the whole array, we use the maximum across all tasks.
    """
    max_wall = max(wall_seconds_per_ns.values()) if wall_seconds_per_ns else 86_400
    qos, capped_wall = _select_qos(max_wall)
    slurm_time = _seconds_to_slurm_time(capped_wall)

    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    array_end = len(namespaces) - 1
    log_prefix = log_dir / f"whg-h3-{run_id}-%A_%a"

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-h3-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --array=0-{array_end}",
        f"#SBATCH --time={slurm_time}",
        f"#SBATCH --partition=htc",
        f"#SBATCH --qos={qos}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=4",
        f"#SBATCH --mem={array_memory_gb(namespaces, STAGED_BASE_DIR)}G",
        "",
        "set -eo pipefail",
        f"source {_CONDA_SH}",
        f"conda activate {_CONDA_ENV}",
        CONDA_LIB_PRELOAD,
        SQLITE_PROBE,
        f"cd {_REPO}",
        "",
        "# Resolve namespace from array-task index",
        f"NAMESPACE=$(python -c \"import json; d=json.load(open('{array_map_path}')); print(d[str($SLURM_ARRAY_TASK_ID)])\")",
        "echo \"Array task $SLURM_ARRAY_TASK_ID → namespace: $NAMESPACE\"",
        "",
        "python -m processing.h3_stage \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        "    --namespace \"$NAMESPACE\" \\",
        f"    --array-task-id $SLURM_ARRAY_TASK_ID",
        "",
        "# Merge H3 patches into the staged snapshot so downstream stages",
        "# (ccode enrichment, toponyms, indexing) read h3_merged/ artefacts.",
        "python -m processing.h3_merge \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        "    --namespace \"$NAMESPACE\"",
        "",
        "# Compact per-namespace H3 coverage to staged/_aggregates/ for use by",
        "# Batch 7 (ccode enrichment pre-filter, when namespace == un) and",
        "# Batch 11 (inventory push).",
        "python -m processing.gazetteer_h3_coverage \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        "    --namespace \"$NAMESPACE\"",
    ]
    return "\n".join(lines) + "\n"


def submit(
    *,
    run_id: str,
    manifest_path: Path,
    dry_run: bool = False,
    max_docs: int | None = None,
) -> str | None:
    """Discover pending namespaces, build sbatch script, and submit (or print if dry_run).

    Returns the Slurm job ID string on successful submission, or None for dry runs.
    """
    manifest = load_run_manifest(manifest_path)
    namespaces = _pending_namespaces(manifest)

    if not namespaces:
        print("No namespaces with pending/failed H3 stage found in manifest.")
        return None

    print(f"Namespaces to process ({len(namespaces)}): {', '.join(namespaces)}")

    # Estimate wall times
    wall_seconds_per_ns: dict[str, int] = {}
    for ns in namespaces:
        # Use the compound key that covers all scripts for this namespace —
        # pick the maximum estimated time across all known script_ids for ns.
        # For simplicity, try common script_id patterns.
        for script_suffix in ("-places", "-toponyms", "-geoshapes", "-relations", "-boundaries"):
            sid = f"{ns}{script_suffix}"
            est = estimate_wall_time_seconds(ns, sid)
            if est > wall_seconds_per_ns.get(ns, 0):
                wall_seconds_per_ns[ns] = est

        if ns not in wall_seconds_per_ns or wall_seconds_per_ns[ns] == 86_400:
            # First-run fallback: large namespaces get 48 h, others 24 h
            if ns in _LARGE_NAMESPACES:
                wall_seconds_per_ns[ns] = _LARGE_DEFAULT_HOURS * 3_600
            else:
                wall_seconds_per_ns[ns] = 86_400

    # Write artefacts under a stable work directory for this run
    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    array_map_path = _write_array_map(namespaces, work_dir)

    # Log estimated wall times
    print("Estimated wall times per namespace:")
    for ns in namespaces:
        ws = wall_seconds_per_ns[ns]
        qos, _ = _select_qos(ws)
        print(f"  {ns:12s}  {_seconds_to_slurm_time(ws):>14s}  (qos: {qos})")

    sbatch_text = _build_sbatch_script(
        run_id=run_id,
        namespaces=namespaces,
        manifest_path=manifest_path,
        array_map_path=array_map_path,
        work_dir=work_dir,
        wall_seconds_per_ns=wall_seconds_per_ns,
        dry_run=dry_run,
    )

    sbatch_path = work_dir / "h3_array.sbatch"
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

    # Parse job id from "Submitted batch job 12345678"
    job_id = next((tok for tok in result.stdout.split() if tok.isdigit()), result.stdout.strip().split()[-1])
    print(f"Submitted Slurm array job: {job_id}  ({len(namespaces)} tasks)")
    return job_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit H3 Slurm array job for all pending namespaces in a run manifest"
    )
    parser.add_argument("--run-id", required=True, help="Run ID (matches staged/runs/<run_id>.json)")
    parser.add_argument("--manifest-path", help="Explicit manifest path (default: derive from run-id)")
    parser.add_argument("--dry-run", action="store_true", help="Print sbatch script but do not submit")
    parser.add_argument("--max-docs", type=int, help="Pass --max-docs to each h3_stage task (for smoke tests)")
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
        dry_run=args.dry_run,
        max_docs=args.max_docs,
    )


if __name__ == "__main__":
    main()

