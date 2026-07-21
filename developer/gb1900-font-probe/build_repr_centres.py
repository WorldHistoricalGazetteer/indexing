"""Geographically representative centre sample for retraining the font reference. The full-coverage order is
north-first (Mercator-y sort), so the first ~1,100 spotted regions were all rural far-north Scotland — no
administrative labels. This bins GB into a lon/lat grid and, cell by cell, takes the MOST label-dense regions
first (round-robin across cells), so a prefix of the output is BOTH spatially spread across GB AND biased to
populated areas — where county/borough/parish labels actually occur.

    python build_repr_centres.py --n 1000              # centres_all.txt (with counts) -> centres_repr.txt
"""
import argparse
from collections import defaultdict

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="centres_all.txt"); ap.add_argument("--out", default="centres_repr.txt")
    ap.add_argument("--n", type=int, default=1000, help="target sample size")
    ap.add_argument("--cell", type=float, default=0.4, help="grid cell size in degrees")
    a = ap.parse_args()
    cells = defaultdict(list); tot = 0
    for line in open(a.src):
        p = line.split()
        if len(p) < 4: continue
        lon, lat = float(p[0]), float(p[1]); tag = p[2]; cnt = int(p[3]); tot += 1
        cells[(round(lon / a.cell), round(lat / a.cell))].append((cnt, lon, lat, tag))
    for k in cells: cells[k].sort(reverse=True)               # densest region first within each cell
    keys = list(cells); ptr = {k: 0 for k in keys}; out = []
    while len(out) < a.n:                                     # round-robin across cells -> spatial spread + density
        added = False
        for k in keys:
            if ptr[k] < len(cells[k]):
                out.append(cells[k][ptr[k]]); ptr[k] += 1; added = True
                if len(out) >= a.n: break
        if not added: break
    with open(a.out, "w") as f:
        for cnt, lon, lat, tag in out: f.write(f"{lon:.5f} {lat:.5f} {tag} {cnt}\n")
    lats = sorted(c[2] for c in out); labs = sorted(c[0] for c in out)
    print(f"{tot} regions in {len(cells)} cells -> {len(out)} sampled ({sum(c[0] for c in out)} labels)")
    print(f"  lat spread {lats[0]:.1f}..{lats[-1]:.1f} (vs north-first)"
          f"; region label-count median {labs[len(labs)//2]}, max {labs[-1]}")

if __name__ == "__main__":
    main()
