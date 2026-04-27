#!/usr/bin/env python3
"""Per-gazetteer temporal extent (Batch 9, Master Plan §1.4.1 + E.2 #2).

Computes ``temporal_extent = [min(start_year), max(end_year)]`` across every
timespan on every record in a namespace's staged snapshot, and writes the
aggregate file ``staged/_aggregates/{namespace}.temporal_extent.json``
consumed by Batch 11 inventory push.

Staged docs carry timespans in three places:

* ``geometries[i].timespans[]``
* ``toponyms[i].timespans[]``
* ``relations[i].timespans[]``

Each timespan entry is shaped ``{"start": {"in": <year>} | {"earliest": ...,
"latest": ...}, "end": same shape}``. We treat *every* integer year found
under ``start`` as a candidate for ``min(start_year)``, and every integer
year under ``end`` as a candidate for ``max(end_year)``. Namespaces with no
parseable years emit ``[null, null]``.

Snapshots are read from the most-enriched staged stage available
(``final/`` → ``h3_merged/`` → ``boundary_merged/`` → ``extract/``). The
script never touches Elasticsearch.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq

from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import (
    record_script_wall_time,
    write_runtime_history_event,
    write_stage_event,
)
from processing.staging_contract import (
    AGGREGATE_TEMPORAL_EXTENT_FILENAME_TEMPLATE,
    validate_temporal_extent_aggregate,
)
from processing.staging_orchestrator import update_namespace_stage_status


_STAGED_SOURCE_PRIORITY = (
    "final",
    "h3_merged",
    "boundary_merged",
    "update_merged",
    "extract",
)


def _aggregates_dir() -> Path:
    return Path(STAGED_BASE_DIR) / "_aggregates"


def aggregate_path_for(namespace: str) -> Path:
    filename = AGGREGATE_TEMPORAL_EXTENT_FILENAME_TEMPLATE.format(namespace=namespace)
    return _aggregates_dir() / filename


def _staged_namespace_source(namespace: str) -> Path | None:
    base = Path(STAGED_BASE_DIR) / namespace
    for stage in _STAGED_SOURCE_PRIORITY:
        parquet = base / stage / "places.parquet"
        if parquet.exists():
            return parquet
        jsonl = base / stage / "places.jsonl"
        if jsonl.exists():
            return jsonl
    return None


def _iter_staged_docs(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix == ".parquet":
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=2000):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _iter_year_ints(node: Any) -> Iterator[int]:
    """Recursively yield every integer year found under a timespan endpoint.

    Accepts ``{"in": <year>}``, ``{"earliest": ..., "latest": ...}``, or any
    other shape with ``int`` leaves. Strings are not coerced — ingest is
    expected to have parsed years to ``int`` already (see
    ``processing/osm_boundary_geometry.parse_year``).
    """
    if isinstance(node, bool):
        return
    if isinstance(node, int):
        yield node
        return
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_year_ints(value)
        return
    if isinstance(node, list):
        for value in node:
            yield from _iter_year_ints(value)


def _collect_extent_for_doc(
    doc: dict[str, Any],
) -> tuple[int | None, int | None]:
    """Return (min_start, max_end) for a single staged place document.

    Either may be ``None`` when the document carries no parseable year on
    that endpoint.
    """
    min_start: int | None = None
    max_end: int | None = None

    def _scan(timespans: Any) -> None:
        nonlocal min_start, max_end
        if not isinstance(timespans, list):
            return
        for ts in timespans:
            if not isinstance(ts, dict):
                continue
            for year in _iter_year_ints(ts.get("start")):
                if min_start is None or year < min_start:
                    min_start = year
            for year in _iter_year_ints(ts.get("end")):
                if max_end is None or year > max_end:
                    max_end = year

    for geom in doc.get("geometries") or []:
        if isinstance(geom, dict):
            _scan(geom.get("timespans"))
    for top in doc.get("toponyms") or []:
        if isinstance(top, dict):
            _scan(top.get("timespans"))
    for rel in doc.get("relations") or []:
        if isinstance(rel, dict):
            _scan(rel.get("timespans"))

    return min_start, max_end


def compute_temporal_extent(namespace: str) -> dict[str, Any]:
    """Compute the aggregate dict for ``namespace`` (no IO side-effects)."""
    src = _staged_namespace_source(namespace)
    if src is None:
        raise FileNotFoundError(
            f"No staged snapshot found for namespace '{namespace}'. "
            "Run extract / boundary_merge / h3_merge / ccode_merge first."
        )

    record_count = 0
    overall_min: int | None = None
    overall_max: int | None = None

    for doc in _iter_staged_docs(src):
        record_count += 1
        doc_min, doc_max = _collect_extent_for_doc(doc)
        if doc_min is not None and (overall_min is None or doc_min < overall_min):
            overall_min = doc_min
        if doc_max is not None and (overall_max is None or doc_max > overall_max):
            overall_max = doc_max

    return {
        "namespace": namespace,
        "record_count": record_count,
        "temporal_extent": [overall_min, overall_max],
        "source_path": str(src),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def run_temporal_extent(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path | None = None,
    slurm_job_id: str | None = None,
) -> dict[str, Any]:
    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "temporal_extent", "running"
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="temporal-extent",
        status="running",
        stage="temporal_extent",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="temporal_extent",
        status="running",
        namespace=namespace,
        stage="temporal_extent",
        slurm_job_id=slurm_job_id,
    )

    started = datetime.now(timezone.utc)
    payload = compute_temporal_extent(namespace)

    # Strip non-contract keys before validation/persistence — source_path is
    # informational only and not part of the aggregate contract.
    aggregate = {
        "namespace": payload["namespace"],
        "record_count": payload["record_count"],
        "temporal_extent": payload["temporal_extent"],
    }
    validate_temporal_extent_aggregate(aggregate)

    out_path = aggregate_path_for(namespace)
    _atomic_write_json(out_path, aggregate)

    finished = datetime.now(timezone.utc)
    wall_seconds = (finished - started).total_seconds()
    metrics = {
        "aggregate_path": str(out_path),
        "record_count": payload["record_count"],
        "temporal_extent": payload["temporal_extent"],
        "source_path": payload["source_path"],
        "wall_seconds": round(wall_seconds, 1),
    }

    try:
        record_script_wall_time(
            namespace=namespace,
            script_id="temporal-extent",
            run_id=run_id,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            wall_seconds=wall_seconds,
            status="completed",
            slurm_job_id=slurm_job_id,
            extra={"record_count": payload["record_count"]},
        )
    except Exception:
        pass  # Non-fatal — history write failure must not abort the stage

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "temporal_extent", "completed", metrics=metrics
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="temporal-extent",
        status="completed",
        stage="temporal_extent",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="temporal_extent",
        status="completed",
        namespace=namespace,
        stage="temporal_extent",
        slurm_job_id=slurm_job_id,
        details=metrics,
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute per-gazetteer temporal extent from staged snapshots"
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

    import os
    slurm_job_id = os.getenv("SLURM_JOB_ID")

    metrics = run_temporal_extent(
        run_id=args.run_id,
        namespace=args.namespace,
        manifest_path=manifest_path if manifest_path.exists() else None,
        slurm_job_id=slurm_job_id,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
