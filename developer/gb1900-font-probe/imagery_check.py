"""Did the spotter have a map to look at? Separates 'looked and found nothing' from 'never looked'.

A region's z17 tiles live on /vast until --cleanup archives them to /ix1, so a region that ran to completion
has its tiles in one store or the other. Counting them is independent of what the spotter reported, which
matters: gating on the spotter's own output would quietly define away the regions where it failed.
"""
import glob, json, math, os
import numpy as np
N17 = 2 ** 17
STORES = ["/ix1/ishi/gb1900/tiles17", "/vast/ishi/gb1900/tiles17"]

def px(lon, lat):
    return ((lon + 180) / 360 * N17 * 256,
            (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256)

centres = {}
for l in open("/vast/ishi/gb1900/probe/font/centres_all.txt"):
    p = l.split()
    if len(p) >= 3:
        centres[p[2]] = (float(p[0]), float(p[1]), int(p[3]) if len(p) > 3 else 0)

rows = []
for f in glob.glob("/vast/ishi/gb1900/edition/spot/boxes_*.jsonl"):
    tag = os.path.basename(f)[6:-6]
    if tag not in centres or os.path.getsize(f) == 0:
        continue
    lon, lat, npin = centres[tag]
    cx, cy = px(lon, lat)
    ctx, cty = int(cx // 256), int(cy // 256)
    have = 0
    for tx in range(ctx - 8, ctx + 9):
        for st in STORES:
            d = f"{st}/{tx}"
            if not os.path.isdir(d):
                continue
            for ty in range(cty - 8, cty + 9):
                p_ = f"{d}/{ty}.png"
                if os.path.exists(p_) and os.path.getsize(p_) > 500:
                    have += 1
    n = sum(1 for _ in open(f))
    rows.append(dict(tag=tag, dets=n, tiles=have, pins=npin))

json.dump(rows, open("/vast/ishi/gb1900/probe/font/imagery_check.json", "w"))
A = np.array([[r["dets"], r["tiles"], r["pins"]] for r in rows], float)
print(f"{len(rows)} completed regions with a centre")
for lo, hi, lbl in ((260, 290, "full imagery (>=260/289 tiles)"),
                    (30, 260, "partial imagery"),
                    (0, 30, "no imagery found")):
    m = (A[:, 1] >= lo) & (A[:, 1] < hi)
    if m.sum():
        dpp = A[m, 0] / np.maximum(1, A[m, 2])
        print(f"  {lbl:34} {m.sum():4d} regions | dets/region median {np.median(A[m,0]):7.0f} | "
              f"pins median {np.median(A[m,2]):6.0f} | dets per pin median {np.median(dpp):5.2f}")
# The gate that matters: regions producing implausibly little text for the amount GB1900 pinned there.
dpp = A[:, 0] / np.maximum(1, A[:, 2])
sus = (A[:, 2] >= 50) & (dpp < 0.25)
print(f"\n{sus.sum()} regions have >=50 pins but under 0.25 detections per pin — the spotter "
      f"cannot have run properly there")
for r in sorted([r for r, s in zip(rows, sus) if s], key=lambda r: -r["pins"])[:12]:
    print(f"    {r['tag']:16} pins {r['pins']:5d} dets {r['dets']:6d} tiles {r['tiles']:4d}")
