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
from typing import Iterable


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


def create_run_manifest(manifest_path: Path, run_id: str, selected_namespaces: list[str]) -> dict:
    """Create a new run manifest; fail if path already exists."""
    if manifest_path.exists():
        raise FileExistsError(f"Run manifest already exists: {manifest_path}")

    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selected_namespaces": selected_namespaces,
        "namespaces": {
            ns: {
                "status": "pending",
                "scripts": {},
                "stages": {
                    "extract": "pending",
                    "h3": "pending",
                    "ccode": "pending",
                },
            }
            for ns in selected_namespaces
        },
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
    """Build lightweight orchestration plan buckets for controller usage."""
    plan = {"pending": [], "running": [], "failed": [], "completed": []}
    ns_data = manifest.get("namespaces", {})
    for ns in selected_namespaces:
        status = ns_data.get(ns, {}).get("status", "pending")
        if status not in plan:
            status = "pending"
        plan[status].append(ns)
    return plan


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
    """Return whether all selected namespaces completed required preprocessing stages."""
    selected = manifest.get("selected_namespaces", [])
    incomplete: dict[str, list[str]] = {}
    ns_data = manifest.get("namespaces", {})
    for ns in selected:
        missing = []
        ns_stages = ns_data.get(ns, {}).get("stages", {})
        for stage in required_stages:
            if ns_stages.get(stage) != "completed":
                missing.append(stage)
        if missing:
            incomplete[ns] = missing
    return (len(incomplete) == 0, incomplete)


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


