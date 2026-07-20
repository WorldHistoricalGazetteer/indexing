"""Assemble the FULL per-label database from everything the pipeline has captured — so nothing the GPU
produced is stranded in the raw box files. Re-runnable at any time from the persisted boxes (no GPU redo).

For every GB1900 crowd label (gb_stamp.jsonl) it attaches:
  - sheet provenance: NLS sheet id, county/country, survey/revision/publication dates, and the 1879 style
    regime (the OS lettering convention changed in 1879, so this gates the font->type reading);
  - spotter evidence where a MapReader box matches (<= MATCH_M): the spotter's own OCR reading, detection
    score, and a compact reconstructable box geometry — centre lon/lat + width/height (metres) + rotation
    (degrees) from the box's minimum rotated rectangle. The full outline stays in boxes_*.jsonl.
Crowd-MISSED boxes (no crowd label nearby) are written to discoveries.jsonl as first-class records with the
same geometry + sheet provenance — the putative new labels the crowd never transcribed.

    /vast/ishi/envs/boundary/bin/python enrich_edition.py
"""
import glob, json, math
import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import shape
from sklearn.neighbors import BallTree

BASE = "/vast/ishi/gb1900/edition"
STAMP = f"{BASE}/gb_stamp.jsonl"
SHEETS = f"{BASE}/sheets_raw.geojson"
BOXES = f"{BASE}/spot/boxes_gb_*.jsonl"
OUT = f"{BASE}/gb_stamp_enriched.jsonl"
DISC = f"{BASE}/discoveries.jsonl"
MATCH_M = 45.0
Z = 17

def px_to_lonlat(px, py):
    n = 2 ** Z * 256
    lon = px / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / n))))
    return lon, lat

def box_geom(gpolys, clat):
    """min rotated rectangle per box -> (clon, clat, w_m, h_m, angle_deg). px are global z17 Web Mercator."""
    polys = np.array([shapely.Polygon(g) for g in gpolys], dtype=object)
    mrr = shapely.minimum_rotated_rectangle(polys)
    coords = shapely.get_coordinates(mrr).reshape(-1, 5, 2)          # 4 corners + closing point
    e1 = coords[:, 1] - coords[:, 0]; e2 = coords[:, 2] - coords[:, 1]
    l1 = np.hypot(e1[:, 0], e1[:, 1]); l2 = np.hypot(e2[:, 0], e2[:, 1])
    w_px = np.maximum(l1, l2); h_px = np.minimum(l1, l2)
    long_e = np.where((l1 >= l2)[:, None], e1, e2)
    ang = np.degrees(np.arctan2(long_e[:, 1], long_e[:, 0]))
    ang = (ang + 90) % 180 - 90                                      # screen orientation, [-90,90]
    res = 156543.03392 * np.cos(np.radians(clat)) / (2 ** Z)         # m/px at this latitude
    return w_px * res, h_px * res, ang

