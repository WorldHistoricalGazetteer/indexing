"""Phase C — ENSEMBLE: raster same-letter kNN (shape) + MapReader SIZE + a few OpenCV features.

The raster-kNN (0.776) uses shape only; the engineered-feature model (0.663) is driven by SIZE, which the
kNN ignores. They're complementary -> combine. Per word: the kNN's same-letter match score to each font
(leave-own-word-out) + cap-height ground-m + mean fill/complexity -> HistGradientBoosting, 5-fold by word.
Tests whether SG's "feed more clues" idea beats 0.776 via ensembling rather than replacing.

    /vast/ishi/envs/boundary/bin/python feat_ensemble.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import json, math, numpy as np, cv2
from collections import Counter
from build_alphabet import force_split
from make_font_testset_v2 import derotate
from discrim_test import sims_row
from feat_disc import glyph_feats, lidx
from sklearn.ensemble import HistGradientBoostingClassifier

DEC = "/vast/ishi/gb1900/probe/font/font_testset_decisions_1.json"
BOXES = "/vast/ishi/gb1900/edition/spot/font_testset_v2_boxes.json"
STYLES = ["italic", "blackletter", "upright"]; SI = {s: i for i, s in enumerate(STYLES)}; N17 = 2 ** 17
def mpp(lat): return 156543.03392 * math.cos(math.radians(lat)) / N17

def main():
    dec = json.load(open(DEC)); font_by_i = {x["i"]: x["font"] for x in dec if x.get("font")}
    samp = json.load(open(BOXES))
    words = []                                          # (font, size_m, [(letter,cap,raster,feats)])
    for i, r in enumerate(samp):
        f = font_by_i.get(i)
        if f not in STYLES or r["text"] != dec[i]["text"]: continue
        patch = derotate(r)
        if patch is None: continue
        letters = [c for c in r["text"] if c.isalnum()]
        gs = force_split(patch, len(letters))
        if len(gs) != len(letters): continue
        poly = np.array(r["gpoly"], np.float32); (_, _), (rw, rh), _ = cv2.minAreaRect(poly)
        size_m = min(rw, rh) * mpp(r["lat"]); gl = []
        for k, g in enumerate(gs):
            fe = glyph_feats(g)
            if fe is not None: gl.append((letters[k].upper(), letters[k].isupper(), g, fe))
        if gl: words.append((f, size_m, gl))
    print(f"words {len(words)} glyphs {sum(len(g) for _,_,g in words)} fonts {dict(Counter(f for f,_,_ in words))}", flush=True)

    # global glyph matrix for the raster kNN
    caps = [(L, cap, g, wi) for wi, (_, _, gl) in enumerate(words) for (L, cap, g, _) in gl]
    letters = np.array([c[0] for c in caps]); caparr = np.array([c[1] for c in caps]); wid = np.array([c[3] for c in caps])
    M = np.array([c[2].astype(np.float32).ravel() for c in caps], np.float32); M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)

    def knn_scores(wi):
        """mean same-letter best-sim to each font over the word's glyphs, EXCLUDING this word."""
        _, _, gl = words[wi]; acc = {s: [] for s in STYLES}
        for L, cap, g, _ in gl:
            same = (letters == L) & (caparr == cap) & (wid != wi)
            if not same.any(): continue
            r = sims_row(g, M)
            for s in STYLES:
                mask = same & np.isin(wid, [j for j in range(len(words)) if words[j][0] == s])
                if mask.any(): acc[s].append(float(r[mask].max()))
        return [float(np.mean(acc[s])) if acc[s] else 0.0 for s in STYLES]

    X = []; y = []
    for wi, (f, size_m, gl) in enumerate(words):
        ks = knn_scores(wi)
        fills = np.mean([fe[0] for _, _, _, fe in gl]); comp = np.mean([fe[5] for _, _, _, fe in gl])
        shear = np.mean([abs(fe[1]) for _, _, _, fe in gl])
        X.append(ks + [math.log1p(max(0.01, size_m)), fills, comp, shear]); y.append(SI[f])
    X = np.array(X, float); y = np.array(y)
    FEATS = ["knn_italic", "knn_black", "knn_upright", "log_size", "fill", "complexity", "shear"]

    # baseline: raster-kNN alone (argmax of the 3 knn scores)
    knn_pred = X[:, :3].argmax(1); knn_acc = (knn_pred == y).mean()

    rng = np.random.RandomState(0); order = rng.permutation(len(words)); folds = np.array_split(order, 5)
    conf = Counter(); tot = Counter()
    for te in folds:
        te = set(te.tolist()); trm = np.array([i not in te for i in range(len(words))])
        clf = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.08, max_depth=3, l2_regularization=1.0, random_state=0)
        clf.fit(X[trm], y[trm])
        for wi in te:
            pred = STYLES[int(clf.predict(X[wi:wi+1])[0])]; tf = STYLES[y[wi]]
            conf[(tf, pred)] += 1; tot[tf] += 1
    N = sum(tot.values()); acc = sum(conf[(s, s)] for s in STYLES) / max(1, N)
    print(f"\nraster-kNN alone (argmax of knn scores): {knn_acc:.3f}")
    print(f"=== ENSEMBLE kNN+size+features (HGB, 5-fold by word, N={N}) ===")
    print(f"accuracy {acc:.3f}   [raster-kNN 0.776, feats-only 0.663, CNN 0.56]")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>8s}" for s in STYLES) + "  recall")
    for s in STYLES:
        print(f"  {s:10s}" + "".join(f"{conf[(s,d)]:>8d}" for d in STYLES) + f"  {conf[(s,s)]/max(1,tot[s]):.2f}")

if __name__ == "__main__":
    main()
