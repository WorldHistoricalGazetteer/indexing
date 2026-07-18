"""Synthetic training batches + real-crop loading for the font-style encoder (iteration 2).

Everything is paper-flattened (flatten.py) so synthetic and real share a canonical paper:
 - synthetic: background tiles are flattened once at load, ink composited on flat paper;
 - real: the tile canvas is flattened before the box is cropped (tile-scale, robust).
Real crops also feed an unsupervised consistency term (lever c) via augmented view pairs.
"""
import os, glob, io, base64, numpy as np
from PIL import Image
from scipy import ndimage as ndi
import fonts as F
import degrade as D
import flatten as FL

# ---------------- synthetic ----------------
def load_bg_pool(tile_dirs, limit=400, rng=None):
    paths = []
    for d in tile_dirs:
        paths += glob.glob(os.path.join(d, "**", "*.png"), recursive=True)
    if rng is not None and len(paths) > limit:
        paths = [paths[i] for i in rng.permutation(len(paths))[:limit]]
    pool = []
    for p in paths[:limit]:
        try:
            g = np.asarray(Image.open(p).convert("L"), np.float32) / 255.0
            pool.append(FL.flatten(g))                      # flatten each bg tile ONCE
        except Exception:
            pass
    if not pool:
        pool = [np.ones((256, 256), np.float32)]
    return pool

def make_sample(rng, bg_pool):
    cls = F.CLASS_NAMES[rng.randint(len(F.CLASS_NAMES))]
    ink = F.render_ink(F.random_text(rng), F.CLASSES[cls], rng)
    gray = D.to_fixed(D.composite(ink, bg_pool[rng.randint(len(bg_pool))], rng), 192)
    return gray, F.CLASS_IDX[cls]

def _norm_batch(xs):
    m = xs.mean(axis=(2, 3), keepdims=True); s = xs.std(axis=(2, 3), keepdims=True) + 1e-5
    return (xs - m) / s

def make_batch(n, rng, bg_pool):
    xs = np.zeros((n, 1, D.TARGET_H, 192), np.float32); ys = np.zeros((n,), np.int64)
    for i in range(n):
        g, y = make_sample(rng, bg_pool); xs[i, 0] = g; ys[i] = y
    return _norm_batch(xs), ys

# ---------------- real crops (shared by train consistency + embed_cluster) ----------------
_tc = {}
def load_tile(tx, ty, tile_dirs):
    k = (tx, ty)
    if k in _tc:
        return _tc[k]
    im = None
    for base in tile_dirs:
        p = os.path.join(base, str(tx), str(ty) + ".png")
        if os.path.exists(p):
            try: im = np.asarray(Image.open(p).convert("L"), np.float32) / 255.0; break
            except Exception: im = None
    if len(_tc) < 1200:
        _tc[k] = im
    return im

def crop_box(gpoly, tile_dirs, pad=0.12):
    """Assemble covering tiles, FLATTEN the canvas (tile-scale), crop the box -> 0..1 array."""
    xs = [p[0] for p in gpoly]; ys = [p[1] for p in gpoly]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    bw, bh = maxx - minx, maxy - miny
    minx -= bw * pad; maxx += bw * pad; miny -= bh * pad; maxy += bh * pad
    tx0, tx1 = int(minx // 256), int(maxx // 256); ty0, ty1 = int(miny // 256), int(maxy // 256)
    W, H = (tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256
    canvas = np.ones((H, W), np.float32); ok = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = load_tile(tx, ty, tile_dirs)
            if t is not None:
                canvas[(ty - ty0) * 256:(ty - ty0) * 256 + t.shape[0],
                       (tx - tx0) * 256:(tx - tx0) * 256 + t.shape[1]] = t; ok = True
    if not ok:
        return None
    canvas = FL.flatten(canvas)                            # tile-scale paper flattening
    ox, oy = tx0 * 256, ty0 * 256
    L, U = max(0, int(minx - ox)), max(0, int(miny - oy))
    R, Dn = min(W, int(maxx - ox)), min(H, int(maxy - oy))
    if R - L < 6 or Dn - U < 6:
        return None
    return canvas[U:Dn, L:R]

def to_fixed01(a01):
    """0..1 array (any HxW) -> 0..1 64x192 (height-resize + width-fix), NOT normalised."""
    if a01.shape[0] != D.TARGET_H:
        a01 = ndi.zoom(a01, (D.TARGET_H / a01.shape[0], D.TARGET_H / a01.shape[0]), order=1)
    return np.clip(D.to_fixed(a01, 192), 0, 1)

def norm1(a01):
    return ((a01 - a01.mean()) / (a01.std() + 1e-5)).astype(np.float32)

def load_real_pool(boxes_glob, tile_dirs, n, rng):
    """crop up to n real spotter boxes -> list of flattened 0..1 64x192 arrays (unlabelled)."""
    import json
    boxes = []
    for f in glob.glob(boxes_glob):
        for line in open(f):
            line = line.strip()
            if line: boxes.append(json.loads(line))
    rng.shuffle(boxes)
    pool = []
    for b in boxes:
        if len(pool) >= n: break
        c = crop_box(b["gpoly"], tile_dirs)
        if c is not None:
            pool.append(to_fixed01(c))
    return pool

def aug_view(a01, rng):
    """one augmented view of a flattened 0..1 crop (photometric + mild geometric)."""
    a = a01.copy()
    if rng.random() < 0.8:
        a = ndi.rotate(a, rng.uniform(-3, 3), reshape=False, order=1, mode="nearest")
    a = np.clip((a - 0.5) * rng.uniform(0.8, 1.2) + 0.5 + rng.uniform(-0.08, 0.08), 0, 1)  # contrast+bright
    a = np.clip(a ** rng.uniform(0.8, 1.25), 0, 1)                                         # gamma
    if rng.random() < 0.5:
        a = ndi.gaussian_filter(a, rng.uniform(0.3, 1.0))
    a = np.clip(a + rng.normal(0, rng.uniform(0.005, 0.025), a.shape), 0, 1)
    return norm1(a)

def real_pair_batch(pool, bs, rng):
    """bs real crops -> two augmented, normalised views each: (v1, v2) tensors [bs,1,64,192]."""
    idx = rng.randint(0, len(pool), size=bs)
    v1 = np.zeros((bs, 1, D.TARGET_H, 192), np.float32); v2 = np.zeros_like(v1)
    for i, j in enumerate(idx):
        v1[i, 0] = aug_view(pool[j], rng); v2[i, 0] = aug_view(pool[j], rng)
    return v1, v2

# ---------------- HITL labelled crops (base64; flatten per-snippet) ----------------
def load_hitl(manifest_path):
    import json
    m = json.load(open(manifest_path)); X, texts, lab = [], [], []
    for s in m["samples"]:
        b = base64.b64decode(s["crop"].split(",", 1)[1])
        g = np.asarray(Image.open(io.BytesIO(b)).convert("L"), np.float32) / 255.0
        X.append(norm1(to_fixed01(FL.flatten(g))))
        texts.append(s.get("text", "")); lab.append(s.get("os_style", "?"))
    return np.stack(X)[:, None], texts, lab

# back-compat name used by embed_cluster
def crop_to_fixed(pil_or_arr):
    if hasattr(pil_or_arr, "convert"):
        a = np.asarray(pil_or_arr.convert("L"), np.float32) / 255.0
    else:
        a = np.asarray(pil_or_arr, np.float32)
        if a.max() > 1.5: a = a / 255.0
    return norm1(to_fixed01(a))
