#!/usr/bin/env python
"""Submit Batch 11 indexing Slurm jobs.

Two jobs:

1. **Places** (single sbatch) — runs ``processing.index_from_stage`` against
   the staging ES instance, loading every per-gazetteer namespace's
   ``staged/{ns}/final/places.parquet`` into a fresh ``places_<run_id>``
   index, then swaps the alias.
2. **Toponyms** (single sbatch, optional) — delegates to the existing
   ``phonetics.inference.update_es index`` flow which reads the staged
   DuckDB + Symphonym embeddings Parquet and rebuilds the dated
   ``toponyms_<run_id>`` index. Skip with ``--skip-toponyms`` when the
   toponym pipeline has already been driven from ``es -update-embeddings``.

The places job is gated on the global barrier (Batch 8) and on every
selected per-gazetteer namespace having ``ccode_merge`` complete (or
skipped). The submitter refuses to submit otherwise unless
``--no-enforce-barrier`` is set.

Usage::

    python -m processing.submit_index_slurm --run-id <RUN_ID> --es-host <URL>
    python -m processing.submit_index_slurm --run-id <RUN_ID> --es-host <URL> \
        --skip-toponyms --depend-on <PRIOR_JOB>
"""

from __future__ import annotations

import argparse
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
from processing.staging_contract import is_relations_only  # noqa: E402
from processing.staging_orchestrator import (  # noqa: E402
    check_global_barrier,
    load_run_manifest,
)


# First-run defaults (used only when the runtime-history file is empty).
_PLACES_WALL_SECONDS = 12 * 3_600     # 12 h ceiling
_TOPONYMS_WALL_SECONDS = 12 * 3_600
_QOS = "htc-htc-s"  # both jobs comfortably fit in the ≤1 day tier


def _estimate_places_wall(manifest: dict) -> int:
    """Sum the per-namespace 'index-from-stage' estimates across every
    selected per-gazetteer namespace. Falls back to the default when no
    namespace has wall-time history."""
    selected = [
        ns for ns in manifest.get("selected_namespaces", [])
        if not is_relations_only(ns)
    ]
    if not selected:
        return _PLACES_WALL_SECONDS
    total = 0
    have_any = False
    for ns in selected:
        est = estimate_wall_time_seconds(
            ns, "index-from-stage", default=0,
        )
        if est > 0:
            have_any = True
            total += est
    return max(total, _PLACES_WALL_SECONDS) if have_any else _PLACES_WALL_SECONDS


def _estimate_toponyms_wall() -> int:
    """Estimate the toponym index load. Single-namespace bookkeeping under
    the synthetic ``toponyms`` namespace key (the toponym index isn't tied
    to any specific gazetteer)."""
    est = estimate_wall_time_seconds(
        "toponyms", "update-es-index", default=0,
    )
    return max(est, _TOPONYMS_WALL_SECONDS)


def _seconds_to_slurm_time(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _build_places_sbatch(
    *,
    run_id: str,
    manifest: dict,
    manifest_path: Path,
    es_host: str,
    depend_on: str | None,
) -> str:
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-index-places-{run_id}-%j"
    wall_seconds = _estimate_places_wall(manifest)

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-index-places-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --time={_seconds_to_slurm_time(wall_seconds)}",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={_QOS}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=8",
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
        "python -u -m processing.index_from_stage \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        f"    --es-host \"{es_host}\"",
    ])
    return "\n".join(lines) + "\n"


def _build_toponyms_sbatch(
    *,
    run_id: str,
    es_host: str,
    duckdb_file: Path,
    embeddings_file: Path,
    schema_file: Path,
    depend_on: str | None,
    embedding_version: int,
) -> str:
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-index-toponyms-{run_id}-%j"
    wall_seconds = _estimate_toponyms_wall()

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-index-toponyms-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --time={_seconds_to_slurm_time(wall_seconds)}",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={_QOS}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=8",
        "#SBATCH --mem=64G",
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
        "python -u -m phonetics.inference.update_es index \\",
        f"    --es-host \"{es_host}\" \\",
        f"    --index toponyms_{run_id} \\",
        f"    --embedding-version {embedding_version} \\",
        f"    --duckdb-file {duckdb_file} \\",
        f"    --embeddings-file {embeddings_file} \\",
        f"    --schema-file {schema_file}",
    ])
    return "\n".join(lines) + "\n"


