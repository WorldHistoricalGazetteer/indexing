"""Choose the sheets to run the big-font pass over, and build each one's MapReader coverage file.

The rare faces are administrative — parish, borough, hundred, poor-law union — so they are not concentrated in
any one place; they are thin everywhere. One sheet yielded seven samples across three previously-empty faces,
which is the right shape of result but not enough of it. The way to more is more sheets, spread out.

Two constraints decide which sheets are eligible:

* The non-coverage gate needs MapReader boxes for the SAME ground. The full-GB spot campaign is partial
  (1,543 of 35,514 centres), and its files are cut by campaign centre, not by sheet — so eligibility is
  measured by spatially filtering every box against each sheet, and a sheet with thin coverage is not
  eligible at all. Running one anyway would not fail; it would silently pass ordinary lettering through the
  gate as "MapReader missed this", which is the failure mode that matters here because it is invisible.
* Spread. Sheets are picked one per grid cell rather than by rank, so the sample is not fifty sheets of the
  same conurbation. Face repertoire varies with what a sheet CONTAINS — a borough boundary, a hundred, a
  parish name — and that varies geographically.

    python pick_sheets.py --n 40 --out sheets_bigfont.json
"""
import argparse, glob, json, math, os
from collections import defaultdict

N17 = 1 << 17


def lonlat_of(r):
    return r.get("lon"), r.get("lat")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheets", default="/vast/ishi/gb1900/sheets/os_6inch_2nd_GB_4326.geojson")
    ap.add_argument("--boxes", default="/vast/ishi/gb1900/edition/spot/boxes_*.jsonl")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--min-boxes", type=int, default=400,
                    help="a sheet with fewer MapReader boxes than this cannot support the coverage gate: the "
                         "page would fill with ordinary lettering wrongly described as 'MapReader missed it'")
    ap.add_argument("--cell", type=float, default=0.6,
                    help="degrees; one sheet per cell, so the sample spans GB rather than one conurbation")
    ap.add_argument("--cover-dir", default="/vast/ishi/gb1900/edition/amg/cover")
    ap.add_argument("--out", default="sheets_bigfont.json")
    a = ap.parse_args()

    gj = json.load(open(a.sheets))
    feats = gj["features"] if isinstance(gj, dict) else gj
    sheets = []
    for f in feats:
        p = f.get("properties", {})
        g = f.get("geometry")
        if not g:
            continue
        coords = g["coordinates"]
        while isinstance(coords[0][0], (list, tuple)):
            coords = coords[0]
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        # Match the naming already in use (sheet_ENG_218_NW): COUNTRY_SHEET_NO. Edition 2 only — the 1879
        # font-change regime is essentially absent from GB1900's coverage, and mixing editions would mix
        # two lettering conventions into one anchor set.
        if str(p.get("EDITION", "")).strip() != "2":
            continue
        name = f"{p.get('COUNTRY','XX')}_{p.get('SHEET_NO','?')}"
        sheets.append(dict(name=str(name).strip().replace(" ", "_").replace("/", "-"),
                           w=min(xs), s=min(ys), e=max(xs), n=max(ys),
                           props={k: p.get(k) for k in ("COUNTRY", "COUNTY", "SHEET_NO",
                                                        "PUB_STA", "SUR_STA") if k in p}))
    print(f"{len(sheets)} sheet polygons", flush=True)

    # Bucket sheets by a coarse grid so each box is tested against a handful of candidates, not all 16,450.
    grid = defaultdict(list)
    for i, sh in enumerate(sheets):
        for gx in range(int(sh["w"] * 10), int(sh["e"] * 10) + 1):
            for gy in range(int(sh["s"] * 10), int(sh["n"] * 10) + 1):
                grid[(gx, gy)].append(i)

    counts = defaultdict(int)
    boxes_by_sheet = defaultdict(list)
    nfiles = 0
    for fp in sorted(glob.glob(a.boxes)):
        nfiles += 1
        for line in open(fp):
            try:
                r = json.loads(line)
            except Exception:
                continue
            lon, lat = lonlat_of(r)
            if lon is None or lat is None or not r.get("gpoly"):
                continue
            for i in grid.get((int(lon * 10), int(lat * 10)), ()):
                sh = sheets[i]
                if sh["w"] <= lon <= sh["e"] and sh["s"] <= lat <= sh["n"]:
                    counts[i] += 1
                    boxes_by_sheet[i].append(r["gpoly"])
                    break
    print(f"{nfiles} box files, {sum(counts.values())} boxes placed on {len(counts)} sheets", flush=True)

    elig = sorted([i for i, c in counts.items() if c >= a.min_boxes], key=lambda i: -counts[i])
    print(f"{len(elig)} sheets with >= {a.min_boxes} boxes", flush=True)

    picked, seen_cell = [], set()
    for i in elig:
        sh = sheets[i]
        cell = (round((sh["w"] + sh["e"]) / 2 / a.cell), round((sh["s"] + sh["n"]) / 2 / a.cell))
        if cell in seen_cell:
            continue
        seen_cell.add(cell)
        picked.append(i)
        if len(picked) >= a.n:
            break
    # If spread ran out of cells before n, top up by coverage — better a second sheet in a well-covered
    # region than a thinly-covered one where the gate cannot be trusted.
    if len(picked) < a.n:
        for i in elig:
            if i not in picked:
                picked.append(i)
            if len(picked) >= a.n:
                break

    os.makedirs(a.cover_dir, exist_ok=True)
    out = []
    for i in picked:
        sh = sheets[i]
        cp = f"{a.cover_dir}/mr_{sh['name']}.jsonl"
        with open(cp, "w") as fh:
            for g in boxes_by_sheet[i]:
                fh.write(json.dumps({"gpoly": g}) + "\n")
        out.append(dict(name=sh["name"], bbox=[sh["w"], sh["s"], sh["e"], sh["n"]],
                        mr_boxes=counts[i], cover=cp, props=sh["props"]))
    json.dump(dict(sheets=out), open(a.out, "w"), indent=1)
    print(f"\npicked {len(out)} sheets:")
    for r in out[:12]:
        print(f"  {r['name']:22s} {r['mr_boxes']:>6d} boxes   "
              f"{r['bbox'][0]:.3f},{r['bbox'][1]:.3f}")
    if len(out) > 12:
        print(f"  ... and {len(out)-12} more")
    print(f"wrote {a.out}")
    print("PICKDONE", flush=True)


if __name__ == "__main__":
    main()
