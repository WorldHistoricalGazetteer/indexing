"""Characterise pin-prompted Hi-SAM detection BEFORE trusting it at scale.

Three questions, in the order that matters:

  1. AGREEMENT — on labels both instruments see, does pin-prompted Hi-SAM measure the same object MapReader
     measured? If the boxes disagree, the font descriptors extracted from them are not a consistent quantity
     across the corpus and nothing downstream is comparable.
  2. RECOVERY — which pins does MapReader miss entirely? That set is the whole reason for the switch, so
     characterise it (ALLCAPS share, token count, sample texts), don't just count it.
  3. FAILURE — where does the new instrument break: prompts landing off ink, line masks truncated at the
     window edge, pins with no ink at all.

Writes a metrics JSON plus overlays (MapReader blue, Hi-SAM word red, Hi-SAM line orange, GB1900 pin green)
for human inspection, because a number without a picture has not been validated.

    python validate_pins.py --tag gb_4338_2896
"""
import argparse, json, os, glob, numpy as np

SPOT = "/vast/ishi/gb1900/edition/spot"
PINS = "/vast/ishi/gb1900/edition/pins"

try:
    from shapely.geometry import Polygon
    HAVE_SHAPELY = True
except ImportError:                                            # bbox IoU is a fair proxy for near-horizontal
    HAVE_SHAPELY = False                                       # labels; only curved river names really need it


def bbox(poly):
    p = np.asarray(poly, float)
    return p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()


def iou(pa, pb):
    if HAVE_SHAPELY:
        a, b = Polygon(pa).buffer(0), Polygon(pb).buffer(0)
        u = a.union(b).area
        return a.intersection(b).area / u if u > 0 else 0.0
    ax0, ay0, ax1, ay1 = bbox(pa)
    bx0, by0, bx1, by1 = bbox(pb)
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    u = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / u if u > 0 else 0.0


def coverage(poly, others):
    """Fraction of `poly`'s area covered by the union of `others` (0 when there is nothing to compare)."""
    if not others:
        return 0.0
    if HAVE_SHAPELY:
        from shapely.ops import unary_union
        a = Polygon(poly).buffer(0)
        u = unary_union([Polygon(o["gpoly"]).buffer(0) for o in others])
        return a.intersection(u).area / a.area if a.area > 0 else 0.0
    x0, y0, x1, y1 = bbox(poly)
    area = max(1e-9, (x1 - x0) * (y1 - y0))
    grid = np.zeros((64, 64), bool)                            # rasterised union — no shapely, no double counting
    for o in others:
        bx0, by0, bx1, by1 = bbox(o["gpoly"])
        gx0 = int(max(0, (bx0 - x0) / (x1 - x0 + 1e-9) * 64)); gx1 = int(min(64, np.ceil((bx1 - x0) / (x1 - x0 + 1e-9) * 64)))
        gy0 = int(max(0, (by0 - y0) / (y1 - y0 + 1e-9) * 64)); gy1 = int(min(64, np.ceil((by1 - y0) / (y1 - y0 + 1e-9) * 64)))
        grid[gy0:gy1, gx0:gx1] = True
    return float(grid.mean())


def norm_text(s):
    return "".join(c for c in s.lower() if c.isalnum())


def contains(poly, x, y):
    if HAVE_SHAPELY:
        from shapely.geometry import Point
        return Polygon(poly).buffer(0).contains(Point(x, y))
    x0, y0, x1, y1 = bbox(poly)
    return x0 <= x <= x1 and y0 <= y <= y1


