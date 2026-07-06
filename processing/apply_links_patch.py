#!/usr/bin/env python
"""Apply a ``links`` update patch to the LIVE ``places`` index in place.

Targeted, idempotent, keyed ``_id = place_id``. Each patch row is
``{"place_id": ..., "links": [{"type", "identifier"}, ...]}`` (produced by
``wikidata_links_patch.py``). Uses a Painless scripted update that **merges**
the patch links into any existing ``links``, deduplicated by ``identifier`` —
so re-running is a no-op and pre-existing authority links are preserved. Does
NOT rebuild or swap the alias. Run ON pitt (prod ES on localhost:9201).
Dry-run by default.

    python -m processing.apply_links_patch --es-host http://localhost:9201 \
        --patch /vast/ishi/staged/wd/update_patch/places.links.jsonl \
        --throttle 0.2 [--execute]
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

# Merge params.links into ctx._source.links, deduped by identifier. Idempotent:
# re-applying the same patch adds nothing, and any existing (non-Wikipedia)
# links survive untouched.
MERGE_SCRIPT = """
if (ctx._source.links == null) {
    ctx._source.links = params.links;
} else {
    def seen = new HashSet();
    for (l in ctx._source.links) { seen.add(l.identifier); }
    for (l in params.links) {
        if (!seen.contains(l.identifier)) { ctx._source.links.add(l); }
    }
}
ctx._source.indexed_at = params.now;
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
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--es-host", required=True)
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    ap.add_argument("--patch", required=True)
    ap.add_argument("--index", default="places")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--throttle", type=float, default=0.0, metavar="SECONDS",
                    help="sleep between bulk chunks to pace prod ES")
    ap.add_argument("--execute", action="store_true", help="apply (default: dry-run)")
    args = ap.parse_args()

    es = _es(args.es_host, args.es_password_file)
    now = datetime.now(timezone.utc).isoformat()

    n = sum(1 for _ in _rows(args.patch))
    print(f"[apply-links] patch rows: {n:,}  target index: {args.index}")
    if not args.execute:
        for r in _rows(args.patch):
            links = r.get("links") or []
            print(f"[apply-links] DRY-RUN: would merge links into {n:,} docs "
                  f"(dedup by identifier, throttle={args.throttle}). "
                  f"sample {r['place_id']}: {len(links)} link(s), "
                  f"first={links[0] if links else None}")
            break
        return

    def to_action(r):
        return {
            "_op_type": "update",
            "_index": args.index,
            "_id": r["place_id"],
            "script": {
                "source": MERGE_SCRIPT,
                "lang": "painless",
                "params": {"links": r["links"], "now": now},
            },
        }

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
                print(f"[apply-links]   {ok:,} ok / {errs:,} err "
                      f"({(ok + errs) / (time.time() - t0):.0f}/s)", flush=True)
                next_report += 20_000
            if args.throttle:
                time.sleep(args.throttle)
    if batch:
        flush(batch)
    es.indices.refresh(index=args.index)
    print(f"[apply-links] done: ok={ok:,} errors={errs:,}  "
          f"({time.time() - t0:.0f}s)  refreshed {args.index}")


if __name__ == "__main__":
    main()
