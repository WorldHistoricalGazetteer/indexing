"""WHOLE-SHEET linework removal for OS six-inch maps, with a per-stage diagnostic overlay.

Supersedes the tile-wise cleaner in clean_sheet.py, which was wrong in principle: connected components were
computed per 256 px tile, so a road or contour crossing the sheet was chopped into fragments at every tile
boundary and each fragment fell under the length threshold. Nothing long could be detected because nothing was
allowed to BE long. Everything here runs on the stitched sheet.

Four stages, each independently switchable and each colour-coded in the diagnostic image, because the only way
to tune this is to see what it took:

  SOLID  (red)    thick filled areas — erosion survives them and kills 2-3 px letter strokes, so an eroded-
                  then-redilated mask isolates them without touching type.
  HATCH  (orange) building interiors drawn as many closely-spaced parallel rules. Individually these are short
                  strokes that no length rule can catch; collectively they are dense. Dilating merges a hatched
                  block into one large high-density blob while well-spaced glyphs stay separate.
  LINE   (blue)   TRACED strokes — followed along their own heading, thin, and cut at any sharp change of
                  direction. Roads, contours, rivers and boundaries survive that test; letters cannot.
  DASH   (green)  dashed boundaries, chained by direction: from each dash, look along its own axis for the next
                  dash of similar length and orientation within a plausible gap, and require the alternation to
                  repeat. A single dash is indistinguishable from a hyphen or a full stop; a chain of six is not.

Text is protected by default when detector boxes are supplied — measured necessity, since letter-spaced street
names merge with the road casings they sit between (see NEXT-PHASE.md).

    python sheet_clean.py --tag sheet_ENG_218_NW --bbox W S E N --out-tiles DIR --diag DIR/diag.png
"""
import argparse, json, math, os, sys, time
import numpy as np, cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hisam_pins import read_tile, N17
from clean_sheet import load_boxes, lat_px


