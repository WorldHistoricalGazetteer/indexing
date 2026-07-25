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
from hisam_pins import window_image, N17

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
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--r", type=int, default=8)
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--out-dir", default=PINS)
    ap.add_argument("--fade", type=float, default=0.45, help="how much to wash out the map under the ink")
    a = ap.parse_args()

    import math
    cxp = (a.lon + 180.0) / 360.0 * N17 * 256
    cyp = (1 - math.log(math.tan(math.radians(a.lat)) + 1 / math.cos(math.radians(a.lat))) / math.pi) / 2 * N17 * 256
    tx0, ty0 = int(cxp // 256) - a.r, int(cyp // 256) - a.r
    side = 2 * a.r + 1
    ox, oy = tx0 * 256, ty0 * 256

    img, hit = window_image(tx0, ty0, side)
    print(f"{a.tag}: {side}x{side} tiles, {hit} present ({img.shape[1]}x{img.shape[0]} px)", flush=True)
    if hit == 0:
        raise SystemExit("no tiles cached for this region")

    base = (img.astype(np.float32) * (1 - a.fade) + 255 * a.fade).astype(np.uint8)
    im = Image.fromarray(base).convert("RGB")
    d = ImageDraw.Draw(im)
    W = img.shape[1]

    def poly(p, colour, width=2):
        pts = [(float(x) - ox, float(y) - oy) for x, y in p]
        if all(v[0] < -50 or v[0] > W + 50 or v[1] < -50 or v[1] > W + 50 for v in pts):
            return False
        d.line(pts + [pts[0]], fill=colour, width=width)
        return True

    n_mr = 0
    mrf = f"{SPOT}/boxes_{a.tag}.jsonl"
    if os.path.exists(mrf):
        for line in open(mrf):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("gpoly"):
                n_mr += poly(r["gpoly"], (0, 158, 60), 2)

    n_hs = 0
    pf = f"{a.out_dir}/pins_{a.tag}.jsonl"
    if os.path.exists(pf):
        for line in open(pf):
            try:
                r = json.loads(line)
            except Exception:
                continue
            p = r.get("line_gpoly") or r.get("gpoly")
            if p:
                n_hs += poly(p, (220, 30, 30), 2)

    # GB1900 last, so the transcripts stay readable over the boxes.
    P = load_pins(a.pins)
    idx = pins_in_box(P, ox, oy, ox + W, oy + W)
    f = font(15)
    for k in idx:
        x, y = float(P["gx"][k]) - ox, float(P["gy"][k]) - oy
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(20, 60, 230), outline=(255, 255, 255))
        t = str(P["text"][k])
        d.text((x + 8, y - 8), t, fill=(20, 60, 230), font=f,
               stroke_width=3, stroke_fill=(255, 255, 255))

    key = ["GB1900 pin + transcript (blue)", "MapReader word box (green)", "Hi-SAM line mask (red)"]
    fk = font(26)
    d.rectangle([8, 8, 620, 8 + 34 * len(key) + 12], fill=(255, 255, 255), outline=(0, 0, 0))
    for i, (txt, col) in enumerate(zip(key, [(20, 60, 230), (0, 158, 60), (220, 30, 30)])):
        d.text((20, 16 + 34 * i), txt, fill=col, font=fk)

    out = f"{a.out_dir}/compare_{a.tag}.png"
    im.save(out)
    print(f"{a.tag}: {len(idx)} GB1900 pins, {n_mr} MapReader boxes, {n_hs} Hi-SAM boxes -> {out} "
          f"({os.path.getsize(out)/1e6:.0f} MB)", flush=True)
    print("COMPAREDONE", flush=True)


if __name__ == "__main__":
    main()
