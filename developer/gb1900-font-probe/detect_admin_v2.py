"""GB-STAMP (b) v2 — group MapReader's LARGE TEXT FRAGMENTS into admin labels. The v1 raw-pixel probe drowned
in buildings; MapReader already discriminates text from buildings, it just can't join the wide letter-spacing
of big admin labels — so it emits fragments ('NE','CH','BER','RO' for NORWICH/BARRACKS). We take its large
short-text detections and do the grouping it won't: horizontal, same-cap-height, baseline-aligned, evenly-
spaced runs. Visualise the grouped runs (with their concatenated recovered text) over the mosaic to judge
whether they land on the real admin labels — before wiring gazetteer-fuzzy reading.

    /vast/ishi/envs/boundary/bin/python detect_admin_v2.py --lon .. --lat .. --tag .. --bigh 55
"""
import argparse, os, io, math, time, json, urllib.request
from collections import defaultdict
import numpy as np, cv2
from PIL import Image

N17 = 2 ** 17
TILES = "/vast/ishi/gb1900/tiles17"; IX1 = "/ix1/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
SPOT = "/vast/ishi/gb1900/edition/spot"; OUT = "/vast/ishi/gb1900/edition/admin"; os.makedirs(OUT, exist_ok=True)

def lonlat_to_px(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y

def get_tile(tx, ty):
    for base in (TILES, IX1):
        p = f"{base}/{tx}/{ty}.png"
        if os.path.exists(p) and os.path.getsize(p) > 500:
            try: return np.asarray(Image.open(p).convert("L"), np.uint8)
            except Exception: pass
    os.makedirs(f"{TILES}/{tx}", exist_ok=True)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-admin"}), timeout=30) as r:
                data = r.read()
            if len(data) > 400:
                open(f"{TILES}/{tx}/{ty}.png", "wb").write(data)
                return np.asarray(Image.open(io.BytesIO(data)).convert("L"), np.uint8)
            return None
        except Exception as e:
            if getattr(e, "code", None) in (403, 404): return None
            time.sleep(1.0 * (attempt + 1))
    return None

def assemble(cx_tile, cy_tile, r):
    side = 2 * r + 1
    canvas = np.full((side * 256, side * 256), 255, np.uint8)
    for i in range(side):
        for j in range(side):
            t = get_tile(cx_tile - r + i, cy_tile - r + j)
            if t is not None: canvas[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = t
    return canvas

def frag(box):                                                 # -> (cx,cy,h,w,text) in GLOBAL z17 px, or None
    g = box.get("gpoly")
    if not g: return None
    xs = [p[0] for p in g]; ys = [p[1] for p in g]
    h = max(ys) - min(ys); w = max(xs) - min(xs)
    t = "".join(c for c in (box.get("text") or "") if c.isalnum())
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, h, w, t)

def group_runs(frags):
    n = len(frags); parent = list(range(n))
    def find(a):
        while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for i in range(n):
        for j in range(i + 1, n):
            cxi, cyi, hi, wi, _ = frags[i]; cxj, cyj, hj, wj, _ = frags[j]
            if not (0.65 <= hi / hj <= 1.54): continue         # same cap-height band
            mh = (hi + hj) / 2
            if abs(cyi - cyj) > 0.5 * mh: continue             # baseline-aligned (horizontal labels)
            gap = abs(cxi - cxj) - (wi + wj) / 2               # edge-to-edge gap
            if not (-0.3 * mh <= gap <= 3.5 * mh): continue    # adjacent, allowing wide letter-spacing
            parent[find(i)] = find(j)
    g = defaultdict(list)
    for i in range(n): g[find(i)].append(i)
    runs = []
    for idxs in g.values():
        if len(idxs) < 2: continue
        idxs.sort(key=lambda i: frags[i][0])
        text = "".join(frags[i][4] for i in idxs)
        if len(text) < 3: continue
        runs.append((idxs, text))
    return runs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lon", type=float, required=True); ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--tag", required=True); ap.add_argument("--r", type=int, default=8)
    ap.add_argument("--bigh", type=float, default=55.0, help="min cap-height px for an admin-scale fragment")
    ap.add_argument("--score", type=float, default=0.5)
    a = ap.parse_args()
    bf = f"{SPOT}/boxes_{a.tag}.jsonl"
    rows = [json.loads(l) for l in open(bf)] if os.path.exists(bf) else []
    frags = [f for f in (frag(r) for r in rows if r.get("score", 0) >= a.score) if f and f[2] >= a.bigh]
    print(f"{a.tag}: {len(rows)} boxes -> {len(frags)} large fragments (h>={a.bigh})", flush=True)
    runs = group_runs(frags)
    runs.sort(key=lambda r: -np.median([frags[i][2] for i in r[0]]))
    print(f"{a.tag}: {len(runs)} grouped runs:", flush=True)
    for idxs, text in runs[:25]:
        mh = int(np.median([frags[i][2] for i in idxs]))
        print(f"    h={mh:3}  {len(idxs)}frag  '{text}'", flush=True)
    # visualise
    cxp, cyp = lonlat_to_px(a.lon, a.lat); ctx, cty = int(cxp // 256), int(cyp // 256)
    ox, oy = (ctx - a.r) * 256, (cty - a.r) * 256
    gray = assemble(ctx, cty, a.r); vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    for (cx, cy, h, w, _t) in frags:
        cv2.rectangle(vis, (int(cx - ox - w / 2), int(cy - oy - h / 2)), (int(cx - ox + w / 2), int(cy - oy + h / 2)), (255, 120, 0), 2)
    for idxs, text in runs:
        pts = [(int(frags[i][0] - ox), int(frags[i][1] - oy)) for i in idxs]
        for k in range(len(pts) - 1): cv2.line(vis, pts[k], pts[k + 1], (0, 0, 255), 3)
        cv2.putText(vis, text[:24], (pts[0][0], pts[0][1] - 12), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 255), 3)
    outp = f"{OUT}/detectv2_{a.tag}.png"
    Image.fromarray(cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)).save(outp)
    print(f"{a.tag}: wrote {outp}", flush=True)

if __name__ == "__main__":
    main()
