#!/usr/bin/env python
"""Submit Slurm array job(s) for staged tile generation.

Tile generation reads only staged snapshots and the geometry store — it does
not touch Elasticsearch — so this submission can run in parallel with other
post-extract work and **before** the global barrier.

The submission is **bucket-driven**: each Slurm array task owns exactly one
tile bucket and streams its full list of contributing namespaces from staged
snapshots. Three families of bucket are produced:

* **Fixed buckets** — ``osm``, ``ohm``, ``osm_misc``: boundary-
  gated, polygon-only. Require ``boundary_merge`` complete on every
  contributing OSM/OHM namespace.
* **Per-namespace buckets** — one bucket per authority namespace
  (``chgis``, ``clio``, ``dgsd``, ``dp``, ``gb``, ``gn``, ``iv``, ``nl``,
  ``pl``, ``po``, ``tgn``, ``tm``, ``un``, ``wd``). Every doc with
  renderable geometry (point or polygon) is included. Require
  ``aat_enrich`` (or ``ccode_merge`` for namespaces that bypass aat_enrich)
  complete.
* **Per-WHG-dataset buckets** — ``whg-<dataset_sub_id>`` per row in
  ``staged/_aggregates/whg.datasets.json``. Require ``whg`` namespace
  ``aat_enrich`` complete.

Resources scale with bucket size: per-namespace buckets are split across
three tiers (small / medium / large), and one Slurm array is submitted per
tier so memory is not over-allocated for small buckets.

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

from processing.generate_tiles import (  # noqa: E402
    _FIXED_BUCKETS,
    _PER_NAMESPACE_BUCKETS,
    _WHG_BUCKET_PREFIX,
    _bucket_contributors,
    _load_whg_dataset_sub_ids,
    _whg_dataset_sub_id,
)
from processing.settings import (  # noqa: E402
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_contract import (  # noqa: E402
    AGGREGATE_TEMPORAL_EXTENT_FILENAME_TEMPLATE,
)
from processing.staging_orchestrator import load_run_manifest  # noqa: E402


# Per-tier resource budgets. Tippecanoe peak RSS scales with the GeoJSONL
# input + tile-pyramid working set.
#
# Fixed-bucket budgets reflect observed peaks: clio (68 MB → 1.7 GB mbtiles,
# peak ~3 GB); po (6.6 MB → 1.1 GB, peak ~2 GB); ohm (6 GB → OOM at
# 16 GB, settles around 96 GB). OSM admin/misc dwarf ohm admin again.
_FIXED_RESOURCES: dict[str, dict[str, int]] = {
    "osm":      {"mem_gb": 192, "wall_s": 24 * 3_600},
    "ohm":      {"mem_gb": 96,  "wall_s": 24 * 3_600},
    "osm_misc":  {"mem_gb": 192, "wall_s": 24 * 3_600},
}

# Per-namespace tiers, picked by record_count on the namespace.
_TIER_SMALL  = {"mem_gb": 16, "wall_s":  4 * 3_600, "qos": "htc-htc-s"}
_TIER_MEDIUM = {"mem_gb": 32, "wall_s": 12 * 3_600, "qos": "htc-htc-s"}
_TIER_LARGE  = {"mem_gb": 64, "wall_s": 24 * 3_600, "qos": "htc-htc-s"}

_DEFAULT_QOS = "htc-htc-s"


def _record_count_for_namespace(namespace: str) -> int | None:
    """Read ``record_count`` from ``staged/_aggregates/<ns>.temporal_extent.json``.

    Returns ``None`` when the aggregate is missing — callers fall back to the
    small tier.
    """
    path = Path(STAGED_BASE_DIR) / "_aggregates" / (
        AGGREGATE_TEMPORAL_EXTENT_FILENAME_TEMPLATE.format(namespace=namespace)
    )
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rc = payload.get("record_count")
    return int(rc) if isinstance(rc, int) else None


def _whg_record_counts() -> dict[str, int]:
    """Map ``dataset_sub_id`` → ``record_count`` from the WHG sidecar."""
    sidecar = Path(STAGED_BASE_DIR) / "_aggregates" / "whg.datasets.json"
    if not sidecar.exists():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, int] = {}
    for entry in payload.get("datasets") or ():
        ds_id = entry.get("id") or ""
        sub = ds_id.split(":", 1)[1] if ":" in ds_id else ds_id
        if not sub:
            continue
        rc = entry.get("record_count")
        out[sub] = int(rc) if isinstance(rc, int) else 0
    return out


def _bucket_resources(bucket: str) -> dict[str, int | str]:
    """Return ``{mem_gb, wall_s, qos}`` for a bucket."""
    if bucket in _FIXED_BUCKETS:
        fixed = _FIXED_RESOURCES[bucket]
        return {"mem_gb": fixed["mem_gb"], "wall_s": fixed["wall_s"], "qos": _DEFAULT_QOS}

    if bucket in _PER_NAMESPACE_BUCKETS:
        rc = _record_count_for_namespace(bucket)
        if rc is None or rc < 100_000:
            tier = _TIER_SMALL
        elif rc < 3_000_000:
            tier = _TIER_MEDIUM
        else:
            tier = _TIER_LARGE
        return dict(tier)

    if bucket.startswith(_WHG_BUCKET_PREFIX):
        # WHG datasets are individually small.
        return dict(_TIER_SMALL)

    return dict(_TIER_SMALL)


def _seconds_to_slurm_time(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _required_stage_for(namespace: str) -> str:
    """Per-namespace prerequisite stage that must be completed before tiles.

    OSM/OHM still need ``boundary_merge`` (full polygons must be present in
    the geom store before tippecanoe runs). Every other namespace needs
    ``aat_enrich``: the per-namespace tile pyramid is the user-visible
    representation of a gazetteer, so tiles should reflect the most-enriched
    state of each doc.
    """
    if namespace in ("osm", "ohm"):
        return "boundary_merge"
    return "aat_enrich"


def _stage_completed(manifest: dict, namespace: str, stage: str) -> bool:
    """True when the manifest OR the per-namespace event log says completed.

    Thin wrapper around ``staging_orchestrator.stage_status_with_fallback``,
    restricted to the strict ``completed`` answer (tile generation doesn't
    treat ``skipped`` as eligible — a skipped namespace contributes no
    features and shouldn't get its own bucket).
    """
    from processing.staging_orchestrator import stage_status_with_fallback
    return stage_status_with_fallback(manifest, namespace, stage) == "completed"


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

    for bucket in _FIXED_BUCKETS:
        contributors = _FIXED_BUCKETS[bucket]
        ready = all(
            _stage_completed(manifest, ns, _required_stage_for(ns))
            for ns in contributors
        )
        if not ready:
            continue
        already_done = all(
            _stage_completed(manifest, ns, "tiles") for ns in contributors
        )
        if already_done:
            continue
        eligible.append(bucket)

    for ns in _PER_NAMESPACE_BUCKETS:
        if not _stage_completed(manifest, ns, _required_stage_for(ns)):
            continue
        # Skip per-namespace buckets without a final/places.* file: there is
        # nothing to stream.
        ns_dir = Path(STAGED_BASE_DIR) / ns / "final"
        if not ((ns_dir / "places.parquet").exists() or (ns_dir / "places.jsonl").exists()):
            continue
        eligible.append(ns)

    if _stage_completed(manifest, "whg", _required_stage_for("whg")):
        for sub_id in _load_whg_dataset_sub_ids():
            eligible.append(f"{_WHG_BUCKET_PREFIX}{sub_id}")

    return eligible


def _group_by_resources(buckets: list[str]) -> list[tuple[dict, list[str]]]:
    """Group buckets by identical resource tier so each Slurm array is
    homogeneous (one ``--mem`` / ``--time`` per array, picked by the
    heaviest task in the group)."""
    groups: dict[tuple, list[str]] = {}
    resources_by_key: dict[tuple, dict] = {}
    for bucket in buckets:
        res = _bucket_resources(bucket)
        key = (res["mem_gb"], res["wall_s"], res["qos"])
        groups.setdefault(key, []).append(bucket)
        resources_by_key[key] = res
    # Order tiers small→large so the small tier (which dominates by count)
    # gets a low array index — Slurm prints those first in `squeue`.
    ordered_keys = sorted(groups.keys(), key=lambda k: (k[0], k[1]))
    return [(resources_by_key[k], groups[k]) for k in ordered_keys]


def _write_array_map(buckets: list[str], work_dir: Path, suffix: str) -> Path:
    array_map = {str(i): bucket for i, bucket in enumerate(buckets)}
    map_path = work_dir / f"tiles_array_map.{suffix}.json"
    map_path.write_text(json.dumps(array_map, indent=2), encoding="utf-8")
    return map_path


def _build_sbatch_script(
    *,
    run_id: str,
    buckets: list[str],
    resources: dict,
    manifest_path: Path,
    array_map_path: Path,
    depend_on: str | None,
    output_dir: Path | None,
    suffix: str,
) -> str:
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-tiles-{run_id}-{suffix}-%A_%a"

    array_end = len(buckets) - 1
    slurm_time = _seconds_to_slurm_time(int(resources["wall_s"]))
    slurm_mem_gb = int(resources["mem_gb"])
    slurm_qos = str(resources.get("qos", _DEFAULT_QOS))

    output_arg = ""
    if output_dir is not None:
        output_arg = f" --output-dir {output_dir}"

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-tiles-{run_id}-{suffix}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        f"#SBATCH --array=0-{array_end}",
        f"#SBATCH --time={slurm_time}",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={slurm_qos}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=4",
        f"#SBATCH --mem={slurm_mem_gb}G",
    ]
    if depend_on:
        lines.append(f"#SBATCH --dependency=afterok:{depend_on}")
    lines.extend([
        "",
        "set -eo pipefail",
        # Tippecanoe holds one fd per concurrent tile-write thread plus
        # bookkeeping; the default 1024 ulimit blew up ohm at
        # 6 GB/68 K-feature scale ("Too many open files"). 65536 is the
        # hard cap on most Linux distros and gives ~10× more headroom
        # than tippecanoe needs even on osm.
        "ulimit -n 65536",
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


def _tier_label(resources: dict) -> str:
    return f"mem{int(resources['mem_gb'])}g-t{int(resources['wall_s']) // 3_600}h"


def _submit_one_sbatch(sbatch_path: Path) -> str:
    cluster = os.environ.get("WHG_SLURM_CLUSTER", "htc")
    result = subprocess.run(
        ["sbatch", "-M", cluster, "--parsable", str(sbatch_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"sbatch failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)
    raw = result.stdout.strip().split(";", 1)[0]
    return raw or result.stdout.strip()


def _build_restart_sbatch_script(
    *,
    run_id: str,
    array_job_ids: list[str],
    buckets: list[str],
) -> str:
    """Build the trailing finalize sbatch fired ``afterok`` of every tile array.

    Per the contract (push → config rewrite → restart → confirm, all before the
    Django inventory push), this one tiny job — gated ``afterok`` so it runs only
    when every bucket pushed cleanly — does a **single** safe rewrite of the
    tileserver ``config.json`` registering all of this run's buckets, restarts the
    server, and verifies each tileset serves. It supersedes the old bare
    ``restart_tileserver`` call (``update_tileserver_config`` restarts + verifies
    itself, preserving every other config entry).
    """
    log_dir = Path(_REPO) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_prefix = log_dir / f"whg-tiles-restart-{run_id}-%j"

    deps = ":".join(array_job_ids)
    bucket_args = " ".join(f"--bucket {b}" for b in buckets)
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=whg-tiles-restart-{run_id}",
        f"#SBATCH --output={log_prefix}.out",
        f"#SBATCH --error={log_prefix}.err",
        "#SBATCH --time=00:10:00",
        "#SBATCH --partition=htc",
        f"#SBATCH --qos={_DEFAULT_QOS}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --cpus-per-task=1",
        "#SBATCH --mem=512M",
        f"#SBATCH --dependency=afterok:{deps}",
        "",
        f"source {_CONDA_SH}",
        f"conda activate {_CONDA_ENV}",
        "set -eo pipefail",
        f"cd {_REPO}",
        "",
        # Single combined rewrite of all this run's buckets + restart + verify.
        f"python -u -m processing.update_tileserver_config {bucket_args} --execute",
    ]
    return "\n".join(lines) + "\n"


def submit(
    *,
    run_id: str,
    manifest_path: Path,
    depend_on: str | None = None,
    dry_run: bool = False,
    output_dir: Path | None = None,
    with_restart: bool = True,
    only_buckets: list[str] | None = None,
) -> list[str]:
    manifest = load_run_manifest(manifest_path)
    buckets = _eligible_buckets(manifest)

    if only_buckets:
        # Intersect with the explicit allow-list. This is the safety hatch
        # for partial rebuilds (e.g. ``--only-bucket osm --only-bucket ohm
        # --only-bucket osm_misc`` when the geom_store has just been
        # refreshed for OSM/OHM but every other namespace's tilesets are
        # already current). Without it the per-namespace event-log
        # fallback in ``stage_status_with_fallback`` re-eligibilises every
        # bucket that has ever completed ``aat_enrich`` — exactly what
        # made the 2026-05-05 postbarrier wipe a working tileset.
        allow = set(only_buckets)
        rejected = [b for b in buckets if b not in allow]
        buckets = [b for b in buckets if b in allow]
        unknown = [b for b in only_buckets if b not in (set(rejected) | set(buckets))]
        if unknown:
            print(f"--only-bucket: ignoring unknown / ineligible: {unknown}")
        if rejected:
            print(f"--only-bucket: filtered out {len(rejected)} bucket(s) "
                  f"that the manifest+events would have queued: {rejected}")

    if not buckets:
        print("No tile buckets eligible (prerequisites incomplete or already done).")
        return []

    print(f"Buckets to generate ({len(buckets)}): {', '.join(buckets)}")

    work_dir = Path(STAGED_BASE_DIR) / "runs" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    groups = _group_by_resources(buckets)
    print(f"\nGrouped into {len(groups)} resource tier(s):")
    for resources, group_buckets in groups:
        print(
            f"  {_tier_label(resources)}: "
            f"{len(group_buckets)} bucket(s) — {', '.join(group_buckets)}"
        )

    job_ids: list[str] = []
    for resources, group_buckets in groups:
        suffix = _tier_label(resources)
        array_map_path = _write_array_map(group_buckets, work_dir, suffix)
        sbatch_text = _build_sbatch_script(
            run_id=run_id,
            buckets=group_buckets,
            resources=resources,
            manifest_path=manifest_path,
            array_map_path=array_map_path,
            depend_on=depend_on,
            output_dir=output_dir,
            suffix=suffix,
        )
        sbatch_path = work_dir / f"tiles_array.{suffix}.sbatch"
        sbatch_path.write_text(sbatch_text, encoding="utf-8")
        print(f"\nSbatch script written to: {sbatch_path}")

        if dry_run:
            print("\n--- DRY RUN: sbatch script contents ---")
            print(sbatch_text)
            print("--- END ---")
            continue

        job_id = _submit_one_sbatch(sbatch_path)
        print(
            f"Submitted Slurm array job: {job_id}  "
            f"({len(group_buckets)} tasks, tier={suffix})"
        )
        job_ids.append(job_id)

    if with_restart and job_ids:
        restart_text = _build_restart_sbatch_script(
            run_id=run_id, array_job_ids=job_ids, buckets=buckets,
        )
        restart_path = work_dir / "tiles_restart.sbatch"
        restart_path.write_text(restart_text, encoding="utf-8")
        print(f"\nRestart sbatch script written to: {restart_path}")

        if dry_run:
            print("\n--- DRY RUN: restart sbatch contents ---")
            print(restart_text)
            print("--- END ---")
        else:
            restart_jid = _submit_one_sbatch(restart_path)
            print(
                f"Submitted tileserver restart job: {restart_jid}  "
                f"(afterok of {len(job_ids)} array job(s))"
            )
            job_ids.append(restart_jid)

    return job_ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit staged tile-generation Slurm array job(s)"
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
    parser.add_argument(
        "--no-restart", dest="with_restart", action="store_false", default=True,
        help="Skip the trailing tileserver-restart job (default: submit it "
             "with afterok dependency on every tile array)",
    )
    parser.add_argument(
        "--only-bucket", dest="only_buckets", action="append", default=None,
        help="Restrict the eligible-buckets set to these names "
             "(repeatable). Use for partial rebuilds (e.g. "
             "'--only-bucket osm --only-bucket ohm --only-bucket osm_misc') "
             "when only specific buckets need regeneration; without this "
             "flag the event-log fallback may re-queue every bucket that "
             "ever completed aat_enrich.",
    )
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
        with_restart=args.with_restart,
        only_buckets=args.only_buckets,
    )


if __name__ == "__main__":
    main()
