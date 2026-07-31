#!/usr/bin/env python
"""Submit one Slurm job per namespace for the staged extract phase.

Usage
-----
    python -m processing.submit_extract_slurm --run-id <RUN_ID> --namespace osm
    python -m processing.submit_extract_slurm --run-id <RUN_ID> --all --dry-run

This is the piece the resumption playbook wrote out by hand
(``developer/plan-ingestionRebuild.execution.md`` step 5): a heredoc sbatch
template per namespace, with sizing chosen from a table. Twenty-seven of those
is twenty-seven chances to fat-finger a wall-time, so it lives here instead.

What each job runs
------------------
``python -m processing.ingest_all_authorities -n <ns> --resume-run <RUN_ID>``,
which is the existing single-namespace entry point. It already writes the
stage events, manifest statuses and wall-time history the barrier and the
downstream submitters read, and it already knows that ``gn`` and ``wd`` need a
second script (``geonames-toponyms`` / ``wikidata-geoshapes``) after their
``*-places`` run.

``--resume-run`` rather than ``--run-id`` deliberately: ``--run-id`` calls
``create_run_manifest``, which raises ``FileExistsError`` on an existing
manifest *and* narrows ``selected_namespaces`` to whatever ``-n`` was given —
so N concurrent jobs would race, all but one would die, and the survivor would
leave a manifest the global barrier reads as a one-namespace run. This module
creates the manifest once, up front, with the full namespace set; the jobs then
resume into it. Resume mode also skips scripts already checkpointed
``completed``, which is what makes a resubmit after a TIMEOUT cheap.

Staged-tree rotation
--------------------
``helpers.write_staged_place_doc`` **appends**. Re-extracting over a populated
``staged/<ns>/extract/`` therefore silently doubles every doc — the trap the
place#164 ``po`` smoke test hit. Before submitting, this rotates
``staged/<ns>`` aside to ``staged/<ns>.prev-<run_id>`` rather than deleting it,
so the previous corpus stays available as a rollback until the rebuild is
verified. Rotation is skipped when a ``.prev`` for this run already exists (a
resubmit after failure must not clobber the rollback), in which case the live
``staged/<ns>`` is cleared instead.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO = Path(os.environ.get("WHG_REPO", str(_REPO_ROOT)))
_CONDA_ENV = os.environ.get("CONDA_ENV", "whg")
_CONDA_SH = os.environ.get(
    "CONDA_SH", "/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
)

sys.path.insert(0, str(_REPO))

from processing.ingest_all_authorities import INGESTION_ORDER  # noqa: E402
from processing.settings import (  # noqa: E402
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_orchestrator import create_run_manifest  # noqa: E402

#: Namespaces in ingestion order, de-duplicated (``gn``/``wd`` appear twice).
ALL_NAMESPACES: list[str] = list(dict.fromkeys(ns for ns, *_ in INGESTION_ORDER))

# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
# (cpus, mem, wall_hours). Walls are deliberately generous: a TIMEOUT costs a
# whole re-run of the same work, while an over-long wall costs only queue
# priority. Calibrated against the 2026-04/05 rebuild plus the dump sizes
# refreshed on 2026-07-30/31 (wd 144 GiB gz, osm 87.5 GiB pbf).
_SIZING: dict[str, tuple[int, str, int]] = {
    "osm": (8, "120G", 96),
    "wd": (8, "96G", 96),
    "gn": (8, "64G", 36),
    "tgn": (8, "64G", 36),
    "ohm": (8, "32G", 12),
    "gb": (4, "32G", 12),
    "whg": (4, "32G", 12),
    "chgis": (4, "24G", 8),
    "tm": (4, "24G", 8),
}
_DEFAULT_SIZING = (4, "16G", 6)

# Wall-time thresholds → QOS (htc partition), ascending.
_QOS_TIERS: list[tuple[int, str]] = [
    (24, "htc-htc-s"),    # ≤ 1 day
    (72, "htc-htc-n"),    # ≤ 3 days
    (144, "htc-htc-l"),   # ≤ 6 days
    (504, "htc-htc-ll"),  # ≤ 21 days
]

#: Namespaces whose extract reads another namespace's staged extract. ``og``
#: matches its Ottoman admin units against ``ofs`` points to derive hulls, and
#: silently stages 6,260 geometry-less docs when ``ofs`` is absent — it does
#: not fail, so the dependency has to be enforced here rather than discovered
#: in the output.
EXTRACT_DEPENDENCIES: dict[str, tuple[str, ...]] = {"og": ("ofs",)}


def _qos_for(hours: int) -> str:
    for cap, qos in _QOS_TIERS:
        if hours <= cap:
            return qos
    return _QOS_TIERS[-1][1]


def _sizing_for(namespace: str) -> tuple[int, str, int]:
    return _SIZING.get(namespace, _DEFAULT_SIZING)


# ---------------------------------------------------------------------------
# Staged-tree rotation
# ---------------------------------------------------------------------------


def rotate_staged(namespace: str, run_id: str, *, dry_run: bool = False) -> str:
    """Move ``staged/<ns>`` aside so the append-only extract starts clean.

    Returns a one-line description of what was done, for the caller to print.
    """
    staged = Path(STAGED_BASE_DIR) / namespace
    prev = Path(STAGED_BASE_DIR) / f"{namespace}.prev-{run_id}"

    if not staged.exists():
        return f"{namespace}: nothing staged yet"
    if prev.exists():
        # A resubmit: the rollback copy is already safe, so the partial output
        # of the failed attempt is what needs clearing.
        if dry_run:
            return f"{namespace}: would CLEAR staged/ (rollback {prev.name} kept)"
        shutil.rmtree(staged)
        return f"{namespace}: cleared staged/ (rollback {prev.name} kept)"
    if dry_run:
        return f"{namespace}: would rotate staged/ → {prev.name}"
    staged.rename(prev)
    return f"{namespace}: rotated staged/ → {prev.name}"


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def _build_sbatch(namespace: str, run_id: str) -> str:
    cpus, mem, hours = _sizing_for(namespace)
    qos = _qos_for(hours)
    log_dir = Path(STAGED_BASE_DIR) / "parallel-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return "\n".join(
        [
            "#!/bin/bash",
            f"#SBATCH --job-name=extract-{namespace}",
            "#SBATCH --partition=htc",
            f"#SBATCH --qos={qos}",
            f"#SBATCH --time={hours}:00:00",
            "#SBATCH --nodes=1",
            "#SBATCH --ntasks=1",
            f"#SBATCH --cpus-per-task={cpus}",
            f"#SBATCH --mem={mem}",
            f"#SBATCH --output={log_dir}/extract-{namespace}-%j.out",
            f"#SBATCH --error={log_dir}/extract-{namespace}-%j.err",
            "",
            "set -eo pipefail",
            f"source {_CONDA_SH}",
            f"conda activate {_CONDA_ENV}",
            f"cd {_REPO}",
            "export WHG_STAGING_MODE=1",
            "",
            "python -u -m processing.ingest_all_authorities \\",
            f"    -n {namespace} \\",
            f"    --resume-run {run_id}",
            "",
        ]
    )


def ensure_manifest(run_id: str, *, dry_run: bool = False) -> Path:
    """Create the run manifest with the FULL namespace set if it doesn't exist.

    The full set matters: the global barrier and ``index_from_stage`` both
    iterate ``selected_namespaces``, so a manifest created by a single-namespace
    job would make a 27-namespace corpus look complete at one.
    """
    manifest_path = Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(runs_dir=STAGED_RUNS_DIR, run_id=run_id)
    )
    if manifest_path.exists():
        print(f"Run manifest exists: {manifest_path}")
        return manifest_path
    if dry_run:
        print(f"Would create run manifest: {manifest_path} ({len(ALL_NAMESPACES)} namespaces)")
        return manifest_path
    Path(STAGED_RUNS_DIR).mkdir(parents=True, exist_ok=True)
    create_run_manifest(manifest_path, run_id, ALL_NAMESPACES)
    print(f"Created run manifest: {manifest_path} ({len(ALL_NAMESPACES)} namespaces)")
    return manifest_path


def submit(
    *,
    run_id: str,
    namespaces: list[str],
    rotate: bool = True,
    dry_run: bool = False,
    ignore_dependencies: bool = False,
) -> dict[str, str]:
    """Rotate and submit one job per namespace. Returns ``{namespace: job_id}``."""
    unknown = [ns for ns in namespaces if ns not in ALL_NAMESPACES]
    if unknown:
        raise SystemExit(f"Unknown namespace(s): {', '.join(unknown)}")

    if not ignore_dependencies:
        selected = set(namespaces)
        for ns in namespaces:
            missing = [d for d in EXTRACT_DEPENDENCIES.get(ns, ()) if d not in selected]
            if missing:
                raise SystemExit(
                    f"'{ns}' reads the staged extract of {', '.join(missing)}, which is not "
                    f"in this submission. Submit {missing[0]} first and wait for it, or pass "
                    f"--ignore-dependencies to accept geometry-less {ns} docs."
                )

    ensure_manifest(run_id, dry_run=dry_run)
    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    if not dry_run:
        work_dir.mkdir(parents=True, exist_ok=True)

    job_ids: dict[str, str] = {}
    for ns in namespaces:
        cpus, mem, hours = _sizing_for(ns)
        if rotate:
            print(f"  {rotate_staged(ns, run_id, dry_run=dry_run)}")
        sbatch_text = _build_sbatch(ns, run_id)

        if dry_run:
            print(f"  {ns}: would submit ({cpus} cpu / {mem} / {hours}h / {_qos_for(hours)})")
            continue

        sbatch_path = work_dir / f"extract-{ns}.sbatch"
        sbatch_path.write_text(sbatch_text, encoding="utf-8")
        result = subprocess.run(
            ["sbatch", "-M", os.environ.get("WHG_SLURM_CLUSTER", "htc"),
             "--account=ishi", "--parsable", str(sbatch_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            print(f"  ✗ {ns}: sbatch failed:\n{result.stderr}", file=sys.stderr)
            continue
        # --parsable prints "<jobid>" or "<jobid>;<cluster>"
        job_id = result.stdout.strip().split(";")[0]
        job_ids[ns] = job_id
        print(f"  ✓ {ns}: job {job_id}  ({cpus} cpu / {mem} / {hours}h / {_qos_for(hours)})")

    return job_ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit per-namespace staged extract jobs to Slurm"
    )
    parser.add_argument("--run-id", required=True, help="Run ID (staged/runs/<run_id>.json)")
    parser.add_argument("--namespace", action="append", default=[],
                        help="Namespace to extract; repeatable")
    parser.add_argument("--all", action="store_true",
                        help=f"Submit all {len(ALL_NAMESPACES)} namespaces")
    parser.add_argument("--no-rotate", action="store_true",
                        help="Do not move staged/<ns> aside first (only when it is "
                             "already empty — the extract writer APPENDS)")
    parser.add_argument("--ignore-dependencies", action="store_true",
                        help="Submit even when a namespace's staged input is absent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.all and args.namespace:
        raise SystemExit("--all and --namespace are mutually exclusive")
    namespaces = ALL_NAMESPACES if args.all else args.namespace
    if not namespaces:
        raise SystemExit("Pass --namespace <ns> (repeatable) or --all")

    submit(
        run_id=args.run_id,
        namespaces=namespaces,
        rotate=not args.no_rotate,
        dry_run=args.dry_run,
        ignore_dependencies=args.ignore_dependencies,
    )


if __name__ == "__main__":
    main()
