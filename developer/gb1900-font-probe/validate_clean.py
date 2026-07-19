"""Phase C (a) — DEFINITIVE validation of same-letter font-typing on CLEAN spotter boxes with HUMAN labels.

We now have 210 MapReader word-boxes each labelled by the reviewer (font ground truth) + MapReader's own
recognised text (letter identity). Extract each box's glyphs (de-rotated), align to its text, and run
LEAVE-ONE-BOX-OUT same-letter classification: type each box from the OTHER boxes' glyphs, compare to the
human font. This is the ceiling of the method on clean, human-verified data — the number the whole pivot
was for.

    /vast/ishi/envs/boundary/bin/python validate_clean.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import json, numpy as np
from collections import Counter, defaultdict
from discrim_test import norm_glyph, sims_row
from build_alphabet import force_split
from make_font_testset_v2 import load, stratified, derotate

DEC = "/home/stephen/PycharmProjects/indexing/developer/gb1900-font-probe/labels/font_testset_decisions (1).json"
DEC_PITT = "/vast/ishi/gb1900/probe/font/font_testset_decisions_1.json"
STYLES = ["italic", "blackletter", "upright"]

def aligned_letters(patch, text):
    """de-rotated patch + text -> [(letter, cap, glyph)] by FORCE-splitting the word into exactly N=len(text)
    blocks at the N-1 deepest column valleys (connected-components can't segment touching map letters)."""
    letters = [c for c in text if c.isalnum()]
    if len(letters) < 2: return []
    gs = force_split(patch, len(letters))
    if len(gs) != len(letters): return []
    return [(letters[i].upper(), letters[i].isupper(), gs[i]) for i in range(len(letters))]

def main():
    dec = json.load(open(DEC_PITT))
    font_by_i = {x["i"]: x["font"] for x in dec if x.get("font")}
    samp = stratified(load())                          # reproduce the exact v2 sample (seed 42)
    caps = []; per_box = defaultdict(list); n_lab = 0; n_aligned = 0
    for i, r in enumerate(samp):
        f = font_by_i.get(i)
        if f not in STYLES or r["text"] != dec[i]["text"]: continue
        n_lab += 1
        al = aligned_letters(derotate(r), r["text"])
        if not al: continue
        n_aligned += 1
        for L, cap, g in al:
            caps.append((L, cap, f, g, i)); per_box[i].append((L, cap, g, f))
    print(f"labelled boxes (italic/black/upright): {n_lab}; cleanly glyph-aligned: {n_aligned} "
          f"({n_aligned/max(1,n_lab)*100:.0f}%); glyphs: {len(caps)}", flush=True)
    print("aligned-box font counts:", dict(Counter(v[0][3] for v in per_box.values())))

    letters = np.array([c[0] for c in caps]); caparr = np.array([c[1] for c in caps])
    fonts = np.array([c[2] for c in caps]); boxid = np.array([c[4] for c in caps])
    M = np.array([c[3].astype(np.float32).ravel() for c in caps], np.float32); M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)

    conf = Counter(); tot = Counter()
    for bi, glyphs in per_box.items():
        true_f = glyphs[0][3]; votes = Counter()
        for L, cap, g, _ in glyphs:
            same = (letters == L) & (caparr == cap) & (boxid != bi)
            if len(set(fonts[same])) < 2: continue
            r = np.where(same, sims_row(g, M), -2.0)
            best = {f: float(r[(fonts == f) & same].max()) for f in set(fonts[same])}
            votes[max(best, key=best.get)] += 1
        if not votes: continue
        pred = votes.most_common(1)[0][0]; conf[(true_f, pred)] += 1; tot[true_f] += 1
    N = sum(tot.values()); acc = sum(conf[(s, s)] for s in STYLES) / max(1, N)
    print(f"\n=== LEAVE-ONE-BOX-OUT same-letter font typing (clean spotter boxes, human labels) ===")
    print(f"overall accuracy: {acc:.3f}  (N={N} boxes typed)")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>8s}" for s in STYLES) + "   recall")
    for s in STYLES:
        print(f"  {s:10s}" + "".join(f"{conf[(s,d)]:>8d}" for d in STYLES) + f"   {conf[(s,s)]/max(1,tot[s]):.2f}")
    # italic-vs-upright only
    iu = [b for b in per_box if per_box[b][0][3] in ("italic", "upright")]
    c2 = Counter(); t2 = Counter()
    for bi in iu:
        glyphs = per_box[bi]; true_f = glyphs[0][3]; votes = Counter()
        for L, cap, g, _ in glyphs:
            same = (letters == L) & (caparr == cap) & (boxid != bi) & (np.isin(fonts, ["italic", "upright"]))
            if len(set(fonts[same])) < 2: continue
            r = np.where(same, sims_row(g, M), -2.0)
            best = {f: float(r[(fonts == f) & same].max()) for f in ["italic", "upright"] if ((fonts == f) & same).any()}
            if best: votes[max(best, key=best.get)] += 1
        if votes: pred = votes.most_common(1)[0][0]; c2[(true_f, pred)] += 1; t2[true_f] += 1
    n2 = sum(t2.values())
    print(f"\nitalic-vs-upright only: acc={sum(c2[(s,s)] for s in ['italic','upright'])/max(1,n2):.3f} (N={n2})")

if __name__ == "__main__":
    main()
