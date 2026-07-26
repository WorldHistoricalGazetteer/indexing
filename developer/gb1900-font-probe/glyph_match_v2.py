"""Representations for glyph matching, compared on the same splits.

Raw ink-IoU scored 0.290 map->map against a 0.234 baseline, and 0.162 CS->map, below chance. The confusions
were not uniform: Upright-Diagonal-Serif -> Upright-Solid-Serif, Italic-Outline-Serif -> Upright-Solid-Ornate.
Those pairs differ in FILL, not in letterform — and size-normalising to a fixed canvas and counting ink overlap
is precisely the operation that throws fill away. An outline O and a solid O carry similar ink after
normalisation; a hatched letter and a solid one likewise.

So the glyph is decomposed along the axes the taxonomy already uses:

  SHAPE   the filled outer form — what letter is this, in what letterform. Outline and hatched glyphs are
          filled in first, so an outline O and a solid O become the same shape, as they should for a match
          that is asking about letterform.
  FILL    how the form is inked: the ink-to-form area ratio, plus the response to oriented openings, which is
          what separates diagonal hatching from horizontal from solid.
  ASPECT  width-to-height of the ink. A face property in its own right (condensed vs expanded), currently
          buried by the letterbox normalisation, and therefore something the transform search wastes its
          budget undoing rather than a feature it can use.

Shape is matched by symmetric CHAMFER distance rather than IoU: near-misses score nearly as well as exact
overlap, which is what a printed-and-scanned letter needs against an engraved template.

    python glyph_match_v2.py --npz labels/alphabet_glyphs_seeded.npz
"""
import argparse, math
from collections import Counter, defaultdict
import numpy as np
import cv2

H, W = 44, 36


def line_se(length, angle_deg):
    se = np.zeros((length, length), np.uint8)
    c = length // 2
    dx, dy = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    cv2.line(se, (int(c - dx * c), int(c - dy * c)), (int(c + dx * c), int(c + dy * c)), 1, 1)
    return se


SES = [line_se(7, a) for a in (0, 45, 90, 135)]


