"""Phase B step 2 (anchor probe) — measure BENCH-MARK (B.M.) cap-heights on real z17 OS tiles, to see how
noisy a SMALL anchor is (SG's reservation). For a sample of 'B.M. <height>' crowd labels: fetch the z17
tiles, crop a window centred on the crowd point, isolate the central text-line band, measure its height in
z17 px, and convert to GROUND-METRES via the tile's metres-per-pixel(latitude) — the latitude-invariant unit
(= paper-mm x 10.56). Reports median + IQR + N so we can judge the measurement noise empirically.

    python measure_z17_bm.py --n 400 --workers 48
"""
import argparse, os, io, math, json, re, time, urllib.request, numpy as np
import concurrent.futures as cf
from PIL import Image

NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
TILES = "/vast/ishi/gb1900/tiles17"          # reuse cache if present
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
N17 = 2 ** 17
BM = re.compile(r"^\s*B[. ]?M[. ]\s*\d")     # 'B.M. <number>' (has a height -> a clear text line)
CROP_W, CROP_H = 300, 96

def px17(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y
def mpp(lat):                                 # z17 metres per pixel at latitude
    return 156543.03392 * math.cos(math.radians(lat)) / N17

_cache = {}
def tile(tx, ty):
    k = (tx, ty)
    if k in _cache: return _cache[k]
    p = f"{TILES}/{tx}/{ty}.png"; im = None
    if os.path.exists(p) and os.path.getsize(p) > 500:
        try: im = np.asarray(Image.open(p).convert("L"), np.float32) / 255.0
        except Exception: im = None
    if im is None:
        try:
            with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-bm"}), timeout=30) as r:
                data = r.read()
            if len(data) > 400:
                os.makedirs(f"{TILES}/{tx}", exist_ok=True); open(p, "wb").write(data)
                im = np.asarray(Image.open(io.BytesIO(data)).convert("L"), np.float32) / 255.0
        except Exception: im = None
    if len(_cache) < 4000: _cache[k] = im
    return im

def crop_window(lon, lat):
    xp, yp = px17(lon, lat)
    x0, y0 = int(xp - CROP_W / 2), int(yp - CROP_H / 2)
    tx0, tx1 = x0 // 256, (x0 + CROP_W) // 256; ty0, ty1 = y0 // 256, (y0 + CROP_H) // 256
    canvas = np.ones(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), np.float32); ok = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = tile(tx, ty)
            if t is not None:
                canvas[(ty - ty0) * 256:(ty - ty0) * 256 + t.shape[0], (tx - tx0) * 256:(tx - tx0) * 256 + t.shape[1]] = t; ok = True
    if not ok: return None
    L, U = x0 - tx0 * 256, y0 - ty0 * 256
    return canvas[U:U + CROP_H, L:L + CROP_W]

def measure(crop):
    """Cap-height (px) of the central text-line band. Central = the ink band nearest the crop centre;
    excludes descenders via modal column-bottom within that band. Returns None if no clear band."""
    ink = crop < 0.5
    if ink.sum() < 30: return None
    rowden = ink.sum(1)
    thr = max(3, 0.12 * rowden.max())
    on = rowden > thr
    # contiguous bands
    bands = []; s = None
    for i, v in enumerate(on):
        if v and s is None: s = i
        elif not v and s is not None: bands.append((s, i - 1)); s = None
    if s is not None: bands.append((s, len(on) - 1))
    if not bands: return None
    cy = CROP_H / 2
    b = min(bands, key=lambda bb: abs((bb[0] + bb[1]) / 2 - cy))   # band nearest centre
    if b[1] - b[0] < 4 or b[1] - b[0] > CROP_H * 0.8: return None
    sub = ink[b[0]:b[1] + 1]
    cols = [c for c in range(sub.shape[1]) if sub[:, c].any()]
    if len(cols) < 4: return None
    tops = [np.where(sub[:, c])[0][0] for c in cols]; bots = [np.where(sub[:, c])[0][-1] for c in cols]
    from collections import Counter
    base = max(Counter(round(x / 2) * 2 for x in bots), key=lambda k: (Counter(round(x / 2) * 2 for x in bots)[k], k))
    base = max(base, int(np.percentile(bots, 60)))
    return base - min(tops)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=400); ap.add_argument("--workers", type=int, default=48)
    a = ap.parse_args()
    cand = []
    for line in open(NT):
        try: d = json.loads(line)
        except Exception: continue
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        lon, lat = d.get("lon"), d.get("lat")
        if tv and lon and lat and BM.match(tv): cand.append((lon, lat, tv))
    step = max(1, len(cand) // a.n); sample = cand[::step][:a.n]
    print(f"{len(cand)} B.M.+height labels; measuring {len(sample)} (nationwide stride)", flush=True)
    t0 = time.time()

    def one(rec):
        lon, lat, tv = rec
        c = crop_window(lon, lat)
        if c is None: return None
        h = measure(c)
        if h is None: return None
        return dict(px=h, ground_m=h * mpp(lat), paper_mm=h * mpp(lat) / 10.56, lat=lat)
    res = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(one, sample)):
            if r: res.append(r)
            if i % 100 == 0: print(f"  {i}/{len(sample)} measured={len(res)} ({time.time()-t0:.0f}s)", flush=True)
    if not res:
        print("no measurements"); return
    px = np.array([r["px"] for r in res]); gm = np.array([r["ground_m"] for r in res]); mm = np.array([r["paper_mm"] for r in res])
    def stats(v, u):
        q1, md, q3 = np.percentile(v, [25, 50, 75]); return f"{u}: median={md:.2f}  IQR=[{q1:.2f},{q3:.2f}]  mean={v.mean():.2f}  std={v.std():.2f}  N={len(v)}"
    print("\n=== B.M. cap-height on z17 ===")
    print(stats(px, "z17 px      "))
    print(stats(gm, "ground m    "))
    print(stats(mm, "paper mm    "))
    print(f"relative spread (IQR/median) px: {(np.percentile(px,75)-np.percentile(px,25))/np.median(px)*100:.0f}%")
    json.dump([{k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()} for r in res],
              open("/vast/ishi/gb1900/edition/bm_capheights.json", "w"))
    print("wrote bm_capheights.json")

if __name__ == "__main__":
    main()
