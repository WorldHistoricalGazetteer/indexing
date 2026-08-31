"""GB-STAMP (b) — whole-sheet detector for the big letter-spaced ADMIN labels (county/hundred/parish), which
MapReader's word-spotter misses because the inter-letter gaps are too wide to group. Here that same wide
letter-spacing is the ENABLER: each large cap is an isolated compact connected component, and (per the OS
sheets) these labels are horizontal, so a label is just a horizontal run of same-cap-height, baseline-aligned,
evenly-spaced large caps.

FEASIBILITY PROBE (validate before wiring gazetteer reading): assemble a z17 mosaic, find large connected
components, filter to letter-like blobs in a cap-height band, group into horizontal runs, and DUMP:
  - the component cap-height histogram (to calibrate the admin size bands vs the ~20px ordinary text)
  - an annotated mosaic (candidates boxed, grouped runs lined + captioned) for a human to judge feasibility.

    /vast/ishi/envs/boundary/bin/python detect_admin.py --lon -2.5 --lat 53.4 --tag probe1 --minh 34
"""
import argparse, os, io, math, time, urllib.request
from collections import defaultdict
import numpy as np, cv2
from PIL import Image

N17 = 2 ** 17
TILES = "/vast/ishi/gb1900/tiles17"; IX1 = "/ix1/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
OUT = "/vast/ishi/gb1900/edition/admin"; os.makedirs(OUT, exist_ok=True)

def lonlat_to_px(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y

def get_tile(tx, ty):                                          # /vast cache -> /ix1 archive -> S3 (retry). Grayscale.
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
    canvas = np.full((side * 256, side * 256), 255, np.uint8); miss = 0
    for i in range(side):
        for j in range(side):
            t = get_tile(cx_tile - r + i, cy_tile - r + j)
            if t is not None: canvas[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = t
            else: miss += 1
    return canvas, miss

def candidates(gray, minh, maxh):
    # binarise (map ink is dark); connected components; keep compact letter-like blobs in the cap-height band.
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 51, 15)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(ink, connectivity=8)
    out = []
    for k in range(1, n):
        x, y, w, h, area = stats[k]
        if not (minh <= h <= maxh): continue                  # cap-height band (large; above ~20px ordinary text)
        if not (0.25 <= w / h <= 1.8): continue               # letter aspect (excludes long thin contour lines)
        fill = area / (w * h)
        if not (0.12 <= fill <= 0.75): continue               # letters: moderate fill (excludes lines & solid blobs)
        out.append((cent[k][0], cent[k][1], w, h, x, y))
    return ink, out

def group_runs(cands):
    # horizontal runs: same cap height, baseline-aligned, adjacent (gap up to ~4x height = wide letter-spacing).
    n = len(cands); parent = list(range(n))
    def find(a):
        while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for i in range(n):
        for j in range(i + 1, n):
            (cxi, cyi, _wi, hi, *_), (cxj, cyj, _wj, hj, *_) = cands[i], cands[j]
            if not (0.7 <= hi / hj <= 1.43): continue
            mh = (hi + hj) / 2
            if abs(cyi - cyj) > 0.45 * mh: continue           # horizontal baseline
            if not (0.4 * mh < abs(cxi - cxj) < 4.0 * mh): continue
            parent[find(i)] = find(j)
    g = defaultdict(list)
    for i in range(n): g[find(i)].append(i)
    runs = []
    for idxs in g.values():
        if len(idxs) < 3: continue
        idxs.sort(key=lambda i: cands[i][0])                  # order by x
        # even-spacing check: coefficient of variation of consecutive gaps should be modest
        xs = [cands[i][0] for i in idxs]
        gaps = np.diff(xs)
        if len(gaps) >= 2 and gaps.std() > 0.9 * gaps.mean(): continue
        runs.append(idxs)
    return runs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lon", type=float, required=True); ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--tag", required=True); ap.add_argument("--r", type=int, default=8)
    ap.add_argument("--minh", type=int, default=34); ap.add_argument("--maxh", type=int, default=260)
    a = ap.parse_args()
    cxp, cyp = lonlat_to_px(a.lon, a.lat); ctx, cty = int(cxp // 256), int(cyp // 256)
    gray, miss = assemble(ctx, cty, a.r)
    print(f"{a.tag}: mosaic {gray.shape} miss={miss}", flush=True)
    ink, cands = candidates(gray, a.minh, a.maxh)
    hs = np.array([c[3] for c in cands])
    if len(hs):
        bins = [0, 34, 45, 60, 80, 110, 160, 260]
        hist = np.histogram(hs, bins=bins)[0]
        print(f"{a.tag}: {len(cands)} large-cap candidates; height bins {list(zip(bins[:-1], bins[1:]))} = {hist.tolist()}", flush=True)
    runs = group_runs(cands)
    print(f"{a.tag}: {len(runs)} grouped horizontal runs (>=3 letters, even-spaced)", flush=True)
    # annotate
    vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    for (cx, cy, w, h, x, y) in cands:
        cv2.rectangle(vis, (int(x), int(y)), (int(x + w), int(y + h)), (255, 120, 0), 2)
    for r_i, idxs in enumerate(runs):
        pts = [(int(cands[i][0]), int(cands[i][1])) for i in idxs]
        for k in range(len(pts) - 1): cv2.line(vis, pts[k], pts[k + 1], (0, 0, 255), 3)
        mh = int(np.median([cands[i][3] for i in idxs]))
        cv2.putText(vis, f"{len(idxs)}L h{mh}", (pts[0][0], pts[0][1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
    outp = f"{OUT}/detect_{a.tag}.png"
    Image.fromarray(cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)).save(outp)
    print(f"{a.tag}: wrote {outp}", flush=True)

if __name__ == "__main__":
    main()
