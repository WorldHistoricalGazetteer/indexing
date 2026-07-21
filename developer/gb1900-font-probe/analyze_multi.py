"""Evaluate the 44-face alphabet beyond per-character top-1:
  A) per-LETTER discriminability — which letters separate faces best (answers the 'vowel model' question);
  B) label-length aggregation — accuracy as a function of how many same-face characters are voted together
     (Point 1: a whole label sums its characters' font-votes, so per-label >> per-character);
  C) 3-class HELD-OUT — classify the human-labelled test glyphs (italic/upright/blackletter) via the alphabet,
     collapse the predicted face to its base style, compare to the human label (a genuine external number);
  D) per-face confusion montage — real glyphs of each face beside the faces it bleeds into.

    FCTILES=/vast/ishi/gb1900/fc_tiles /vast/ishi/envs/mapreader/bin/python analyze_multi.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, json, numpy as np
from collections import defaultdict, Counter
from PIL import Image, ImageDraw
from discrim_test import sims_row

HERE = "/vast/ishi/gb1900/probe/font"; OUT = "/vast/ishi/gb1900/edition/discover"
TAX = {x["key"]: x for x in json.load(open(f"{HERE}/font_taxonomy.json"))}
VOWELS = set("AEIOU")

d = np.load(f"{OUT}/alphabet_multi.npz", allow_pickle=True)
G, LET, CAP, STY = d["glyphs"], d["letter"], d["cap"], d["style"]
fonts = sorted(set(STY.tolist())); base = {f: (TAX.get(f, {}).get("base_style") or "?") for f in fonts}
by = defaultdict(list)
for i in range(len(G)): by[(str(LET[i]), bool(CAP[i]))].append(i)
# per-bucket normalised matrices + face arrays
BK = {}
for k, idx in by.items():
    M = np.array([G[i].astype(np.float32).ravel() for i in idx], np.float32); M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)
    BK[k] = (idx, np.array([STY[i] for i in idx]), M)

def glyph_facesims(g, k, drop=-1):
    """best same-letter kNN similarity to each face for a query glyph in bucket k (optionally drop index)."""
    idx, sty, M = BK[k]; r = sims_row(g, M)
    if 0 <= drop < len(r): r[drop] = -2.0
    out = {}
    for j, f in enumerate(sty):
        if r[j] > out.get(f, -9): out[f] = float(r[j])
    return out

# ---------- A) per-letter discriminability ----------
print("=== A) per-LETTER discriminability (LOO top-1, caps only, >=2 faces, >=20 glyphs) ===", flush=True)
perL = {}
for (L, cap), (idx, sty, M) in BK.items():
    if not cap or len(set(sty.tolist())) < 2 or len(idx) < 20: continue
    ok = 0
    for j in range(len(idx)):
        fs = glyph_facesims(G[idx[j]], (L, cap), drop=j)
        if max(fs, key=fs.get) == sty[j]: ok += 1
    perL[L] = (ok / len(idx), len(idx))
for L in sorted(perL, key=lambda L: -perL[L][0]):
    acc, n = perL[L]; print(f"  {L} {'(vowel)' if L in VOWELS else '        '} acc={acc:.2f}  n={n}", flush=True)
vac = np.mean([perL[L][0] for L in perL if L in VOWELS]) if any(L in VOWELS for L in perL) else 0
cac = np.mean([perL[L][0] for L in perL if L not in VOWELS]) if any(L not in VOWELS for L in perL) else 0
print(f"  MEAN vowels={vac:.3f}  consonants={cac:.3f}", flush=True)

# ---------- B) label-length aggregation ----------
print("\n=== B) label-length aggregation (vote k same-face chars together) ===", flush=True)
rng = np.random.default_rng(0)
face_pool = defaultdict(list)                      # face -> [(bucket_key, local_index)]
for k, (idx, sty, M) in BK.items():
    for j in range(len(idx)):
        if k[1]: face_pool[sty[j]].append((k, j))  # caps only (labels are mostly caps for admin)
elig = [f for f in face_pool if len(face_pool[f]) >= 8]
for K in (1, 2, 3, 5, 8):
    ok = tot = 0
    for f in elig:
        pool = face_pool[f]
        for _ in range(60):
            pick = [pool[t] for t in rng.choice(len(pool), size=min(K, len(pool)), replace=False)]
            agg = defaultdict(float)
            for (bk, j) in pick:
                for ff, s in glyph_facesims(G[BK[bk][0][j]], bk, drop=j).items(): agg[ff] += s
            if max(agg, key=agg.get) == f: ok += 1
            tot += 1
    print(f"  label length k={K}: top-1 = {ok/tot:.2f}  (over {tot} simulated labels, {len(elig)} faces)", flush=True)

# ---------- C) 3-class held-out on human labels ----------
print("\n=== C) 3-class HELD-OUT (human-labelled test glyphs -> predicted base style) ===", flush=True)
try:
    from ssl_eval import harvest, SETS
    B3 = {"italic": "italic", "upright": "upright", "blackletter": "blackletter"}
    hum = []
    for bf, df in SETS:
        for hs, gl in harvest(bf, df):
            for (L, cap, g) in gl: hum.append((str(L), bool(cap), g, hs))
    conf = Counter(); tot = Counter()
    for L, cap, g, hs in hum:
        if (L, cap) not in BK: continue
        fs = glyph_facesims(g, (L, cap))
        pred_base = base.get(max(fs, key=fs.get), "?")
        pb = pred_base if pred_base in B3 else ("upright" if "upright" in pred_base or "CAPS" in pred_base else pred_base)
        pb = "italic" if "italic" in str(pred_base) else ("blackletter" if "blackletter" in str(pred_base) else "upright")
        tot[hs] += 1; conf[(hs, pb)] += 1
    N = sum(tot.values()); acc = sum(conf[(s, s)] for s in tot) / max(1, N)
    print(f"  glyphs evaluated: {N}; overall base-style accuracy = {acc:.2f}", flush=True)
    for s in sorted(tot):
        row = {p: conf[(s, p)] for p in tot}
        print(f"    true {s:<12} -> {row}  (recall {conf[(s,s)]/tot[s]:.2f})", flush=True)
except Exception as e:
    print(f"  held-out skipped: {e}", flush=True)

# ---------- D) per-face confusion montage ----------
print("\n=== D) confusion montage ===", flush=True)
fi = {f: i for i, f in enumerate(fonts)}
cm = np.zeros((len(fonts), len(fonts))); ct = np.zeros(len(fonts))
for (L, cap), (idx, sty, M) in BK.items():
    if len(set(sty.tolist())) < 2: continue
    for j in range(len(idx)):
        fs = glyph_facesims(G[idx[j]], (L, cap), drop=j)
        cm[fi[sty[j]], fi[max(fs, key=fs.get)]] += 1; ct[fi[sty[j]]] += 1
def samp(face, n=3):
    ii = [i for i in range(len(G)) if STY[i] == face and bool(CAP[i])][:n] or [i for i in range(len(G)) if STY[i] == face][:n]
    return ii
CELL = 40; rowh = CELL + 8; cols = 8
canvas = Image.new("RGB", (cols * CELL + 260, rowh * len(fonts)), "white"); dr = ImageDraw.Draw(canvas)
for r, f in enumerate(sorted(fonts, key=lambda f: -(cm[fi[f], fi[f]] / ct[fi[f]] if ct[fi[f]] else 0))):
    y = r * rowh
    for c, i in enumerate(samp(f, 3)):
        g = (1 - G[i].astype(np.uint8)) * 255; im = Image.fromarray(g).resize((CELL - 4, CELL - 4))
        canvas.paste(im.convert("RGB"), (c * CELL, y))
    top = sorted(((cm[fi[f], fi[o]] / ct[fi[f]], o) for o in fonts if o != f and ct[fi[f]]), reverse=True)[:2]
    acc = cm[fi[f], fi[f]] / ct[fi[f]] if ct[fi[f]] else 0
    dr.text((3 * CELL + 6, y + 2), f"{f}  acc {acc:.2f}", fill=(20, 20, 20))
    dr.text((3 * CELL + 6, y + 18), "-> " + ", ".join(f"{o} {v:.2f}" for v, o in top), fill=(150, 40, 30))
canvas.save(f"{OUT}/confusion_montage.png")
print(f"  confusion_montage.png -> {OUT}/confusion_montage.png", flush=True)
