"""Hi-SAM in AUTOMATIC mode over a whole OS sheet — the independent "what text is actually printed here" layer.

The pin-prompted detector can only ever find labels GB1900 pinned, and MapReader only finds what its word
spotter fires on. Neither measures what is PRINTED on the sheet, so "both models are missing texts" cannot be
tested with either of them. AMG samples a foreground point grid and returns every text instance it can find,
independent of both — not ground truth, but a third opinion that is not conditioned on the other two.

Tiles the sheet into native-resolution windows (AMG's cost is per window, and downscaling a whole sheet to
1024px is what lost the dense small text in the original feasibility run). Detections are kept only when their
centroid falls in the window's core, which de-duplicates the overlap without any distance heuristic.

    python run_amg_sheet.py --tag sheet_ENG_218_NW --bbox -1.5875 53.7823 -1.514 53.8115
"""
import argparse, json, math, os, sys, time
import numpy as np, cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = "/vast/ishi/gb1900/probe/hisam/Hi-SAM"
sys.path.insert(0, REPO)
import torch
from hi_sam.modeling.auto_mask_generator import AutoMaskGenerator          # noqa: F401  (via build_model)
from hisam_pins import build_model, read_tile, mask_poly, px_to_lonlat, N17, LINE, PARA

OUT = "/vast/ishi/gb1900/edition/amg"


