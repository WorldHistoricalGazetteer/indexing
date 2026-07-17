#!/usr/bin/env python
"""Track C1 — self-labelled boundary-glyph segmentation (Ilastik-style RF).

Composite synthetic training data: REAL boundary-free map crops (realistic
negatives) + procedurally rendered OS mereing glyphs (dots/dashes/x/arrows) at
KNOWN pixel positions (free masks). Then skimage multiscale features + sklearn
RandomForest (the documented Ilastik-like trainable segmenter). Test on a REAL
crop that contains a genuine admin boundary and see if it isolates it from the
field mesh / tidal-dot look-alike.
"""
import sys, numpy as np, cv2
from PIL import Image
from skimage.feature import multiscale_basic_features
from sklearn.ensemble import RandomForestClassifier

rng = np.random.default_rng(0)
BG_SRC = "parish_probe.png"
# hand-marked boundary corridors in parish_probe (x0,y0,x1,y1) to EXCLUDE from negatives
BOUNDARY_BOXES = [(940, 0, 1180, 700), (600, 700, 980, 900), (600, 900, 780, 1280)]
g_full = np.asarray(Image.open(BG_SRC).convert("L"), np.uint8)
H, W = g_full.shape
PATCH = 128


def boundary_free(x, y, s=PATCH):
    for bx0, by0, bx1, by1 in BOUNDARY_BOXES:
        if not (x + s < bx0 or x > bx1 or y + s < by0 or y > by1):
            return False
    return True


def sample_bg():
    for _ in range(200):
        x = rng.integers(0, W - PATCH); y = rng.integers(0, H - PATCH)
        if boundary_free(x, y) and g_full[y:y+PATCH, x:x+PATCH].mean() > 120:
            return g_full[y:y+PATCH, x:x+PATCH].copy()
    return g_full[:PATCH, :PATCH].copy()


def smooth_path(n=6):
    pts = rng.integers(8, PATCH - 8, size=(n, 2)).astype(np.float32)
    pts = pts[np.argsort(pts[:, 0])]                     # left->right-ish
    ts = np.linspace(0, 1, 220)
    # Catmull-Rom-ish via numpy interp on each axis
    xs = np.interp(ts, np.linspace(0, 1, n), pts[:, 0])
    ys = np.interp(ts, np.linspace(0, 1, n), pts[:, 1])
    xs = np.convolve(xs, np.ones(9)/9, "same"); ys = np.convolve(ys, np.ones(9)/9, "same")
    return np.stack([xs, ys], 1)


def render(bg):
    """Overlay a mereing boundary; return (img, mask) mask: 0 bg,1 dot,2 dash,3 cross,4 arrow."""
    img = bg.copy().astype(np.int16)
    mask = np.zeros_like(bg, np.uint8)
    path = smooth_path()
    ink = int(rng.integers(20, 70))
    d_next = rng.uniform(5, 8)          # dot pitch
    x_next = rng.uniform(38, 75)        # cross pitch
    acc = 0.0; xa = rng.uniform(0, 30)
    dash_mode = rng.random() < 0.35
    for i in range(1, len(path)):
        p0, p1 = path[i-1], path[i]
        seg = np.hypot(*(p1 - p0)); acc += seg; xa += seg
        if acc >= d_next:
            acc = 0
            cx, cy = p1.astype(int)
            if dash_mode:                # short dash along tangent
                t = (p1 - p0); t = t/(np.linalg.norm(t)+1e-6)
                q0 = (p1 - t*2).astype(int); q1 = (p1 + t*2).astype(int)
                cv2.line(img, tuple(q0), tuple(q1), ink, 1); cv2.line(mask, tuple(q0), tuple(q1), 2, 1)
            else:
                cv2.circle(img, (cx, cy), 1, ink, -1); cv2.circle(mask, (cx, cy), 1, 1, -1)
        if xa >= x_next:                 # bold x, slightly offset
            xa = 0; x_next = rng.uniform(38, 75)
            off = rng.integers(-4, 5, 2); cx, cy = (p1 + off).astype(int)
            a = int(rng.integers(6, 9)); th = int(rng.integers(2, 3))
            for dxdy in ((a, a), (a, -a)):
                cv2.line(img, (cx-dxdy[0], cy-dxdy[1]), (cx+dxdy[0], cy+dxdy[1]), ink, th)
                cv2.line(mask, (cx-dxdy[0], cy-dxdy[1]), (cx+dxdy[0], cy+dxdy[1]), 3, th)
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    return img, mask


FEAT = dict(intensity=True, edges=True, texture=True, sigma_min=1, sigma_max=8, num_sigma=4)


def feats(gray):
    return multiscale_basic_features(gray, channel_axis=None, **FEAT)


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    X, Y = [], []
    print(f"[c1] generating {N} composites...")
    for k in range(N):
        img, mask = render(sample_bg())
        f = feats(img)                       # H,W,F
        bnd = mask > 0
        # positives = boundary-glyph pixels; negatives = sampled non-boundary (incl real lines/text)
        pi = np.argwhere(bnd)
        ni = np.argwhere(~bnd)
        take = min(len(pi), 4000)
        if take == 0:
            continue
        sel_n = ni[rng.choice(len(ni), take, replace=False)]
        for (yy, xx) in pi[rng.choice(len(pi), take, replace=False)]:
            X.append(f[yy, xx]); Y.append(1)
        for (yy, xx) in sel_n:
            X.append(f[yy, xx]); Y.append(0)
        if k == 0:
            Image.fromarray(img).save("synth_example.png")
            Image.fromarray((mask*70).astype(np.uint8)).save("synth_mask.png")
    X = np.asarray(X); Y = np.asarray(Y)
    print(f"[c1] training RF on {X.shape[0]:,} px, {X.shape[1]} feats")
    clf = RandomForestClassifier(n_estimators=120, max_depth=None, n_jobs=-1,
                                 min_samples_leaf=4, random_state=0)
    clf.fit(X, Y)
    print(f"[c1] train acc {clf.score(X, Y):.3f}")

    # apply to the REAL full crop (contains a genuine boundary)
    ff = feats(g_full).reshape(-1, X.shape[1])
    prob = clf.predict_proba(ff)[:, 1].reshape(H, W)
    Image.fromarray((np.clip(prob, 0, 1)*255).astype(np.uint8)).save("rf_prob.png")
    rgb = cv2.cvtColor(g_full, cv2.COLOR_GRAY2BGR)
    heat = cv2.applyColorMap((np.clip(prob, 0, 1)*255).astype(np.uint8), cv2.COLORMAP_JET)
    over = cv2.addWeighted(rgb, 0.55, heat, 0.45, 0)
    over[prob < 0.5] = rgb[prob < 0.5]         # only tint confident boundary px
    cv2.imwrite("rf_overlay.png", over)
    print("[c1] wrote rf_prob.png rf_overlay.png synth_example.png")


if __name__ == "__main__":
    main()
