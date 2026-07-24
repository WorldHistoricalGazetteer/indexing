"""How much weak-labelled material does the bootstrap actually yield, per signature?

Phase C's viability rests on the lexicon supplying enough candidates for the signatures the discriminator
FAILS on (NEXT-PHASE.md §1.3: upright·solid·serif at 0.071, numeral·solid·plain at 0.000) — not on the ones it
already handles. Counting overall yield would hide that. Runs over the pin-prompted detections produced so far,
so it can be re-run as the sample fills.

    python weak_label_report.py            # CPU, seconds
"""
import argparse, glob, json, os, sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weak_sig import weak_sig

PINS = "/vast/ishi/gb1900/edition/pins"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pins-dir", default=PINS)
    ap.add_argument("--examples", type=int, default=6)
    ap.add_argument("--out", default=f"{PINS}/weak_label_report.json")
    a = ap.parse_args()

    files = sorted(glob.glob(f"{a.pins_dir}/pins_*.jsonl"))
    sigs = Counter()
    rules = Counter()
    ex = defaultdict(list)
    n = unlabelled = 0
    for f in files:
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            n += 1
            s, rule = weak_sig(r.get("text", ""))
            if not s:
                unlabelled += 1
                continue
            sigs[s] += 1
            rules[rule] += 1
            if len(ex[s]) < a.examples:
                ex[s].append(r.get("text", ""))

    print(f"{len(files)} region files, {n} detections, {n - unlabelled} weakly labelled "
          f"({(n-unlabelled)/max(1,n):.1%}); {unlabelled} carry no type word — correctly left unlabelled",
          flush=True)
    for s, c in sigs.most_common():
        print(f"  {s:32s} {c:>7d}   e.g. {', '.join(ex[s][:3])}", flush=True)
    print("\nby rule:", flush=True)
    for r, c in rules.most_common():
        print(f"  {r:28s} {c:>7d}", flush=True)

    # The two signatures Phase B showed collapsing — the only ones whose supply actually gates Phase C.
    print("\nsupply for the FAILING signatures (§1.3):", flush=True)
    for s in ("upright·solid·serif", "numeral·solid·plain"):
        print(f"  {s:32s} {sigs.get(s, 0):>7d}", flush=True)

    json.dump(dict(files=len(files), detections=n, weak_labelled=n - unlabelled,
                   per_signature=dict(sigs), per_rule=dict(rules),
                   examples={k: v for k, v in ex.items()}),
              open(a.out, "w"), indent=2, ensure_ascii=False)
    print(f"\nwrote {a.out}\nWEAKREPORTDONE", flush=True)


if __name__ == "__main__":
    main()
