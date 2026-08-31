"""GB-STAMP (b) HARVESTER — produce a vetted set of gazetteer-confirmed admin-font labels as training seed.

Refines detect_admin_v3 with the fixes the yield scan called for:
  - ROAD-GENERIC stoplist: drop matches whose gazetteer name / run text is a road word (STREET/ROAD/...),
    which otherwise false-match the Somerset hundred 'Street' etc.
  - CAP-HEIGHT gate: admin labels are large; keep runs >= --minh (above the ~31px ordinary-text median).
  - MULTI-FRAGMENT flag: nfrag>=2 = a genuinely letter-spaced recovery (what MapReader misses); nfrag==1 = a
    large single-word label MapReader already caught that matches an admin name (still a valid large example).
  - cross-region DEDUP (same physical label in overlapping mosaics) by (name, rounded global px).
  - CROPS + montage.html grouped by face, so a human can verify the harvest is admin-font, not settlement noise.

    /vast/ishi/envs/boundary/bin/python detect_admin_harvest.py --minh 55
Outputs: /vast/ishi/gb1900/edition/admin/harvest.jsonl + harvest_montage.html
"""
import argparse, os, io, math, time, json, glob, difflib, base64
from collections import defaultdict
import numpy as np
from PIL import Image

HERE = "/vast/ishi/gb1900/probe/font"; SPOT = "/vast/ishi/gb1900/edition/spot"
OUT = "/vast/ishi/gb1900/edition/admin"; os.makedirs(OUT, exist_ok=True)
N17 = 2 ** 17; TILES = "/vast/ishi/gb1900/tiles17"; IX1 = "/ix1/ishi/gb1900/tiles17"
import urllib.request
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
ROAD = {"STREET", "ROAD", "LANE", "AVENUE", "TERRACE", "CRESCENT", "PARADE", "ROW", "PLACE", "SQUARE",
        "GARDENS", "WHARF", "QUAY", "COURT", "WALK", "GROVE", "DRIVE", "CLOSE", "BUILDINGS", "COTTAGES"}

def norm(s): return "".join(c for c in (s or "") if c.isalnum()).upper()

def get_tile(tx, ty):
    for base in (TILES, IX1):
        p = f"{base}/{tx}/{ty}.png"
        if os.path.exists(p) and os.path.getsize(p) > 500:
            try: return np.asarray(Image.open(p).convert("L"), np.uint8)
            except Exception: pass
    os.makedirs(f"{TILES}/{tx}", exist_ok=True)
    for k in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-admin"}), timeout=30) as r:
                d = r.read()
            if len(d) > 400:
                open(f"{TILES}/{tx}/{ty}.png", "wb").write(d); return np.asarray(Image.open(io.BytesIO(d)).convert("L"), np.uint8)
            return None
        except Exception as e:
            if getattr(e, "code", None) in (403, 404): return None
            time.sleep(1.0 * (k + 1))
    return None

def is_allcaps(t):
    a = [c for c in (t or "") if c.isalpha()]
    return bool(a) and all(c.isupper() for c in a)

def frag(box):
    g = box.get("gpoly")
    if not g: return None
    t = box.get("text") or ""
    if not is_allcaps(t): return None                          # admin faces are ALL-CAPS; drop title-case settlement/descriptive labels
    xs = [p[0] for p in g]; ys = [p[1] for p in g]
    return (min(xs), min(ys), max(xs), max(ys), norm(t))

