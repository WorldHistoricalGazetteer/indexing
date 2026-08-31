"""GB-STAMP route (1), STAGE 1 — STYLE classifier (italic/upright/blackletter/numeral), trained + validated on
SG's 260 human-labelled real crops (font_testset_v3). The crops live as `const CROPS=[...]` (b64) in the v3
review HTML, in the same order as font_testset_v3_decisions.json. Features are interpretable (whole-word slant,
ink density, stroke rhythm/width, digit fraction); a small RandomForest (5-fold CV) sorts them, with a rule
baseline if sklearn is absent. This is the first split of style -> fill -> decor -> face.

    /vast/ishi/envs/mapreader/bin/python style_classify.py   (or boundary env)
"""
import re, json, io, base64, os
from collections import Counter, defaultdict
import numpy as np, cv2
from PIL import Image

HTML = "/vast/ishi/gb1900/edition/spot/font_testset_v3.html"
DEC = "/vast/ishi/gb1900/probe/font/font_testset_v3_decisions.json"

def load_pairs():
    h = open(HTML).read()
    crops = json.loads(re.search(r"const CROPS\s*=\s*(\[.*?\]);", h, re.S).group(1))
    dec = json.load(open(DEC))
    # find the b64 image field
    imgkey = next(k for k, v in crops[0].items() if isinstance(v, str) and len(v) > 200)
    pairs = []
    for i, d in enumerate(dec):
        if i >= len(crops): break
        font = d.get("font")
        if font in (None, "unclear"): continue
        b = crops[i][imgkey]
        b = b.split(",", 1)[1] if b.startswith("data:") else b
        g = np.asarray(Image.open(io.BytesIO(base64.b64decode(b))).convert("L"), np.uint8)
        pairs.append((g, d.get("text", ""), font))
    return pairs

def deslant(m):
    # shear that verticalises the strokes (max column-ink variance) — length-independent italic slant.
    H, W = m.shape; mm = (m > 0).astype(np.float32); best_a, best_v = 0.0, -1.0
    for a in np.arange(-0.7, 0.71, 0.05):
        Wt = W + int(abs(a) * H) + 1
        sh = cv2.warpAffine(mm, np.float32([[1, a, -a * H / 2 + abs(a) * H / 2], [0, 1, 0]]), (Wt, H))
        v = float(sh.sum(0).var())
        if v > best_v: best_v, best_a = v, a
    return best_a                                             # italic -> |a| large; upright -> ~0

def feats(gray, text):
    m = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    ys, xs = np.nonzero(m)
    if len(xs) < 20: return None
    h = ys.max() - ys.min() + 1; w = xs.max() - xs.min() + 1
    slant = deslant(m)                                        # shear-deslant italic slope (length-independent)
    ink_density = len(xs) / (h * w)
    digit_frac = sum(c.isdigit() for c in text) / max(1, sum(c.isalnum() for c in text))
    # stroke width via distance transform (blackletter = thick, low variance; outline = thin)
    dt = cv2.distanceTransform(m, cv2.DIST_L2, 3)
    sw = dt[dt > 0]
    sw_mean = float(sw.mean()) / h if len(sw) else 0.0
    sw_cv = float(sw.std() / (sw.mean() + 1e-6)) if len(sw) else 0.0
    # vertical-stroke rhythm: FFT of column ink profile (blackletter = dense regular verticals)
    col = m.sum(0).astype(np.float32); col -= col.mean()
    F = np.abs(np.fft.rfft(col * np.hanning(len(col)))) if len(col) > 8 else np.array([0.0])
    rhythm = float(F[2:].max() / (F.mean() + 1e-6)) if len(F) > 2 else 0.0
    return [abs(slant), slant, ink_density, digit_frac, sw_mean, sw_cv, rhythm, float(h)]

FNAMES = ["abs_slant", "slant", "ink_density", "digit_frac", "sw_mean", "sw_cv", "rhythm", "height"]

def main():
    pairs = load_pairs()
    X, Y = [], []
    for g, t, f in pairs:
        fe = feats(g, t)
        if fe: X.append(fe); Y.append(f)
    X = np.array(X); Y = np.array(Y)
    print(f"training set: {len(Y)} crops; classes {dict(Counter(Y))}", flush=True)
    # per-class feature means (interpretability)
    print("\nper-class feature means:")
    print("  class        " + "  ".join(f"{n:>10}" for n in FNAMES))
    for c in sorted(set(Y)):
        mu = X[Y == c].mean(0)
        print(f"  {c:11} " + "  ".join(f"{v:10.3f}" for v in mu))
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_predict, StratifiedKFold
        clf = RandomForestClassifier(n_estimators=300, min_samples_leaf=2, random_state=0, class_weight="balanced")
        pred = cross_val_predict(clf, X, Y, cv=StratifiedKFold(5, shuffle=True, random_state=0))
        acc = (pred == Y).mean()
        print(f"\nRandomForest 5-fold CV accuracy: {acc:.1%}")
        labs = sorted(set(Y))
        print("confusion (row=true, col=pred): " + " ".join(f"{l[:5]:>6}" for l in labs))
        for tl in labs:
            row = [int(((Y == tl) & (pred == pl)).sum()) for pl in labs]
            print(f"  {tl:11} " + " ".join(f"{v:6d}" for v in row))
        clf.fit(X, Y)
        imp = sorted(zip(FNAMES, clf.feature_importances_), key=lambda z: -z[1])
        print("feature importance:", ", ".join(f"{n}={v:.2f}" for n, v in imp))
    except ImportError:
        print("\n(sklearn absent — rule baseline)")
        pred = []
        for fe in X:
            asl, sl, dens, dig, swm, swcv, rhy, ht = fe
            pred.append("numeral" if dig > 0.5 else "italic" if asl > 0.27 else "upright")
        pred = np.array(pred); m = Y != "blackletter"
        print(f"rule acc (excl blackletter): {(pred[m]==Y[m]).mean():.1%}")

if __name__ == "__main__":
    main()
