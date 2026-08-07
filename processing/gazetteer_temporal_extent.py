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
from processing.temporal import coerce_year


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
    """Recursively yield every year found under a timespan endpoint.

    Accepts ``{"in": <year>}``, ``{"earliest": ..., "latest": ...}``, or any
    other shape with scalar leaves.

    **Strings ARE coerced** (place#164). They used not to be, on the assumption
    that ingest had already parsed years to ``int``. It had not: 208,937 ``whg``
    docs carry ``{"start": {"earliest": "2022"}}`` as *strings* — LPF
    ``earliest``/``latest`` are "sometimes a string ISO date, sometimes a bare
    number" (``processing/staged_parquet.py``) — so those places were computed
    as **undated** while their dates sat in the index in plain sight: no tile
    temporal props, no range-mode filtering, absent from their datasets'
    registry ``temporal_extent``. See :func:`processing.temporal.coerce_year`.
    """
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float, str)):
        year = coerce_year(node)
        if year is not None:
            yield year
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

    # The *attested envelope* (place#164): take min/max over every in-range
    # year in the doc, but only report an endpoint at all if that endpoint
    # carries a year — which preserves the open-ended conventions
    # ``(start, None)`` = ongoing and ``(None, end)`` = open-started.
    #
    # Partitioning the min to `start` and the max to `end`, as this used to,
    # inverts attestation encodings: `gb`'s survey window is
    # `start.latest 1914 / end.earliest 1888`, which returned (1914, 1888) —
    # a range that ends before it begins. Pooling the years fixes that
    # without changing any correctly-encoded lifespan.
    has_start = False
    has_end = False
    lo: int | None = None
    hi: int | None = None

    def _scan(timespans: Any) -> None:
        nonlocal min_start, max_end, rejected, has_start, has_end, lo, hi
        if not isinstance(timespans, list):
            return
        for ts in timespans:
            if not isinstance(ts, dict):
                continue
            for endpoint, seen_flag in (("start", "start"), ("end", "end")):
                for year in _iter_year_ints(ts.get(endpoint)):
                    if year < clamp_min or year > clamp_max:
                        rejected += 1
                        continue
                    if seen_flag == "start":
                        has_start = True
                    else:
                        has_end = True
                    if lo is None or year < lo:
                        lo = year
                    if hi is None or year > hi:
                        hi = year

    for geom in doc.get("geometries") or []:
        if isinstance(geom, dict):
            _scan(geom.get("timespans"))
    for top in doc.get("toponyms") or []:
        if isinstance(top, dict):
            _scan(top.get("timespans"))
    for rel in doc.get("relations") or []:
        if isinstance(rel, dict):
            _scan(rel.get("timespans"))

    min_start = lo if has_start else None
    max_end = hi if has_end else None
    return min_start, max_end, rejected


