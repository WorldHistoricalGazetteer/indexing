#!/usr/bin/env python
"""Attach open admin tags (nation / district / parish) to GB-STAMP via a GB1900 gazetteer join.

The GB1900 complete/abridged gazetteer (CC-BY-SA, visionofbritain.org.uk) already carries
`nation, local_authority (district), parish` per pin — VoB's own point-in-polygon result,
published openly. Our `gb:<pin_id>` == the gazetteer pin_id, so this is a direct join (no
boundary geometry, no CAMPOP/GBHGIS). See plan-gb1900-typing.md §0a.

Fill in the gazetteer is ~nation 100% / district 100% / parish 95%. For any point still
lacking a facet (not in the gazetteer, or an empty parish), a **triangulation fallback**
(SG 2026-07-18): among the points that DO have the facet, if the Delaunay triangle containing
the test point has all three vertices agreeing on that facet, adopt it. Provenance recorded
per facet (`gazetteer` | `interp` ).

  python -m processing.gb1900_admin_join \
      --gazetteer /vast/…/gb1900_gazetteer_complete.csv \
      --records   /vast/ishi/gb1900/edition/national_typed.jsonl \
      --out       /vast/ishi/gb1900/edition/gb_admin.jsonl
"""
from __future__ import annotations
import argparse, csv, io, json, sys
import numpy as np

FACETS = ["nation", "district", "parish"]
_GAZ_COL = {"nation": "nation", "district": "local_authority", "parish": "parish"}


def load_gazetteer(path, encoding):
    """pin_id -> {lon, lat, nation, district, parish}."""
    gaz = {}
    with io.open(path, encoding=encoding, newline="") as f:
        for row in csv.DictReader(f):
            pid = (row.get("pin_id") or "").strip()
            if not pid:
                continue
            try:
                lon = float(row["longitude"]); lat = float(row["latitude"])
            except (KeyError, ValueError, TypeError):
                lon = lat = None
            gaz[pid] = {"lon": lon, "lat": lat,
                        **{fac: (row.get(col) or "").strip() or None
                           for fac, col in _GAZ_COL.items()}}
    return gaz


def build_delaunay(gaz):
    """One Delaunay over all gazetteer points that have coords; aligned per-facet value arrays."""
    from scipy.spatial import Delaunay
    pts, vals = [], {f: [] for f in FACETS}
    for g in gaz.values():
        if g["lon"] is None:
            continue
        pts.append((g["lon"], g["lat"]))
        for f in FACETS:
            vals[f].append(g[f])
    pts = np.asarray(pts, dtype=float)
    print(f"[admin] Delaunay over {len(pts):,} gazetteer points…", flush=True)
    tri = Delaunay(pts)
    return tri, {f: np.asarray(vals[f], dtype=object) for f in FACETS}


def run(a):
    gaz = load_gazetteer(a.gazetteer, a.encoding)
    print(f"[admin] gazetteer: {len(gaz):,} pins", flush=True)

    # pass 1 — stream records, direct join, collect the ones needing interpolation per facet
    recs = []                      # (place_id, pin_id, lon, lat, {facet: val}, {facet: source})
    need = {f: [] for f in FACETS}  # indices into recs that still lack facet f (and have coords)
    for line in open(a.records, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        pid = r.get("pin_id")
        lon, lat = r.get("lon"), r.get("lat")
        g = gaz.get(pid, {})
        val = {f: g.get(f) for f in FACETS}
        src = {f: ("gazetteer" if val[f] else None) for f in FACETS}
        idx = len(recs)
        recs.append([r.get("place_id") or (f"gb:{pid}" if pid else None), pid, lon, lat, val, src])
        if lon is not None and lat is not None:
            for f in FACETS:
                if not val[f]:
                    need[f].append(idx)
    print(f"[admin] {len(recs):,} records; needing interp: "
          f"{ {f: len(need[f]) for f in FACETS} }", flush=True)

    # pass 2 — triangulation fallback (only if something is missing)
    if any(need[f] for f in FACETS):
        tri, vals = build_delaunay(gaz)
        for f in FACETS:
            if not need[f]:
                continue
            q = np.array([(recs[i][2], recs[i][3]) for i in need[f]], dtype=float)
            simp = tri.find_simplex(q)                     # -1 = outside convex hull
            adopted = 0
            for k, i in enumerate(need[f]):
                s = simp[k]
                if s < 0:
                    continue
                vs = vals[f][tri.simplices[s]]              # 3 triangle-vertex values
                if vs[0] and vs[0] == vs[1] == vs[2]:       # all present + agree
                    recs[i][4][f] = vs[0]; recs[i][5][f] = "interp"; adopted += 1
            print(f"[admin] {f}: interpolated {adopted:,}/{len(need[f]):,}", flush=True)

    # write patch
    stats = {f: {"gazetteer": 0, "interp": 0, "none": 0} for f in FACETS}
    with open(a.out, "w", encoding="utf-8") as out:
        for pidname, pin, lon, lat, val, src in recs:
            if pidname is None:
                continue
            admin = {f: val[f] for f in FACETS if val[f]}
            asrc = {f: src[f] for f in FACETS if src[f]}
            for f in FACETS:
                stats[f][src[f] or "none"] += 1
            if admin:
                out.write(json.dumps({"place_id": pidname, "admin": admin,
                                      "admin_source": asrc}, ensure_ascii=False) + "\n")
    print("[admin] " + json.dumps(stats))
    return stats


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gazetteer", required=True, help="GB1900 complete gazetteer CSV (pin_id,…,nation,local_authority,parish,…)")
    p.add_argument("--records", required=True, help="GB-STAMP records JSONL (place_id/pin_id/lon/lat)")
    p.add_argument("--out", required=True, help="output patch JSONL: {place_id, admin{}, admin_source{}}")
    p.add_argument("--encoding", default="utf-16", help="gazetteer CSV encoding (VoB CSVs are utf-16)")
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