def main():
    # sheets
    sj = json.load(open(SHEETS))["features"]
    sgeom = [shape(f["geometry"]) for f in sj]; sprop = [f["properties"] for f in sj]
    stree = STRtree(sgeom)
    def sheet_meta(idx):
        p = sprop[idx]
        try: yr = int(str(p.get("PUB_STA") or "")[:4])
        except ValueError: yr = None
        return {"sheet": p.get("SHEET_NO"), "sheet_map": p.get("SHEET_MAP"), "county": p.get("COUNTY"),
                "country": p.get("COUNTRY"), "survey": p.get("SUR_STA"), "revised": p.get("REV_STA"),
                "published": p.get("PUB_STA"),
                "regime": ("pre-1879" if (yr and yr < 1879) else "1879+" if yr else None)}

    # crowd labels
    recs = []; lon = []; lat = []
    for line in open(STAMP):
        try: r = json.loads(line)
        except Exception: continue
        recs.append(r); lon.append(r.get("lon")); lat.append(r.get("lat"))
    lon = np.array([x if x is not None else 999.0 for x in lon]); lat = np.array([y if y is not None else 0.0 for y in lat])
    print(f"crowd labels {len(recs)}", flush=True)

    # assign each label to a sheet
    valid = lon < 900
    pts = shapely.points(lon, lat)
    li, si = stree.query(pts[valid], predicate="intersects")
    vidx = np.where(valid)[0]
    lab_sheet = {}
    for a, b in zip(li, si):
        gi = vidx[a]
        if gi not in lab_sheet: lab_sheet[gi] = b
    print(f"labels on a sheet: {len(lab_sheet)}", flush=True)

    # spotter boxes
    btext = []; bscore = []; bgpoly = []; blon = []; blat = []
    for fp in glob.glob(BOXES):
        for line in open(fp):
            try: b = json.loads(line)
            except Exception: continue
            if b.get("lon") is None or not b.get("gpoly"): continue
            btext.append(b["text"]); bscore.append(b.get("score")); bgpoly.append(b["gpoly"])
            blon.append(b["lon"]); blat.append(b["lat"])
    nb = len(btext); print(f"spotter boxes {nb}", flush=True)

    box_match = {}                     # label idx -> box idx
    disc_idx = []                      # unmatched box indices
    if nb:
        blon = np.array(blon); blat = np.array(blat)
        bw, bh, bang = box_geom(bgpoly, blat)
        if valid.any():
            bt = BallTree(np.radians(np.c_[lat[valid], lon[valid]]), metric="haversine")
            d, j = bt.query(np.radians(np.c_[blat, blon]), k=1)
            dm = d[:, 0] * 6371000.0
            for bi in range(nb):
                if dm[bi] <= MATCH_M:
                    gi = vidx[j[bi, 0]]
                    if gi not in box_match: box_match[gi] = bi
                else:
                    disc_idx.append(bi)
        # which sheet each box is on
        bpts = shapely.points(blon, blat)
        bsheet = {}
        bi2, bs2 = stree.query(bpts, predicate="intersects")
        for a, b in zip(bi2, bs2):
            if a not in bsheet: bsheet[a] = b
    print(f"boxes matched to a label {len(box_match)}; crowd-missed {len(disc_idx)}", flush=True)

    # write enriched edition
    nsheet = nspot = 0
    with open(OUT, "w") as f:
        for gi, r in enumerate(recs):
            out = dict(r)
            if gi in lab_sheet: out["sheet"] = sheet_meta(lab_sheet[gi]); nsheet += 1
            else: out["sheet"] = None
            if gi in box_match:
                bi = box_match[gi]; nspot += 1
                out["spotter"] = {"text": btext[bi], "score": bscore[bi],
                                  "box_w_m": round(float(bw[bi]), 1), "box_h_m": round(float(bh[bi]), 1),
                                  "box_angle_deg": round(float(bang[bi]), 1)}
            else: out["spotter"] = None
            f.write(json.dumps(out, ensure_ascii=False) + "\n")

    # discoveries (crowd-missed detections) as first-class records
    with open(DISC, "w") as f:
        for bi in disc_idx:
            sm = sheet_meta(bsheet[bi]) if bi in bsheet else None
            f.write(json.dumps({"text": btext[bi], "score": bscore[bi],
                                "lon": round(float(blon[bi]), 6), "lat": round(float(blat[bi]), 6),
                                "box_w_m": round(float(bw[bi]), 1), "box_h_m": round(float(bh[bi]), 1),
                                "box_angle_deg": round(float(bang[bi]), 1), "sheet": sm},
                               ensure_ascii=False) + "\n")
    print(f"ENRICHDONE {len(recs)} labels -> {OUT} ({nsheet} sheeted, {nspot} with spotter evidence); "
          f"{len(disc_idx)} discoveries -> {DISC}", flush=True)

if __name__ == "__main__":
    main()
