"""Show that lettering SIZE is measurable (free) from the spotter box, orthogonal to style.
Reproduce the label-manifest crop order, map each human label to its box, report ground
cap-height (z16 px -> metres) per style + the '###ath' large-italic example.
"""
import json, glob, math, numpy as np
from collections import defaultdict
import data as DATA

BOXES = "/vast/ishi/gb1900/probe/mapreader_text/region/boxes/worker*.jsonl"
TILES = ["/vast/ishi/gb1900/probe/mapreader_text/region/tiles", "/vast/ishi/gb1900/tiles/16"]
LABELS = "/vast/ishi/gb1900/probe/font/font_labels.json"

def box_h_m(b):
    ys = [p[1] for p in b["gpoly"]]
    h_px = max(ys) - min(ys)
    yy = (min(ys) + max(ys)) / 2 / 256.0                       # global z16 tile-y
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / (2**16)))))
    res = 40075016.686 * math.cos(math.radians(lat)) / (2**24)  # m per z16 px
    return h_px * res

rng = np.random.RandomState(0)
boxes = []
for f in glob.glob(BOXES):
    for line in open(f):
        line = line.strip()
        if line: boxes.append(json.loads(line))
rng.shuffle(boxes)
kept = []
for b in boxes:
    if len(kept) >= 2500: break
    if DATA.crop_box(b["gpoly"], TILES) is not None:
        kept.append(b)
print("kept:", len(kept), flush=True)

labels = json.load(open(LABELS))
by = defaultdict(list)
for r in labels:
    i = int(r["id"].split("_")[-1])
    if i < len(kept):
        by[r["label"]].append((r["text"], box_h_m(kept[i])))

print("\nground cap-height (m) by labelled style:")
for st, items in sorted(by.items()):
    hs = np.array(sorted(h for _, h in items))
    print("  %-14s n=%2d  median %5.1f  p10 %5.1f  p90 %5.1f  range %.1f-%.1f"
          % (st, len(hs), np.median(hs), np.percentile(hs, 10), np.percentile(hs, 90), hs.min(), hs.max()))

print("\nserif_italic sizes sorted (m):", [round(h, 1) for _, h in sorted(by.get("serif_italic", []), key=lambda t: t[1])])
for r in labels:
    if "ath" in (r["text"] or "").lower():
        i = int(r["id"].split("_")[-1])
        print("  '###ath' ->", round(box_h_m(kept[i]), 1), "m  (vs serif_italic median above)")
