"""Choose the stratified Phase-A sample: which regions to localise, and why.

Full-corpus typing is deployment, not the paper — the paper needs enough entries per signature to put error
bars on, spread across the country. Two design choices matter:

  * CLUSTER-stratified, not pin-stratified. 2.5M pins scattered at random would mean fetching ~16 z17 tiles per
    pin; sampling REGIONS (the existing 17x17-tile centres, ~150-400 pins each) keeps windows dense, so tiles
    amortise across many labels and the run is a targeted prefetch rather than a full-GB crawl.
  * Selection is greedy on CATEGORY DEFICIT with a geographic penalty. Picking the densest regions would buy
    entries cheaply but would be all city centre — over-representing roads and street names and starving
    antiquities, which is the imbalance that hurt the last measurement.

Emits `centres_sample.txt` in the same `lon lat tag count` format as centres_all.txt, so it drops straight into
the existing resumable Slurm array pattern.

    python build_sample.py --target 20000 --per-category 600
"""
import argparse, json, os, sys, numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_pin_index import load_pins, pins_in_box
from pin_category_coverage import COMPILED, is_admin_shape

HERE = "/vast/ishi/gb1900/probe/font"
N17 = 2 ** 17
CATS = [c for c, _ in COMPILED] + ["admin_shape"]


def categorise(texts):
    """Weak category labels from the transcript — the same lexicons the labelling bootstrap will use."""
    out = np.zeros((len(texts), len(CATS)), bool)
    for j, (_, rx) in enumerate(COMPILED):
        out[:, j] = [bool(rx.search(t)) for t in texts]
    lexical = out[:, :len(COMPILED)].any(1)
    out[:, -1] = [is_admin_shape(t) for t in texts] & ~lexical
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--centres", default=f"{HERE}/centres_all.txt")
    ap.add_argument("--out", default=f"{HERE}/centres_sample.txt")
    ap.add_argument("--target", type=int, default=20000, help="total GB1900 entries wanted")
    ap.add_argument("--per-category", type=int, default=600,
                    help="minimum weakly-labelled entries per category (headroom over the ~30-50 verified)")
    ap.add_argument("--r", type=int, default=8, help="region radius in tiles, must match the hisam_pins run")
    ap.add_argument("--spread-km", type=float, default=100.0, help="cell size for the geographic penalty")
    ap.add_argument("--min-pins", type=int, default=40, help="skip regions too sparse to be worth a job")
    a = ap.parse_args()

    P = load_pins(a.pins)
    texts_all = P["text"].astype(str)
    centres = []
    for line in open(a.centres):
        p = line.split()
        if len(p) >= 3:
            centres.append((float(p[0]), float(p[1]), p[2]))
    print(f"{len(centres)} candidate regions, {len(texts_all)} pins", flush=True)

    side = 2 * a.r + 1
    # ~1 z17 px = 156543*cos(lat)/2^17 m; at GB latitudes ~0.8 m, so a 100 km cell is ~125k px. Good enough for
    # a spread penalty — this is not a projection, just a bucketing.
    cell_px = a.spread_km * 1000.0 / 0.8

    regions = []
    for lon, lat, tag in centres:
        cxp = (lon + 180.0) / 360.0 * N17 * 256
        cyp = (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / np.pi) / 2 * N17 * 256
        tx0, ty0 = int(cxp // 256) - a.r, int(cyp // 256) - a.r
        idx = pins_in_box(P, tx0 * 256, ty0 * 256, (tx0 + side) * 256, (ty0 + side) * 256)
        if len(idx) < a.min_pins:
            continue
        cats = categorise(texts_all[idx]).sum(0)
        regions.append(dict(lon=lon, lat=lat, tag=tag, n=len(idx), cats=cats,
                            cell=(int(cxp // cell_px), int(cyp // cell_px))))
    print(f"{len(regions)} regions with >={a.min_pins} pins", flush=True)
    if not regions:
        raise SystemExit("no regions qualify — check --pins and --centres")

    C = np.array([r["cats"] for r in regions], float)             # region x category
    NP = np.array([r["n"] for r in regions], float)
    cell_ids = {c: i for i, c in enumerate({r["cell"] for r in regions})}
    cidx = np.array([cell_ids[r["cell"]] for r in regions])
    visits = np.zeros(len(cell_ids))

    deficit = np.full(len(CATS), float(a.per_category))
    taken = np.zeros(len(regions), bool)
    chosen, total = [], 0
    while (total < a.target or deficit.max() > 0) and not taken.all():
        gain = np.minimum(deficit[None, :], C).sum(1)
        if total < a.target:
            gain = gain + 0.05 * np.minimum(NP, a.target - total)  # generic entries still count to the total
        # Diminishing returns for repeat visits to the same ~100 km cell: the second region in a cell is worth
        # half, the third a third, and so on. Keeps the sample national without banning clusters outright.
        gain = gain / (1.0 + visits[cidx])
        gain[taken] = -1.0
        k = int(np.argmax(gain))
        if gain[k] <= 0:
            break
        best = regions[k]
        taken[k] = True
        chosen.append(best)
        visits[cidx[k]] += 1
        deficit = np.maximum(0.0, deficit - best["cats"])
        total += best["n"]

    got = np.sum([r["cats"] for r in chosen], 0)
    used_cells = {c for c, v in zip(cell_ids, visits) if v > 0}
    print(f"\nselected {len(chosen)} regions, {total} entries, {int((visits > 0).sum())} distinct "
          f"{a.spread_km:.0f} km cells", flush=True)
    for j, c in enumerate(CATS):
        flag = "" if got[j] >= a.per_category else "   << SHORT"
        print(f"  {c:14s} {int(got[j]):>7d}{flag}", flush=True)
    short = [CATS[j] for j in range(len(CATS)) if got[j] < a.per_category]
    if short:
        print(f"NOTE: {len(short)} categories under target ({', '.join(short)}) — the corpus simply has fewer "
              f"of these in dense regions; raise --target or accept the cap, but do not silently rebalance.",
              flush=True)

    with open(a.out, "w") as f:
        for r in chosen:
            f.write(f"{r['lon']:.5f} {r['lat']:.5f} {r['tag']} {r['n']}\n")
    json.dump(dict(regions=len(chosen), entries=int(total), cells=int((visits > 0).sum()),
                   per_category={CATS[j]: int(got[j]) for j in range(len(CATS))},
                   target=a.target, per_category_target=a.per_category),
              open(a.out.replace(".txt", ".json"), "w"), indent=2)
    print(f"wrote {a.out}", flush=True)
    print("SAMPLEDONE", flush=True)


if __name__ == "__main__":
    main()
