"""Submit Batch 12 — SQLite hard-link harvest + ship-to-Pitt.

Single-job sbatch that runs three harvesters in sequence into one SQLite
file, then atomically ships it to the Pitt gateway via
``clustering.sqlite_overlay.ship_to_pitt``:

1. ``clustering.harvest.hard_links_staged`` — one pass over every selected
   per-gazetteer namespace's staged final snapshot.
2. ``clustering.harvest.loc_links`` — LOC NDJSON → transitive pairs.
3. ``clustering.harvest.contributor_replay`` — DO PG ``status='active'``
   rows → ``contributor:<user_id>[:legacy_v3_2]`` source.

Then ship the file to Pitt and write
``staged/runs/{run_id}.hardlink_ship.json`` as the completion marker that
``processing.push_gazetteer_inventory --require-hardlink-marker`` checks.

This is **Job B** in the post-barrier flow: independent of Batch 11
(no ES dependency), parallelisable with Batch 9.

Usage::

    python -m processing.submit_hardlinks_slurm --run-id <RUN_ID> [--dry-run]
    python -m processing.submit_hardlinks_slurm --run-id <RUN_ID> \
        --pitt-user whgadmin --pitt-host pitt-vm \
        --pitt-dir /ix1/ishi/hardlinks
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

from processing.settings import (  # noqa: E402
    IX1_BASE,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import estimate_wall_time_seconds  # noqa: E402
from processing.staging_orchestrator import (  # noqa: E402
    check_global_barrier,
    load_run_manifest,
)


# First-run default (used only when the runtime-history file is empty).
# Hard-link harvest is single-threaded SQLite + bulk staged file scan; 12 h
# is plenty for ~50M rows. Subsequent runs auto-tune from history.
_WALL_SECONDS = 12 * 3_600
_QOS = "htc-htc-s"  # ≤ 1 day tier


def _estimate_hardlinks_wall() -> int:
    """Single-job estimate keyed on synthetic 'hardlinks' namespace."""
    return estimate_wall_time_seconds(
        "hardlinks", "hard-links-staged", default=_WALL_SECONDS,
    )


def _seconds_to_slurm_time(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _build_sbatch(
    *,
    run_id: str,
    manifest_path: Path,
    db_path: Path,
    marker_path: Path,
    depend_on: str | None,
    pitt_user: str | None,
    pitt_host: str | None,
    pitt_dir: str | None,
    pitt_filename: str,
    skip_loc: bool,
    skip_contributors: bool,
    loc_source: str | None,
) -> str:
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-hardlinks-{run_id}-%j"

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-hardlinks-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --time={_seconds_to_slurm_time(_estimate_hardlinks_wall())}",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={_QOS}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=4",
        "#SBATCH --mem=32G",
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
        f"DB_PATH={db_path}",
        "mkdir -p \"$(dirname \"$DB_PATH\")\"",
        "",
        "echo '--- Phase 1A: authority hard-links from staged snapshots ---'",
        "python -u -m clustering.harvest.hard_links_staged \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        "    --db-path \"$DB_PATH\"",
    ])

    if not skip_loc:
        loc_arg = f" --source {loc_source}" if loc_source else ""
        lines.extend([
            "",
            "echo '--- Phase 1A.LOC: LOC-mediated transitive pairs ---'",
            "python -u -m clustering.harvest.loc_links \\",
            f"    --db-path \"$DB_PATH\"{loc_arg}",
        ])

    if not skip_contributors:
        lines.extend([
            "",
            "echo '--- Phase 1B: contributor replay from DO PG ---'",
            "python -u -m clustering.harvest.contributor_replay \\",
            "    --db-path \"$DB_PATH\"",
        ])

    if pitt_user and pitt_host and pitt_dir:
        lines.extend([
            "",
            "echo '--- Ship-to-Pitt with atomic swap ---'",
            "python -u <<'PYEOF'",
            "import json, os, sys",
            "from pathlib import Path",
            f"sys.path.insert(0, {str(_REPO)!r})",
            "from clustering.sqlite_overlay import ship_to_pitt, finalise_local",
            f"db_path = Path({str(db_path)!r})",
            "row_count = finalise_local(db_path)",
            "result = ship_to_pitt(",
            "    local_db=db_path,",
            f"    remote_user={pitt_user!r},",
            f"    remote_host={pitt_host!r},",
            f"    remote_dir={pitt_dir!r},",
            f"    remote_filename={pitt_filename!r},",
            ")",
            "result['row_count'] = row_count",
            f"marker = Path({str(marker_path)!r})",
            "marker.parent.mkdir(parents=True, exist_ok=True)",
            "marker.write_text(json.dumps(result, indent=2, sort_keys=True))",
            "print('shipped:', json.dumps(result))",
            "PYEOF",
        ])
    else:
        lines.extend([
            "",
            "echo 'Ship-to-Pitt skipped (pass --pitt-user/--pitt-host/--pitt-dir to enable).'",
            "python -u <<'PYEOF'",
            "import json, sys",
            "from pathlib import Path",
            f"sys.path.insert(0, {str(_REPO)!r})",
            "from clustering.sqlite_overlay import finalise_local",
            f"db_path = Path({str(db_path)!r})",
            "row_count = finalise_local(db_path)",
            "print(json.dumps({'db_path': str(db_path), 'row_count': row_count}))",
            "PYEOF",
        ])

    return "\n".join(lines) + "\n"


def _submit(sbatch_path: Path, *, dry_run: bool) -> str | None:
    if dry_run:
        print(f"\n--- DRY RUN: {sbatch_path.name} ---")
        print(sbatch_path.read_text(), end="")
        print("--- END ---")
        return None
    result = subprocess.run(
        ["sbatch", str(sbatch_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"sbatch failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)
    return result.stdout.strip().split()[-1]


def submit(
    *,
    run_id: str,
    manifest_path: Path,
    db_path: Path | None = None,
    marker_path: Path | None = None,
    depend_on: str | None = None,
    pitt_user: str | None = None,
    pitt_host: str | None = None,
    pitt_dir: str | None = None,
    pitt_filename: str = "hard_links.sqlite",
    skip_loc: bool = False,
    skip_contributors: bool = False,
    loc_source: str | None = None,
    enforce_barrier: bool = True,
    dry_run: bool = False,
) -> str | None:
    manifest = load_run_manifest(manifest_path)

    if enforce_barrier:
        is_complete, report = check_global_barrier(manifest)
        if not is_complete:
            failing = [ns for ns, entry in report.items() if "__missing__" in entry]
            print(
                "Refusing to submit Batch 12: global barrier incomplete for "
                f"{len(failing)} namespace(s): {', '.join(sorted(failing))}",
                file=sys.stderr,
            )
            sys.exit(1)

    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    if db_path is None:
        db_path = Path(IX1_BASE) / "hardlinks" / f"hard_links_{run_id}.sqlite"
    if marker_path is None:
        marker_path = Path(STAGED_RUNS_DIR) / f"{run_id}.hardlink_ship.json"

    sbatch_text = _build_sbatch(
        run_id=run_id,
        manifest_path=manifest_path,
        db_path=db_path,
        marker_path=marker_path,
        depend_on=depend_on,
        pitt_user=pitt_user,
        pitt_host=pitt_host,
        pitt_dir=pitt_dir,
        pitt_filename=pitt_filename,
        skip_loc=skip_loc,
        skip_contributors=skip_contributors,
        loc_source=loc_source,
    )
    sbatch_path = work_dir / "hardlinks.sbatch"
    sbatch_path.write_text(sbatch_text, encoding="utf-8")
    print(f"Sbatch written: {sbatch_path}")
    print(f"DB path: {db_path}")
    if pitt_user:
        print(f"Ship target: {pitt_user}@{pitt_host}:{pitt_dir}/{pitt_filename}")
        print(f"Completion marker: {marker_path}")
    else:
        print("Ship-to-Pitt: SKIPPED (no --pitt-user / --pitt-host / --pitt-dir)")

    job_id = _submit(sbatch_path, dry_run=dry_run)
    if job_id:
        print(f"Submitted hardlinks job: {job_id}")
    return job_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit Batch 12 hard-link harvest + ship-to-Pitt"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument("--db-path", help="Override SQLite output path")
    parser.add_argument("--marker-path", help="Override completion marker path")
    parser.add_argument("--depend-on", help="Slurm job ID this batch waits on (afterok)")
    parser.add_argument("--pitt-user")
    parser.add_argument("--pitt-host")
    parser.add_argument("--pitt-dir")
    parser.add_argument("--pitt-filename", default="hard_links.sqlite")
    parser.add_argument("--skip-loc", action="store_true",
                        help="Skip the LOC harvest phase")
    parser.add_argument("--skip-contributors", action="store_true",
                        help="Skip the DO PG contributor replay phase")
    parser.add_argument("--loc-source",
                        help="Override LOC NDJSON source (file or directory)")
    parser.add_argument("--no-enforce-barrier", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.manifest_path:
        manifest_path = Path(args.manifest_path)
    else:
        manifest_path = Path(
            STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
                runs_dir=STAGED_RUNS_DIR, run_id=args.run_id,
            )
        )
    if not manifest_path.exists():
        print(f"Run manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    submit(
        run_id=args.run_id,
        manifest_path=manifest_path,
        db_path=Path(args.db_path) if args.db_path else None,
        marker_path=Path(args.marker_path) if args.marker_path else None,
        depend_on=args.depend_on,
        pitt_user=args.pitt_user,
        pitt_host=args.pitt_host,
        pitt_dir=args.pitt_dir,
        pitt_filename=args.pitt_filename,
        skip_loc=args.skip_loc,
        skip_contributors=args.skip_contributors,
        loc_source=args.loc_source,
        enforce_barrier=not args.no_enforce_barrier,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
