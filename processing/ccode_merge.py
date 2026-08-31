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
    atomic_staged_snapshot,
    normalize_for_parquet,
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


def _load_ccode_patches(namespace: str, *,
                        allow_missing: bool = False) -> dict[str, list[str]]:
    """Return ``{place_id: [ccodes...]}`` — last-write-wins per place_id.

    ``allow_missing`` turns the merge into a **pass-through**: no patch, every
    document copied from ``h3_merged`` to ``final`` with its ccodes untouched.
    That is what a namespace whose ccode stage was *skipped* needs — see
    ``run_ccode_merge``. A missing patch is otherwise a hard error, because for
    every other namespace it means the enrichment silently produced nothing.
    """
    patch_path = Path(STAGED_BASE_DIR) / namespace / "ccode" / "places.ccode.jsonl"
    if not patch_path.exists():
        if allow_missing:
            print(f"  ccode_merge: no patch at {patch_path} — pass-through "
                  f"(ccodes copied through unchanged)", flush=True)
            return {}
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
    allow_missing_patch: bool = False,
) -> dict[str, Any]:
    """Merge the staged ccode patch into ``h3_merged`` and write ``final/``.

    ``allow_missing_patch`` runs the stage as a pure pass-through when no patch
    exists. **``final/`` is written by this stage and by nothing else**, so a
    namespace whose ccode enrichment is deliberately skipped (``un``, which is
    the ccode *source*) still has to come through here or it keeps whatever
    ``final/`` a previous run left. That is Fault 12: on 5 Aug 2026 ``un``'s
    improved ``h3_cover`` sat in ``h3_merged`` for three days while the index
    served a stale copy, invisible to the freshness gate because the stale
    ``final/`` was internally self-consistent — and ``un`` is the namespace that
    supplies ``contained_in`` regions, so it nullified the place#174 fix on its
    own.
    """
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

    patches = _load_ccode_patches(namespace, allow_missing=allow_missing_patch)

    out_dir = Path(STAGED_BASE_DIR) / namespace / "final"
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "places.parquet"
    jsonl_path = out_dir / "places.jsonl"

    docs_seen = 0
    docs_updated = 0
    docs_written = 0

    # Written to temps and renamed into place only once complete. This stage
    # is the ONLY writer of final/, which the indexer and the tile submitter
    # both consume, so a half-written snapshot here is read in preference to
    # the complete h3_merged/ it derives from. See
    # staged_parquet.atomic_staged_snapshot (§2.8).
    #
    # The parquet sidecar is derived by streaming the canonical JSONL through
    # hull-strip + null-strip; the canonical JSONL keeps hull and explicit
    # nulls intact for downstream consumers.
    with atomic_staged_snapshot(jsonl_path, parquet_path,
                                label="ccode_merge") as out_jsonl:
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

    metrics = {
        "docs_seen": docs_seen,
        "docs_updated": docs_updated,
        "docs_written": docs_written,
        "patches_unmatched": len(patches),
        "passthrough": bool(allow_missing_patch and not docs_updated),
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
    parser.add_argument(
        "--allow-missing-patch", action="store_true",
        help="Treat an absent ccode patch as empty and pass every document "
             "through (for namespaces whose ccode stage is deliberately "
             "skipped, e.g. un). final/ is still regenerated.")
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
        allow_missing_patch=args.allow_missing_patch,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
