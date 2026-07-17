#!/usr/bin/env python
"""Stage-1 v2 — MULTI-CLASS line-component RF + proper contrast normalisation.

Per the user's Ilastik experience: RF excels at classifying non-solid line COMPONENTS
(dot / dash / cross / solid). So instead of binary boundary/not, classify each pixel
into {bg, dot, dash, cross, solid}. The classes disambiguate feature types by
construction: hedge=dash, road=solid, footpath=dot, ADMIN BOUNDARY = dot+cross
(mereing). Output = per-component probability maps (the channels fed to Stage-2).

Preprocessing (was missing): background-flatten (divide by morphological paper
estimate) + CLAHE, so faded/uneven scans normalise to consistent ink contrast.
"""
import sys, numpy as np, cv2
from PIL import Image
from skimage.feature import multiscale_basic_features
from sklearn.ensemble import RandomForestClassifier

rng = np.random.default_rng(0)
SRC = "z17_stitch.png"
P = 160
CLASSES = ["bg", "dot", "dash", "cross", "solid"]   # 0..4
COLORS = {1: (0, 200, 0), 2: (255, 60, 0), 3: (0, 0, 255), 4: (0, 200, 255)}  # BGR: dot=grn dash=blu cross=red solid=yel


def preprocess(gray):
    """Flatten paper illumination + CLAHE -> consistent ink contrast (ink stays dark)."""
    g = gray.astype(np.float32)
    paper = cv2.morphologyEx(gray, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))).astype(np.float32)
    flat = np.clip(g / np.maximum(paper, 1) * 200.0, 0, 255).astype(np.uint8)   # flatten
    return cv2.createCLAHE(2.0, (8, 8)).apply(flat)


g_full = preprocess(np.asarray(Image.open(SRC).convert("L"), np.uint8))
H, W = g_full.shape
BOUNDARY_BOXES = [(800, 0, W, H), (0, 880, 560, 1250)]


def boundary_free(x, y, s=P):
    return all(x + s < b[0] or x > b[2] or y + s < b[1] or y > b[3] for b in BOUNDARY_BOXES)


def sample_bg():
    for _ in range(300):
        x = rng.integers(0, W - P); y = rng.integers(0, H - P)
        if boundary_free(x, y) and g_full[y:y+P, x:x+P].mean() > 110:
            return g_full[y:y+P, x:x+P].copy()
    return g_full[:P, :P].copy()


def smooth_path(n=6):
    pts = rng.integers(6, P - 6, size=(n, 2)).astype(np.float32); pts = pts[np.argsort(pts[:, 0])]
    ts = np.linspace(0, 1, 300)
    xs = np.convolve(np.interp(ts, np.linspace(0, 1, n), pts[:, 0]), np.ones(11)/11, "same")
    ys = np.convolve(np.interp(ts, np.linspace(0, 1, n), pts[:, 1]), np.ones(11)/11, "same")
    return np.stack([xs, ys], 1)


def _soft(ink, dark):
    """blur a hard 0..1 ink layer, composite dark ink onto bg-sized float image."""
    return cv2.GaussianBlur(ink, (0, 0), rng.uniform(0.6, 1.3))


