"""Does a LEXICALLY-HARVESTED reference type real lettering as well as human labels do?

If yes, the data-scarcity wall is gone: weak supervision scales to any face whose OS category has an
unambiguous vocabulary, and the starved faces stop being starved.

THE TEST SET is `font_testset_decisions_1.json` — 225 human decisions on the COARSE axis (italic 114,
upright 45, blackletter 33, plus numeral/unclear which are dropped). It is not the 38 face-level anchors:
mapped onto the two harvested faces those give 12 items over 2 classes, which cannot decide anything, and
reporting a number from them would be false precision.

WHAT IS AND IS NOT ESTABLISHED. The coarse axis conflates faces — "upright" covers Upright-Solid-Serif
(woods, parish churches) and Upright-Solid-Plain (roads, railways, Roman antiquities) alike. So a good
result here shows the harvest picks genuinely blackletter / upright / italic lettering; it does NOT show
the finer face distinction, for which no adequately-sized human set exists. Said plainly rather than
quietly conflated.

TWO REFERENCES, ONE SPACE, ONE TEST SET — the only way the comparison means anything:

    baseline   leave-one-out within the 225 human items          the instrument's ceiling on this data
    harvested  the SAME 225 typed using ONLY harvested anchors   what weak supervision achieves

Both in the backbone descriptor space, both on the same items. A raw comparison against the historical
0.776 would be cross-instrument (that figure is same-letter kNN in the SSL space) and is quoted only for
context.

SPACE CONTROL. Harvested crops come from the tile corpus derotated by minimum-area rectangle; the test
boxes are cropped by the same `derotate`, so they DO share a code path — but the control is reported anyway:
if harvested anchors sit in a different region of the space from the test items, nearest-neighbour distances
will be systematically larger than within-test distances, and the accuracy figure is then meaningless
rather than merely low.

    python anchor_decisive_test.py --wdesc labels/wdesc --out labels/decisive_test.json
"""
import argparse, glob, json, os, sys
from collections import Counter

import numpy as np

COARSE = {"Blackletter": "blackletter", "Upright-Solid-Serif": "upright",
          "Italic-Solid-Serif": "italic"}
KEEP = {"italic", "upright", "blackletter"}


def unit(X):
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)


def knn_predict(ref, ref_y, test, k=5):
    S = test @ ref.T
    idx = np.argsort(-S, axis=1)[:, :k]
    out, marg = [], []
    for r, row in enumerate(idx):
        votes = Counter(ref_y[j] for j in row)
        top, n = votes.most_common(1)[0]
        out.append(top)
        marg.append(float(S[r, row[0]]))
    return np.array(out), np.array(marg)


def loo_predict(X, y, k=5):
    S = X @ X.T
    np.fill_diagonal(S, -np.inf)
    idx = np.argsort(-S, axis=1)[:, :k]
    return np.array([Counter(y[j] for j in row).most_common(1)[0][0] for row in idx])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wdesc", default="labels/wdesc")
    ap.add_argument("--testdesc", default="labels/testset_desc.npz",
                    help="backbone descriptors for the 225 human-labelled test boxes")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default="labels/decisive_test.json")
    a = ap.parse_args()

    z = np.load(a.testdesc, allow_pickle=True)
    Xt, yt = unit(z["desc"].astype(np.float32)), z["font"].astype(str)
    m = np.isin(yt, list(KEEP))
    Xt, yt = Xt[m], yt[m]
    print(f"test set: {len(yt)} human-labelled items  {dict(Counter(yt.tolist()))}")
    chance = max(Counter(yt.tolist()).values()) / len(yt)

    D, F = [], []
    for p in sorted(glob.glob(os.path.join(a.wdesc, "wdesc_*.npz"))):
        w = np.load(p, allow_pickle=True)
        if len(w["desc"]) and w["desc"].ndim == 2:
            D.append(w["desc"].astype(np.float32)); F.append(w["face"].astype(str))
    if not D:
        sys.exit("no harvested descriptors")
    Xh, fh = unit(np.concatenate(D)), np.concatenate(F)
    yh = np.array([COARSE.get(f, f) for f in fh])
    print(f"harvested reference: {len(yh)}  {dict(Counter(yh.tolist()))}")

    base = loo_predict(Xt, yt, a.k)
    acc_base = float((base == yt).mean())
    pred, marg = knn_predict(Xh, yh, Xt, a.k)
    acc_harv = float((pred == yt).mean())

    # Space control: nearest-harvested similarity vs nearest-other-test similarity, same items.
    St = Xt @ Xt.T; np.fill_diagonal(St, -np.inf)
    near_test = St.max(1)
    near_harv = (Xt @ Xh.T).max(1)
    gap = float(np.median(near_test) - np.median(near_harv))

    print()
    print(f"  chance (majority class)                {chance:.3f}")
    print(f"  BASELINE  human-reference LOO          {acc_base:.3f}")
    print(f"  HARVESTED reference, same {len(yt)} items    {acc_harv:.3f}")
    print()
    print(f"  space control: median nearest-neighbour similarity")
    print(f"    test->test      {np.median(near_test):.4f}")
    print(f"    test->harvested {np.median(near_harv):.4f}   gap {gap:+.4f}")
    if gap > 0.05:
        print("    WARNING: harvested anchors sit measurably further from the test items than the test "
              "items sit from each other. Treat the harvested accuracy as a SPACE MISMATCH, not a weak "
              "signal, until the crop conventions are reconciled.")
    print()
    for cls in sorted(KEEP):
        mm = yt == cls
        if mm.sum():
            print(f"  {cls:12s} n={mm.sum():3d}  baseline {(base[mm]==cls).mean():.3f}   "
                  f"harvested {(pred[mm]==cls).mean():.3f}")
    json.dump(dict(n_test=int(len(yt)), chance=chance, baseline_loo=acc_base,
                   harvested=acc_harv, space_gap=gap,
                   per_class={c: dict(n=int((yt == c).sum()),
                                      baseline=float((base[yt == c] == c).mean()),
                                      harvested=float((pred[yt == c] == c).mean()))
                              for c in sorted(KEEP) if (yt == c).sum()}),
              open(a.out, "w"), indent=1)
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
