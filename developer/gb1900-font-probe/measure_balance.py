"""Is the descriptor blind to the rare faces, or merely outvoted by the common ones?

The unbalanced readout says Italic-Outline-Serif scores 0.333 against Upright-Solid-Plain's 0.956, which
invites the conclusion that the backbone cannot see outline lettering. That conclusion is not yet earned.
maxsim-LOO takes each class's single best neighbour, so a class with 297 members has 297 chances to throw up a
spuriously close match and a class with 7 has 7. The rare faces are competing against far more draws than they
field, and losing on a measure that is partly counting anchors.

So the same anchors are scored again with every class capped at the same size, averaged over repeated random
draws. If a face recovers under balancing, the descriptor CAN see it and the anchor set is simply too thin. If
it stays near chance, the descriptor genuinely cannot, and no amount of further labelling in that face will
help — which would be worth knowing before spending another session on it.

Chance is reported alongside, because with 9 balanced classes it is 0.111 and a "0.333" reads very differently
against that than against the 0.44 majority baseline of the unbalanced set.

    python measure_balance.py --bank /vast/.../anchor_desc_hisam.npz
"""
import argparse
from collections import Counter, defaultdict
import numpy as np


def norm(X):
    X = X.astype(np.float32)
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)


def maxsim_loo(X, S, idx=None):
    """Leave-one-out over `idx` only, but matching against all of X (so a capped run still uses its own pool).

    Returns per-class accuracy plus the confusion each class loses to, which is the diagnostic that matters:
    "Upright-Outline-Plain is read as Upright-Solid-Plain" says the descriptor is missing the FILL, whereas
    scattered confusion would say it is missing everything.
    """
    idx = np.arange(len(X)) if idx is None else np.asarray(idx)
    sim = X[idx] @ X.T
    classes = sorted(set(S))
    cols = {c: np.where(S == c)[0] for c in classes}
    hits, tot = Counter(), Counter()
    conf = defaultdict(Counter)
    for row, i in enumerate(idx):
        best, bestc = -9.0, None
        for c in classes:
            m = cols[c][cols[c] != i]
            if not len(m):
                continue
            v = float(sim[row, m].max())
            if v > best:
                best, bestc = v, c
        tot[S[i]] += 1
        if bestc == S[i]:
            hits[S[i]] += 1
        else:
            conf[S[i]][bestc] += 1
    return hits, tot, conf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", default="/vast/ishi/gb1900/edition/spot/anchor_desc_hisam.npz")
    ap.add_argument("--column", default="desc_mr")
    ap.add_argument("--cap", type=int, default=20, help="anchors per class in the balanced run")
    ap.add_argument("--draws", type=int, default=25)
    ap.add_argument("--min-n", type=int, default=3, help="classes below this cannot be estimated at all")
    ap.add_argument("--min-shared", type=int, default=10,
                    help="anchors a face needs from EACH origin to enter the same-face comparison")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    z = np.load(a.bank, allow_pickle=True)
    X = norm(z[a.column])
    S = z["sigs"].astype(str)
    org = z["origin"].astype(str) if "origin" in z.files else np.array(["?"] * len(S))
    cnt = Counter(S)
    usable = sorted([c for c in cnt if cnt[c] >= a.min_n])
    print(f"{len(S)} anchors, {len(cnt)} faces, {len(usable)} with >= {a.min_n} anchors")
    print(f"unbalanced majority baseline {max(cnt.values())/len(S):.3f}   "
          f"balanced chance {1/len(usable):.3f}\n")

    keep = np.isin(S, usable)
    Xu, Su, Ou = X[keep], S[keep], org[keep]
    h, t, conf = maxsim_loo(Xu, Su)
    print(f"{'face':26s} {'n':>4} {'unbal':>7} {'balanced':>9} {'±':>6}   confused with")
    rng = np.random.default_rng(a.seed)
    cols = {c: np.where(Su == c)[0] for c in usable}

    bal = defaultdict(list)
    for _ in range(a.draws):
        sub = np.concatenate([rng.choice(cols[c], min(a.cap, len(cols[c])), replace=False) for c in usable])
        hb, tb, _ = maxsim_loo(Xu[sub], Su[sub])
        for c in usable:
            if tb[c]:
                bal[c].append(hb[c] / tb[c])

    overall_b = []
    for c in usable:
        b = np.array(bal[c]) if bal[c] else np.array([np.nan])
        overall_b.append(np.nanmean(b))
        top = ", ".join(f"{k} {v}" for k, v in conf[c].most_common(2)) or "—"
        print(f"  {c:24s} {t[c]:>4} {h[c]/max(1,t[c]):>7.3f} {np.nanmean(b):>9.3f} "
              f"{np.nanstd(b):>6.3f}   {top}")
    print(f"\n  {'MEAN over faces':24s} {'':>4} "
          f"{np.mean([h[c]/max(1,t[c]) for c in usable]):>7.3f} {np.nanmean(overall_b):>9.3f}")

    print("\nby crop origin (unbalanced, usable faces only):")
    for o in sorted(set(Ou)):
        i = np.where(Ou == o)[0]
        hh, tt, _ = maxsim_loo(Xu, Su, i)
        print(f"  {o:12s} n={len(i):>4}  {sum(hh.values())/max(1,sum(tt.values())):.3f}")

    # The origin gap is confounded: nearly every rare-face anchor is a big-font row, and the rare faces are
    # the hard ones — so a raw gap could be composition rather than crop convention. Restricting both origins
    # to the faces they SHARE separates the two. If the gap survives here it is the convention; if it closes,
    # the big-font rows are simply where the difficult faces live, and mixing conventions costs nothing.
    shared = [c for c in usable
              if (Ou[Su == c] == "mapreader").sum() >= a.min_shared
              and (Ou[Su == c] == "hisam-line").sum() >= a.min_shared]
    if shared:
        m = np.isin(Su, shared)
        Xs, Ss, Os = Xu[m], Su[m], Ou[m]
        print(f"\nsame-face comparison, faces held by both origins ({', '.join(shared)}):")
        for o in sorted(set(Os)):
            i = np.where(Os == o)[0]
            hh, tt, _ = maxsim_loo(Xs, Ss, i)
            print(f"  {o:12s} n={len(i):>4}  {sum(hh.values())/max(1,sum(tt.values())):.3f}")
    else:
        print(f"\nno face has >= {a.min_shared} anchors from both origins — gap not separable")
    print("BALANCEDONE")


if __name__ == "__main__":
    main()