def window_image_rect(tx0, ty0, nx, ny, fetch=False):
    canvas = np.full((ny * 256, nx * 256, 3), 255, np.uint8)
    hit = 0
    for i in range(nx):
        for j in range(ny):
            t = read_tile(tx0 + i, ty0 + j, fetch)
            if t is not None:
                canvas[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = t
                hit += 1
    return canvas, hit


@torch.no_grad()
def amg_lines(amg, model, n_points, nms_iou):
    """AMG point sampling, but decoded at the LINE level and de-duplicated.

    AutoMaskGenerator.predict returns only the word masks; the line and paragraph channels are computed and
    thrown away. Here the decoder is called directly so the line channel survives. Every foreground point on a
    label returns that whole label, so the same line comes back many times over — greedy IoU suppression keeps
    the highest-scoring instance of each.
    """
    pts = amg.forward_foreground_points(False, n_points)
    if pts is None or pts.shape[0] == 0:
        return None, None
    masks, scores = [], []
    for s in range(0, pts.shape[0], 100):
        pb = pts[s:s + 100]
        hi, iou, _ = amg.forward_hi_decoder(pb, torch.ones((len(pb), 1), device=pb.device))
        masks.append((hi[:, LINE] > model.mask_threshold).cpu().numpy())
        scores.append(iou[:, LINE].float().cpu().numpy())
    m = np.concatenate(masks)
    sc = np.concatenate(scores)
    order = np.argsort(-sc)
    keep, kept = [], []
    flat = m.reshape(len(m), -1).astype(bool)
    for i in order:
        a_ = flat[i]
        n_ = a_.sum()
        if n_ < 8:
            continue
        dup = False
        for j in kept:
            b_ = flat[j]
            inter = np.count_nonzero(a_ & b_)
            if inter and inter / (n_ + b_.sum() - inter) > nms_iou:
                dup = True
                break
        if not dup:
            kept.append(i)
            keep.append(i)
    if not keep:
        return None, None
    # line masks are 256px; the caller scales polygons by the window/256 ratio
    return m[keep][:, None, :, :], sc[keep][:, None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--win", type=int, default=4, help="window side in tiles (4 = 1024px = Hi-SAM native)")
    ap.add_argument("--margin", type=int, default=1)
    ap.add_argument("--points", type=int, default=1500, help="AMG foreground point grid per window")
    ap.add_argument("--weight", default="/vast/ishi/gb1900/probe/hisam/weights/hi_sam_l.pth")
    ap.add_argument("--model-type", default="vit_l")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--level", default="word", choices=["word", "line"],
                    help="LINE groups a letter-spaced label into one detection. The big admin labels are "
                         "exactly the ones that fragment at word level — a spaced ST ALDATE becomes eight "
                         "masks of 6px, which is why the word-level output looked as though the sheet had no "
                         "large lettering on it at all.")
    ap.add_argument("--nms", type=float, default=0.55,
                    help="line masks repeat: every foreground point on one label returns that whole label")
    ap.add_argument("--out-dir", default=OUT)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    t0 = time.time()

    def lat_px(lat):
        return (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256

    w, s, e, n = a.bbox
    tx0 = int(((w + 180.0) / 360.0 * N17 * 256) // 256)
    tx1 = int(((e + 180.0) / 360.0 * N17 * 256) // 256)
    ty0, ty1 = int(lat_px(n) // 256), int(lat_px(s) // 256)
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    core = a.win - 2 * a.margin
    print(f"{a.tag}: {nx}x{ny} tiles, core {core}, ~{math.ceil(nx/core)*math.ceil(ny/core)} windows", flush=True)

    model, amg = build_model(a.model_type, a.weight)
    recs = []
    nwin = 0
    for i in range(0, nx, core):
        for j in range(0, ny, core):
            cx0, cy0 = tx0 + i, ty0 + j
            cx1, cy1 = min(cx0 + core, tx0 + nx), min(cy0 + core, ty0 + ny)
            wx0, wy0 = cx0 - a.margin, cy0 - a.margin
            img, hit = window_image_rect(wx0, wy0, a.win, a.win, a.fetch)
            nwin += 1
            if hit == 0:
                continue
            ox, oy = wx0 * 256, wy0 * 256
            amg.set_image(img)
            try:
                if a.level == "word":
                    masks, scores, _ = amg.predict(from_low_res=False, fg_points_num=a.points,
                                                   batch_points_num=100, score_thresh=0.5, nms_thresh=0.5)
                else:
                    masks, scores = amg_lines(amg, model, a.points, a.nms)
            except torch.cuda.OutOfMemoryError:
                print(f"  OOM at window {wx0},{wy0}", flush=True)
                torch.cuda.empty_cache()
                continue
            if masks is None:
                continue
            # Keep only detections centred in the CORE: the margin exists for context, and anything centred
            # there belongs to the neighbouring window. Exact de-duplication, no distance threshold.
            kx0, ky0 = (cx0 - wx0) * 256, (cy0 - wy0) * 256
            kx1, ky1 = (cx1 - wx0) * 256, (cy1 - wy0) * 256
            scale = (a.win * 256) / masks.shape[-1]     # line masks are 256px whatever the window size
            for k in range(masks.shape[0]):
                m = masks[k, 0]
                ys, xs = np.where(m)
                if len(xs) < 6:
                    continue
                cxm, cym = xs.mean() * scale, ys.mean() * scale
                if not (kx0 <= cxm < kx1 and ky0 <= cym < ky1):
                    continue
                poly, area = mask_poly(m, scale, ox, oy)
                if poly is None:
                    continue
                gcx = sum(p[0] for p in poly) / 4.0
                gcy = sum(p[1] for p in poly) / 4.0
                lon, lat = px_to_lonlat(gcx, gcy)
                recs.append(dict(gpoly=poly, area=area, gcx=round(gcx, 1), gcy=round(gcy, 1),
                                 lon=round(lon, 6), lat=round(lat, 6),
                                 # word mode returns (n,2) scores, line mode (n,1) — take the last either way
                                 score=round(float(np.ravel(scores[k])[-1]) if scores is not None else 0.0, 4)))
            if nwin % 10 == 0:
                print(f"  [{a.tag}] window {nwin} ({len(recs)} detections, {time.time()-t0:.0f}s)", flush=True)

    outp = f"{a.out_dir}/amg_{a.level}_{a.tag}.jsonl"
    with open(outp, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    print(f"[{a.tag}] DONE {len(recs)} AMG detections in {nwin} windows -> {outp} "
          f"({time.time()-t0:.0f}s)", flush=True)
    print("AMGSHEETDONE", flush=True)


if __name__ == "__main__":
    main()
