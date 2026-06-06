#!/usr/bin/env python
"""Apply a Phase-3 ``update_patch`` (``geometries_to_replace``) to the LIVE index in place.

Targeted, idempotent geometry update keyed ``_id = place_id``: sets each doc's
``geometries`` from the patch (+ ``h3_centroid``/``h3_cover``), stamping
``geom_ref``/``source``/``approximation`` on each geometry entry. It does NOT
rebuild or swap the alias, so it preserves incremental in-place adds already in
prod (e.g. ofs/og). Run ON pitt (prod ES on localhost:9201). Dry-run by default.

Used to land the Wikidata geoshape boundaries (``wd:Q*``) onto the live wd docs
after ``geom_store --merge`` consolidated their WKB into the shared store.

    python -m processing.apply_update_patch --es-host http://localhost:9201 \
        --patch /vast/ishi/staged/wd/update_patch/places.update.jsonl \
        --source wd --approximation exact --throttle 0.2 [--execute]
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch import helpers as es_helpers

DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"


def _es(host, pwf):
    kw = {"request_timeout": 300}
    p = Path(pwf)
    if p.exists():
        try:
            kw["basic_auth"] = ("elastic", p.read_text().strip())
        except PermissionError:
            pass
    return Elasticsearch(host, **kw)


def _rows(path):
    for line in Path(path).open(encoding="utf-8"):
        if line.strip():
            yield json.loads(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--es-host", required=True)
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    ap.add_argument("--patch", required=True)
    ap.add_argument("--index", default="places")
    ap.add_argument("--source", default="wd", help="geometries[].source provenance value")
    ap.add_argument("--approximation", default="exact")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--throttle", type=float, default=0.0, metavar="SECONDS",
                    help="sleep between bulk chunks to pace prod ES")
    ap.add_argument("--execute", action="store_true", help="apply (default: dry-run)")
    args = ap.parse_args()

    es = _es(args.es_host, args.es_password_file)
    now = datetime.now(timezone.utc).isoformat()

    n = sum(1 for _ in _rows(args.patch))
    print(f"[apply] patch rows: {n:,}  target index: {args.index}  source={args.source}")
    if not args.execute:
        for r in _rows(args.patch):
            g = (r.get("geometries_to_replace") or [{}])[0]
            print(f"[apply] DRY-RUN: would update {n:,} docs (geometries+h3, "
                  f"geom_ref={r['place_id']}_0, throttle={args.throttle}). "
                  f"sample {r['place_id']}: has_geom={g.get('has_geom')} "
                  f"hull={(g.get('hull') or {}).get('type')}")
            break
        return

    def to_action(r):
        pid = r["place_id"]
        geoms = []
        for i, g in enumerate(r.get("geometries_to_replace", [])):
            g = dict(g)
            g["geom_ref"] = f"{pid}_{i}"
            g["geometry_index"] = i
            g["source"] = args.source
            g["approximation"] = args.approximation
            geoms.append(g)
        doc = {"geometries": geoms, "indexed_at": now}
        if r.get("h3_centroid"):
            doc["h3_centroid"] = r["h3_centroid"]
        if r.get("h3_cover"):
            doc["h3_cover"] = r["h3_cover"]
        return {"_op_type": "update", "_index": args.index, "_id": pid, "doc": doc}

    es_opt = es.options(request_timeout=300)
    ok = errs = 0
    t0 = time.time()
    next_report = 20_000
    batch = []

    def flush(b):
        nonlocal ok, errs
        o, e = es_helpers.bulk(es_opt, b, raise_on_error=False, max_retries=3, initial_backoff=2)
        ok += o
        errs += len(e) if isinstance(e, list) else e

    for r in _rows(args.patch):
        batch.append(to_action(r))
        if len(batch) >= args.batch_size:
            flush(batch)
            batch = []
            if ok + errs >= next_report:
                print(f"[apply]   {ok:,} ok / {errs:,} err "
                      f"({(ok + errs) / (time.time() - t0):.0f}/s)", flush=True)
                next_report += 20_000
            if args.throttle:
                time.sleep(args.throttle)
    if batch:
        flush(batch)
    es.indices.refresh(index=args.index)
    print(f"[apply] done: ok={ok:,} errors={errs:,}  ({time.time() - t0:.0f}s)  refreshed {args.index}")


if __name__ == "__main__":
    main()
