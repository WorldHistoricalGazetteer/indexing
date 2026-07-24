"""PIN-PROMPTED Hi-SAM label detection — Phase A and Phase B in one pass.

The handoff plan (NEXT-PHASE.md) proposed running Hi-SAM's automatic mask generator over full-res tiles and
then spatially joining the detections back to the GB1900 gazetteer for transcripts. Hi-SAM's decoder accepts
`oracle_point_prompts`, so the two steps collapse: prompt the hierarchical decoder AT each GB1900 pin and every
detection is born already attached to its transcript. Consequences, all in our favour:

  * no 1500-point AMG grid per window — the decoder runs once per pin (a handful per window), and the ViT-L
    image encoder pass becomes the only real cost;
  * no cross-window merge/dedup of the letter-spaced admin labels, and no nearest-match radius to tune: the
    association is exact, by construction;
  * the hierarchy does the grouping for free. Hi-SAM's HiDecoder returns four mask tokens per prompt; after the
    leading single-mask token they are (word, line, paragraph). A letter-spaced `ST  ALDATE` fragments at the
    WORD level but is one mask at the LINE level, which is precisely the label extent we want to crop.

What it CANNOT do is audit GB1900's own recall (labels printed on the sheet that no volunteer pinned) — that
needs the AMG sweep and is a separate question.

Output is one record per pin, deliberately field-compatible with the MapReader `boxes_*.jsonl` records
(`text`/`score`/`gpoly`/`gcx`/`gcy`/`lon`/`lat`) so the existing crop + descriptor tooling reads it unchanged.

    python hisam_pins.py --tag gb_4338_2896 --lon -1.25931 --lat 51.74149 --r 8
"""
import argparse, io, json, math, os, sys, time, urllib.request
import numpy as np, cv2
from PIL import Image

REPO = "/vast/ishi/gb1900/probe/hisam/Hi-SAM"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import torch
from types import SimpleNamespace
from hi_sam.modeling.build import model_registry
from hi_sam.modeling.auto_mask_generator import AutoMaskGenerator
from build_pin_index import load_pins, pins_in_box

N17 = 2 ** 17
TILES = "/vast/ishi/gb1900/tiles17"
IX1 = "/ix1/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
OUT = "/vast/ishi/gb1900/edition/pins"
# HiDecoder emits 4 mask tokens; forward(multimask_output=True) already drops token 0 (the single-mask output)
# from BOTH the masks and the iou predictions, so what forward_hi_decoder hands back is the hierarchy itself:
WORD, LINE, PARA = 0, 1, 2
# (AutoMaskGenerator.predict slices [:, 1:] a second time, which is why it addresses line/para as [-2]/[-1]
# while still reading scores[:, 1] — the un-sliced line score. Easy to mirror wrongly; don't.)


def px_to_lonlat(px, py):
    lon = px / (N17 * 256) * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / (N17 * 256)))))
    return lon, lat


