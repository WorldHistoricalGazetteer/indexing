"""Composite a rendered ink-alpha map onto a REAL (pre-flattened) map background + degradation.

Iteration 2: backgrounds are already paper-flattened (see flatten.py / data.load_bg_pool), so we
ADD ONLY high-frequency degradation that survives flattening on real crops (speckle, broken strokes,
blur, linework crossing the text) and DROP smooth foxing (flattening removes it on real → keeping it
on synthetic would re-open the domain gap). Plus curved baselines (lever b). Output: 64 x W float32
grayscale in [0,1] (1 = flattened paper).
"""
import numpy as np
from scipy import ndimage as ndi

TARGET_H = 64

def _rot(a, deg):
    return ndi.rotate(a, deg, reshape=True, order=1, mode="constant", cval=0.0)

def _curve_warp(a, rng):
    """gentle sinusoidal baseline curvature (roads/contours make OS labels bend)."""
    H, W = a.shape
    A = rng.uniform(0, 0.10) * H
    period = rng.uniform(W * 0.6, W * 1.6)
    ph = rng.uniform(0, 6.283)
    dy = (A * np.sin(2 * np.pi * np.arange(W) / period + ph)).astype(np.float32)
    yy = np.clip(np.arange(H)[:, None] - dy[None, :], 0, H - 1)
    xx = np.broadcast_to(np.arange(W), (H, W)).astype(np.float32)
    return ndi.map_coordinates(a, [yy, xx], order=1, mode="constant", cval=0.0)

def _add_linework(out, rng):
    """1-2 dark lines crossing the snippet (roads/contours/hachures over text)."""
    H, W = out.shape
    for _ in range(rng.randint(0, 3)):
        m = rng.uniform(-0.5, 0.5); b = rng.uniform(0, H); w = rng.uniform(0.5, 1.6)
        dark = rng.uniform(0.1, 0.4)
        ys = m * np.arange(W) + b
        for x in range(W):
            y0 = int(ys[x])
            for dy in range(-int(w), int(w) + 1):
                y = y0 + dy
                if 0 <= y < H:
                    out[y, x] = min(out[y, x], dark)
    return out

def _add_road_casing(out, oy, ih, rng):
    """Two roughly-parallel lines flanking the text band = the road casing a road name sits in.
    A strong contextual cue for road-name labels (small solid caps)."""
    H, W = out.shape
    dark = rng.uniform(0.12, 0.38); w = rng.uniform(0.5, 1.3)
    A = rng.uniform(0, 0.04) * H; period = rng.uniform(W, 2.2 * W); ph = rng.uniform(0, 6.283)
    slope = rng.uniform(-0.08, 0.08)
    curve = slope * (np.arange(W) - W / 2) + A * np.sin(2 * np.pi * np.arange(W) / period + ph)
    for base in (oy - rng.uniform(1, 4), oy + ih + rng.uniform(1, 4)):   # above + below the text
        ys = base + curve
        for x in range(W):
            y0 = int(ys[x])
            for dy in range(-int(w), int(w) + 1):
                y = y0 + dy
                if 0 <= y < H:
                    out[y, x] = min(out[y, x], dark)
    return out

def composite(ink, bg, rng, road=False):
    """ink: HxW alpha (0..1); bg: pre-flattened grayscale (~1=paper). -> 64xW grayscale.
    road=True adds the parallel road-casing lines a road name sits between (context cue)."""
    if rng.random() < 0.8:
        ink = _rot(ink, rng.uniform(-4, 4))
    ih = max(1, ink.shape[0])
    tgt = rng.uniform(0.55, 0.85) * TARGET_H
    s = tgt / ih
    ink = np.asarray(_zoom(ink, max(8, int(ink.shape[0] * s)), max(8, int(ink.shape[1] * s))), np.float32)
    if rng.random() < 0.6:
        ink = _curve_warp(ink, rng)
    ink = ndi.gaussian_filter(ink, rng.uniform(0.5, 1.4))            # soft edges
    if rng.random() < 0.7:                                            # broken strokes
        holes = ndi.binary_dilation(rng.random(ink.shape) < rng.uniform(0.02, 0.10), iterations=1)
        ink = ink * (1.0 - 0.9 * holes)

    H, W = TARGET_H, ink.shape[1]
    canvas = _bg_patch(bg, H, W + 8, rng)
    ink_h = ink.shape[0]
    oy = rng.randint(0, max(1, H - ink_h)); ox = 4
    a = np.zeros((H, canvas.shape[1]), np.float32)
    a[oy:oy + ink_h, ox:ox + W] = np.clip(ink, 0, 1)

    ink_val = rng.uniform(0.08, 0.32)                                # ink darkness (never pure black)
    a = a * rng.uniform(0.60, 0.95)
    out = canvas * (1 - a) + ink_val * a
    if road:                                                         # road-name casing (context)
        out = _add_road_casing(out, oy, ink_h, rng)
    elif rng.random() < 0.6:
        out = _add_linework(out, rng)
    out = out + rng.normal(0, rng.uniform(0.005, 0.03), out.shape)   # fine speckle
    return np.clip(out, 0, 1).astype(np.float32)

def _zoom(a, zh, zw):
    return ndi.zoom(a, (zh / a.shape[0], zw / a.shape[1]), order=1)

def _bg_patch(bg, H, W, rng):
    """random HxW crop of a (pre-flattened) background tile; paper already ~1.0, minimal jitter."""
    bh, bw = bg.shape
    if bh < H or bw < W:
        bg = np.tile(bg, (int(np.ceil(H / bh)), int(np.ceil(W / bw)))); bh, bw = bg.shape
    y = rng.randint(0, bh - H + 1); x = rng.randint(0, bw - W + 1)
    p = bg[y:y + H, x:x + W].copy()
    return np.clip(p * rng.uniform(0.95, 1.03), 0, 1).astype(np.float32)

def to_fixed(gray, W=192):
    h, w = gray.shape
    if w == W:
        return gray
    if w > W:
        return ndi.zoom(gray, (1.0, W / w), order=1)[:, :W]
    out = np.ones((TARGET_H, W), np.float32) * float(np.median(gray[:, :3]))
    off = (W - w) // 2
    out[:, off:off + w] = gray
    return out
