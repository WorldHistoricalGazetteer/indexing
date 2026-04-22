"""Batch 3 stage writer utilities.

This module records lightweight stage events as JSONL artefacts under the
staging directory. It is intentionally minimal and best-effort.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch

from processing.settings import STAGED_BASE_DIR, STAGED_STAGE_DIR_TEMPLATE


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_dir(namespace: str, stage: str) -> Path:
    stage_path = STAGED_STAGE_DIR_TEMPLATE.format(
        base=STAGED_BASE_DIR,
        namespace=namespace,
        stage=stage,
    )
    return Path(stage_path)


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


