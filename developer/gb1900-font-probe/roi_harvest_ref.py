"""Augment the ROI reference with lexically-harvested anchors, in ROI space.

The pool-label ROI reference cannot support corpus inference: `numeral` has n=5, `italic·none·serif` n=1
and `blackletter·solid·fancy` n=11. Unweighted voting then drifts to the majority class (80% italic against
an independent 59%), and inverse-frequency weighting explodes the rare classes (14.6% blackletter, 13%
numeral — with blackletter's median slant landing at 4.29°, which is italic's signature, not Gothic's).
Neither is a vote-tuning problem. The reference is too thin.

Blackletter is the class that matters and the one weak supervision validated: harvested anchors scored
0.818 against human labels' 0.788 on the coarse test. So it is rebuilt here from the lexicon at ~400 items
instead of 11, in ROI space so it is comparable with everything else.

The other harvested faces are included for balance. Classes with no adequate support in EITHER source are
dropped rather than carried at n=1 — a class that cannot be estimated should not be offered as an answer.

    python roi_harvest_ref.py --per-face 400 --out labels/roi_reference_aug.npz
"""
import argparse, json, math, os, re, sys, time
from collections import Counter, defaultdict

import numpy as np

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")

HERE = "/vast/ishi/gb1900/probe/font"
SPOT2 = "/vast/ishi/gb1900/edition/spot2"
TILE, MOS = 256, 8
MIN_SUPPORT = 20

FACE_SIG = {"Blackletter": "blackletter·solid·fancy",
            "Upright-Solid-Serif": "upright·solid·serif",
            "Italic-Solid-Serif": "italic·solid·serif"}
FACE_LEX = {
    "Blackletter": {"tumulus", "tumuli", "cairn", "cairns", "barrow", "barrows",
                    "earthwork", "earthworks", "tumbrel"},
    "Upright-Solid-Serif": {"wood", "woods", "copse", "copses", "plantation", "plantations",
                            "covert", "coverts", "shaw"},
    "Italic-Solid-Serif": {"spring", "springs", "well", "wells", "ford", "weir", "sluice",
                           "quarry", "quarries", "issues", "sinks", "site"},
}
W2F = {w: f for f, ws in FACE_LEX.items() for w in ws}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="/vast/ishi/gb1900/edition/gb_stamp_labels.jsonl")
    ap.add_argument("--pool-ref", default=f"{HERE}/labels/roi_reference.npz")
    ap.add_argument("--per-face", type=int, default=400)
    ap.add_argument("--per-term-frac", type=float, default=0.34)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default=f"{HERE}/labels/roi_reference_aug.npz")
    a = ap.parse_args()

    # 1. select candidates, grouped by region so each mosaic is forwarded once
    term_cap = max(1, int(a.per_face * a.per_term_frac))
    per_term, per_face = Counter(), Counter()
    by_region = defaultdict(list)
    with open(a.labels, encoding="utf-8") as f:
        for line in f:
            if all(per_face[k] >= a.per_face for k in FACE_LEX):
                break
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for wd in rec.get("words", []):
                t = re.sub(r"[^a-z]", "", str(wd["text"]).lower())
                face = W2F.get(t)
                if not face or per_face[face] >= a.per_face or per_term[(face, t)] >= term_cap:
                    continue
                by_region[rec["region"]].append((face, wd["poly"]))
                per_face[face] += 1; per_term[(face, t)] += 1
    print(f"selected {sum(per_face.values())} candidates in {len(by_region)} regions: {dict(per_face)}",
          flush=True)

    # 2. ROI descriptors, one mosaic forward per (region, window)
    from face_pass_corpus import load_model, fmaps, roi_concat
    import spot_sheet as S
    model, feat, dev, fmt = load_model()
    D, Y = [], []
    t0 = time.time()
    for n, (tag, items) in enumerate(by_region.items()):
        groups = defaultdict(list)
        for face, poly in items:
            p = np.array(poly, np.float64)
            x0, y0, x1, y1 = p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()
            mx0, my0 = int(x0 // TILE) - 1, int(y0 // TILE) - 1
            groups[(mx0, my0)].append((face, x0, y0, x1, y1))
        for (mx0, my0), rows in groups.items():
            ox, oy = mx0 * TILE, my0 * TILE
            W = H = MOS * TILE
            rows = [r for r in rows if r[1] >= ox and r[2] >= oy and r[3] <= ox + W and r[4] <= oy + H]
            if not rows:
                continue
            img, _ = S.mosaic(mx0, my0, MOS)
            im3 = np.repeat(np.asarray(img.convert("L"), np.uint8)[:, :, None], 3, 2)
            mp = fmaps(model, feat, dev, fmt, im3)
            if not mp:
                continue
            for face, x0, y0, x1, y1 in rows:
                d = roi_concat(mp, (x0 - ox, y0 - oy, x1 - ox, y1 - oy), (H, W))
                if d is None:
                    continue
                D.append(d.astype(np.float32)); Y.append(FACE_SIG[face])
        if (n + 1) % 100 == 0:
            print(f"  {n+1}/{len(by_region)} regions, {len(D)} descriptors ({time.time()-t0:.0f}s)",
                  flush=True)
    print(f"harvested {len(D)} ROI descriptors: {dict(Counter(Y))}", flush=True)

    # 3. merge with the pooled labels, then drop classes nobody can estimate
    z = np.load(a.pool_ref, allow_pickle=True)
    PX, PY = z["desc"].astype(np.float32), z["sig"].astype(str)
    X = np.concatenate([PX, np.array(D, np.float32)]) if D else PX
    Yy = np.concatenate([PY, np.array(Y)]) if D else PY
    cnt = Counter(Yy.tolist())
    keep = {c for c, n in cnt.items() if n >= MIN_SUPPORT}
    dropped = {c: n for c, n in cnt.items() if c not in keep}
    m = np.isin(Yy, list(keep))
    X, Yy = X[m], Yy[m]
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    print(f"\nmerged reference {len(Yy)} over {len(keep)} classes; dropped {dropped} "
          f"(< {MIN_SUPPORT} support)")
    for c, n in Counter(Yy.tolist()).most_common():
        print(f"    {c:26s} {n:5d}")

    # 4. LOO with mild (sqrt) balancing — full inverse-frequency is what exploded the rare classes
    RN = Counter(Yy.tolist())
    w = np.array([1.0 / math.sqrt(RN[y]) for y in Yy], np.float32)
    S_ = X @ X.T
    np.fill_diagonal(S_, -np.inf)
    idx = np.argsort(-S_, axis=1)[:, :a.k]
    pred = []
    for row in idx:
        acc = defaultdict(float)
        for j in row:
            acc[Yy[j]] += w[j]
        pred.append(max(acc.items(), key=lambda kv: kv[1])[0])
    pred = np.array(pred)
    acc = float((pred == Yy).mean())
    coarse = np.array([s.split("·")[0] for s in Yy])
    cp = np.array([s.split("·")[0] for s in pred])
    print(f"\n  LOO (sqrt-balanced) signature {acc:.3f}   coarse {float((cp==coarse).mean()):.3f}")
    for c, n in RN.most_common():
        mm = Yy == c
        print(f"    {c:26s} n={n:5d}  recall {float((pred[mm]==c).mean()):.3f}")

    np.savez_compressed(a.out, desc=X.astype(np.float16), sig=Yy)
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
