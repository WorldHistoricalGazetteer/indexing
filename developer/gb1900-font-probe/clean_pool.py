"""Clean the SSL glyph pool so unsupervised clustering sees FONTS not noise. The raw pool (force_split on real
spotter words) is full of map-linework contamination, blanks, and clutter fragments that dominate the clusters.
Here each 40x40 pool glyph gets line-erase (remove crossing map lines) + a quality filter (drop near-blank,
over-dense clutter, and heavily-fragmented glyphs), keeping only words that still have >=2 clean glyphs.

    /vast/ishi/envs/mapreader/bin/python clean_pool.py   # ssl_glyphs.npz -> ssl_glyphs_clean.npz
"""
import numpy as np, cv2
from collections import Counter
from line_erase import erase_crossing_lines

SPOT = "/vast/ishi/gb1900/edition/spot"
d = np.load(f"{SPOT}/ssl_glyphs.npz", allow_pickle=True)
glyphs, letters, wid = d["glyphs"], d["letters"], d["wid"]
print(f"raw pool: {len(glyphs)} glyphs, {len(set(wid.tolist()))} words", flush=True)

kg, kl, kw = [], [], []
for i in range(len(glyphs)):
    g = glyphs[i]
    g2, _ = erase_crossing_lines(g)                                   # remove map lines crossing the box
    ink = cv2.threshold(g2, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    dens = float(ink.sum()) / 255.0 / ink.size
    ncomp = cv2.connectedComponents(ink)[0] - 1
    if dens < 0.04 or dens > 0.50 or ncomp > 10:                      # blank / dense clutter / shredded
        continue
    kg.append(g2); kl.append(letters[i]); kw.append(wid[i])
    if i % 20000 == 0: print(f"  {i}/{len(glyphs)} kept={len(kg)}", flush=True)

kw = np.array(kw)
cnt = Counter(kw.tolist()); ok = np.array([cnt[w] >= 2 for w in kw])   # words need >=2 glyphs (contrastive positives)
kg = np.array(kg, np.uint8)[ok]; kl = np.array(kl)[ok]; kw = kw[ok]
np.savez_compressed(f"{SPOT}/ssl_glyphs_clean.npz", glyphs=kg, letters=kl, wid=kw)
print(f"CLEAN pool: {len(kg)} glyphs ({len(kg)/len(glyphs)*100:.0f}% kept), {len(set(kw.tolist()))} words "
      f"-> {SPOT}/ssl_glyphs_clean.npz", flush=True)
