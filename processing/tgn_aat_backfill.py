#!/usr/bin/env python
"""Backfill real Getty AAT place types onto the LIVE ``places`` tgn docs.

TGN ingestion (``authorities/tgn-places.py``) historically hardcoded a single
generic ``{"identifier": "place", "label": "tgn"}`` type for every one of the
~3M TGN records, discarding the actual Getty place types — which are themselves
**AAT concepts**. This one-time, in-place backfill recovers them from the Getty
release and patches them onto the live index (no full re-ingest).

Where the types come from
-------------------------
The Getty TGN "explicit" N-Triples release ships ``TGNOut_PlaceTypes.nt``, where
each place-type assertion is a reified relationship whose **URI encodes both
ids**::

    <…/tgn/rel/7005155-placeType-300000774> …

i.e. TGN concept ``7005155`` has AAT place type ``300000774``. A row's
``rdf:predicate = gvp:placeTypePreferred`` marks the primary type; ``historicFlag``
marks historic (vs current) types. We extract every ``(tgn_id, aat_id)`` pair,
resolve each AAT id's materialised ``path`` + ``term`` from the live ``types``
index (so ``aat_paths`` is authoritative), and emit a patch row per TGN place.

Two steps (run ON pitt — prod ES on localhost:9201; NT file on ``/ix1``):

    # 1. Build the patch (parses the 3 GB NT file, resolves via the types index)
    python -m processing.tgn_aat_backfill extract \\
        --source /ix1/ishi/data/authorities/tgn/explicit.zip \\
        --es-host http://localhost:9201 --out /vast/ishi/staged/tgn/aat_patch.jsonl

    # 2. Apply it (dry-run by default; --execute to write)
    python -m processing.tgn_aat_backfill apply \\
        --es-host http://localhost:9201 \\
        --patch /vast/ishi/staged/tgn/aat_patch.jsonl --throttle 0.2 [--execute]

The apply step **replaces** each patched doc's ``types`` with the real AAT types
(the generic ``place`` goes away); TGN docs with no resolvable place type are left
untouched. Idempotent — re-running produces the same result.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch import helpers as es_helpers

DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"
_PLACETYPES_MEMBER = "TGNOut_PlaceTypes.nt"

# Extract (tgn_id, aat_id) straight from the reified relationship URI. Tolerates
# the ``placeTypePreferred`` infix variant as well as plain ``placeType``.
_REL_RE = re.compile(r"/tgn/rel/(\d+)-placeType(?:Preferred)?-(\d+)\b")
# A row is the *preferred* type when its rdf:predicate object is placeTypePreferred.
_PREFERRED_PRED = "placeTypePreferred"


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


def _stream_placetypes(zip_path: Path):
    with zipfile.ZipFile(zip_path, "r") as zf:
        member = _PLACETYPES_MEMBER
        if member not in zf.namelist():
            cands = [n for n in zf.namelist() if "PlaceType" in n]
            if not cands:
                raise FileNotFoundError(f"{member} not in {zip_path}")
            member = cands[0]
        with zf.open(member, "r") as f:
            for line in f:
                yield line.decode("utf-8", "replace")


def _parse_placetypes(zip_path: Path) -> tuple[dict[str, list[str]], set[tuple[str, str]]]:
    """Return ``({tgn_id: [aat_id, …]}, preferred_pairs)``.

    ``aat_id`` order per TGN id is: preferred first, then by ascending id. The
    ``(tgn_id, aat_id)`` pair is read from the URI on *every* triple of the rel,
    so a pair is captured even if its predicate/order triples are non-contiguous.
    """
    pairs: set[tuple[str, str]] = set()
    preferred: set[tuple[str, str]] = set()
    n = 0
    for line in _stream_placetypes(zip_path):
        n += 1
        m = _REL_RE.search(line)
        if not m:
            continue
        tgn_id, aat_id = m.group(1), m.group(2)
        pairs.add((tgn_id, aat_id))
        if _PREFERRED_PRED in line and "predicate" in line:
            preferred.add((tgn_id, aat_id))
        if n % 20_000_000 == 0:
            sys.stderr.write(f"\r  scanned {n:,} lines, {len(pairs):,} type pairs")
            sys.stderr.flush()
    sys.stderr.write(f"\n  {n:,} lines → {len(pairs):,} (tgn,aat) pairs, "
                     f"{len(preferred):,} preferred\n")

    by_tgn: dict[str, list[str]] = defaultdict(list)
    for tgn_id, aat_id in pairs:
        by_tgn[tgn_id].append(aat_id)
    # Stable order: preferred first, then numeric id.
    for tgn_id, aats in by_tgn.items():
        aats.sort(key=lambda a: (0 if (tgn_id, a) in preferred else 1, int(a)))
    return by_tgn, preferred


def _resolve_aat(es: Elasticsearch, aat_ids: set[str]) -> dict[str, dict]:
    """``{aat_id: {"path": str, "term": str}}`` from the live ``types`` index.

    Only place-type-bearing AAT ids that carry a materialised ``path`` resolve;
    the rest are dropped from the patch (logged)."""
    out: dict[str, dict] = {}
    ids = sorted(aat_ids, key=int)
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        resp = es.search(index="types", size=len(chunk), _source=["aat_id", "path", "term"],
                         query={"terms": {"aat_id": [int(a) for a in chunk]}})
        for h in resp["hits"]["hits"]:
            s = h["_source"]
            path = s.get("path")
            if path:
                out[str(s["aat_id"])] = {"path": path, "term": s.get("term") or ""}
    return out


def _run_extract(args) -> int:
    es = _es(args.es_host, args.es_password_file)
    zip_path = Path(args.source)
    sys.stderr.write(f"[tgn-aat] parsing {zip_path} …\n")
    by_tgn, _pref = _parse_placetypes(zip_path)

    distinct = {a for aats in by_tgn.values() for a in aats}
    sys.stderr.write(f"[tgn-aat] resolving {len(distinct):,} distinct AAT ids "
                     f"against the types index …\n")
    aat_meta = _resolve_aat(es, distinct)
    unresolved = distinct - set(aat_meta)
    sys.stderr.write(f"[tgn-aat] resolved {len(aat_meta):,} / {len(distinct):,} "
                     f"({len(unresolved):,} AAT ids not in the types index)\n")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = skipped = 0
    with out.open("w", encoding="utf-8") as fh:
        for tgn_id, aats in by_tgn.items():
            types = []
            seen_aat: set[str] = set()
            for a in aats:
                meta = aat_meta.get(a)
                if not meta or a in seen_aat:
                    continue
                seen_aat.add(a)
                types.append({
                    "identifier": a,               # TGN's native type IS the AAT id
                    "label": "tgn",
                    "sourceLabel": meta["term"],   # human-readable AAT term
                    "aat_ids": [int(a)],
                    "aat_paths": [meta["path"]],
                })
            if not types:
                skipped += 1
                continue
            fh.write(json.dumps({"place_id": f"tgn:{tgn_id}", "types": types}) + "\n")
            rows += 1
    sys.stderr.write(f"[tgn-aat] wrote {rows:,} patch rows → {out} "
                     f"({skipped:,} TGN concepts had no resolvable AAT type)\n")
    return 0


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

# Replace the generic `place` with the real AAT types. Overwrite (not merge):
# every current tgn doc has exactly the placeholder, so the real types supersede
# it. Idempotent — re-applying sets the same array.
_APPLY_SCRIPT = """
ctx._source.types = params.types;
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
    print(f"[tgn-aat apply] patch rows: {total:,}  index: {args.index}")
    if not args.execute:
        for r in _rows(args.patch):
            print(f"[tgn-aat apply] DRY-RUN: would REPLACE types on {total:,} tgn docs. "
                  f"sample {r['place_id']}: {len(r['types'])} type(s), first={r['types'][0]}")
            break
        return 0

    def to_action(r):
        return {
            "_op_type": "update", "_index": args.index, "_id": r["place_id"],
            "script": {"source": _APPLY_SCRIPT, "lang": "painless",
                       "params": {"types": r["types"], "now": now}},
        }

    es_opt = es.options(request_timeout=300)
    ok = errs = missing = 0
    t0 = time.time()
    next_report = 50_000
    batch: list[dict] = []

    def flush(b):
        nonlocal ok, errs, missing
        o, e = es_helpers.bulk(es_opt, b, raise_on_error=False, max_retries=3, initial_backoff=2)
        ok += o
        if isinstance(e, list):
            for item in e:
                # doc_missing = TGN concept not present in `places` (never indexed) — expected.
                if "document_missing" in json.dumps(item):
                    missing += 1
                else:
                    errs += 1
        else:
            errs += e

    for r in _rows(args.patch):
        batch.append(to_action(r))
        if len(batch) >= args.batch_size:
            flush(batch); batch = []
            if ok + errs + missing >= next_report:
                print(f"[tgn-aat apply]   {ok:,} ok / {missing:,} not-indexed / {errs:,} err "
                      f"({(ok + errs + missing) / (time.time() - t0):.0f}/s)", flush=True)
                next_report += 50_000
            if args.throttle:
                time.sleep(args.throttle)
    if batch:
        flush(batch)
    es.indices.refresh(index=args.index)
    print(f"[tgn-aat apply] done: ok={ok:,} not-indexed={missing:,} errors={errs:,} "
          f"({time.time() - t0:.0f}s)  refreshed {args.index}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--es-host", required=True)
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract", help="parse PlaceTypes.nt + resolve → patch JSONL")
    ex.add_argument("--source", default="/ix1/ishi/data/authorities/tgn/explicit.zip")
    ex.add_argument("--out", required=True)

    ap_ = sub.add_parser("apply", help="apply the patch to the live places index")
    ap_.add_argument("--patch", required=True)
    ap_.add_argument("--index", default="places")
    ap_.add_argument("--batch-size", type=int, default=1000)
    ap_.add_argument("--throttle", type=float, default=0.0, metavar="SECONDS")
    ap_.add_argument("--execute", action="store_true", help="apply (default: dry-run)")

    args = ap.parse_args()
    if args.cmd == "extract":
        return _run_extract(args)
    return _run_apply(args)


if __name__ == "__main__":
    sys.exit(main())
