"""Cut boxed Characteristic-Sheet letters into the SAME normalisation as the map-derived glyph library.

Templates and spots have to be normalised identically or an overlay match is comparing conventions rather than
letterforms. So this reproduces `extract_alphabet.norm_glyph` exactly: binarise, crop to the ink bounding box,
scale to fit 44x36 preserving aspect, centre on a black canvas.

CS letters are ENGRAVED rather than printed-and-scanned, so they binarise cleanly and need no line-erasure —
the map-derived glyphs need both. That difference is worth remembering if seeded faces later behave unlike
sampled ones; it is a domain difference, not a bug.

Merges into the library with `source` marking each glyph's origin, so a CS-seeded face can always be told from
a map-sampled one, and either can be excluded.

    python extract_cs_glyphs.py --boxes labels/cs_letter_boxes.json
"""
import argparse, json, os
from collections import defaultdict
import numpy as np
import cv2

H, W = 44, 36


def norm_glyph(sub):
    """Identical to extract_alphabet.norm_glyph — deliberately, so templates match the sampled library."""
    ys, xs = np.where(sub > 0)
    if len(ys) < 6:
        return None
    g = (sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1] > 0).astype(np.uint8) * 255
    scale = min(H / g.shape[0], W / g.shape[1])
    g = cv2.resize(g, (max(1, int(g.shape[1] * scale)), max(1, int(g.shape[0] * scale))),
                   interpolation=cv2.INTER_AREA)
    canvas = np.zeros((H, W), np.uint8)
    oy, ox = (H - g.shape[0]) // 2, (W - g.shape[1]) // 2
    canvas[oy:oy + g.shape[0], ox:ox + g.shape[1]] = g
    return canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boxes", default="labels/cs_letter_boxes.json")
    ap.add_argument("--dir", default="reference")
    ap.add_argument("--library", default="labels/alphabet_glyphs_curated.npz")
    ap.add_argument("--out", default="labels/alphabet_glyphs_seeded.npz")
    ap.add_argument("--min-box", type=int, default=6, help="px; smaller boxes cannot hold a letter")
    a = ap.parse_args()

    spec = json.load(open(a.boxes))["specimens"]
    # Faces merge as the inventory is refined; an alias map lets earlier boxing stay valid without rewriting
    # the export, which would lose the record of what was originally assigned.
    alias = {}
    if os.path.exists("labels/face_inventory.json"):
        alias = json.load(open("labels/face_inventory.json")).get("aliases", {})
    if alias:
        print(f"applying {len(alias)} face alias(es): " +
              ", ".join(f"{k} -> {v}" for k, v in alias.items()), flush=True)
    glyphs, chars, faces, words, angles, srcs = [], [], [], [], [], []
    skipped = defaultdict(int)
    noface = []
    wordid = 100000                       # keep CS "words" clear of the map-derived word ids

    for s in spec:
        face = (s.get("face") or "").strip()
        img = cv2.imread(f"{a.dir}/ex_{s['stem']}.jpg", cv2.IMREAD_GRAYSCALE)
        if img is None:
            skipped["image missing"] += len(s["boxes"])
            continue
        wordid += 1
        for b in s["boxes"]:
            bface = (b.get("face") or face).strip()      # per-box face wins; per-image is the fallback
            bface = alias.get(bface, bface)
            ch = b.get("ch", "")
            if not ch:
                skipped["no character"] += 1
                continue
            if not bface:
                noface.append((s["stem"], ch))
                continue
            x, y = int(round(b["x"])), int(round(b["y"]))
            w, h = int(round(b["w"])), int(round(b["h"]))
            if w < a.min_box or h < a.min_box:
                skipped["box too small"] += 1
                continue
            sub = img[max(0, y):y + h, max(0, x):x + w]
            if sub.size < 40:
                skipped["crop empty"] += 1
                continue
            # Otsu per box: a specimen's ink is uniform, but exposure varies across the sheet.
            _, bw = cv2.threshold(sub, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            g = norm_glyph(bw)
            if g is None:
                skipped["no ink in box"] += 1
                continue
            glyphs.append(g)
            chars.append(ch)
            faces.append(bface)
            words.append(wordid)
            angles.append(0.0)            # CS specimens are set level; nothing to de-rotate
            srcs.append("CS")

    print(f"{len(glyphs)} glyphs cut from {len(spec)} specimens")
    for k, v in sorted(skipped.items()):
        print(f"  skipped {v:>3d}: {k}")
    if noface:
        byst = defaultdict(int)
        for st, _ in noface:
            byst[st] += 1
        print(f"  skipped {len(noface):>3d}: no face assigned "
              f"({', '.join(f'{k}:{v}' for k, v in sorted(byst.items()))})")

    lib = np.load(a.library, allow_pickle=True)
    n_lib = len(lib["glyphs"])
    out = dict(
        glyphs=np.concatenate([lib["glyphs"], np.array(glyphs, np.uint8)]) if glyphs else lib["glyphs"],
        chars=np.concatenate([lib["chars"].astype(str), np.array(chars, dtype="U1")]),
        faces=np.concatenate([lib["faces"].astype(str), np.array(faces, dtype=object).astype(str)]),
        word=np.concatenate([lib["word"], np.array(words, np.int64)]),
        angle=np.concatenate([lib["angle"], np.array(angles, np.float64)]),
        oid=np.concatenate([lib["oid"] if "oid" in lib.files else np.arange(n_lib),
                            np.arange(200000, 200000 + len(glyphs))]),
        source=np.concatenate([np.array(["map"] * n_lib, dtype=object),
                               np.array(srcs, dtype=object)]).astype(str),
    )
    np.savez_compressed(a.out, **out)

    fc, ch, sr = out["faces"], out["chars"], out["source"]
    per = defaultdict(lambda: defaultdict(set))
    for f, c, s_ in zip(fc, ch, sr):
        per[f][s_].add(c)
    print(f"\nlibrary: {n_lib} map + {len(glyphs)} CS = {len(fc)} glyphs\n")
    print(f"  {'face':28s} {'map':>4} {'CS':>4}   letters (CS-only marked *)")
    inv = json.load(open("labels/face_inventory.json"))["faces"] if os.path.exists(
        "labels/face_inventory.json") else {}
    for f in list(inv) + [x for x in sorted(set(fc)) if x not in inv]:
        m = int(((fc == f) & (sr == "map")).sum())
        c = int(((fc == f) & (sr == "CS")).sum())
        if not (m or c):
            print(f"  {f:28s} {0:>4} {0:>4}   << still empty")
            continue
        allc = sorted(per[f]["map"] | per[f]["CS"])
        s_ = "".join(x if x in per[f]["map"] else f"{x}*" for x in allc)
        print(f"  {f:28s} {m:>4} {c:>4}   {s_}")
    print(f"\nwrote {a.out}")
    print("CSEXTRACTDONE")


if __name__ == "__main__":
    main()
