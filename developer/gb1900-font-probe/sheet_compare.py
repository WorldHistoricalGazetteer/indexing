"""Resolve a real OS six-inch sheet by name and emit the commands to compare both detectors over ALL of it.

Earlier comparison images were arbitrary 17x17-tile squares centred on a spot region, which is not a sheet and
is not what anyone means by "an OS sheet". A quarter-sheet (e.g. Yorkshire CCXVIII.NW) is ~27x18 z17 tiles,
~6850x4607 px — a real cartographic unit, and small enough to render at native resolution.

The comparison is only fair if BOTH detectors have seen the whole sheet, so this prints the bbox and the
covering square MapReader's square-region spotter needs, rather than reusing whatever partial coverage happens
to exist from the sampling run.

    python sheet_compare.py --sheet 218_NW --country ENG
    python sheet_compare.py --at -1.5477 53.80957
"""
import argparse, json, math, sys

N17 = 2 ** 17
SHEETS = "/vast/ishi/gb1900/sheets/os_6inch_2nd_GB_4326.geojson"


def lat_px(lat):
    return (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256


def lon_px(lon):
    return (lon + 180.0) / 360.0 * N17 * 256


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheets", default=SHEETS)
    ap.add_argument("--sheet", help="SHEET_NO, e.g. 218_NW")
    ap.add_argument("--county", help="narrow by COUNTY when SHEET_NO repeats across counties")
    ap.add_argument("--at", type=float, nargs=2, metavar=("LON", "LAT"), help="the sheet containing this point")
    ap.add_argument("--edition", default="2")
    a = ap.parse_args()

    from shapely.geometry import shape, Point
    g = json.load(open(a.sheets))
    feats = [f for f in g["features"] if str(f["properties"].get("EDITION")) == a.edition]

    hits = []
    if a.at:
        pt = Point(*a.at)
        for f in feats:
            try:
                s = shape(f["geometry"])
            except Exception:
                continue
            if s.contains(pt):
                hits.append((f, s))
    else:
        for f in feats:
            p = f["properties"]
            if str(p.get("SHEET_NO")) != a.sheet:
                continue
            if a.county and a.county.lower() not in str(p.get("COUNTY", "")).lower():
                continue
            hits.append((f, shape(f["geometry"])))
    if not hits:
        raise SystemExit("no sheet matched")

    for f, s in hits:
        p = f["properties"]
        w, so, e, n = s.bounds
        tx0, tx1 = int(lon_px(w) // 256), int(lon_px(e) // 256)
        ty0, ty1 = int(lat_px(n) // 256), int(lat_px(so) // 256)
        nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
        clon = (w + e) / 2
        clat = (so + n) / 2
        # MapReader's spotter only takes a square region, so it needs a radius covering the longer side.
        r = int(math.ceil(max(nx, ny) / 2)) + 1
        print(f"\n{p.get('COUNTY')} {p.get('SHEET_MAP')}  ({p.get('SHEET_NO')}, {p.get('COUNTRY')}, "
              f"pub {p.get('PUB_STA')}-{p.get('PUB_END')}, survey {p.get('SUR_STA')}-{p.get('SUR_END')})")
        print(f"  bbox   {w:.5f} {so:.5f} {e:.5f} {n:.5f}")
        print(f"  z17    {nx}x{ny} tiles = {nx*ny}  ({nx*256}x{ny*256} px)")
        print(f"  centre {clon:.5f} {clat:.5f}   MapReader --r {r} (square superset: {(2*r+1)**2} tiles)")
        print(f"  TAG=sheet_{p.get('COUNTRY')}_{p.get('SHEET_NO')}")
        print(f"  BBOX='{w:.5f} {so:.5f} {e:.5f} {n:.5f}'")
    print("\nSHEETCOMPAREDONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
