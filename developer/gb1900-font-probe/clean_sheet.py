"""Build a de-noised and/or masked copy of a sheet's z17 tiles, for a second spotter pass.

Two INDEPENDENT interventions, written so they can be measured separately — the point of the experiment is to
learn which one helps, and running them only in combination would answer neither:

  MASK   keep only the union of detector boxes (buffered), whiten the rest. Hi-SAM's automatic boxes crop
         tight and MapReader's are loose, so they get different buffers. This hands the second pass clean
         paper with candidate text on it and nothing else.
  CLEAN  OpenCV removal of the map's own linework: long continuous strokes (roads, contours, boundaries,
         railways) and the solid/hatched blocks that represent buildings. Independent of any detector.

A bound worth stating plainly: masking can only ever recover text that at least ONE detector already boxed.
Text both detectors missed is whitened out, so a masked pass CANNOT find it. What masking tests is whether
clutter was suppressing MapReader's reading of text Hi-SAM did find — not whether it can find the unfound.

Writes tiles to a parallel cache so the spotter can be pointed at it unchanged (SPOT_TILES).

    python clean_sheet.py --tag sheet_ENG_218_NW --bbox W S E N --mask --clean --out-tiles /vast/.../tiles_x
"""
import argparse, glob, json, math, os, sys
import numpy as np, cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# See sheet_clean: hisam_pins drags in the Hi-SAM repo for a tile reader this module can get locally.
from make_font_testset_v2 import _get_tile
N17 = 1 << 17


def read_tile(tx, ty, fetch=False):
    import numpy as _np
    t = _get_tile(tx, ty)
    return None if t is None else _np.repeat(t[:, :, None], 3, 2)


def lat_px(lat):
    return (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256


def load_boxes(path, buffer_px):
    """Detector boxes as buffered global-px polygons."""
    out = []
    if not path or not os.path.exists(path):
        return out
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        p = r.get("gpoly") or r.get("line_gpoly")
        if not p or len(p) < 3:
            continue
        a = np.asarray(p, np.float64)
        c = a.mean(0)
        v = a - c
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        a = c + v * (1.0 + buffer_px / np.maximum(norms, 1e-6))   # push each vertex out by buffer_px
        out.append(a)
    return out


def clean_tile(gray, max_line=180, solid_min=260, fill_ratio=0.52, speck=3, protect=None):
    """Remove long linework and solid/hatched building blocks; keep glyph-sized marks.

    Discrimination is by connected-component SHAPE, not by intensity: map linework forms components that are
    either very long (a road or contour runs off the tile) or compact and densely filled (a building block),
    while a glyph is small and sparse. Deliberately conservative — erasing a letter costs more than leaving a
    line, since the whole point is to help the spotter, not to produce a pretty image.
    """
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(bw, 8)
    drop = np.zeros(n, bool)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        diag = math.hypot(w, h)
        if area <= speck:
            drop[i] = True                                     # dust
        elif diag > max_line and area / max(1.0, w * h) < 0.35:
            drop[i] = True                                     # long thin run = road / contour / boundary
        elif area > solid_min and (area / max(1.0, w * h)) > fill_ratio and min(w, h) > 10:
            drop[i] = True                                     # dense block = building fill / hatching
    if protect is not None and drop.any():
        # Never erase ink a detector called text. Measured necessity, not caution: without this, letter-spaced
        # street names merge with the road casings they sit between into one long thin component, and the
        # "long thin = linework" rule deletes the very labels the pass is meant to read (recall 0.92 -> 0.87,
        # token recognition 0.78 -> 0.63), while the building hatching it was aimed at survives.
        keep = np.unique(lab[protect > 0])
        drop[keep[keep < len(drop)]] = False
    if drop.any():
        bw[np.isin(lab, np.where(drop)[0])] = 0
    out = np.full_like(gray, 255)
    out[bw > 0] = gray[bw > 0]                                 # keep original greys of surviving ink
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--out-tiles", required=True)
    ap.add_argument("--mask", action="store_true")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--mr-file", default=None)
    ap.add_argument("--amg-file", default=None)
    ap.add_argument("--mr-buffer", type=float, default=4.0, help="MapReader boxes are already loose")
    ap.add_argument("--amg-buffer", type=float, default=10.0, help="AMG boxes crop tight to the ink")
    ap.add_argument("--pad", type=int, default=2, help="extra tiles around the sheet, for mosaic overrun")
    ap.add_argument("--protect", action="store_true",
                    help="with --clean, never erase ink that falls inside a detector box")
    a = ap.parse_args()

    w, s, e, n = a.bbox
    tx0 = int(((w + 180.0) / 360.0 * N17 * 256) // 256) - a.pad
    tx1 = int(((e + 180.0) / 360.0 * N17 * 256) // 256) + a.pad
    ty0, ty1 = int(lat_px(n) // 256) - a.pad, int(lat_px(s) // 256) + a.pad

    polys = []
    if a.mask or a.protect:
        polys = load_boxes(a.mr_file, a.mr_buffer) + load_boxes(a.amg_file, a.amg_buffer)
        if not polys:
            raise SystemExit("--mask/--protect need at least one of --mr-file/--amg-file with boxes")
        print(f"{a.tag}: {len(polys)} buffered boxes "
              f"({'mask' if a.mask else ''}{'+protect' if a.protect else ''})", flush=True)

    # Bucket polygons by tile so each tile only rasterises what touches it.
    by_tile = {}
    for p in polys:
        x0, y0 = p[:, 0].min(), p[:, 1].min()
        x1, y1 = p[:, 0].max(), p[:, 1].max()
        for tx in range(int(x0) // 256, int(x1) // 256 + 1):
            for ty in range(int(y0) // 256, int(y1) // 256 + 1):
                by_tile.setdefault((tx, ty), []).append(p)

    nt = kept = 0
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = read_tile(tx, ty, fetch=False)
            if t is None:
                continue
            nt += 1
            g = cv2.cvtColor(t, cv2.COLOR_RGB2GRAY)
            m = None
            if polys:
                m = np.zeros(g.shape, np.uint8)
                for p in by_tile.get((tx, ty), []):
                    q = (p - [tx * 256, ty * 256]).astype(np.int32)
                    cv2.fillPoly(m, [q], 255)
            if a.clean:
                g = clean_tile(g, protect=(m if a.protect else None))
            if a.mask:
                if m is not None and m.any():
                    kept += 1
                g = np.where(m > 0, g, 255).astype(np.uint8)
            d = f"{a.out_tiles}/{tx}"
            os.makedirs(d, exist_ok=True)
            cv2.imwrite(f"{d}/{ty}.png", cv2.cvtColor(np.repeat(g[:, :, None], 3, 2), cv2.COLOR_RGB2BGR))
    print(f"{a.tag}: wrote {nt} tiles -> {a.out_tiles} "
          f"({'mask ' if a.mask else ''}{'clean' if a.clean else ''}; {kept} tiles carry boxes)", flush=True)
    print("CLEANSHEETDONE", flush=True)


if __name__ == "__main__":
    main()
