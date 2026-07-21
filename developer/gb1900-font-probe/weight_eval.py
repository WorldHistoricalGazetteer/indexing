"""Per-letter vote weighting: weight each character's font-vote by how font-discriminative that letter is
(measured on TRAIN), so distinctive letters (Q/C/D/B/G) count more than vowels (A/I/U). Re-measure the
LABEL-level accuracy (aggregate k same-face characters) uniform vs weighted, on a held-out 30% split.

    /vast/ishi/envs/boundary/bin/python weight_eval.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import numpy as np
from collections import defaultdict
from discrim_test import sims_row

OUT = "/vast/ishi/gb1900/edition/discover"
d = np.load(f"{OUT}/alphabet_multi.npz", allow_pickle=True)
G, LET, CAP, STY = d["glyphs"], d["letter"], d["cap"], d["style"]
rng = np.random.default_rng(1)

by = defaultdict(list)
for i in range(len(G)): by[(str(LET[i]), bool(CAP[i]))].append(i)

# 70/30 split
train = set(); test = []
for k, idx in by.items():
    idx = list(idx); rng.shuffle(idx); c = int(len(idx) * 0.7)
    train.update(idx[:c]); test += idx[c:]

BK = {}                                              # bucket -> (styles, normalised train matrix)
for k, idx in by.items():
    ti = [i for i in idx if i in train]
    if not ti: continue
    M = np.array([G[i].astype(np.float32).ravel() for i in ti], np.float32); M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)
    BK[k] = (np.array([STY[i] for i in ti]), M)

def facesims(g, k):
    sty, M = BK[k]; r = sims_row(g, M); out = {}
    for t, f in enumerate(sty):
        if r[t] > out.get(f, -9): out[f] = float(r[t])
    return out

# per-letter weight = TRAIN LOO discriminability (caps buckets, >=2 faces, >=10 glyphs); else 0.5
W = {}
for k, (sty, M) in BK.items():
    if not k[1] or len(set(sty.tolist())) < 2 or len(sty) < 10: W[k] = 0.5; continue
    ok = 0
    for j in range(len(sty)):
        r = M @ M[j]; r[j] = -2
        best = {}
        for t, f in enumerate(sty):
            if r[t] > best.get(f, -9): best[f] = r[t]
        if max(best, key=best.get) == sty[j]: ok += 1
    W[k] = ok / len(sty)
wl = sorted(((W[k], k[0]) for k in W if k[1]), reverse=True)
print("per-letter weights (train discriminability): " + "  ".join(f"{L}:{w:.2f}" for w, L in wl[:8]) + "  ...  " +
      "  ".join(f"{L}:{w:.2f}" for w, L in wl[-5:]), flush=True)

# label simulation on TEST: uniform vs weighted aggregation
face_test = defaultdict(list)
for i in test:
    k = (str(LET[i]), bool(CAP[i]))
    if k in BK and CAP[i]: face_test[STY[i]].append((i, k))
elig = [f for f in face_test if len(face_test[f]) >= 8]
print(f"\nlabel-level top-1 (held-out TEST, {len(elig)} faces):", flush=True)
print("  k     uniform  weighted", flush=True)
for K in (1, 2, 3, 5, 8):
    ou = ow = tot = 0
    for f in elig:
        pool = face_test[f]
        for _ in range(80):
            pick = [pool[t] for t in rng.choice(len(pool), size=min(K, len(pool)), replace=False)]
            au = defaultdict(float); aw = defaultdict(float)
            for (i, k) in pick:
                fs = facesims(G[i], k); w = W.get(k, 0.5)
                for ff, s in fs.items(): au[ff] += s; aw[ff] += w * s
            ou += (max(au, key=au.get) == f); ow += (max(aw, key=aw.get) == f); tot += 1
    print(f"  {K:<5} {ou/tot:.2f}     {ow/tot:.2f}", flush=True)
