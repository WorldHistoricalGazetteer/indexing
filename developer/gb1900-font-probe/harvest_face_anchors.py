"""Weak-supervision anchor harvest for the STARVED reachable faces, across the whole series.

Blackletter holds 64 anchor glyphs and Upright-Solid-Serif 168, against ~1,500 each for the two dominant
faces. That imbalance — not the nine out-of-reach faces — is the real weakness in the instrument. It is
closable from spotter output by the lexical targeting that already took blackletter from 2 anchors to 96%,
now run over 8.8M labels instead of a sample.

THE LEXICON IS DELIBERATELY NARROW. Only words whose OS category is unambiguous are used:

    Blackletter          tumulus/tumuli/cairn/barrow/earthwork  -> Antiquities (Pre-historic, Saxon, Norman)
    Upright-Solid-Serif  wood/copse/plantation/covert/shaw      -> Woods and Copses

`camp`, `castle`, `fort`, `abbey`, `priory`, `chapel` are EXCLUDED even though they are common antiquity
words, because they are exactly the context-dependent cases the co-occurrence analysis exists to settle:
Camp and Castle mean an antiquity in the antiquity hand and a modern feature in roman or italic. Harvesting
them as Blackletter would assume the conclusion and the analysis would then "confirm" it. `bridge` and
`chapel` are excluded for the same reason on the other face — County Bridges are upright serif but Trust
Bridges are italic, Parish Churches upright but Chapelries italic. `Stone` was already excluded upstream
("italic standalone but blackletter in Standing Stone").

Independent corroboration before a pixel is read: the selected words' median cap heights are 33.0px
(Blackletter) and 35.2px (Upright-Solid-Serif), against the 1897 Characteristic Sheet's 33.0 for
Antiquities-Norman and 36.0 for Woods and Copses.

STORAGE. Glyph rasters only — small uint8 arrays — never crops or tiles. Tiles are read from the /ix1
block corpus; with FCTILES unset there is no fetch and no write-through cache, and TILES would in any case
resolve to node-local $SLURM_SCRATCH. /vast is a 1TB quota shared with production Elasticsearch and this
project has driven it read-only before.

    python harvest_face_anchors.py --shard 0 --of 16 --out-dir labels/harvest
    python harvest_face_anchors.py --merge --out-dir labels/harvest --out labels/anchors_harvested.npz
"""
import argparse, glob, json, os, re, sys, time
from collections import Counter, defaultdict

import numpy as np

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
from build_alphabet import force_split
from make_font_testset_v2 import derotate

FACE_LEX = {
    "Blackletter": {"tumulus", "tumuli", "cairn", "cairns", "barrow", "barrows",
                    "earthwork", "earthworks", "tumbrel"},
    "Upright-Solid-Serif": {"wood", "woods", "copse", "copses", "plantation", "plantations",
                            "covert", "coverts", "shaw"},
}
W2F = {w: f for f, ws in FACE_LEX.items() for w in ws}
GH, GW = 48, 48                       # glyph raster; matches the existing alphabet scale closely enough


def norm_word(t):
    return re.sub(r"[^a-z]", "", str(t).lower())


def glyphs_of(box, text):
    """Derotate the word and cut it into exactly len(text) letters. Returns [(char, raster), ...]."""
    import cv2
    g = derotate(box)
    if g is None or g.size == 0:
        return []
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:                                  # too short to cut reliably
        return []
    parts = force_split(g, len(letters))
    if len(parts) != len(letters):
        return []
    out = []
    for ch, r in zip(letters, parts):
        if r is None or r.size == 0:
            continue
        # force_split returns BOOLEAN ink masks; cv2.resize refuses bool. Scale to 0/255 uint8 so the
        # rasters match the existing alphabet glyphs rather than silently becoming a different dtype.
        if r.dtype == bool:
            r = (r.astype(np.uint8) * 255)
        elif r.dtype != np.uint8:
            r = np.clip(r, 0, 255).astype(np.uint8)
        out.append((ch, cv2.resize(r, (GW, GH), interpolation=cv2.INTER_AREA)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="/vast/ishi/gb1900/edition/gb_stamp_labels.jsonl")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--per-face", type=int, default=3000,
                    help="target GLYPHS per face overall; balance with the dominant faces (~1,500) matters "
                         "more than volume, and a bigger pile of one word is not more information")
    ap.add_argument("--per-term-frac", type=float, default=0.34,
                    help="no single lexicon term may supply more than this share of a face")
    ap.add_argument("--out-dir", default="labels/harvest")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--out", default="labels/anchors_harvested.npz")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    if a.merge:
        return merge(a)

    # Per-shard budget, and a per-term cap so `wood` (77k occurrences) cannot swamp `shaw` (3.5k) — a face
    # learned from one word is a word detector, not a face detector.
    budget = max(1, a.per_face // max(1, a.of))
    term_cap = max(1, int(budget * a.per_term_frac))

    kept = defaultdict(list)          # face -> [(char, raster)]
    per_term = Counter()
    seen_words = 0
    t0 = time.time()
    with open(a.labels, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i % a.of != a.shard:
                continue
            if all(len(v) >= budget for v in kept.values()) and len(kept) == len(FACE_LEX):
                break
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for wd in rec.get("words", []):
                t = norm_word(wd["text"])
                face = W2F.get(t)
                if not face or len(kept[face]) >= budget:
                    continue
                if per_term[(face, t)] >= term_cap:
                    continue
                seen_words += 1
                gl = glyphs_of({"gpoly": wd["poly"]}, wd["text"])
                if not gl:
                    continue
                # Count GLYPHS against the cap, not words. The budget is in glyphs, so a word-denominated
                # cap never binds and one term (`wood`, 77k occurrences) can supply a whole face.
                per_term[(face, t)] += len(gl)
                kept[face].extend(gl)
            if (i + 1) % 400000 == 0:
                got = {k: len(v) for k, v in kept.items()}
                print(f"  [{a.shard}] line {i+1:,}: {got} ({time.time()-t0:.0f}s)", flush=True)

    faces, chars, rasters = [], [], []
    for face, items in kept.items():
        for ch, r in items:
            faces.append(face); chars.append(ch); rasters.append(r)
    outp = os.path.join(a.out_dir, f"harvest_{a.shard:03d}.npz")
    np.savez_compressed(outp, glyphs=np.array(rasters, np.uint8) if rasters else np.zeros((0, GH, GW), np.uint8),
                        faces=np.array(faces), chars=np.array(chars))
    got = {k: len(v) for k, v in kept.items()}
    print(f"HARVESTDONE {a.shard}: {got} from {seen_words} candidate words -> {outp} "
          f"({time.time()-t0:.0f}s)", flush=True)


def merge(a):
    G, F, C = [], [], []
    for p in sorted(glob.glob(os.path.join(a.out_dir, "harvest_*.npz"))):
        z = np.load(p, allow_pickle=True)
        if len(z["glyphs"]):
            G.append(z["glyphs"]); F.append(z["faces"]); C.append(z["chars"])
    if not G:
        print("nothing harvested"); return
    G = np.concatenate(G); F = np.concatenate(F); C = np.concatenate(C)
    np.savez_compressed(a.out, glyphs=G, faces=F, chars=C)
    n = Counter(F.tolist())
    print(f"HARVESTMERGED {len(G):,} glyphs -> {a.out}")
    for face, c in n.most_common():
        print(f"  {face:24s} {c:6,d} glyphs, {len(set(C[F == face])):2d} distinct characters")
    print(f"  size {os.path.getsize(a.out)/2**20:.1f} MB")


if __name__ == "__main__":
    main()
