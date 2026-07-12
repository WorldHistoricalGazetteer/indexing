#!/usr/bin/env python3
"""Augment staged docs' ``types[]`` with AAT id+path from the data files.

Post-chain stage. Runs between ``ccode_merge`` and ``gazetteer_temporal_extent``.

Reads ``staged/{ns}/final/places.{parquet|jsonl}``, walks each ``types[]``
entry, and overlays:

* ``aat_ids``   — ``[<aat_id>, …]`` from ``typesystem/data/<vocab>.json``.
* ``aat_paths`` — ``["/300000000/.../<aat_id>", …]`` looked up in
  ``typesystem/data/aat_hierarchy.json``. Stored as keyword for prefix
  hierarchy filters at search time.

Both arrays are parallel: ``len(aat_paths) <= len(aat_ids)`` (an aat_id
without a hierarchy entry contributes only the id; the path slot is omitted
to keep `aat_paths` sane for prefix queries).

Writes back to ``staged/{ns}/final/places.{jsonl,parquet}`` in place — the
``final/`` snapshot remains the authoritative pre-index dataset, now with
AAT enrichment included. Downstream stages (``temporal_extent``, tile gen,
``index_from_stage``) pick it up automatically.

Why file-based (vs the ES ``processing.aat_lookup`` path): Slurm compute
nodes are firewalled from the production gateway, so ingestion-time
augmentation must read from the shared ``/ix1`` mount. The data files are
refreshed by ``scripts/types.sh --build-vocabs`` on the Pitt VM during the
mass-rebuild prep phase. See ``processing/aat_data_lookup`` for loaders.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq

from processing.aat_data_lookup import (
    load_aat_hierarchy,
    load_all_aat_mappings,
    lookup_key_for_type_entry,
    vocab_for_type_entry,
)
from processing.manual_aat_maps import manual_aat_ids
from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staged_parquet import normalize_for_parquet, write_parquet_from_jsonl
from processing.stage_writers import write_runtime_history_event, write_stage_event
from processing.staging_orchestrator import update_namespace_stage_status

# Default location of the typesystem data files. Overridable via
# ``--data-dir`` for tests / dated-snapshot runs.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "typesystem" / "data"

# Source-snapshot priority — same cascade as ``gazetteer_temporal_extent``.
# Namespaces that bypass ccode (po — temporal-only; un — supplies ccodes
# itself) never produce ``final/``, so we read from the most-enriched
# upstream snapshot instead. Output always lands in ``final/`` so
# downstream stages (temporal_extent, tile gen, index_from_stage) have a
# single canonical input dir.
_STAGED_SOURCE_PRIORITY = (
    "final",
    "h3_merged",
    "boundary_merged",
    "update_merged",
    "extract",
)


def _resolve_source_path(namespace: str) -> Path:
    base = Path(STAGED_BASE_DIR) / namespace
    for stage in _STAGED_SOURCE_PRIORITY:
        for filename in ("places.parquet", "places.jsonl"):
            candidate = base / stage / filename
            if candidate.exists():
                return candidate
    raise FileNotFoundError(
        f"No staged snapshot found for namespace '{namespace}' — "
        f"checked {_STAGED_SOURCE_PRIORITY} under {base}"
    )


def _iter_source_docs(namespace: str) -> Iterable[dict[str, Any]]:
    """Stream the namespace's most-enriched snapshot."""
    src = _resolve_source_path(namespace)
    if src.suffix == ".parquet":
        parquet = pq.ParquetFile(src)
        for batch in parquet.iter_batches(batch_size=2000):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row
        return
    with src.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def augment_doc(
    doc: dict[str, Any],
    mappings: dict[str, dict[str, list[int]]],
    hierarchy: dict[int, dict[str, Any]],
    namespace: str | None = None,
) -> tuple[dict[str, Any], int, int]:
    """Augment a single doc's ``types[]`` entries.

    Returns ``(new_doc, types_seen, types_augmented)``. The doc is shallow-copied
    only when augmentation actually applies; otherwise the original is returned
    so the JSONL stream stays cheap on docs that contribute no AAT mapping.

    ``namespace`` (for the curated manual map) is taken from the argument if
    given, else derived from the doc's ``namespace`` / ``place_id`` — callers
    that fetch a trimmed ``_source`` (e.g. ``apply_aat_enrich`` requests only
    ``types``) must pass it explicitly.
    """
    types = doc.get("types") or []
    if not isinstance(types, list) or not types:
        return doc, 0, 0

    namespace = (namespace or doc.get("namespace")
                 or (doc.get("place_id", "").split(":", 1)[0] or None))

    new_types: list[dict[str, Any]] = []
    types_seen = 0
    augmented = 0
    changed = False

    for entry in types:
        if not isinstance(entry, dict):
            new_types.append(entry)
            continue
        types_seen += 1
        # Curated manual map (namespace + identifier → AAT ids) for authorities
        # with a small/bespoke type vocabulary (ukhc, clio, tm, dgsd, nl, dp, …;
        # see processing.manual_aat_maps). Injects aat_ids + paths without a
        # generated vocab file and without per-authority-script changes.
        manual = manual_aat_ids(namespace, entry.get("identifier"))
        if manual and not entry.get("aat_ids"):
            new_entry = dict(entry)
            new_entry["aat_ids"] = list(manual)
            paths = []
            for a in manual:
                h = hierarchy.get(int(a))
                if h and h.get("path"):
                    paths.append(h["path"])
            if paths:
                new_entry["aat_paths"] = paths
            new_types.append(new_entry)
            augmented += 1
            changed = True
            continue
        # Path-fill: a type that already carries aat_ids but no aat_paths gets its
        # hierarchy paths here. Covers direct-AAT authorities (ofs/og) whose label
        # ('ottnfs'/'ottgaz') isn't a mapped vocab — their aat_ids are intrinsic, set
        # by the authority script — so they'd otherwise never become hierarchy-
        # searchable. Idempotent for vocab-mapped entries (they already carry paths).
        existing_ids = entry.get("aat_ids")
        if existing_ids and not entry.get("aat_paths"):
            paths = []
            for a in existing_ids:
                try:
                    h = hierarchy.get(int(a))
                except (TypeError, ValueError):
                    h = None
                if h and h.get("path"):
                    paths.append(h["path"])
            if paths:
                new_entry = dict(entry)
                new_entry["aat_paths"] = paths
                new_types.append(new_entry)
                augmented += 1
                changed = True
                continue
        vocab = vocab_for_type_entry(entry.get("label"))
        if vocab is None:
            new_types.append(entry)
            continue
        key = lookup_key_for_type_entry(vocab, entry)
        if key is None:
            new_types.append(entry)
            continue

        aat_ids = mappings.get(vocab, {}).get(key)
        if not aat_ids:
            new_types.append(entry)
            continue

        # Build parallel arrays: paths[] only includes ids that have a
        # hierarchy entry. `aat_ids` always reflects the full mapping —
        # so a missing path doesn't hide the id from term-filter searches.
        aat_paths: list[str] = []
        for aid in aat_ids:
            entry_h = hierarchy.get(aid)
            if entry_h and entry_h.get("path"):
                aat_paths.append(entry_h["path"])

        new_entry = dict(entry)
        new_entry["aat_ids"] = list(aat_ids)
        if aat_paths:
            new_entry["aat_paths"] = aat_paths
        new_types.append(new_entry)
        augmented += 1
        changed = True

    if not changed:
        return doc, types_seen, 0
    new_doc = dict(doc)
    new_doc["types"] = new_types
    return new_doc, types_seen, augmented