def synth():
    """Compose 1-3 random line-features on real bg; return (img, label map 0..4)."""
    img = sample_bg().astype(np.float32); lab = np.zeros((P, P), np.uint8)
    nfeat = rng.integers(1, 4)
    for _ in range(nfeat):
        typ = rng.choice(["dot", "dash", "cross", "solid", "boundary"])
        path = smooth_path(); ink = np.zeros((P, P), np.float32); comp = np.zeros((P, P), np.uint8)
        dark = float(rng.integers(5, 45))
        if typ == "solid":
            cv2.polylines(ink, [path.astype(np.int32)], False, 1.0, int(rng.integers(1, 3)))
            comp[ink > 0.3] = 4
        else:
            d_pitch = rng.uniform(10, 24) if typ != "dash" else rng.uniform(8, 16)
            acc = 0.0; xa = rng.uniform(0, 40); x_pitch = rng.uniform(55, 150)
            if typ == "boundary" and rng.random() < 0.4:                 # mere along a line
                cv2.polylines(ink, [path.astype(np.int32)], False, float(rng.uniform(.4, .8)), 1)
                comp[ink > 0.3] = 4
            for i in range(1, len(path)):
                p0, p1 = path[i-1], path[i]; seg = np.hypot(*(p1-p0)); acc += seg; xa += seg
                if acc >= d_pitch:
                    acc = 0; c = p1.astype(int); k = np.zeros((P, P), np.float32)
                    if typ == "dash":
                        t = (p1-p0); t /= (np.linalg.norm(t)+1e-6)
                        cv2.line(k, tuple((p1-t*3).astype(int)), tuple((p1+t*3).astype(int)), 1.0, 2); cl = 2
                    else:   # dot or boundary-dot
                        cv2.circle(k, tuple(c), int(rng.choice([2, 2, 3])), 1.0, -1); cl = 1
                    ink = np.maximum(ink, k); comp[k > 0.3] = cl
                if typ == "boundary" and xa >= x_pitch:
                    xa = 0; x_pitch = rng.uniform(55, 150); off = rng.integers(-6, 7, 2); c = (p1+off).astype(int)
                    a = int(rng.integers(9, 14)); th = int(rng.integers(2, 4)); k = np.zeros((P, P), np.float32)
                    cv2.line(k, (c[0]-a, c[1]-a), (c[0]+a, c[1]+a), 1.0, th)
                    cv2.line(k, (c[0]-a, c[1]+a), (c[0]+a, c[1]-a), 1.0, th)
                    ink = np.maximum(ink, k); comp[k > 0.3] = 3
        ink = cv2.GaussianBlur(ink, (0, 0), rng.uniform(0.6, 1.3))
        img = img * (1 - ink) + dark * ink
        lab[comp > 0] = comp[comp > 0]
    g = rng.uniform(0.85, 1.2); img = np.clip(255.0*(np.clip(img, 0, 255)/255.0)**g, 0, 255)
    if rng.random() < 0.4: img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.4, 0.8))
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    k = rng.integers(0, 4); img = np.rot90(img, k).copy(); lab = np.rot90(lab, k).copy()
    return img, lab


FEAT = dict(intensity=True, edges=True, texture=True, sigma_min=1, sigma_max=12, num_sigma=5)
def feats(g): return multiscale_basic_features(g, channel_axis=None, **FEAT)


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 70
    X, Y = [], []
    print(f"[s1mc] generating {N} multi-class composites...")
    for k in range(N):
        img, lab = synth(); f = feats(img)
        for cls in range(5):
            idx = np.argwhere(lab == cls)
            if len(idx) == 0: continue
            cap = 1200 if cls else 2500
            sel = idx[rng.choice(len(idx), min(len(idx), cap), replace=False)]
            for (yy, xx) in sel: X.append(f[yy, xx]); Y.append(cls)
        if k == 0: Image.fromarray(img).save("s1mc_synth.png")
    X = np.asarray(X); Y = np.asarray(Y)
    print(f"[s1mc] RF on {X.shape[0]:,} px; class counts {np.bincount(Y)}")
    clf = RandomForestClassifier(180, n_jobs=-1, min_samples_leaf=3, class_weight="balanced", random_state=0)
    clf.fit(X, Y); print(f"[s1mc] train acc {clf.score(X, Y):.3f}")
    ff = feats(g_full).reshape(-1, X.shape[1])
    proba = clf.predict_proba(ff)                       # (HW, 5)
    np.save("s1mc_proba.npy", proba.reshape(H, W, 5).astype(np.float32))
    pred = proba.argmax(1).reshape(H, W)
    viz = cv2.cvtColor(g_full, cv2.COLOR_GRAY2BGR)
    for cls, col in COLORS.items():
        m = pred == cls; viz[m] = (0.4*viz[m] + 0.6*np.array(col)).astype(np.uint8)
    cv2.imwrite("s1mc_components.png", viz)
    Image.fromarray(preprocess(np.asarray(Image.open(SRC).convert("L"), np.uint8))).save("s1mc_preproc.png")
    print("[s1mc] wrote s1mc_components.png s1mc_proba.npy s1mc_synth.png s1mc_preproc.png")


if __name__ == "__main__":
    main()
