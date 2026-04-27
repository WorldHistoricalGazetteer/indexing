#!/usr/bin/env python3
"""Per-gazetteer H3 coverage compaction (Batch 6, Master Plan §1.4.1 + E.2 #1).

Reads the H3 patch JSONL produced by ``processing/h3_stage.py`` and emits a
single per-namespace aggregate file describing the gazetteer's spatial
footprint:

* **Non-global namespaces** — accumulate the union of every ``h3_centroid`` and
  ``h3_cover`` cell observed in the patch, then compact via ``h3.compact_cells``
  to the smallest representation. Output::

      {"namespace": "<ns>", "coverage": ["8a2a...", ...], "compacted": true,
       "cell_count_uncompacted": <int>, "cell_count_compacted": <int>}

* **Global namespaces** (``GLOBAL_COVERAGE_NAMESPACES``) — skip cell-set
  computation entirely and emit the sentinel::

      {"namespace": "<ns>", "coverage": "global"}

Output is written to
``{STAGED_BASE_DIR}/_aggregates/{namespace}.h3_coverage.json`` and consumed by
Batch 11 (inventory push, Job A side of the post-barrier flow).

This stage runs **after** ``h3_stage.py`` and **before** Batch 7
(``ccode_enrichment.py``) — the ccode enrichment pre-filter for non-UN
namespaces also reads UN's coverage file from this same path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

from processing.settings import (
    GLOBAL_COVERAGE_NAMESPACES,
    H3_COVERAGE_GLOBAL_SENTINEL,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import write_runtime_history_event, write_stage_event
from processing.staging_contract import (
    AGGREGATE_H3_COVERAGE_FILENAME_TEMPLATE,
    H3_PATCH_GEOMETRY_REQUIRED_FIELDS,
    H3_PATCH_REQUIRED_FIELDS,
    validate_h3_coverage_aggregate,
    validate_required_fields,
)
from processing.staging_orchestrator import update_namespace_stage_status

try:
    import h3 as _h3
    _H3_AVAILABLE = True
except ImportError:
    _H3_AVAILABLE = False


def _aggregates_dir() -> Path:
    return Path(STAGED_BASE_DIR) / "_aggregates"


def aggregate_path_for(namespace: str) -> Path:
    """Return the aggregate file path for ``namespace``."""
    filename = AGGREGATE_H3_COVERAGE_FILENAME_TEMPLATE.format(namespace=namespace)
    return _aggregates_dir() / filename


def _patch_path(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / namespace / "h3" / "places.h3.jsonl"


def _iter_patch_cells(patch_path: Path) -> Iterator[str]:
    """Yield every h3 cell (centroid + cover) from a patch file.

    Malformed patch rows are silently skipped — they are already filtered by
    ``h3_merge.py`` and we do not want a single bad line to abort coverage
    compaction.
    """
    with patch_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                validate_required_fields(rec, H3_PATCH_REQUIRED_FIELDS)
            except ValueError:
                continue
            for entry in rec.get("geometries") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    validate_required_fields(entry, H3_PATCH_GEOMETRY_REQUIRED_FIELDS)
                except ValueError:
                    continue
                centroid = entry.get("h3_centroid")
                if isinstance(centroid, str) and centroid:
                    yield centroid
                cover = entry.get("h3_cover") or []
                if isinstance(cover, list):
                    for cell in cover:
                        if isinstance(cell, str) and cell:
                            yield cell


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def compact_cells(cells: set[str]) -> list[str]:
    """Compact a set of H3 cells to the smallest equivalent representation.

    ``h3.compact_cells`` requires every input cell to be at the same
    resolution. The cells coming out of ``h3_stage`` are a mix —
    ``h3_centroid`` is res 7 while ``h3_cover`` may be res 3, 5, or 7
    depending on polyfill — so we partition by resolution, compact each
    group independently, and concatenate the results.
    """
    if not cells:
        return []
    if not _H3_AVAILABLE:
        return sorted(cells)

    by_resolution: dict[int, list[str]] = {}
    for cell in cells:
        try:
            res = _h3.get_resolution(cell)
        except Exception:
            continue
        by_resolution.setdefault(res, []).append(cell)

    compacted: list[str] = []
    for res, group in by_resolution.items():
        try:
            compacted.extend(_h3.compact_cells(group))
        except Exception:
            # Fall back to the uncompacted group on any per-resolution
            # failure — better to over-state coverage than to lose cells.
            compacted.extend(group)
    return sorted(compacted)


def build_global_aggregate(namespace: str) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "coverage": H3_COVERAGE_GLOBAL_SENTINEL,
    }


def build_compacted_aggregate(namespace: str, patch_path: Path) -> dict[str, Any]:
    cells: set[str] = set()
    for cell in _iter_patch_cells(patch_path):
        cells.add(cell)

    compacted = compact_cells(cells)
    return {
        "namespace": namespace,
        "coverage": compacted,
        "compacted": True,
        "cell_count_uncompacted": len(cells),
        "cell_count_compacted": len(compacted),
    }


def run_h3_coverage(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "h3_coverage", "running"
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="h3-coverage",
        status="running",
        stage="h3_coverage",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="h3_coverage",
        status="running",
        namespace=namespace,
        stage="h3_coverage",
    )

    if namespace in GLOBAL_COVERAGE_NAMESPACES:
        payload = build_global_aggregate(namespace)
    else:
        patch_path = _patch_path(namespace)
        if not patch_path.exists():
            raise FileNotFoundError(
                f"H3 patch file not found for '{namespace}': {patch_path}. "
                "Run processing.h3_stage first."
            )
        payload = build_compacted_aggregate(namespace, patch_path)

    validate_h3_coverage_aggregate(payload)

    out_path = aggregate_path_for(namespace)
    _atomic_write_json(out_path, payload)

    metrics = {
        "aggregate_path": str(out_path),
        "is_global": namespace in GLOBAL_COVERAGE_NAMESPACES,
        "cell_count_compacted": (
            payload.get("cell_count_compacted")
            if namespace not in GLOBAL_COVERAGE_NAMESPACES
            else None
        ),
        "cell_count_uncompacted": (
            payload.get("cell_count_uncompacted")
            if namespace not in GLOBAL_COVERAGE_NAMESPACES
            else None
        ),
    }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "h3_coverage", "completed", metrics=metrics
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="h3-coverage",
        status="completed",
        stage="h3_coverage",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="h3_coverage",
        status="completed",
        namespace=namespace,
        stage="h3_coverage",
        details=metrics,
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compact per-gazetteer H3 coverage from staged patch files"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--namespace", required=True, help="Namespace")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
            runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
        )
    )

    metrics = run_h3_coverage(
        run_id=args.run_id,
        namespace=args.namespace,
        manifest_path=manifest_path if manifest_path.exists() else None,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
