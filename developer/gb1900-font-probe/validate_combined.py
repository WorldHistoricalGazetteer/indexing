"""Phase C — re-measure the raster same-letter kNN on the COMBINED round-1 + round-2 human reference (~436
words), the enlarged/balanced set. Leave-one-word-out, margin-weighted voting + confidence-gating sweep.
Tells us whether more balanced human labels lift the 0.776 baseline (esp. upright, the class that grew most).

    /vast/ishi/envs/boundary/bin/python validate_combined.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import json, numpy as np
from collections import Counter, defaultdict
from build_alphabet import force_split
from make_font_testset_v2 import derotate
from discrim_test import sims_row

SPOT = "/vast/ishi/gb1900/edition/spot"; FD = "/vast/ishi/gb1900/probe/font"
SETS = [(f"{SPOT}/font_testset_v2_boxes.json", f"{FD}/font_testset_decisions_1.json"),
        (f"{SPOT}/font_testset_v3_boxes.json", f"{FD}/font_testset_v3_decisions.json")]
STYLES = ["italic", "blackletter", "upright"]

def harvest(boxfile, decfile, src):
    dec = json.load(open(decfile)); font_by_i = {x["i"]: x["font"] for x in dec if x.get("font")}
    samp = json.load(open(boxfile)); words = []
    for i, r in enumerate(samp):
        f = font_by_i.get(i)
        if f not in STYLES or r["text"] != dec[i]["text"]: continue
        patch = derotate(r)
        if patch is None: continue
        letters = [c for c in r["text"] if c.isalnum()]
        gs = force_split(patch, len(letters))
        if len(gs) != len(letters): continue
        words.append((f, [(letters[k].upper(), letters[k].isupper(), gs[k]) for k in range(len(letters))], src))
    return words

def main():
    words = []
    for si, (bf, df) in enumerate(SETS): words += harvest(bf, df, si)
    print(f"combined words: {len(words)}  fonts: {dict(Counter(w[0] for w in words))}", flush=True)
    caps_all = [(L, cap, g, wi) for wi, w in enumerate(words) for (L, cap, g) in w[1]]
    CAP_PER = int(__import__("os").environ.get("CAP_PER", "0"))       # 0 = no balancing
    if CAP_PER:
        seen = defaultdict(int); caps = []
        for c in caps_all:
            k = (c[0], c[1], words[c[3]][0])
            if seen[k] < CAP_PER: caps.append(c); seen[k] += 1
        print(f"balanced reference: cap {CAP_PER}/cell -> {len(caps)} of {len(caps_all)} glyphs", flush=True)
    else:
        caps = caps_all
    letters = np.array([c[0] for c in caps]); caparr = np.array([c[1] for c in caps]); boxid = np.array([c[3] for c in caps])
    gfont = np.array([words[c[3]][0] for c in caps])                 # font per glyph
    M = np.array([c[2].astype(np.float32).ravel() for c in caps], np.float32); M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)

    records = []
    for wi, w in enumerate(words):
        tf, gl, src = w
        wvote = defaultdict(float)
        for L, cap, g in gl:
            same = (letters == L) & (caparr == cap) & (boxid != wi)
            if len(set(gfont[same])) < 2: continue
            r = np.where(same, sims_row(g, M), -2.0)
            best = sorted(((s, float(r[(gfont == s) & same].max())) for s in set(gfont[same])), key=lambda kv: -kv[1])
            wvote[best[0][0]] += best[0][1] - (best[1][1] if len(best) > 1 else 0)
        if not wvote: continue
        pred = max(wvote, key=wvote.get); conf = wvote[pred] / (sum(wvote.values()) + 1e-9)
        records.append((tf, pred, conf, src))

    def show(recs, tag, Nall):
        c = Counter(); t = Counter()
        for tf, pf, *_ in recs: c[(tf, pf)] += 1; t[tf] += 1
        N = sum(t.values()); acc = sum(c[(s, s)] for s in STYLES) / max(1, N)
        print(f"\n=== {tag}: acc={acc:.3f} N={N} ===")
        print(f"{'true':12s}" + "".join(f"{s[:5]:>8s}" for s in STYLES) + "  recall")
        for s in STYLES:
            print(f"  {s:10s}" + "".join(f"{c[(s,d)]:>8d}" for d in STYLES) + f"  {c[(s,s)]/max(1,t[s]):.2f}")
    Nall = len(records)
    show(records, "UNGATED combined (both rounds' words, ~436-word reference)", Nall)
    show([r for r in records if r[3] == 0], "round-1 words only (combined reference)", Nall)
    show([r for r in records if r[3] == 1], "round-2 words only (combined reference)", Nall)
    print(f"\n[round-1 words typed from round-1-ONLY reference were 0.776 -> compare to round-1 line above]")
    print("confidence gating (all):")
    print(f"{'tau':>8s}{'coverage':>10s}{'accuracy':>10s}")
    for tau in [0.0, 0.5, 0.6, 0.7, 0.8, 0.9]:
        kept = [r for r in records if r[2] >= tau]
        if kept: print(f"{tau:>8.2f}{len(kept)/Nall*100:>9.0f}%{sum(1 for a,b,*_ in kept if a==b)/len(kept):>10.3f}")


if __name__ == "__main__":
    main()
