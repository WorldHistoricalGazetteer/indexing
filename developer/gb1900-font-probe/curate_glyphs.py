"""Apply the face-curation spec to the glyph library: drop unusable faces, merge duplicate typefaces.

The Characteristic Sheet names CATEGORIES, not typefaces — several of its categories are the same face at a
different size, and size is already normalised out of these glyphs. So the inventory has to be collapsed
before any matcher is built, or it will be asked to separate faces that are not actually different.

Reports coverage PER CASE as well as per face. That distinction decides what a per-letter matcher can do: an
upright serif holding ten capitals and seven lowercase is not a face with seventeen letters, it is two thin
alphabets, and a lowercase spot can only ever be matched against the lowercase half.

    python curate_glyphs.py --spec labels/face_curation.json
"""
import argparse, json, os
from collections import defaultdict
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="labels/alphabet_glyphs.npz")
    ap.add_argument("--spec", default="labels/face_curation.json")
    ap.add_argument("--out", default="labels/alphabet_glyphs_curated.npz")
    a = ap.parse_args()

    spec = json.load(open(a.spec))
    drop = set(spec.get("drop", {}))
    merge = spec.get("merge", {})
    repop = spec.get("repopulate", {})

    d = np.load(a.npz, allow_pickle=True)
    faces = d["faces"].astype(str)
    unknown = (drop | set(merge)) - set(faces)
    if unknown:
        raise SystemExit(f"spec names faces not in the library: {sorted(unknown)}")

    keep = np.array([f not in drop for f in faces])
    newface = np.array([merge.get(f, f) for f in faces])
    print(f"{len(faces)} glyphs, {len(set(faces))} faces")
    for f in sorted(drop):
        print(f"  DROP  {f:32s} {int((faces == f).sum()):>3d} glyphs")
    for src in sorted(merge):
        print(f"  MERGE {src:32s} {int((faces == src).sum()):>3d} glyphs -> {merge[src]}")

    out = {k: d[k][keep] for k in d.files if k != "faces"}
    out["faces"] = newface[keep]
    np.savez_compressed(a.out, **out)

    fc = out["faces"]
    ch = d["chars"].astype(str)[keep]
    per = defaultdict(lambda: defaultdict(set))
    for f, c in zip(fc, ch):
        per[f]["upper" if c.isupper() else "lower" if c.islower() else "other"].add(c)
    print(f"\ncurated: {len(fc)} glyphs, {len(set(fc))} faces\n")
    print(f"  {'face':30s} {'glyphs':>6} {'UPPER':>6} {'lower':>6}   letters")
    for f in sorted(set(fc)):
        n = int((fc == f).sum())
        up, lo = per[f]["upper"], per[f]["lower"]
        flag = ""
        if f in repop:
            flag = "  << repopulate: " + repop[f]
        elif max(len(up), len(lo)) < 5:
            flag = "  << thin in BOTH cases"
        print(f"  {f:30s} {n:>6d} {len(up):>6d} {len(lo):>6d}   "
              f"{''.join(sorted(up))}{'/' if up and lo else ''}{''.join(sorted(lo))}{flag}")

    # Which letters can actually discriminate: those held by two or more faces, counted per case.
    byc = defaultdict(set)
    for f, c in zip(fc, ch):
        byc[c].add(f)
    shared = sorted(c for c, s in byc.items() if len(s) >= 2)
    print(f"\n{len(shared)} characters are held by 2+ faces (the only ones a cross-face match can use):")
    print(f"  {''.join(shared)}")
    print(f"\nwrote {a.out}")
    print("CURATEDONE")


if __name__ == "__main__":
    main()
