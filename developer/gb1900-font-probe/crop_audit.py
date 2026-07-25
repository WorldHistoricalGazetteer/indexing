"""Did the line mask actually CUT LABELS OFF — and if so, are the descriptors truncated too?

A human labelling round reported that some snippets showed only part of a label, or were too tight to tell
which of several similarly-named labels was meant. The display can be fixed with padding, but the same
`line_gpoly` is what `extract_descriptors_pins.py` crops, so if the masks really are short then the descriptor
bank is measuring fragments and the display was only the symptom.

Test, with a built-in control: the labelling export marks 9 cards unclear and 231 clear. A label's crop aspect
(long side / short side) should grow with its transcript length, since it is one line of text. If the unclear
cards sit systematically BELOW the length-aspect trend the clear ones follow, the mask was short — the cause is
truncation, not human hesitancy. If they sit on the trend, the crops were complete and the problem was purely
missing context, which is a display fix only.

Reports the same statistic corpus-wide so the truncation rate is a number, not an impression.

    python crop_audit.py --labels "labels/pool_labels_round (4).json"
"""
import argparse, glob, json, os, sys, numpy as np

PINS = "/vast/ishi/gb1900/edition/pins"


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def poly_wh(poly):
    """Long and short side of a 4-point minAreaRect polygon."""
    p = np.asarray(poly, float)
    e = [float(np.hypot(*(p[(i + 1) % 4] - p[i]))) for i in range(4)]
    return max(e[0], e[1]), min(e[0], e[1])


def nchars(t):
    return sum(1 for c in t if not c.isspace())


def stats(rows, label):
    if not rows:
        print(f"  {label:10s} n=0")
        return None
    r = np.array([x["ratio"] for x in rows])
    print(f"  {label:10s} n={len(rows):<4d} aspect/char median {np.median(r):.3f}  "
          f"p10 {np.percentile(r,10):.3f}  p90 {np.percentile(r,90):.3f}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--pins-dir", default=PINS)
    ap.add_argument("--out", default=f"{PINS}/crop_audit.json")
    a = ap.parse_args()

    lab = json.load(open(a.labels))
    want = {key(x["gcx"], x["gcy"]): x for x in lab}
    print(f"{len(lab)} labelled cards ({sum(1 for x in lab if not x.get('sig'))} unclear)", flush=True)

    recs = {}
    allrows = []
    for f in sorted(glob.glob(f"{a.pins_dir}/pins_*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            poly = r.get("line_gpoly") or r.get("gpoly")
            if not poly:
                continue
            w, h = poly_wh(poly)
            n = nchars(r.get("text", ""))
            if h <= 0 or n < 2:
                continue
            row = dict(ratio=(w / h) / n, w=w, h=h, n=n, text=r.get("text", ""),
                       truncated=bool(r.get("truncated")), on_ink=bool(r.get("on_ink")))
            allrows.append(row)
            k = key(r["gcx"], r["gcy"])
            if k in want:
                recs[k] = row

    print(f"joined {len(recs)}/{len(lab)} labelled cards to their detections", flush=True)
    unclear = [recs[k] for k, v in want.items() if k in recs and not v.get("sig")]
    clear = [recs[k] for k, v in want.items() if k in recs and v.get("sig")]

    print("\naspect-per-character (higher = more of the label present):", flush=True)
    ru = stats(unclear, "unclear")
    rc = stats(clear, "clear")
    stats(allrows, "corpus")

    verdict = None
    if ru is not None and rc is not None and len(ru) >= 3:
        # Mann-Whitney U via rank sums — no scipy in this env, and n=9 makes any parametric test meaningless.
        both = np.concatenate([ru, rc])
        order = both.argsort().argsort() + 1.0
        n1 = len(ru)
        u1 = order[:n1].sum() - n1 * (n1 + 1) / 2
        auc = u1 / (n1 * len(rc))                       # P(unclear > clear); 0.5 = no difference
        print(f"\n  P(unclear card has a HIGHER aspect/char than a clear one) = {auc:.2f}", flush=True)
        verdict = ("truncation" if auc < 0.35 else "context" if auc > 0.65 else "inconclusive")
        print(f"  -> {'unclear cards are systematically SHORTER: truncation is real' if auc < 0.35 else ''}"
              f"{'unclear cards are NOT shorter: the crops were complete, so this is a DISPLAY/context problem' if auc >= 0.35 else ''}",
              flush=True)

    # Corpus-wide: how many crops look short for their transcript, against the corpus's own trend?
    r_all = np.array([x["ratio"] for x in allrows])
    thresh = float(np.percentile(r_all, 10))
    short = [x for x in allrows if x["ratio"] < thresh]
    print(f"\ncorpus: {len(allrows)} crops; {len(short)} ({len(short)/len(allrows):.1%}) below the 10th-percentile "
          f"aspect/char of {thresh:.3f}", flush=True)
    print(f"  window-edge truncated flag set on {sum(x['truncated'] for x in allrows)/len(allrows):.2%}; "
          f"prompt on ink {sum(x['on_ink'] for x in allrows)/len(allrows):.1%}", flush=True)
    print("  shortest examples:", flush=True)
    for x in sorted(allrows, key=lambda z: z["ratio"])[:8]:
        print(f"    {x['ratio']:.3f}  {x['w']:.0f}x{x['h']:.0f}px  {x['text'][:44]!r}", flush=True)

    json.dump(dict(labelled=len(lab), joined=len(recs), unclear=len(unclear), clear=len(clear),
                   verdict=verdict,
                   median_ratio=dict(unclear=float(np.median(ru)) if ru is not None else None,
                                     clear=float(np.median(rc)) if rc is not None else None,
                                     corpus=float(np.median(r_all))),
                   short_rate=len(short) / len(allrows)),
              open(a.out, "w"), indent=2)
    print(f"\nwrote {a.out}\nCROPAUDITDONE", flush=True)


if __name__ == "__main__":
    main()
