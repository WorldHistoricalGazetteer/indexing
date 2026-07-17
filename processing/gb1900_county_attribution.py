#!/usr/bin/env python
"""Label GB-STAMP records with their HISTORIC COUNTY via HCT polygons (point-in-polygon).

The HCT / `ukhc` historic-county polygons (Historic Counties Trust, county-borders.co.uk;
OPEN, attribution) are correct-period for counties, so each GB-STAMP label can be given a
`hc_county` = HCT **3-character HCS_CODE** (e.g. `CRN` Caernarfonshire, `DBH` Denbighshire)
— not the full name.

Test point per label = the **centre of the label**, because the GB1900 pin sits at the
BOTTOM-LEFT of the text bounding box (its anchor), not the middle. Where a bounding box was
detected (VLM stage) we use its centre; otherwise a **best-guess offset** from the pin
(up-and-right by ~half the estimated label extent). For county assignment the offset is
small vs county size — it only matters within ~100 m of a border — but we honour it for
those edge cases.

  python -m processing.gb1900_county_attribution \
      --records /vast/ishi/gb1900/edition/national_typed.jsonl \
      --hct     /ix1/ishi/data/authorities/ukhc/UKDefinitionA.shp \
      --out     /vast/ishi/gb1900/edition/gb_hc_county.jsonl
"""
from __future__ import annotations
import argparse, json, math, sys
from pathlib import Path

# six-inch (1:10560) nominal label metrics -> ground metres (best-guess offset)
CHAR_W_M = 9.0     # ~ground width of one character
CAP_H_M = 14.0     # ~ground cap height


def _text(rec):
    t = rec.get("text")
    return (t.get("value") if isinstance(t, dict) else t) or ""


def label_centre(rec):
    """(lon, lat) of the label centre. bbox centre if present, else pin + best-guess offset."""
    lon, lat = rec.get("lon"), rec.get("lat")
    if lon is None or lat is None:
        return None
    bb = rec.get("bbox") or (rec.get("crop") or {}).get("bbox")
    if bb and len(bb) == 4:                       # [lon0,lat0,lon1,lat1] or pixel? assume geo
        return ((bb[0]+bb[2])/2, (bb[1]+bb[3])/2)
    # best-guess offset: pin is bottom-left of horizontal text -> centre is up & right
    n = max(len(_text(rec)), 1)
    right_m = n * CHAR_W_M / 2.0
    up_m = CAP_H_M / 2.0
    dlat = up_m / 111320.0
    dlon = right_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return (lon + dlon, lat + dlat)


def load_hct(shp_path):
    import shapefile
    from shapely.geometry import shape
    from shapely import STRtree
    r = shapefile.Reader(shp_path)
    flds = [f[0] for f in r.fields[1:]]
    ci = flds.index("HCS_CODE")
    geoms, codes = [], []
    for sr in r.shapeRecords():
        g = shape(sr.shape.__geo_interface__)
        if not g.is_valid:
            g = g.buffer(0)
        geoms.append(g); codes.append(sr.record[ci])
    return STRtree(geoms), geoms, codes


def run(a):
    from shapely.geometry import Point
    from shapely import prepared
    tree, geoms, codes = load_hct(a.hct)
    prep = [prepared.prep(g) for g in geoms]
    print(f"[county] loaded {len(geoms)} HCT counties")
    out = open(a.out, "w", encoding="utf-8")
    # near-border labels -> work-list for VLM TRUE bounding-box finding (precise centre -> county)
    unc = open(a.uncertain_out, "w", encoding="utf-8") if a.uncertain_out else None
    stats = {"total": 0, "assigned": 0, "no_county": 0, "bbox": 0, "offset": 0, "uncertain": 0}
    for line in open(a.records, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        stats["total"] += 1
        c = label_centre(rec)
        if c is None:
            continue
        stats["bbox" if (rec.get("bbox") or (rec.get("crop") or {}).get("bbox")) else "offset"] += 1
        pt = Point(*c)
        code = None; ci = None
        for i in tree.query(pt):                   # bbox candidates
            if prep[i].contains(pt):
                code = codes[i]; ci = i; break
        pid = rec.get("place_id") or f"gb:{rec.get('pin_id')}"
        if code:
            stats["assigned"] += 1
            # uncertainty: point within `uncertain_m` of the county boundary -> the pin/centre
            # offset (or the c.1900-vs-HCT geometry) could flip the county. Flag it.
            dist_m = geoms[ci].boundary.distance(pt) * 111000.0
            uncertain = dist_m < a.uncertain_m
            rec_out = {"place_id": pid, "hc_county": code}
            if uncertain:
                rec_out["hc_county_uncertain"] = True
                rec_out["hc_county_border_m"] = round(dist_m)
                stats["uncertain"] += 1
                if unc is not None and not (rec.get("bbox") or (rec.get("crop") or {}).get("bbox")):
                    # no true bbox yet -> send to VLM for precise bbox (then recompute county)
                    unc.write(json.dumps({"place_id": pid, "lon": rec.get("lon"),
                                          "lat": rec.get("lat"), "text": _text(rec),
                                          "border_m": round(dist_m)}, ensure_ascii=False) + "\n")
            out.write(json.dumps(rec_out, ensure_ascii=False) + "\n")
        else:
            stats["no_county"] += 1
        if stats["total"] % 200000 == 0:
            print(f"[county] {stats['total']:,} processed, {stats['assigned']:,} assigned")
    out.close()
    if unc is not None:
        unc.close()
    print("[county]", json.dumps(stats))
    return stats


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--records", required=True, help="GB-STAMP records JSONL (lon/lat + text[/bbox])")
    p.add_argument("--hct", required=True, help="HCT UKDefinitionA shapefile (.shp)")
    p.add_argument("--out", required=True, help="output JSONL: {place_id, hc_county[, hc_county_uncertain]}")
    p.add_argument("--uncertain-m", type=float, default=100.0,
                   help="flag hc_county_uncertain when the test point is within this many metres of the border")
    p.add_argument("--uncertain-out",
                   help="work-list JSONL of near-border labels (no bbox yet) to send to the VLM for true-bbox finding")
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
