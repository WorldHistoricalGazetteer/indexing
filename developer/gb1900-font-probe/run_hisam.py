"""Run Hi-SAM word detection on the test mosaics and overlay its detections (RED) against MapReader's boxes
(BLUE) on the same imagery — the direct "does a newer segmenter recover what MapReader missed" comparison,
focused on the letter-spaced admin caps (OXFORD / ST ALDATE / HOLYWELL) and sparse antiquities. Mosaics are
downscaled to <=2048 px (the big spaced labels survive; keeps mask memory sane); MapReader boxes scaled to match.

    python run_hisam.py --weight weights/hi_sam_l.pth --model-type vit_l
"""
import argparse, os, glob, json, sys, numpy as np, cv2
REPO = "/vast/ishi/gb1900/probe/hisam/Hi-SAM"
sys.path.insert(0, REPO)                                       # importable without chdir
import torch
from types import SimpleNamespace
from PIL import Image
from hi_sam.modeling.build import model_registry
from hi_sam.modeling.auto_mask_generator import AutoMaskGenerator

TEST = "/vast/ishi/gb1900/edition/spot/hisam_test"; MAX = 2048

def build(model_type, ckpt):
    args = SimpleNamespace(model_type=model_type, checkpoint=ckpt, hier_det=True,
                           attn_layers=1, prompt_len=12, input_size=[1024, 1024], device="cuda")
    m = model_registry[model_type](args); m.to("cuda"); m.eval()
    return AutoMaskGenerator(m)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weight", required=True); ap.add_argument("--model-type", default="vit_l")
    ap.add_argument("--points", type=int, default=1500); a = ap.parse_args()
    weight = os.path.abspath(a.weight)                         # resolve BEFORE chdir
    os.chdir(REPO)                                             # build reads pretrained_checkpoint/ relative to CWD
    print(f"building {a.model_type} from {weight}", flush=True)
    amg = build(a.model_type, weight)

    for mp in sorted(glob.glob(f"{TEST}/mosaic_*.png")):
        tag = os.path.basename(mp)[7:-4]
        img0 = np.asarray(Image.open(mp).convert("RGB"), np.uint8); H0, W0 = img0.shape[:2]
        sc = min(1.0, MAX / max(H0, W0)); img = cv2.resize(img0, (int(W0 * sc), int(H0 * sc))) if sc < 1 else img0
        H, W = img.shape[:2]
        amg.set_image(img)
        try:
            masks, scores, aff = amg.predict(from_low_res=False, fg_points_num=a.points,
                                             batch_points_num=100, score_thresh=0.5, nms_thresh=0.5)
        except torch.cuda.OutOfMemoryError:
            print(f"  {tag}: OOM at {W}x{H}", flush=True); torch.cuda.empty_cache(); continue
        hboxes = []
        if masks is not None:
            mk = masks[:, 0, :, :].astype(np.uint8)                       # (n,h,w) word masks
            for m in mk:
                ys, xs = np.where(m > 0)
                if len(xs) > 4: hboxes.append((int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())))
        # MapReader boxes (mosaic px -> scaled)
        mrb = []
        mpf = f"{TEST}/mrboxes_{tag}.json"
        if os.path.exists(mpf):
            for b in json.load(open(mpf)):
                p = np.array(b["poly"]) * sc; mrb.append((p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()))
        # overlay: lighten map, MapReader blue, Hi-SAM red
        ov = (img.astype(np.float32) * 0.55 + 255 * 0.45).astype(np.uint8)
        for x0, y0, x1, y1 in mrb: cv2.rectangle(ov, (int(x0), int(y0)), (int(x1), int(y1)), (30, 80, 220), 2)
        for x0, y0, x1, y1 in hboxes: cv2.rectangle(ov, (x0, y0), (x1, y1), (220, 40, 40), 2)
        Image.fromarray(ov).save(f"{TEST}/overlay_{tag}.png")
        v = Image.fromarray(ov); v.thumbnail((1600, 1600)); v.save(f"{TEST}/overlayview_{tag}.png")
        print(f"  {tag}: Hi-SAM words {len(hboxes)}  |  MapReader boxes {len(mrb)}  ({W}x{H}) -> overlay_{tag}.png", flush=True)
    print("HISAMDONE", flush=True)

if __name__ == "__main__":
    main()
