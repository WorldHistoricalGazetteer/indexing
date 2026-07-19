"""Sheet-wide DISCOVERY localizer (pure OpenCV). Assemble a whole sheet-sized area of z17 tiles, find every
character-sized connected component, group them into text LINES across the ENTIRE image (so a label is
never split at a tile edge — labels don't cross sheet boundaries), measure each line's CAP-HEIGHT, and
match line-centres against GB1900 crowd points. Outputs:
  - the cap-height DISTRIBUTION of every label on the sheet (is size a real multi-rung ladder or one band?),
  - matched (crowd-known) vs unmatched (crowd-MISSED, i.e. discoveries),
  - a downscaled visualization (green=matched, red=discovered).
This one transparent tool serves size-calibration (Phase B), typing crops (Phase C), and missed-text spotting.

    python discover_sheet.py --lon -2.755 --lat 52.707 --r 10 --workers 48
"""
import argparse, os, io, math, json, time, urllib.request, numpy as np, cv2
import concurrent.futures as cf
from collections import Counter
from PIL import Image, ImageDraw

NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
TILES = "/vast/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
OUT = "/vast/ishi/gb1900/edition/discover"; os.makedirs(OUT, exist_ok=True)
N17 = 2 ** 17

def lonlat_to_px(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y
def px_to_lonlat(px, py):
    lon = px / (N17 * 256) * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / (N17 * 256)))))
    return lon, lat
def mpp(lat): return 156543.03392 * math.cos(math.radians(lat)) / N17

