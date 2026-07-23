#!/usr/bin/env python
"""One-off remediation (place#145): backfill the *derived* geometry fields —
``h3_centroid`` / ``h3_cover`` / ``bounds`` — that some ingests never wrote, in
place (no reindex / no alias swap → no downtime).

These are all recomputable from what the geometry already carries, so nothing is
invented. Found by the 2026-07-23 audit:

* ``chgis`` 127 and ``og`` 214 geometries have a ``repr_point`` but a null
  ``h3_centroid`` / ``h3_cover`` — invisible to every H3 gate (spatial
  containment, ccode assignment, coverage tiles).
* ``tm`` 24,538 geometries have no ``bounds`` — the field the region builder
  uses for its bbox gate, and the one ``recompute_h3_index`` reads to decide
  whether a feature is sub-cell.

``recompute_h3_index`` cannot reach any of them: it filters on ``has_geom``, and
these are points (or WHG-computed approximations), so there is no geom-store
polygon to recompute from.

Sources, in descending order of fidelity, per geometry:

1. an inline ``hull`` polygon (WHG-computed approximations — e.g. the ottgaz
   admin hulls — are deliberately kept OUT of the geom store and live here);
2. an inline ``geom`` (points keep theirs inline);
3. the ``repr_point`` alone → point semantics, ``h3_cover = [h3_centroid]`` and
   a degenerate ``bounds``, exactly as ``enrich_geometry`` would have written.

Geometries with ``has_geom: true`` are **skipped** — their derived fields must
come from the real polygon in the geom store, which is
``recompute_h3_index``'s job.

Two phases, run separately so the result can be verified between them::

    python -m processing.fix_point_derived_fields scan \
        --namespaces chgis,og,tm --out /vast/ishi/h3fix/point_fields.jsonl

    python -m processing.fix_point_derived_fields apply \
        --patch /vast/ishi/h3fix/point_fields.jsonl --rps 1000

``apply`` uses scripted ``_bulk``, never ``_update_by_query``: the places index
carries the ``extract_namespace`` default_pipeline, which update-by-query
re-runs, and its toponym processor rewrites ``label`` from the already
normalised ``toponym_id`` — silently truncating any label with a parenthesis or
comma. Verified against prod 2026-07-23. Only the missing fields are written; a
field the doc already has is left alone, so the patch is idempotent.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from typing import Any, Iterable

from processing.helpers import (
    COORDINATE_PRECISION,
    H3_CENTROID_RESOLUTION,
    compute_h3_fields,
)
from processing.recompute_h3_index import ES_URL, PLACES_ALIAS, _paginate, _session

try:
    from shapely.geometry import shape as _shape
    _SHAPELY = True
except Exception:  # pragma: no cover
    _SHAPELY = False

DERIVED_FIELDS = ("h3_centroid", "h3_cover", "bounds")


def _repr_lonlat(g: dict) -> tuple[float, float] | None:
    rp = g.get("repr_point")
    if isinstance(rp, dict) and isinstance(rp.get("lon"), (int, float)):
        return float(rp["lon"]), float(rp["lat"])
    if isinstance(rp, (list, tuple)) and len(rp) == 2:
        return float(rp[0]), float(rp[1])
    return None


def _inline_geometry(g: dict) -> dict | None:
    """The best inline geometry for this entry: a WHG-computed ``hull`` first
    (approximation polygons live only here), else an inline ``geom``."""
    for key in ("hull", "geom"):
        obj = g.get(key)
        if isinstance(obj, dict) and obj.get("type") and (
                obj.get("coordinates") or obj.get("geometries")):
            return obj
    return None


def _derive(g: dict) -> dict[str, Any]:
    """The derived fields this geometry is missing, computed from what it has.
    Empty dict when nothing is missing or nothing can be derived."""
    missing = [f for f in DERIVED_FIELDS if not g.get(f)]
    if not missing:
        return {}
    ll = _repr_lonlat(g)
    if ll is None:
        return {}
    lon, lat = ll
    inline = _inline_geometry(g)

    out: dict[str, Any] = {}
    if "h3_centroid" in missing or "h3_cover" in missing:
        centroid, cover = compute_h3_fields(lon=lon, lat=lat, geojson_geom=inline)
        if centroid and "h3_centroid" in missing:
            out["h3_centroid"] = centroid
        if cover and "h3_cover" in missing:
            out["h3_cover"] = list(cover)
    if "bounds" in missing:
        b: list[float] | None = None
        if inline is not None and _SHAPELY:
            try:
                shp = _shape(inline)
                if not shp.is_empty:
                    b = list(shp.bounds)
            except Exception:
                b = None
        if b is None:
            b = [lon, lat, lon, lat]          # a point's own envelope
        out["bounds"] = [round(v, COORDINATE_PRECISION) for v in b]
    return out


# ---------------------------------------------------------------------------
# Phase 1 — scan
# ---------------------------------------------------------------------------

def scan(args) -> int:
    sess = _session()
    namespaces = [n.strip() for n in (args.namespaces or "").split(",") if n.strip()]

    # Any geometry missing at least one derived field while carrying a
    # repr_point. `exists` treats an explicit null as absent, which is how the
    # chgis records present.
    missing_any = {"bool": {"should": [
        {"bool": {"must_not": [{"exists": {"field": f"geometries.{f}"}}]}}
        for f in DERIVED_FIELDS], "minimum_should_match": 1}}
    nested = {"nested": {"path": "geometries", "query": {"bool": {
        "filter": [{"exists": {"field": "geometries.repr_point"}}, missing_any]}}}}
    query: dict[str, Any] = {"bool": {"filter": [nested]}}
    if namespaces:
        query["bool"]["filter"].append({"terms": {"namespace": namespaces}})

    source = ["place_id", "geometries"]
    docs = changed = geoms = skipped_area = 0
    by_field: dict[str, int] = {}
    by_ns: dict[str, int] = {}
    t0 = time.time()
    with open(args.out, "w", encoding="utf-8") as out:
        for hit, _sort in _paginate(sess, query, source, args.batch,
                                    args.slice, args.of):
            docs += 1
            src = hit.get("_source", {})
            pid = src.get("place_id")
            patch_geoms = []
            for idx, g in enumerate(src.get("geometries", []) or []):
                if not isinstance(g, dict):
                    continue
                if g.get("has_geom"):
                    # Real polygon in the geom store → recompute_h3_index's job.
                    skipped_area += 1
                    continue
                derived = _derive(g)
                if not derived:
                    continue
                geoms += 1
                for f in derived:
                    by_field[f] = by_field.get(f, 0) + 1
                patch_geoms.append(
                    {"geometry_index": g.get("geometry_index", idx), **derived})
            if patch_geoms:
                changed += 1
                ns = (pid or ":").split(":", 1)[0]
                by_ns[ns] = by_ns.get(ns, 0) + 1
                out.write(json.dumps({"place_id": pid, "geometries": patch_geoms}) + "\n")
            if docs % 50000 == 0:
                out.flush()
                print(f"[slice {args.slice}/{args.of}] scanned={docs:,} "
                      f"changed={changed:,} rate={docs/max(time.time()-t0,1e-6):.0f}/s",
                      flush=True)
    print(f"[slice {args.slice}/{args.of}] DONE scanned={docs:,} docs_changed={changed:,} "
          f"geoms_changed={geoms:,} skipped_area={skipped_area:,} "
          f"in {time.time()-t0:.0f}s -> {args.out}", flush=True)
    print("by field:    ", json.dumps(by_field, sort_keys=True), flush=True)
    print("by namespace:", json.dumps(by_ns, sort_keys=True), flush=True)
    return 0


# ---------------------------------------------------------------------------
# Phase 2 — apply
# ---------------------------------------------------------------------------

# Writes ONLY the fields present in the patch, and only onto the matching
# geometry — a field the doc has since acquired is left alone.
_UPDATE_SCRIPT = (
    "if (ctx._source.geometries == null) { return; } "
    "int i = 0; "
    "for (g in ctx._source.geometries) { "
    "  def gi = g.geometry_index != null ? g.geometry_index : i; "
    "  def u = params.u.get(gi.toString()); "
    "  if (u != null) { "
    "    if (u.containsKey('h3_centroid')) { g.h3_centroid = u.h3_centroid; } "
    "    if (u.containsKey('h3_cover'))    { g.h3_cover    = u.h3_cover; } "
    "    if (u.containsKey('bounds'))      { g.bounds      = u.bounds; } "
    "  } "
    "  i++; "
    "}"
)


def _iter_patch(patterns: list[str]) -> Iterable[dict]:
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
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
        resp = sess.post(f"{ES_URL}/{index}/_bulk?refresh=false",
                         data="\n".join(batch_lines) + "\n")
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
            print(f"  applied sent={sent:,} ok={ok:,} failed={failed} "
                  f"rate={sent/max(time.time()-t0,1e-6):.0f}/s", flush=True)

    for rec in _iter_patch(patterns):
        pid = rec.get("place_id")
        u = {str(g["geometry_index"]): {k: v for k, v in g.items()
                                        if k != "geometry_index"}
             for g in rec.get("geometries", [])}
        if not pid or not u:
            continue
        batch_lines.append(json.dumps({"update": {"_id": pid}}))
        batch_lines.append(json.dumps({
            "script": {"source": _UPDATE_SCRIPT, "lang": "painless",
                       "params": {"u": u}}}))
        if len(batch_lines) >= args.batch * 2:
            flush()
    flush()
    print(f"APPLY DONE sent={sent:,} ok={ok:,} failed={failed} "
          f"in {time.time()-t0:.0f}s", flush=True)
    return 1 if failed else 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Backfill derived point-geometry fields (place#145)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="find geometries missing derived fields")
    s.add_argument("--namespaces", default=None,
                   help="comma-separated namespace filter (default: all)")
    s.add_argument("--out", required=True)
    s.add_argument("--slice", type=int, default=0)
    s.add_argument("--of", type=int, default=1)
    s.add_argument("--batch", type=int, default=1000)
    s.set_defaults(func=scan)

    a = sub.add_parser("apply", help="apply the patch in place (throttled)")
    a.add_argument("--patch", required=True, help="patch file or glob")
    a.add_argument("--rps", type=int, default=1000)
    a.add_argument("--batch", type=int, default=500)
    a.set_defaults(func=apply)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
