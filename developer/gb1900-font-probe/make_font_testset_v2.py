"""Phase C (c, redo) — HITL font test-set from MapReader SPOTTER boxes (clean whole words, not fragments).

Replaces make_font_testset.py, which cropped the fragmenting discovery boxes (34/93 came out unclear
because the crop didn't match the text). Here each crop is a MapReader word-box: whole, correctly
recognised, and DE-ROTATED to horizontal via its polygon (so rotated river/relief labels are legible).
Stratified sampling (antiquity-/water-word dense) only ensures italic/blackletter coverage — the FONT
label comes from the reviewer. Export -> font_testset_v2_decisions.json = the real validation set.

    /vast/ishi/envs/boundary/bin/python make_font_testset_v2.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, re, glob, json, base64, random, io, math, numpy as np, cv2
import concurrent.futures as cf
from collections import Counter
from PIL import Image
from make_font_testset import HTML

SPOT = "/vast/ishi/gb1900/edition/spot"
# tile cache: default is the live grind's dir, but font_classify sets FCTILES to an isolated cache and relies
# on fetch-on-miss (grind --cleanup deletes tiles, and GPU/pitt have network so glyph tiles are re-fetchable)
TILES = os.environ.get("FCTILES") or "/vast/ishi/gb1900/tiles17"
_S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
_FETCH = bool(os.environ.get("FCTILES"))          # only the isolated backfill cache fetches on miss
IX1 = "/ix1/ishi/gb1900/tiles17"                  # archived tiles (moved here after spotting)
import io, urllib.request

def _read(p):
    try: return np.asarray(Image.open(p).convert("L"), np.uint8)
    except Exception: return None

def _get_tile(tx, ty):
    p = f"{TILES}/{tx}/{ty}.png"
    if os.path.exists(p): return _read(p)         # hot /vast cache
    q = f"{IX1}/{tx}/{ty}.png"
    if os.path.exists(q): return _read(q)         # /ix1 archive (cheaper than re-hitting NLS)
    if not _FETCH: return None                    # inline mode: tiles are hot, a miss means genuinely absent
    try:
        os.makedirs(f"{TILES}/{tx}", exist_ok=True)
        data = urllib.request.urlopen(urllib.request.Request(_S3.format(x=tx, y=ty),
                                      headers={"User-Agent": "whg-fc"}), timeout=30).read()
        if len(data) > 400:
            open(p, "wb").write(data); return np.asarray(Image.open(io.BytesIO(data)).convert("L"), np.uint8)
    except Exception: pass
    return None
OUT = f"{SPOT}/font_testset_v2.html"; random.seed(42)
ANTIQ = re.compile(r"(Tumul|Cairn|Camp|Barrow|Earthwork|Enclosure|Castle|Moat|Priory|Abbey|Fort|Stone|Site|Cross|Roman|Tower)", re.I)
WATER = re.compile(r"(River|Brook|Burn|Beck|Nant|Afon|Stream|Canal|Well|Pool|Mere|Lake|Ford|Water)", re.I)
N_ANTIQ, N_WATER, N_RAND = 75, 75, 90

def load():
    out = []
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(f):
            r = json.loads(line)
            if len([c for c in r["text"] if c.isalnum()]) >= 3 and r["score"] >= 0.55: out.append(r)
    return out

def assemble(bbox, pad):
    x0, y0, x1, y1 = bbox; x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    tx0, tx1, ty0, ty1 = x0 // 256, x1 // 256, y0 // 256, y1 // 256
    cv = np.full(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), 255, np.uint8); ok = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = _get_tile(tx, ty)
            if t is not None:
                cv[(ty - ty0) * 256:(ty - ty0) * 256 + 256, (tx - tx0) * 256:(tx - tx0) * 256 + 256] = t; ok = True
    if not ok: return None, 0, 0
    return cv, x0, y0

def derotate(r):
    """crop the word-box and rotate to horizontal using its polygon; fallback to axis-aligned bbox."""
    poly = np.array(r["gpoly"], np.float32)
    xs, ys = poly[:, 0], poly[:, 1]
    bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    canvas, ox, oy = assemble(bbox, pad=10)
    if canvas is None: return None
    local = poly - [ox, oy]
    try:
        (cx, cy), (w, h), ang = cv2.minAreaRect(local)
        if w < h: ang += 90; w, h = h, w
        if w < 6 or h < 6: return None
        M = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
        rot = cv2.warpAffine(canvas, M, (canvas.shape[1], canvas.shape[0]), borderValue=255)
        patch = cv2.getRectSubPix(rot, (int(w) + 8, int(h) + 8), (cx, cy))
        return patch
    except Exception:
        return canvas

def stratified(boxes):
    antiq = [r for r in boxes if ANTIQ.search(r["text"])]
    water = [r for r in boxes if WATER.search(r["text"])]
    picked = {}
    for pool, n in [(antiq, N_ANTIQ), (water, N_WATER)]:
        random.shuffle(pool)
        for r in pool[:n]: picked[(r["gcx"], r["gcy"])] = r
    rest = [r for r in boxes if (r["gcx"], r["gcy"]) not in picked]
    random.shuffle(rest)
    for r in rest[:N_RAND]: picked[(r["gcx"], r["gcy"])] = r
    out = list(picked.values()); random.shuffle(out); return out

def make_crop(r):
    patch = derotate(r)
    if patch is None or patch.size < 80: return None
    im = Image.fromarray(patch).convert("L")
    th = 220                                       # large — letterform must be very clearly readable
    sc = th / max(1, im.height)
    im = im.resize((max(1, int(im.width * sc)), th), Image.LANCZOS)
    if im.width > 2000: im = im.resize((2000, th), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=90)
    return dict(text=r["text"], img=base64.b64encode(buf.getvalue()).decode())

def main():
    boxes = load(); print(f"boxes (score>=.55, >=3 chars): {len(boxes)}", flush=True)
    samp = stratified(boxes); print(f"sampled: {len(samp)}", flush=True)
    crops = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for c in ex.map(make_crop, samp):
            if c: crops.append(c)
    print(f"cropped: {len(crops)}", flush=True)
    html = (HTML.replace("data:image/png", "data:image/jpeg")
                .replace("minmax(230px,1fr)", "minmax(560px,1fr)")
                .replace("min-height:70px", "min-height:250px")
                .replace(".imgwrap img{{image-rendering:auto;max-width:100%}}", ".imgwrap img{{image-rendering:auto}}"))
    open(OUT, "w").write(html.format(crops=json.dumps(crops)))
    print(f"wrote {OUT} ({os.path.getsize(OUT)//1024} KB)", flush=True)

if __name__ == "__main__":
    main()
