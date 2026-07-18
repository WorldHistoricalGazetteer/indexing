"""Composite a rendered ink-alpha map onto a REAL map background + print degradation.

Domain-gap mitigation (the boundary-probe lesson): a net trained on clean cv2 glyphs keys
on edge sharpness and collapses on real scans. So we (a) place ink over real tile crops,
(b) soften edges, (c) vary ink opacity, (d) break strokes, (e) add foxing / speckle, before
the encoder ever sees it. Output: 64 x W float32 grayscale in [0,1] (1 = white paper).
"""
import numpy as np
from scipy import ndimage as ndi

TARGET_H = 64

def _rot(a, deg):
    return ndi.rotate(a, deg, reshape=True, order=1, mode="constant", cval=0.0)

def composite(ink, bg, rng):
    """ink: HxW alpha (0..1); bg: HbxWb grayscale 0..1 (1=paper). -> 64xW grayscale."""
    # small rotation + scale jitter on the ink
    if rng.random() < 0.8:
        ink = _rot(ink, rng.uniform(-4, 4))
    # scale ink so its height fills ~55-85% of TARGET_H
    ih = max(1, ink.shape[0])
    tgt = rng.uniform(0.55, 0.85) * TARGET_H
    s = tgt / ih
    zh, zw = max(8, int(ink.shape[0] * s)), max(8, int(ink.shape[1] * s))
    ink = np.asarray(_zoom(ink, zh, zw), np.float32)

    # soft edges (defocus/paper spread)
    ink = ndi.gaussian_filter(ink, rng.uniform(0.5, 1.4))
    # broken strokes: knock out random speckle from the ink
    if rng.random() < 0.7:
        holes = rng.random(ink.shape) < rng.uniform(0.02, 0.10)
        holes = ndi.binary_dilation(holes, iterations=1)
        ink = ink * (1.0 - 0.9 * holes)

    H, W = TARGET_H, ink.shape[1]
    canvas = _bg_patch(bg, H, W + 8, rng)      # a little wider than the ink
    # place ink with vertical jitter
    oy = rng.randint(0, max(1, H - ink.shape[0]))
    ox = 4
    a = np.zeros((H, canvas.shape[1]), np.float32)
    a[oy:oy + ink.shape[0], ox:ox + W] = np.clip(ink, 0, 1)

    ink_val = rng.uniform(0.08, 0.32)          # ink darkness (never pure black)
    opacity = rng.uniform(0.60, 0.95)
    a = a * opacity
    out = canvas * (1 - a) + ink_val * a

    # foxing: low-frequency brown/grey stains
    if rng.random() < 0.5:
        stain = ndi.gaussian_filter(rng.random(out.shape), rng.uniform(8, 20))
        stain = (stain - stain.min()) / (np.ptp(stain) + 1e-6)
        out = out * (1 - 0.12 * stain)
    # fine speckle
    out = out + rng.normal(0, rng.uniform(0.005, 0.03), out.shape)
    return np.clip(out, 0, 1).astype(np.float32)

def _zoom(a, zh, zw):
    return ndi.zoom(a, (zh / a.shape[0], zw / a.shape[1]), order=1)

def _bg_patch(bg, H, W, rng):
    """random HxW crop of a background tile, tiled if too small."""
    bh, bw = bg.shape
    if bh < H or bw < W:
        ry = int(np.ceil(H / bh)); rx = int(np.ceil(W / bw))
        bg = np.tile(bg, (ry, rx))
        bh, bw = bg.shape
    y = rng.randint(0, bh - H + 1); x = rng.randint(0, bw - W + 1)
    p = bg[y:y + H, x:x + W].copy()
    # gentle brightness/contrast jitter of the paper
    p = np.clip((p - 0.5) * rng.uniform(0.85, 1.15) + rng.uniform(0.42, 0.58) + 0.0, 0, 1)
    return p.astype(np.float32)

def to_fixed(gray, W=192):
    """resize a 64xW' grayscale to fixed 64xW (pad with paper / center-crop)."""
    h, w = gray.shape
    if w == W:
        return gray
    if w > W:
        # resize down keeping height
        gray = ndi.zoom(gray, (1.0, W / w), order=1)
        return gray[:, :W]
    out = np.ones((TARGET_H, W), np.float32) * float(np.median(gray[:, :3]))
    off = (W - w) // 2
    out[:, off:off + w] = gray
    return out