def group_runs(frags):
    n = len(frags); parent = list(range(n))
    cx = [(f[0] + f[2]) / 2 for f in frags]; cy = [(f[1] + f[3]) / 2 for f in frags]
    h = [f[3] - f[1] for f in frags]; w = [f[2] - f[0] for f in frags]
    def find(a):
        while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for i in range(n):
        for j in range(i + 1, n):
            if not (0.65 <= h[i] / h[j] <= 1.54): continue
            mh = (h[i] + h[j]) / 2
            if abs(cy[i] - cy[j]) > 0.5 * mh: continue
            if not (-0.3 * mh <= abs(cx[i] - cx[j]) - (w[i] + w[j]) / 2 <= 3.5 * mh): continue
            parent[find(i)] = find(j)
    g = defaultdict(list)
    for i in range(n): g[find(i)].append(i)
    runs = []
    for idxs in g.values():
        idxs.sort(key=lambda i: cx[i])
        text = "".join(frags[i][4] for i in idxs)
        if len(text) < 5: continue
        x0 = min(frags[i][0] for i in idxs); y0 = min(frags[i][1] for i in idxs)
        x1 = max(frags[i][2] for i in idxs); y1 = max(frags[i][3] for i in idxs)
        mh = sorted(h[i] for i in idxs)[len(idxs) // 2]
        runs.append((text, mh, len(idxs), x0, y0, x1, y1))
    return runs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minh", type=float, default=55.0); ap.add_argument("--score", type=float, default=0.5)
    ap.add_argument("--ratio", type=float, default=0.85); ap.add_argument("--montage", type=int, default=500)
    ap.add_argument("--r", type=int, default=8)
    a = ap.parse_args()
    vob = json.load(open(f"{HERE}/labels/vob_admin_names.json"))
    name2face = defaultdict(set)
    for face, names in vob.items():
        for nm in names:
            k = norm(nm)
            if len(k) >= 6 and k not in ROAD: name2face[k].add(face)   # drop road-generic gazetteer names
    by_len = defaultdict(list)
    for k in name2face: by_len[len(k)].append(k)
    print(f"gazetteer: {len(name2face)} admin names (>=6 chars, road-generics dropped)", flush=True)

    hits = []
    for bf in glob.glob(f"{SPOT}/boxes_gb_*.jsonl"):
        tag = os.path.basename(bf)[6:-6]
        rows = [json.loads(l) for l in open(bf)]
        frags = [f for f in (frag(r) for r in rows if r.get("score", 0) >= a.score) if f and (f[3] - f[1]) >= a.minh]
        if not frags: continue
        for text, mh, nf, x0, y0, x1, y1 in group_runs(frags):
            if text in ROAD: continue
            if (x1 - x0) < 1.4 * (y1 - y0): continue           # horizontal label; rejects tall-thin rotated slivers
            best, bestr, bestf = None, 0.0, None
            for L in range(max(6, int(len(text) * 0.6)), int(len(text) * 1.15) + 1):
                for cand in by_len.get(L, []):
                    r = difflib.SequenceMatcher(None, text, cand).ratio()
                    if r > bestr: bestr, best, bestf = r, cand, name2face[cand]
            if bestr >= a.ratio and best not in ROAD and len(text) >= 0.7 * len(best):
                hits.append(dict(ratio=round(bestr, 3), name=best, faces=sorted(bestf), text=text,
                                 cap_h=int(mh), nfrag=nf, tag=tag, x0=x0, y0=y0, x1=x1, y1=y1,
                                 gcx=int((x0 + x1) / 2), gcy=int((y0 + y1) / 2)))
    # dedup by (name, rounded global px) — same label across overlapping mosaics; keep best ratio then largest
    dd = {}
    for hcur in sorted(hits, key=lambda d: (-d["ratio"], -d["cap_h"])):
        key = (hcur["name"], hcur["gcx"] // 200, hcur["gcy"] // 200)
        if key not in dd: dd[key] = hcur
    hits = list(dd.values())
    with open(f"{OUT}/harvest.jsonl", "w") as fo:
        for h in hits: fo.write(json.dumps(h) + "\n")
    byface = defaultdict(int); multi = sum(1 for h in hits if h["nfrag"] >= 2)
    for h in hits:
        for f in h["faces"]: byface[f] += 1
    print(f"HARVEST: {len(hits)} confirmed admin labels ({multi} multi-fragment / letter-spaced); by face {dict(byface)}", flush=True)

    # crops + montage: top-N by (multi-fragment, cap-h, ratio); assemble each needed region mosaic once
    pick = sorted(hits, key=lambda h: (h["nfrag"] >= 2, h["cap_h"], h["ratio"]), reverse=True)[:a.montage]
    bytag = defaultdict(list)
    for h in pick: bytag[h["tag"]].append(h)
    cropf = {}
    for tag, hs in bytag.items():
        p = hs[0]  # need region center to place mosaic; recover from tile math via any box? use gcx/gcy -> tile
        # mosaic covers tiles centred on the region centre; derive from the label global px is enough to crop
        for h in hs:
            x0, y0, x1, y1 = int(h["x0"]), int(h["y0"]), int(h["x1"]), int(h["y1"])
            tx0 = x0 // 256 - 1; ty0 = y0 // 256 - 1; tx1 = x1 // 256 + 1; ty1 = y1 // 256 + 1
            canvas = np.full(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), 255, np.uint8)
            for i in range(tx1 - tx0 + 1):
                for j in range(ty1 - ty0 + 1):
                    t = get_tile(tx0 + i, ty0 + j)
                    if t is not None: canvas[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = t
            cx0, cy0 = tx0 * 256, ty0 * 256
            pad = 8
            sub = canvas[max(0, y0 - cy0 - pad):y1 - cy0 + pad, max(0, x0 - cx0 - pad):x1 - cx0 + pad]
            if sub.size == 0: continue
            im = Image.fromarray(sub); H = 70
            im = im.resize((max(1, int(im.width * H / max(1, im.height))), H))
            bio = io.BytesIO(); im.save(bio, "PNG"); cropf[id(h)] = base64.b64encode(bio.getvalue()).decode()
    # montage grouped by (primary) face
    face_groups = defaultdict(list)
    for h in pick:
        if id(h) in cropf: face_groups[h["faces"][0]].append(h)
    html = ["<!doctype html><meta charset=utf-8><title>GB-STAMP admin harvest</title><style>",
            "body{font-family:system-ui;margin:14px;background:#f6f3ec}h2{margin:16px 0 4px}",
            ".s{display:inline-block;border:1px solid #ccc;margin:4px;padding:4px;background:#fff;text-align:center;vertical-align:top}",
            ".s img{display:block;height:70px}.c{font-size:11px;color:#333;max-width:240px}.m{color:#a5322e}</style>"]
    html.append(f"<h1>Admin-label harvest — {len(hits)} confirmed ({multi} letter-spaced), showing top {len(pick)}</h1>")
    for face in sorted(face_groups, key=lambda f: -len(face_groups[f])):
        hs = face_groups[face]
        html.append(f"<h2>{face} — {len(hs)}</h2>")
        for h in hs:
            tagm = " <span class=m>[LS]</span>" if h["nfrag"] >= 2 else ""
            html.append(f'<div class=s><img src="data:image/png;base64,{cropf[id(h)]}">'
                        f'<div class=c>{h["name"]} h{h["cap_h"]} r{h["ratio"]}{tagm}<br>&lt;{h["text"][:24]}&gt;</div></div>')
    open(f"{OUT}/harvest_montage.html", "w").write("".join(html))
    print(f"wrote {OUT}/harvest.jsonl and harvest_montage.html ({len(pick)} crops)", flush=True)

if __name__ == "__main__":
    main()
