#!/usr/bin/env python
"""Submit a Slurm array job for per-namespace ccode enrichment + merge.

Each array task runs::

    python -m processing.ccode_enrichment --namespace <NS>
    python -m processing.ccode_merge      --namespace <NS>

The job is intended to run **after** the H3 array (``submit_h3_slurm.py``) has
completed for every selected namespace, because:

* ``ccode_enrichment`` reads UN's per-geometry ``h3_cover`` to build the
  prefilter; UN must therefore have a completed ``h3_merge`` *and*
  ``h3_coverage`` stage on disk;
* every other namespace needs its own ``h3_merged/`` snapshot as input.

Pass ``--depend-on <SLURM_JOB_ID>`` to chain this submission onto the H3
array via ``--dependency=afterok:<job>``. Without it, the caller is
responsible for sequencing.

Usage
-----
    python -m processing.submit_ccode_slurm --run-id <RUN_ID> \
        --depend-on <H3_JOB_ID> [--dry-run]

The UN namespace itself is excluded — UN docs already carry their own ccodes
from the source data; ccode enrichment is only meaningful for non-UN places.
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

from processing.ccode_enrichment import UN_NAMESPACE  # noqa: E402
from processing.settings import (  # noqa: E402
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import estimate_wall_time_seconds  # noqa: E402
from processing.staging_contract import is_relations_only  # noqa: E402
from processing.staging_orchestrator import (  # noqa: E402
    array_memory_gb,
    load_run_manifest,
    update_namespace_stage_status,
)


_QOS_TIERS: list[tuple[int, str]] = [
    (86_400,      "htc-htc-s"),   # ≤ 1 day
    (3 * 86_400,  "htc-htc-n"),   # ≤ 3 days
    (6 * 86_400,  "htc-htc-l"),   # ≤ 6 days
    (21 * 86_400, "htc-htc-ll"),  # ≤ 21 days
]

_LARGE_NAMESPACES = {"osm", "ohm", "gn", "wd"}


def _select_qos(wall_seconds: int) -> tuple[str, int]:
    for cap, qos in _QOS_TIERS:
        if wall_seconds <= cap:
            return qos, wall_seconds
    return _QOS_TIERS[-1][1], _QOS_TIERS[-1][0]


def _seconds_to_slurm_time(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _mark_un_skipped(manifest: dict, manifest_path: Path, *, dry_run: bool = False) -> None:
    """Record UN's ccode stages as ``skipped`` so the global barrier can pass."""
    if UN_NAMESPACE not in manifest.get("namespaces", {}):
        return
    stages = manifest["namespaces"][UN_NAMESPACE].get("stages", {})
    todo = [s for s in ("ccode", "ccode_merge")
            if stages.get(s) not in ("completed", "skipped")]
    if not todo:
        return
    if dry_run:
        print(f"  would mark {UN_NAMESPACE} {'/'.join(todo)} as skipped "
              f"(it is the ccode source)")
        return
    for stage in todo:
        update_namespace_stage_status(manifest_path, UN_NAMESPACE, stage, "skipped")
    print(f"  marked {UN_NAMESPACE} {'/'.join(todo)} as skipped (it is the ccode source)")


def _pending_namespaces(manifest: dict, *,
                        h3_pending_ok: bool = False) -> list[str]:
    """Namespaces eligible for ccode enrichment.

    Eligibility:
    * must not be the UN namespace itself (UN docs supply ccodes directly);
    * must not be relations-only (e.g. LOC);
    * must have a completed ``h3_merge`` stage — **unless** ``h3_pending_ok``,
      see below;
    * ``ccode`` stage status must be ``pending`` or ``failed`` (skip when
      already completed so resumes are idempotent).

    ``h3_pending_ok`` exists because eligibility is evaluated when the array is
    *submitted*, not when it *runs*. Chaining this array behind the H3 array
    with ``--depend-on`` therefore froze the namespace list against a
    part-finished H3 run: on 5 Aug 2026 it selected 11 of 27 namespaces — the
    small ones whose H3 tasks had already completed in the few minutes since
    submission — and silently omitted ``osm``, ``gn``, ``wd``, ``tgn`` and
    ``ohm``, i.e. every namespace that mattered. The dependency implies the H3
    stage will have run by then, so when one is given the H3 gate must not be
    applied at selection time.
    """
    pending = []
    for ns, info in manifest.get("namespaces", {}).items():
        if ns == UN_NAMESPACE or is_relations_only(ns):
            continue
        stages = info.get("stages", {})
        if not h3_pending_ok and stages.get("h3_merge") != "completed":
            continue
        ccode_status = stages.get("ccode", "pending")
        if ccode_status in ("pending", "failed"):
            pending.append(ns)
    return pending


