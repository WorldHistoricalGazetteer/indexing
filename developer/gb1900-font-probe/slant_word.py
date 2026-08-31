"""Whole-word slant, and whether it repairs the descriptor's one systematic weakness.

Upright-Solid-Serif scores 0.314 balanced and loses 23 of its 65 anchors to Italic-Solid-Serif. That is the
worst confusion in the inventory and it is a single geometric property — lean — which the backbone descriptor
is built to discard: it is a global mean-pool over feature maps, and mean-pooling sums evidence across
positions, so a consistent lean averages away.

Two estimators, because the obvious one is not the right one:

* SECOND MOMENT (mu11/mu02) is what an earlier pass used. It measures the lean of the whole ink cloud, which
  in a word is driven by where the ascenders and descenders happen to fall — "Hall" and "yell" differ in it
  while sharing a slant. Kept as the baseline precisely because it was the previous attempt.
* SHEAR SEARCH is the whole-word measure. A word is sheared through a range of angles and the one that makes
  the vertical projection profile most concentrated wins: at the correct de-shear the vertical strokes line up
  into tall narrow columns, and at any other angle they smear. It reads STROKE direction rather than ink
  layout, and it needs no letter boundaries — which matters, because letter segmentation on this corpus
  failed (4.7% of words split on their own ink gaps).

Reported three ways: how well slant alone separates upright from italic, how it does on the hard pair
specifically, and whether appending it to the 896-d descriptor moves the balanced leave-one-out. The last is
the only one that decides anything — a cue can separate classes on its own and still add nothing to a
descriptor that already encodes it.

    python slant_word.py --qc slant_qc.html
"""
import argparse, glob, json, os, sys
from collections import Counter, defaultdict
import numpy as np
import cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate
from segment_spots import binarise

SPOT = "/vast/ishi/gb1900/edition/spot"


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def shear_slant(bw, lo=-0.8, hi=0.8, step=0.02):
    """Shear angle (degrees) that most concentrates the vertical projection profile.

    Score is sum of squared column sums: it is maximised when ink stacks into few tall columns, i.e. when the
    vertical strokes are upright. Total ink is preserved under shear, so the scores are comparable across
    angles without normalisation.

    Returns (degrees, sharpness) where sharpness is the peak's prominence over the mean score — near zero for
    a crop with no coherent stroke direction, which is the honest signal for "this measurement means nothing
    here" and lets a caller discard it rather than treat noise as an upright reading.
    """
    ys, xs = np.where(bw > 0)
    if len(xs) < 40:
        return np.nan, 0.0
    sub = bw[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = sub.shape
    if h < 8 or w < 8:
        return np.nan, 0.0
    yc = h / 2.0
    pad = int(abs(hi) * h) + 2
    canvas = np.zeros((h, w + 2 * pad), np.uint8)
    canvas[:, pad:pad + w] = sub
    scores, angles = [], []
    for s in np.arange(lo, hi + 1e-9, step):
        M = np.float32([[1, s, -s * yc], [0, 1, 0]])
        sh = cv2.warpAffine(canvas, M, (canvas.shape[1], h), flags=cv2.INTER_NEAREST)
        col = (sh > 0).sum(0).astype(np.float64)
        scores.append(float((col ** 2).sum()))
        angles.append(s)
    scores = np.array(scores)
    best = int(scores.argmax())
    sharp = float((scores[best] - scores.mean()) / (scores.std() + 1e-9))
    # The shear that CORRECTS the lean is the negative of the lean itself.
    return float(np.degrees(np.arctan(-angles[best]))), sharp


def moment_slant(bw):
    """mu11/mu02 — the previous attempt's estimator, kept as the baseline to beat."""
    m = cv2.moments((bw > 0).astype(np.uint8), binaryImage=True)
    if m["m00"] < 40 or m["mu02"] < 1e-6:
        return np.nan
    return float(np.degrees(np.arctan(m["mu11"] / m["mu02"])))


def auc(pos, neg):
    """Mann-Whitney AUC — threshold-free, so it does not flatter a cue by tuning a cut point on the test set.

    Returned ORIENTED: an AUC below 0.5 means the cue separates the classes perfectly well but with the sign
    reversed, which is the expected case here (italic leans one way, so its values are the LOWER ones). A raw
    0.121 reads as catastrophic when it is in fact 0.879 discrimination, so the direction is reported
    separately rather than left to trip up whoever reads the log.
    """
    pos, neg = np.asarray(pos), np.asarray(neg)
    pos, neg = pos[~np.isnan(pos)], neg[~np.isnan(neg)]
    if not len(pos) or not len(neg):
        return np.nan, 0, 0
    allv = np.concatenate([pos, neg])
    r = allv.argsort().argsort().astype(float) + 1
    rp = r[: len(pos)].sum()
    A = float((rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))
    return max(A, 1 - A), ("pos<neg" if A < 0.5 else "pos>neg"), len(pos), len(neg)


def norm(X):
    X = X.astype(np.float32)
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)