def load_jsonl(p):
    out = []
    if not os.path.exists(p):
        return out
    for line in open(p):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def profile(rows, key="text"):
    """Shape of a label set — the discriminating axes are case and token count, not the words themselves."""
    if not rows:
        return dict(n=0)
    txt = [r[key] for r in rows]
    caps = [t for t in txt if t.isupper() and any(c.isalpha() for c in t)]
    multi = [t for t in txt if len(t.split()) > 1]
    return dict(n=len(txt), allcaps=round(len(caps) / len(txt), 3), multitoken=round(len(multi) / len(txt), 3),
                median_chars=int(np.median([len(t) for t in txt])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--pins-dir", default=PINS)
    ap.add_argument("--spot-dir", default=SPOT)
    ap.add_argument("--overlays", type=int, default=6, help="how many 1024px overlay crops to render")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    det = load_jsonl(f"{a.pins_dir}/pins_{a.tag}.jsonl")
    mr = [r for r in load_jsonl(f"{a.spot_dir}/boxes_{a.tag}.jsonl") if r.get("gpoly")]
    print(f"{a.tag}: {len(det)} pin-prompted detections, {len(mr)} MapReader boxes", flush=True)
    if not det:
        raise SystemExit("no detections to validate")

    # --- index MapReader boxes on a coarse grid so the match loop stays linear ---
    grid = {}
    for m in mr:
        x0, y0, x1, y1 = bbox(m["gpoly"])
        m["_bb"] = (x0, y0, x1, y1)
        for gx in range(int(x0) // 512, int(x1) // 512 + 1):
            for gy in range(int(y0) // 512, int(y1) // 512 + 1):
                grid.setdefault((gx, gy), []).append(m)

    matched, recovered = [], []
    for d in det:
        wp = d.get("gpoly") or d.get("line_gpoly")
        x0, y0, x1, y1 = bbox(wp)
        cand = []
        for gx in range(int(x0) // 512, int(x1) // 512 + 1):
            for gy in range(int(y0) // 512, int(y1) // 512 + 1):
                cand += grid.get((gx, gy), [])
        best, best_i, overlapping = None, 0.0, []
        for m in {id(c): c for c in cand}.values():
            # A MapReader box counts as the same label if it overlaps the detection OR simply covers the pin —
            # the second clause matters because MapReader boxes a spaced label's fragments, not its extent.
            v = iou(wp, m["gpoly"])
            if v > 0:
                overlapping.append(m)
            if v == 0 and contains(m["gpoly"], d["pin_gx"], d["pin_gy"]):
                v = 1e-6
            if v > best_i:
                best, best_i = m, v
        if best is None:
            recovered.append(d)
        else:
            d["_iou"] = best_i
            d["_mr_text"] = best["text"]
            # A GB1900 entry is usually a whole LABEL ("ST. ALDATE'S STREET") while MapReader boxes single
            # WORDS, so best-single-box IoU is capped near 1/k for a k-word label and measures tokenisation,
            # not agreement. Coverage against the UNION of overlapping MapReader boxes is the like-for-like
            # question: does the detection span the same ink?
            d["_cover"] = coverage(wp, overlapping)
            d["_nmr"] = len(overlapping)
            matched.append(d)

    ious = np.array([d["_iou"] for d in matched]) if matched else np.zeros(0)
    cov = np.array([d["_cover"] for d in matched]) if matched else np.zeros(0)
    single = [d for d in matched if len(d["text"].split()) == 1]      # only these are token-for-token comparable
    sious = np.array([d["_iou"] for d in single]) if single else np.zeros(0)
    agree = [d for d in matched if norm_text(d["text"]) == norm_text(d["_mr_text"])]
    lw = np.array([d["line_area"] / max(1, d["word_area"]) for d in det if d.get("word_area")])

    # --- is the LINE mask the right crop unit? ---
    # A GB1900 entry is a whole label, so the word mask under-covers the 70% that are multi-token and the line
    # mask is the natural crop. The risk is over-merge: a line mask that swallows the NEIGHBOURING label would
    # put two typefaces in one crop and poison the descriptor. Testing that with other GB1900 PINS rather than
    # MapReader's text avoids leaning on its OCR, which agrees with the transcript only ~27% of the time here.
    pin_xy = np.array([[d["pin_gx"], d["pin_gy"]] for d in det])
    for d in det:
        for fld, out in (("gpoly", "_word_extra"), ("line_gpoly", "_line_extra")):
            poly = d.get(fld)
            if not poly:
                d[out] = None
                continue
            x0, y0, x1, y1 = bbox(poly)
            near = np.where((pin_xy[:, 0] >= x0) & (pin_xy[:, 0] <= x1) &
                            (pin_xy[:, 1] >= y0) & (pin_xy[:, 1] <= y1))[0]
            d[out] = sum(1 for i in near
                         if not (pin_xy[i][0] == d["pin_gx"] and pin_xy[i][1] == d["pin_gy"])
                         and contains(poly, pin_xy[i][0], pin_xy[i][1]))
    we = [d["_word_extra"] for d in det if d["_word_extra"] is not None]
    le = [d["_line_extra"] for d in det if d["_line_extra"] is not None]
    multi = [d for d in det if len(d["text"].split()) > 1 and d["_line_extra"] is not None]

    caps_multi = [d for d in det if d["text"].isupper() and len(d["text"].split()) > 1]
    metrics = dict(
        tag=a.tag, detections=len(det), mapreader_boxes=len(mr), shapely=HAVE_SHAPELY,
        on_ink_rate=round(float(np.mean([d["on_ink"] for d in det])), 3),
        truncated_rate=round(float(np.mean([d["truncated"] for d in det])), 3),
        snapped_rate=round(float(np.mean([d.get("snapped", False) for d in det])), 3),
        matched=len(matched), recovered=len(recovered),
        iou_median=round(float(np.median(ious)), 3) if len(ious) else None,
        iou_p25=round(float(np.percentile(ious, 25)), 3) if len(ious) else None,
        iou_ge_05=round(float(np.mean(ious >= 0.5)), 3) if len(ious) else None,
        # like-for-like: single-token labels only, where both instruments box the same one word
        singletoken_n=len(single),
        singletoken_iou_median=round(float(np.median(sious)), 3) if len(sious) else None,
        singletoken_iou_ge_05=round(float(np.mean(sious >= 0.5)), 3) if len(sious) else None,
        # does the detection span the same ink as MapReader's word boxes for this label?
        cover_median=round(float(np.median(cov)), 3) if len(cov) else None,
        cover_ge_08=round(float(np.mean(cov >= 0.8)), 3) if len(cov) else None,
        text_agreement=round(len(agree) / len(matched), 3) if matched else None,
        line_over_word_area_median=round(float(np.median(lw)), 2) if len(lw) else None,
        # over-merge: how often a mask swallows a NEIGHBOURING label's pin (lower is better; word is the floor)
        word_swallows_other_pin=round(float(np.mean(np.asarray(we) > 0)), 3) if we else None,
        line_swallows_other_pin=round(float(np.mean(np.asarray(le) > 0)), 3) if le else None,
        line_swallows_other_pin_multitoken=round(
            float(np.mean([d["_line_extra"] > 0 for d in multi])), 3) if multi else None,
        # If the line level is doing its job, spaced ALLCAPS labels should show a much larger line/word ratio
        # than ordinary single-word labels — that is the mechanism, stated as a testable number.
        line_over_word_caps_multitoken=round(float(np.median(
            [d["line_area"] / max(1, d["word_area"]) for d in caps_multi])), 2) if caps_multi else None,
        profile_matched=profile(matched), profile_recovered=profile(recovered),
        recovered_sample=[d["text"] for d in recovered[:60]],
    )
    out = a.out or f"{a.pins_dir}/validate_{a.tag}.json"
    json.dump(metrics, open(out, "w"), indent=2, ensure_ascii=False)
    print(json.dumps(metrics, indent=2, ensure_ascii=False), flush=True)

    if a.overlays:
        render_overlays(a.tag, det, mr, recovered, a.overlays, a.pins_dir)
    print("VALIDATEDONE", flush=True)


def render_overlays(tag, det, mr, recovered, n, outdir):
    """Crops centred on RECOVERED detections — the interesting cases are where the instruments disagree."""
    import cv2
    from hisam_pins import window_image
    picks = recovered[:: max(1, len(recovered) // n)][:n] if recovered else det[:: max(1, len(det) // n)][:n]
    for k, d in enumerate(picks):
        tx0 = int(d["pin_gx"]) // 256 - 2
        ty0 = int(d["pin_gy"]) // 256 - 2
        img, hit = window_image(tx0, ty0, 4)
        if hit == 0:
            continue
        ox, oy = tx0 * 256, ty0 * 256
        ov = (img.astype(np.float32) * 0.55 + 255 * 0.45).astype(np.uint8)
        W = img.shape[0]

        def draw(poly, colour, th=2):
            p = (np.asarray(poly, float) - [ox, oy]).astype(np.int32)
            if p[:, 0].max() < 0 or p[:, 1].max() < 0 or p[:, 0].min() > W or p[:, 1].min() > W:
                return
            cv2.polylines(ov, [p], True, colour, th)

        for m in mr:
            draw(m["gpoly"], (30, 80, 220))
        for e in det:
            if abs(e["pin_gx"] - d["pin_gx"]) > W or abs(e["pin_gy"] - d["pin_gy"]) > W:
                continue
            if e.get("line_gpoly"):
                draw(e["line_gpoly"], (250, 150, 20), 1)
            if e.get("gpoly"):
                draw(e["gpoly"], (220, 40, 40))
            cv2.circle(ov, (int(e["pin_gx"] - ox), int(e["pin_gy"] - oy)), 4, (20, 170, 60), -1)
        cv2.imwrite(f"{outdir}/overlay_{tag}_{k}.png", cv2.cvtColor(ov, cv2.COLOR_RGB2BGR))
    print(f"wrote {len(picks)} overlays -> {outdir}/overlay_{tag}_*.png", flush=True)


if __name__ == "__main__":
    main()