def read_tile(tx, ty, fetch=False):
    for base in (TILES, IX1):
        p = f"{base}/{tx}/{ty}.png"
        if os.path.exists(p) and os.path.getsize(p) > 500:
            try:
                return np.asarray(Image.open(p).convert("RGB"), np.uint8)
            except Exception:
                pass
    if not fetch:
        return None
    os.makedirs(f"{TILES}/{tx}", exist_ok=True)
    for attempt in range(5):                                   # S3 answers 503 SlowDown under array load
        try:
            req = urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-spot"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            if len(data) <= 400:
                return None                                    # ocean/absent tile — legitimately empty
            open(f"{TILES}/{tx}/{ty}.png", "wb").write(data)
            return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), np.uint8)
        except Exception as e:
            if getattr(e, "code", None) in (403, 404):
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def window_image(tx0, ty0, ntiles, fetch=False):
    """Assemble an ntiles x ntiles z17 window. Missing tiles stay white (Hi-SAM sees blank paper, not noise)."""
    W = ntiles * 256
    canvas = np.full((W, W, 3), 255, np.uint8)
    hit = 0
    for i in range(ntiles):
        for j in range(ntiles):
            t = read_tile(tx0 + i, ty0 + j, fetch)
            if t is not None:
                canvas[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = t
                hit += 1
    return canvas, hit


def build_model(model_type, ckpt):
    # Hi-SAM's _build_sam opens the SAM backbone at the RELATIVE path 'pretrained_checkpoint/...', so the build
    # only works with CWD at the repo. Resolve our own checkpoint first, then restore CWD — every other path in
    # this script is absolute, but leaving the process parked in the repo is a trap for the next caller.
    args = SimpleNamespace(model_type=model_type, checkpoint=os.path.abspath(ckpt), hier_det=True,
                           attn_layers=1, prompt_len=12, input_size=[1024, 1024], device="cuda")
    cwd = os.getcwd()
    os.chdir(REPO)
    try:
        m = model_registry[model_type](args)
    finally:
        os.chdir(cwd)
    m.to("cuda")
    m.eval()
    return m, AutoMaskGenerator(m)


@torch.no_grad()
def foreground_mask(amg, model, out_hw):
    """Hi-SAM's pixel text-foreground mask for the current image, at window resolution.

    One extra decoder pass per window (the same route AutoMaskGenerator uses to seed its point grid), which is
    negligible next to the ViT-L encode we have already paid for.
    """
    sparse = model.modal_aligner(amg.features)
    _, high, _, _ = model.mask_decoder(image_embeddings=amg.features,
                                       image_pe=model.prompt_encoder.get_dense_pe(),
                                       sparse_prompt_embeddings=sparse, multimask_output=False)
    fg = (high > model.mask_threshold).squeeze(1)[0].cpu().numpy().astype(np.uint8)
    if fg.shape != tuple(out_hw):
        fg = cv2.resize(fg, (out_hw[1], out_hw[0]), interpolation=cv2.INTER_NEAREST)
    return fg


def snap_to_ink(pts, fg, radius):
    """Move each prompt to the nearest text pixel within `radius`, leaving it alone if there is none.

    GB1900 pins are volunteer click points, so a fair share land in the white space beside their label. SAM-family
    models are prompt-sensitive: a point on blank paper is a genuinely different query from a point on the ink.
    Snapping is a correction to the PROMPT, not to the detection — a pin with no ink nearby stays put and is
    recorded as off-ink so it can be counted rather than quietly rescued.
    """
    H, W = fg.shape
    out = pts.copy()
    moved = 0
    for i, (x, y) in enumerate(pts):
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < W and 0 <= yi < H and fg[yi, xi]:
            continue
        x0, x1 = max(0, xi - radius), min(W, xi + radius + 1)
        y0, y1 = max(0, yi - radius), min(H, yi + radius + 1)
        sub = fg[y0:y1, x0:x1]
        ys, xs = np.nonzero(sub)
        if len(xs) == 0:
            continue
        d = (xs + x0 - x) ** 2 + (ys + y0 - y) ** 2
        k = int(np.argmin(d))
        out[i] = [xs[k] + x0, ys[k] + y0]
        moved += 1
    return out, moved


@torch.no_grad()
def prompt_pins(amg, model, pts_xy, batch=32):
    """Run the hierarchical decoder at the given window-pixel points.

    Deliberately bypasses AutoMaskGenerator.predict(): that method score-thresholds and NMSes the prompt set,
    which would silently break the 1:1 pin->detection correspondence this whole design rests on. Here every
    prompt returns its own masks and the caller decides what to keep.

    Returns (word_masks (n,H,W) bool, hier (n,3,256,256) bool = word/line/para, scores (n,3) float).
    """
    pts = amg.transform.apply_coords(pts_xy.astype(np.float64), amg.original_size)
    fg = torch.as_tensor(pts, dtype=torch.int64, device=amg.device)[:, None, :]
    words, hiers, scores = [], [], []
    for s in range(0, len(fg), batch):
        pb = fg[s:s + batch]
        lab = torch.ones((len(pb), 1), device=pb.device)
        hi_logits, iou, word_logits = amg.forward_hi_decoder(pb, lab)
        hiers.append((hi_logits > model.mask_threshold).cpu().numpy())
        wm = model.postprocess_masks(word_logits, amg.input_size, amg.original_size)
        words.append((wm > model.mask_threshold)[:, 0].cpu().numpy())
        scores.append(iou.float().cpu().numpy())
    return np.concatenate(words), np.concatenate(hiers), np.concatenate(scores)


def mask_poly(mask, scale=1.0, ox=0.0, oy=0.0):
    """Oriented extent of a mask as a 4-point polygon in global px (minAreaRect, like MapReader's gpoly).

    Areas are returned in GLOBAL px, i.e. scaled by `scale`**2 — the hierarchy masks are always 256px while the
    word mask comes back at window resolution, so raw pixel counts from the two are 16x apart and any
    line-vs-word ratio computed from them is meaningless.
    """
    ys, xs = np.where(mask)
    if len(xs) < 6:
        return None, 0
    pts = np.stack([xs, ys], 1).astype(np.float32)
    box = cv2.boxPoints(cv2.minAreaRect(pts))
    poly = [[round(float(x) * scale + ox, 1), round(float(y) * scale + oy, 1)] for x, y in box]
    return poly, int(round(len(xs) * scale * scale))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--r", type=int, default=8, help="region radius in z17 tiles (side = 2r+1)")
    ap.add_argument("--win", type=int, default=4, help="window side in tiles (4 = 1024px = Hi-SAM native)")
    ap.add_argument("--margin", type=int, default=1, help="context tiles kept outside the window's pin core")
    ap.add_argument("--weight", default="/vast/ishi/gb1900/probe/hisam/weights/hi_sam_l.pth")
    ap.add_argument("--model-type", default="vit_l")
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--fetch", action="store_true", help="fetch uncached tiles from S3 (GPU nodes have net)")
    ap.add_argument("--out-dir", default=OUT)
    ap.add_argument("--min-score", type=float, default=0.0, help="keep-all by default; filter downstream")
    ap.add_argument("--snap", type=int, default=0, metavar="PX",
                    help="snap each prompt to the nearest text pixel within PX (0 = prompt at the raw pin)")
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    outp = f"{a.out_dir}/pins_{a.tag}.jsonl"
    t0 = time.time()

    P = load_pins(a.pins)
    cxp = (a.lon + 180.0) / 360.0 * N17 * 256
    cyp = (1 - math.log(math.tan(math.radians(a.lat)) + 1 / math.cos(math.radians(a.lat))) / math.pi) / 2 * N17 * 256
    ctx, cty = int(cxp // 256), int(cyp // 256)
    tx0, ty0 = ctx - a.r, cty - a.r
    side = 2 * a.r + 1
    region = pins_in_box(P, tx0 * 256, ty0 * 256, (tx0 + side) * 256, (ty0 + side) * 256)
    print(f"{a.tag}: region {side}x{side} tiles, {len(region)} GB1900 pins", flush=True)
    if len(region) == 0:
        open(outp, "w").close()
        print(f"[{a.tag}] DONE 0 pins -> {outp}", flush=True)
        return

    model, amg = build_model(a.model_type, a.weight)
    core = a.win - 2 * a.margin
    if core < 1:
        raise SystemExit(f"--win {a.win} with --margin {a.margin} leaves no pin core")

    # Step over CORE tiles; each window adds `margin` tiles of context on every side so a label whose pin sits
    # near the core edge still has its ink inside the encoded image.
    done = set()
    nwin = nempty = nmoved = 0
    recs = []
    for i in range(0, side, core):
        for j in range(0, side, core):
            cx0, cy0 = tx0 + i, ty0 + j
            cx1, cy1 = min(cx0 + core, tx0 + side), min(cy0 + core, ty0 + side)
            sel = pins_in_box(P, cx0 * 256, cy0 * 256, cx1 * 256, cy1 * 256)
            sel = np.array([k for k in sel if k not in done], np.int64)
            if len(sel) == 0:
                continue
            wx0, wy0 = cx0 - a.margin, cy0 - a.margin
            img, hit = window_image(wx0, wy0, a.win, a.fetch)
            nwin += 1
            if hit == 0:
                nempty += 1
                continue
            ox, oy = wx0 * 256, wy0 * 256
            pts_raw = np.stack([P["gx"][sel] - ox, P["gy"][sel] - oy], 1)
            pts = pts_raw
            amg.set_image(img)
            try:
                if a.snap:
                    fg = foreground_mask(amg, model, img.shape[:2])
                    pts, mv = snap_to_ink(pts_raw, fg, a.snap)
                    nmoved += mv
                words, hier, scores = prompt_pins(amg, model, pts)
            except torch.cuda.OutOfMemoryError:
                print(f"  [{a.tag}] OOM on window {wx0},{wy0} ({len(sel)} pins) — skipped", flush=True)
                torch.cuda.empty_cache()
                continue
            W = a.win * 256
            hs = W / hier.shape[-1]                       # hierarchy masks are 256px regardless of window size
            for n, k in enumerate(sel):
                done.add(int(k))
                if float(scores[n][WORD]) < a.min_score:
                    continue
                gpoly, warea = mask_poly(words[n], 1.0, ox, oy)
                lpoly, larea = mask_poly(hier[n][LINE], hs, ox, oy)
                ppoly, parea = mask_poly(hier[n][PARA], hs, ox, oy)
                if gpoly is None and lpoly is None:
                    continue                              # prompt landed on blank paper — no ink to describe
                ref = gpoly or lpoly
                gcx = sum(p[0] for p in ref) / 4.0
                gcy = sum(p[1] for p in ref) / 4.0
                lon, lat = px_to_lonlat(gcx, gcy)
                px, py = float(pts_raw[n][0]), float(pts_raw[n][1])   # judge on-ink at the VOLUNTEER's point
                lm = hier[n][LINE]
                recs.append(dict(
                    pin_id=str(P["pin_id"][k]), text=str(P["text"][k]),
                    score=round(float(scores[n][WORD]), 4),
                    line_score=round(float(scores[n][LINE]), 4),
                    gpoly=gpoly, line_gpoly=lpoly, para_gpoly=ppoly,
                    word_area=warea, line_area=larea, para_area=parea,
                    gcx=round(gcx, 1), gcy=round(gcy, 1), lon=round(lon, 6), lat=round(lat, 6),
                    pin_gx=round(float(P["gx"][k]), 1), pin_gy=round(float(P["gy"][k]), 1),
                    # QC flags — the honest caveats, recorded per detection rather than assumed away:
                    on_ink=bool(words[n][min(int(py), W - 1), min(int(px), W - 1)]),
                    snapped=bool(a.snap and (pts[n][0] != pts_raw[n][0] or pts[n][1] != pts_raw[n][1])),
                    truncated=bool(lm[0].any() or lm[-1].any() or lm[:, 0].any() or lm[:, -1].any()),
                    win=[wx0, wy0, a.win],
                ))
            if nwin % 10 == 0:
                print(f"  [{a.tag}] window {nwin} ({len(recs)} detections, {time.time()-t0:.0f}s)", flush=True)

    with open(outp, "w") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    onink = sum(r["on_ink"] for r in recs)
    trunc = sum(r["truncated"] for r in recs)
    print(f"[{a.tag}] DONE {len(recs)}/{len(region)} pins detected in {nwin} windows ({nempty} blank); "
          f"snapped {nmoved}, on-ink {onink} ({onink/max(1,len(recs)):.1%}), line-truncated {trunc} "
          f"({trunc/max(1,len(recs)):.1%}) -> {outp} ({time.time()-t0:.0f}s)", flush=True)
    print("PINSDONE", flush=True)


if __name__ == "__main__":
    main()
