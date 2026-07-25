"""How many crops contain no text at all? A QC pass the labelling round proved is necessary.

Re-presenting the 9 cards a labeller could not label showed the real cause: several had a mask sitting on blank
paper or building hatching, with the transcript's label nowhere inside the crop. That is not a display problem
— it means the descriptor for those pins was computed on map furniture and carries no font information, while
still being indexed under a real GB1900 transcript. A human refused to label them; nothing in the automated
pipeline noticed.

`on_ink` did not catch this: it asks whether the pin lies inside the WORD mask, and a mask over blank ground
still contains the pin. The direct question is whether the CROP CONTAINS INK, and how much relative to the
number of characters the transcript claims.

Samples the corpus for a rate, and reports the labelled cards separately so the "unclear" verdicts can be
checked against the measure — if the measure is any good, the 9 unclear should be among the worst.

    python crop_qc.py --sample 6000 --labels "pool_labels_round (4).json"
"""
import argparse, glob, json, os, random, sys, numpy as np, cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate

PINS = "/vast/ishi/gb1900/edition/pins"


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def ink_stats(crop):
    """Fraction of the crop that is ink, and ink area per transcript character.

    Otsu on the crop: OS sheets are near-monochrome, so the split is clean. A crop of blank paper has almost
    no dark pixels; a crop of hatching has many but they are structured, so the fraction alone cannot tell
    those apart — which is why the per-character figure matters more than the raw fraction.
    """
    g = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    if g.size < 80:
        return None
    _, ink = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    frac = float((ink > 0).mean())
    # Otsu always splits SOMETHING, even pure paper with a faint gradient; require the dark class to be
    # genuinely darker than the light class before believing it is ink.
    dark = g[ink > 0].mean() if (ink > 0).any() else 255.0
    light = g[ink == 0].mean() if (ink == 0).any() else 255.0
    contrast = float(light - dark)
    return frac, contrast


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pins-dir", default=PINS)
    ap.add_argument("--labels", default=None)
    ap.add_argument("--sample", type=int, default=6000)
    ap.add_argument("--min-contrast", type=float, default=40.0, help="below this the 'ink' is paper texture")
    ap.add_argument("--out", default=f"{PINS}/crop_qc.json")
    a = ap.parse_args()
    random.seed(42)

    verified = {}
    if a.labels and os.path.exists(a.labels):
        verified = {key(x["gcx"], x["gcy"]): x.get("sig") for x in json.load(open(a.labels))}

    recs = []
    for f in sorted(glob.glob(f"{a.pins_dir}/pins_*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("line_gpoly") or r.get("gpoly"):
                recs.append(r)
    print(f"{len(recs)} detections", flush=True)

    labelled = [r for r in recs if key(r["gcx"], r["gcy"]) in verified]
    pool = random.sample(recs, min(a.sample, len(recs)))
    print(f"sampling {len(pool)}; plus {len(labelled)} labelled cards", flush=True)

    def measure(rows, name):
        out = []
        for r in rows:
            crop = derotate({"gpoly": r.get("line_gpoly") or r.get("gpoly")})
            if crop is None:
                continue
            st = ink_stats(crop)
            if st is None:
                continue
            frac, contrast = st
            n = max(1, sum(1 for c in r.get("text", "") if not c.isspace()))
            out.append(dict(frac=frac, contrast=contrast, per_char=frac * crop.size / n,
                            text=r.get("text", ""), gcx=r["gcx"], gcy=r["gcy"],
                            blank=bool(contrast < a.min_contrast)))
        if out:
            fr = np.array([x["frac"] for x in out])
            ct = np.array([x["contrast"] for x in out])
            nb = sum(x["blank"] for x in out)
            print(f"\n{name}: n={len(out)}", flush=True)
            print(f"  ink fraction  median {np.median(fr):.3f}  p10 {np.percentile(fr,10):.3f}", flush=True)
            print(f"  ink contrast  median {np.median(ct):.0f}  p10 {np.percentile(ct,10):.0f}", flush=True)
            print(f"  BLANK (contrast < {a.min_contrast:.0f}): {nb} = {nb/len(out):.2%}", flush=True)
        return out

    corpus = measure(pool, "corpus sample")
    lab = measure(labelled, "labelled cards")

    if lab and verified:
        unclear = [x for x in lab if not verified.get(key(x["gcx"], x["gcy"]))]
        clear = [x for x in lab if verified.get(key(x["gcx"], x["gcy"]))]
        if unclear and clear:
            ub = sum(x["blank"] for x in unclear)
            cb = sum(x["blank"] for x in clear)
            print(f"\n  cards the labeller marked UNCLEAR: {ub}/{len(unclear)} blank ({ub/len(unclear):.0%})",
                  flush=True)
            print(f"  cards the labeller LABELLED:      {cb}/{len(clear)} blank ({cb/len(clear):.0%})",
                  flush=True)
            print("  -> the human was detecting exactly this failure" if ub / len(unclear) > 2 * max(1e-9, cb / len(clear))
                  else "  -> blankness does NOT explain the unclear verdicts; look elsewhere", flush=True)
        print("\n  blank examples:", flush=True)
        for x in sorted(lab, key=lambda z: z["contrast"])[:6]:
            print(f"    contrast {x['contrast']:5.0f}  frac {x['frac']:.3f}  {x['text'][:40]!r}", flush=True)

    json.dump(dict(sampled=len(corpus), blank_rate=sum(x["blank"] for x in corpus) / max(1, len(corpus)),
                   min_contrast=a.min_contrast,
                   labelled=len(lab), labelled_blank=sum(x["blank"] for x in lab)),
              open(a.out, "w"), indent=2)
    print(f"\nwrote {a.out}\nCROPQCDONE", flush=True)


if __name__ == "__main__":
    main()
