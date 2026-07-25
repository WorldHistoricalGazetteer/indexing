"""Is the descriptor throwing away SLANT — the one cue a human uses to split upright from italic serif?

With 131 verified `upright·solid·serif` and 161 `italic·solid·serif` examples, the binding constraint is no
longer data (the class went 0.071 -> 0.382 when it got examples). It is that the backbone descriptor is a
GLOBAL MEAN-POOL over feature maps, and mean-pooling is orientation-blind by construction: it sums evidence
over spatial positions and discards the consistent lean that distinguishes an italic from an upright.

Slant, by contrast, is trivially measurable from the pixels — a second-moment shear of the ink in the already
de-rotated crop. This tests whether appending it to the 896-d descriptor recovers the split.

The measurement is honest about its own confound: the verified labels came from cards the lexicon proposed as
one category, so they concentrate in a single confusable neighbourhood and are NOT a random sample of the
corpus. Numbers here are hard-case numbers, and the comparison that means anything is backbone vs backbone+slant
on the SAME labels, not either against the corpus-wide figure.

    python slant_fusion.py --labels "pool_labels_round (4).json"     # mapreader env (needs the tile cache)
"""
import argparse, glob, json, os, sys, numpy as np, cv2
from collections import Counter

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate

PINS = "/vast/ishi/gb1900/edition/pins"


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def slant_features(crop):
    """Shear of the ink distribution, from central second moments of the de-rotated crop.

    mu11/mu02 is the horizontal displacement per unit height — exactly the italic lean, and zero for an
    upright face whatever its serifs or weight. Ink is taken as darkness above the Otsu threshold so the
    measure follows the strokes rather than the paper.
    """
    g = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    if g.size < 80:
        return None
    _, ink = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if ink.sum() == 0:
        return None
    m = cv2.moments(ink, binaryImage=True)
    if m["m00"] <= 0 or m["mu02"] <= 1e-6:
        return None
    shear = m["mu11"] / m["mu02"]                       # dx per unit dy — the italic lean
    elong = m["mu20"] / m["mu02"] if m["mu02"] > 0 else 0.0
    # Column-wise stroke lean: for each ink row, the mean x; its slope against y is a second, more local
    # estimate that survives descenders better than a whole-blob moment.
    ys, xs = np.nonzero(ink)
    slope = 0.0
    if len(ys) > 20 and ys.std() > 1:
        slope = float(np.polyfit(ys, xs, 1)[0])
    return np.array([shear, np.tanh(elong / 4.0), np.tanh(slope), abs(shear)], np.float32)


def norm(X):
    X = np.asarray(X, np.float32)
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)


def loo_maxsim(X, S, per_class=False):
    X = norm(X)
    sl = sorted(set(S))
    cols = {s: np.where(S == s)[0] for s in sl}
    sim = X @ X.T
    np.fill_diagonal(sim, -2)
    ok = 0
    hit = {s: 0 for s in sl}
    for i in range(len(X)):
        sc = [sim[i, cols[s][cols[s] != i]].max() if len(cols[s][cols[s] != i]) else -9 for s in sl]
        good = sl[int(np.argmax(sc))] == S[i]
        ok += good
        hit[S[i]] += good
    if per_class:
        return ok / len(X), {s: (hit[s], len(cols[s])) for s in sl}
    return ok / len(X)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--bank", default=f"{PINS}/desc/shard_*.npz")
    ap.add_argument("--pins-dir", default=PINS)
    ap.add_argument("--weight", type=float, default=1.0, help="L2 weight of the slant block after normalising")
    ap.add_argument("--out", default=f"{PINS}/slant_fusion.json")
    a = ap.parse_args()

    lab = json.load(open(a.labels))
    verified = {key(x["gcx"], x["gcy"]): x["sig"] for x in lab if x.get("sig")}

    polys = {}
    for f in sorted(glob.glob(f"{a.pins_dir}/pins_*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = key(r["gcx"], r["gcy"])
            if k in verified and k not in polys:
                polys[k] = r.get("line_gpoly") or r.get("gpoly")

    D, SL, S = [], [], []
    seen = set()
    for f in sorted(glob.glob(a.bank)):
        d = np.load(f, allow_pickle=True)
        for i in range(len(d["desc"])):
            k = key(d["gcx"][i], d["gcy"][i])
            if k not in verified or k in seen or k not in polys:
                continue
            crop = derotate({"gpoly": polys[k]})
            if crop is None or crop.size < 80:
                continue
            sf = slant_features(crop)
            if sf is None:
                continue
            seen.add(k)
            D.append(d["desc"][i].astype(np.float32))
            SL.append(sf)
            S.append(verified[k])
    D = np.array(D)
    SL = np.array(SL)
    S = np.array(S)
    print(f"{len(D)} verified labels with both descriptor and slant "
          f"({len(set(S))} sigs, majority {max(Counter(S).values())/len(S):.2f})", flush=True)

    # Is the raw slant even different between the two serif classes? If not, fusing it cannot help and the
    # explanation is wrong — check that before reading any accuracy.
    for s in ("upright·solid·serif", "italic·solid·serif"):
        m = S == s
        if m.sum():
            print(f"  {s:26s} n={int(m.sum()):<4d} shear median {np.median(SL[m,0]):+.3f} "
                  f"IQR [{np.percentile(SL[m,0],25):+.3f},{np.percentile(SL[m,0],75):+.3f}]", flush=True)

    base, per_b = loo_maxsim(D, S, per_class=True)
    fused = np.hstack([norm(D), a.weight * norm(SL)])
    fus, per_f = loo_maxsim(fused, S, per_class=True)
    slonly, per_s = loo_maxsim(SL, S, per_class=True)

    print(f"\nmaxsim-LOO on the SAME {len(S)} verified labels:", flush=True)
    print(f"  backbone 896-d            {base:.3f}", flush=True)
    print(f"  slant only (4-d)          {slonly:.3f}", flush=True)
    print(f"  backbone + slant          {fus:.3f}", flush=True)
    print("\nper signature (backbone -> fused):", flush=True)
    for s in sorted(per_b, key=lambda z: -per_b[z][1]):
        hb, n = per_b[s]
        hf = per_f[s][0]
        print(f"  {s:32s} n={n:<4d} {hb/n:.3f} -> {hf/n:.3f}", flush=True)

    json.dump(dict(n=len(S), backbone=base, slant_only=slonly, fused=fus,
                   per_class_backbone={k: list(v) for k, v in per_b.items()},
                   per_class_fused={k: list(v) for k, v in per_f.items()}),
              open(a.out, "w"), indent=2, ensure_ascii=False)
    print(f"\nwrote {a.out}\nSLANTDONE", flush=True)


if __name__ == "__main__":
    main()
