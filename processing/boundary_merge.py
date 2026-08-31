#!/usr/bin/env python3
"""Merge staged boundary patches into namespace snapshots.

Reads:
  - {STAGED_BASE_DIR}/{namespace}/extract/places.parquet|places.jsonl
  - {STAGED_BASE_DIR}/{namespace}/boundary/places.boundary.jsonl

Writes:
  - {STAGED_BASE_DIR}/{namespace}/boundary_merged/places.parquet
  - {STAGED_BASE_DIR}/{namespace}/boundary_merged/places.jsonl

This stage runs before H3/ccode processing and does not access Elasticsearch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq

from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import (
    _augment_doc_for_stage,
    write_runtime_history_event,
    write_stage_event,
)
from processing.staged_parquet import (
    atomic_staged_snapshot,
    normalize_for_parquet,
)
from processing.staging_orchestrator import update_namespace_stage_status

#: Emit a progress line every N documents in the merge loop, and announce the
#: Parquet conversion separately.
#:
#: These stages used to print exactly once, on completion. That is fine at
#: small scale and actively harmful at osm's: the H3 merge ran for five hours
#: against a 20.6M-doc corpus with a zero-byte log, which is indistinguishable
#: from a hang. It cost a near-cancellation of a healthy job, and later a
#: mis-diagnosis of a dying one — the JSONL had completed and the OOM came in
#: the Parquet step, which nothing announced.
_PROGRESS_EVERY = 1_000_000


def _progress(label: str, n: int, *, every: int = _PROGRESS_EVERY) -> None:
    if n and n % every == 0:
        print(f"  {label}: {n:,} docs", flush=True)



def _iter_extract_docs(namespace: str) -> Iterable[dict[str, Any]]:
    extract_dir = Path(STAGED_BASE_DIR) / namespace / "extract"
    parquet_path = extract_dir / "places.parquet"
    jsonl_path = extract_dir / "places.jsonl"

    if parquet_path.exists():
        parquet = pq.ParquetFile(parquet_path)
        for batch in parquet.iter_batches(batch_size=2000):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row
        return

    if jsonl_path.exists():
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
        return

    raise FileNotFoundError(f"No staged extract found for namespace '{namespace}'")


def _load_boundary_patches(namespace: str) -> dict[str, dict[str, Any]]:
    patch_path = Path(STAGED_BASE_DIR) / namespace / "boundary" / "places.boundary.jsonl"
    if not patch_path.exists():
        raise FileNotFoundError(f"Boundary patch file not found: {patch_path}")

    patches: dict[str, dict[str, Any]] = {}
    with patch_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            place_id = rec.get("place_id")
            if not place_id:
                continue
            patches[place_id] = rec
    return patches


def _merge_update(doc: dict[str, Any], update_doc: dict[str, Any]) -> dict[str, Any]:
    merged = dict(doc)
    for k, v in update_doc.items():
        # Boundary stage geometry updates are authoritative for these keys.
        if k in {"geometries", "boundary", "toponyms", "types", "relations", "title"}:
            merged[k] = v
        else:
            merged[k] = v
    return merged


# ``normalize_for_parquet`` lives in ``processing.staged_parquet`` so
# h3_merge can share it.
_normalize_for_parquet = normalize_for_parquet


def run_boundary_merge(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path | None = None,
    allow_upsert: bool = False,
) -> dict[str, Any]:
    if namespace not in {"osm", "ohm"}:
        raise ValueError("Boundary merge currently supports only 'osm' and 'ohm'")

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "boundary_merge", "running")

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="boundary-merge",
        status="running",
        stage="boundary_merge",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="boundary_merge",
        status="running",
        namespace=namespace,
        stage="boundary_merge",
    )

    patches = _load_boundary_patches(namespace)

    out_dir = Path(STAGED_BASE_DIR) / namespace / "boundary_merged"
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "places.parquet"
    jsonl_path = out_dir / "places.jsonl"

    docs_seen = 0
    docs_updated = 0
    docs_written = 0

    # Written to temps and renamed into place only once complete —
    # boundary_merged/ outranks extract/ for every consumer, so a half-written
    # snapshot here is preferred over the complete extract it derives from.
    # See staged_parquet.atomic_staged_snapshot (§2.8).
    with atomic_staged_snapshot(jsonl_path, parquet_path,
                                label="boundary_merge") as out_jsonl:
        for doc in _iter_extract_docs(namespace):
            docs_seen += 1
            place_id = doc.get("place_id")
            patch = patches.pop(place_id, None) if place_id else None
            if patch:
                doc = _merge_update(doc, patch.get("update_doc") or {})
                docs_updated += 1

            # Re-augment geom_ref on every row: boundary_stage's patches
            # ship ``has_geom: True`` but no ``geom_ref`` (the patch payload
            # bypasses ``_augment_doc_for_stage``), so the merged doc would
            # otherwise lose the lookup key tile generation needs.
            doc = _augment_doc_for_stage(doc)
            out_jsonl.write(json.dumps(normalize_for_parquet(doc), ensure_ascii=True) + "\n")
            docs_written += 1
            _progress("boundary_merge", docs_written)

        # Optionally append upserts for patches with no existing base row.
        if allow_upsert:
            for patch in patches.values():
                upsert_doc = patch.get("upsert_doc")
                if not isinstance(upsert_doc, dict):
                    continue
                upsert_doc = _augment_doc_for_stage(upsert_doc)
                out_jsonl.write(
                    json.dumps(normalize_for_parquet(upsert_doc), ensure_ascii=True) + "\n"
                )
                docs_written += 1

    # The parquet sidecar is derived by streaming the canonical JSONL through
    # hull-strip. The canonical JSONL keeps hull intact for downstream
    # consumers (ccode_enrichment, generate_tiles); only the parquet sidecar
    # is hull-less (lossless — those consumers don't read it).

    metrics = {
        "docs_seen": docs_seen,
        "docs_updated": docs_updated,
        "docs_written": docs_written,
        "patches_unmatched": len(patches),
        "parquet_path": str(parquet_path),
        "jsonl_path": str(jsonl_path),
    }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "boundary_merge", "completed", metrics=metrics)

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="boundary-merge",
        status="completed",
        stage="boundary_merge",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="boundary_merge",
        status="completed",
        namespace=namespace,
        stage="boundary_merge",
        details=metrics,
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge boundary patches into staged snapshot")
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--namespace", required=True, choices=["osm", "ohm"], help="Namespace")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    parser.add_argument("--allow-upsert", action="store_true", help="Append unmatched upsert docs")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(runs_dir=STAGED_RUNS_DIR, run_id=args.run_id)
    )

    metrics = run_boundary_merge(
        run_id=args.run_id,
        namespace=args.namespace,
        manifest_path=manifest_path if manifest_path.exists() else None,
        allow_upsert=args.allow_upsert,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()




