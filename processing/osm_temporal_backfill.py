#!/usr/bin/env python
"""Backfill real OSM start_date/end_date onto the LIVE ``places`` osm docs.

`osm-places.py` discarded both tags until #246 item 1, so 226,468 ingested
features (1.098%; 132,841 of them `historic=*`) assert "attested 2026" while the
source states a real start year. 232,343 carry either tag. The code fix stops
this recurring; this repairs what is already indexed, without a re-ingest —
1.13% of a namespace does not justify rebuilding 20.6M places.

    # 1. Build the patch from the planet PBF (Slurm; reads /ix1, touches nothing)
    python -m processing.osm_temporal_backfill extract \\
        --pbf /ix1/ishi/data/authorities/osm/planet-latest.osm.pbf \\
        --out /vast/ishi/staged/osm/temporal_patch.jsonl

    # 2. Apply (dry-run by default; --execute to write). Run ON pitt.
    python -m processing.osm_temporal_backfill --es-host http://localhost:9201 apply \\
        --patch /vast/ishi/staged/osm/temporal_patch.jsonl --throttle 0.1 [--execute]

⚠ VERIFY AGAINST THE SCAN'S COUNT, NEVER AGAINST `_bulk`'s SUCCESS COUNT. The
TGN backfill reported `ok=9,623 not-indexed=0 errors=0` and changed nothing at
all: it keyed on toponym names absent from the index, every operation "succeeded"
against a document it did not alter, and the acceptance evidence was a re-measure
that caught it. `ok=N errors=0` is evidence that N requests were accepted, and
nothing more. This module patches by `place_id` — which is derived from the OSM
id and cannot be absent the way a name can — but the rule stands regardless.

⚠ `_bulk`, not `_update_by_query`: the latter re-runs the `extract_namespace`
ingest pipeline over every touched document.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"
KEYS = ("place", "natural", "water", "waterway", "historic", "landuse", "boundary")


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def cmd_extract(args) -> int:
    """Emit one patch row per ingested feature carrying a usable date."""
    import osmium
    from processing.temporal import dated_or_attested, parse_osm_year

    year = args.attestation_year
    if year is None:
        try:
            stamp = osmium.io.Reader(args.pbf).header().get("osmosis_replication_timestamp")
            year = int(str(stamp)[:4]) if stamp else None
        except Exception as exc:
            print(f"WARN: could not read replication timestamp: {exc}")
    if year is None:
        raise SystemExit(
            "ABORT: no attestation year. It must match what the ingest used, or "
            "the unchanged branch stops being byte-identical and the patch's "
            "diff is no longer attributable to the dated features. Pass "
            "--attestation-year explicitly.")
    print(f"[osm-temporal extract] attestation year {year}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")

    fp = (osmium.FileProcessor(args.pbf)
          .with_filter(osmium.filter.KeyFilter("name")))
    seen = written = unparseable = 0
    t0 = time.time()
    with tmp.open("w", encoding="utf-8") as fh:
        for obj in fp:
            tags = obj.tags
            if "name" not in tags or not any(k in tags for k in KEYS):
                continue
            seen += 1
            raw_s, raw_e = tags.get("start_date"), tags.get("end_date")
            if raw_s is None and raw_e is None:
                continue
            s, e = parse_osm_year(raw_s), parse_osm_year(raw_e)
            if s is None and e is None:
                # Tag present but unparseable ("unknown", "?"). Skipping is
                # correct — the patched value would equal the current one — but
                # it is COUNTED, because the difference between this and the
                # scan's 232,343 is otherwise an unexplained shortfall.
                unparseable += 1
                continue
            kind = ("n" if isinstance(obj, osmium.osm.Node)
                    else "w" if isinstance(obj, osmium.osm.Way) else "r")
            fh.write(json.dumps({
                "place_id": f"osm:{kind}{obj.id}",
                "timespans": dated_or_attested(s, e, year),
            }, ensure_ascii=False) + "\n")
            written += 1
    tmp.replace(out)
    print(f"[osm-temporal extract] {written:,} patch rows from {seen:,} ingested "
          f"features ({unparseable:,} had a date tag that would not parse) "
          f"in {(time.time()-t0)/60:.1f} min → {out}")
    return 0


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _es(host: str, pwf: str):
    from elasticsearch import Elasticsearch
    kw = {"request_timeout": 300}
    p = Path(pwf)
    if p.exists():
        try:
            kw["basic_auth"] = ("elastic", p.read_text().strip())
        except PermissionError:
            pass
    return Elasticsearch(host, **kw)


def cmd_apply(args) -> int:
    from elasticsearch import helpers as es_helpers

    rows = [json.loads(l) for l in Path(args.patch).open(encoding="utf-8")]
    print(f"[osm-temporal apply] patch rows: {len(rows):,}  index: {args.index}")
    es = _es(args.es_host, args.es_password_file)

    def actions():
        for r in rows:
            yield {
                "_op_type": "update", "_index": args.index, "_id": r["place_id"],
                "script": {
                    "source": (
                        "for (t in ctx._source.toponyms) { t.timespans = params.ts } "
                        "if (ctx._source.geometries != null) { "
                        "for (g in ctx._source.geometries) { g.timespans = params.ts } }"),
                    "params": {"ts": r["timespans"]},
                },
            }

    if not args.execute:
        sample = rows[0] if rows else None
        print(f"[osm-temporal apply] DRY-RUN: would set timespans on {len(rows):,} "
              f"osm docs. sample {sample['place_id'] if sample else '-'}: "
              f"{json.dumps(sample['timespans']) if sample else '-'}")
        print("[osm-temporal apply] ⚠ re-measure AFTER executing. A success count "
              "from _bulk is not evidence that any document changed.")
        return 0

    ok = errs = 0
    t0 = time.time()
    for success, info in es_helpers.streaming_bulk(
            es, actions(), chunk_size=args.batch_size, raise_on_error=False,
            max_retries=3, yield_ok=True):
        if success:
            ok += 1
        else:
            errs += 1
            if errs <= 5:
                print(f"  error: {json.dumps(info)[:300]}")
        if args.throttle and (ok + errs) % args.batch_size == 0:
            time.sleep(args.throttle)
    print(f"[osm-temporal apply] done: ok={ok:,} errors={errs:,} "
          f"({time.time()-t0:.0f}s)")
    print("[osm-temporal apply] ⚠ THIS IS NOT THE ACCEPTANCE EVIDENCE. Re-measure "
          "the index and assert: document count unchanged; timespans altered only "
          "on the patched ids; nothing else altered. A fourth kind of difference "
          "is a stop signal.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--es-host", default="http://localhost:9201")
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract", help="planet PBF → patch rows (Slurm)")
    ex.add_argument("--pbf", required=True)
    ex.add_argument("--out", required=True)
    ex.add_argument("--attestation-year", type=int,
                    help="must match what the ingest used; read from the PBF header "
                         "when omitted")
    ex.set_defaults(func=cmd_extract)

    ap_ = sub.add_parser("apply", help="patch rows → bulk-update prod ES (pitt)")
    ap_.add_argument("--patch", required=True)
    ap_.add_argument("--index", default="places")
    ap_.add_argument("--batch-size", type=int, default=1000)
    ap_.add_argument("--throttle", type=float, default=0.0)
    ap_.add_argument("--execute", action="store_true")
    ap_.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