def fetch_tile(t):
    tx, ty = t; p = f"{TILES}/{tx}/{ty}.png"
    if os.path.exists(p) and os.path.getsize(p) > 500: return t, None
    os.makedirs(f"{TILES}/{tx}", exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-disc"}), timeout=30) as r:
            data = r.read()
        if len(data) > 400: open(p, "wb").write(data)
    except Exception: pass
    return t, None

def find_chars(gray):
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lbl, st, cen = cv2.connectedComponentsWithStats(ink, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if h < 6 or h > 95: continue
        if w > 4.5 * h and w > 45: continue
        fill = area / max(1, w * h)
        if area < 12 or fill > 0.88: continue            # drop specks + near-solid blocks (buildings/symbols)
        out.append(dict(x=int(x), y=int(y), w=int(w), h=int(h), cx=x + w / 2., cy=y + h / 2., top=int(y), bot=int(y + h)))
    return out

def group_lines(chars):
    """Group ALL chars into same-line contiguous runs (no seed). Each run = one label line."""
    runs = []; used = [False] * len(chars)
    order = sorted(range(len(chars)), key=lambda i: (chars[i]["cy"], chars[i]["cx"]))
    idx = {id(chars[i]): i for i in range(len(chars))}
    for i0 in order:
        if used[i0]: continue
        seed = chars[i0]
        band = sorted([j for j in range(len(chars)) if not used[j] and abs(chars[j]["cy"] - seed["cy"]) <= 0.6 * max(seed["h"], 10)],
                      key=lambda j: chars[j]["x"])
        pos = band.index(i0); run = [i0]
        for j in band[pos + 1:]:
            if chars[j]["x"] - (chars[run[-1]]["x"] + chars[run[-1]]["w"]) <= 1.7 * max(chars[run[-1]]["h"], chars[j]["h"]): run.append(j)
            else: break
        for j in reversed(band[:pos]):
            if chars[run[0]]["x"] - (chars[j]["x"] + chars[j]["w"]) <= 1.7 * max(chars[run[0]]["h"], chars[j]["h"]): run.insert(0, j)
            else: break
        for j in run: used[j] = True
        runs.append([chars[j] for j in run])
    return runs

def cap_height(run):
    # drop mis-grouped outlier chars (a tall symbol / merged blob) so one bad component can't inflate it
    hs = np.array([c["h"] for c in run], float); med = np.median(hs)
    core = [c for c in run if 0.55 * med <= c["h"] <= 1.8 * med] or run
    bots = [c["bot"] for c in core]; tops = [c["top"] for c in core]
    cnt = Counter(round(b / 2) * 2 for b in bots)
    base = max(cnt, key=lambda k: (cnt[k], k)); base = max(base, int(np.percentile(bots, 60)))
    return base - min(tops), base

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lon", type=float, required=True); ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--r", type=int, default=10, help="tile radius (z17); full side = 2r+1 tiles")
    ap.add_argument("--workers", type=int, default=48)
    a = ap.parse_args(); t0 = time.time()
    cxp, cyp = lonlat_to_px(a.lon, a.lat); ctx, cty = int(cxp // 256), int(cyp // 256)
    txs = range(ctx - a.r, ctx + a.r + 1); tys = range(cty - a.r, cty + a.r + 1)
    need = [(tx, ty) for tx in txs for ty in tys]
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex: list(ex.map(fetch_tile, need))
    W = len(txs) * 256; H = len(tys) * 256; ox, oy = min(txs) * 256, min(tys) * 256
    canvas = np.full((H, W), 255, np.uint8)
    for tx in txs:
        for ty in tys:
            p = f"{TILES}/{tx}/{ty}.png"
            if os.path.exists(p):
                try: canvas[(ty - min(tys)) * 256:(ty - min(tys)) * 256 + 256, (tx - min(txs)) * 256:(tx - min(txs)) * 256 + 256] = np.asarray(Image.open(p).convert("L"), np.uint8)
                except Exception: pass
    print(f"sheet {W}x{H}px ({len(need)} tiles, {time.time()-t0:.0f}s); detecting...", flush=True)

    chars = find_chars(canvas)
    runs = group_lines(chars)
    # keep TEXT-LIKE runs: >=2 chars of consistent height, and NOT the over-regular signature of hatching
    kept = []
    for run in runs:
        if len(run) < 2: continue
        hs = np.array([c["h"] for c in run], float); ws = np.array([c["w"] for c in run], float)
        if hs.std() / max(1., hs.mean()) > 0.45: continue          # inconsistent height = not text
        if len(run) >= 4:
            gaps = np.array([run[i + 1]["x"] - (run[i]["x"] + run[i]["w"]) for i in range(len(run) - 1)], float)
            wcv = ws.std() / max(1., ws.mean()); gcv = gaps.std() / (abs(gaps.mean()) + 1.)
            if wcv < 0.17 and gcv < 0.45: continue                 # too regular width+spacing = hatching, not text
        kept.append(run)
    print(f"chars={len(chars)} runs={len(runs)} text-like-runs={len(kept)}", flush=True)
    runs = kept
    labels = []
    for run in runs:
        if len(run) < 1: continue
        ch, base = cap_height(run)
        x0 = min(c["x"] for c in run); x1 = max(c["x"] + c["w"] for c in run); y0 = min(c["top"] for c in run)
        gcx = (x0 + x1) / 2 + ox; gcy = base + oy
        lon, lat = px_to_lonlat(gcx, gcy)
        labels.append(dict(box=(x0, y0, x1, base), box_g=[int(x0 + ox), int(y0 + oy), int(x1 + ox), int(base + oy)],
                           nchar=len(run), caph=ch, ground_m=ch * mpp(lat), lon=round(lon, 6), lat=round(lat, 6)))
    print(f"chars={len(chars)} label-lines={len(labels)} ({time.time()-t0:.0f}s)", flush=True)

    # match to crowd points in the bbox
    w0, s0 = px_to_lonlat(ox, oy + H); e0, n0 = px_to_lonlat(ox + W, oy)
    crowd = []
    for line in open(NT):
        try: d = json.loads(line)
        except Exception: continue
        lo, la = d.get("lon"), d.get("lat")
        if lo is None or la is None or not (w0 <= lo <= e0 and s0 <= la <= n0): continue
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        crowd.append((lo, la, tv))
    cpx = [(((lo + 180) / 360 * N17 * 256) - ox, ((1 - math.log(math.tan(math.radians(la)) + 1 / math.cos(math.radians(la))) / math.pi) / 2 * N17 * 256) - oy, tv) for lo, la, tv in crowd]
    matched = 0
    for L in labels:
        bx = (L["box"][0] + L["box"][2]) / 2; by = (L["box"][1] + L["box"][3]) / 2
        best = min(((math.hypot(px - bx, py - by), tv) for px, py, tv in cpx), default=(1e9, None))
        L["crowd"] = best[1] if best[0] <= 55 else None
        if L["crowd"] is not None: matched += 1

    # report + histogram
    caps = np.array([L["caph"] for L in labels]); gm = np.array([L["ground_m"] for L in labels])
    print(f"\n=== sheet discovery ===")
    print(f"labels={len(labels)}  matched-to-crowd={matched} ({matched/max(1,len(labels))*100:.0f}%)  "
          f"discovered(crowd-missed)={len(labels)-matched}  crowd-points-in-bbox={len(crowd)}")
    print(f"cap-height px: median={np.median(caps):.1f} p10={np.percentile(caps,10):.0f} p90={np.percentile(caps,90):.0f} max={caps.max()}")
    print("cap-height histogram (px bins):")
    hist, edges = np.histogram(caps, bins=[0, 12, 16, 20, 24, 28, 34, 42, 55, 200])
    for k in range(len(hist)): print(f"   {int(edges[k]):3d}-{int(edges[k+1]):3d}px: {'#'*int(hist[k]*60/max(hist))} {hist[k]}")
    json.dump([{k: v for k, v in L.items() if k != 'box'} for L in labels], open(f"{OUT}/labels_{ctx}_{cty}.json", "w"))

    # visualization (downscale)
    vis = cv2.cvtColor(canvas, cv2.COLOR_GRAY2RGB)
    for L in labels:
        x0, y0, x1, y1 = L["box"]; col = (0, 170, 0) if L["crowd"] else (230, 0, 0)
        cv2.rectangle(vis, (x0, y0), (x1, y1), col, 2)
    sc = 2000 / max(W, H)
    Image.fromarray(cv2.resize(vis, (int(W * sc), int(H * sc)))).save(f"{OUT}/sheet_{ctx}_{cty}.png")
    print(f"wrote sheet_{ctx}_{cty}.png (green=matched, red=discovered) + labels json", flush=True)

if __name__ == "__main__":
    main()
