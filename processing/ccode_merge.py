#!/usr/bin/env python3
"""Merge staged ccode patches into namespace snapshots.

Reads:
  - {STAGED_BASE_DIR}/{namespace}/h3_merged/places.parquet|jsonl
  - {STAGED_BASE_DIR}/{namespace}/ccode/places.ccode.jsonl

Writes:
  - {STAGED_BASE_DIR}/{namespace}/final/places.parquet
  - {STAGED_BASE_DIR}/{namespace}/final/places.jsonl

Patch semantics: ``ccodes`` from the patch is **authoritative** — it overwrites
any existing ``ccodes`` on the document. Unmatched documents pass through with
their original ``ccodes`` (or none) untouched.

This stage runs after Batch 7 (ccode enrichment). It does not access
Elasticsearch.
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
from processing.stage_writers import write_runtime_history_event, write_stage_event
from processing.staged_parquet import (
    normalize_for_parquet,
    write_parquet_from_jsonl,
)
from processing.staging_contract import (
    CCODE_PATCH_REQUIRED_FIELDS,
    validate_required_fields,
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



def _iter_source_docs(namespace: str) -> Iterable[dict[str, Any]]:
    src_dir = Path(STAGED_BASE_DIR) / namespace / "h3_merged"
    parquet_path = src_dir / "places.parquet"
    jsonl_path = src_dir / "places.jsonl"

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

    raise FileNotFoundError(
        f"No H3-merged source found for namespace '{namespace}' in {src_dir}"
    )


def _load_ccode_patches(namespace: str) -> dict[str, list[str]]:
    """Return ``{place_id: [ccodes...]}`` — last-write-wins per place_id."""
    patch_path = Path(STAGED_BASE_DIR) / namespace / "ccode" / "places.ccode.jsonl"
    if not patch_path.exists():
        raise FileNotFoundError(f"CCode patch file not found: {patch_path}")

    patches: dict[str, list[str]] = {}
    with patch_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            try:
                validate_required_fields(rec, CCODE_PATCH_REQUIRED_FIELDS)
            except ValueError:
                continue
            ccodes = rec.get("ccodes")
            if not isinstance(ccodes, list):
                continue
            cleaned = [str(c) for c in ccodes if isinstance(c, str) and c]
            patches[rec["place_id"]] = cleaned
    return patches


# ``normalize_for_parquet`` lives in ``processing.staged_parquet`` so
# h3_merge, boundary_merge and ccode_merge all share it.
_normalize_for_parquet = normalize_for_parquet


def run_ccode_merge(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "ccode_merge", "running")

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="ccode-merge",
        status="running",
        stage="ccode_merge",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="ccode_merge",
        status="running",
        namespace=namespace,
        stage="ccode_merge",
    )

    patches = _load_ccode_patches(namespace)

    out_dir = Path(STAGED_BASE_DIR) / namespace / "final"
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "places.parquet"
    jsonl_path = out_dir / "places.jsonl"

    docs_seen = 0
    docs_updated = 0
    docs_written = 0

    with jsonl_path.open("w", encoding="utf-8") as out_jsonl:
        for doc in _iter_source_docs(namespace):
            docs_seen += 1
            place_id = doc.get("place_id")
            new_ccodes = patches.pop(place_id, None) if place_id else None
            if new_ccodes is not None:
                # Patch is authoritative — overwrite any existing ccodes.
                doc = dict(doc)
                doc["ccodes"] = new_ccodes
                docs_updated += 1

            out_jsonl.write(json.dumps(normalize_for_parquet(doc), ensure_ascii=True) + "\n")
            docs_written += 1
            _progress("ccode_merge", docs_written)

    # Stream the canonical JSONL through hull-strip + null-strip into a
    # temp parquet-input JSONL, then convert to parquet. The canonical
    # JSONL keeps hull and explicit nulls intact for downstream consumers.
    print(f"  ccode_merge: merged {docs_written:,} docs; converting to Parquet ...",
          flush=True)
    write_parquet_from_jsonl(jsonl_path, parquet_path)
    print(f"  ccode_merge: Parquet written", flush=True)

    metrics = {
        "docs_seen": docs_seen,
        "docs_updated": docs_updated,
        "docs_written": docs_written,
        "patches_unmatched": len(patches),
        "parquet_path": str(parquet_path),
        "jsonl_path": str(jsonl_path),
    }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "ccode_merge", "completed", metrics=metrics
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="ccode-merge",
        status="completed",
        stage="ccode_merge",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="ccode_merge",
        status="completed",
        namespace=namespace,
        stage="ccode_merge",
        details=metrics,
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge ccode patches into staged snapshot")
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--namespace", required=True, help="Namespace")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
            runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
        )
    )

    metrics = run_ccode_merge(
        run_id=args.run_id,
        namespace=args.namespace,
        manifest_path=manifest_path if manifest_path.exists() else None,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
