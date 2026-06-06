#!/usr/bin/env python
"""Apply AAT enrichment to the LIVE places index in place (prod counterpart to ``aat_enrich``).

The staged ``processing.aat_enrich`` stage folds ``aat_ids`` + ``aat_paths`` into
each native ``types[]`` entry of ``staged/{ns}/final`` during a rebuild. When that
stage is found to have silently never run for a namespace (e.g. ``osm`` in the
``postbarrier-20260502`` rebuild), this script reproduces the *identical* folded
shape directly on the production index — no cutover, no separate ``label:"aat"``
element (that is the legacy ``aat_lookup`` shape and is NOT corpus-consistent).

It reuses ``aat_enrich.augment_doc`` so the prod result is byte-for-byte the same
as a fresh rebuild would have produced. Idempotent: re-running on docs that
already carry ``aat_ids`` simply overwrites with the same values and adds any
missing ``aat_paths`` (so it also backfills paths on namespaces enriched by an
earlier direct top-up). Dry-run by default. Run ON pitt (prod ES).

    python -m processing.apply_aat_enrich --es-host http://localhost:9201 \
        --namespace osm --throttle 0.1 [--execute]
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch import helpers as es_helpers

from processing.aat_data_lookup import load_aat_hierarchy, load_all_aat_mappings
from processing.aat_enrich import DEFAULT_DATA_DIR, augment_doc

DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"


def _es(host: str, pwf: str) -> Elasticsearch:
    kw = {"request_timeout": 300}
    p = Path(pwf)
    if p.exists():
        try:
            kw["basic_auth"] = ("elastic", p.read_text().strip())
        except PermissionError:
            pass
    return Elasticsearch(host, **kw)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--es-host", required=True)
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    ap.add_argument("--namespace", required=True, help="place_id namespace prefix (e.g. osm)")
    ap.add_argument("--index", default="places")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--throttle", type=float, default=0.0, metavar="SECONDS")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    es = _es(args.es_host, args.es_password_file)
    data_dir = Path(args.data_dir)
    mappings = load_all_aat_mappings(data_dir)
    hierarchy = load_aat_hierarchy(data_dir)

    query = {"prefix": {"place_id": f"{args.namespace}:"}}
    total = es.count(index=args.index, query=query)["count"]
    print(f"[aat-enrich] {args.namespace}:* docs in '{args.index}': {total:,}  "
          f"data-dir={data_dir}  mode={'EXECUTE' if args.execute else 'DRY-RUN'}")

    def actions():
        scan = es_helpers.scan(
            es, index=args.index, query={"query": query, "_source": ["types"]},
            size=args.batch_size, scroll="10m", preserve_order=False,
        )
        for hit in scan:
            src = hit["_source"]
            new_doc, _seen, n_aug = augment_doc(src, mappings, hierarchy)
            if n_aug:
                yield {"_op_type": "update", "_index": args.index,
                       "_id": hit["_id"], "doc": {"types": new_doc["types"]}}

    if not args.execute:
        # Dry-run: count how many docs WOULD change (scan only, no writes).
        would = sum(1 for _ in actions())
        print(f"[aat-enrich] DRY-RUN: {would:,}/{total:,} docs would gain AAT enrichment. No writes.")
        return

    es_opt = es.options(request_timeout=300)
    ok = errs = 0
    t0 = time.time()
    next_report = 100_000
    batch = []

    def flush(b):
        nonlocal ok, errs
        o, e = es_helpers.bulk(es_opt, b, raise_on_error=False, max_retries=3, initial_backoff=2)
        ok += o
        errs += len(e) if isinstance(e, list) else e

    for a in actions():
        batch.append(a)
        if len(batch) >= args.batch_size:
            flush(batch)
            batch = []
            if ok + errs >= next_report:
                print(f"[aat-enrich]   {ok:,} ok / {errs:,} err "
                      f"({(ok + errs) / (time.time() - t0):.0f}/s)", flush=True)
                next_report += 100_000
            if args.throttle:
                time.sleep(args.throttle)
    if batch:
        flush(batch)
    es.indices.refresh(index=args.index)
    print(f"[aat-enrich] done: ok={ok:,} errors={errs:,}  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
