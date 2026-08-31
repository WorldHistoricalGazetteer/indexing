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

from processing.ingest_all_authorities import (  # noqa: E402
    INGESTION_ORDER,
    STATE_FILES as _STATE_FILES,
)
from processing.settings import (  # noqa: E402
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_orchestrator import (  # noqa: E402
    _atomic_write_json,
    _manifest_lock,
    create_run_manifest,
    load_run_manifest,
    stage_status_with_fallback,
)

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

    The staged extract and the authority script's resume checkpoint
    (``osm_state.json`` / ``ohm_state.json``) are two halves of one state and
    must move together. Getting that pairing wrong is silent both ways:

    * checkpoint kept, staged cleared → ``osm-places`` resumes near the end of
      the planet and stages a fraction of the corpus, reporting success;
    * checkpoint deleted, staged kept → it restarts from the top and appends a
      second copy of everything already written.

    So: a **first** submission for this run rotates the tree aside and drops the
    checkpoint, starting clean. A **resubmit** (``.prev`` already exists) keeps
    both when a checkpoint survives — that is the whole point of a 96 h job
    having one — and clears the tree only when there is no checkpoint to resume
    from, since those scripts restart at the top.

    Returns a one-line description of what was done, for the caller to print.
    """
    staged = Path(STAGED_BASE_DIR) / namespace
    prev = Path(STAGED_BASE_DIR) / f"{namespace}.prev-{run_id}"
    state_file = _STATE_FILES.get(namespace)
    has_checkpoint = bool(state_file) and Path(state_file).exists()

    if prev.exists():
        if has_checkpoint:
            return (f"{namespace}: RESUMING from {Path(state_file).name} "
                    f"— staged/ and checkpoint both kept")
        if not staged.exists():
            return f"{namespace}: nothing staged; restarting clean"
        if dry_run:
            return f"{namespace}: would CLEAR staged/ (no checkpoint; rollback {prev.name} kept)"
        shutil.rmtree(staged)
        return f"{namespace}: cleared staged/ (no checkpoint; rollback {prev.name} kept)"

    note = ""
    if has_checkpoint:
        note = f", dropped stale {Path(state_file).name}"
        if not dry_run:
            Path(state_file).unlink()
    if not staged.exists():
        return f"{namespace}: nothing staged yet{note}"
    if dry_run:
        return f"{namespace}: would rotate staged/ → {prev.name}{note}"
    staged.rename(prev)
    return f"{namespace}: rotated staged/ → {prev.name}{note}"


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


# Environment the submitting shell may pin for a run, carried into the job.
# GEOM_STORE_STAGING_DIR is the one that matters in practice: geometry staging
# defaults to the single directory ``consolidate_geom_store`` scans, so an
# extract running alongside somebody else's merge has to be able to write
# somewhere private — otherwise their merge silently adopts your half-written
# shards. Only vars explicitly set in the submitting environment are carried,
# so the default behaviour is unchanged.
_PASSTHROUGH_ENV = ("GEOM_STORE_STAGING_DIR",)


def _passthrough_exports() -> list[str]:
    return [f"export {name}={os.environ[name]}"
            for name in _PASSTHROUGH_ENV if os.environ.get(name)]


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
            *_passthrough_exports(),
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


def _extract_is_complete(manifest_path: Path, namespace: str) -> bool:
    """Has this namespace already finished its extract for this run?

    Reads the manifest with the ``events.jsonl`` fallback, so a completion
    recorded by a job whose manifest update was lost still counts.
    """
    if not manifest_path.exists():
        return False
    try:
        manifest = load_run_manifest(manifest_path)
    except (OSError, ValueError):
        return False
    return stage_status_with_fallback(manifest, namespace, "extract") == "completed"


def _clear_checkpoints(manifest_path: Path, namespace: str) -> None:
    """Drop a namespace's per-script checkpoints so ``--resume-run`` re-runs it."""
    with _manifest_lock(manifest_path):
        manifest = load_run_manifest(manifest_path)
        entry = manifest.get("namespaces", {}).get(namespace)
        if not entry:
            return
        entry["scripts"] = {}
        entry["status"] = "pending"
        entry.setdefault("stages", {})["extract"] = "pending"
        _atomic_write_json(manifest_path, manifest)


def submit(
    *,
    run_id: str,
    namespaces: list[str],
    rotate: bool = True,
    dry_run: bool = False,
    ignore_dependencies: bool = False,
    force: bool = False,
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

    manifest_path = ensure_manifest(run_id, dry_run=dry_run)
    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    if not dry_run:
        work_dir.mkdir(parents=True, exist_ok=True)

    # Submit dependants last, so a dependency submitted in this same call has
    # a job id to chain against.
    order = sorted(namespaces, key=lambda ns: len(EXTRACT_DEPENDENCIES.get(ns, ())))

    job_ids: dict[str, str] = {}
    for ns in order:
        cpus, mem, hours = _sizing_for(ns)

        # Rotation versus the resume checkpoint is the same trap as the staged
        # tree versus osm_state.json, one level up. `--resume-run` skips any
        # script already checkpointed completed, so resubmitting a finished
        # namespace deletes its staged output and then declines to regenerate
        # it. That is exactly what happened to `un`: 247 BNDA country polygons
        # staged, cleared on a needless resubmit, job exited 0 having done
        # nothing, and the loss showed up only as an empty directory.
        if _extract_is_complete(manifest_path, ns) and not force:
            print(f"  {ns}: extract already completed for this run — skipping "
                  f"(--force to redo it)")
            continue
        if force and not dry_run and manifest_path.exists():
            _clear_checkpoints(manifest_path, ns)

        if rotate:
            print(f"  {rotate_staged(ns, run_id, dry_run=dry_run)}")
        sbatch_text = _build_sbatch(ns, run_id)

        # Co-selection is not enough: without an afterok chain Slurm is free to
        # start `og` before `ofs` has written the extract it reads.
        after = [job_ids[d] for d in EXTRACT_DEPENDENCIES.get(ns, ()) if d in job_ids]

        if dry_run:
            chained = f", after {'+'.join(after)}" if after else ""
            print(f"  {ns}: would submit ({cpus} cpu / {mem} / {hours}h / {_qos_for(hours)}{chained})")
            # Stand-in id so a dependant printed later shows its chain rather
            # than silently reporting none.
            job_ids[ns] = f"<{ns}>"
            continue

        sbatch_path = work_dir / f"extract-{ns}.sbatch"
        sbatch_path.write_text(sbatch_text, encoding="utf-8")
        command = ["sbatch", "-M", os.environ.get("WHG_SLURM_CLUSTER", "htc"),
                   "--account=ishi", "--parsable"]
        if after:
            command.append(f"--dependency=afterok:{':'.join(after)}")
        command.append(str(sbatch_path))
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"  ✗ {ns}: sbatch failed:\n{result.stderr}", file=sys.stderr)
            continue
        # --parsable prints "<jobid>" or "<jobid>;<cluster>"
        job_id = result.stdout.strip().split(";")[0]
        job_ids[ns] = job_id
        chained = f", after {'+'.join(after)}" if after else ""
        print(f"  ✓ {ns}: job {job_id}  ({cpus} cpu / {mem} / {hours}h / {_qos_for(hours)}{chained})")

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
    parser.add_argument("--force", action="store_true",
                        help="Re-extract even if this run already completed the namespace "
                             "(clears its script checkpoints so --resume-run re-runs it)")
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
        force=args.force,
    )


if __name__ == "__main__":
    main()
