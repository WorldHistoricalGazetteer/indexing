"""Stitch a z17 region and overlay all three layers on it, for eyeballing.

  GB1900   blue dot + transcript      what we are trying to type (the corpus)
  MapReader GREEN box                 the old detector's word boxes
  Hi-SAM    RED box                   the pin-prompted LINE mask = the crop the descriptor actually sees

Written at native z17 resolution rather than downscaled, because the question these images answer — is the red
box on the label the transcript names? — is invisible at any smaller scale.

    python render_compare.py --tag gb_4331_2813 --lon .. --lat .. --r 8
"""
import argparse, json, os, sys, numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_pin_index import load_pins, pins_in_box
from hisam_pins import read_tile, N17


def window_image_rect(tx0, ty0, nx, ny, fetch=False, tiles_dir=None):
    """Stitch a RECTANGULAR tile extent — an OS sheet is 3:2, not square.

    `tiles_dir` reads a PROCESSED cache (masked / de-noised) with no fallback to the original imagery, so a
    missing processed tile shows as blank rather than silently reverting to the untouched sheet.
    """
    canvas = np.full((ny * 256, nx * 256, 3), 255, np.uint8)
    hit = 0
    for i in range(nx):
        for j in range(ny):
            if tiles_dir:
                p = f"{tiles_dir}/{tx0+i}/{ty0+j}.png"
                t = None
                if os.path.exists(p):
                    try:
                        t = np.asarray(Image.open(p).convert("RGB"), np.uint8)
                    except Exception:
                        t = None
            else:
                t = read_tile(tx0 + i, ty0 + j, fetch)
            if t is not None:
                canvas[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = t
                hit += 1
    return canvas, hit

SPOT = "/vast/ishi/gb1900/edition/spot"
PINS = "/vast/ishi/gb1900/edition/pins"


def font(sz):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    return ImageFont.load_default()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--r", type=int, default=8)
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                    help="an OS sheet's extent, rather than an arbitrary square")
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--mr-file", default=None, help="MapReader boxes jsonl (default: boxes_<tag>.jsonl)")
    ap.add_argument("--pins-file", default=None, help="Hi-SAM detections jsonl (default: pins_<tag>.jsonl)")
    ap.add_argument("--amg-file", default=None, help="Hi-SAM AUTOMATIC-mode detections jsonl (4th layer)")
    ap.add_argument("--out-dir", default=PINS)
    ap.add_argument("--fetch", action="store_true", help="pull uncached tiles rather than leaving holes")
    ap.add_argument("--tiles-dir", default=None,
                    help="stitch a PROCESSED tile cache (masked / de-noised) instead of the original imagery")
    ap.add_argument("--jpeg", type=int, default=0, metavar="Q", help="write JPEG at quality Q instead of PNG")
    ap.add_argument("--fade", type=float, default=0.45, help="how much to wash out the map under the ink")
    a = ap.parse_args()

    import math

    def lat_px(lat):
        return (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256

    if a.bbox:
        w, s, e, n = a.bbox
        tx0 = int(((w + 180.0) / 360.0 * N17 * 256) // 256)
        tx1 = int(((e + 180.0) / 360.0 * N17 * 256) // 256)
        ty0, ty1 = int(lat_px(n) // 256), int(lat_px(s) // 256)
        nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    else:
        tx0, ty0 = int(((a.lon + 180.0) / 360.0 * N17 * 256) // 256) - a.r, int(lat_px(a.lat) // 256) - a.r
        nx = ny = 2 * a.r + 1
    ox, oy = tx0 * 256, ty0 * 256

    img, hit = window_image_rect(tx0, ty0, nx, ny, a.fetch, a.tiles_dir)
    print(f"{a.tag}: {nx}x{ny} tiles, {hit}/{nx*ny} present ({img.shape[1]}x{img.shape[0]} px)", flush=True)
    if hit == 0:
        raise SystemExit("no tiles for this extent")

    base = (img.astype(np.float32) * (1 - a.fade) + 255 * a.fade).astype(np.uint8)
    im = Image.fromarray(base).convert("RGB")
    d = ImageDraw.Draw(im)
    W, H = img.shape[1], img.shape[0]

    def poly(p, colour, width=2):
        pts = [(float(x) - ox, float(y) - oy) for x, y in p]
        if all(v[0] < -50 or v[0] > W + 50 or v[1] < -50 or v[1] > H + 50 for v in pts):
            return False
        d.line(pts + [pts[0]], fill=colour, width=width)
        return True

    n_mr = 0
    mrf = a.mr_file or f"{SPOT}/boxes_{a.tag}.jsonl"
    if os.path.exists(mrf):
        for line in open(mrf):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("gpoly"):
                n_mr += poly(r["gpoly"], (0, 158, 60), 2)

    n_hs = 0
    pf = a.pins_file or f"{a.out_dir}/pins_{a.tag}.jsonl"
    if os.path.exists(pf):
        for line in open(pf):
            try:
                r = json.loads(line)
            except Exception:
                continue
            p = r.get("line_gpoly") or r.get("gpoly")
            if p:
                n_hs += poly(p, (220, 30, 30), 2)

    # AMG under the others: it is the densest layer and would otherwise bury them.
    n_amg = 0
    if a.amg_file and os.path.exists(a.amg_file):
        for line in open(a.amg_file):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("gpoly"):
                n_amg += poly(r["gpoly"], (170, 40, 190), 2)

    # GB1900 last, so the transcripts stay readable over the boxes.
    P = load_pins(a.pins)
    idx = pins_in_box(P, ox, oy, ox + W, oy + H)
    f = font(15)
    for k in idx:
        x, y = float(P["gx"][k]) - ox, float(P["gy"][k]) - oy
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(20, 60, 230), outline=(255, 255, 255))
        t = str(P["text"][k])
        d.text((x + 8, y - 8), t, fill=(20, 60, 230), font=f,
               stroke_width=3, stroke_fill=(255, 255, 255))

    key = [("GB1900 pin + transcript (blue)", (20, 60, 230)),
           ("MapReader word box (green)", (0, 158, 60)),
           ("Hi-SAM pin-prompted line mask (red)", (220, 30, 30))]
    if n_amg:
        key.append(("Hi-SAM automatic / AMG (purple)", (170, 40, 190)))
    fk = font(26)
    d.rectangle([8, 8, 700, 8 + 34 * len(key) + 12], fill=(255, 255, 255), outline=(0, 0, 0))
    for i, (txt, col) in enumerate(key):
        d.text((20, 16 + 34 * i), txt, fill=col, font=fk)

    if a.jpeg:
        out = f"{a.out_dir}/compare_{a.tag}.jpg"
        im.save(out, quality=a.jpeg, subsampling=0)      # 4:4:4 — chroma subsampling smears thin red boxes
    else:
        out = f"{a.out_dir}/compare_{a.tag}.png"
        im.save(out)
    print(f"{a.tag}: {len(idx)} GB1900 pins, {n_mr} MapReader boxes, {n_hs} Hi-SAM boxes -> {out} "
          f"({os.path.getsize(out)/1e6:.0f} MB)", flush=True)
    print("COMPAREDONE", flush=True)


if __name__ == "__main__":
    main()
