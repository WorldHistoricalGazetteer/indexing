"""Are the style classifiers' FAILURE MODES complementary? Run the whole-word feature RF and the per-glyph
same-letter kNN on SHARED 5-fold-by-word splits, compare per-word predictions, and test whether combining
their probability scores beats either alone. Reports: each method's acc, agreement, ORACLE (either-correct =
ceiling for any combiner), and a probability-averaged ensemble.

    /vast/ishi/envs/mapreader/bin/python style_ensemble.py   (or boundary env; needs sklearn)
"""
import re, json, io, base64, sys
from collections import defaultdict, Counter
import numpy as np
from PIL import Image
import cv2
sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
from build_alphabet import force_split
from discrim_test import sims_row

HTML = "/vast/ishi/gb1900/edition/spot/font_testset_v3.html"
DEC = "/vast/ishi/gb1900/probe/font/font_testset_v3_decisions.json"
K = 5

def load_words():
    h = open(HTML).read()
    crops = json.loads(re.search(r"const CROPS\s*=\s*(\[.*?\]);", h, re.S).group(1))
    dec = json.load(open(DEC))
    imgkey = next(k for k, v in crops[0].items() if isinstance(v, str) and len(v) > 200)
    out = []
    for i, d in enumerate(dec):
        if i >= len(crops) or d.get("font") in (None, "unclear"): continue
        b = crops[i][imgkey]; b = b.split(",", 1)[1] if b.startswith("data:") else b
        g = np.asarray(Image.open(io.BytesIO(base64.b64decode(b))).convert("L"), np.uint8)
        out.append((g, d.get("text", ""), d["font"]))
    return out

def deslant(m):
    H, W = m.shape; mm = (m > 0).astype(np.float32); best_a, best_v = 0.0, -1.0
    for a in np.arange(-0.7, 0.71, 0.05):
        Wt = W + int(abs(a) * H) + 1
        sh = cv2.warpAffine(mm, np.float32([[1, a, -a * H / 2 + abs(a) * H / 2], [0, 1, 0]]), (Wt, H))
        v = float(sh.sum(0).var())
        if v > best_v: best_v, best_a = v, a
    return best_a

def wfeats(gray, text):
    m = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    ys, xs = np.nonzero(m)
    if len(xs) < 20: return None
    h = ys.max() - ys.min() + 1; w = xs.max() - xs.min() + 1
    sl = deslant(m); dens = len(xs) / (h * w)
    dig = sum(c.isdigit() for c in text) / max(1, sum(c.isalnum() for c in text))
    dt = cv2.distanceTransform(m, cv2.DIST_L2, 3); sw = dt[dt > 0]
    swm = float(sw.mean()) / h if len(sw) else 0.0
    swcv = float(sw.std() / (sw.mean() + 1e-6)) if len(sw) else 0.0
    col = m.sum(0).astype(np.float32); col -= col.mean()
    F = np.abs(np.fft.rfft(col * np.hanning(len(col)))) if len(col) > 8 else np.array([0.0])
    rhy = float(F[2:].max() / (F.mean() + 1e-6)) if len(F) > 2 else 0.0
    return [abs(sl), sl, dens, dig, swm, swcv, rhy, float(h)]

def main():
    words = load_words(); n = len(words)
    STYLES = sorted({w[2] for w in words}); SI = {s: i for i, s in enumerate(STYLES)}
    # features + glyphs per word
    X = np.full((n, 8), np.nan); glyphs = defaultdict(list)   # word -> [(letter, unitvec)]
    for wi, (g, t, s) in enumerate(words):
        fe = wfeats(g, t)
        if fe: X[wi] = fe
        letters = [c for c in t if c.isalnum()]
        if len(letters) >= 2:
            gs = force_split(g, len(letters))
            if len(gs) == len(letters):
                for L, gg in zip(letters, gs):
                    v = gg.astype(np.float32).ravel(); v /= (np.linalg.norm(v) + 1e-6)
                    glyphs[wi].append((L.upper(), v, gg))
    y = np.array([SI[w[2]] for w in words])
    rng = np.random.RandomState(0); order = rng.permutation(n); folds = np.array_split(order, 5)
    from sklearn.ensemble import RandomForestClassifier
    rf_p = np.zeros((n, len(STYLES))); kn_p = np.zeros((n, len(STYLES))); has = np.zeros(n, bool)
    for fi in range(5):
        te = folds[fi]; tr = np.concatenate([folds[j] for j in range(5) if j != fi])
        trf = [i for i in tr if not np.isnan(X[i]).any()]
        clf = RandomForestClassifier(300, min_samples_leaf=2, random_state=0, class_weight="balanced").fit(X[trf], y[trf])
        for i in te:
            if np.isnan(X[i]).any(): continue
            rf_p[i] = clf.predict_proba(X[i:i + 1])[0]
        # kNN: bank from TRAIN words
        bank = defaultdict(list)
        for wi in tr:
            for L, v, _ in glyphs.get(wi, []): bank[L].append((v, y[wi]))
        for i in te:
            gl = glyphs.get(i, [])
            if not gl: continue
            dist = np.zeros(len(STYLES))
            for L, v, gg in gl:
                cand = bank.get(L, [])
                if not cand: continue
                Mn = np.stack([c[0] for c in cand]); r = sims_row(gg, Mn)
                for t in np.argsort(-r)[:K]: dist[cand[t][1]] += 1
            if dist.sum() > 0: kn_p[i] = dist / dist.sum(); has[i] = True
    val = has & ~np.isnan(X).any(1)
    rf_pred = rf_p.argmax(1); kn_pred = kn_p.argmax(1)
    ens = (rf_p + kn_p); ens_pred = ens.argmax(1)
    rf_ok = (rf_pred == y); kn_ok = (kn_pred == y); en_ok = (ens_pred == y)
    m = val
    print(f"evaluable words: {m.sum()}   classes={STYLES}")
    print(f"  RF acc:        {rf_ok[m].mean():.3f}")
    print(f"  kNN acc:       {kn_ok[m].mean():.3f}")
    print(f"  agreement (RF==kNN pred): {(rf_pred==kn_pred)[m].mean():.3f}")
    print(f"  ORACLE (either correct):  {(rf_ok|kn_ok)[m].mean():.3f}   <- ceiling any combiner can reach")
    print(f"  ENSEMBLE (prob-sum) acc:  {en_ok[m].mean():.3f}")
    # where do they differ? complementarity breakdown
    both = (rf_ok & kn_ok)[m].mean(); neither = (~rf_ok & ~kn_ok)[m].mean()
    rf_only = (rf_ok & ~kn_ok)[m].mean(); kn_only = (~rf_ok & kn_ok)[m].mean()
    print(f"\n  both-right {both:.2f}  RF-only {rf_only:.2f}  kNN-only {kn_only:.2f}  both-wrong {neither:.2f}")
    print(f"  -> {rf_only+kn_only:.2f} of words are gettable by exactly one method (complementary error mass)")

if __name__ == "__main__":
    main()
