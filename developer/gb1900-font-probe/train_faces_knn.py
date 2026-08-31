"""Face/signature classifier on the human alphabet labels: per-glyph same-letter kNN, leave-one-WORD-out.
Reports at BOTH levels:
  - FACE (48 admin/semantic classes) — what you label;
  - SIGNATURE (style·fill·decor, ~16 classes) — what's typographically DISTINGUISHABLE from the glyph, and the
    real classifier target (faces sharing a signature are the same font; size/word/gazetteer split them later).
The signature view is more accurate AND needs far fewer samples (~16×8 vs 48×8). Depth target ~8-10 words/sig.

    python3 train_faces_knn.py
"""
import json, numpy as np
from collections import defaultdict, Counter

TAX = {f["key"]: (f["base_style"], f.get("fill"), f.get("decor")) for f in json.load(open("font_taxonomy.json"))}
def sig(face): return "·".join(str(x) for x in TAX.get(face, (face, "", "")))

d = np.load("labels/alphabet_glyphs.npz", allow_pickle=True)
G = d["glyphs"].astype(np.float32); chars = d["chars"]; faces = d["faces"]; word = d["word"]; N = len(G)
V = G.reshape(N, -1); V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-6)
K = 5

def shifted(g):
    out = [np.roll(np.roll(g, dy, 0), dx, 1).ravel() for dy in (-2, -1, 0, 1, 2) for dx in (-1, 0, 1)]
    m = np.array(out, np.float32); return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-6)

bychar = defaultdict(list)
for i in range(N): bychar[chars[i]].append(i)

def evaluate(lab, name):
    wperclass = Counter(); seen = set()
    for i in range(N):
        if (int(word[i]), str(lab[i])) not in seen: seen.add((int(word[i]), str(lab[i]))); wperclass[lab[i]] += 1
    wv = defaultdict(Counter); gtot = gok = 0
    for i in range(N):
        cand = [j for j in bychar[chars[i]] if word[j] != word[i]]
        if not cand: continue
        sims = (shifted(G[i]) @ V[cand].T).max(0); top = np.argsort(-sims)[:K]
        vote = Counter(lab[cand[t]] for t in top).most_common(1)[0][0]
        gtot += 1; gok += (vote == lab[i]); wv[int(word[i])][vote] += 1
    truth = {int(word[i]): lab[i] for i in range(N)}
    wok = sum(1 for w, vc in wv.items() if vc.most_common(1)[0][0] == truth[w])
    learn = [c for c in wperclass if wperclass[c] >= 2]
    lw = [w for w in wv if wperclass[truth[w]] >= 2]
    lacc = sum(1 for w in lw if wv[w].most_common(1)[0][0] == truth[w]) / max(1, len(lw))
    print(f"\n== {name} ({len(set(lab))} classes) ==")
    print(f"  per-glyph {gok/max(1,gtot):.3f}   per-word {wok/max(1,len(wv)):.3f}   "
          f"per-word on the {len(learn)} classes with >=2 words {lacc:.3f}")
    print(f"  classes with >=2 words: {sorted(learn)}")
    return wperclass

evaluate(faces, "BY FACE")
sigs = np.array([sig(f) for f in faces])
wps = evaluate(sigs, "BY SIGNATURE (real target)")
TARGET = 8; allsig = set(sig(k) for k in TAX)
cov = sum(1 for s in allsig if wps[s] >= TARGET); par = sum(1 for s in allsig if 0 < wps[s] < TARGET)
print(f"\nSIGNATURE coverage (target {TARGET} words): {cov} covered · {par} partial · {len(allsig)-cov-par} none  (of {len(allsig)})")
