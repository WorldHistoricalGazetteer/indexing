"""Batch 3 orchestration helpers.

This module provides lightweight, dependency-free utilities for:
- resolving selected authorities from `authority-selection.md`
- cleaning staged artefacts for deselected authorities
- writing run-level checkpoints
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from processing.staging_contract import (
    RELATIONS_ONLY_NAMESPACES,
    is_relations_only,
    partition_namespaces,
)


_CHECKBOX_RE = re.compile(r"^\s*-\s*\[(?P<state>[xX\s])]\s*`(?P<name>[^`]+)`")


def generate_run_id(prefix: str = "ingest") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{ts}"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_run_manifest(manifest_path: Path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _default_per_gazetteer_stages() -> dict[str, str]:
    return {
        "extract": "pending",
        "update_patch": "pending",
        "update_merge": "pending",
        "boundary": "pending",
        "boundary_merge": "pending",
        "h3": "pending",
        "h3_merge": "pending",
        "h3_coverage": "pending",
        "ccode": "pending",
        "ccode_merge": "pending",
        "tiles": "pending",
        "temporal_extent": "pending",
        "index": "pending",
    }


def _default_relations_only_stages() -> dict[str, str]:
    # Relations-only namespaces (LOC) are consumed by Batch 12 only; the
    # per-gazetteer pipeline stages do not apply.
    return {"hard_link_harvest": "pending"}


def create_run_manifest(
    manifest_path: Path,
    run_id: str,
    selected_namespaces: list[str],
    *,
    run_mode: str = "full",
    update_namespaces: list[str] | None = None,
) -> dict:
    """Create a new run manifest; fail if path already exists.

    Args:
        run_mode: ``"full"`` (default) re-stages every selected namespace.
            ``"partial"`` re-stages only ``update_namespaces``; staged artefacts
            for namespaces in ``selected_namespaces`` but not in
            ``update_namespaces`` are left in place untouched and treated as
            barrier inputs alongside the freshly produced ones.
        update_namespaces: required when ``run_mode='partial'``.
    """
    if manifest_path.exists():
        raise FileExistsError(f"Run manifest already exists: {manifest_path}")

    if run_mode not in ("full", "partial"):
        raise ValueError(f"run_mode must be 'full' or 'partial', got {run_mode!r}")
    if run_mode == "partial":
        if not update_namespaces:
            raise ValueError("partial run_mode requires update_namespaces")
        unknown = set(update_namespaces) - set(selected_namespaces)
        if unknown:
            raise ValueError(
                f"update_namespaces contains entries not in selected_namespaces: "
                f"{sorted(unknown)}"
            )

    update_set = set(update_namespaces or selected_namespaces)

    namespaces: dict[str, dict] = {}
    for ns in selected_namespaces:
        if is_relations_only(ns):
            stages = _default_relations_only_stages()
        else:
            stages = _default_per_gazetteer_stages()

        ns_entry: dict = {
            "status": "pending",
            "scripts": {},
            "stages": stages,
            "in_update_set": ns in update_set,
        }
        # In partial mode, namespaces outside the update set are treated as
        # carried-over from a prior run; their stage states will be reconciled
        # against on-disk artefacts by the controller before the barrier check.
        if run_mode == "partial" and ns not in update_set:
            ns_entry["status"] = "carried_over"
        namespaces[ns] = ns_entry

    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "run_mode": run_mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selected_namespaces": selected_namespaces,
        "update_namespaces": sorted(update_set),
        "namespaces": namespaces,
    }
    _atomic_write_json(manifest_path, manifest)
    return manifest


def parse_authority_selection_file(selection_file: Path) -> dict[str, bool]:
    """Parse markdown checkbox entries from authority-selection file."""
    selected: dict[str, bool] = {}
    if not selection_file.exists():
        return selected

    for line in selection_file.read_text(encoding="utf-8").splitlines():
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        checked = m.group("state").lower() == "x"
        selected[name] = checked
    return selected


def resolve_selected_authorities(
    selection_file: Path,
    known_namespaces: Iterable[str],
) -> list[str]:
    """Resolve selected local authorities from markdown selection file.

    Unknown entries (e.g. `whg:892`) are ignored here; this function returns only
    local namespaces that are in `known_namespaces`.
    """
    known = set(known_namespaces)
    selected_map = parse_authority_selection_file(selection_file)
    selected = [ns for ns in known_namespaces if selected_map.get(ns, False) and ns in known]
    return selected


def cleanup_deselected_staged_artefacts(
    staged_base_dir: Path,
    selected_namespaces: Iterable[str],
    known_namespaces: Iterable[str],
) -> list[str]:
    """Delete staged namespace directories for deselected authorities only."""
    selected = set(selected_namespaces)
    removed: list[str] = []
    for ns in known_namespaces:
        if ns in selected:
            continue
        ns_dir = staged_base_dir / ns
        if ns_dir.exists() and ns_dir.is_dir():
            shutil.rmtree(ns_dir)
            removed.append(ns)
    return removed


def load_or_init_run_manifest(manifest_path: Path, run_id: str, selected_namespaces: list[str]) -> dict:
    # Backward-compatible shim used by existing callers.
    if manifest_path.exists():
        return load_run_manifest(manifest_path)
    return create_run_manifest(manifest_path, run_id, selected_namespaces)


def update_namespace_checkpoint(
    manifest_path: Path,
    namespace: str,
    script_id: str,
    status: str,
    error: str | None = None,
) -> None:
    manifest = load_run_manifest(manifest_path)
    ns_entry = manifest.setdefault("namespaces", {}).setdefault(namespace, {"status": "pending", "scripts": {}})
    scripts = ns_entry.setdefault("scripts", {})
    scripts[script_id] = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if error:
        scripts[script_id]["error"] = error

    script_statuses = [v.get("status") for v in scripts.values()]
    if script_statuses and all(s == "completed" for s in script_statuses):
        ns_entry["status"] = "completed"
    elif any(s == "failed" for s in script_statuses):
        ns_entry["status"] = "failed"
    elif any(s == "running" for s in script_statuses):
        ns_entry["status"] = "running"

    _atomic_write_json(manifest_path, manifest)


def update_namespace_stage_status(
    manifest_path: Path,
    namespace: str,
    stage: str,
    status: str,
    *,
    error: str | None = None,
    metrics: dict | None = None,
) -> None:
    """Update a namespace preprocessing stage status in the run manifest."""
    manifest = load_run_manifest(manifest_path)
    ns_entry = manifest.setdefault("namespaces", {}).setdefault(
        namespace,
        {"status": "pending", "scripts": {}, "stages": {}},
    )
    stages = ns_entry.setdefault("stages", {})
    stages[stage] = status
    if error:
        stage_errors = ns_entry.setdefault("stage_errors", {})
        stage_errors[stage] = error
    if metrics:
        stage_metrics = ns_entry.setdefault("stage_metrics", {})
        stage_metrics[stage] = metrics
    _atomic_write_json(manifest_path, manifest)


def get_namespace_stage_status(manifest: dict, namespace: str, stage: str) -> str | None:
    return manifest.get("namespaces", {}).get(namespace, {}).get("stages", {}).get(stage)


def get_script_checkpoint(manifest_path: Path, namespace: str, script_id: str) -> str | None:
    """Return status for a namespace/script checkpoint if present."""
    if not manifest_path.exists():
        return None
    manifest = load_run_manifest(manifest_path)
    return (
        manifest.get("namespaces", {})
        .get(namespace, {})
        .get("scripts", {})
        .get(script_id, {})
        .get("status")
    )


def should_skip_script(manifest_path: Path, namespace: str, script_id: str) -> bool:
    """Checkpoint rule for resume: skip only already completed scripts."""
    return get_script_checkpoint(manifest_path, namespace, script_id) == "completed"


def mark_run_resumed(manifest_path: Path) -> dict:
    manifest = load_run_manifest(manifest_path)
    manifest["resumed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["resume_count"] = int(manifest.get("resume_count", 0)) + 1
    _atomic_write_json(manifest_path, manifest)
    return manifest


def summarize_run_manifest(manifest: dict) -> dict:
    namespaces = manifest.get("namespaces", {})
    statuses = [entry.get("status", "pending") for entry in namespaces.values()]
    summary = {
        "total_namespaces": len(statuses),
        "completed": sum(1 for s in statuses if s == "completed"),
        "failed": sum(1 for s in statuses if s == "failed"),
        "running": sum(1 for s in statuses if s == "running"),
        "pending": sum(1 for s in statuses if s == "pending"),
    }
    return summary


def build_fanout_plan(selected_namespaces: list[str], manifest: dict) -> dict:
    """Build lightweight orchestration plan buckets for controller usage.

    Only per-gazetteer namespaces are included; relations-only namespaces
    (LOC) skip the per-gazetteer pipeline entirely. Use ``relations_only_in_run``
    to retrieve those for Batch 12.
    """
    plan = {"pending": [], "running": [], "failed": [], "completed": []}
    ns_data = manifest.get("namespaces", {})
    per_gazetteer, _ = partition_namespaces(selected_namespaces)
    for ns in per_gazetteer:
        status = ns_data.get(ns, {}).get("status", "pending")
        if status == "carried_over":
            # Treat carried-over namespaces (partial runs) as completed for the
            # purposes of the per-gazetteer fan-out — they will not be
            # re-staged and the barrier reconciler vouches for their artefacts.
            status = "completed"
        if status not in plan:
            status = "pending"
        plan[status].append(ns)
    return plan


def relations_only_in_run(manifest: dict) -> list[str]:
    """Return relations-only namespaces selected in this run (Batch 12 input)."""
    selected = manifest.get("selected_namespaces", [])
    _, relations_only = partition_namespaces(selected)
    return relations_only


def check_completion_barrier(manifest: dict) -> tuple[bool, list[str]]:
    """Return (is_complete, incomplete_namespaces) for selected authority set."""
    selected = manifest.get("selected_namespaces", [])
    ns_data = manifest.get("namespaces", {})
    incomplete = [ns for ns in selected if ns_data.get(ns, {}).get("status") != "completed"]
    return (len(incomplete) == 0, incomplete)


def check_preprocessing_barrier(
    manifest: dict,
    required_stages: tuple[str, ...] = ("extract", "h3", "ccode"),
) -> tuple[bool, dict[str, list[str]]]:
    """Return whether all selected namespaces completed required preprocessing stages.

    Relations-only namespaces (e.g. LOC) are excluded from this check entirely:
    they have no per-gazetteer stages and are consumed only by Batch 12.
    """
    selected = manifest.get("selected_namespaces", [])
    incomplete: dict[str, list[str]] = {}
    ns_data = manifest.get("namespaces", {})
    for ns in selected:
        if is_relations_only(ns):
            continue
        missing = []
        ns_stages = ns_data.get(ns, {}).get("stages", {})
        for stage in required_stages:
            if ns_stages.get(stage) != "completed":
                missing.append(stage)
        if missing:
            incomplete[ns] = missing
    return (len(incomplete) == 0, incomplete)


# ---------------------------------------------------------------------------
# Batch 8: Global barrier — all selected gazetteers preprocessed
# ---------------------------------------------------------------------------

# Stages every per-gazetteer namespace must reach before corpus-wide phases
# (Batch 9 toponyms/Symphonym, Batch 11 indexing, Batch 12 hard-link harvest)
# may start. ``boundary`` / ``boundary_merge`` are tolerated as ``skipped``
# for non-OSM/OHM namespaces; ``ccode`` / ``ccode_merge`` are tolerated as
# ``skipped`` for ``un`` (UN is the ccode authority) and ``tiles`` is
# tolerated as ``skipped`` for namespaces that don't contribute tile features.
GLOBAL_BARRIER_REQUIRED_STAGES: tuple[str, ...] = (
    "extract",
    "boundary_merge",
    "h3",
    "h3_merge",
    "h3_coverage",
    "ccode",
    "ccode_merge",
)

# Statuses that satisfy a stage requirement. ``skipped`` is treated as a pass
# because the orchestrator emits it for stages that don't apply to a given
# namespace (e.g. boundary stages on non-OSM authorities).
_BARRIER_PASSING_STATUSES = frozenset({"completed", "skipped"})


def check_global_barrier(
    manifest: dict,
    *,
    required_stages: tuple[str, ...] = GLOBAL_BARRIER_REQUIRED_STAGES,
) -> tuple[bool, dict[str, dict[str, str]]]:
    """Return ``(is_complete, report)`` for the Batch 8 global barrier.

    The report is keyed by namespace. Each value is a dict of
    ``{stage: observed_status}`` for every stage in ``required_stages``,
    plus a synthetic ``__missing__`` entry listing stages that did not pass.
    Stages with status in ``_BARRIER_PASSING_STATUSES`` count as a pass.

    Relations-only namespaces (e.g. ``loc``) are excluded — they have no
    per-gazetteer pipeline and are consumed only by Batch 12.

    The barrier is complete iff every selected per-gazetteer namespace has
    every required stage in a passing status.
    """
    selected = manifest.get("selected_namespaces", [])
    ns_data = manifest.get("namespaces", {})
    report: dict[str, dict[str, str]] = {}
    all_pass = True
    for ns in selected:
        if is_relations_only(ns):
            continue
        ns_stages = ns_data.get(ns, {}).get("stages", {})
        per_stage: dict[str, str] = {}
        missing: list[str] = []
        for stage in required_stages:
            status = ns_stages.get(stage, "pending")
            per_stage[stage] = status
            if status not in _BARRIER_PASSING_STATUSES:
                missing.append(stage)
        if missing:
            all_pass = False
            per_stage["__missing__"] = ",".join(missing)
        report[ns] = per_stage
    return all_pass, report


def materialise_gazetteer_inventory(
    manifest: dict,
    *,
    staged_base_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the run-level gazetteer inventory consumed by Batch 9 and Batch 11.

    Returns a dict::

        {
            "run_id": <str>,
            "generated_at": <ISO 8601>,
            "selected_namespaces": [...],
            "per_gazetteer": [
                {"namespace": ns,
                 "stages": {stage: status, ...},
                 "h3_coverage_path": "<staged/_aggregates/{ns}.h3_coverage.json>"|None,
                 "temporal_extent_path": "<...temporal_extent.json>"|None,
                 "extract_path": "<staged/{ns}/extract/places.parquet>"|None,
                 "final_path":   "<staged/{ns}/final/places.parquet>"|None},
                ...
            ],
            "relations_only": [...],
        }

    File paths are populated only when the artefact exists on disk (when
    ``staged_base_dir`` is provided). When ``staged_base_dir`` is ``None``
    the path fields are returned as ``None``.
    """
    selected = manifest.get("selected_namespaces", [])
    ns_data = manifest.get("namespaces", {})
    per_gazetteer: list[dict[str, Any]] = []
    relations_only: list[str] = []

    for ns in selected:
        if is_relations_only(ns):
            relations_only.append(ns)
            continue

        stages = dict(ns_data.get(ns, {}).get("stages", {}))
        entry: dict[str, Any] = {
            "namespace": ns,
            "stages": stages,
            "h3_coverage_path": None,
            "temporal_extent_path": None,
            "extract_path": None,
            "final_path": None,
        }
        if staged_base_dir is not None:
            agg_dir = staged_base_dir / "_aggregates"
            h3_cov = agg_dir / f"{ns}.h3_coverage.json"
            tem_ext = agg_dir / f"{ns}.temporal_extent.json"
            extract_p = staged_base_dir / ns / "extract" / "places.parquet"
            final_p = staged_base_dir / ns / "final" / "places.parquet"
            entry["h3_coverage_path"] = str(h3_cov) if h3_cov.exists() else None
            entry["temporal_extent_path"] = str(tem_ext) if tem_ext.exists() else None
            entry["extract_path"] = str(extract_p) if extract_p.exists() else None
            entry["final_path"] = str(final_p) if final_p.exists() else None
        per_gazetteer.append(entry)

    return {
        "run_id": manifest.get("run_id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selected_namespaces": list(selected),
        "per_gazetteer": per_gazetteer,
        "relations_only": relations_only,
    }


def write_gazetteer_inventory(
    inventory_path: Path,
    inventory: dict[str, Any],
) -> None:
    """Atomically write the materialised inventory to disk."""
    _atomic_write_json(inventory_path, inventory)


def format_global_barrier_report(
    is_complete: bool,
    report: dict[str, dict[str, str]],
) -> str:
    """Pretty-print a barrier report for human consumption."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("BATCH 8 — GLOBAL BARRIER")
    lines.append("=" * 78)
    if is_complete:
        lines.append("✓ All selected gazetteers ready for corpus-wide phases.")
    else:
        lines.append("✗ Barrier NOT complete — corpus-wide phases must not start.")
    lines.append("")

    if not report:
        lines.append("(no per-gazetteer namespaces selected)")
        return "\n".join(lines)

    stage_columns = sorted(
        {stage for entry in report.values() for stage in entry if stage != "__missing__"}
    )
    name_width = max(len(ns) for ns in report)
    header = f"{'namespace':<{name_width}}  " + "  ".join(
        f"{s:<14}" for s in stage_columns
    )
    lines.append(header)
    lines.append("-" * len(header))
    for ns in sorted(report):
        per_stage = report[ns]
        cells = [f"{per_stage.get(s, '-'):<14}" for s in stage_columns]
        marker = " " if "__missing__" not in per_stage else " ✗"
        lines.append(f"{ns:<{name_width}}  " + "  ".join(cells) + marker)
        if "__missing__" in per_stage:
            lines.append(f"{'':<{name_width}}    missing: {per_stage['__missing__']}")
    return "\n".join(lines)


def reconcile_carried_over_namespaces(
    manifest_path: Path,
    staged_base_dir: Path,
    required_stages: tuple[str, ...] = ("extract", "h3", "ccode"),
) -> dict[str, list[str]]:
    """For partial runs, mark carried-over namespaces' stages as ``completed``
    when the on-disk staged artefacts are present.

    Returns a dict mapping namespace → stages that could not be reconciled
    (i.e. files missing on disk). Callers should treat a non-empty result as
    a failure of the partial-run premise — those namespaces need a full re-run.
    """
    manifest = load_run_manifest(manifest_path)
    if manifest.get("run_mode") != "partial":
        return {}

    update_set = set(manifest.get("update_namespaces", []))
    ns_data = manifest.get("namespaces", {})
    failures: dict[str, list[str]] = {}

    stage_artefact_paths = {
        "extract": ("extract/places.parquet", "extract/places.jsonl"),
        "update_patch": ("update_patch/places.update.jsonl",),
        "update_merge": ("update_merged/places.parquet", "update_merged/places.jsonl"),
        "boundary": ("boundary/places.boundary.jsonl",),
        "boundary_merge": ("boundary_merged/places.parquet",),
        "h3": ("h3/places.h3.jsonl",),
        "h3_merge": ("h3_merged/places.parquet", "h3_merged/places.jsonl"),
        "ccode": ("ccode/places.ccode.jsonl",),
        "ccode_merge": ("final/places.parquet", "final/places.jsonl"),
    }

    for ns, entry in ns_data.items():
        if ns in update_set or is_relations_only(ns):
            continue
        ns_dir = staged_base_dir / ns
        missing: list[str] = []
        for stage in required_stages:
            candidates = stage_artefact_paths.get(stage, ())
            if not any((ns_dir / rel).exists() for rel in candidates):
                missing.append(stage)
                continue
            entry.setdefault("stages", {})[stage] = "completed"
        if missing:
            failures[ns] = missing
        else:
            entry["status"] = "completed"

    _atomic_write_json(manifest_path, manifest)
    return failures


def finalize_run_manifest(manifest_path: Path, run_status: str) -> dict:
    """Finalize a run manifest with terminal status and summary."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_status"] = run_status
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["summary"] = summarize_run_manifest(manifest)
    _atomic_write_json(manifest_path, manifest)
    return manifest


def resolve_run_manifest_path(runs_dir: Path, run_id: str | None = None) -> Path | None:
    """Resolve a run manifest by run_id or latest modified file in runs_dir."""
    if run_id:
        candidate = runs_dir / f"{run_id}.json"
        return candidate if candidate.exists() else None

    if not runs_dir.exists():
        return None
    candidates = sorted(
        [p for p in runs_dir.glob("*.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def list_run_manifest_paths(runs_dir: Path, limit: int = 20) -> list[Path]:
    """Return latest run manifests ordered by modification time desc."""
    if not runs_dir.exists():
        return []
    paths = sorted(
        [p for p in runs_dir.glob("*.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return paths[:limit]


