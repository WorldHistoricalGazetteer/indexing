#!/usr/bin/env python
"""Track C1 at ZOOM-17 — self-labelled boundary-glyph RF (Ilastik-style), z17 scale.

z17 doubles glyph size vs z16 (dots ~2-2.5px @ pitch ~11px; x arms ~11px/thick 3-4px),
so a real thickness/shape gap opens vs the ~2px field mesh. Same composite recipe (real
boundary-free crops + rendered mereing glyphs w/ free masks) at z17 scale, plus hard
negatives (bg crops deliberately include buildings/text). Test on the real z17 stitch.
"""
import sys, numpy as np, cv2
from PIL import Image
from skimage.feature import multiscale_basic_features
from sklearn.ensemble import RandomForestClassifier

rng = np.random.default_rng(0)
SRC = "z17_stitch.png"
g_full = np.asarray(Image.open(SRC).convert("L"), np.uint8)
H, W = g_full.shape
PATCH = 160
# z17 boundary corridors to EXCLUDE from clean-negative background sampling
BOUNDARY_BOXES = [(800, 0, W, H), (0, 880, 560, 1250)]


def boundary_free(x, y, s=PATCH):
    for bx0, by0, bx1, by1 in BOUNDARY_BOXES:
        if not (x + s < bx0 or x > bx1 or y + s < by0 or y > by1):
            return False
    return True


def sample_bg():
    for _ in range(300):
        x = rng.integers(0, W - PATCH); y = rng.integers(0, H - PATCH)
        if boundary_free(x, y) and g_full[y:y+PATCH, x:x+PATCH].mean() > 120:
            return g_full[y:y+PATCH, x:x+PATCH].copy()
    return g_full[:PATCH, :PATCH].copy()


def smooth_path(n=6):
    pts = rng.integers(10, PATCH - 10, size=(n, 2)).astype(np.float32)
    pts = pts[np.argsort(pts[:, 0])]
    ts = np.linspace(0, 1, 260)
    xs = np.interp(ts, np.linspace(0, 1, n), pts[:, 0])
    ys = np.interp(ts, np.linspace(0, 1, n), pts[:, 1])
    xs = np.convolve(xs, np.ones(11)/11, "same"); ys = np.convolve(ys, np.ones(11)/11, "same")
    return np.stack([xs, ys], 1)


def render(bg):
    """Overlay a z17 mereing boundary; mask 0 bg,1 dot/dash,2 cross,3 arrow."""
    img = bg.copy().astype(np.int16); mask = np.zeros_like(bg, np.uint8)
    path = smooth_path(); ink = int(rng.integers(15, 60))
    d_pitch = rng.uniform(9, 14); x_pitch = rng.uniform(55, 130)
    dot_r = rng.choice([1, 2, 2]); dash = rng.random() < 0.3
    acc = 0.0; xa = rng.uniform(0, 40)
    for i in range(1, len(path)):
        p0, p1 = path[i-1], path[i]; seg = np.hypot(*(p1-p0)); acc += seg; xa += seg
        if acc >= d_pitch:
            acc = 0; cx, cy = p1.astype(int)
            if dash:
                t = (p1-p0); t = t/(np.linalg.norm(t)+1e-6)
                q0 = (p1-t*3).astype(int); q1 = (p1+t*3).astype(int)
                cv2.line(img, tuple(q0), tuple(q1), ink, 2); cv2.line(mask, tuple(q0), tuple(q1), 1, 2)
            else:
                cv2.circle(img, (cx, cy), int(dot_r), ink, -1); cv2.circle(mask, (cx, cy), int(dot_r), 1, -1)
        if xa >= x_pitch:
            xa = 0; x_pitch = rng.uniform(55, 130)
            off = rng.integers(-6, 7, 2); cx, cy = (p1+off).astype(int)
            a = int(rng.integers(9, 14)); th = int(rng.integers(3, 5))
            for dd in ((a, a), (a, -a)):
                cv2.line(img, (cx-dd[0], cy-dd[1]), (cx+dd[0], cy+dd[1]), ink, th)
                cv2.line(mask, (cx-dd[0], cy-dd[1]), (cx+dd[0], cy+dd[1]), 2, th)
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    return img, mask


FEAT = dict(intensity=True, edges=True, texture=True, sigma_min=1, sigma_max=12, num_sigma=5)
def feats(gray): return multiscale_basic_features(gray, channel_axis=None, **FEAT)


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    X, Y = [], []
    print(f"[c1-z17] generating {N} composites (patch {PATCH})...")
    for k in range(N):
        img, mask = render(sample_bg()); f = feats(img); bnd = mask > 0
        pi = np.argwhere(bnd); ni = np.argwhere(~bnd)
        take = min(len(pi), 3500)
        if take == 0: continue
        for (yy, xx) in pi[rng.choice(len(pi), take, replace=False)]:
            X.append(f[yy, xx]); Y.append(1)
        for (yy, xx) in ni[rng.choice(len(ni), take, replace=False)]:
            X.append(f[yy, xx]); Y.append(0)
        if k == 0:
            Image.fromarray(img).save("z17_synth_example.png")
    X = np.asarray(X); Y = np.asarray(Y)
    print(f"[c1-z17] RF on {X.shape[0]:,} px, {X.shape[1]} feats")
    clf = RandomForestClassifier(n_estimators=150, n_jobs=-1, min_samples_leaf=4, random_state=0)
    clf.fit(X, Y); print(f"[c1-z17] train acc {clf.score(X, Y):.3f}")
    ff = feats(g_full).reshape(-1, X.shape[1])
    prob = clf.predict_proba(ff)[:, 1].reshape(H, W)
    Image.fromarray((np.clip(prob, 0, 1)*255).astype(np.uint8)).save("z17_rf_prob.png")
    rgb = cv2.cvtColor(g_full, cv2.COLOR_GRAY2BGR)
    heat = cv2.applyColorMap((np.clip(prob, 0, 1)*255).astype(np.uint8), cv2.COLORMAP_JET)
    over = cv2.addWeighted(rgb, 0.5, heat, 0.5, 0); over[prob < 0.5] = rgb[prob < 0.5]
    cv2.imwrite("z17_rf_overlay.png", over)
    print("[c1-z17] wrote z17_rf_overlay.png z17_rf_prob.png z17_synth_example.png")


if __name__ == "__main__":
    main()