def _write_array_map(namespaces: list[str], work_dir: Path) -> Path:
    array_map = {str(i): ns for i, ns in enumerate(namespaces)}
    map_path = work_dir / "ccode_array_map.json"
    map_path.write_text(json.dumps(array_map, indent=2), encoding="utf-8")
    return map_path


def _build_sbatch_script(
    *,
    run_id: str,
    namespaces: list[str],
    manifest_path: Path,
    array_map_path: Path,
    wall_seconds_per_ns: dict[str, int],
    depend_on: str | None,
) -> str:
    max_wall = max(wall_seconds_per_ns.values()) if wall_seconds_per_ns else 86_400
    qos, capped_wall = _select_qos(max_wall)
    slurm_time = _seconds_to_slurm_time(capped_wall)

    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    array_end = len(namespaces) - 1
    log_prefix = log_dir / f"whg-ccode-{run_id}-%A_%a"

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-ccode-{run_id}",
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
        f"NAMESPACE=$(python -c \"import json; d=json.load(open('{array_map_path}')); print(d[str($SLURM_ARRAY_TASK_ID)])\")",
        "echo \"Array task $SLURM_ARRAY_TASK_ID → namespace: $NAMESPACE\"",
        "",
        "python -m processing.ccode_enrichment \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        "    --namespace \"$NAMESPACE\"",
        "",
        "python -m processing.ccode_merge \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        "    --namespace \"$NAMESPACE\"",
    ])
    return "\n".join(lines) + "\n"


def submit(
    *,
    run_id: str,
    manifest_path: Path,
    depend_on: str | None = None,
    dry_run: bool = False,
    only_namespaces: list[str] | None = None,
) -> str | None:
    manifest = load_run_manifest(manifest_path)
    if only_namespaces:
        namespaces = list(only_namespaces)
        known = set(manifest.get("namespaces", {}))
        unknown = [ns for ns in namespaces if ns not in known]
        if unknown:
            print(f"Not in this run manifest: {', '.join(unknown)}",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Explicit namespace override: {', '.join(namespaces)}")
    else:
        # A dependency means the H3 array will have completed before this one
        # runs, so its stage status at SUBMIT time must not gate selection.
        namespaces = _pending_namespaces(manifest,
                                         h3_pending_ok=bool(depend_on))
    if depend_on and not only_namespaces:
        not_yet = [ns for ns in namespaces
                   if (manifest["namespaces"][ns].get("stages") or {}
                       ).get("h3_merge") != "completed"]
        if not_yet:
            print(f"  --depend-on {depend_on}: including {len(not_yet)} "
                  f"namespace(s) whose h3_merge is still pending "
                  f"({', '.join(sorted(not_yet)[:8])}"
                  f"{' …' if len(not_yet) > 8 else ''})")

    # `un` is excluded from the array because it supplies ccodes rather than
    # receiving them — but excluding it is not the same as recording it, and
    # GLOBAL_BARRIER_REQUIRED_STAGES demands `completed` or `skipped` for both
    # ccode stages. Left `pending`, `un` blocks the barrier for ever, and the
    # barrier report says only that `un` is missing ccode — which reads like a
    # failure rather than a namespace that was never meant to run.
    _mark_un_skipped(manifest, manifest_path, dry_run=dry_run)

    if not namespaces:
        print("No namespaces eligible for ccode enrichment in this manifest.")
        return None

    print(f"Namespaces to process ({len(namespaces)}): {', '.join(namespaces)}")

    wall_seconds_per_ns: dict[str, int] = {}
    for ns in namespaces:
        est = estimate_wall_time_seconds(ns, "ccode-enrichment")
        if est == 86_400 and ns in _LARGE_NAMESPACES:
            est = 2 * 86_400
        wall_seconds_per_ns[ns] = est

    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    array_map_path = _write_array_map(namespaces, work_dir)

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
        wall_seconds_per_ns=wall_seconds_per_ns,
        depend_on=depend_on,
    )

    sbatch_path = work_dir / "ccode_array.sbatch"
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
    print(f"Submitted Slurm array job: {job_id}  ({len(namespaces)} tasks)")
    return job_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit ccode enrichment + merge Slurm array job"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument(
        "--depend-on",
        help="Slurm job ID this array should wait on (afterok)",
    )
    parser.add_argument(
        "--namespaces",
        help=(
            "Comma-separated namespaces to submit, overriding eligibility. "
            "Needed after a task is cancelled: its ccode stage is left at "
            "'running', which _pending_namespaces treats as neither pending "
            "nor failed, so a plain resubmit silently skips it."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print sbatch but do not submit")
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
        only_namespaces=([n.strip() for n in args.namespaces.split(",")
                          if n.strip()] if args.namespaces else None),
    )


if __name__ == "__main__":
    main()
