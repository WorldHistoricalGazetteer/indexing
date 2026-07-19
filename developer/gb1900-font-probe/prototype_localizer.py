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

from collections import Counter
def find_chars(crop):
    """Otsu ink -> character-sized connected components (drop contours/rules/specks)."""
    _, ink = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lbl, st, cen = cv2.connectedComponentsWithStats(ink, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if h < 5 or h > 95: continue
        if w > 4.5 * h and w > 45: continue               # long thin = contour/rule/road
        if area < 8 or area > 6 * w * h: continue
        out.append(dict(x=int(x), y=int(y), w=int(w), h=int(h), cx=x + w / 2., cy=y + h / 2., top=int(y), bot=int(y + h)))
    return out

def grow_run(seed, chars):
    """Same-line contiguous run of chars around `seed` (within-word/label gaps)."""
    band = sorted([c for c in chars if abs(c["cy"] - seed["cy"]) <= 0.6 * max(seed["h"], 10)], key=lambda c: c["x"])
    if seed not in band: return [seed]
    si = band.index(seed); run = [seed]
    for c in band[si + 1:]:
        if c["x"] - (run[-1]["x"] + run[-1]["w"]) <= 1.7 * max(run[-1]["h"], c["h"]): run.append(c)
        else: break
    for c in reversed(band[:si]):
        if run[0]["x"] - (c["x"] + c["w"]) <= 1.7 * max(run[0]["h"], c["h"]): run.insert(0, c)
        else: break
    return run

def localize(crop, cx, cy, text):
    """Offset-tolerant crowd-guided localizer with a confidence + true CAP-HEIGHT. Considers every char
    within a generous radius as a run seed, scores each candidate run by (proximity to the crowd point,
    length agreement with the crowd text, glyph-height consistency), keeps the best, and returns a
    confidence so callers can REJECT weak hits. Cap-height = baseline (modal char-bottom, excl. descenders)
    -> topmost ink, measured inside the clean box. Returns dict or None."""
    chars = find_chars(crop)
    if not chars: return None
    nalnum = sum(ch.isalnum() for ch in text)
    seeds = [c for c in chars if math.hypot(c["cx"] - cx, c["cy"] - cy) <= 120]   # offset tolerance
    if not seeds: return None
    best, best_cost, seen = None, 1e9, set()
    for s in seeds:
        run = grow_run(s, chars)
        key = (min(c["x"] for c in run), round(run[0]["cy"] / 4))
        if key in seen: continue
        seen.add(key)
        rcx = sum(c["cx"] for c in run) / len(run); rcy = sum(c["cy"] for c in run) / len(run)
        dist = math.hypot(rcx - cx, rcy - cy)
        lenmis = abs(len(run) - nalnum) / max(2, nalnum) if nalnum >= 2 else 0.3
        hs = np.array([c["h"] for c in run], float); textness = hs.std() / max(1., hs.mean())
        cost = dist / 55 + lenmis * 2.0 + textness * 3.0 + (2 if len(run) < 2 else 0)
        if cost < best_cost: best_cost, best = cost, run
    if best is None: return None
    conf = max(0.0, min(1.0, 1.0 - best_cost / 4.5))
    bots = [c["bot"] for c in best]; tops = [c["top"] for c in best]
    cnt = Counter(round(b / 2) * 2 for b in bots)
    base = max(cnt, key=lambda k: (cnt[k], k)); base = max(base, int(np.percentile(bots, 60)))
    caph = base - min(tops)
    x0 = min(c["x"] for c in best); x1 = max(c["x"] + c["w"] for c in best)
    return dict(box=(int(x0), int(min(tops)), int(x1), int(base)), caph=int(caph), conf=round(conf, 2), nchar=len(best))

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

    CONF = 0.45                                        # reject weak hits below this confidence
    def one(item):
        cat, (lon, lat, tv) = item
        crop, ctr = window(lon, lat)
        if crop is None: return None
        r = localize(crop, ctr[0], ctr[1], tv)
        if r is None or r["conf"] < CONF: return (cat, None, None, None)
        return (cat, r["caph"], r["caph"] * mpp(lat), (crop, r["box"], f"{tv}  c={r['conf']}"))
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(one, sample)):
            if r is None: continue
            cat, caph, gm, v = r
            if caph is None: results[cat + "_REJ"].append(1); continue
            results[cat].append((caph, gm))
            if len(vis[cat]) < 6 and v: vis[cat].append(v)
            if i % 100 == 0: print(f"  {i}/{len(sample)} ({time.time()-t0:.0f}s)", flush=True)

    print(f"\n=== localizer validation (z17, cap-height, reject conf<{CONF}) ===")
    for cat in ["county", "admin", "settlement", "bm"]:
        vals = results.get(cat, []); rej = len(results.get(cat + "_REJ", []))
        if not vals: print(f"  {cat:11s} kept=0 rejected={rej}"); continue
        px = np.array([v[0] for v in vals]); gm = np.array([v[1] for v in vals])
        rel = (np.percentile(px, 75) - np.percentile(px, 25)) / max(1, np.median(px)) * 100
        keep = len(vals); print(f"  {cat:11s} kept={keep} rejected={rej} ({keep/(keep+rej)*100:.0f}% keep)  "
              f"cap-h px median={np.median(px):.1f} IQR=[{np.percentile(px,25):.0f},{np.percentile(px,75):.0f}] rel={rel:.0f}%  "
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
