"""Apply the face-curation spec to the glyph library: drop, clear, re-source, merge, rename, delete.

The Characteristic Sheet names CATEGORIES, not typefaces — several of its categories are the same face at a
different size, and size is already normalised out of these glyphs. So the inventory has to be collapsed
before any matcher is built, or it will be asked to separate faces that are not actually different. Face names
follow the signature convention Style-Fill-Decor (Italic-Solid-Serif, Upright-Outline-Plain, ...).

Every operation is declared against the ORIGINAL npz and applied from scratch, so the spec remains a complete,
reproducible description of the curation however many rounds it accumulates — rather than a pile of edits
whose order matters. Individual glyphs are addressed by their original index, which is therefore stable.

Operation order is fixed and deliberate: delete individual glyphs, drop whole faces, RE-SOURCE a face's
samples from another face, then merge, then rename. Re-sourcing has to precede merge/rename so that a face
can be re-sourced and renamed in the same pass.

    python curate_glyphs.py --spec labels/face_curation.json
"""
import argparse, json, os
from collections import defaultdict
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="labels/alphabet_glyphs.npz")
    ap.add_argument("--spec", default="labels/face_curation.json")
    ap.add_argument("--inventory", default="labels/face_inventory.json")
    ap.add_argument("--out", default="labels/alphabet_glyphs_curated.npz")
    a = ap.parse_args()

    spec = json.load(open(a.spec))
    drop = set(spec.get("drop", {}))
    replace = spec.get("replace_glyphs", {})
    merge = spec.get("merge", {})
    rename = spec.get("rename", {})
    dele = {int(k) for k in spec.get("delete_glyph_ids", {})}
    repop = spec.get("repopulate", {})
    # The inventory is the authority on which faces EXIST, including permutations no sample has been found
    # for yet. Keeping it separate from the curation spec matters: the spec describes operations on the glyphs
    # we have, the inventory describes the typefaces we are trying to tell apart, and those are not the same
    # list — a face with no samples is still a face the matcher must eventually account for.
    inv = {}
    if os.path.exists(a.inventory):
        inv = json.load(open(a.inventory)).get("faces", {})

    d = np.load(a.npz, allow_pickle=True)
    faces = d["faces"].astype(str)
    named = set(drop) | set(merge) | set(rename) | set(replace) | set(replace.values())
    unknown = named - set(faces)
    if unknown:
        raise SystemExit(f"spec names faces not in the library: {sorted(unknown)}")
    bad = [i for i in dele if i >= len(faces)]
    if bad:
        raise SystemExit(f"delete_glyph_ids out of range: {bad}")

    print(f"{len(faces)} glyphs, {len(set(faces))} faces")
    keep = np.ones(len(faces), bool)

    for i in sorted(dele):
        print(f"  DELETE glyph #{i:<4d} {faces[i]} '{d['chars'].astype(str)[i]}'")
        keep[i] = False

    for f in sorted(drop):
        n = int(((faces == f) & keep).sum())
        print(f"  DROP     {f:30s} {n:>3d} glyphs")
        keep &= faces != f

    newface = faces.copy()
    for tgt, src in replace.items():
        n_old = int(((faces == tgt) & keep).sum())
        n_new = int(((faces == src) & keep).sum())
        print(f"  RESOURCE {tgt:30s} {n_old:>3d} own glyphs discarded, "
              f"{n_new:>3d} taken from {src}")
        keep &= faces != tgt                      # the face keeps its NAME but loses its own samples
        newface = np.where(faces == src, tgt, newface)

    for src in sorted(merge):
        print(f"  MERGE    {src:30s} {int(((faces == src) & keep).sum()):>3d} glyphs -> {merge[src]}")
        newface = np.where(newface == src, merge[src], newface)

    for src in sorted(rename):
        print(f"  RENAME   {src:30s} -> {rename[src]}")
        newface = np.where(newface == src, rename[src], newface)

    out = {k: d[k][keep] for k in d.files if k != "faces"}
    out["faces"] = newface[keep]
    # Carry the ORIGINAL index through, so a deletion marked in the curated view names the original glyph.
    out["oid"] = (d["oid"] if "oid" in d.files else np.arange(len(faces)))[keep]
    np.savez_compressed(a.out, **out)

    fc = out["faces"]
    ch = d["chars"].astype(str)[keep]
    per = defaultdict(lambda: defaultdict(set))
    for f, c in zip(fc, ch):
        per[f]["upper" if c.isupper() else "lower" if c.islower() else "other"].add(c)
    order = {n: i for i, n in enumerate(inv)}          # inventory order is semantic, not alphabetical
    allfaces = sorted(set(fc) | set(inv), key=lambda f: (order.get(f, 10**6), f))
    stray = sorted(set(fc) - set(inv))
    if stray:
        print(f"\n  NOTE: {len(stray)} face(s) hold glyphs but are not in the inventory: {stray}")
    print(f"\ncurated: {len(fc)} glyphs, {len(set(fc))} faces with samples, "
          f"{len(allfaces)} in the inventory\n")
    print(f"  {'face':26s} {'glyphs':>6} {'UPPER':>6} {'lower':>6}   letters")
    for f in allfaces:
        n = int((fc == f).sum())
        up, lo = per[f]["upper"], per[f]["lower"]
        if n == 0:
            flag = "  << no samples yet" + (f"  [{inv[f]['os']}]" if inv.get(f, {}).get("os") else "")
        elif f in repop:
            flag = "  << repopulate"
        elif max(len(up), len(lo)) < 5:
            flag = "  << thin in BOTH cases"
        else:
            flag = ""
        print(f"  {f:26s} {n:>6d} {len(up):>6d} {len(lo):>6d}   "
              f"{''.join(sorted(up))}{'/' if up and lo else ''}{''.join(sorted(lo))}{flag}")

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
