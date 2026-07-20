"""Per-sheet statistics for the GB-STAMP map's sheet-outline overlay. Spatially joins every GB1900 crowd
label and every MapReader spotter box to the NLS OS six-inch sheet index (edition 2), and matches spotter
boxes to crowd labels so we can quantify the *crowd-missed* detections the spotter surfaces.

For each sheet we emit: crowd-label count; typed / font-read counts + mean font confidence + top types;
spotter boxes so far and how many are *putative new* (no crowd label within MATCH_M metres); plus the sheet's
NLS metadata (dates, county, image URL). Sheets with no boxes yet are flagged not-yet-read.

    /vast/ishi/envs/boundary/bin/python compute_sheet_stats.py   # -> sheets.geojson (for docs/data/)
"""
import glob, json, math, os
import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import shape, mapping
from sklearn.neighbors import BallTree

BASE = "/vast/ishi/gb1900/edition"
SHEETS = f"{BASE}/sheets_raw.geojson"
STAMP = f"{BASE}/gb_stamp.jsonl"
BOXES = f"{BASE}/spot/boxes_gb_*.jsonl"
OUT = f"{BASE}/sheets.geojson"
MATCH_M = 45.0                       # a box within this of a crowd label counts as "matched"
SIMPLIFY = 0.0004                    # ~30 m; sheet footprints are quads, keeps geojson tiny

def main():
    sj = json.load(open(SHEETS))["features"]
    geoms = [shape(f["geometry"]) for f in sj]
    props = [f["properties"] for f in sj]
    tree = STRtree(geoms)
    ns = len(geoms); print(f"sheets: {ns}", flush=True)

    # --- crowd labels ---
    lon = []; lat = []; typed = []; fonted = []; conf = []; ty = []
    for line in open(STAMP):
        try: r = json.loads(line)
        except Exception: continue
        if r.get("lon") is None: continue
        lon.append(r["lon"]); lat.append(r["lat"])
        t = r.get("type"); typed.append(bool(t)); ty.append(t or "")
        fs = r.get("font_style"); c = r.get("font_conf") or 0.0
        fonted.append(bool(fs) and c < 0.999)    # genuine classifier read (exclude OS-convention forced, conf=1.0)
        conf.append(c)
    lon = np.array(lon); lat = np.array(lat)
    typed = np.array(typed); fonted = np.array(fonted); conf = np.array(conf); ty = np.array(ty)
    print(f"crowd labels: {len(lon)}", flush=True)

    crowd = shapely.points(lon, lat)
    ci, si = tree.query(crowd, predicate="intersects")     # ci->crowd idx, si->sheet idx
    from collections import defaultdict, Counter
    S_crowd = np.zeros(ns, int); S_typed = np.zeros(ns, int); S_font = np.zeros(ns, int)
    S_conf = np.zeros(ns); S_types = defaultdict(Counter)
    for c, s in zip(ci, si):
        S_crowd[s] += 1
        if typed[c]: S_typed[s] += 1; S_types[s][ty[c]] += 1
        if fonted[c]: S_font[s] += 1; S_conf[s] += conf[c]
    print("crowd->sheet joined", flush=True)

    # --- spotter boxes ---
    blon = []; blat = []
    for fp in glob.glob(BOXES):
        for line in open(fp):
            try: b = json.loads(line)
            except Exception: continue
            if b.get("lon") is None: continue
            blon.append(b["lon"]); blat.append(b["lat"])
    S_box = np.zeros(ns, int); S_new = np.zeros(ns, int)
    if blon:
        blon = np.array(blon); blat = np.array(blat)
        print(f"spotter boxes: {len(blon)}", flush=True)
        # match each box to nearest crowd label (haversine BallTree in radians)
        bt = BallTree(np.radians(np.c_[lat, lon]), metric="haversine")
        d, _ = bt.query(np.radians(np.c_[blat, blon]), k=1)
        matched = (d[:, 0] * 6371000.0) <= MATCH_M          # earth radius m
        boxpts = shapely.points(blon, blat)
        bi, bs = tree.query(boxpts, predicate="intersects")
        for b, s in zip(bi, bs):
            S_box[s] += 1
            if not matched[b]: S_new[s] += 1
        print("boxes->sheet joined", flush=True)

    feats = []
    for i, (g, p) in enumerate(zip(geoms, props)):
        sg = g.simplify(SIMPLIFY, preserve_topology=True)
        top = [{"k": k, "n": n} for k, n in S_types[i].most_common(4)]
        mean_conf = round(S_conf[i] / S_font[i], 3) if S_font[i] else None
        feats.append({"type": "Feature", "geometry": mapping(sg), "properties": {
            "sheet": p.get("SHEET_NO"), "map": p.get("SHEET_MAP"), "county": p.get("COUNTY"),
            "country": p.get("COUNTRY"), "dates": p.get("DATES"), "pub": p.get("PUB_STA"),
            "url": p.get("IMAGEURL"),
            "crowd": int(S_crowd[i]), "typed": int(S_typed[i]), "font": int(S_font[i]),
            "mean_conf": mean_conf, "top": top,
            "boxes": int(S_box[i]), "new": int(S_new[i]), "read": bool(S_box[i] > 0)}})
    json.dump({"type": "FeatureCollection", "features": feats}, open(OUT, "w"), ensure_ascii=False)
    read_n = sum(1 for f in feats if f["properties"]["read"])
    print(f"wrote {len(feats)} sheets -> {OUT} ({os.path.getsize(OUT)//1024} KB); {read_n} read so far", flush=True)

if __name__ == "__main__":
    main()
