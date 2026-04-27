"""Batch 12 — Authority hard-link harvest from staged files.

Replaces the ES-based ``clustering/harvest/hard_links.py`` for the production
path. Iterates each selected gazetteer's ``staged/{namespace}/final/places.parquet``
(falling back through the stage chain) and emits one
``hard_link_assertions`` row per ``relations[]`` entry whose ``relation_type``
is in ``IDENTITY_RELATION_TYPES``.

Filters preserved from the legacy harvester:

* Same-namespace self-references (e.g. ``gn`` → ``gn``) are dropped.
* Targets in unknown / non-WHG namespaces are dropped.
* ``related_place_id`` must be namespaced (``ns:id``).

Output rows are bulk-inserted into the SQLite via
``clustering.sqlite_overlay`` with ``INSERT OR IGNORE`` so re-runs are
idempotent and assertions made by multiple sources are merged on
``(place_a, place_b, relation_type, source_id)`` uniqueness.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq

from clustering.config import IDENTITY_RELATION_TYPES, KNOWN_ES_NAMESPACES
from clustering.sqlite_overlay import builder, insert_rows
from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_contract import is_relations_only
from processing.staging_orchestrator import load_run_manifest


_STAGED_SOURCE_PRIORITY = (
    "final",
    "h3_merged",
    "boundary_merged",
    "update_merged",
    "extract",
)


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


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    """Return ``(place_a, place_b)`` lex-ordered to satisfy the SQL CHECK."""
    return (a, b) if a < b else (b, a)


def iter_hard_link_rows_for_namespace(
    namespace: str,
) -> Iterator[dict[str, Any]]:
    """Yield validated hard-link rows from one namespace's staged snapshot.

    Each yielded dict is shaped per ``HARD_LINK_REQUIRED_FIELDS`` with
    ``source_category='authority'`` and ``source_id=<namespace>``. Rows are
    pre-filtered (same-namespace, unknown-target-namespace, malformed
    relations are dropped silently); the SQLite layer runs schema-level
    validation again before insert.
    """
    src = _staged_namespace_source(namespace)
    if src is None:
        return

    for doc in _iter_staged_docs(src):
        place_id = doc.get("place_id")
        if not place_id or ":" not in place_id:
            continue
        source_ns = place_id.split(":", 1)[0]

        for rel in doc.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            rel_type = rel.get("relation_type")
            if rel_type not in IDENTITY_RELATION_TYPES:
                continue
            target = rel.get("related_place_id")
            if not isinstance(target, str) or ":" not in target:
                continue
            target_ns = target.split(":", 1)[0]

            # Same-namespace self-reference (e.g. gn → gn closeMatch).
            if target_ns == source_ns:
                continue
            # Targets we don't index (e.g. dp → glottolog).
            if target_ns not in KNOWN_ES_NAMESPACES:
                continue

            place_a, place_b = _canonical_pair(place_id, target)
            justification = rel.get("justification") or rel.get("source")
            asserted_at = rel.get("asserted_at") or rel.get("timestamp")

            yield {
                "place_a": place_a,
                "place_b": place_b,
                "relation_type": rel_type,
                "source_category": "authority",
                "source_id": namespace,
                "asserted_at": asserted_at,
                "justification": justification if isinstance(justification, str) else None,
            }


def harvest_namespace(
    namespace: str,
    *,
    db_path: Path,
    batch_size: int = 5_000,
) -> dict[str, Any]:
    """Harvest one namespace into ``db_path`` and return per-namespace metrics."""
    with builder(db_path) as conn:
        stats = insert_rows(
            conn, iter_hard_link_rows_for_namespace(namespace),
            batch_size=batch_size,
        )
    stats["namespace"] = namespace
    return stats


def harvest_all_selected(
    *,
    manifest_path: Path,
    db_path: Path,
    namespaces: list[str] | None = None,
    batch_size: int = 5_000,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Harvest every selected per-gazetteer namespace into ``db_path``.

    When ``run_id`` is provided the wall-clock time of the harvest is
    appended to the persistent runtime-history file so subsequent
    ``submit_hardlinks_slurm`` invocations can size ``--time`` from the
    median of recent runs.
    """
    import os, time
    from datetime import datetime, timezone
    from processing.stage_writers import record_script_wall_time

    manifest = load_run_manifest(manifest_path)
    selected = [
        ns for ns in manifest.get("selected_namespaces", [])
        if not is_relations_only(ns)
    ]
    if namespaces:
        selected = [ns for ns in selected if ns in namespaces]

    per_namespace: dict[str, dict[str, Any]] = {}
    totals = {"attempted": 0, "inserted": 0, "rejected": 0}

    started_at = datetime.now(timezone.utc)
    started_mono = time.monotonic()

    # One outer connection so we don't reopen the file per namespace.
    with builder(db_path) as conn:
        for ns in selected:
            stats = insert_rows(
                conn, iter_hard_link_rows_for_namespace(ns),
                batch_size=batch_size,
            )
            stats["namespace"] = ns
            per_namespace[ns] = stats
            for key in ("attempted", "inserted", "rejected"):
                totals[key] += stats[key]
            print(
                f"  {ns}: attempted={stats['attempted']:,} "
                f"inserted={stats['inserted']:,} rejected={stats['rejected']:,}"
            )

    finished_at = datetime.now(timezone.utc)
    wall_seconds = time.monotonic() - started_mono

    if run_id:
        try:
            record_script_wall_time(
                namespace="hardlinks",
                script_id="hard-links-staged",
                run_id=run_id,
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                wall_seconds=wall_seconds,
                status="completed",
                slurm_job_id=os.environ.get("SLURM_JOB_ID"),
                extra={"inserted": totals["inserted"]},
            )
        except Exception:
            pass

    return {
        "db_path": str(db_path),
        "namespaces": list(per_namespace),
        "per_namespace": per_namespace,
        "totals": totals,
        "wall_seconds": round(wall_seconds, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Harvest authority hard-link assertions from staged snapshots"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest-path")
    parser.add_argument("--db-path", required=True,
                        help="SQLite output path (built incrementally)")
    parser.add_argument("--namespace", action="append",
                        help="Restrict to one or more namespaces")
    parser.add_argument("--batch-size", type=int, default=5_000)
    args = parser.parse_args()

    if args.manifest_path:
        manifest_path = Path(args.manifest_path)
    else:
        manifest_path = Path(
            STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
                runs_dir=STAGED_RUNS_DIR, run_id=args.run_id,
            )
        )
    if not manifest_path.exists():
        print(f"Run manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    summary = harvest_all_selected(
        manifest_path=manifest_path,
        db_path=Path(args.db_path),
        namespaces=args.namespace,
        batch_size=args.batch_size,
        run_id=args.run_id,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