def _submit(sbatch_path: Path, *, dry_run: bool) -> str | None:
    if dry_run:
        print(f"\n--- DRY RUN: {sbatch_path.name} ---")
        print(sbatch_path.read_text(), end="")
        print("--- END ---")
        return None
    result = subprocess.run(
        ["sbatch", "-M", os.environ.get("WHG_SLURM_CLUSTER", "htc"), str(sbatch_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"sbatch failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)
    return next((tok for tok in result.stdout.split() if tok.isdigit()), result.stdout.strip().split()[-1])


def submit(
    *,
    run_id: str,
    manifest_path: Path,
    es_host: str,
    depend_on: str | None = None,
    skip_places: bool = False,
    skip_toponyms: bool = False,
    enforce_barrier: bool = True,
    duckdb_file: Path | None = None,
    embeddings_file: Path | None = None,
    schema_file: Path | None = None,
    embedding_version: int | None = None,
    dry_run: bool = False,
) -> dict[str, str | None]:
    manifest = load_run_manifest(manifest_path)

    if enforce_barrier:
        is_complete, report = check_global_barrier(manifest)
        if not is_complete:
            failing = [ns for ns, entry in report.items() if "__missing__" in entry]
            print(
                "Refusing to submit Batch 11: global barrier incomplete for "
                f"{len(failing)} namespace(s): {', '.join(sorted(failing))}",
                file=sys.stderr,
            )
            sys.exit(1)

    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    submitted: dict[str, str | None] = {}

    if not skip_places:
        sbatch_text = _build_places_sbatch(
            run_id=run_id,
            manifest=manifest,
            manifest_path=manifest_path,
            es_host=es_host,
            depend_on=depend_on,
        )
        sbatch_path = work_dir / "index_places.sbatch"
        sbatch_path.write_text(sbatch_text, encoding="utf-8")
        print(f"Sbatch written: {sbatch_path}")
        submitted["places"] = _submit(sbatch_path, dry_run=dry_run)
        if submitted["places"]:
            print(f"Submitted places-index job: {submitted['places']}")

    if not skip_toponyms:
        # Defaults match symphonym.sh::do_update_embeddings_index conventions.
        if duckdb_file is None or embeddings_file is None or schema_file is None or embedding_version is None:
            print(
                "Skipping toponyms index: pass --duckdb-file, --embeddings-file, "
                "--schema-file, and --embedding-version (or invoke via "
                "es -update-embeddings-index instead).",
                file=sys.stderr,
            )
        else:
            # Chain after places when both are submitted in this call.
            tdep = depend_on
            if submitted.get("places") and not dry_run:
                tdep = submitted["places"]
            sbatch_text = _build_toponyms_sbatch(
                run_id=run_id,
                es_host=es_host,
                duckdb_file=duckdb_file,
                embeddings_file=embeddings_file,
                schema_file=schema_file,
                depend_on=tdep,
                embedding_version=embedding_version,
            )
            sbatch_path = work_dir / "index_toponyms.sbatch"
            sbatch_path.write_text(sbatch_text, encoding="utf-8")
            print(f"Sbatch written: {sbatch_path}")
            submitted["toponyms"] = _submit(sbatch_path, dry_run=dry_run)
            if submitted["toponyms"]:
                print(f"Submitted toponyms-index job: {submitted['toponyms']}")

    return submitted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit Batch 11 indexing Slurm jobs (places + toponyms)"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument("--es-host", required=True,
                        help="Staging ES URL (must be running before the array starts)")
    parser.add_argument("--depend-on",
                        help="Slurm job ID this batch should wait on (afterok)")
    parser.add_argument("--skip-places", action="store_true")
    parser.add_argument("--skip-toponyms", action="store_true",
                        help="Skip toponyms index (typical when es -update-embeddings-index runs separately)")
    parser.add_argument("--no-enforce-barrier", action="store_true")
    parser.add_argument("--duckdb-file", help="Toponyms DuckDB (Batch 9 output)")
    parser.add_argument("--embeddings-file", help="Symphonym embeddings Parquet")
    parser.add_argument("--schema-file",
                        default=str(_REPO / "schemas" / "toponyms.json"))
    parser.add_argument("--embedding-version", type=int)
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
        es_host=args.es_host,
        depend_on=args.depend_on,
        skip_places=args.skip_places,
        skip_toponyms=args.skip_toponyms,
        enforce_barrier=not args.no_enforce_barrier,
        duckdb_file=Path(args.duckdb_file) if args.duckdb_file else None,
        embeddings_file=Path(args.embeddings_file) if args.embeddings_file else None,
        schema_file=Path(args.schema_file) if args.schema_file else None,
        embedding_version=args.embedding_version,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
