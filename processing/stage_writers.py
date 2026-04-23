"""Stage writer utilities for namespace-scoped staged artefacts.

This module records lightweight stage events and writes namespace-scoped
extract snapshots under the staging directory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch
import pyarrow as pa
import pyarrow.parquet as pq

from processing.settings import (
    GEOM_STORE_STAGING_DIR,
    NAMESPACE_RUNTIME_HISTORY_FILE,
    STAGED_BASE_DIR,
    STAGED_RUNS_DIR,
    STAGED_STAGE_DIR_TEMPLATE,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_dir(namespace: str, stage: str) -> Path:
    stage_path = STAGED_STAGE_DIR_TEMPLATE.format(
        base=STAGED_BASE_DIR,
        namespace=namespace,
        stage=stage,
    )
    return Path(stage_path)


def _utc_now_path_safe() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _runtime_history_path(run_id: str) -> Path:
    runs_dir = Path(STAGED_RUNS_DIR)
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir / f"{run_id}.runtime.jsonl"


def _geometry_staging_artefacts(namespace: str) -> list[str]:
    base = Path(GEOM_STORE_STAGING_DIR)
    if not base.exists():
        return []
    artefacts = sorted(str(path) for path in base.glob(f"{namespace}*.bin"))
    artefacts.extend(sorted(str(path) for path in base.glob(f"{namespace}*.index.json")))
    return artefacts


def _augment_doc_for_stage(source_doc: dict[str, Any]) -> dict[str, Any]:
    doc = json.loads(json.dumps(source_doc))
    place_id = doc.get("place_id")
    geometries = doc.get("geometries") or []
    if isinstance(geometries, list):
        staged_geometries = []
        for idx, geom in enumerate(geometries):
            if not isinstance(geom, dict):
                staged_geometries.append(geom)
                continue
            staged_geom = dict(geom)
            staged_geom.setdefault("geometry_index", idx)
            staged_geom["geom_ref"] = f"{place_id}_{idx}" if place_id and staged_geom.get("has_geom") else None
            staged_geometries.append(staged_geom)
        doc["geometries"] = staged_geometries
    return doc


def _write_parquet_batches(out_file: Path, rows: list[dict[str, Any]], writer: pq.ParquetWriter | None) -> pq.ParquetWriter:
    table = pa.Table.from_pylist(rows)
    if writer is None:
        writer = pq.ParquetWriter(str(out_file), table.schema)
    writer.write_table(table)
    return writer


def write_stage_event(
    *,
    run_id: str,
    namespace: str,
    script_id: str,
    status: str,
    stage: str = "extract",
    error: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> Path:
    """Append one JSON event to namespace stage events log.

    Returns the log path written to.
    """
    payload: dict[str, Any] = {
        "run_id": run_id,
        "namespace": namespace,
        "script_id": script_id,
        "stage": stage,
        "status": status,
        "timestamp": _utc_now_iso(),
    }
    if error:
        payload["error"] = error
    if metrics:
        payload["metrics"] = metrics

    out_dir = _stage_dir(namespace, stage)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "events.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")
    return log_path


def write_runtime_history_event(
    *,
    run_id: str,
    event: str,
    status: str,
    namespace: str | None = None,
    script_id: str | None = None,
    stage: str | None = None,
    slurm_job_id: str | None = None,
    slurm_array_task_id: str | None = None,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> Path:
    """Append one JSON event to run-scoped runtime history.

    This complements per-namespace stage logs with controller/job lifecycle
    history that can be tailed independently from artefact directories.
    """
    payload: dict[str, Any] = {
        "run_id": run_id,
        "event": event,
        "status": status,
        "timestamp": _utc_now_iso(),
    }
    if namespace:
        payload["namespace"] = namespace
    if script_id:
        payload["script_id"] = script_id
    if stage:
        payload["stage"] = stage
    if slurm_job_id:
        payload["slurm_job_id"] = slurm_job_id
    if slurm_array_task_id:
        payload["slurm_array_task_id"] = slurm_array_task_id
    if error:
        payload["error"] = error
    if details:
        payload["details"] = details

    log_path = _runtime_history_path(run_id)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")
    return log_path


def record_script_wall_time(
    *,
    namespace: str,
    script_id: str,
    run_id: str,
    started_at: str,
    finished_at: str,
    wall_seconds: float,
    status: str,
    slurm_job_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a wall-time record to the **persistent cross-run** namespace runtime
    history file.

    This file lives at ``NAMESPACE_RUNTIME_HISTORY_FILE`` (default
    ``{STAGED_BASE_DIR}/namespace-runtime-history.json``) and accumulates timing
    data across all runs so that Slurm job submission can estimate sensible
    ``--time`` allocations from historical runtimes.

    Structure::

        {
          "nl": {
            "nl-places": [
              {
                "run_id": "ingest-20260423T...",
                "started_at": "2026-04-23T...",
                "finished_at": "2026-04-23T...",
                "wall_seconds": 47.3,
                "status": "completed",
                "slurm_job_id": "22862393"
              }
            ]
          }
        }

    The file is updated atomically (write-tmp / replace) so concurrent writers
    are unlikely to corrupt it, though true concurrent access should use a lock.
    """
    history_path = Path(NAMESPACE_RUNTIME_HISTORY_FILE)
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing history (tolerant of missing or empty file)
    history: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            history = {}

    record: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_seconds": round(wall_seconds, 1),
        "status": status,
    }
    if slurm_job_id:
        record["slurm_job_id"] = slurm_job_id
    if extra:
        record.update(extra)

    ns_history = history.setdefault(namespace, {})
    script_history = ns_history.setdefault(script_id, [])
    script_history.append(record)

    # Atomic write
    tmp = history_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(history, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(history_path)


def estimate_wall_time_seconds(namespace: str, script_id: str, default: int = 86400) -> int:
    """Return an estimated wall-time in seconds for a namespace/script pair.

    Looks up the median of the last 5 completed run times from the persistent
    runtime history file.  Falls back to *default* (24 h) when no history
    exists.  Adds a 20 % safety margin.
    """
    history_path = Path(NAMESPACE_RUNTIME_HISTORY_FILE)
    if not history_path.exists():
        return default

    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default

    records = history.get(namespace, {}).get(script_id, [])
    completed = [r["wall_seconds"] for r in records if r.get("status") == "completed"]
    if not completed:
        return default

    # Median of last 5
    sample = sorted(completed[-5:])
    median = sample[len(sample) // 2]
    return int(median * 1.20)  # 20 % safety margin


def write_namespace_places_snapshot_jsonl(
    *,
    es_client: Elasticsearch,
    index_name: str,
    namespace: str,
    run_id: str,
    batch_size: int = 1000,
    max_docs: int | None = None,
) -> dict[str, Any]:
    """Write a namespace-scoped places snapshot into staged extract artefacts.

    This is a Batch 4 starter path that provides a canonical staged extract file
    (`places.jsonl`) and sidecar metadata. It uses scroll-based streaming to avoid
    loading all documents in memory.
    """
    out_dir = _stage_dir(namespace, "extract")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "places.jsonl"

    query = {
        "query": {"prefix": {"place_id": f"{namespace}:"}},
        "size": batch_size,
        "sort": ["_doc"],
    }

    docs_written = 0
    scroll = "5m"
    resp = es_client.search(index=index_name, body=query, scroll=scroll)
    scroll_id = resp.get("_scroll_id")

    with out_file.open("w", encoding="utf-8") as f:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break

            for hit in hits:
                src = hit.get("_source", {})
                f.write(json.dumps(src, ensure_ascii=True) + "\n")
                docs_written += 1
                if max_docs is not None and docs_written >= max_docs:
                    break

            if max_docs is not None and docs_written >= max_docs:
                break

            resp = es_client.scroll(scroll_id=scroll_id, scroll=scroll)

    if scroll_id:
        try:
            es_client.clear_scroll(scroll_id=scroll_id)
        except Exception:
            pass

    metadata = {
        "run_id": run_id,
        "namespace": namespace,
        "index": index_name,
        "docs_written": docs_written,
        "generated_at": _utc_now_iso(),
        "path": str(out_file),
    }
    (out_dir / "places.snapshot.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return metadata


def write_namespace_places_snapshot_parquet(
    *,
    es_client: Elasticsearch,
    index_name: str,
    namespace: str,
    run_id: str,
    batch_size: int = 1000,
    max_docs: int | None = None,
) -> dict[str, Any]:
    """Write a namespace-scoped staged places snapshot as Parquet.

    Rows retain the full ES document shape and augment each geometry entry with
    ``geometry_index`` and ``geom_ref`` for downstream geometry-store lookups.
    """
    out_dir = _stage_dir(namespace, "extract")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "places.parquet"

    query = {
        "query": {"prefix": {"place_id": f"{namespace}:"}},
        "size": batch_size,
        "sort": ["_doc"],
    }

    docs_written = 0
    scroll = "5m"
    resp = es_client.search(index=index_name, body=query, scroll=scroll)
    scroll_id = resp.get("_scroll_id")
    writer: pq.ParquetWriter | None = None

    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break

            batch_rows: list[dict[str, Any]] = []
            for hit in hits:
                src = hit.get("_source", {})
                batch_rows.append(_augment_doc_for_stage(src))
                docs_written += 1
                if max_docs is not None and docs_written >= max_docs:
                    break

            if batch_rows:
                writer = _write_parquet_batches(out_file, batch_rows, writer)

            if max_docs is not None and docs_written >= max_docs:
                break

            resp = es_client.scroll(scroll_id=scroll_id, scroll=scroll)
    finally:
        if writer is not None:
            writer.close()
        if scroll_id:
            try:
                es_client.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass

    metadata = {
        "run_id": run_id,
        "namespace": namespace,
        "index": index_name,
        "docs_written": docs_written,
        "generated_at": _utc_now_iso(),
        "path": str(out_file),
        "geometry_staging_artefacts": _geometry_staging_artefacts(namespace),
        "snapshot_id": f"{namespace}-{_utc_now_path_safe()}",
    }
    (out_dir / "places.snapshot.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return metadata


