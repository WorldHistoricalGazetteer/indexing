#!/usr/bin/env python
"""Submit Batch 9 Slurm jobs (post-global-barrier, Job A in the dependency flow).

Three jobs are submitted from the same call:

1. **Per-gazetteer AAT enrichment** (array, one task per per-gazetteer
   namespace) — runs ``processing.aat_enrich``, folding ``aat_ids`` +
   ``aat_paths`` into each ``types[]`` entry of the namespace's
   ``staged/{ns}/final`` snapshot. This MUST precede temporal extent / tiles /
   indexing so the indexed corpus carries AAT. (Previously this stage existed
   and was barrier-required but was never wired into any submit script, so it
   only ran when invoked by hand — leaving namespaces silently un-enriched.)

2. **Per-gazetteer temporal extent** (array, one task per per-gazetteer
   namespace) — runs ``processing.gazetteer_temporal_extent`` over the
   namespace's most-enriched staged snapshot and writes
   ``staged/_aggregates/{ns}.temporal_extent.json``. Depends on the AAT
   enrichment array so it reads the enriched ``final`` snapshot.

3. **Global toponym extraction** (single job) — runs
   ``phonetics.extraction.rebuild_toponyms_index`` in staged-only mode
   (``--run-id <RUN_ID> --skip-es-index --confirm``). STEP 1 reads from
   staged places, STEPs 2–3 build the DuckDB + vocabulary + PanPhon JSONL.
   Subsequent Symphonym embedding (``update_es.py compute``) is still
   triggered separately via the existing ``es -update-embeddings`` flow —
   it depends on the model checkpoint + vocab paths that flow lives outside
   the run manifest.

Both jobs require the global barrier (Batch 8) to have passed. Pass
``--depend-on <BARRIER_JOB>`` to chain them after the barrier check, or run
the barrier locally first via ``processing.run_global_barrier`` and submit
Batch 9 only on success.

Usage::

    python -m processing.submit_batch9_slurm --run-id <RUN_ID> [--dry-run]
    python -m processing.submit_batch9_slurm --run-id <RUN_ID> --depend-on <JOB>
    python -m processing.submit_batch9_slurm --run-id <RUN_ID> --skip-toponyms
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
    IX3_BASE,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_orchestrator import array_memory_gb
from processing.staging_contract import is_relations_only  # noqa: E402
from processing.stage_writers import estimate_wall_time_seconds  # noqa: E402
from processing.staging_orchestrator import (  # noqa: E402
    check_global_barrier,
    load_run_manifest,
)


# First-run defaults (used only when the runtime-history file is empty).
_TEMPORAL_WALL_SECONDS = 4 * 3_600  # 4 h ceiling per task
_TEMPORAL_QOS = "htc-htc-s"

# AAT enrich is a streaming file scan + in-memory dict lookups (~30 MB of vocab
# maps + the hierarchy). Even osm (~20 M docs, the largest) is parquet-in /
# parquet-out — comfortably under the 4 h ceiling.
_AAT_ENRICH_WALL_SECONDS = 4 * 3_600
_AAT_ENRICH_QOS = "htc-htc-s"

# Toponym extraction (rebuild_toponyms_index.py) is large: full corpus scan +
# IPA/PanPhon for every training-namespace toponym. Match the existing
# allocation used by symphonym.sh::do_rebuild_toponyms on first runs; later
# runs auto-tune via the runtime-history median.
_TOPONYM_WALL_SECONDS = 48 * 3_600  # 2 days
_TOPONYM_QOS = "htc-htc-n"          # ≤ 3 days tier


def _estimate_temporal_wall(namespaces: list[str]) -> int:
    """Max of per-namespace 'temporal-extent' history; default-aware."""
    if not namespaces:
        return _TEMPORAL_WALL_SECONDS
    estimates = [
        estimate_wall_time_seconds(ns, "temporal-extent",
                                   default=_TEMPORAL_WALL_SECONDS)
        for ns in namespaces
    ]
    return max(estimates)


def _estimate_aat_enrich_wall(namespaces: list[str]) -> int:
    """Max of per-namespace 'aat-enrich' history; default-aware."""
    if not namespaces:
        return _AAT_ENRICH_WALL_SECONDS
    estimates = [
        estimate_wall_time_seconds(ns, "aat-enrich",
                                   default=_AAT_ENRICH_WALL_SECONDS)
        for ns in namespaces
    ]
    return max(estimates)


def _estimate_toponym_wall() -> int:
    """Single-job estimate keyed on synthetic 'toponyms' namespace."""
    return estimate_wall_time_seconds(
        "toponyms", "rebuild-toponyms-index", default=_TOPONYM_WALL_SECONDS,
    )


def _seconds_to_slurm_time(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _eligible_temporal_namespaces(manifest: dict) -> list[str]:
    """Return per-gazetteer namespaces with ``ccode_merge`` complete that have
    not already produced a temporal extent.

    Uses the manifest+event-log fallback so retries under fresh run_ids
    self-heal (see ``staging_orchestrator.stage_status_with_fallback``).
    """
    from processing.staging_orchestrator import stage_status_with_fallback

    out: list[str] = []
    for ns in manifest.get("selected_namespaces", []):
        if is_relations_only(ns):
            continue
        ccode_status = stage_status_with_fallback(manifest, ns, "ccode_merge")
        if ccode_status not in ("completed", "skipped"):
            continue
        if stage_status_with_fallback(manifest, ns, "temporal_extent") == "completed":
            continue
        out.append(ns)
    return out


def _eligible_aat_enrich_namespaces(manifest: dict) -> list[str]:
    """Return per-gazetteer namespaces with ``ccode_merge`` complete that have
    not already been AAT-enriched.

    Same gate as temporal extent (``ccode_merge`` completed/skipped — ``skipped``
    covers ``un``/``po``, which bypass ccode and whose ``final`` is sourced from
    the most-enriched upstream snapshot). Uses the manifest+event-log fallback so
    retries under fresh run_ids self-heal.
    """
    from processing.staging_orchestrator import stage_status_with_fallback

    out: list[str] = []
    for ns in manifest.get("selected_namespaces", []):
        if is_relations_only(ns):
            continue
        ccode_status = stage_status_with_fallback(manifest, ns, "ccode_merge")
        if ccode_status not in ("completed", "skipped"):
            continue
        if stage_status_with_fallback(manifest, ns, "aat_enrich") == "completed":
            continue
        out.append(ns)
    return out


def _write_array_map(
    namespaces: list[str],
    work_dir: Path,
    name: str = "temporal_extent_array_map.json",
) -> Path:
    array_map = {str(i): ns for i, ns in enumerate(namespaces)}
    map_path = work_dir / name
    map_path.write_text(json.dumps(array_map, indent=2), encoding="utf-8")
    return map_path


def _build_aat_enrich_sbatch(
    *,
    run_id: str,
    namespaces: list[str],
    manifest_path: Path,
    array_map_path: Path,
    depend_on: str | None,
) -> str:
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-aat-enrich-{run_id}-%A_%a"

    array_end = len(namespaces) - 1
    slurm_time = _seconds_to_slurm_time(_estimate_aat_enrich_wall(namespaces))

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-aat-enrich-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --array=0-{array_end}",
        f"#SBATCH --time={slurm_time}",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={_AAT_ENRICH_QOS}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=2",
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
        "python -m processing.aat_enrich \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        "    --namespace \"$NAMESPACE\"",
    ])
    return "\n".join(lines) + "\n"


def _build_temporal_sbatch(
    *,
    run_id: str,
    namespaces: list[str],
    manifest_path: Path,
    array_map_path: Path,
    depend_on: str | None,
) -> str:
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-temporal-{run_id}-%A_%a"

    array_end = len(namespaces) - 1
    slurm_time = _seconds_to_slurm_time(_estimate_temporal_wall(namespaces))

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-temporal-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --array=0-{array_end}",
        f"#SBATCH --time={slurm_time}",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={_TEMPORAL_QOS}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=2",
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
        "python -m processing.gazetteer_temporal_extent \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        "    --namespace \"$NAMESPACE\"",
    ])
    return "\n".join(lines) + "\n"


def _build_toponym_sbatch(
    *,
    run_id: str,
    manifest_path: Path,
    depend_on: str | None,
    db_path: Path,
    output_dir: Path,
    extra_args: str,
) -> str:
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-toponyms-{run_id}-%j"

    slurm_time = _seconds_to_slurm_time(_estimate_toponym_wall())

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-toponyms-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --time={slurm_time}",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={_TOPONYM_QOS}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=16",
        "#SBATCH --mem=300G",
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
        # Per-job scratch on CRC compute nodes.
        "SCRATCH_DIR=\"/scratch/slurm-${SLURM_JOB_ID}\"",
        "mkdir -p \"$SCRATCH_DIR\"",
        f"mkdir -p {output_dir}",
        "",
        "python -u -m phonetics.extraction.rebuild_toponyms_index \\",
        f"    --run-id {run_id} \\",
        f"    --manifest-path {manifest_path} \\",
        f"    --db-path {db_path} \\",
        f"    --output-dir {output_dir} \\",
        "    --scratch-dir \"$SCRATCH_DIR\" \\",
        "    --skip-es-index \\",
        f"    --confirm {extra_args}",
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
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"sbatch failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)
    return next((tok for tok in result.stdout.split() if tok.isdigit()), result.stdout.strip().split()[-1])


def submit(
    *,
    run_id: str,
    manifest_path: Path,
    depend_on: str | None = None,
    dry_run: bool = False,
    skip_toponyms: bool = False,
    skip_temporal_extent: bool = False,
    skip_aat_enrich: bool = False,
    enforce_barrier: bool = True,
    db_path: Path | None = None,
    output_dir: Path | None = None,
    extra_toponym_args: str = "",
    for_retrain: bool = False,
) -> dict[str, str | None]:
    """Submit the Batch 9 array(s); return ``{job_kind: slurm_job_id|None}``."""
    manifest = load_run_manifest(manifest_path)

    if enforce_barrier:
        is_complete, report = check_global_barrier(manifest)
        if not is_complete:
            failing = [ns for ns, entry in report.items() if "__missing__" in entry]
            print(
                "Refusing to submit Batch 9: global barrier incomplete for "
                f"{len(failing)} namespace(s): {', '.join(sorted(failing))}",
                file=sys.stderr,
            )
            print(
                "Run `python -m processing.run_global_barrier --run-id "
                f"{run_id}` for the full report.",
                file=sys.stderr,
            )
            sys.exit(1)

    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    submitted: dict[str, str | None] = {}

    # ---- Per-gazetteer AAT enrichment (array) ----------------------------
    # Must precede temporal extent / tiles / indexing: it rewrites each
    # namespace's ``final`` snapshot with folded aat_ids/aat_paths. Temporal
    # extent then depends on it so it reads the enriched snapshot.
    aat_enrich_job: str | None = None
    if not skip_aat_enrich:
        ns_aat = _eligible_aat_enrich_namespaces(manifest)
        if not ns_aat:
            print("No namespaces eligible for aat_enrich (already done or "
                  "ccode_merge not complete) — skipping.")
            submitted["aat_enrich"] = None
        else:
            print(f"AAT enrich namespaces ({len(ns_aat)}): {', '.join(ns_aat)}")
            aat_map_path = _write_array_map(
                ns_aat, work_dir, name="aat_enrich_array_map.json"
            )
            sbatch_text = _build_aat_enrich_sbatch(
                run_id=run_id,
                namespaces=ns_aat,
                manifest_path=manifest_path,
                array_map_path=aat_map_path,
                depend_on=depend_on,
            )
            sbatch_path = work_dir / "aat_enrich_array.sbatch"
            sbatch_path.write_text(sbatch_text, encoding="utf-8")
            print(f"Sbatch written: {sbatch_path}")
            submitted["aat_enrich"] = _submit(sbatch_path, dry_run=dry_run)
            aat_enrich_job = submitted["aat_enrich"]
            if aat_enrich_job:
                print(f"Submitted aat_enrich array: {aat_enrich_job}")

    # ---- Per-gazetteer temporal extent (array) ---------------------------
    # Chain after AAT enrich (when submitted) so it reads the enriched
    # ``final`` snapshot; otherwise fall back to the barrier dependency.
    temporal_depend = aat_enrich_job or depend_on
    if not skip_temporal_extent:
        namespaces = _eligible_temporal_namespaces(manifest)
        if not namespaces:
            print("No namespaces eligible for temporal_extent (already done or "
                  "ccode_merge not complete) — skipping.")
            submitted["temporal_extent"] = None
        else:
            print(f"Temporal extent namespaces ({len(namespaces)}): "
                  f"{', '.join(namespaces)}")
            array_map_path = _write_array_map(namespaces, work_dir)
            sbatch_text = _build_temporal_sbatch(
                run_id=run_id,
                namespaces=namespaces,
                manifest_path=manifest_path,
                array_map_path=array_map_path,
                depend_on=temporal_depend,
            )
            sbatch_path = work_dir / "temporal_extent_array.sbatch"
            sbatch_path.write_text(sbatch_text, encoding="utf-8")
            print(f"Sbatch written: {sbatch_path}")
            submitted["temporal_extent"] = _submit(sbatch_path, dry_run=dry_run)
            if submitted["temporal_extent"]:
                print(f"Submitted temporal_extent array: {submitted['temporal_extent']}")

    # ---- Global toponym extraction (single job) --------------------------
    if not skip_toponyms:
        # Both default to /vast. A ~35 GB DuckDB under sustained random write
        # is exactly the access pattern /ix1 handles worst — it is why ES, the
        # repo and the Slurm WorkDir all moved to flash on 2026-05-01. The
        # default was left pointing at /ix1 through that migration.
        db_path = db_path or Path(IX3_BASE) / "data" / f"toponyms-{run_id}.db"
        output_dir = output_dir or (Path(IX3_BASE) / "models" / "phonetic" / "data" / f"v{run_id}")
        # IPA + PanPhon are training-only artefacts: the active toponyms
        # schema has no panphon_embedding field and the gateway never reads
        # IPA. Default to skipping the Epitran/Phonikud/CharsiuG2P pipeline
        # by passing an unmatched name to --training-namespaces. Re-training
        # cycles must opt in with --for-retrain so the export Parquet ships
        # with the teacher signal.
        if for_retrain:
            extra_args = (" " + extra_toponym_args.strip()) if extra_toponym_args else ""
        else:
            skip_panphon_arg = "--training-namespaces _none_"
            tail = (" " + extra_toponym_args.strip()) if extra_toponym_args else ""
            extra_args = f" {skip_panphon_arg}{tail}"
        sbatch_text = _build_toponym_sbatch(
            run_id=run_id,
            manifest_path=manifest_path,
            depend_on=depend_on,
            db_path=db_path,
            output_dir=output_dir,
            extra_args=extra_args,
        )
        sbatch_path = work_dir / "toponyms.sbatch"
        sbatch_path.write_text(sbatch_text, encoding="utf-8")
        print(f"Sbatch written: {sbatch_path}")
        submitted["toponyms"] = _submit(sbatch_path, dry_run=dry_run)
        if submitted["toponyms"]:
            print(f"Submitted toponyms job: {submitted['toponyms']}")

    return submitted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit Batch 9 Slurm jobs (temporal extent + toponym extraction)"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument(
        "--depend-on",
        help="Slurm job ID this batch should wait on (afterok)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Write sbatch scripts and print them; do not submit")
    parser.add_argument("--skip-toponyms", action="store_true",
                        help="Skip the toponym extraction job")
    parser.add_argument("--skip-temporal-extent", action="store_true",
                        help="Skip the per-namespace temporal extent array")
    parser.add_argument("--skip-aat-enrich", action="store_true",
                        help="Skip the per-namespace AAT enrichment array")
    parser.add_argument("--no-enforce-barrier", action="store_true",
                        help="Submit even if the global barrier check fails (debug only)")
    parser.add_argument("--db-path", help="Override the DuckDB output path for toponyms")
    parser.add_argument("--output-dir",
                        help="Override the output directory for toponym vocab/coverage stats")
    parser.add_argument("--toponym-extra-args", default="",
                        help="Extra args appended to rebuild_toponyms_index (e.g. --resume)")
    parser.add_argument("--for-retrain", action="store_true",
                        help="Compute IPA + PanPhon (gn/wd/tgn defaults) for the next "
                             "Symphonym re-train cycle. Without this flag, those columns "
                             "are skipped — they're not used at search time.")
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
        skip_toponyms=args.skip_toponyms,
        skip_temporal_extent=args.skip_temporal_extent,
        skip_aat_enrich=args.skip_aat_enrich,
        enforce_barrier=not args.no_enforce_barrier,
        db_path=Path(args.db_path) if args.db_path else None,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        extra_toponym_args=args.toponym_extra_args,
        for_retrain=args.for_retrain,
    )


if __name__ == "__main__":
    main()
