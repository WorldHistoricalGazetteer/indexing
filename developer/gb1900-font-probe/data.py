"""Synthetic training batches for the font-style encoder + real-crop loading.

Backgrounds are sampled from REAL cached OS tiles so the encoder never keys on clean paper.
"""
import os, glob, io, base64, numpy as np
from PIL import Image
import fonts as F
import degrade as D

def load_bg_pool(tile_dirs, limit=300, rng=None):
    paths = []
    for d in tile_dirs:
        paths += glob.glob(os.path.join(d, "**", "*.png"), recursive=True)
    if rng is not None and len(paths) > limit:
        paths = [paths[i] for i in rng.permutation(len(paths))[:limit]]
    pool = []
    for p in paths[:limit]:
        try:
            im = Image.open(p).convert("L")
            pool.append(np.asarray(im, np.float32) / 255.0)
        except Exception:
            pass
    if not pool:                       # fallback: flat paper
        pool = [np.ones((256, 256), np.float32) * 0.9]
    return pool

def make_sample(rng, bg_pool):
    cls = F.CLASS_NAMES[rng.randint(len(F.CLASS_NAMES))]
    recipe = F.CLASSES[cls]
    txt = F.random_text(rng)
    ink = F.render_ink(txt, recipe, rng)
    bg = bg_pool[rng.randint(len(bg_pool))]
    gray = D.composite(ink, bg, rng)
    gray = D.to_fixed(gray, 192)
    return gray, F.CLASS_IDX[cls]

def make_batch(n, rng, bg_pool):
    xs = np.zeros((n, 1, D.TARGET_H, 192), np.float32)
    ys = np.zeros((n,), np.int64)
    for i in range(n):
        g, y = make_sample(rng, bg_pool)
        xs[i, 0] = g
        ys[i] = y
    # normalize per-image (mean/std) — robust to paper brightness
    m = xs.mean(axis=(2, 3), keepdims=True)
    s = xs.std(axis=(2, 3), keepdims=True) + 1e-5
    xs = (xs - m) / s
    return xs, ys

# ---- real crops -----------------------------------------------------------------
def crop_to_fixed(pil_gray):
    a = np.asarray(pil_gray.convert("L"), np.float32) / 255.0
    if a.shape[0] != D.TARGET_H:
        from scipy import ndimage as ndi
        a = ndi.zoom(a, (D.TARGET_H / a.shape[0], D.TARGET_H / a.shape[0]), order=1)
    a = D.to_fixed(a, 192)
    a = (a - a.mean()) / (a.std() + 1e-5)
    return a

def load_hitl(manifest_path):
    """the 78 VLM-labelled clean crops -> (X, texts, vlm_os_style)."""
    import json
    m = json.load(open(manifest_path))
    X, texts, lab = [], [], []
    for s in m["samples"]:
        b = base64.b64decode(s["crop"].split(",", 1)[1])
        X.append(crop_to_fixed(Image.open(io.BytesIO(b))))
        texts.append(s.get("text", ""))
        lab.append(s.get("os_style", "?"))
    return np.stack(X)[:, None], texts, lab
