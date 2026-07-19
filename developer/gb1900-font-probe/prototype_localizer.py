"""Prototype 'crowd-guided localizer' (candidate replacement for MapReader, for GB-STAMP's needs).

We already have, per label, the crowd TEXT + an approximate POINT. So we don't need spotting — only to
delineate the KNOWN label's box near the KNOWN point. This prototype does that with stable, pip-installable
tools (Tesseract word detection + a connected-component fallback), on z17 tiles, and VALIDATES across a
stratified size range (large admin, medium settlement, small B.M.). It reports cap-height per category
(median/IQR) and saves a boxed visualization so a human can judge localization quality.

    python prototype_localizer.py --per 60 --workers 40
"""
import argparse, os, io, math, json, re, time, urllib.request, numpy as np, cv2
import concurrent.futures as cf
from collections import defaultdict
from PIL import Image, ImageDraw

NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
TILES = "/vast/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
ADMIN = set(x.lower() for x in json.load(open("/vast/ishi/gb1900/probe/font/admin_names.json")).get("names", [])) \
    if os.path.exists("/vast/ishi/gb1900/probe/font/admin_names.json") else set()
COUNTIES = set(c.lower() for c in ["Cheshire", "Shropshire", "Herefordshire", "Montgomeryshire", "Staffordshire",
    "Worcestershire", "Denbighshire", "Merionethshire", "Radnorshire", "Flintshire", "Lancashire", "Yorkshire",
    "Derbyshire", "Warwickshire", "Cornwall", "Devonshire", "Somerset", "Norfolk", "Suffolk", "Cumberland"])
BM = re.compile(r"^\s*B[. ]?M[. ]\s*\d")
N17 = 2 ** 17; OUT = "/vast/ishi/gb1900/edition/localizer_test"; os.makedirs(OUT, exist_ok=True)

def px17(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y
def mpp(lat): return 156543.03392 * math.cos(math.radians(lat)) / N17

_c = {}
def tile(tx, ty):
    k = (tx, ty)
    if k in _c: return _c[k]
    p = f"{TILES}/{tx}/{ty}.png"; im = None
    if os.path.exists(p) and os.path.getsize(p) > 500:
        try: im = np.asarray(Image.open(p).convert("L"), np.uint8)
        except Exception: im = None
    if im is None:
        try:
            with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-loc"}), timeout=30) as r:
                data = r.read()
            if len(data) > 400:
                os.makedirs(f"{TILES}/{tx}", exist_ok=True); open(p, "wb").write(data)
                im = np.asarray(Image.open(io.BytesIO(data)).convert("L"), np.uint8)
        except Exception: im = None
    if len(_c) < 6000: _c[k] = im
    return im

def window(lon, lat, W=460, H=170):
    xp, yp = px17(lon, lat); x0, y0 = int(xp - W / 2), int(yp - H / 2)
    tx0, tx1, ty0, ty1 = x0 // 256, (x0 + W) // 256, y0 // 256, (y0 + H) // 256
    canvas = np.full(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), 255, np.uint8); ok = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = tile(tx, ty)
            if t is not None: canvas[(ty - ty0) * 256:(ty - ty0) * 256 + t.shape[0], (tx - tx0) * 256:(tx - tx0) * 256 + t.shape[1]] = t; ok = True
    if not ok: return None, None
    L, U = x0 - tx0 * 256, y0 - ty0 * 256
    return canvas[U:U + H, L:L + W], (W / 2, H / 2)

