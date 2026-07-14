#!/usr/bin/env python
"""Live-index backfill of ISO ``ccodes`` (and, for docs missing it,
``geometries[].h3_*``) via the shared UN-overlap resolver.

Motivation
----------
ccodes are resolved *spatially*, so coverage is only meaningful over docs that
**have geometry** — a doc with no geometry can never be assigned a country. An
audit conditioned on ``geometries.repr_point`` found the real gap is dominated
by:

* ``tgn`` — 100 % of ~2.97M docs, because tgn carries **no h3 at all**
  (the authority wrote h3 to the doc top-level instead of into
  ``geometries[]`` — fixed in ``authorities/tgn-places.py``); without
  ``h3_cover`` the ccode prefilter skipped every tgn doc.
* ``osm`` (~4.3M), ``wd`` (~2.3M), ``ohm`` (~290K) tails — h3 present, but the
  staged ccode stage never covered them (or they are genuinely at-sea /
  extraterrestrial and correctly stay empty).

Because tgn's ccode-target set and h3-missing set are the *same* ~2.97M docs,
this one backfill computes **both** h3 (from ``repr_point``) and ccodes in a
single pass; for osm/wd/ohm the h3 branch is a no-op (h3 already present).

Design — three phases, mirroring ``processing.recompute_h3_index``
-----------------------------------------------------------------
``export``  (run on **pitt**, read-only):
    PIT + ``search_after`` scroll of target docs (``prefix ns`` ∧ has
    ``repr_point`` ∧ *not* ``exists ccodes``) → JSONL of
    ``{place_id, geometries:[{geometry_index, repr_point, h3_cover, geom_ref,
    has_geom}]}``. Sliceable for parallelism.

``resolve`` (run on **Slurm/htc**, needs UN geoms ~24 GiB):
    Build the UN prefilter once, then per doc call the *shared*
    :func:`processing.ccode_enrichment.resolve_ccodes_for_doc` with
    ``synth_res=PREFILTER_RESOLUTION`` (so h3-less tgn docs still resolve). Also
    compute ``h3_centroid``/``h3_cover`` from ``repr_point`` for any geometry
    that lacks it. → patch JSONL ``{place_id, ccodes?, geometries?[]}``.

``apply``  (run on **pitt**, throttled):
    Scripted ``_bulk`` update that sets ``ccodes`` only when absent and per-geom
    h3 only when absent — **never overwrites** existing values.

Usage
-----
    # 1. export (pitt)
    python -m processing.backfill_ccodes export \
        --namespaces tgn,osm,ohm,wd,alc,clio,nl,dp,pl,po,og \
        --out /vast/ishi/ccodefix/targets.jsonl

    # 2. resolve (Slurm/htc; --slice/--of for an array)
    python -m processing.backfill_ccodes resolve \
        --in '/vast/ishi/ccodefix/targets.jsonl' \
        --out /vast/ishi/ccodefix/patch.jsonl

    # 3. apply (pitt)
    python -m processing.backfill_ccodes apply \
        --patch '/vast/ishi/ccodefix/patch.*.jsonl' --rps 1500 --batch 500
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Reuse the battle-tested PIT pagination + session/auth from the h3 repair tool.
from processing.recompute_h3_index import (  # noqa: E402
    ES_URL,
    PLACES_ALIAS,
    _paginate,
    _session,
)

_SOURCE_FIELDS = [
    "place_id",
    "geometries.geometry_index",
    "geometries.repr_point",
    "geometries.h3_cover",
    "geometries.h3_centroid",
    "geometries.geom_ref",
    "geometries.has_geom",
]


# ---------------------------------------------------------------------------
# Phase 1 — export (pitt, read-only)
# ---------------------------------------------------------------------------

def _target_query(namespaces: list[str]) -> dict:
    """Docs in the selected namespaces that HAVE geometry but LACK ccodes."""
    return {
        "bool": {
            "filter": [
                {
                    "bool": {
                        "should": [
                            {"prefix": {"place_id": f"{ns}:"}} for ns in namespaces
                        ],
                        "minimum_should_match": 1,
                    }
                },
                {
                    "nested": {
                        "path": "geometries",
                        "query": {"exists": {"field": "geometries.repr_point"}},
                    }
                },
            ],
            "must_not": [{"exists": {"field": "ccodes"}}],
        }
    }


def export(args) -> int:
    sess = _session()
    namespaces = [n.strip() for n in args.namespaces.split(",") if n.strip()]
    query = _target_query(namespaces)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n = 0
    t0 = time.time()
    with out_path.open("w", encoding="utf-8") as fh:
        for hit, _sort in _paginate(
            sess, query, _SOURCE_FIELDS, args.batch, args.slice, args.of
        ):
            src = hit.get("_source") or {}
            pid = src.get("place_id")
            geoms = src.get("geometries") or []
            if not pid or not geoms:
                continue
            fh.write(json.dumps({"place_id": pid, "geometries": geoms},
                                ensure_ascii=True) + "\n")
            n += 1
            if n % 200_000 == 0:
                print(f"  exported {n} (rate={n/max(time.time()-t0,1e-6):.0f}/s)",
                      flush=True)
    print(f"EXPORT DONE n={n} slice={args.slice}/{args.of} -> {out_path} "
          f"in {time.time()-t0:.0f}s", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Phase 2 — resolve (Slurm/htc, needs UN geoms)
# ---------------------------------------------------------------------------

def resolve(args) -> int:
    # Imported here so `export`/`apply` on pitt don't need h3/shapely/UN geoms.
    from processing.ccode_enrichment import (
        SOURCE_LABEL,
        UnCountryIndex,
        _load_un_records,
        resolve_ccodes_for_doc_exact,
    )
    from processing.geom_store import GeomStoreReader
    from processing.settings import GEOM_STORE_DIR
    from processing.helpers import compute_h3_fields

    try:
        place_reader = GeomStoreReader(GEOM_STORE_DIR)
    except FileNotFoundError:
        place_reader = None
    print("loading UN records + building STRtree country index "
          "(exact; no h3-prefilter gaps)...", flush=True)
    un_records = _load_un_records()
    country_index = UnCountryIndex(un_records, place_reader)
    print(f"  un_records={len(un_records)} country_geoms={len(country_index._geoms)} "
          f"geom_store={'yes' if place_reader else 'no'}", flush=True)

    patterns = [args.infile] if isinstance(args.infile, str) else list(args.infile)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sl, of = args.slice, args.of
    seen = ok_cc = ok_h3 = no_geom = no_match = 0
    line_no = -1
    t0 = time.time()
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in _iter_jsonl(patterns):
            line_no += 1
            if of > 1 and (line_no % of) != sl:
                continue
            seen += 1
            pid = rec.get("place_id")
            if not pid:
                continue

            # Per-doc: resolve against the place's repr_point (guaranteed within
            # its geometry, so its country IS the place's country). Pass reader
            # None so we DON'T re-read each polygon from the geom store — that
            # disk read per doc capped throughput at ~300/s. The country_index
            # itself was built WITH the reader (real NE country polygons).
            ccodes, outcome = resolve_ccodes_for_doc_exact(
                rec, country_index, None, snap_tol_deg=args.snap_deg,
            )
            if outcome == "ok":
                ok_cc += 1
            elif outcome == "no_geom":
                no_geom += 1
            elif outcome == "no_match":
                no_match += 1

            # Compute per-geom h3 for any geometry missing it (tgn's 2.97M).
            geom_patch: list[dict[str, Any]] = []
            for geom in rec.get("geometries") or []:
                if not isinstance(geom, dict):
                    continue
                has_h3 = geom.get("h3_cover") or geom.get("h3_centroid")
                if has_h3:
                    continue
                rp = geom.get("repr_point")
                if not rp:
                    continue
                try:
                    lon, lat = float(rp["lon"]), float(rp["lat"])
                except (KeyError, TypeError, ValueError):
                    continue
                h3c, h3cover = compute_h3_fields(
                    lon, lat, {"type": "Point", "coordinates": [lon, lat]}
                )
                if not h3c:
                    continue
                geom_patch.append({
                    "geometry_index": geom.get("geometry_index"),
                    "h3_centroid": h3c,
                    "h3_cover": h3cover,
                })
                ok_h3 += 1

            if not ccodes and not geom_patch:
                continue
            patch: dict[str, Any] = {"place_id": pid}
            if ccodes:
                patch["ccodes"] = ccodes
                patch["source"] = SOURCE_LABEL
            if geom_patch:
                patch["geometries"] = geom_patch
            fh.write(json.dumps(patch, ensure_ascii=True) + "\n")

            if seen % 200_000 == 0:
                print(f"  resolved {seen} cc_ok={ok_cc} h3_set={ok_h3} "
                      f"no_match={no_match} no_geom={no_geom} "
                      f"({seen/max(time.time()-t0,1e-6):.0f}/s)", flush=True)

    if place_reader is not None:
        place_reader.close()
    print(f"RESOLVE DONE seen={seen} cc_ok={ok_cc} h3_set={ok_h3} "
          f"no_match={no_match} no_geom={no_geom} "
          f"-> {out_path} in {time.time()-t0:.0f}s", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Phase 3 — apply (pitt, throttled, in-place scripted update; never overwrites)
# ---------------------------------------------------------------------------

_UPDATE_SCRIPT = (
    "if (params.ccodes != null && params.ccodes.length > 0) { "
    "  if (ctx._source.ccodes == null || ctx._source.ccodes.length == 0) { "
    "    ctx._source.ccodes = params.ccodes; "
    "  } "
    "} "
    "if (params.h3 != null && ctx._source.geometries != null) { "
    "  for (g in ctx._source.geometries) { "
    "    if (g.geometry_index != null && params.h3.containsKey(g.geometry_index.toString())) { "
    "      if (g.h3_cover == null && g.h3_centroid == null) { "
    "        def x = params.h3.get(g.geometry_index.toString()); "
    "        g.h3_cover = x.cover; g.h3_centroid = x.centroid; "
    "      } "
    "    } "
    "  } "
    "}"
)


def _iter_jsonl(patterns: list[str]) -> Iterable[dict]:
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) or ([pattern] if os.path.exists(pattern) else [])
        for path in matches:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)


def apply(args) -> int:
    sess = _session()
    r = sess.get(f"{ES_URL}/_alias/{PLACES_ALIAS}")
    r.raise_for_status()
    index = next(iter(r.json().keys()))
    print(f"target index: {index}  (rps≈{args.rps}, batch={args.batch})", flush=True)

    patterns = [args.patch] if isinstance(args.patch, str) else list(args.patch)
    sent = ok = failed = 0
    t0 = time.time()
    batch_lines: list[str] = []

    def flush():
        nonlocal sent, ok, failed, batch_lines
        if not batch_lines:
            return
        payload = "\n".join(batch_lines) + "\n"
        resp = sess.post(f"{ES_URL}/{index}/_bulk?refresh=false", data=payload)
        resp.raise_for_status()
        body = resp.json()
        n = len(batch_lines) // 2
        sent += n
        if body.get("errors"):
            for item in body.get("items", []):
                res = item.get("update", {})
                if res.get("status", 200) >= 300:
                    failed += 1
                    if failed <= 5:
                        print("  update error:", json.dumps(res)[:200], file=sys.stderr)
                else:
                    ok += 1
        else:
            ok += n
        batch_lines = []
        elapsed = time.time() - t0
        target = sent / max(args.rps, 1)
        if target > elapsed:
            time.sleep(target - elapsed)
        if sent % (args.batch * 20) == 0:
            print(f"  applied sent={sent} ok={ok} failed={failed} "
                  f"rate={sent/max(time.time()-t0,1e-6):.0f}/s", flush=True)

    for rec in _iter_jsonl(patterns):
        pid = rec.get("place_id")
        if not pid:
            continue
        ccodes = rec.get("ccodes") or None
        h3 = {
            str(g["geometry_index"]): {"cover": g["h3_cover"], "centroid": g["h3_centroid"]}
            for g in (rec.get("geometries") or [])
            if g.get("geometry_index") is not None
        } or None
        if not ccodes and not h3:
            continue
        batch_lines.append(json.dumps({"update": {"_id": pid}}))
        batch_lines.append(json.dumps({
            "script": {"source": _UPDATE_SCRIPT, "lang": "painless",
                       "params": {"ccodes": ccodes, "h3": h3}}
        }))
        if len(batch_lines) >= args.batch * 2:
            flush()
    flush()
    print(f"APPLY DONE sent={sent} ok={ok} failed={failed} in {time.time()-t0:.0f}s",
          flush=True)
    return 1 if failed else 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Backfill ccodes (+ missing h3) in the live places index")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="scroll target docs from live ES (read-only)")
    e.add_argument("--namespaces", required=True,
                   help="comma-separated namespaces (e.g. tgn,osm,ohm,wd)")
    e.add_argument("--out", required=True)
    e.add_argument("--slice", type=int, default=0)
    e.add_argument("--of", type=int, default=1)
    e.add_argument("--batch", type=int, default=2000)
    e.set_defaults(func=export)

    r = sub.add_parser("resolve", help="resolve ccodes + h3 (needs UN geoms; Slurm)")
    r.add_argument("--in", dest="infile", required=True, help="export JSONL or glob")
    r.add_argument("--out", required=True)
    r.add_argument("--slice", type=int, default=0, help="process lines where i%%of==slice")
    r.add_argument("--of", type=int, default=1)
    r.add_argument("--snap-deg", dest="snap_deg", type=float, default=0.0,
                   help="unambiguous nearest-country snap tolerance in degrees "
                        "(0=off; 0.01≈1km). Recovers border-gap points where "
                        "exactly one country is within tolerance.")
    r.set_defaults(func=resolve)

    a = sub.add_parser("apply", help="throttled scripted update into live ES")
    a.add_argument("--patch", required=True, help="patch file or glob")
    a.add_argument("--rps", type=int, default=1500)
    a.add_argument("--batch", type=int, default=500)
    a.set_defaults(func=apply)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