def run_aat_enrich(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "aat_enrich", "running")

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="aat-enrich",
        status="running",
        stage="aat_enrich",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="aat_enrich",
        status="running",
        namespace=namespace,
        stage="aat_enrich",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
    )

    started = time.time()

    # Load the lookup tables once. Total memory ~30 MB across the five
    # vocab maps + the hierarchy file; trivial vs the per-doc cost.
    mappings = load_all_aat_mappings(data_dir)
    hierarchy = load_aat_hierarchy(data_dir)

    out_dir = Path(STAGED_BASE_DIR) / namespace / "final"
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "places.parquet"
    jsonl_path = out_dir / "places.jsonl"

    # Write to a sibling temp first so a crash doesn't leave the
    # ``final/places.jsonl`` half-rewritten and lose the prior pipeline output.
    tmp_jsonl = out_dir / "places.jsonl.aat_enrich_tmp"

    docs_seen = 0
    docs_augmented = 0
    types_seen = 0
    types_augmented = 0

    with tmp_jsonl.open("w", encoding="utf-8") as fh:
        for doc in _iter_source_docs(namespace):
            docs_seen += 1
            new_doc, n_seen, n_aug = augment_doc(doc, mappings, hierarchy)
            if n_aug:
                docs_augmented += 1
            types_seen += n_seen
            types_augmented += n_aug
            fh.write(json.dumps(normalize_for_parquet(new_doc), ensure_ascii=True) + "\n")

    tmp_jsonl.replace(jsonl_path)
    parquet_written = write_parquet_from_jsonl(jsonl_path, parquet_path)

    metrics = {
        "docs_seen": docs_seen,
        "docs_augmented": docs_augmented,
        "types_seen": types_seen,
        "types_augmented": types_augmented,
        "parquet_written": parquet_written,
        "data_dir": str(data_dir),
        "jsonl_path": str(jsonl_path),
        "parquet_path": str(parquet_path),
        "wall_seconds": round(time.time() - started, 1),
    }

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "aat_enrich", "completed", metrics=metrics
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="aat-enrich",
        status="completed",
        stage="aat_enrich",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="aat_enrich",
        status="completed",
        namespace=namespace,
        stage="aat_enrich",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
        details=metrics,
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Augment staged final/places.jsonl with AAT id + path"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--namespace", required=True, help="Namespace")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    parser.add_argument(
        "--data-dir",
        help=f"Override typesystem/data directory (default: {DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
            runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
        )
    )

    metrics = run_aat_enrich(
        run_id=args.run_id,
        namespace=args.namespace,
        manifest_path=manifest_path if manifest_path.exists() else None,
        data_dir=Path(args.data_dir) if args.data_dir else DEFAULT_DATA_DIR,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