def localize(crop, cx, cy, text):
    """Pure-CV crowd-guided localizer. Otsu ink -> character-sized connected components -> the run of same-
    line characters through the crowd point (within-word gaps) = the label box. The crowd POINT anchors it;
    the crowd TEXT length sanity-checks it. Returns (x0,y0,x1,y1) or None. No OCR, no external binaries."""
    _, ink = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lbl, st, cen = cv2.connectedComponentsWithStats(ink, 8)
    chars = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if h < 5 or h > 95: continue                      # plausible glyph height at z17
        if w > 4.5 * h and w > 45: continue               # drop long thin runs (contours/rules/roads)
        if area < 8 or area > 6 * w * h: continue
        chars.append([x, y, w, h, x + w / 2.0, y + h / 2.0])
    if not chars: return None
    seed = min(chars, key=lambda c: (c[4] - cx) ** 2 + (c[5] - cy) ** 2)
    if math.hypot(seed[4] - cx, seed[5] - cy) > 75: return None   # no glyph near the crowd point
    band = [c for c in sorted(chars, key=lambda c: c[4]) if abs(c[5] - seed[5]) <= 0.7 * max(seed[3], 10)]
    # grow a contiguous run around the seed: neighbours whose gap < ~1.7x local glyph height
    si = band.index(seed); run = [seed]
    for c in band[si + 1:]:
        if c[0] - (run[-1][0] + run[-1][2]) <= 1.7 * max(run[-1][3], c[3]): run.append(c)
        else: break
    for c in reversed(band[:si]):
        if run[0][0] - (c[0] + c[2]) <= 1.7 * max(run[0][3], c[3]): run.insert(0, c)
        else: break
    x0 = min(c[0] for c in run); y0 = min(c[1] for c in run)
    x1 = max(c[0] + c[2] for c in run); y1 = max(c[1] + c[3] for c in run)
    # length sanity: run char-count should be within 2.5x of the crowd text's alnum count
    nalnum = sum(ch.isalnum() for ch in text)
    if nalnum >= 2 and not (0.35 * nalnum <= len(run) <= 3.0 * nalnum + 2): return None
    return (int(x0), int(y0), int(x1), int(y1))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--per", type=int, default=60); ap.add_argument("--workers", type=int, default=40)
    a = ap.parse_args()
    buckets = defaultdict(list)
    for line in open(NT):
        try: d = json.loads(line)
        except Exception: continue
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        lon, lat = d.get("lon"), d.get("lat")
        if not (tv and lon and lat): continue
        tvs = tv.strip(); low = tvs.lower()
        cat = None
        if BM.match(tvs): cat = "bm"
        elif low in COUNTIES: cat = "county"
        elif " " not in tvs and low in ADMIN and len(tvs) >= 4: cat = "admin"
        elif tvs != low and " " not in tvs and len(tvs) >= 5: cat = "settlement"
        if cat and len(buckets[cat]) < a.per * 3:
            buckets[cat].append((lon, lat, tvs))
    sample = [(cat, r) for cat, rs in buckets.items() for r in rs[::max(1, len(rs) // a.per)][:a.per]]
    print("sampled:", {k: min(len(v), a.per) for k, v in buckets.items()}, flush=True)
    t0 = time.time(); results = defaultdict(list); vis = defaultdict(list)

    def one(item):
        cat, (lon, lat, tv) = item
        crop, ctr = window(lon, lat)
        if crop is None: return None
        box = localize(crop, ctr[0], ctr[1], tv)
        if box is None: return (cat, None, None, None)
        ch_px = box[3] - box[1]                     # box height (px) ~ text height; cap-height refinement later
        return (cat, ch_px, ch_px * mpp(lat), (crop, box, tv))
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(one, sample)):
            if r is None: continue
            cat, ch_px, gm, v = r
            if ch_px is None: results[cat + "_MISS"].append(1); continue
            results[cat].append((ch_px, gm))
            if len(vis[cat]) < 6 and v: vis[cat].append(v)
            if i % 100 == 0: print(f"  {i}/{len(sample)} ({time.time()-t0:.0f}s)", flush=True)

    print("\n=== localizer validation (z17) ===")
    for cat in ["county", "admin", "settlement", "bm"]:
        vals = results.get(cat, []); miss = len(results.get(cat + "_MISS", []))
        if not vals: print(f"  {cat:11s} localized=0 miss={miss}"); continue
        px = np.array([v[0] for v in vals]); gm = np.array([v[1] for v in vals])
        rel = (np.percentile(px, 75) - np.percentile(px, 25)) / max(1, np.median(px)) * 100
        hit = len(vals); print(f"  {cat:11s} localized={hit} miss={miss} ({hit/(hit+miss)*100:.0f}% hit)  "
              f"box-h px median={np.median(px):.1f} IQR=[{np.percentile(px,25):.0f},{np.percentile(px,75):.0f}] rel={rel:.0f}%  "
              f"ground-m median={np.median(gm):.2f}")
    # visualization montages
    for cat, items in vis.items():
        if not items: continue
        cells = []
        for crop, box, tv in items:
            im = Image.fromarray(crop).convert("RGB"); dr = ImageDraw.Draw(im)
            dr.rectangle(box, outline=(230, 0, 0), width=2); cells.append(im)
        wmax = max(c.width for c in cells); H = sum(c.height + 4 for c in cells)
        sheet = Image.new("RGB", (wmax, H), (245, 243, 238)); y = 0
        for c in cells: sheet.paste(c, (0, y)); y += c.height + 4
        sheet.save(f"{OUT}/vis_{cat}.png")
    print(f"wrote visualizations -> {OUT}/vis_*.png", flush=True)

if __name__ == "__main__":
    main()