def stitch(tx0, ty0, nx, ny):
    """Stitch in COLOUR — the paper tone is warm and uneven, and flat-fielding needs the channels."""
    canvas = np.full((ny * 256, nx * 256, 3), 255, np.uint8)
    hit = 0
    for i in range(nx):
        for j in range(ny):
            t = read_tile(tx0 + i, ty0 + j, fetch=False)
            if t is not None:
                canvas[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = t
                hit += 1
    return canvas, hit


def flat_field(rgb, scale=8, ksize=31):
    """Neutralise paper colour and brightness before anything else looks at the ink.

    Sheets are warm-toned, unevenly lit and vary across a single sheet, so a global Otsu on the raw grey
    splits partly on paper tone rather than on ink — which puts noise into every stage downstream. Estimating
    the background by a median at 1/8 scale (large enough to step over any lettering, cheap enough to run on
    47 Mpx) and dividing by it per channel flattens the paper to white while leaving neutral ink dark.
    """
    h, w = rgb.shape[:2]
    small = cv2.resize(rgb, (max(1, w // scale), max(1, h // scale)), interpolation=cv2.INTER_AREA)
    bg = cv2.medianBlur(small, ksize)                      # median steps over ink, keeps the paper field
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    bg[bg == 0] = 1
    return cv2.divide(rgb, bg, scale=255)


def binarize(gray):
    """Otsu on the whole sheet. Paper tone varies slowly, ink is near-black, so a global split is stable."""
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def find_solid(bw, k=5):
    """Thick filled areas, via a morphological OPENING.

    Opening = erode then dilate with the SAME element, so a thick region keeps its true extent instead of
    being inflated. Letter strokes at six-inch are 2-3 px and vanish under a 5x5 erosion; a solid block does
    not. (An earlier version dilated back with a much larger element and swallowed half the sheet.)
    """
    return cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))


def _line_se(length, angle_deg, thick=1):
    se = np.zeros((length, length), np.uint8)
    c = length // 2
    dx, dy = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    cv2.line(se, (int(c - dx * c), int(c - dy * c)), (int(c + dx * c), int(c + dy * c)), 1, thick)
    return se


def _skeleton(mask):
    try:
        from skimage.morphology import skeletonize
        return skeletonize(mask > 0)
    except ImportError:
        thin = getattr(getattr(cv2, "ximgproc", None), "thinning", None)
        if thin is None:
            raise SystemExit("need skimage or opencv-contrib for skeletonisation")
        return thin((mask > 0).astype(np.uint8) * 255) > 0


def trace_lines(bw, max_half_width=2.6, min_length=70, step=7, max_turn=32.0, dilate_extra=1):
    """TRACE strokes: follow each one along its own heading, and cut it where it turns sharply.

    Neither a connected-component test nor an oriented opening is tracing. A component test asks "is this blob
    big", which on a map is one blob covering everything. An oriented opening asks "is there a straight run
    here", which fires on the stem of a capital and on the edge of a building, and misses a curved contour
    entirely. Tracing asks the question that actually separates map linework from type:

      DIRECTIONALITY   the stroke continues along its own heading, step after step;
      NO UNDUE BREADTH the stroke is thin — its distance transform stays under `max_half_width`, so a fat
                       letter stroke or a solid fill is excluded before tracing starts;
      NO SHARP CORNERS the heading changes only gradually. Roads, contours, rivers and boundaries curve
                       smoothly; letters are built from short strokes meeting at sharp angles, so a glyph
                       cannot survive as one traced path.

    Skeletonise, split the skeleton graph into branches at junctions and endpoints, then walk each branch in
    `step`-px chords and break it wherever the chord-to-chord turn exceeds `max_turn`. Sub-paths reaching
    `min_length` are linework; they are painted back out to the stroke's own width.
    """
    thin_ink = (bw > 0)
    dist = cv2.distanceTransform((thin_ink * 255).astype(np.uint8), cv2.DIST_L2, 5)
    skel = _skeleton(thin_ink) & (dist <= max_half_width)     # breadth gate, before any tracing
    if not skel.any():
        return np.zeros_like(bw), 0

    sk = skel.astype(np.uint8)
    # 8-neighbour degree of every skeleton pixel
    k = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.uint8)
    deg = cv2.filter2D(sk, -1, k, borderType=cv2.BORDER_CONSTANT) * sk
    H, W = sk.shape
    NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    def nbrs(y, x):
        out = []
        for dy, dx in NB:
            b, c = y + dy, x + dx
            if 0 <= b < H and 0 <= c < W and sk[b, c]:
                out.append((b, c))
        return out

    visited = np.zeros_like(sk, bool)
    branches = []
    lookback = 6

    def walk(y0, x0, first):
        """Follow the stroke from (y0,x0) into `first`, continuing THROUGH junctions along the heading.

        Stopping at junctions is what killed the first version: on a dense sheet every road casing is touched
        by buildings, so the skeleton is a mesh and each branch between junctions is far shorter than any
        sensible length threshold — nothing could ever qualify as a line. A junction is not a corner. At each
        one, carry on with whichever continuation best preserves the current heading, and stop only when even
        the best available turn is sharper than `max_turn`.
        """
        path = [(y0, x0), first]
        visited[y0, x0] = True
        visited[first] = True
        while True:
            cy, cx = path[-1]
            py, px = path[-min(len(path), lookback)]
            hy, hx = cy - py, cx - px
            if hy == 0 and hx == 0:
                hy, hx = cy - path[-2][0], cx - path[-2][1]
            head = math.degrees(math.atan2(hy, hx))
            best, best_turn = None, None
            for ny_, nx_ in nbrs(cy, cx):
                if visited[ny_, nx_]:
                    continue
                ang = math.degrees(math.atan2(ny_ - cy, nx_ - cx))
                turn = abs((ang - head + 180.0) % 360.0 - 180.0)
                if best_turn is None or turn < best_turn:
                    best, best_turn = (ny_, nx_), turn
            # Loose bound HERE on purpose: a skeleton step is quantised to 45 deg increments, so testing a
            # single pixel step against the real curvature tolerance kills every walk after two pixels. The
            # walk just follows the stroke; "no sharp changes of direction" is enforced below, on chords,
            # where the measurement is meaningful.
            if best is None or best_turn > 89.0:
                break
            visited[best] = True
            path.append(best)
        return path

    # Seed from endpoints first (a stroke's true end), then from anything left over (closed loops, meshes).
    for order in (1, None):
        if order == 1:
            ys, xs = np.nonzero((deg == 1) & (sk > 0))
        else:
            ys, xs = np.nonzero((sk > 0) & ~visited)
        for y0, x0 in zip(ys.tolist(), xs.tolist()):
            if visited[y0, x0]:
                continue
            free = [p for p in nbrs(y0, x0) if not visited[p]]
            if not free:
                visited[y0, x0] = True
                continue
            p = walk(y0, x0, free[0])
            if len(p) > 2:
                branches.append(p)

    out = np.zeros_like(bw)
    kept = 0
    for path in branches:
        if len(path) < min_length // 2:
            continue
        p = np.array(path, np.float32)                        # (n,2) as (y,x)
        # chord headings every `step` px along the path
        idx = list(range(0, len(p), step))
        if idx[-1] != len(p) - 1:
            idx.append(len(p) - 1)
        pts = p[idx]
        if len(pts) < 3:
            seg = [(0, len(p) - 1)]
        else:
            v = np.diff(pts, axis=0)
            ang = np.degrees(np.arctan2(v[:, 0], v[:, 1]))
            turn = np.abs((np.diff(ang) + 180.0) % 360.0 - 180.0)
            cuts = [0] + [i + 1 for i, t in enumerate(turn) if t > max_turn] + [len(idx) - 1]
            seg = [(idx[cuts[i]], idx[cuts[i + 1]]) for i in range(len(cuts) - 1)]
        for s0, s1 in seg:
            sub = p[s0:s1 + 1]
            if len(sub) < 2:
                continue
            length = float(np.abs(np.diff(sub, axis=0)).sum())    # 4/8-connected step count ~ path length
            if length < min_length:
                continue
            kept += 1
            for y, x in sub.astype(int):
                r = int(dist[y, x]) + dilate_extra
                cv2.circle(out, (int(x), int(y)), max(1, r), 255, -1)
    return (out & bw), kept


def find_hatch(bw, short=9, n_ang=12, win=31, min_density=0.42):
    """Hatched fill: short parallel ruling packed densely.

    Individually a hatch stroke is the size of a letter stroke, so it cannot be caught by length. What
    separates it is company: hatching fills an area at high, regular density, while type leaves white space
    between glyphs and between lines. So: ink that is part of a short straight run AND sits in a dense
    neighbourhood.
    """
    acc = np.zeros_like(bw)
    for i in range(n_ang):
        acc |= cv2.morphologyEx(bw, cv2.MORPH_OPEN, _line_se(short, 180.0 * i / n_ang))
    dens = cv2.blur((bw > 0).astype(np.float32), (win, win))
    return ((acc > 0) & (dens >= min_density)).astype(np.uint8) * 255


def find_dashes(bw, dash_len=(4, 34), min_elong=1.6, gap=(3, 40), ang_tol=22.0,
                len_ratio=2.2, min_chain=5):
    """Dashed lines, chained by direction — the alternation is the evidence, not any single mark.

    A lone dash cannot be told from a hyphen, a full stop or a tick. So candidates are only erased when they
    form a run: from each dash, step along its own axis by a plausible gap and require the next mark to share
    its orientation and roughly its length, repeatedly. Requiring `min_chain` links makes a false positive need
    a coincidence of five collinear, similarly-sized, similarly-angled marks.
    """
    n, lab, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    cands = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        L, S = max(w, h), min(w, h)
        if not (dash_len[0] <= L <= dash_len[1]) or area < 3:
            continue
        if S > 0 and L / max(1.0, S) < min_elong and L > 8:
            continue                                     # blobby and biggish -> not a dash
        ys, xs = np.nonzero(lab[y:y + h, x:x + w] == i)
        if len(xs) < 3:
            continue
        pts = np.stack([xs, ys], 1).astype(np.float32)
        mean = pts.mean(0)
        u, s, vt = np.linalg.svd(pts - mean, full_matrices=False)
        ang = math.degrees(math.atan2(vt[0][1], vt[0][0])) % 180.0
        cands.append(dict(i=i, c=np.array([cent[i][0], cent[i][1]]), ang=ang, L=float(L)))
    if not cands:
        return np.zeros_like(bw), 0

    # grid index for neighbour lookup
    cell = max(8, gap[1])
    grid = {}
    for k, d in enumerate(cands):
        grid.setdefault((int(d["c"][0] // cell), int(d["c"][1] // cell)), []).append(k)

    def neighbours(p):
        gx, gy = int(p[0] // cell), int(p[1] // cell)
        out = []
        for a in range(gx - 1, gx + 2):
            for b in range(gy - 1, gy + 2):
                out += grid.get((a, b), [])
        return out

    def angdiff(a, b):
        d = abs(a - b) % 180.0
        return min(d, 180.0 - d)

    used = set()
    chains = []
    for k, d in enumerate(cands):
        if k in used:
            continue
        chain = [k]
        for direction in (1, -1):                        # extend both ways from the seed
            cur = d
            while True:
                th = math.radians(cur["ang"])
                axis = np.array([math.cos(th), math.sin(th)]) * direction
                best, bestd = None, None
                for m in neighbours(cur["c"]):
                    if m in used or m in chain:
                        continue
                    o = cands[m]
                    v = o["c"] - cur["c"]
                    dist = float(np.linalg.norm(v))
                    if dist < 1e-6:
                        continue
                    along = float(v @ axis)
                    off = math.sqrt(max(0.0, dist * dist - along * along))
                    step = along - (cur["L"] + o["L"]) / 2.0        # centre distance minus the two half-dashes
                    if along <= 0 or not (gap[0] <= step <= gap[1]):
                        continue
                    if off > 0.35 * max(1.0, along):                # must lie near the axis, not beside it
                        continue
                    if angdiff(cur["ang"], o["ang"]) > ang_tol:
                        continue
                    r = max(cur["L"], o["L"]) / max(1.0, min(cur["L"], o["L"]))
                    if r > len_ratio:
                        continue
                    if bestd is None or dist < bestd:
                        best, bestd = m, dist
                if best is None:
                    break
                chain.append(best)
                cur = cands[best]
        if len(chain) >= min_chain:
            used.update(chain)
            chains.append(chain)

    out = np.zeros_like(bw)
    for ch in chains:
        for k in ch:
            i = cands[k]["i"]
            x, y, w, h, _ = stats[i]
            out[y:y + h, x:x + w][lab[y:y + h, x:x + w] == i] = 255
    return out, len(chains)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--out-tiles", default=None, help="write cleaned tiles here for a later spotter pass")
    ap.add_argument("--diag", default=None, help="write a colour-coded diagnostic PNG here")
    ap.add_argument("--clean-png", default=None, help="write the cleaned sheet itself here")
    ap.add_argument("--pad", type=int, default=2)
    ap.add_argument("--stages", default="solid,hatch,line,dash")
    ap.add_argument("--no-flatten", dest="flatten", action="store_false",
                    help="skip paper-tone neutralisation (on by default)")
    ap.add_argument("--solid-k", type=int, default=5, help="opening element for solid fill")
    ap.add_argument("--line-length", type=int, default=70, help="px a TRACED stroke must reach to be linework")
    ap.add_argument("--max-half-width", type=float, default=2.6, help="undue breadth: thicker is not a line")
    ap.add_argument("--max-turn", type=float, default=32.0, help="degrees per chord; above this the stroke is cut")
    ap.add_argument("--hatch-short", type=int, default=9)
    ap.add_argument("--hatch-density", type=float, default=0.42)
    ap.add_argument("--mr-file", default=None)
    ap.add_argument("--amg-file", default=None)
    ap.add_argument("--protect", action="store_true", help="never erase ink inside a detector box")
    ap.add_argument("--mr-buffer", type=float, default=4.0)
    ap.add_argument("--amg-buffer", type=float, default=10.0)
    a = ap.parse_args()
    stages = set(s.strip() for s in a.stages.split(",") if s.strip())
    t0 = time.time()

    w, s, e, n = a.bbox
    tx0 = int(((w + 180.0) / 360.0 * N17 * 256) // 256) - a.pad
    tx1 = int(((e + 180.0) / 360.0 * N17 * 256) // 256) + a.pad
    ty0 = int(lat_px(n) // 256) - a.pad
    ty1 = int(lat_px(s) // 256) + a.pad
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    rgb, hit = stitch(tx0, ty0, nx, ny)
    print(f"{a.tag}: stitched {nx}x{ny} tiles ({hit} present) = {rgb.shape[1]}x{rgb.shape[0]} px "
          f"({time.time()-t0:.0f}s)", flush=True)
    if a.flatten:
        rgb = flat_field(rgb)
        print(f"  flat-fielded paper tone ({time.time()-t0:.0f}s)", flush=True)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    bw = binarize(gray)
    total_ink = int(bw.astype(bool).sum())
    masks = {}
    def frac(m):
        # share of INK claimed, not share of area: a blob mask also covers the white between strokes
        return ((m > 0) & (bw > 0)).sum() / max(1, total_ink)

    if "solid" in stages:
        masks["solid"] = find_solid(bw, a.solid_k)
        print(f"  solid  {frac(masks['solid']):.1%} of ink ({time.time()-t0:.0f}s)", flush=True)
    if "line" in stages:
        masks["line"], ntr = trace_lines(bw, max_half_width=a.max_half_width,
                                         min_length=a.line_length, max_turn=a.max_turn)
        print(f"  line   {frac(masks['line']):.1%} of ink in {ntr} traced strokes "
              f"({time.time()-t0:.0f}s)", flush=True)
    if "hatch" in stages:
        masks["hatch"] = find_hatch(bw, a.hatch_short, min_density=a.hatch_density)
        print(f"  hatch  {frac(masks['hatch']):.1%} of ink ({time.time()-t0:.0f}s)", flush=True)
    if "dash" in stages:
        masks["dash"], nch = find_dashes(bw)
        print(f"  dash   {frac(masks['dash']):.1%} of ink in {nch} chains "
              f"({time.time()-t0:.0f}s)", flush=True)

    protect = None
    if a.protect:
        polys = load_boxes(a.mr_file, a.mr_buffer) + load_boxes(a.amg_file, a.amg_buffer)
        protect = np.zeros_like(bw)
        for p in polys:
            cv2.fillPoly(protect, [(p - [tx0 * 256, ty0 * 256]).astype(np.int32)], 255)
        print(f"  protecting {len(polys)} boxes = {protect.astype(bool).mean():.1%} of the sheet", flush=True)

    removed = np.zeros_like(bw)
    for m in masks.values():
        removed |= (m > 0)
    removed = (removed > 0) & (bw > 0)
    if protect is not None:
        removed &= ~(protect > 0)
    print(f"  REMOVED {removed.sum()/total_ink:.1%} of all ink", flush=True)

    if a.diag:
        # Original in grey; each stage's removals in its own colour, so tuning is a visual act.
        rgb = np.repeat(gray[:, :, None], 3, 2).copy()
        for nm, col in (("solid", (220, 40, 40)), ("hatch", (240, 150, 20)),
                        ("line", (40, 90, 220)), ("dash", (30, 170, 70))):
            if nm not in masks:
                continue
            sel = (masks[nm] > 0) & (bw > 0)
            if protect is not None:
                sel &= ~(protect > 0)
            rgb[sel] = col
        os.makedirs(os.path.dirname(a.diag), exist_ok=True)
        cv2.imwrite(a.diag, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        print(f"  diagnostic -> {a.diag}", flush=True)

    out = gray.copy()
    out[removed] = 255
    if a.clean_png:
        os.makedirs(os.path.dirname(a.clean_png), exist_ok=True)
        cv2.imwrite(a.clean_png, out)
        print(f"  cleaned sheet -> {a.clean_png}", flush=True)
    if a.out_tiles:
        for i in range(nx):
            for j in range(ny):
                d = f"{a.out_tiles}/{tx0+i}"
                os.makedirs(d, exist_ok=True)
                t = out[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256]
                cv2.imwrite(f"{d}/{ty0+j}.png", cv2.cvtColor(np.repeat(t[:, :, None], 3, 2), cv2.COLOR_RGB2BGR))
        print(f"  wrote {nx*ny} cleaned tiles -> {a.out_tiles}", flush=True)
    print(f"SHEETCLEANDONE ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
