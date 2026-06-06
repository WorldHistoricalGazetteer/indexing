#!/usr/bin/env python
"""Apply a ccode patch (``{place_id, ccodes}`` JSONL) to the LIVE index in place.

Targeted, idempotent per-``_id`` fill: a painless script sets ``ccodes`` ONLY when
the doc currently has none (``ctx.op='noop'`` otherwise), so docs that already
carry the (deterministic) ccode don't churn segments. No cutover, so it preserves
prod's incremental in-place adds. Run ON pitt (prod ES). Dry-run by default.

Used to land ``ccode_enrichment`` output onto the live docs after the rebuild's
``ccode_enrichment``/``ccode_merge`` stage was found incomplete (osm/ohm).

    python -m processing.apply_ccode_patch --es-host http://localhost:9201 \
        --patch /vast/ishi/staged/osm/ccode/places.ccode.jsonl \
        --throttle 0.2 [--execute]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch import helpers as es_helpers

DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"

# Fill ccodes only if absent/empty — otherwise no-op (avoid rewriting correct docs).
_FILL_IF_EMPTY = """
if (ctx._source.ccodes == null || ctx._source.ccodes.size() == 0) {
  ctx._source.ccodes = params.ccodes;
} else {
  ctx.op = 'noop';
}
"""


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
    ap.add_argument("--overwrite", action="store_true",
                    help="overwrite existing ccodes too (default: fill-if-empty)")
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--throttle", type=float, default=0.0, metavar="SECONDS")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    es = _es(args.es_host, args.es_password_file)
    n = sum(1 for _ in _rows(args.patch))
    mode = "OVERWRITE" if args.overwrite else "fill-if-empty"
    print(f"[ccode] patch rows: {n:,}  index: {args.index}  mode: {mode}")
    if not args.execute:
        print(f"[ccode] DRY-RUN: would update up to {n:,} docs (throttle={args.throttle}). No writes.")
        return

    def to_action(r):
        a = {"_op_type": "update", "_index": args.index, "_id": r["place_id"]}
        if args.overwrite:
            a["doc"] = {"ccodes": r["ccodes"]}
        else:
            a["script"] = {"source": _FILL_IF_EMPTY, "lang": "painless",
                           "params": {"ccodes": r["ccodes"]}}
        return a

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

    for r in _rows(args.patch):
        batch.append(to_action(r))
        if len(batch) >= args.batch_size:
            flush(batch)
            batch = []
            if ok + errs >= next_report:
                print(f"[ccode]   {ok:,} ok / {errs:,} err "
                      f"({(ok + errs) / (time.time() - t0):.0f}/s)", flush=True)
                next_report += 100_000
            if args.throttle:
                time.sleep(args.throttle)
    if batch:
        flush(batch)
    es.indices.refresh(index=args.index)
    print(f"[ccode] done: ok={ok:,} errors={errs:,}  ({time.time() - t0:.0f}s)  refreshed {args.index}")


if __name__ == "__main__":
    main()
