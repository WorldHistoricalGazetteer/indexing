"""GB-STAMP route (1), STAGE 1 — reproduce the ESTABLISHED-BEST style classifier: per-glyph raster same-letter
kNN, leave-one-WORD-out (the documented 0.776 baseline). NOT a CNN (already tried -> 0.56) and NOT whole-word
features (my RF -> 0.60). The frozen box records are gone, but the derotated crops are embedded in the v3
review HTML (`const CROPS`, aligned to font_testset_v3_decisions.json), so we run the exact method on them.

Each labelled word -> force_split into per-letter glyphs -> norm_glyph. For each glyph, kNN among SAME-LETTER
glyphs from OTHER words (shift-augmented cosine); glyph votes -> word style. Reports per-glyph + per-word acc.

    /vast/ishi/envs/mapreader/bin/python style_knn.py   (or boundary env)
"""
import re, json, io, base64, sys
from collections import defaultdict, Counter
import numpy as np
from PIL import Image
sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
from build_alphabet import force_split
from discrim_test import norm_glyph, _shifted, sims_row

HTML = "/vast/ishi/gb1900/edition/spot/font_testset_v3.html"
DEC = "/vast/ishi/gb1900/probe/font/font_testset_v3_decisions.json"
K = 5

def load_words():
    h = open(HTML).read()
    crops = json.loads(re.search(r"const CROPS\s*=\s*(\[.*?\]);", h, re.S).group(1))
    dec = json.load(open(DEC))
    imgkey = next(k for k, v in crops[0].items() if isinstance(v, str) and len(v) > 200)
    out = []
    for i, d in enumerate(dec):
        if i >= len(crops) or d.get("font") in (None, "unclear"): continue
        b = crops[i][imgkey]; b = b.split(",", 1)[1] if b.startswith("data:") else b
        g = np.asarray(Image.open(io.BytesIO(base64.b64decode(b))).convert("L"), np.uint8)
        out.append((g, d.get("text", ""), d["font"]))
    return out

def main():
    words = load_words()
    # split each word into per-letter norm-glyphs
    glyphs = []                                   # (letter, unit_vec, word_idx, style)
    okw = 0
    for wi, (gray, text, style) in enumerate(words):
        letters = [c for c in text if c.isalnum()]
        if len(letters) < 2: continue
        gs = force_split(gray, len(letters))
        if len(gs) != len(letters): continue
        okw += 1
        for L, g in zip(letters, gs):
            v = g.astype(np.float32).ravel(); v /= (np.linalg.norm(v) + 1e-6)
            glyphs.append((L.upper(), g, wi, style))
    print(f"words: {len(words)} usable(split-ok): {okw}; glyphs: {len(glyphs)}", flush=True)
    bylet = defaultdict(list)
    for idx, (L, g, wi, st) in enumerate(glyphs): bylet[L].append(idx)
    # per-glyph LOWO kNN
    word_votes = defaultdict(Counter); gtot = gok = 0
    for L, members in bylet.items():
        for idx in members:
            _, g, wi, st = glyphs[idx]
            cand = [j for j in members if glyphs[j][2] != wi]
            if not cand: continue
            Mn = np.stack([glyphs[j][1].astype(np.float32).ravel() /
                           (np.linalg.norm(glyphs[j][1].astype(np.float32).ravel()) + 1e-6) for j in cand])
            r = sims_row(g, Mn)
            top = np.argsort(-r)[:K]
            vote = Counter(glyphs[cand[t]][3] for t in top).most_common(1)[0][0]
            gtot += 1; gok += (vote == st)
            word_votes[wi][vote] += 1
    # word-level
    wtot = wok = 0; conf = defaultdict(lambda: defaultdict(int))
    truth = {wi: words[wi][2] for wi in range(len(words))}
    for wi, vc in word_votes.items():
        pred = vc.most_common(1)[0][0]; t = truth[wi]
        wtot += 1; wok += (pred == t); conf[t][pred] += 1
    print(f"\nPER-GLYPH same-letter kNN acc: {gok/max(1,gtot):.3f}  (n={gtot})")
    print(f"PER-WORD (glyph-vote) acc:      {wok/max(1,wtot):.3f}  (n={wtot})   [baseline: raster-kNN 0.776, CNN 0.56, RF 0.60]")
    labs = sorted({words[w][2] for w in truth})
    print("\nword confusion (row=true, col=pred): " + " ".join(f"{l[:5]:>6}" for l in labs))
    for tl in labs:
        print(f"  {tl:11} " + " ".join(f"{conf[tl][pl]:6d}" for pl in labs))

if __name__ == "__main__":
    main()
