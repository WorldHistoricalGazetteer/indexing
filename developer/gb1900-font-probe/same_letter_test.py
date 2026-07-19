"""Phase C — SAME-LETTER discrimination test (faithful to SG's alphabet proposal).

The letter-agnostic test failed because raster similarity is dominated by LETTER IDENTITY, not style
(upright-C ~ blackletter-C ~ italic-C). Here we hold the letter CONSTANT using the crowd transcription:
glyph-boxes sort left-to-right and align to the label's letters, so each glyph gets its letter for free.
A test capital is then matched ONLY against same-letter templates of each style — the winning style is
decided by letterform alone. Ground-truth style comes from unambiguous crowd text (italic<-watercourses,
upright<-churches, blackletter<-antiquity descriptors). Leave-one-out over the harvested capitals.

    /vast/ishi/envs/boundary/bin/python same_letter_test.py
"""
import os, re, glob, json, math, numpy as np, cv2
import concurrent.futures as cf
from collections import Counter, defaultdict
from PIL import Image
from discrim_test import norm_glyph, sims_row, crop_box, H, W

DISC = "/vast/ishi/gb1900/edition/discover"
WATER = re.compile(r"^(R\.|Afon|Nant)\b|\b(River|Brook|Burn|Beck)\b", re.I)
NOTWATER = re.compile(r"\b(Farm|Ho|House|Cottage|Wood|Hall|Fm|Mill|Green|Lane|Bank|Bridge|Field|Moor|Hill)\b", re.I)
ANTIQ = re.compile(r"\b(Tumulus|Tumuli|Earthwork|Earthworks|Cairn|Stone Circle|Standing Stone|Site of|Camp|Enclosure)\b", re.I)
CHURCH = re.compile(r"\b(Church|Chapel)\b|\bCh\.?$", re.I)

def style_of(t):
    if WATER.search(t) and not NOTWATER.search(t): return "italic"
    if ANTIQ.search(t): return "blackletter"
    if CHURCH.search(t): return "upright"
    return None

def glyphs_pos(gray):
    """letter-sized connected components, normalised, returned LEFT-TO-RIGHT."""
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lbl, st, _ = cv2.connectedComponentsWithStats(ink, 8)
    hs = [st[i, 3] for i in range(1, n) if st[i, 3] >= 6]
    if not hs: return []
    mh = np.median(hs); out = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if h < 0.55 * mh or h > 2.2 * mh or area < 10 or w > 3.2 * h: continue
        g = norm_glyph((lbl[y:y + h, x:x + w] == i))
        if g is not None: out.append((int(x), g))
    out.sort(key=lambda t: t[0])
    return [g for _, g in out]

def harvest_one(job):
    style, box_g, text = job
    crop = crop_box(box_g)
    if crop is None: return []
    gs = glyphs_pos(crop)
    letters = [c for c in text if c.isalnum()]
    if not gs or not letters: return []
    rows = []
    if len(gs) == len(letters):                         # clean 1:1 alignment -> label every glyph
        for i, g in enumerate(gs):
            rows.append((letters[i].upper(), letters[i].isupper(), style, g))
    else:                                               # fall back to the initial capital only (robust)
        rows.append((letters[0].upper(), True, style, gs[0]))
    return rows

def main():
    jobs = []
    for f in glob.glob(f"{DISC}/labels_*.json"):
        for Lb in json.load(open(f)):
            t = (Lb.get("crowd") or "").strip()
            if not t or "box_g" not in Lb: continue
            st = style_of(t)
            if st: jobs.append((st, Lb["box_g"], t))
    print("unambiguous-style labels:", Counter(j[0] for j in jobs), flush=True)
    harvested = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for rows in ex.map(harvest_one, jobs): harvested += rows
    caps = [(L, s, g) for (L, cap, s, g) in harvested if cap]
    print(f"harvested glyphs: {len(harvested)}  capitals: {len(caps)}", flush=True)
    print("capital style counts:", Counter(s for _, s, _ in caps))

    letters = np.array([c[0] for c in caps]); styles = np.array([c[1] for c in caps])
    Mn = np.array([g.astype(np.float32).ravel() for _, _, g in caps], np.float32)
    Mn /= (np.linalg.norm(Mn, axis=1, keepdims=True) + 1e-6)
    stset = ["italic", "blackletter", "upright"]

    # per-(letter,style) template counts — so we can see coverage + which letters are testable
    cov = Counter((L, s) for L, s, _ in caps)
    multi = sorted({L for L in set(letters) if sum((L, s) in cov for s in stset) >= 2})
    print(f"capital letters with >=2 styles (discriminative subset): {multi}")

    conf = Counter(); tot = Counter(); n_skip = 0
    for i in range(len(caps)):
        same = (letters == letters[i]) & (np.arange(len(caps)) != i)
        cand_styles = set(styles[same])
        if len(cand_styles) < 2:                        # letter present in only one style -> not a real test
            n_skip += 1; continue
        r = sims_row(caps[i][2], Mn)
        r = np.where(same, r, -2.0)
        best = {s: float(r[(styles == s) & same].max()) if ((styles == s) & same).any() else -2.0 for s in stset}
        pred = max(best, key=best.get)
        conf[(styles[i], pred)] += 1; tot[styles[i]] += 1

    N = sum(tot.values())
    acc = sum(conf[(s, s)] for s in stset) / max(1, N)
    print(f"\n=== SAME-LETTER capital discrimination (leave-one-out, discriminative subset N={N}, skipped {n_skip} single-style) ===")
    print(f"overall accuracy: {acc:.3f}")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>7s}" for s in stset) + "   recall")
    for s in stset:
        print(f"  {s:10s}" + "".join(f"{conf[(s,d)]:>7d}" for d in stset) + f"   {conf[(s,s)]/max(1,tot[s]):.2f}")

    # italic-vs-upright ONLY (most data, the axis the CRNN embed plateaued on)
    iu = [(L, s, g) for (L, s, g) in caps if s in ("italic", "upright")]
    if iu:
        Lu = np.array([c[0] for c in iu]); Su = np.array([c[1] for c in iu])
        M2 = np.array([g.astype(np.float32).ravel() for _, _, g in iu], np.float32); M2 /= (np.linalg.norm(M2, axis=1, keepdims=True) + 1e-6)
        c2 = Counter(); t2 = Counter()
        for i in range(len(iu)):
            same = (Lu == Lu[i]) & (np.arange(len(iu)) != i)
            if len(set(Su[same])) < 2: continue
            r = np.where(same, sims_row(iu[i][2], M2), -2.0)
            pred = "italic" if r[(Su == "italic") & same].max() >= r[(Su == "upright") & same].max() else "upright"
            c2[(Su[i], pred)] += 1; t2[Su[i]] += 1
        n2 = sum(t2.values())
        print(f"\nitalic-vs-upright same-letter: acc={sum(c2[(s,s)] for s in ['italic','upright'])/max(1,n2):.3f} (N={n2}) "
              f"[italic rec {c2[('italic','italic')]/max(1,t2['italic']):.2f}, upright rec {c2[('upright','upright')]/max(1,t2['upright']):.2f}]")

if __name__ == "__main__":
    main()
