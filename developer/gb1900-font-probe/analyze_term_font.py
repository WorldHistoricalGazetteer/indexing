"""Scheduled GB-STAMP analysis: which text strings are FONT-AMBIGUOUS — i.e. the same word appears in more
than one OS lettering style, so its feature type must be font-conditioned (e.g. 'Camp' in blackletter = a
Roman camp/antiquity; in roman = a modern encampment/place). Mines the font-classified boxes and reports
the terms that occur across >=2 font styles, with per-font counts, ranked — the candidates for
font-conditioned typing rules in fuse_edition.FONT_COND.

    python analyze_term_font.py --gate 0.75 --min 4
"""
import argparse, json, re
from collections import defaultdict, Counter

FONT = "/vast/ishi/gb1900/edition/spot/boxes_font.jsonl"
GENERIC_WORD = re.compile(r"[A-Za-z]")

def norm(t):
    return re.sub(r"[^a-z]", "", (t or "").lower())

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--gate", type=float, default=0.75); ap.add_argument("--min", type=int, default=4)
    a = ap.parse_args()
    by = defaultdict(Counter); raw = {}
    for line in open(FONT):
        r = json.loads(line)
        if r["conf"] < a.gate: continue
        w = norm(r["text"])
        if len(w) < 3: continue
        by[w][r["font"]] += 1; raw.setdefault(w, r["text"].strip())
    amb = []
    for w, fonts in by.items():
        tot = sum(fonts.values())
        if len(fonts) >= 2 and tot >= a.min: amb.append((raw[w], tot, dict(fonts)))
    amb.sort(key=lambda x: -x[1])
    print(f"font-AMBIGUOUS terms (>=2 styles, >={a.min} occ, conf>={a.gate}): {len(amb)}")
    print(f"{'term':22s}{'total':>6s}   per-font")
    for term, tot, fonts in amb[:60]:
        frac = ", ".join(f"{f}:{c}" for f, c in sorted(fonts.items(), key=lambda kv: -kv[1]))
        print(f"  {term:20s}{tot:>6d}   {frac}")
    # also: strongly single-font terms (reliable, per-font high purity) for the unconditional lexicon
    pure = [(raw[w], sum(fonts.values()), next(iter(fonts))) for w, fonts in by.items()
            if len(fonts) == 1 and sum(fonts.values()) >= a.min * 2]
    pure.sort(key=lambda x: -x[1])
    print(f"\nstrongly SINGLE-font terms (>= {a.min*2} occ, one style): {len(pure)} (top 30)")
    for term, tot, f in pure[:30]:
        print(f"  {term:20s}{tot:>6d}   {f}")

if __name__ == "__main__":
    main()