def shape_of(g):
    """Filled outer form: closes small gaps, then fills every external contour."""
    c = cv2.morphologyEx(g, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(c, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = np.zeros_like(g)
    if cnts:
        cv2.drawContours(out, cnts, -1, 255, -1)
    return out


def fill_feats(g, s):
    """How the outer form is inked — the axis a shape match must not be asked to carry."""
    ai, as_ = float((g > 0).sum()), float((s > 0).sum())
    ratio = ai / max(1.0, as_)
    resp = [float(cv2.morphologyEx(g, cv2.MORPH_OPEN, se).sum()) / max(1.0, float(g.sum())) for se in SES]
    # directional anisotropy: hatching is oriented, solid ink is not
    aniso = (max(resp) - min(resp))
    return np.array([ratio] + resp + [aniso], np.float32)


def aspect_of(g):
    ys, xs = np.nonzero(g)
    if not len(xs):
        return 1.0
    return (xs.max() - xs.min() + 1) / max(1.0, (ys.max() - ys.min() + 1))


def warp(g, ang, scale):
    M = cv2.getRotationMatrix2D((W / 2.0, H / 2.0), ang, scale)
    return cv2.warpAffine(g, M, (W, H), flags=cv2.INTER_NEAREST, borderValue=0)


def chamfer(a, b, dt_b):
    """Symmetric mean chamfer, converted to a similarity in (0,1]."""
    ay, ax = np.nonzero(a)
    by, bx = np.nonzero(b)
    if not len(ax) or not len(bx):
        return 0.0
    dt_a = cv2.distanceTransform((a == 0).astype(np.uint8), cv2.DIST_L2, 3)
    d = 0.5 * (float(dt_b[ay, ax].mean()) + float(dt_a[by, bx].mean()))
    return 1.0 / (1.0 + d)


def iou(a, b):
    aa, bb = a > 0, b > 0
    inter = np.count_nonzero(aa & bb)
    if not inter:
        return 0.0
    return inter / (np.count_nonzero(aa) + np.count_nonzero(bb) - inter)


def build(G):
    S = [shape_of(g) for g in G]
    F = np.array([fill_feats(g, s) for g, s in zip(G, S)])
    A = np.array([aspect_of(g) for g in G], np.float32)
    F = (F - F.mean(0)) / (F.std(0) + 1e-6)                # standardise so no single feature dominates
    DT_ink = [cv2.distanceTransform((g == 0).astype(np.uint8), cv2.DIST_L2, 3) for g in G]
    DT_shp = [cv2.distanceTransform((s == 0).astype(np.uint8), cv2.DIST_L2, 3) for s in S]
    return S, F, A, DT_ink, DT_shp


def score(i, j, variant, G, S, F, A, DT_ink, DT_shp, angles, scales, w_fill, w_asp):
    """Best score of glyph i fitted onto glyph j, under one representation."""
    best = 0.0
    src, tgt, dt = (G[i], G[j], DT_ink[j]) if variant in ("iou", "chamfer") else (S[i], S[j], DT_shp[j])
    for ang in angles:
        for sc in scales:
            w = warp(src, ang, sc)
            v = iou(w, tgt) if variant == "iou" else chamfer(w, tgt, dt)
            if v > best:
                best = v
    if variant in ("iou", "chamfer", "shape"):
        return best
    # shape + the axes the shape match deliberately discards
    fd = float(np.linalg.norm(F[i] - F[j]))
    ad = abs(A[i] - A[j])
    return best - w_fill * fd / 10.0 - w_asp * ad


def run(lib, test, name, **kw):
    G, CH, FC, WD = kw["G"], kw["CH"], kw["FC"], kw["WD"]
    ok = tot = 0
    per = defaultdict(lambda: [0, 0])
    conf = Counter()
    for i in test:
        cands = [j for j in lib if j != i and CH[j] == CH[i] and (not kw["low"] or WD[j] != WD[i])]
        if not cands:
            continue
        sc = sorted(((score(i, j, kw["variant"], G, kw["S"], kw["F"], kw["A"], kw["DTi"], kw["DTs"],
                            kw["angles"], kw["scales"], kw["w_fill"], kw["w_asp"]), j) for j in cands),
                    reverse=True)[:kw["k"]]
        if not sc:
            continue
        vote = defaultdict(float)
        for s_, j in sc:
            vote[FC[j]] += max(0.0, s_)
        if not vote or max(vote.values()) <= 0:
            continue
        pred = max(vote, key=vote.get)
        tot += 1
        ok += pred == FC[i]
        per[FC[i]][1] += 1
        per[FC[i]][0] += pred == FC[i]
        if pred != FC[i]:
            conf[(FC[i], pred)] += 1
    maj = Counter(FC[i] for i in test).most_common(1)[0][1] / max(1, len(test))
    return (ok / tot if tot else 0.0), tot, maj, per, conf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="labels/alphabet_glyphs_seeded.npz")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--rot", type=float, default=12.0)
    ap.add_argument("--rot-step", type=float, default=4.0)
    ap.add_argument("--scale", type=float, default=0.12)
    ap.add_argument("--scale-step", type=float, default=0.06)
    ap.add_argument("--w-fill", type=float, default=1.0)
    ap.add_argument("--w-asp", type=float, default=0.5)
    ap.add_argument("--detail", default="shape+fill", help="variant to print per-face detail for")
    a = ap.parse_args()

    d = np.load(a.npz, allow_pickle=True)
    G = d["glyphs"]
    CH = d["chars"].astype(str)
    FC = d["faces"].astype(str)
    SR = d["source"].astype(str) if "source" in d.files else np.array(["map"] * len(G))
    WD = d["word"]
    angles = np.arange(-a.rot, a.rot + 1e-9, a.rot_step)
    scales = np.arange(1 - a.scale, 1 + a.scale + 1e-9, a.scale_step)
    S, F, A, DTi, DTs = build(G)
    mp = [i for i in range(len(G)) if SR[i] == "map"]
    cs = [i for i in range(len(G)) if SR[i] == "CS"]
    allx = list(range(len(G)))
    print(f"{len(G)} glyphs · {len(set(FC))} faces · {len(angles)}x{len(scales)} transforms\n")

    splits = [("map -> map", mp, mp, True), ("CS -> map", cs, mp, False),
              ("all -> all", allx, allx, True)]
    print(f"  {'variant':12s} " + "  ".join(f"{n:>12s}" for n, _, _, _ in splits))
    results = {}
    for variant in ("iou", "chamfer", "shape", "shape+fill"):
        row = []
        for nm, lib, test, low in splits:
            acc, tot, maj, per, conf = run(lib, test, nm, G=G, CH=CH, FC=FC, WD=WD, S=S, F=F, A=A,
                                           DTi=DTi, DTs=DTs, angles=angles, scales=scales,
                                           k=a.k, variant=variant, low=low,
                                           w_fill=a.w_fill, w_asp=a.w_asp)
            row.append(f"{acc:.3f}")
            results[(variant, nm)] = (acc, tot, maj, per, conf)
        print(f"  {variant:12s} " + "  ".join(f"{v:>12s}" for v in row))
    _, _, maj_m, _, _ = results[("iou", "map -> map")]
    _, _, maj_c, _, _ = results[("iou", "CS -> map")]
    _, _, maj_a, _, _ = results[("iou", "all -> all")]
    print(f"  {'baseline':12s} " + "  ".join(f"{v:>12.3f}" for v in (maj_m, maj_c, maj_a)))

    for nm in ("map -> map", "CS -> map"):
        acc, tot, maj, per, conf = results[(a.detail, nm)]
        print(f"\n{a.detail} · {nm}: {acc:.3f} on {tot} (baseline {maj:.3f})")
        for f in sorted(per, key=lambda z: -per[z][1])[:8]:
            g, n = per[f]
            print(f"    {f:26s} {g:>3d}/{n:<3d} {g/n:.3f}")
        for (t, p), n in conf.most_common(4):
            print(f"    {n:>3d} confusions  {t} -> {p}")
    print("\nMATCH2DONE")


if __name__ == "__main__":
    main()
