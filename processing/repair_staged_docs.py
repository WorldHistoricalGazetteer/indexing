#!/usr/bin/env python
"""Re-apply the current write-path rules to an already-staged snapshot.

Usage
-----
    python -m processing.repair_staged_docs --namespace wd            # report
    python -m processing.repair_staged_docs --namespace wd --execute
    python -m processing.repair_staged_docs --namespace wd --execute \\
        --reindex --es-host "$ES_URL" --index places_<run_id>

Why this exists
---------------
A staged ``final/`` snapshot is the durable artefact a rebuild indexes from, so
a rule fixed in ``processing.temporal`` after staging does not reach it. The
alternative is re-extracting, which for ``wd`` means re-parsing a 144 GiB dump
to correct a handful of fields.

Two rules are applied, both learned from documents Elasticsearch rejected
outright — ES fails the **whole document**, not the offending field:

* **Timespans** — years outside what the mapping can store. Wikidata models the
  age of the universe as an ordinary time claim.
* **repr_point longitude** — folded into [-180, 180]. Sources disagree on
  convention: Wikidata carries some coordinates in [0, 360] and some already
  shifted past the dateline; a Native Land treaty polygon straddling the
  antimeridian yields a representative point at -186.05. This was the real
  cause of 3,636 ``wd`` failures and one ``nl`` failure.

Both are now enforced at extract time (``processing.temporal`` and
``processing.helpers.wrap_longitude``), so a future rebuild never produces
them — but a snapshot already on disk still carries whatever it was written
with, and re-extracting ``wd`` means re-parsing a 144 GiB dump.

What it does
------------
Streams the namespace's ``final/`` snapshot and repairs only the documents that
need it. Timespans live in three places on a doc — ``geometries[].timespans``,
``toponyms[].timespans`` and a doc-level ``timespans`` — and all three are
covered, as is ``geometries[].repr_point``.

``--reindex`` then bulk-indexes **only the changed documents**, so repairing a
20 M-doc namespace costs one pass plus a few thousand writes rather than a full
reload. The repair is idempotent: a second run reports zero changes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq

from processing.helpers import wrap_longitude
from processing.settings import STAGED_BASE_DIR
from processing.staged_parquet import normalize_for_parquet, write_parquet_from_jsonl
from processing.temporal import (
    ENDPOINTS,
    SUBFIELDS,
    coerce_year,
    normalise_timespans,
    representable_year,
)

#: Where timespans can appear on a place doc.
_DOC_LEVEL = "timespans"
_NESTED = ("geometries", "toponyms")


def _has_unrepresentable_year(timespans: Any) -> bool:
    """True when any year present is outside what Elasticsearch can store.

    The precise question, deliberately. An earlier version asked "did
    normalise_timespans change anything?", which reads as *every* doc changing:
    a Parquet round trip materialises the whole schema with explicit nulls
    (``{"in": None, "earliest": -49, "latest": None}``) and normalisation strips
    them. That reported 9,017 of 9,017 ``po`` docs as damaged when 14 were —
    and would have re-indexed 11.5 M ``wd`` docs to repair 3,639.
    """
    if not isinstance(timespans, list):
        return False
    for ts in timespans:
        if not isinstance(ts, dict):
            continue
        for endpoint in ENDPOINTS:
            node = ts.get(endpoint)
            if not isinstance(node, dict):
                continue
            for sub in SUBFIELDS:
                if sub not in node:
                    continue
                year = coerce_year(node.get(sub))
                if year is not None and representable_year(year) is None:
                    return True
    return False


def _repair_repr_points(doc: dict[str, Any]) -> bool:
    """Fold any out-of-range ``repr_point`` longitude. True if one moved."""
    changed = False
    for geom in (doc.get("geometries") or []):
        if not isinstance(geom, dict):
            continue
        rp = geom.get("repr_point")
        if not isinstance(rp, dict):
            continue
        lon = rp.get("lon")
        if isinstance(lon, (int, float)) and not (-180.0 <= lon <= 180.0):
            rp["lon"] = round(wrap_longitude(lon), 6)
            changed = True
    return changed


def _repair_doc(doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return ``(doc, changed)``, repairing only docs that actually need it."""
    changed = _repair_repr_points(doc)

    original = doc.get(_DOC_LEVEL)
    if _has_unrepresentable_year(original):
        doc[_DOC_LEVEL] = normalise_timespans(original)
        changed = True

    for field in _NESTED:
        items = doc.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            original = item.get(_DOC_LEVEL)
            if _has_unrepresentable_year(original):
                item[_DOC_LEVEL] = normalise_timespans(original)
                changed = True

    return doc, changed


def _iter_staged(path: Path) -> Iterator[dict[str, Any]]:
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
            if line:
                yield json.loads(line)


