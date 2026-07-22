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


# Sanity clamp on individual year readings before they reach the aggregate.
# Catches obvious upstream typos (OHM's ``end_date=20222`` for what was
# clearly meant to be ``2022``, ``-99999`` placeholders, etc.) without
# distorting legitimate historical/contemporary content. The clamp is
# applied **per year reading**, not to the final aggregate — so a single
# bogus reading on one record can't poison the whole namespace's extent.
#
# Default range: -10000 (oldest known human civilizations) through
# ``current_year + 100`` (allows reasonable forecast / planning data).
# Per-namespace overrides below extend the range for sources that
# legitimately go deeper in time (geological epochs, palaeontology, etc.).
DEFAULT_CLAMP_MIN = -10_000

# Namespaces whose corpus legitimately exceeds the default range. po
# (PeriodO) contains geological-epoch records — the Hadean's start is
# ~4.568 billion years ago — so we widen its lower bound to keep them.
_NAMESPACE_CLAMP_OVERRIDES: dict[str, tuple[int, int]] = {
    "po": (-5_000_000_000, 10_000),
}


def _default_clamp_max() -> int:
    """Lazy default for upper clamp; computed at run time so the value
    keeps current as the calendar advances and remains testable."""
    return datetime.now(timezone.utc).year + 100


def clamp_range_for(namespace: str) -> tuple[int, int]:
    """Return ``(min_year, max_year)`` for clamping individual year readings."""
    if namespace in _NAMESPACE_CLAMP_OVERRIDES:
        return _NAMESPACE_CLAMP_OVERRIDES[namespace]
    return DEFAULT_CLAMP_MIN, _default_clamp_max()


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
    *,
    clamp_min: int,
    clamp_max: int,
) -> tuple[int | None, int | None, int]:
    """Return ``(min_start, max_end, rejected_count)`` for one staged document.

    Year readings outside ``[clamp_min, clamp_max]`` are rejected as outliers
    (typically upstream parse bugs / typos like OHM's ``end_date=20222``).
    Rejected readings are counted but never contribute to the extent.
    Either of ``min_start`` / ``max_end`` may be ``None`` when the document
    carries no in-range parseable year on that endpoint.
    """
    min_start: int | None = None
    max_end: int | None = None
    rejected = 0

    def _scan(timespans: Any) -> None:
        nonlocal min_start, max_end, rejected
        if not isinstance(timespans, list):
            return
        for ts in timespans:
            if not isinstance(ts, dict):
                continue
            for year in _iter_year_ints(ts.get("start")):
                if year < clamp_min or year > clamp_max:
                    rejected += 1
                    continue
                if min_start is None or year < min_start:
                    min_start = year
            for year in _iter_year_ints(ts.get("end")):
                if year < clamp_min or year > clamp_max:
                    rejected += 1
                    continue
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

    return min_start, max_end, rejected


def doc_temporal_range(
    doc: dict[str, Any], namespace: str
) -> tuple[int | None, int | None]:
    """Return ``(start_year, end_year)`` for a single staged place doc.

    A thin, side-effect-free wrapper around :func:`_collect_extent_for_doc`
    exposing the *per-document* temporal span (widest ``[min(start), max(end)]``
    across the doc's geometry/toponym/relation timespans, with the same
    per-namespace outlier clamp as the namespace aggregate). Shared by the
    :mod:`processing.gazetteer_temporal_extent` aggregate and the vector-tile
    builder (``processing.generate_tiles``) so the Atlas map's per-feature date
    filter and the registry's ``temporal_extent`` use identical semantics.

    Either bound may be ``None``: ``(None, None)`` means undated (no parseable
    in-range year on either endpoint); ``(start, None)`` means an *ongoing*
    feature (a start but no end — the WHG convention for still-current
    features); ``(None, end)`` means an open-started feature.
    """
    clamp_min, clamp_max = clamp_range_for(namespace)
    min_start, max_end, _ = _collect_extent_for_doc(
        doc, clamp_min=clamp_min, clamp_max=clamp_max
    )
    return min_start, max_end


def compute_temporal_extent(
    namespace: str,
    *,
    clamp_min: int | None = None,
    clamp_max: int | None = None,
) -> dict[str, Any]:
    """Compute the aggregate dict for ``namespace`` (no IO side-effects).

    ``clamp_min`` / ``clamp_max`` override the per-namespace default
    (``clamp_range_for(namespace)``) — primarily useful for tests; production
    runs should rely on the namespace defaults.
    """
    src = _staged_namespace_source(namespace)
    if src is None:
        raise FileNotFoundError(
            f"No staged snapshot found for namespace '{namespace}'. "
            "Run extract / boundary_merge / h3_merge / ccode_merge first."
        )

    default_min, default_max = clamp_range_for(namespace)
    if clamp_min is None:
        clamp_min = default_min
    if clamp_max is None:
        clamp_max = default_max

    record_count = 0
    rejected_total = 0
    overall_min: int | None = None
    overall_max: int | None = None

    for doc in _iter_staged_docs(src):
        record_count += 1
        doc_min, doc_max, doc_rejected = _collect_extent_for_doc(
            doc, clamp_min=clamp_min, clamp_max=clamp_max,
        )
        rejected_total += doc_rejected
        if doc_min is not None and (overall_min is None or doc_min < overall_min):
            overall_min = doc_min
        if doc_max is not None and (overall_max is None or doc_max > overall_max):
            overall_max = doc_max

    return {
        "namespace": namespace,
        "record_count": record_count,
        "temporal_extent": [overall_min, overall_max],
        "source_path": str(src),
        "clamp_range": [clamp_min, clamp_max],
        "rejected_readings": rejected_total,
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
    clamp_min: int | None = None,
    clamp_max: int | None = None,
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
    payload = compute_temporal_extent(
        namespace, clamp_min=clamp_min, clamp_max=clamp_max,
    )

    # Strip non-contract keys before validation/persistence — source_path,
    # clamp_range and rejected_readings are informational only and not part
    # of the aggregate contract.
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
        "clamp_range": payload["clamp_range"],
        "rejected_readings": payload["rejected_readings"],
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
    parser.add_argument(
        "--clamp-min", type=int,
        help="Override the lower clamp on individual year readings "
             "(default: per-namespace, see clamp_range_for)",
    )
    parser.add_argument(
        "--clamp-max", type=int,
        help="Override the upper clamp on individual year readings "
             "(default: per-namespace, see clamp_range_for)",
    )
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
        clamp_min=args.clamp_min,
        clamp_max=args.clamp_max,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
