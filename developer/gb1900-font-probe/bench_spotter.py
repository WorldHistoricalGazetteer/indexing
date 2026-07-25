"""Score ANY text spotter against GB1900 on our own sheets — no Rumsey ground truth needed.

GB1900 gives ~2.55M (transcript, location) pairs over exactly the map series we care about. That is not a
detection ground truth (no boxes, and the pins sit at a label's START rather than on it), but it is enough to
answer the two questions that decide which spotter to use:

  RECALL-ON-PINNED   what fraction of labels a human transcribed does this spotter detect?
  RECOGNITION        of those, how often does it read the same string the human read?

Both are computed on OS six-inch sheets rather than on Rumsey, so they measure the thing we actually need
rather than a proxy. The same match also yields verified (box, text) pairs for fine-tuning, and — usefully —
a QC signal the pixel-based attempts could not provide: a detection whose reading matches a nearby transcript
is a real label, which is precisely what a crop of blank paper or building hatching can never be.

THREE THINGS THIS CANNOT MEASURE, stated so no one quotes it as if it could:
  * Absolute recall. GB1900 volunteers skipped things (numerals entirely), so text the spotter finds and
    GB1900 lacks is NOT a false positive — it is reported separately and never subtracted.
  * Anything about a detector with no recogniser. Hi-SAM's AMG emits no text, so it scores on recall only.
  * ANYTHING AT ALL about the pin-prompted detector. Its `text` field IS the GB1900 transcript, copied in at
    detection time, and it emits exactly one detection per pin — so it scores 1.000 on both axes by
    construction and the row is pure circularity. `--allow-circular` exists only to make that visible.
  * A fine-tuned model's own quality, if it was trained on these matches. Hold sheets out.

    python bench_spotter.py --det boxes_sheet_ENG_218_NW.jsonl --bbox W S E N --name mapreader
"""
import argparse, glob, json, math, os, re, sys, numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_pin_index import load_pins, pins_in_box

N17 = 2 ** 17


