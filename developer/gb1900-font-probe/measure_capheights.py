"""Phase B step 1 — measure the CAP-HEIGHT of every Characteristic Sheet exemplar (baseline → cap-top,
EXCLUDING descenders), in Characteristic-Sheet NATIVE PIXELS (one scan, so px are comparable across all
exemplars). This gives the relative size structure per category; true paper-mm calibration follows.

Baseline = the modal bottom-of-ink across columns (most glyphs sit on it; descenders drop below it).
Cap-top = the topmost ink row. cap_height_px(crop) → native via the manifest scale (native_w / crop_w).

    python measure_capheights.py            # prints a table + writes reference/cap_heights.json
"""
import os, json, numpy as np
from PIL import Image
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__)); REF = os.path.join(HERE, "reference")
MAN = json.load(open(os.path.join(REF, "ex_manifest.json")))

# categories whose SIZE is variable (only these two groups per the Characteristic Sheet)
SIZE_VAR = {"ex_bogs_moors_word", "ex_forests_word", "ex_ranges_hills"}

def cap_height_px(im):
    """Return (cap_top, baseline, height) in crop pixels. Baseline excludes descenders via the modal
    column-bottom; cap_top is the topmost ink row."""
    g = np.asarray(im.convert("L"), np.float32); ink = g < 128
    H, W = ink.shape
    cols = [c for c in range(W) if ink[:, c].any()]
    if not cols: return None
    tops, bots = [], []
    for c in cols:
        rows = np.where(ink[:, c])[0]
        tops.append(int(rows[0])); bots.append(int(rows[-1]))
    cap_top = min(tops)
    # baseline = modal column-bottom (bin to 2px). Descenders are a minority below it.
    binned = Counter(round(b / 2) * 2 for b in bots)
    baseline = max(binned, key=lambda k: (binned[k], k))    # most common, prefer lower on ties
    # guard: baseline shouldn't be above the median bottom (i.e. mostly ascenders) — use max of modal / median-ish
    baseline = max(baseline, int(np.percentile(bots, 60)))
    return cap_top, baseline, baseline - cap_top

def main():
    rows = []
    for key, m in MAN.items():
        p = os.path.join(REF, f"{key}.jpg")
        if not os.path.exists(p): continue
        im = Image.open(p); r = cap_height_px(im)
        if not r: continue
        cap_top, baseline, ch_crop = r
        scale = m["w"] / im.size[0]                          # native px per crop px (uniform)
        ch_native = ch_crop * scale
        rows.append({"key": key, "cap_h_native_px": round(ch_native, 1),
                     "crop_ch_px": ch_crop, "crop_size": list(im.size),
                     "native_wh": [m["w"], m["h"]], "size_variable": key in SIZE_VAR})
    rows.sort(key=lambda r: -r["cap_h_native_px"])
    json.dump(rows, open(os.path.join(REF, "cap_heights.json"), "w"), indent=1)
    print(f"{'exemplar':30s} {'cap-h (CS px)':>13s}  {'crop':>10s}  var")
    for r in rows:
        print(f"{r['key']:30s} {r['cap_h_native_px']:>13.1f}  {str(tuple(r['crop_size'])):>10s}  {'*' if r['size_variable'] else ''}")
    print(f"\n{len(rows)} exemplars measured -> reference/cap_heights.json")

if __name__ == "__main__":
    main()
