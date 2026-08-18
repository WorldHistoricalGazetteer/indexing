"""How far do GB1900 and GB-STAMP agree, per OS sheet, on NON-NUMERIC labels?

Two questions, in both directions:

  GB-STAMP MISSES   a label a GB1900 volunteer transcribed, that the spotter did not detect
  GB1900 MISSES     text the spotter detected and read, that no volunteer ever pinned

Neither side is ground truth, so neither figure is an error rate. GB1900 is a crowd transcription with known
gaps; GB-STAMP is a detector with its own failure modes. What the pair gives is the SIZE OF THE DISAGREEMENT,
which is what decides whether the spotter is worth running over the whole series to check and extend the
crowd's work.

THE TRAP THIS SCRIPT EXISTS TO AVOID. Outside a spotted area "no detection" means NOT LOOKED AT, and
counting it as a miss would invent an arbitrarily bad recall figure. Every count here is therefore taken
inside the MEASURABLE FOOTPRINT: the sheet polygon intersected with the union of spotted regions.

As of 4 Aug 2026 the WHOLE series is spotted — all 35,514 regions, 16.77M detections — so the footprint is
now essentially the entire country and this guard is close to a no-op. It still earns its place in two
ways: at the OUTER boundary of the spotted union (the coast and the series edge), where the margin below is
a real cut; and as insurance, because a future partial re-spot would silently reintroduce exactly the
contamination that made the first attempt at these figures unquotable. Do not remove it because coverage
happens to be complete today.

`--spot-dir` MUST be set to the corpus-fed pass (`.../edition/spot2`). The default below still names the
old network-fed directory, whose boxes files were deleted on 4 Aug — pointing at it silently yields a ~100%
GB-STAMP miss rate that looks like a finding rather than an empty directory.

Note where the erosion goes. Spotting runs on a grid of overlapping regions (~500px of overlap, far more than
a label is long), so a label cut by the edge of one region is whole in its neighbour and no margin is needed
BETWEEN spotted regions — eroding each square individually would discard good interior area for a problem the
overlap has already solved. The margin is applied to the OUTER boundary of the spotted union only, where the
neighbour has not been run yet and the cut is real.

THREE ASYMMETRIES THAT WOULD OTHERWISE BE READ AS ERRORS:
  * MapReader is a WORD spotter and GB1900 pins a WHOLE LABEL. "Middleton Moor" is one pin and two boxes, so
    the second box looks unpinned. Reported both ways — strictly, and after crediting a detection whose text
    is a word of a nearby pin's text.
  * A GB1900 pin sits at the START of a label and often just off the ink, so matching needs a radius. The
    headline is quoted at several radii rather than at one flattering choice.
  * GB1900 volunteers skipped numerals wholesale. Numeric-only strings are dropped from BOTH sides — this is
    what "non-numeric" means here — because keeping them would make the crowd look bad for a documented
    convention rather than for a miss.

    python bench_sheets.py --plan                 # which sheets have enough coverage to measure
    python bench_sheets.py --sheets 12 --out bench_sheets.json
"""
import argparse, glob, json, math, os, re, sys
from collections import defaultdict
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_pin_index import load_pins, pins_in_box

N17 = 2 ** 17
SHEETS = "/vast/ishi/gb1900/sheets/os_6inch_2nd_GB_4326.geojson"
SPOT = "/vast/ishi/gb1900/edition/spot"
CENTRES = "centres_all.txt"


def lonlat_px(lon, lat):
    x = (lon + 180.0) / 360.0 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y


