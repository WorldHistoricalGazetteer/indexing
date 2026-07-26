#!/usr/bin/env python
"""Backfill ``geometries[].geom_class`` onto existing docs, in place (place#145).

``geom_class`` (shape ∈ point/line/area) is written at ingest by
``enrich_geometry`` going forward, and the osm/ohm way restore already set it on
the ~10.5M ways. This backfills the geometry that predates that:

  * ``--mode areal`` — ``has_geom`` geometries with no ``geom_class`` (relations,
    gazetteer boundaries; ~984k). geom_class is derived from the **actual stored
    geometry type** via ``geom_class_of`` (Polygon/MultiPolygon/GC → area, etc.)
    — no guessing.
  * ``--mode points`` — located geometries with no ``geom_class`` and no
    ``has_geom`` (~35M points). These are set ``point`` directly.

NOTE the gateway already treats a missing ``geom_class`` correctly via a
transitional fallback (``is_areal`` uses ``has_geom`` when geom_class is absent),
so this backfill is about **completeness** (an explicit shape flag + the
``geom_class ∈ {area,line} AND NOT has_geom`` defect predicate covering the whole
corpus), not correctness. ``points`` in particular is correctness-neutral.

Runs on the Pitt VM (needs both ES and the /vast geom store). Scripted ``_bulk``
UPDATE ops only — never ``_update_by_query`` (which would re-run the
``extract_namespace`` pipeline and rewrite toponym labels). Idempotent (skips
geometries that already have geom_class); throttled.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from processing.helpers import geom_class_of
from processing.recompute_h3_index import ES_URL, PLACES_ALIAS, _paginate, _session


_UPDATE_SCRIPT = (
    "if (ctx._source.geometries == null) { return; } "
    "int i = 0; "
    "for (g in ctx._source.geometries) { "
    "  def gi = g.geometry_index != null ? g.geometry_index : i; "
    "  if (params.u.containsKey(gi.toString())) { g.geom_class = params.u.get(gi.toString()); } "
    "  i++; "
    "}"
)


def run(args) -> int:
    sess = _session()
    r = sess.get(f"{ES_URL}/_alias/{PLACES_ALIAS}")
    r.raise_for_status()
    index = next(iter(r.json().keys()))

    reader = None
    if args.mode == "areal":
        from processing.geom_store import GeomStoreReader
        from processing.settings import GEOM_STORE_DIR
        reader = GeomStoreReader(GEOM_STORE_DIR)

    # geometries missing geom_class, gated by has_geom for the two modes.
    has_geom = {"term": {"geometries.has_geom": True}}
    no_gc = {"bool": {"must_not": [{"exists": {"field": "geometries.geom_class"}}]}}
    if args.mode == "areal":
        inner = {"bool": {"filter": [has_geom], "must": [no_gc]}}
    else:  # points
        inner = {"bool": {"filter": [{"exists": {"field": "geometries.repr_point"}}],
                          "must": [no_gc], "must_not": [has_geom]}}
    query = {"nested": {"path": "geometries", "query": inner}}
    source = ["place_id", "geometries.geometry_index", "geometries.has_geom",
              "geometries.geom_class", "geometries.geom_ref", "geometries.repr_point"]

    sent = ok = failed = docs = geoms = errors = 0
    batch: list[str] = []
    t0 = time.time()

    def flush():
        nonlocal sent, ok, failed, batch
        if not batch:
            return
        resp = sess.post(f"{ES_URL}/{index}/_bulk?refresh=false", data="\n".join(batch) + "\n")
        resp.raise_for_status()
        body = resp.json()
        n = len(batch) // 2
        sent += n
        if body.get("errors"):
            for it in body.get("items", []):
                st = it.get("update", {}).get("status", 200)
                if st >= 300:
                    failed += 1
                elif True:
                    ok += 1
        else:
            ok += n
        batch = []
        el = time.time() - t0
        tgt = sent / max(args.rps, 1)
        if tgt > el:
            time.sleep(tgt - el)
        if sent % (args.batch * 40) == 0:
            print(f"  applied sent={sent:,} ok={ok:,} failed={failed} "
                  f"rate={sent/max(time.time()-t0,1e-6):.0f}/s", flush=True)

    for hit, _sort in _paginate(sess, query, source, args.scroll, 0, 1):
        docs += 1
        src = hit.get("_source", {})
        pid = src.get("place_id")
        upd = {}
        for idx, g in enumerate(src.get("geometries", []) or []):
            if not isinstance(g, dict) or g.get("geom_class"):
                continue
            gi = g.get("geometry_index", idx)
            if args.mode == "points":
                if g.get("has_geom") or not g.get("repr_point"):
                    continue
                upd[str(gi)] = "point"
                geoms += 1
            else:  # areal — derive from the real stored geometry
                if not g.get("has_geom"):
                    continue
                ref = g.get("geom_ref") or f"{pid}_{gi}"
                try:
                    gj = reader.get(ref)
                except Exception:
                    gj = None
                gc = geom_class_of(gj) if gj else None
                if not gc:
                    errors += 1
                    continue
                upd[str(gi)] = gc
                geoms += 1
        if upd:
            batch.append(json.dumps({"update": {"_id": pid}}))
            batch.append(json.dumps({"script": {"source": _UPDATE_SCRIPT, "lang": "painless",
                                                "params": {"u": upd}}}))
            if len(batch) >= args.batch * 2:
                flush()
        if docs % 200000 == 0:
            print(f"  scanned={docs:,} geoms={geoms:,} errors={errors} "
                  f"rate={docs/max(time.time()-t0,1e-6):.0f}/s", flush=True)
    flush()
    print(f"DONE mode={args.mode} scanned={docs:,} geoms_set={geoms:,} "
          f"store_misses={errors} sent={sent:,} ok={ok:,} failed={failed} "
          f"in {time.time()-t0:.0f}s", flush=True)
    return 1 if failed else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill geometries[].geom_class (place#145)")
    p.add_argument("--mode", choices=["areal", "points"], required=True)
    p.add_argument("--rps", type=int, default=1500)
    p.add_argument("--batch", type=int, default=500)
    p.add_argument("--scroll", type=int, default=2000)
    args = p.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
