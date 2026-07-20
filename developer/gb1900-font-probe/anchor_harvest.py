"""Phase C — WEAK-SUPERVISION anchor harvest + decisive test.

High-purity lexical anchors (per-word, context-independent) auto-label thousands of clean per-font glyphs
from the spotter corpus, with NO human labels. Decisive test: use the anchor-only reference to type the
HUMAN-labelled held-out set (same-letter kNN) and compare to the 0.776 human-reference baseline. If the
anchor-only reference matches it, the anchors break the data-scarcity wall (and cover the starved upright/
blackletter classes). Human labels stay the untouched gold eval. Numeral is a text rule, excluded here.

    /vast/ishi/envs/boundary/bin/python anchor_harvest.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import glob, json, numpy as np
from collections import Counter, defaultdict
from build_alphabet import force_split
from make_font_testset_v2 import derotate
from discrim_test import sims_row

SPOT = "/vast/ishi/gb1900/edition/spot"
DEC = "/vast/ishi/gb1900/probe/font/font_testset_decisions_1.json"
BOXES = f"{SPOT}/font_testset_v2_boxes.json"
STYLES = ["italic", "blackletter", "upright"]
# context-INDEPENDENT anchors (font holds at the word/box level, regardless of neighbours). Stone excluded
# (context-dependent: italic standalone but blackletter in "Standing Stone") -> needs pin check, added later.
LEX = {
    "italic":      {"spring", "well", "ford", "site", "weir", "sluice", "quarry", "issues", "sinks"},
    "upright":     {"church", "wood", "chapel", "copse", "plantation", "covert", "shaw"},
    "blackletter": {"tumulus", "tumuli", "cairn", "barrow", "earthwork", "earthworks", "dyke", "tumbrel"},
}
WORD2FONT = {w: f for f, ws in LEX.items() for w in ws}

def word_glyphs(r):
    patch = derotate(r)
    if patch is None: return None
    letters = [c for c in r["text"] if c.isalnum()]
    if len(letters) < 2: return None
    gs = force_split(patch, len(letters))
    if len(gs) != len(letters): return None
    return [(letters[i].upper(), letters[i].isupper(), gs[i]) for i in range(len(letters))]

def main():
    testcoords = {(r["gcx"], r["gcy"]) for r in json.load(open(BOXES))}
    boxes = []
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(f): boxes.append(json.loads(line))
    # harvest anchor glyphs (exclude any box that is a human-test box)
    ref = []                                             # (letter, cap, glyph, font)
    per_font = Counter()
    for r in boxes:
        t = r["text"].strip().lower()
        if t not in WORD2FONT or (r["gcx"], r["gcy"]) in testcoords: continue
        wg = word_glyphs(r)
        if not wg: continue
        f = WORD2FONT[t]
        for L, cap, g in wg: ref.append((L, cap, g, f))
        per_font[f] += 1
    print(f"anchor boxes: {dict(per_font)}  anchor glyphs: {len(ref)}", flush=True)

    letters = np.array([c[0] for c in ref]); caparr = np.array([c[1] for c in ref]); fonts = np.array([c[3] for c in ref])
    M = np.array([c[2].astype(np.float32).ravel() for c in ref], np.float32); M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)

    # human held-out test set
    dec = json.load(open(DEC)); font_by_i = {x["i"]: x["font"] for x in dec if x.get("font")}
    samp = json.load(open(BOXES))
    conf = Counter(); tot = Counter()
    for i, r in enumerate(samp):
        hf = font_by_i.get(i)
        if hf not in STYLES or r["text"] != dec[i]["text"]: continue
        wg = word_glyphs(r)
        if not wg: continue
        votes = Counter()
        for L, cap, g in wg:
            same = (letters == L) & (caparr == cap)
            if not same.any() or len(set(fonts[same])) < 2: continue
            rr = np.where(same, sims_row(g, M), -2.0)
            best = {s: float(rr[(fonts == s) & same].max()) for s in set(fonts[same])}
            votes[max(best, key=best.get)] += 1
        if not votes: continue
        pred = votes.most_common(1)[0][0]; conf[(hf, pred)] += 1; tot[hf] += 1
    N = sum(tot.values()); acc = sum(conf[(s, s)] for s in STYLES) / max(1, N)
    print(f"\n=== ANCHOR-ONLY reference (no human labels) typing the HUMAN test set (same-letter kNN, N={N}) ===")
    print(f"accuracy {acc:.3f}   [human-reference baseline 0.776]")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>8s}" for s in STYLES) + "  recall")
    for s in STYLES:
        print(f"  {s:10s}" + "".join(f"{conf[(s,d)]:>8d}" for d in STYLES) + f"  {conf[(s,s)]/max(1,tot[s]):.2f}")
    # per (letter,case) anchor coverage
    cells = defaultdict(set)
    for L, cap, _, f in ref: cells[(L, cap)].add(f)
    print(f"\nanchor (letter,case) cells with >=2 fonts: {sum(1 for v in cells.values() if len(v) >= 2)}")

if __name__ == "__main__":
    main()
