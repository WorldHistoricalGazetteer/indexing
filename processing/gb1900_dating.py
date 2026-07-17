#!/usr/bin/env python
"""GB1900 STAMP — per-label sheet-precise dating (plan §10b).

Upgrades each label's dataset-level 1888-1914 span to the publication/survey date
of the OS six-inch 2nd-ed **sheet** it falls on, via a point-in-polygon join
against the NLS sheet index.

Input:
  - the GB-STAMP edition JSONL (records carry lon/lat), and
  - the NLS sheet-index GeoJSON/shapefile (sheet polygons + per-sheet date fields).
Output: the edition JSONL with a per-record ``timespan`` + ``sheet`` provenance.

Field names for the sheet id and the date(s) are CONFIGURABLE (`--sheet-id-field`,
`--survey-field`, `--pub-field`, `--revision-field`) because the exact NLS schema is
confirmed separately; if a date field isn't given, a fallback scans all properties
for 4-digit years. CRS is auto-handled (assumes WGS84 per GeoJSON spec unless
`--src-epsg` says otherwise; sheets are reprojected to WGS84 once at load).

  python -m processing.gb1900_dating \
      --edition  /vast/ishi/gb1900/edition/gb-stamp_edition.jsonl \
      --sheets   /vast/ishi/gb1900/sheets/os_6inch_2nd_sheets.geojson \
      --out      /vast/ishi/gb1900/edition/gb-stamp_edition.dated.jsonl \
      --sheet-id-field SHEET --pub-field PUBLISHED --survey-field SURVEYED
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_YEAR = re.compile(r"\b(1[89]\d{2})\b")   # 1800-1999


# ---------------------------------------------------------------------------
# Sheet index loading (GeoJSON or shapefile) → shapely STRtree in WGS84
# ---------------------------------------------------------------------------

def _load_features(path: str):
    """Yield (shapely_geom, properties) from a GeoJSON or ESRI shapefile."""
    from shapely.geometry import shape
    p = Path(path)
    if p.suffix.lower() in (".geojson", ".json"):
        gj = json.loads(p.read_text(encoding="utf-8"))
        feats = gj.get("features", gj if isinstance(gj, list) else [])
        for f in feats:
            geom = f.get("geometry")
            if geom:
                yield shape(geom), (f.get("properties") or {})
    else:  # shapefile
        import shapefile  # pyshp
        r = shapefile.Reader(path)
        flds = [f[0] for f in r.fields[1:]]
        for sr in r.shapeRecords():
            gj = sr.shape.__geo_interface__
            yield shape(gj), dict(zip(flds, sr.record))


def load_sheets(path: str, src_epsg: int | None):
    """Return (STRtree, geoms, props) in WGS84 lon/lat."""
    from shapely import STRtree
    from shapely.ops import transform as shp_transform
    geoms, props = [], []
    reproj = None
    if src_epsg and src_epsg != 4326:
        from pyproj import Transformer
        tr = Transformer.from_crs(src_epsg, 4326, always_xy=True)
        reproj = lambda x, y, z=None: tr.transform(x, y)
    for geom, pr in _load_features(path):
        if not geom.is_valid:
            geom = geom.buffer(0)
        if reproj is not None:
            geom = shp_transform(reproj, geom)
        geoms.append(geom)
        props.append(pr)
    return STRtree(geoms), geoms, props


# ---------------------------------------------------------------------------
# Date extraction from a sheet's properties
# ---------------------------------------------------------------------------

def _year(props: dict, field: str | None) -> int | None:
    if field and props.get(field) not in (None, ""):
        m = _YEAR.search(str(props[field]))
        if m:
            return int(m.group(1))
    return None


def _fallback_years(props: dict) -> list[int]:
    ys = []
    for v in props.values():
        if v:
            ys += [int(m) for m in _YEAR.findall(str(v))]
    return sorted(set(ys))


def sheet_timespan(props: dict, args) -> dict:
    """Build a per-label timespan + sheet provenance from a sheet's properties.
    Prefers survey year for 'start' (when features were current) and publication
    year for 'end'/imprint; falls back to any 4-digit years found."""
    survey = _year(props, args.survey_field)
    revision = _year(props, args.revision_field)
    pub = _year(props, args.pub_field)
    if survey is None and revision is None and pub is None:
        ys = _fallback_years(props)
        survey = ys[0] if ys else None
        pub = ys[-1] if ys else None
    start = survey or revision or pub
    end = pub or revision or survey
    sheet_id = props.get(args.sheet_id_field) if args.sheet_id_field else None
    return {
        "timespan": {"start": start, "end": end},
        "sheet": {"id": sheet_id, "surveyed": survey, "revised": revision,
                  "published": pub, "source": "nls-os-6inch-2nd"},
    }


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def date_edition(args) -> dict:
    from shapely.geometry import Point
    tree, geoms, props = load_sheets(args.sheets, args.src_epsg)
    print(f"[dating] loaded {len(geoms):,} sheets from {args.sheets}")
    stats = {"total": 0, "dated": 0, "no_sheet": 0, "multi_sheet": 0}
    out = open(args.out, "w", encoding="utf-8")
    for line in open(args.edition, encoding="utf-8"):
        rec = json.loads(line)
        stats["total"] += 1
        lon, lat = rec.get("lon"), rec.get("lat")
        if lon is None or lat is None:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n"); continue
        pt = Point(lon, lat)
        cand = tree.query(pt)                       # candidate indices (bbox)
        hits = [i for i in cand if geoms[i].contains(pt)]
        if not hits:
            stats["no_sheet"] += 1
            out.write(json.dumps(rec, ensure_ascii=False) + "\n"); continue
        if len(hits) > 1:
            stats["multi_sheet"] += 1
            # prefer the sheet with the latest publication (the current imprint)
            hits.sort(key=lambda i: (_year(props[i], args.pub_field) or 0), reverse=True)
        dat = sheet_timespan(props[hits[0]], args)
        rec["timespan"] = dat["timespan"]
        rec.setdefault("edits", []).append(
            {"field": "timespan", "to": dat["timespan"], "method": "sheet-date",
             "sheet": dat["sheet"]["id"]})
        rec["sheet"] = dat["sheet"]
        if len(hits) > 1:
            rec["sheet"]["ambiguous"] = True         # near a sheet seam (§9 caveat)
        stats["dated"] += 1
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out.close()
    return stats


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--edition", required=True, help="GB-STAMP edition JSONL")
    p.add_argument("--sheets", required=True, help="NLS sheet index GeoJSON/shapefile")
    p.add_argument("--out", required=True)
    p.add_argument("--src-epsg", type=int, default=4326,
                   help="CRS of the sheet index (4326 default; e.g. 27700 for OSGB)")
    p.add_argument("--sheet-id-field", help="property field for the sheet id")
    p.add_argument("--survey-field", help="property field for the survey date")
    p.add_argument("--revision-field", help="property field for the revision date")
    p.add_argument("--pub-field", help="property field for the publication date")
    args = p.parse_args(argv)
    stats = date_edition(args)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
