#!/usr/bin/env python
"""Backfill real Getty TGN temporal data onto the LIVE ``places`` tgn docs.

TGN ingestion used a ``[2025, 2025]`` placeholder for every timespan. The source
actually holds sparse real dates (see ``processing.tgn_temporal``):

* **term-level** name-in-use dates → the matching **toponym's** timespan;
* **relation-level** dates (+ historicFlag) → the **place's** temporal extent →
  every geometry's timespan.

This one-time backfill patches both on the live index (no re-ingest). Only docs
with at least one *real* bound are touched; the rest keep the placeholder. The
search temporal filter reads ``toponyms.timespans``; the clustering ``s.t`` fuel
(``temporal_range``) reads ``geometries.timespans`` — so both get corrected.

    # 1. Build the patch (parses the TGN release; a few minutes)
    python -m processing.tgn_temporal_backfill extract \\
        --source /ix1/ishi/data/authorities/tgn/explicit.zip \\
        --out /vast/ishi/staged/tgn/temporal_patch.jsonl

    # 2. Apply (dry-run by default; --execute to write). Run ON pitt.
    python -m processing.tgn_temporal_backfill apply \\
        --es-host http://localhost:9201 \\
        --patch /vast/ishi/staged/tgn/temporal_patch.jsonl --throttle 0.1 [--execute]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch import helpers as es_helpers

from processing.tgn_temporal import (
    parse_concept_toponym_dates,
    parse_relation_dates,
    timespan,
)

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


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def _run_extract(args) -> int:
    zip_path = Path(args.source)
    sys.stderr.write("[tgn-temporal] parsing term-level dates …\n")
    topo = parse_concept_toponym_dates(zip_path)   # {concept: {toponym_id: (s,e)}}
    sys.stderr.write(f"[tgn-temporal]   {len(topo):,} concepts with term dates\n")
    sys.stderr.write("[tgn-temporal] parsing relation-level dates …\n")
    rel = parse_relation_dates(zip_path)           # {concept: (s,e,historic)}
    sys.stderr.write(f"[tgn-temporal]   {len(rel):,} concepts with relation dates\n")

    concepts = set(topo) | set(rel)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with out.open("w", encoding="utf-8") as fh:
        for cid in concepts:
            row: dict = {"place_id": f"tgn:{cid}"}
            # Toponym name-in-use spans.
            tspans = {}
            for tid, (s, e) in topo.get(cid, {}).items():
                if s is None and e is None:
                    continue
                tspans[tid] = timespan(s, e)
            if tspans:
                row["toponym_spans"] = tspans
            # Place extent from relations (skip historic-only-with-no-dates).
            rd = rel.get(cid)
            if rd and (rd[0] is not None or rd[1] is not None):
                row["geom_span"] = timespan(rd[0], rd[1])
            if "toponym_spans" in row or "geom_span" in row:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows += 1
    sys.stderr.write(f"[tgn-temporal] wrote {rows:,} patch rows → {out}\n")
    return 0


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

_APPLY_SCRIPT = """
if (params.containsKey('geom_span') && ctx._source.geometries != null) {
    for (g in ctx._source.geometries) { g.timespans = params.geom_span; }
}
if (params.containsKey('toponym_spans') && ctx._source.toponyms != null) {
    for (t in ctx._source.toponyms) {
        if (params.toponym_spans.containsKey(t.toponym_id)) {
            t.timespans = params.toponym_spans[t.toponym_id];
        }
    }
}
ctx._source.indexed_at = params.now;
"""


def _rows(path: str):
    for line in Path(path).open(encoding="utf-8"):
        if line.strip():
            yield json.loads(line)


def _run_apply(args) -> int:
    es = _es(args.es_host, args.es_password_file)
    now = datetime.now(timezone.utc).isoformat()
    total = sum(1 for _ in _rows(args.patch))
    print(f"[tgn-temporal apply] patch rows: {total:,}  index: {args.index}")
    if not args.execute:
        for r in _rows(args.patch):
            print(f"[tgn-temporal apply] DRY-RUN: would set timespans on {total:,} tgn docs. "
                  f"sample {r['place_id']}: geom_span={r.get('geom_span')}, "
                  f"{len(r.get('toponym_spans', {}))} toponym span(s)")
            break
        return 0

    def to_action(r):
        params = {"now": now}
        if "geom_span" in r:
            params["geom_span"] = r["geom_span"]
        if "toponym_spans" in r:
            params["toponym_spans"] = r["toponym_spans"]
        return {"_op_type": "update", "_index": args.index, "_id": r["place_id"],
                "script": {"source": _APPLY_SCRIPT, "lang": "painless", "params": params}}

    es_opt = es.options(request_timeout=300)
    ok = errs = missing = 0
    t0 = time.time()
    batch: list[dict] = []

    def flush(b):
        nonlocal ok, errs, missing
        o, e = es_helpers.bulk(es_opt, b, raise_on_error=False, max_retries=3, initial_backoff=2)
        ok += o
        if isinstance(e, list):
            for item in e:
                missing += 1 if "document_missing" in json.dumps(item) else 0
                errs += 0 if "document_missing" in json.dumps(item) else 1
        else:
            errs += e

    for r in _rows(args.patch):
        batch.append(to_action(r))
        if len(batch) >= args.batch_size:
            flush(batch); batch = []
            if args.throttle:
                time.sleep(args.throttle)
    if batch:
        flush(batch)
    es.indices.refresh(index=args.index)
    print(f"[tgn-temporal apply] done: ok={ok:,} not-indexed={missing:,} errors={errs:,} "
          f"({time.time() - t0:.0f}s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--es-host", default="http://localhost:9201")
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--source", default="/ix1/ishi/data/authorities/tgn/explicit.zip")
    ex.add_argument("--out", required=True)
    ap_ = sub.add_parser("apply")
    ap_.add_argument("--patch", required=True)
    ap_.add_argument("--index", default="places")
    ap_.add_argument("--batch-size", type=int, default=1000)
    ap_.add_argument("--throttle", type=float, default=0.0)
    ap_.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    return _run_extract(args) if args.cmd == "extract" else _run_apply(args)


if __name__ == "__main__":
    sys.exit(main())