def doc_temporal_range(
    doc: dict[str, Any], namespace: str
) -> tuple[int | None, int | None]:
    """Return ``(start_year, end_year)`` for a single staged place doc.

    A thin, side-effect-free wrapper around :func:`_collect_extent_for_doc`
    exposing the *per-document* temporal span (widest ``[min(start), max(end)]``
    across the doc's geometry/toponym/relation timespans, with the same
    per-namespace outlier clamp as the namespace aggregate). Feeds the registry
    ``temporal_extent`` aggregate.

    ⚠️ **This is the attested COVERAGE extent, not the possible envelope — and
    the two must not be conflated again** (place#176). It pools every year found
    under an endpoint (``in``, ``earliest`` *and* ``latest``), so an attestation
    — ``{"start": {"latest": 2026}}``, meaning *began no later than 2026,
    unbounded before* — reports 2026 as a lower bound. That is the right reading
    for "which period does this gazetteer describe?", where ``osm`` should read
    as contemporary rather than as spanning all of time; it is the wrong reading
    for any overlap or filter test, where it asserts a beginning the source
    never claimed.

    The vector-tile builder shared this helper and inherited the over-claim,
    stamping every contemporary feature ``(2026, 2026)`` — so switching on the
    map's date filter blanked ``osm`` / ``osm_misc`` / ``tgn`` / ``nl`` on any
    historical range. It now reads :func:`doc_temporal_bounds` instead.
    **For filtering or overlap use that function, never this one.**

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


def _endpoint_bound(node: Any, keys: tuple[str, ...]) -> int | None:
    """First present, coercible year among ``keys`` under a timespan endpoint."""
    if not isinstance(node, dict):
        return None
    for key in keys:
        year = coerce_year(node.get(key))
        if year is not None:
            return year
    return None


def doc_temporal_bounds(
    doc: dict[str, Any], namespace: str
) -> tuple[int | None, int | None, int | None, int | None]:
    """Return ``(start_earliest, start_latest, end_earliest, end_latest)``.

    The four bounds the Atlas date filter needs (place#164), where
    :func:`doc_temporal_range` gives the single envelope the registry
    aggregate wants. ``None`` means **unbounded**, and that is load-bearing:

    .. code-block::

        definitely alive at Q :  start_latest <= Q <= end_earliest
        possibly  alive at Q :  (start_earliest ?? -inf) <= Q <= (end_latest ?? +inf)

    A correctly-encoded OSM boundary (attested 2026) yields
    ``(None, 2026, 2026, None)`` — not *definitely* alive in 1500, but
    *possibly* alive, because the outer bounds are absent. That is what
    dissolves the "OSM blanks out on any historical range" defect without a
    ``end = 9999`` sentinel or a "+Contemporary" toggle.

    Each bound reads its own sub-field, or ``in`` — which is exact and so
    serves as both bounds at once. There is deliberately **no cross-fallback**:
    ``start.earliest`` is a *lower* bound and cannot stand in for
    ``start_latest``, and reading it as one would manufacture a definite core
    the source never claimed.

    **Approximation:** a doc with several disjoint timespans is reduced to one
    interval — the widest definite core and the widest possible extent. This
    over-claims for genuinely disjoint spans (a place that existed 1200–1300
    and again 1600–1700). Almost every doc carries a single timespan; where
    that stops being true this should become a list.
    """
    clamp_min, clamp_max = clamp_range_for(namespace)

    def _ok(year: int | None) -> int | None:
        if year is None or year < clamp_min or year > clamp_max:
            return None
        return year

    start_earliest: int | None = None
    start_latest: int | None = None
    end_earliest: int | None = None
    end_latest: int | None = None
    saw_any = False
    # An unbounded outer edge on ANY timespan makes the doc's outer edge
    # unbounded, so track whether every timespan supplied one.
    all_have_start_earliest = True
    all_have_end_latest = True

    def _scan(timespans: Any) -> None:
        nonlocal start_earliest, start_latest, end_earliest, end_latest
        nonlocal saw_any, all_have_start_earliest, all_have_end_latest
        if not isinstance(timespans, list):
            return
        for ts in timespans:
            if not isinstance(ts, dict):
                continue
            start, end = ts.get("start"), ts.get("end")
            if not isinstance(start, dict) and not isinstance(end, dict):
                continue
            saw_any = True

            # Each bound reads its OWN sub-field, or `in` (which is exact and
            # therefore both bounds at once). No cross-fallback: `earliest` is
            # a lower bound and cannot serve as an upper one, nor `latest` as
            # a lower one — treating `start.earliest` as an upper bound on
            # start would manufacture a definite core that the source never
            # claimed.
            se = _ok(_endpoint_bound(start, ("earliest", "in")))
            sl = _ok(_endpoint_bound(start, ("latest", "in")))
            ee = _ok(_endpoint_bound(end, ("earliest", "in")))
            el = _ok(_endpoint_bound(end, ("latest", "in")))

            if se is None:
                all_have_start_earliest = False
            elif start_earliest is None or se < start_earliest:
                start_earliest = se

            if el is None:
                all_have_end_latest = False
            elif end_latest is None or el > end_latest:
                end_latest = el

            # Widest definite core across timespans.
            if sl is not None and (start_latest is None or sl < start_latest):
                start_latest = sl
            if ee is not None and (end_earliest is None or ee > end_earliest):
                end_earliest = ee

    for geom in doc.get("geometries") or []:
        if isinstance(geom, dict):
            _scan(geom.get("timespans"))
    for top in doc.get("toponyms") or []:
        if isinstance(top, dict):
            _scan(top.get("timespans"))
    for rel in doc.get("relations") or []:
        if isinstance(rel, dict):
            _scan(rel.get("timespans"))

    if not saw_any:
        return None, None, None, None
    if not all_have_start_earliest:
        start_earliest = None
    if not all_have_end_latest:
        end_latest = None
    return start_earliest, start_latest, end_earliest, end_latest


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
