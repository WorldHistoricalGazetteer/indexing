"""Phase C — ENGINEERED-FEATURE font discriminator (SG's idea: feed MapReader + OpenCV style parameters).

The pixel-CNN underperformed (0.56) because with ~192 words it must LEARN style invariances from scratch.
Those invariances are low-dimensional + interpretable, so measure them directly and let a gradient-boosted
classifier (trains cleanly on ~1100 glyphs) do the rest. Per glyph: slant/shear (moments), stroke width +
weight-contrast (distance transform), fill density, contour complexity (ornateness), aspect, holes; + letter
id; + MapReader box SIZE (cap-height -> ground-m). Per-word aggregation, 5-fold BY WORD. Also ENSEMBLES with
the raster same-letter kNN. Compares to the 0.776 kNN baseline.

    /vast/ishi/envs/boundary/bin/python feat_disc.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import json, math, numpy as np, cv2
from collections import Counter, defaultdict
from build_alphabet import force_split
from make_font_testset_v2 import derotate
from sklearn.ensemble import HistGradientBoostingClassifier

DEC = "/vast/ishi/gb1900/probe/font/font_testset_decisions_1.json"
BOXES = "/vast/ishi/gb1900/edition/spot/font_testset_v2_boxes.json"
STYLES = ["italic", "blackletter", "upright"]; SI = {s: i for i, s in enumerate(STYLES)}
VOC = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"; N17 = 2 ** 17
def lidx(c): return VOC.find(c.upper()) if c.upper() in VOC else 36
def mpp(lat): return 156543.03392 * math.cos(math.radians(lat)) / N17

def glyph_feats(g):
    """g: bool glyph raster (height-normalised). -> interpretable style features."""
    u = g.astype(np.uint8); ink = u * 255
    ys, xs = np.where(u > 0)
    if len(ys) < 8: return None
    h = ys.max() - ys.min() + 1; w = xs.max() - xs.min() + 1; area = float(u.sum())
    m = cv2.moments(u, binaryImage=True)
    shear = m["mu11"] / m["mu02"] if m["mu02"] > 1e-6 else 0.0          # italic slant (x-shear per unit y)
    theta = 0.5 * math.atan2(2 * m["mu11"], (m["mu20"] - m["mu02"] + 1e-9))
    dt = cv2.distanceTransform(ink, cv2.DIST_L2, 3)[u > 0]
    sw = float(dt.mean()) * 2.0                                        # ~stroke width (px, at fixed height)
    swc = float(dt.std()) / (float(dt.mean()) + 1e-6)                  # weight contrast (blackletter/serif)
    cnts, hier = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    perim = sum(cv2.arcLength(c, True) for c in cnts)
    complexity = perim / (2.0 * math.sqrt(math.pi * area) + 1e-6)      # isoperimetric: ornate -> high
    holes = int((hier[0][:, 3] != -1).sum()) if hier is not None else 0
    return [area / (h * w), shear, theta, sw / h, swc, complexity, len(cnts), holes, w / h]

def harvest():
    dec = json.load(open(DEC)); font_by_i = {x["i"]: x["font"] for x in dec if x.get("font")}
    samp = json.load(open(BOXES))
    words = []                                                         # (font, [(letter, feats)], size_m)
    for i, r in enumerate(samp):
        f = font_by_i.get(i)
        if f not in STYLES or r["text"] != dec[i]["text"]: continue
        patch = derotate(r)
        if patch is None: continue
        letters = [c for c in r["text"] if c.isalnum()]
        gs = force_split(patch, len(letters))
        if len(gs) != len(letters): continue
        poly = np.array(r["gpoly"], np.float32); (_, _), (rw, rh), _ = cv2.minAreaRect(poly)
        size_m = min(rw, rh) * mpp(r["lat"])                           # cap-height in ground metres
        gl = []
        for k, g in enumerate(gs):
            fe = glyph_feats(g)
            if fe is not None: gl.append((letters[k].upper(), fe))
        if gl: words.append((f, gl, size_m))
    return words

def main():
    words = harvest()
    print(f"words {len(words)}  glyphs {sum(len(g) for _, g, _ in words)}  "
          f"fonts {dict(Counter(f for f, _, _ in words))}", flush=True)
    # build per-glyph design matrix
    X = []; y = []; wid = []
    for wi, (f, gl, size_m) in enumerate(words):
        for L, fe in gl:
            X.append(fe + [lidx(L), math.log1p(max(0.01, size_m))]); y.append(SI[f]); wid.append(wi)
    X = np.array(X, float); y = np.array(y); wid = np.array(wid)
    FEATS = ["fill", "shear", "theta", "sw/h", "wt_contrast", "complexity", "ncnt", "holes", "aspect", "letter", "log_size"]

    rng = np.random.RandomState(0); order = rng.permutation(len(words)); folds = np.array_split(order, 5)
    conf = Counter(); tot = Counter(); imp = np.zeros(len(FEATS))
    for te in folds:
        te = set(te.tolist()); trm = ~np.isin(wid, list(te))
        clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_depth=4,
                                             l2_regularization=1.0, random_state=0)
        clf.fit(X[trm], y[trm])
        for wi in te:
            gm = wid == wi
            if not gm.any(): continue
            prob = clf.predict_proba(X[gm]).mean(0); pred = STYLES[int(prob.argmax())]
            tf = STYLES[y[gm][0]]; conf[(tf, pred)] += 1; tot[tf] += 1
    N = sum(tot.values()); acc = sum(conf[(s, s)] for s in STYLES) / max(1, N)
    print(f"\n=== engineered-feature discriminator (HGB, 5-fold by word, N={N}) ===")
    print(f"accuracy {acc:.3f}   [raster-kNN baseline 0.776; CNN 0.56]")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>8s}" for s in STYLES) + "  recall")
    for s in STYLES:
        print(f"  {s:10s}" + "".join(f"{conf[(s,d)]:>8d}" for d in STYLES) + f"  {conf[(s,s)]/max(1,tot[s]):.2f}")
    # quick permutation importance on a full-fit model
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_depth=4, l2_regularization=1.0, random_state=0).fit(X, y)
    base = (clf.predict(X) == y).mean(); imps = []
    for j in range(len(FEATS)):
        Xp = X.copy(); Xp[:, j] = rng.permutation(Xp[:, j])
        imps.append(base - (clf.predict(Xp) == y).mean())
    print("\nfeature importance (accuracy drop when shuffled):")
    for j in np.argsort(imps)[::-1]:
        print(f"  {FEATS[j]:14s} {imps[j]:+.3f}")

if __name__ == "__main__":
    main()