def norm(s):
    """Fold what is transcription convention rather than reading difference."""
    s = (s or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", s)


def tokens(s):
    return set(t for t in (norm(w) for w in re.split(r"[\s\-/]+", s or "")) if t)


def is_alpha(s):
    """Non-numeric = carries at least one letter. 'B.M. 412.7' counts, '412.7' does not."""
    return bool(re.search(r"[a-z]", (s or "").lower()))


def m_per_px(lat):
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** 17)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="report coverage per sheet and stop")
    ap.add_argument("--sheets", type=int, default=12, help="how many sheets to assess")
    ap.add_argument("--max-per-county", type=int, default=2,
                    help="cap per county so one well-covered county cannot supply the whole sample")
    ap.add_argument("--min-cover-km2", type=float, default=4.0,
                    help="a sheet needs this much measurable footprint to be worth quoting")
    ap.add_argument("--edge", type=float, default=160.0,
                    help="px eroded from the OUTER boundary of the spotted union, where no neighbouring "
                         "region has run yet and a cut label really is cut. Not applied between adjacent "
                         "spotted regions — their ~500px overlap already covers that")
    ap.add_argument("--dump-misses", default=None,
                    help="jsonl of every GB1900 label the spotter did not find, for recovery and inspection")
    ap.add_argument("--radius", type=float, default=48.0, help="px slack when matching a pin to a detection")
    ap.add_argument("--radii", type=float, nargs="*", default=[24.0, 48.0, 96.0],
                    help="the headline is quoted across these, so it is not one flattering choice")
    ap.add_argument("--word-radius-mult", type=float, default=4.0,
                    help="a detection reading a WORD of a pinned label may sit this many radii from the pin")
    ap.add_argument("--min-score", type=float, default=0.0, help="drop detections below this spotter score")
    ap.add_argument("--sheet-index", default=SHEETS)
    ap.add_argument("--spot-dir", default=SPOT)
    ap.add_argument("--centres", default=CENTRES)
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--out", default="bench_sheets.json")
    a = ap.parse_args()

    from shapely.geometry import Polygon, MultiPolygon, Point, box, shape
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    from shapely.prepared import prep

    # ---- spotted regions: where the spotter has actually looked -------------------------------------
    centres = {}
    for line in open(a.centres):
        p = line.split()
        if len(p) >= 3:
            centres[p[2]] = (float(p[0]), float(p[1]))
    done = {}
    for f in glob.glob(os.path.join(a.spot_dir, "boxes_*.jsonl")):
        if os.path.getsize(f) == 0:
            continue
        tag = os.path.basename(f)[6:-6]
        if tag in centres:
            done[tag] = f
    print(f"{len(done)} spotted regions with a known centre "
          f"(of {len(glob.glob(os.path.join(a.spot_dir,'boxes_*.jsonl')))} files, {len(centres)} centres)")

    reg = {}
    for tag, path in done.items():
        lon, lat = centres[tag]
        cx, cy = lonlat_px(lon, lat)
        ctx, cty = int(cx // 256), int(cy // 256)
        x0, y0 = (ctx - 8) * 256, (cty - 8) * 256
        side = 17 * 256
        reg[tag] = (box(x0, y0, x0 + side, y0 + side), path)
    rtags = list(reg)
    rtree = STRtree([reg[t][0] for t in rtags])
    # Erode the OUTER boundary of the spotted area only. Between neighbours the overlap already keeps labels
    # whole, so the union is taken first and the margin applied to it — not to each square in turn.
    spotted = unary_union([reg[t][0] for t in rtags])
    ov = np.median([reg[rtags[i]][0].intersection(reg[rtags[j]][0]).bounds[2]
                    - reg[rtags[i]][0].intersection(reg[rtags[j]][0]).bounds[0]
                    for i in range(min(40, len(rtags)))
                    for j in [int(k) for k in rtree.query(reg[rtags[i]][0])]
                    if j != i and reg[rtags[i]][0].intersects(reg[rtags[j]][0])
                    and not reg[rtags[i]][0].intersection(reg[rtags[j]][0]).is_empty] or [0])
    print(f"  neighbouring regions overlap by ~{ov:.0f}px, so no margin is needed between them")
    spotted = spotted.buffer(-a.edge)
    print(f"  spotted union eroded {a.edge:.0f}px at its outer boundary only")

    # ---- sheets ------------------------------------------------------------------------------------
    gj = json.load(open(a.sheet_index))
    rows = []
    for ft in gj["features"]:
        pr = ft["properties"]
        try:
            geom = shape(ft["geometry"])
        except Exception:
            continue
        if geom.is_empty:
            continue
        # sheet outline in z17 px (the sheet is small enough that a per-vertex Mercator map is exact)
        def to_px(poly):
            return Polygon([lonlat_px(x, y) for x, y in poly.exterior.coords])
        try:
            polys = [to_px(p) for p in (geom.geoms if geom.geom_type == "MultiPolygon" else [geom])]
        except Exception:
            continue
        sp = unary_union([p.buffer(0) for p in polys])
        if sp.is_empty:
            continue
        hits = [rtags[int(i)] for i in rtree.query(sp)]
        hits = [t for t in hits if reg[t][0].intersects(sp)]
        if not hits:
            continue
        foot = sp.intersection(spotted)
        if foot.is_empty or foot.area <= 0:
            continue
        lat = geom.centroid.y
        mpp = m_per_px(lat)
        km2 = foot.area * mpp * mpp / 1e6
        rows.append(dict(sheet=f"{pr.get('COUNTRY','')}_{pr.get('SHEET_NO','')}",
                         county=pr.get("COUNTY", ""), country=pr.get("COUNTRY", ""),
                         sur=pr.get("SUR_STA", ""), lat=round(lat, 4), lon=round(geom.centroid.x, 4),
                         cover_km2=round(km2, 2),
                         cover_frac=round(foot.area / sp.area, 3),
                         regions=hits, foot=foot, mpp=mpp))
    rows.sort(key=lambda r: -r["cover_km2"])
    print(f"{len(rows)} sheets touched by a spotted region; "
          f"{sum(1 for r in rows if r['cover_km2'] >= a.min_cover_km2)} with >= {a.min_cover_km2} km2 measurable")

    P = load_pins(a.pins)

    def pins_in(foot, mpp):
        x0, y0, x1, y1 = foot.bounds
        idx = pins_in_box(P, x0, y0, x1, y1)
        pf = prep(foot)
        keep = [k for k in idx if pf.contains(Point(float(P["gx"][k]), float(P["gy"][k])))]
        return keep

    elig = [r for r in rows if r["cover_km2"] >= a.min_cover_km2]
    for r in elig:
        r["pins_all"] = len(pins_in(r["foot"], r["mpp"]))
        r["pins_per_km2"] = round(r["pins_all"] / max(1e-6, r["cover_km2"]), 1)

    if a.plan:
        print(f"\n{'sheet':16} {'county':22} {'km2':>7} {'%sheet':>7} {'pins':>7} {'pins/km2':>9}  regions")
        for r in elig[:60]:
            print(f"{r['sheet']:16} {r['county'][:22]:22} {r['cover_km2']:7.2f} {r['cover_frac']*100:6.0f}% "
                  f"{r['pins_all']:7d} {r['pins_per_km2']:9.1f}  {len(r['regions'])}")
        return

    # Sample ACROSS pin density, not down the coverage ranking: the best-covered sheets are all Shetland and
    # Orkney, because the spotting array works through the centres file from the north, and a figure taken
    # there would describe empty moorland rather than the series. Sheets are ranked by pin density and drawn
    # evenly through that ranking, with a per-county cap so one well-covered county cannot supply the sample.
    elig.sort(key=lambda r: r["pins_per_km2"])
    n = min(a.sheets, len(elig))
    used, seen_s, sel = defaultdict(int), set(), []
    for i in range(n):
        target = int(round(i * (len(elig) - 1) / max(1, n - 1)))
        for off in range(len(elig)):                    # search outward from the target rank
            for j in ((target + off), (target - off)):
                if not (0 <= j < len(elig)):
                    continue
                r = elig[j]
                if r["sheet"] in seen_s or used[r["county"]] >= a.max_per_county:
                    continue
                used[r["county"]] += 1
                seen_s.add(r["sheet"])
                sel.append(r)
                break
            else:
                continue
            break
    sel.sort(key=lambda r: r["pins_per_km2"])
    print(f"\nassessing {len(sel)} sheets, sampled evenly across pin density "
          f"({sel[0]['pins_per_km2']}..{sel[-1]['pins_per_km2']} pins/km2)\n")

    det_cache = {}

    def load_dets(tag):
        if tag not in det_cache:
            out = []
            for line in open(reg[tag][1]):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                p = r.get("gpoly") or r.get("line_gpoly")
                if not p:
                    continue
                if r.get("score", 1.0) < a.min_score:
                    continue
                cx = sum(q[0] for q in p) / len(p)
                cy = sum(q[1] for q in p) / len(p)
                out.append((cx, cy, str(r.get("text", "")), float(r.get("score", 1.0))))
            det_cache[tag] = out
        return det_cache[tag]

    results = []
    for r in sel:
        foot, pf = r["foot"], prep(r["foot"])
        # Regions overlap, so the same label can appear in two files; one detection is one piece of ink.
        dd = {}
        for tag in r["regions"]:
            for cx, cy, txt, sc in load_dets(tag):
                if not pf.contains(Point(cx, cy)):
                    continue
                dd[(round(cx / 8), round(cy / 8), norm(txt))] = (cx, cy, txt, sc)
        dets = list(dd.values())
        pidx = pins_in(foot, r["mpp"])

        det_alpha = [d for d in dets if is_alpha(d[2])]
        pin_alpha = [k for k in pidx if is_alpha(str(P["text"][k]))]
        if not pin_alpha or not det_alpha:
            print(f"{r['sheet']}: too little to measure ({len(pin_alpha)} pins, {len(det_alpha)} dets)")
            continue

        DP = np.array([[d[0], d[1]] for d in det_alpha])
        PP = np.array([[float(P["gx"][k]), float(P["gy"][k])] for k in pin_alpha])
        dtree = STRtree([Point(x, y) for x, y in DP])
        ptree = STRtree([Point(x, y) for x, y in PP])

        per_radius = {}
        for rad in a.radii:
            pin_hit = np.zeros(len(PP), bool)
            det_hit = np.zeros(len(DP), bool)
            exact = token = 0
            for i, (px, py) in enumerate(PP):
                cand = [int(c) for c in dtree.query(Point(px, py).buffer(rad))]
                cand = [c for c in cand if math.hypot(DP[c][0] - px, DP[c][1] - py) <= rad]
                if not cand:
                    continue
                pin_hit[i] = True
                for c in cand:
                    det_hit[c] = True
                truth = str(P["text"][pin_alpha[i]])
                nt, tk = norm(truth), tokens(truth)
                cand.sort(key=lambda c: math.hypot(DP[c][0] - px, DP[c][1] - py))
                if norm(det_alpha[cand[0]][2]) == nt:
                    exact += 1
                elif any(norm(det_alpha[c][2]) in tk for c in cand):
                    token += 1
            # A word spotter boxes each word separately while GB1900 pins the label once, so before calling a
            # detection unpinned, check whether it reads a WORD of a pin's text a little further off.
            wr = rad * a.word_radius_mult
            det_word = det_hit.copy()
            for j in np.where(~det_hit)[0]:
                dx, dy = DP[j]
                nd = norm(det_alpha[j][2])
                if not nd:
                    continue
                for c in ptree.query(Point(dx, dy).buffer(wr)):
                    k = pin_alpha[int(c)]
                    if math.hypot(PP[int(c)][0] - dx, PP[int(c)][1] - dy) <= wr and nd in tokens(str(P["text"][k])):
                        det_word[j] = True
                        break
            per_radius[rad] = dict(
                pins=len(PP), dets=len(DP),
                gbstamp_misses=round(float((~pin_hit).mean()), 3),
                gb1900_misses_strict=round(float((~det_hit).mean()), 3),
                gb1900_misses_wordadj=round(float((~det_word).mean()), 3),
                read_exact=round(exact / max(1, int(pin_hit.sum())), 3),
                read_exact_or_token=round((exact + token) / max(1, int(pin_hit.sum())), 3),
            )
        # The misses themselves, at the headline radius. These are the labels a volunteer read and the
        # spotter did not find — the candidates for recovery by prompting a detector at the pin.
        if a.dump_misses:
            rad = a.radius
            with open(a.dump_misses, "a") as fh:
                for i, (px, py) in enumerate(PP):
                    cand = [int(c) for c in dtree.query(Point(px, py).buffer(rad))]
                    if any(math.hypot(DP[c][0] - px, DP[c][1] - py) <= rad for c in cand):
                        continue
                    k = pin_alpha[i]
                    txt = str(P["text"][k])
                    # distance to the nearest detection of any kind: a miss sitting 60px from a box is a
                    # near-miss of the matching rule, one sitting 800px away is genuinely undetected ink.
                    allc = [int(c) for c in dtree.query(Point(px, py).buffer(1200))]
                    nd = min([math.hypot(DP[c][0] - px, DP[c][1] - py) for c in allc], default=None)
                    fh.write(json.dumps(dict(
                        sheet=r["sheet"], county=r["county"], pin_id=str(P["pin_id"][k]),
                        text=txt, gx=float(px), gy=float(py),
                        words=len(tokens(txt)), chars=len(norm(txt)),
                        nearest_det_px=round(nd, 1) if nd is not None else None), ensure_ascii=False) + "\n")
        m = per_radius[a.radius]
        res = dict(sheet=r["sheet"], county=r["county"], country=r["country"], survey=r["sur"],
                   cover_km2=r["cover_km2"], cover_frac=r["cover_frac"], regions=len(r["regions"]),
                   pins_all=len(pidx), pins_nonnumeric=len(PP),
                   dets_all=len(dets), dets_nonnumeric=len(DP),
                   pin_numeric_frac=round(1 - len(PP) / max(1, len(pidx)), 3),
                   det_numeric_frac=round(1 - len(DP) / max(1, len(dets)), 3),
                   by_radius={str(k): v for k, v in per_radius.items()})
        results.append(res)
        print(f"{r['sheet']:14} {r['county'][:18]:18} {r['cover_km2']:6.1f}km2 "
              f"{len(PP):5d} pins {len(DP):5d} dets | "
              f"GB-STAMP misses {m['gbstamp_misses']:.3f} | "
              f"GB1900 misses {m['gb1900_misses_strict']:.3f} strict / {m['gb1900_misses_wordadj']:.3f} adj | "
              f"reads {m['read_exact']:.3f}", flush=True)

    if results:
        print(f"\n=== over {len(results)} sheets, matching radius {a.radius}px ===")
        for key, lbl in (("gbstamp_misses", "GB-STAMP misses (pinned label not detected)"),
                         ("gb1900_misses_strict", "GB1900 misses, strict (detection with no pin)"),
                         ("gb1900_misses_wordadj", "GB1900 misses, word-adjusted"),
                         ("read_exact", "reading agrees exactly, on matched labels")):
            v = np.array([x["by_radius"][str(a.radius)][key] for x in results])
            print(f"  {lbl:48} median {np.median(v):.3f}  range {v.min():.3f}-{v.max():.3f}")
        # Pooled: sheet-level medians hide how much of the total each sheet carries.
        tp = sum(x["by_radius"][str(a.radius)]["pins"] for x in results)
        td = sum(x["by_radius"][str(a.radius)]["dets"] for x in results)
        mp = sum(x["by_radius"][str(a.radius)]["pins"] * x["by_radius"][str(a.radius)]["gbstamp_misses"]
                 for x in results)
        md = sum(x["by_radius"][str(a.radius)]["dets"] * x["by_radius"][str(a.radius)]["gb1900_misses_wordadj"]
                 for x in results)
        print(f"  pooled over {tp} non-numeric pins: GB-STAMP misses {mp/max(1,tp):.3f}")
        print(f"  pooled over {td} non-numeric detections: GB1900 misses {md/max(1,td):.3f} (word-adjusted)")
        print("  numerals dropped: "
              f"{np.mean([x['pin_numeric_frac'] for x in results]):.1%} of pins, "
              f"{np.mean([x['det_numeric_frac'] for x in results]):.1%} of detections")
        for rad in a.radii:
            v = np.array([x["by_radius"][str(rad)]["gbstamp_misses"] for x in results])
            w = np.array([x["by_radius"][str(rad)]["gb1900_misses_wordadj"] for x in results])
            print(f"  radius {rad:5.0f}px: GB-STAMP misses {np.median(v):.3f}, GB1900 misses {np.median(w):.3f}")

    json.dump(dict(params=vars(a), sheets=results), open(a.out, "w"), indent=1, default=str)
    print(f"wrote {a.out}\nBENCHSHEETSDONE", flush=True)


if __name__ == "__main__":
    main()