def norm(s):
    """Fold the differences that are transcription convention rather than reading error."""
    s = (s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def tokens(s):
    return [t for t in (norm(w) for w in re.split(r"[\s\-]+", s or "")) if t]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--det", required=True, help="detections jsonl with gpoly (+ text if it recognises)")
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--name", default=None)
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--radius", type=float, default=48.0,
                    help="px slack around a detection when matching a pin; GB1900 pins sit at the label's "
                         "start and often just off the ink, so exact containment under-counts")
    ap.add_argument("--out", default=None)
    ap.add_argument("--allow-circular", action="store_true",
                    help="score a file whose text came FROM GB1900 (pin-prompted output). Refused by default")
    a = ap.parse_args()
    name = a.name or os.path.basename(a.det)
    if "pin_id" in open(a.det).readline() and not a.allow_circular:
        raise SystemExit(f"{a.det} carries pin_id: its text IS the GB1900 transcript, so scoring it against "
                         f"GB1900 measures nothing. Use --allow-circular only to demonstrate that.")

    from shapely.geometry import Polygon, Point
    from shapely.strtree import STRtree

    def lat_px(lat):
        return (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256

    w, s, e, n = a.bbox
    x0 = (w + 180.0) / 360.0 * N17 * 256
    x1 = (e + 180.0) / 360.0 * N17 * 256
    y0, y1 = lat_px(n), lat_px(s)

    dets = []
    outside = 0
    for line in open(a.det):
        try:
            r = json.loads(line)
        except Exception:
            continue
        p = r.get("gpoly") or r.get("line_gpoly")
        if not p:
            continue
        # Clip to the sheet. MapReader only takes a SQUARE region, so its file covers a superset of the sheet;
        # counting those extras as "text with no GB1900 pin" would inflate that figure by ~2x.
        cx = sum(q[0] for q in p) / len(p)
        cy = sum(q[1] for q in p) / len(p)
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            outside += 1
            continue
        try:
            g = Polygon(p).buffer(0)
        except Exception:
            continue
        if g.is_empty or g.area <= 0:
            continue
        dets.append((g, str(r.get("text", "")), r))
    if not dets:
        raise SystemExit(f"no usable detections in {a.det}")
    has_text = sum(1 for _, t, _ in dets if t.strip()) / len(dets)
    if outside:
        print(f"  ({outside} detections outside the sheet bbox, dropped)", flush=True)
    P = load_pins(a.pins)
    idx = pins_in_box(P, x0, y0, x1, y1)
    print(f"{name}: {len(dets)} detections ({has_text:.0%} carry text), {len(idx)} GB1900 pins in bbox",
          flush=True)

    tree = STRtree([g for g, _, _ in dets])
    covered = 0
    exact_nearest = exact_any = token_any = 0
    matched_pairs = []
    hit_det = set()
    for k in idx:
        pt = Point(float(P["gx"][k]), float(P["gy"][k]))
        cand = tree.query(pt.buffer(a.radius))
        cand = [int(c) for c in cand]
        near = [(dets[c][0].distance(pt), c) for c in cand if dets[c][0].distance(pt) <= a.radius]
        if not near:
            continue
        covered += 1
        near.sort()
        for _, c in near:
            hit_det.add(c)
        truth = str(P["text"][k])
        nt = norm(truth)
        toks = set(tokens(truth))
        # strict: only the CLOSEST detection may answer — no credit for a lucky neighbour in dense text
        if norm(dets[near[0][1]][1]) == nt:
            exact_nearest += 1
        got_exact = any(norm(dets[c][1]) == nt for _, c in near)
        # a word spotter boxes ONE word of a multi-word label, so token membership is the fair test for it
        got_token = any(norm(dets[c][1]) in toks for _, c in near if norm(dets[c][1]))
        exact_any += got_exact
        token_any += got_token
        if got_exact or got_token:
            c = near[0][1]
            matched_pairs.append(dict(pin_id=str(P["pin_id"][k]), text=truth,
                                      det_text=dets[c][1], gpoly=dets[c][2].get("gpoly")))

    unmatched = len(dets) - len(hit_det)
    res = dict(
        name=name, detections=len(dets), pins=len(idx), det_with_text=round(has_text, 3),
        recall_on_pinned=round(covered / max(1, len(idx)), 3),
        recog_exact_nearest=round(exact_nearest / max(1, covered), 3) if has_text else None,
        recog_exact_any=round(exact_any / max(1, covered), 3) if has_text else None,
        recog_token_any=round(token_any / max(1, covered), 3) if has_text else None,
        verified_pairs=len(matched_pairs),
        detections_with_no_pin=unmatched,
        detections_with_no_pin_frac=round(unmatched / len(dets), 3),
    )
    print(f"  recall on pinned labels   {res['recall_on_pinned']:.3f}  ({covered}/{len(idx)})", flush=True)
    if has_text:
        print(f"  recognition, nearest only {res['recog_exact_nearest']:.3f}", flush=True)
        print(f"  recognition, any in range {res['recog_exact_any']:.3f}", flush=True)
        print(f"  token match (word spotter){res['recog_token_any']:.3f}", flush=True)
        print(f"  VERIFIED (box,text) pairs {len(matched_pairs)}", flush=True)
    else:
        print("  (no recogniser — recall only)", flush=True)
    print(f"  detections with no GB1900 pin: {unmatched} ({res['detections_with_no_pin_frac']:.0%}) "
          f"— text GB1900 did not pin, NOT false positives", flush=True)

    out = a.out or a.det.replace(".jsonl", "") + f".bench_{re.sub(r'[^a-z0-9]+','',name.lower())}.json"
    json.dump(dict(metrics=res, pairs=matched_pairs[:5000]), open(out, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {out}\nBENCHDONE", flush=True)


if __name__ == "__main__":
    main()
