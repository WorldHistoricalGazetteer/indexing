"""Stage 1 of backbone-on-clean-strokes: for each of the 189 anchors, use Hi-SAM's stroke mask to WHITEN the
map background of the grayscale crop (keep ink pixels at their grey values — NOT the binary mask, which is OOD
for the ViTAEv2 backbone). Save {orig, clean} grayscale crops for Stage 2 (backbone descriptor, mapreader env).

    python hisam_clean_crops.py --weight /vast/.../hi_sam_l.pth --model-type vit_l   # hisam env, GPU
"""
import argparse, os, glob, json, sys, numpy as np, cv2
sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
REPO = "/vast/ishi/gb1900/probe/hisam/Hi-SAM"; sys.path.insert(0, REPO); os.chdir(REPO)
import torch
from types import SimpleNamespace
from hi_sam.modeling.build import model_registry
from hi_sam.modeling.auto_mask_generator import AutoMaskGenerator
from make_font_testset_v2 import derotate

HERE = "/vast/ishi/gb1900/probe/font"; SPOT = "/vast/ishi/gb1900/edition/spot"
def key(gx, gy): return (round(float(gx), 1), round(float(gy), 1))

def build(mt, ckpt):
    args = SimpleNamespace(model_type=mt, checkpoint=os.path.abspath(ckpt), hier_det=True,
                           attn_layers=1, prompt_len=12, input_size=[1024, 1024], device="cuda")
    m = model_registry[mt](args); m.to("cuda"); m.eval(); return m, AutoMaskGenerator(m)

@torch.no_grad()
def stroke_native(amg, model, crop_gray):
    amg.set_image(np.repeat(crop_gray[:, :, None], 3, 2))
    feat = amg.features
    sparse = model.modal_aligner(feat)
    low, high, _, _ = model.mask_decoder(image_embeddings=feat, image_pe=model.prompt_encoder.get_dense_pe(),
                                         sparse_prompt_embeddings=sparse, multimask_output=False)
    fg = (high > model.mask_threshold).squeeze(1)[0].cpu().numpy().astype(np.uint8)
    h, w = crop_gray.shape[:2]; s = 1024.0 / max(h, w); rh, rw = max(1, int(h * s)), max(1, int(w * s))
    return cv2.resize(fg[:rh, :rw] * 255, (w, h), interpolation=cv2.INTER_NEAREST) > 127

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--weight", default="weights/hi_sam_l.pth")
    ap.add_argument("--model-type", default="vit_l"); a = ap.parse_args()
    model, amg = build(a.model_type, a.weight)
    lab = [l for l in json.load(open(f"{HERE}/labels/pool_labels.json")) if l.get("sig")]
    want = {key(l["gcx"], l["gcy"]) for l in lab}; rec = {}
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(f):
            try: r = json.loads(line)
            except Exception: continue
            k = key(r["gcx"], r["gcy"])
            if k in want and k not in rec: rec[k] = r
        if len(rec) >= len(want): break
    origs, cleans, sigs = [], [], []
    for l in lab:
        r = rec.get(key(l["gcx"], l["gcy"]))
        if r is None: continue
        crop = derotate(r)
        if crop is None or crop.size < 80: continue
        m = stroke_native(amg, model, crop)
        cleaned = np.where(m, crop, 255).astype(np.uint8)     # keep ink greys, whiten background
        origs.append(crop.astype(np.uint8)); cleans.append(cleaned); sigs.append(l["sig"])
    np.savez(f"{SPOT}/clean_crops.npz", orig=np.array(origs, object),
             clean=np.array(cleans, object), sigs=np.array(sigs, object))
    print(f"saved {len(origs)} orig+clean crops -> {SPOT}/clean_crops.npz\nCLEANCROPSDONE", flush=True)

if __name__ == "__main__":
    main()