def maxsim_acc(X, S, cap=None, draws=1, seed=0):
    """Balanced maxsim-LOO, same measure as measure_balance so the numbers are directly comparable."""
    classes = sorted(set(S))
    rng = np.random.default_rng(seed)
    cols_all = {c: np.where(S == c)[0] for c in classes}
    out = []
    for _ in range(draws):
        if cap:
            idx = np.concatenate([rng.choice(cols_all[c], min(cap, len(cols_all[c])), replace=False)
                                  for c in classes])
        else:
            idx = np.arange(len(S))
        Xs, Ss = X[idx], S[idx]
        sim = Xs @ Xs.T
        np.fill_diagonal(sim, -2)
        cols = {c: np.where(Ss == c)[0] for c in classes}
        per = {}
        for c in classes:
            ii = cols[c]
            if not len(ii):
                continue
            ok = 0
            for i in ii:
                sc = [sim[i, cols[t][cols[t] != i]].max() if len(cols[t][cols[t] != i]) else -9
                      for t in classes]
                ok += (classes[int(np.argmax(sc))] == c)
            per[c] = ok / len(ii)
        out.append(per)
    return {c: float(np.mean([o[c] for o in out if c in o])) for c in classes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="labels/pool_labels_faced.json")
    ap.add_argument("--boxes", default=f"{SPOT}/boxes_*.jsonl")
    ap.add_argument("--bank", default=f"{SPOT}/anchor_desc_hisam.npz")
    ap.add_argument("--min-sharp", type=float, default=1.0,
                    help="discard a slant reading whose shear peak is this weak — no coherent stroke direction")
    ap.add_argument("--weights", type=float, nargs="*", default=[0.25, 0.5, 1.0, 2.0],
                    help="L2 weight of the slant block appended to the unit-norm descriptor")
    ap.add_argument("--cap", type=int, default=20)
    ap.add_argument("--draws", type=int, default=25)
    ap.add_argument("--out", default=f"{SPOT}/slant_word.npz")
    a = ap.parse_args()

    lab = [l for l in json.load(open(a.labels)) if l.get("face")]
    want = {key(l["gcx"], l["gcy"]): l for l in lab}
    boxes = {}
    for f in sorted(glob.glob(a.boxes)):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = key(r["gcx"], r["gcy"])
            if k in want and k not in boxes:
                boxes[k] = r
        if len(boxes) >= len(want):
            break
    print(f"{len(lab)} anchors, {len(boxes)} matched to MapReader boxes", flush=True)

    gk, gf, sh, mo, shp = [], [], [], [], []
    miss = 0
    for k, l in want.items():
        r = boxes.get(k) or ({"gpoly": l["gpoly"]} if l.get("gpoly") else None)
        if r is None:
            miss += 1
            continue
        crop = derotate(r)
        if crop is None or crop.size < 200:
            miss += 1
            continue
        bw = binarise(crop)
        s, p = shear_slant(bw)
        gk.append(k)
        gf.append(l["face"])
        sh.append(s)
        mo.append(moment_slant(bw))
        shp.append(p)
    F = np.array(gf, dtype=object).astype(str)
    SH = np.array(sh, float)
    MO = np.array(mo, float)
    SP = np.array(shp, float)
    weak = np.isnan(SH) | (SP < a.min_sharp)
    print(f"{len(F)} measured ({miss} unusable); {int(weak.sum())} discarded as having no coherent "
          f"stroke direction (sharpness < {a.min_sharp})", flush=True)

    print(f"\n{'face':26s} {'n':>4} {'shear slant':>22} {'moment slant':>16}")
    for f in sorted(set(F)):
        m = (F == f) & ~weak
        if not m.sum():
            print(f"  {f:24s} {int((F==f).sum()):>4}   (all readings discarded)")
            continue
        print(f"  {f:24s} {int(m.sum()):>4}   median {np.nanmedian(SH[m]):>7.1f}deg  "
              f"IQR {np.nanpercentile(SH[m],75)-np.nanpercentile(SH[m],25):>5.1f}   "
              f"median {np.nanmedian(MO[m]):>7.1f}deg")

    up = np.array([f.startswith("Upright") for f in F])
    it = np.array([f.startswith("Italic") for f in F])
    print("\nseparating UPRIGHT from ITALIC (all faces whose name declares it):")
    for nm, V in (("shear ", SH), ("moment", MO)):
        A, d, npos, nneg = auc(V[it & ~weak], V[up & ~weak])
        print(f"  {nm} AUC {A:.3f} ({d})   (italic n={npos}, upright n={nneg})")

    hard_i = (F == "Italic-Solid-Serif") & ~weak
    hard_u = (F == "Upright-Solid-Serif") & ~weak
    print("\nthe hard pair — Upright-Solid-Serif vs Italic-Solid-Serif:")
    for nm, V in (("shear ", SH), ("moment", MO)):
        A, d, npos, nneg = auc(V[hard_i], V[hard_u])
        print(f"  {nm} AUC {A:.3f} ({d})   (italic n={npos}, upright n={nneg})")

    np.savez_compressed(a.out, gcx=np.array([k[0] for k in gk]), gcy=np.array([k[1] for k in gk]),
                        face=F.astype(object), shear=SH, moment=MO, sharp=SP)
    print(f"\nwrote {a.out}")

    # The decisive test: does slant ADD anything to a descriptor that may already encode it?
    if not os.path.exists(a.bank):
        print("no descriptor bank — skipping fusion")
        print("SLANTDONE", flush=True)
        return
    z = np.load(a.bank, allow_pickle=True)
    if "gcx" not in z.files:
        print("bank has no gcx/gcy — rebuild it to run the fusion test")
        print("SLANTDONE", flush=True)
        return
    bmap = {key(x, y): i for i, (x, y) in enumerate(zip(z["gcx"], z["gcy"]))}
    rows = [(bmap[k], j) for j, k in enumerate(gk) if k in bmap]
    bi = np.array([r[0] for r in rows])
    si = np.array([r[1] for r in rows])
    X = norm(z["desc_mr"][bi])
    S = z["sigs"].astype(str)[bi]
    sv = SH[si].copy()
    sv[np.isnan(sv) | (SP[si] < a.min_sharp)] = 0.0        # a discarded reading contributes nothing, not noise
    sv = (sv / 30.0).reshape(-1, 1)                        # ~30 deg of lean maps to unit scale
    usable = [c for c in set(S) if (S == c).sum() >= 3]
    m = np.isin(S, usable)
    print(f"\nfusion test on {int(m.sum())} anchors over {len(usable)} faces "
          f"(balanced cap {a.cap}, {a.draws} draws):")
    base = maxsim_acc(X[m], S[m], a.cap, a.draws)
    print(f"  {'descriptor alone':28s} mean over faces {np.mean(list(base.values())):.3f}")
    best = None
    for w in a.weights:
        Xf = norm(np.hstack([X[m], sv[m] * w]))
        per = maxsim_acc(Xf, S[m], a.cap, a.draws)
        mean = float(np.mean(list(per.values())))
        print(f"  {'+ slant (weight %.2f)' % w:28s} mean over faces {mean:.3f}")
        if best is None or mean > best[1]:
            best = (w, mean, per)
    print(f"\nper-face, descriptor alone vs + slant (weight {best[0]:.2f}):")
    for c in sorted(base):
        d = best[2][c] - base[c]
        print(f"  {c:26s} {base[c]:.3f} -> {best[2][c]:.3f}  {d:+.3f}")
    print("SLANTDONE", flush=True)


if __name__ == "__main__":
    main()
