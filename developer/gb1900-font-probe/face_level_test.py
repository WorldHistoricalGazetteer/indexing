"""Face-level (SIGNATURE-level) accuracy of the backbone instrument — the handoff's standing open question.

"The face signal remains untested at the level that matters" has been open because the only sized human set
(`font_testset_decisions_1.json`, 225 items) is the COARSE axis: italic / upright / blackletter. But three
pooled labelling rounds already carry SIGNATURE labels — base_style·fill·decor, which is exactly the face
inventory (`upright·solid·serif` == Upright-Solid-Serif). Together they hold ~560 labels over 5 of the 6
reachable faces. No new labelling round is required.

Why signature and not the 49 OS categories: established 2026-07-23 and recorded in build_label_ui.py — the
OS used one generic serif at overlapping sizes for a whole family of small descriptive features, so within a
signature the categories are typographically indistinguishable (county_bridges 36px == woods_copses 36px).
No font/size method can split them; that separation has to come from text, gazetteer or symbol downstream.
The signature is what is eye- and backbone-distinguishable, so the signature is the honest target.

Reported:
  LOO within the human signature labels          the instrument's face-level ceiling
  harvested-Blackletter as reference             does the validated harvest hold up at face level
  per-class, with n                              a 28-item class cannot carry a headline number

    python face_level_test.py --out labels/face_level_test.json
"""
import argparse, glob, json, math, os, re, sys
from collections import Counter

import numpy as np

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")

HERE = "/vast/ishi/gb1900/probe/font"
SPOT2 = "/vast/ishi/gb1900/edition/spot2"
POOLS = ["labels/pool_labels_faced.json", "labels/pool_labels.json", "labels/pool_labels_hisam.json"]


def load_pool_labels():
    """Union the labelling rounds, deduped on position — the rounds overlap by construction."""
    out, seen = [], set()
    for f in POOLS:
        p = os.path.join(HERE, f)
        if not os.path.exists(p):
            continue
        for r in json.load(open(p, encoding="utf-8")):
            if not isinstance(r, dict):
                continue
            sig = r.get("sig")
            if not sig or "gcx" not in r or "gcy" not in r:
                continue
            k = (round(r["gcx"] / 4), round(r["gcy"] / 4))
            if k in seen:
                continue
            seen.add(k)
            out.append(dict(gcx=float(r["gcx"]), gcy=float(r["gcy"]), sig=sig,
                            text=str(r.get("text", "")), src=os.path.basename(f)))
    return out


def region_index():
    """Map a global z17 pixel to the spotted regions that contain it, via centres_all + the r=8 extent."""
    N17 = 2 ** 17
    idx = {}
    for line in open(f"{HERE}/centres_all.txt"):
        p = line.split()
        if len(p) < 3:
            continue
        lon, lat, tag = float(p[0]), float(p[1]), p[2]
        x = (lon + 180) / 360 * N17 * 256
        y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
        ctx, cty = int(x // 256), int(y // 256)
        idx[tag] = (ctx - 8, cty - 8, ctx + 8, cty + 8)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--wdesc", default=f"{HERE}/labels/wdesc")
    ap.add_argument("--out", default=f"{HERE}/labels/face_level_test.json")
    a = ap.parse_args()

    lab = load_pool_labels()
    print(f"{len(lab)} pooled signature labels, deduped")
    print("  ", Counter(x["sig"] for x in lab).most_common())

    # Locate each labelled position's detection polygon in the corpus-fed pass.
    ri = region_index()
    want = {}
    for r in lab:
        tx, ty = int(r["gcx"] // 256), int(r["gcy"] // 256)
        for tag, (x0, y0, x1, y1) in ri.items():
            if x0 <= tx <= x1 and y0 <= ty <= y1:
                want.setdefault(tag, []).append(r)
                break
    print(f"labels fall in {len(want)} spotted regions")

    from make_font_testset_v2 import derotate
    from harvest_word_descriptors import backbone_concat, square512

    D, Y, T = [], [], []
    miss = 0
    for tag, rows in want.items():
        bf = f"{SPOT2}/boxes_{tag}.jsonl"
        if not os.path.exists(bf):
            miss += len(rows); continue
        boxes = [json.loads(l) for l in open(bf, encoding="utf-8") if l.strip()]
        if not boxes:
            miss += len(rows); continue
        bx = np.array([[b["gcx"], b["gcy"]] for b in boxes])
        for r in rows:
            d = np.hypot(bx[:, 0] - r["gcx"], bx[:, 1] - r["gcy"])
            j = int(np.argmin(d))
            if d[j] > 24:                        # no detection at the labelled position
                miss += 1; continue
            im = square512(derotate({"gpoly": boxes[j]["gpoly"]}))
            if im is None:
                miss += 1; continue
            desc = backbone_concat(im)
            if desc is None:
                miss += 1; continue
            D.append(desc.astype(np.float32)); Y.append(r["sig"]); T.append(r["text"])
    print(f"embedded {len(D)} labels ({miss} unmatched)")
    if len(D) < 40:
        sys.exit("too few matched labels to test")

    X = np.array(D, np.float32)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    Y = np.array(Y)
    cnt = Counter(Y.tolist())
    chance = max(cnt.values()) / len(Y)

    S = X @ X.T
    np.fill_diagonal(S, -np.inf)
    idx = np.argsort(-S, axis=1)[:, :a.k]
    pred = np.array([Counter(Y[j] for j in row).most_common(1)[0][0] for row in idx])
    acc = float((pred == Y).mean())

    print()
    print(f"  classes {len(cnt)}   chance (majority) {chance:.3f}")
    print(f"  FACE-LEVEL LOO accuracy              {acc:.3f}")
    print()
    per = {}
    for c, n in cnt.most_common():
        m = Y == c
        per[c] = dict(n=int(n), recall=float((pred[m] == c).mean()))
        flag = "  <- too few to quote" if n < 30 else ""
        print(f"  {c:26s} n={n:4d}  recall {per[c]['recall']:.3f}{flag}")

    json.dump(dict(n=len(Y), classes=len(cnt), chance=chance, loo=acc, per_class=per),
              open(a.out, "w"), indent=1)
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
