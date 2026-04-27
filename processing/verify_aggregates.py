#!/usr/bin/env python3
"""Verify per-gazetteer aggregate files against the contracts.

Covers the Batch 6 / Batch 9 validation gates:

* Every selected non-global namespace has a non-sentinel
  ``staged/_aggregates/{ns}.h3_coverage.json`` (Batch 6).
* Every selected global namespace's coverage file carries the ``"global"``
  sentinel (Batch 6).
* Every selected per-gazetteer namespace has a
  ``staged/_aggregates/{ns}.temporal_extent.json`` (Batch 9), validated by
  ``validate_temporal_extent_aggregate``.
* Sampled non-global cell lists round-trip through ``h3.uncompact_cells``
  (Batch 6 gate explicitly named in the plan) — verifies the cells are
  valid and the compacted set is internally consistent.

Exit codes: ``0`` all gates pass, ``1`` if any gate fails, ``2`` on input
errors (manifest missing, etc.).

Usage::

    python -m processing.verify_aggregates --run-id <RUN_ID>
    python -m processing.verify_aggregates --run-id <RUN_ID> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from processing.gazetteer_h3_coverage import aggregate_path_for as h3_path_for
from processing.gazetteer_temporal_extent import aggregate_path_for as temporal_path_for
from processing.settings import (
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_contract import (
    GLOBAL_COVERAGE_NAMESPACES,
    H3_COVERAGE_GLOBAL_SENTINEL,
    is_relations_only,
    validate_h3_coverage_aggregate,
    validate_temporal_extent_aggregate,
)
from processing.staging_orchestrator import (
    load_run_manifest,
    resolve_run_manifest_path,
)

try:
    import h3 as _h3
    _H3_AVAILABLE = True
except ImportError:
    _H3_AVAILABLE = False


# How many cells to round-trip per non-global namespace as a smoke test.
_ROUND_TRIP_SAMPLE = 64


def _verify_h3_for(namespace: str) -> dict[str, Any]:
    """Verify the h3_coverage aggregate for one namespace."""
    path = h3_path_for(namespace)
    result: dict[str, Any] = {"path": str(path), "ok": True, "errors": []}

    if not path.exists():
        result["ok"] = False
        result["errors"].append("file missing")
        return result

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result["ok"] = False
        result["errors"].append(f"invalid JSON: {exc}")
        return result

    try:
        validate_h3_coverage_aggregate(payload)
    except ValueError as exc:
        result["ok"] = False
        result["errors"].append(f"contract violation: {exc}")
        return result

    coverage = payload.get("coverage")
    is_global = namespace in GLOBAL_COVERAGE_NAMESPACES
    result["is_global"] = is_global

    if is_global:
        if coverage != H3_COVERAGE_GLOBAL_SENTINEL:
            result["ok"] = False
            result["errors"].append(
                f"global namespace must use sentinel {H3_COVERAGE_GLOBAL_SENTINEL!r}; "
                f"got {coverage!r}"
            )
        return result

    if not isinstance(coverage, list):
        result["ok"] = False
        result["errors"].append(
            f"non-global coverage must be a list; got {type(coverage).__name__}"
        )
        return result
    if not coverage:
        # An empty coverage list is suspicious — every per-gazetteer namespace
        # has at least one record after the H3 stage. Flag but do not fail.
        result["errors"].append("WARNING: cell list is empty")

    result["cell_count_compacted"] = len(coverage)

    # Round-trip via uncompact + recompact: should be a no-op for a well-formed
    # compacted set. h3 requires uniform resolution per call, so we group the
    # sample by resolution and check each group independently. Sampling keeps
    # the check cheap on huge coverages.
    if _H3_AVAILABLE and coverage:
        sample = coverage[: _ROUND_TRIP_SAMPLE]
        by_resolution: dict[int, list[str]] = {}
        for cell in sample:
            try:
                res = _h3.get_resolution(cell)
            except Exception as exc:
                result["ok"] = False
                result["errors"].append(f"invalid cell {cell!r}: {exc}")
                return result
            by_resolution.setdefault(res, []).append(cell)
        for res, group in by_resolution.items():
            try:
                # Uncompact at the group's own resolution (no-op since the
                # group is already at res); the round-trip exercises the
                # parser more than the compactor itself.
                uncompacted = _h3.uncompact_cells(group, res)
                recompacted = _h3.compact_cells(list(uncompacted))
            except Exception as exc:
                result["ok"] = False
                result["errors"].append(
                    f"round-trip failed at res {res}: {exc}"
                )
                return result
            extra = set(recompacted) - set(group)
            if extra:
                result["ok"] = False
                result["errors"].append(
                    f"round-trip introduced unexpected cells at res {res}: "
                    f"{sorted(extra)[:5]}…"
                )

    return result


def _verify_temporal_extent_for(namespace: str) -> dict[str, Any]:
    path = temporal_path_for(namespace)
    result: dict[str, Any] = {"path": str(path), "ok": True, "errors": []}

    if not path.exists():
        result["ok"] = False
        result["errors"].append("file missing")
        return result

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result["ok"] = False
        result["errors"].append(f"invalid JSON: {exc}")
        return result

    try:
        validate_temporal_extent_aggregate(payload)
    except ValueError as exc:
        result["ok"] = False
        result["errors"].append(f"contract violation: {exc}")
        return result

    extent = payload.get("temporal_extent") or [None, None]
    start, end = extent[0], extent[1]
    if start is not None and end is not None and start > end:
        result["ok"] = False
        result["errors"].append(
            f"temporal_extent[0]={start} > temporal_extent[1]={end}"
        )
    result["temporal_extent"] = extent
    result["record_count"] = payload.get("record_count")
    return result


def verify(manifest_path: Path) -> dict[str, Any]:
    manifest = load_run_manifest(manifest_path)
    selected = [
        ns for ns in manifest.get("selected_namespaces", [])
        if not is_relations_only(ns)
    ]

    h3_report: dict[str, dict[str, Any]] = {}
    temporal_report: dict[str, dict[str, Any]] = {}

    for ns in selected:
        h3_report[ns] = _verify_h3_for(ns)
        temporal_report[ns] = _verify_temporal_extent_for(ns)

    h3_failed = [ns for ns, r in h3_report.items() if not r["ok"]]
    temporal_failed = [ns for ns, r in temporal_report.items() if not r["ok"]]

    return {
        "run_id": manifest.get("run_id"),
        "manifest_path": str(manifest_path),
        "selected_namespaces": selected,
        "h3_coverage": h3_report,
        "temporal_extent": temporal_report,
        "h3_failed": h3_failed,
        "temporal_failed": temporal_failed,
        "ok": not h3_failed and not temporal_failed,
    }


def _format_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("AGGREGATE VERIFICATION")
    lines.append("=" * 78)
    lines.append(f"Run: {report['run_id']}")
    lines.append(f"Manifest: {report['manifest_path']}")
    lines.append("")

    lines.append("H3 coverage:")
    for ns, entry in sorted(report["h3_coverage"].items()):
        marker = "✓" if entry["ok"] else "✗"
        kind = "global" if entry.get("is_global") else (
            f"{entry.get('cell_count_compacted', 0):,} cells"
        )
        lines.append(f"  {marker} {ns:<8} {kind}")
        for err in entry["errors"]:
            lines.append(f"      {err}")

    lines.append("")
    lines.append("Temporal extent:")
    for ns, entry in sorted(report["temporal_extent"].items()):
        marker = "✓" if entry["ok"] else "✗"
        extent = entry.get("temporal_extent")
        rc = entry.get("record_count")
        suffix = ""
        if extent is not None:
            suffix = f" extent={extent} records={rc}"
        lines.append(f"  {marker} {ns:<8}{suffix}")
        for err in entry["errors"]:
            lines.append(f"      {err}")

    lines.append("")
    lines.append("=" * 78)
    if report["ok"]:
        lines.append("✓ All aggregate gates pass.")
    else:
        lines.append("✗ Aggregate verification FAILED.")
        if report["h3_failed"]:
            lines.append(f"  H3 failures: {', '.join(report['h3_failed'])}")
        if report["temporal_failed"]:
            lines.append(f"  Temporal failures: {', '.join(report['temporal_failed'])}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify per-gazetteer aggregate files against contracts"
    )
    parser.add_argument("--run-id", help="Run ID (default: latest manifest)")
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of the text report")
    args = parser.parse_args()

    if args.manifest_path:
        manifest_path = Path(args.manifest_path)
    else:
        resolved = resolve_run_manifest_path(
            Path(STAGED_RUNS_DIR), run_id=args.run_id
        )
        if resolved is None:
            print("ERROR: no run manifest found", file=sys.stderr)
            return 2
        manifest_path = resolved

    if not manifest_path.exists():
        print(f"ERROR: run manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    report = verify(manifest_path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
