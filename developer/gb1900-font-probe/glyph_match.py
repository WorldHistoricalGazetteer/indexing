"""Overlay matching between glyphs: does fitting one letterform onto another separate the faces?

This is the instrument the whole plan rests on, so it is measured before anything is built around it. Each
glyph is fitted to each candidate of the SAME character over a small search in rotation and scale — the
letterforms are already de-rotated to their baseline and size-normalised, so what remains is residual slant and
the aspect differences between faces, which is precisely what should discriminate them.

Scored leave-one-out, voting the face from the top-k matches. Two splits, because they answer different things:

  map -> map   the honest test. Both sides are printed-and-scanned map crops, so a number here is what the
               method would do in production.
  CS -> map    the domain question. Templates are engraved Characteristic-Sheet specimens, spots are printed
               and scanned. Eleven of seventeen faces are CS-only, so if this collapses, those faces cannot be
               matched from CS seeds however good the matcher is.

The baseline to beat is not zero but the majority class, printed alongside every score.

    python glyph_match.py --npz labels/alphabet_glyphs_seeded.npz
"""
import argparse, json
from collections import Counter, defaultdict
import numpy as np
import cv2

H, W = 44, 36


def warp(g, ang, scale):
    """Rotate about the centre and scale, keeping the 44x36 frame."""
    M = cv2.getRotationMatrix2D((W / 2.0, H / 2.0), ang, scale)
    return cv2.warpAffine(g, M, (W, H), flags=cv2.INTER_NEAREST, borderValue=0)


def fit(a, b, angles, scales):
    """Best ink-IoU of `a` onto `b` over the transform search. Symmetric enough for our purposes."""
    bb = b > 0
    nb = bb.sum()
    best = 0.0
    for ang in angles:
        for sc in scales:
            w = warp(a, ang, sc) > 0
            inter = np.count_nonzero(w & bb)
            if not inter:
                continue
            union = np.count_nonzero(w) + nb - inter
            if union:
                v = inter / union
                if v > best:
                    best = v
    return best


def run(lib_idx, test_idx, G, CH, FC, angles, scales, k, name, ban_same_word=None, WD=None):
    """Vote each test glyph's face from its top-k same-character matches in the library."""
    ok = tot = 0
    per = defaultdict(lambda: [0, 0])
    conf = Counter()
    for i in test_idx:
        cands = [j for j in lib_idx if j != i and CH[j] == CH[i]]
        if ban_same_word is not None and WD is not None:
            cands = [j for j in cands if WD[j] != WD[i]]      # never score a letter against its own word
        if not cands:
            continue
        scored = sorted(((fit(G[j], G[i], angles, scales), j) for j in cands), reverse=True)[:k]
        if not scored or scored[0][0] <= 0:
            continue
        vote = defaultdict(float)
        for s, j in scored:
            vote[FC[j]] += s
        pred = max(vote, key=vote.get)
        tot += 1
        ok += pred == FC[i]
        per[FC[i]][1] += 1
        per[FC[i]][0] += pred == FC[i]
        if pred != FC[i]:
            conf[(FC[i], pred)] += 1
    if not tot:
        print(f"\n{name}: nothing testable")
        return
    maj = Counter(FC[i] for i in test_idx).most_common(1)[0][1] / len(test_idx)
    print(f"\n{name}: {ok}/{tot} = {ok/tot:.3f}   (majority-class baseline {maj:.3f})")
    for f in sorted(per, key=lambda z: -per[z][1]):
        g, n = per[f]
        print(f"    {f:26s} {g:>3d}/{n:<3d} {g/n:.3f}")
    if conf:
        print("  most common confusions (true -> predicted):")
        for (t, p), n in conf.most_common(5):
            print(f"    {n:>3d}  {t}  ->  {p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="labels/alphabet_glyphs_seeded.npz")
    ap.add_argument("--k", type=int, default=3, help="matches voted per letter")
    ap.add_argument("--rot", type=float, default=12.0, help="+/- degrees searched")
    ap.add_argument("--rot-step", type=float, default=3.0)
    ap.add_argument("--scale", type=float, default=0.12, help="+/- fractional scale searched")
    ap.add_argument("--scale-step", type=float, default=0.06)
    a = ap.parse_args()

    d = np.load(a.npz, allow_pickle=True)
    G = d["glyphs"]
    CH = d["chars"].astype(str)
    FC = d["faces"].astype(str)
    SR = d["source"].astype(str) if "source" in d.files else np.array(["map"] * len(G))
    WD = d["word"]
    angles = np.arange(-a.rot, a.rot + 1e-9, a.rot_step)
    scales = np.arange(1 - a.scale, 1 + a.scale + 1e-9, a.scale_step)
    print(f"{len(G)} glyphs · {len(set(FC))} faces · searching {len(angles)} rotations x {len(scales)} scales")

    mp = [i for i in range(len(G)) if SR[i] == "map"]
    cs = [i for i in range(len(G)) if SR[i] == "CS"]

    # map -> map, excluding a glyph's own word so neighbouring letters of one label cannot vote for themselves
    run(mp, mp, G, CH, FC, angles, scales, a.k, "map -> map (leave-one-WORD-out)", ban_same_word=True, WD=WD)
    # CS -> map: can engraved templates type printed letters?
    run(cs, mp, G, CH, FC, angles, scales, a.k, "CS -> map (cross-domain)")
    # all -> all, the number the production library would give as it stands
    run(list(range(len(G))), list(range(len(G))), G, CH, FC, angles, scales, a.k,
        "all -> all (leave-one-WORD-out)", ban_same_word=True, WD=WD)
    print("\nMATCHDONE")


if __name__ == "__main__":
    main()
