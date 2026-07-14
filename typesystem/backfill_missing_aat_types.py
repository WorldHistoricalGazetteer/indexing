#!/usr/bin/env python
"""Backfill AAT concept ids that appear on place docs but are missing from the
`types` index (so the gateway's facet-label resolver falls back to the raw numeric
id, e.g. "300008584" instead of "inhabited places").

These ids reached place docs via the enrichment crosswalks (static maps, P1014,
P279, manual maps, tgn backfill) but sit OUTSIDE the AAT entry-point subtrees that
`sync_aat_types` loads, so they were never indexed with a preferred term. This
module resolves each missing id's Getty preferred term + broader ancestor path and
indexes a schema-matching `types` doc (term/term_full/path/ancestors/depth/
fclasses/is_place_type), reusing sync_aat_types' fetch + extract logic.

Idempotent; run after any enrichment that can add new AAT ids.

    python -m typesystem.backfill_missing_aat_types --es-host http://localhost:9201 [--dry-run] [--execute]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

from typesystem.aat_config import AAT_FCLASS_MAP
from typesystem.sync_aat_types import (
    _fetch_concept_json,
    _extract_label_and_note,
    _aat_id_from_uri,
    is_place_type_for,
    _PLACE_ROOTS,
    ES_TYPES_INDEX,
)

DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"
PLACES_INDEX = "places"
_MAX_DEPTH = 30


def _es_session(es_host: str, pw_file: str):
    s = requests.Session()
    p = Path(pw_file)
    if p.exists():
        try:
            s.auth = ("elastic", p.read_text().strip())
        except PermissionError:
            pass
    s.headers.update({"Content-Type": "application/json"})
    return s, es_host.rstrip("/")


def find_missing_ids(es, host):
    """aat_ids on place docs that are absent from `types` (or lack a term)."""
    agg = es.post(f"{host}/{PLACES_INDEX}/_search", json={
        "size": 0,
        "aggs": {"n": {"nested": {"path": "types"}, "aggs": {
            "ids": {"terms": {"field": "types.aat_ids", "size": 5000}}}}},
    }, timeout=120).json()
    buckets = agg["aggregations"]["n"]["ids"]["buckets"]
    place_ids = [str(b["key"]) for b in buckets]
    docfreq = {str(b["key"]): b["doc_count"] for b in buckets}
    present = set()
    for i in range(0, len(place_ids), 500):
        chunk = place_ids[i:i + 500]
        r = es.get(f"{host}/{ES_TYPES_INDEX}/_mget",
                   json={"ids": ["aat:" + x for x in chunk]}, timeout=60).json()
        for d in r["docs"]:
            if d.get("found") and d["_source"].get("term"):
                present.add(d["_id"].split(":")[1])
    missing = [x for x in place_ids if x not in present]
    return missing, docfreq


def resolve_concept(aat_id, gsession):
    """Fetch a concept's term + ancestor path (root→leaf) from Getty.

    Walks `broader` upward (preferred parent) to a place-root or AAT top,
    depth-capped. Returns a schema-matching `types` doc, or None if no term.
    """
    data = _fetch_concept_json(aat_id, gsession)
    if data is None:
        return None
    label, note, labels_ml, notes_ml = _extract_label_and_note(data)
    if not label:
        return None

    # Build ancestor chain (leaf→root) by walking broader.
    chain = [aat_id]
    seen = {aat_id}
    cur, cur_data = aat_id, data
    parent_id = None
    for _ in range(_MAX_DEPTH):
        nxt = None
        for b in (cur_data.get("broader") or []):
            pid = _aat_id_from_uri(b.get("id", ""))
            if pid is not None and pid not in seen:
                nxt = pid
                break
        if nxt is None:
            break
        if cur == aat_id:
            parent_id = nxt
        chain.append(nxt)
        seen.add(nxt)
        if nxt in _PLACE_ROOTS:
            break
        cur = nxt
        cur_data = _fetch_concept_json(nxt, gsession)
        if cur_data is None:
            break

    ancestors = list(reversed(chain))            # root→leaf, ids
    path = ".".join(str(x) for x in ancestors)
    fclasses = sorted(set(
        fc for anc in ancestors if (fc := AAT_FCLASS_MAP.get(anc))
    )) or list(AAT_FCLASS_MAP.get(aat_id, "") or "")

    doc = {
        "aat_id": aat_id,
        "parent_id": parent_id,
        "term": label[:100],
        "term_full": label[:100],
        "note": note,
        "fclasses": fclasses,
        "path": path,
        "ancestors": ancestors,
        "depth": len(ancestors) - 1,
        "is_place_type": is_place_type_for(ancestors, aat_id),
    }
    if labels_ml:
        doc["labels"] = labels_ml
    if notes_ml:
        doc["notes"] = notes_ml
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--es-host", default="http://localhost:9201")
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve + print, don't index")
    ap.add_argument("--execute", action="store_true",
                    help="Actually index (default is a safe no-write summary)")
    args = ap.parse_args()

    es, host = _es_session(args.es_host, args.es_password_file)
    missing, docfreq = find_missing_ids(es, host)
    hits = sum(docfreq.get(x, 0) for x in missing)
    print(f"[aat-backfill] {len(missing)} AAT ids on place docs missing from "
          f"'{ES_TYPES_INDEX}' (cover {hits:,} nested type-hits)")
    if not missing:
        return

    gsession = requests.Session()
    gsession.headers.update({"Accept": "application/json"})
    docs, unresolved = [], []
    for i, aid in enumerate(sorted(missing, key=lambda x: -docfreq.get(x, 0)), 1):
        d = resolve_concept(int(aid), gsession)
        if d:
            docs.append(d)
        else:
            unresolved.append(aid)
        if i % 10 == 0:
            print(f"    ... {i}/{len(missing)} resolved={len(docs)} unresolved={len(unresolved)}")
    print(f"[aat-backfill] resolved {len(docs)}/{len(missing)}  unresolved={unresolved}")
    for d in docs[:8]:
        print(f"    {d['aat_id']} -> {d['term']!r}  place_type={d['is_place_type']} depth={d['depth']}")

    if args.dry_run or not args.execute:
        print("[aat-backfill] dry-run / no --execute: not indexing.")
        return

    # Bulk index (_id = aat:<id>), refresh so facets resolve immediately.
    lines = []
    for d in docs:
        lines.append(f'{{"index":{{"_index":"{ES_TYPES_INDEX}","_id":"aat:{d["aat_id"]}"}}}}')
        import json as _json
        lines.append(_json.dumps(d))
    body = "\n".join(lines) + "\n"
    r = es.post(f"{host}/_bulk?refresh=true", data=body.encode(),
                headers={"Content-Type": "application/x-ndjson"}, timeout=120).json()
    errs = [it for it in r.get("items", []) if it.get("index", {}).get("status", 200) >= 300]
    print(f"[aat-backfill] indexed {len(docs) - len(errs)}/{len(docs)}  errors={len(errs)}")
    if errs:
        print("  first error:", errs[0])


if __name__ == "__main__":
    main()