def repair(
    namespace: str,
    *,
    execute: bool = False,
    staged_base: Path | None = None,
    changed_out: Path | None = None,
) -> dict[str, Any]:
    """Re-normalise a namespace's staged ``final/`` snapshot.

    Writes the corrected snapshot only under ``execute``. When ``changed_out``
    is given, the changed docs are written there as JSONL so ``--reindex`` can
    load just those.
    """
    base = (staged_base or Path(STAGED_BASE_DIR)) / namespace / "final"
    src = next((base / n for n in ("places.parquet", "places.jsonl")
                if (base / n).is_file()), None)
    if src is None:
        raise FileNotFoundError(f"No staged final/ snapshot for namespace {namespace!r}")

    tmp_jsonl = base / "places.jsonl.repair_tmp"
    changed_fh = changed_out.open("w", encoding="utf-8") if (execute and changed_out) else None

    seen = changed = 0
    samples: list[str] = []
    try:
        with tmp_jsonl.open("w", encoding="utf-8") as out:
            for doc in _iter_staged(src):
                seen += 1
                doc, was_changed = _repair_doc(doc)
                if was_changed:
                    changed += 1
                    if len(samples) < 5:
                        samples.append(doc.get("place_id", "?"))
                    if changed_fh:
                        changed_fh.write(json.dumps(doc, ensure_ascii=True) + "\n")
                out.write(json.dumps(normalize_for_parquet(doc), ensure_ascii=True) + "\n")
                if seen % 1_000_000 == 0:
                    print(f"  scanned {seen:,} docs, {changed:,} repaired", flush=True)
    finally:
        if changed_fh:
            changed_fh.close()

    result = {
        "namespace": namespace,
        "source": str(src),
        "docs_seen": seen,
        "docs_repaired": changed,
        "sample_place_ids": samples,
    }

    if not execute:
        tmp_jsonl.unlink(missing_ok=True)
        result["applied"] = False
        return result

    if changed == 0:
        # Nothing to do — leave the existing snapshot untouched rather than
        # rewriting it identically and disturbing its mtime.
        tmp_jsonl.unlink(missing_ok=True)
        result["applied"] = False
        return result

    jsonl_path = base / "places.jsonl"
    parquet_path = base / "places.parquet"
    backup = base / "places.jsonl.pre_repair"
    if jsonl_path.is_file() and not backup.exists():
        shutil.copyfile(jsonl_path, backup)
    tmp_jsonl.replace(jsonl_path)
    print(f"  rewriting {parquet_path.name} ...", flush=True)
    write_parquet_from_jsonl(jsonl_path, parquet_path)
    result["applied"] = True
    return result


def reindex(changed_jsonl: Path, *, es_host: str, index: str) -> dict[str, Any]:
    """Bulk-index only the repaired documents."""
    from elasticsearch import Elasticsearch, helpers

    es = Elasticsearch(es_host, request_timeout=300, max_retries=3)
    if not es.ping():
        raise SystemExit(f"cannot reach Elasticsearch at {es_host}")

    def actions():
        with changed_jsonl.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                doc = json.loads(line)
                pid = doc.get("place_id")
                if pid:
                    yield {"_op_type": "index", "_index": index, "_id": pid, "_source": doc}

    ok = errors = 0
    samples: list[str] = []
    for succeeded, item in helpers.streaming_bulk(
        es, actions(), chunk_size=500, raise_on_error=False, max_retries=2
    ):
        if succeeded:
            ok += 1
        else:
            errors += 1
            if len(samples) < 5:
                samples.append(json.dumps(item)[:400])
    return {"indexed": ok, "errors": errors, "error_samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-apply current temporal rules to a staged snapshot"
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--execute", action="store_true",
                        help="Write the corrected snapshot (default: report only)")
    parser.add_argument("--reindex", action="store_true",
                        help="Bulk-index the repaired docs (requires --execute)")
    parser.add_argument("--es-host", help="Elasticsearch URL for --reindex")
    parser.add_argument("--index", help="Target index name for --reindex")
    args = parser.parse_args()

    if args.reindex and not (args.execute and args.es_host and args.index):
        print("--reindex requires --execute, --es-host and --index", file=sys.stderr)
        sys.exit(2)

    changed_path = Path(STAGED_BASE_DIR) / args.namespace / "final" / "places.repaired.jsonl"
    result = repair(args.namespace, execute=args.execute,
                    changed_out=changed_path if args.reindex else None)
    print(json.dumps(result, indent=2))

    if args.reindex and result.get("docs_repaired"):
        print(f"\nRe-indexing {result['docs_repaired']:,} repaired docs → {args.index}")
        print(json.dumps(reindex(changed_path, es_host=args.es_host, index=args.index),
                         indent=2))


if __name__ == "__main__":
    main()
