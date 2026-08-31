"""Does Hi-SAM improve FONT DISCRIMINATION (the real gap)? For each of the 189 labelled anchor words, run
Hi-SAM on its de-rotated crop and extract two candidate signals:
  (a) CLEAN STROKE RASTER — Hi-SAM's pixel text-foreground mask (modal_aligner -> mask_decoder -> thresholded
      high_res_mask): the ink isolated from the map background, i.e. the noise-free glyph input we never had
      (segmentation noise was what killed unsupervised clustering + forced the line-erase hack).
  (b) SAM ViT-L ENCODER FEATURE — global-pooled amg.features: a fresh appearance descriptor vs MapReader's
      ViTAEv2 backbone (which gave imbalance-robust maxsim-LOO 0.63 on these same anchors).
Re-measure signature separation (imbalance-robust per-class-max-sim LOO + plain kNN) and compare to baselines.

    python hisam_font_probe.py --weight weights/hi_sam_l.pth --model-type vit_l
"""
import argparse, os, glob, json, sys, numpy as np, cv2
sys.path.insert(0, "/vast/ishi/gb1900/probe/font")              # make_font_testset_v2.derotate (tile crop)
REPO = "/vast/ishi/gb1900/probe/hisam/Hi-SAM"; sys.path.insert(0, REPO)
os.chdir(REPO)                                                  # build reads pretrained_checkpoint/ relative
import torch
from types import SimpleNamespace
from collections import Counter
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
def extract(amg, model, crop_gray):
    rgb = np.repeat(crop_gray[:, :, None], 3, 2)               # OS maps ~monochrome; 3ch for SAM
    amg.set_image(rgb)
    feat = amg.features                                        # [1,256,64,64] SAM encoding
    samvec = feat.mean(dim=(2, 3)).squeeze(0).float().cpu().numpy()
    sparse = model.modal_aligner(feat)
    low, high, _, _ = model.mask_decoder(image_embeddings=feat, image_pe=model.prompt_encoder.get_dense_pe(),
                                         sparse_prompt_embeddings=sparse, multimask_output=False)
    fg = (high > model.mask_threshold).squeeze(1)[0].cpu().numpy().astype(np.uint8)   # (1024,1024) clean ink
    h, w = crop_gray.shape[:2]; s = 1024.0 / max(h, w); rh, rw = max(1, int(h * s)), max(1, int(w * s))
    m = fg[:rh, :rw]; ys, xs = np.where(m > 0)                 # crop occupies top-left of the padded 1024
    if len(xs) < 6: return samvec, None
    g = (m[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255).astype(np.uint8)
    return samvec, cv2.resize(g, (36, 44), interpolation=cv2.INTER_AREA)

def norm(X): X = X.astype(np.float32); return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
def loo_maxsim(X, S):                                          # imbalance-robust: nearest per-class prototype
    X = norm(X); sl = sorted(set(S)); cols = {s: np.where(S == s)[0] for s in sl}
    sim = X @ X.T; np.fill_diagonal(sim, -2); ok = 0
    for i in range(len(X)):
        sc = [sim[i, cols[s][cols[s] != i]].max() if len(cols[s][cols[s] != i]) else -9 for s in sl]
        ok += (sl[int(np.argmax(sc))] == S[i])
    return ok / len(X)
def loo_knn(X, S, k=5):
    X = norm(X); sim = X @ X.T; np.fill_diagonal(sim, -2); ok = 0
    for i in range(len(X)):
        nn = np.argsort(-sim[i])[:k]; ok += (Counter(S[j] for j in nn).most_common(1)[0][0] == S[i])
    return ok / len(X)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--weight", default="weights/hi_sam_l.pth")
    ap.add_argument("--model-type", default="vit_l"); a = ap.parse_args()
    print(f"building {a.model_type}", flush=True); model, amg = build(a.model_type, a.weight)
    lab = [l for l in json.load(open(f"{HERE}/labels/pool_labels.json")) if l.get("sig")]
    want = {key(l["gcx"], l["gcy"]) for l in lab}; rec = {}
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(f):
            try: r = json.loads(line)
            except Exception: continue
            k = key(r["gcx"], r["gcy"])
            if k in want and k not in rec: rec[k] = r
        if len(rec) >= len(want): break
    print(f"{len(lab)} anchors, {len(rec)} matched to boxes", flush=True)
    sams, rasters, sigs, miss, nostroke = [], [], [], 0, 0
    for l in lab:
        r = rec.get(key(l["gcx"], l["gcy"]))
        if r is None: miss += 1; continue
        crop = derotate(r)
        if crop is None or crop.size < 80: miss += 1; continue
        sv, rast = extract(amg, model, crop)
        if rast is None: nostroke += 1; rast = np.zeros((44, 36), np.uint8)
        sams.append(sv); rasters.append(rast); sigs.append(l["sig"])
    sigs = np.array(sigs)
    print(f"got {len(sams)} (tile-miss {miss}, empty-stroke {nostroke})", flush=True)
    SAM = np.array(sams); RASTb = np.array([(r.flatten() > 128).astype(np.float32) for r in rasters])
    print(f"\n{len(SAM)} anchors / {len(set(sigs))} sigs (majority {max(Counter(sigs).values())/len(sigs):.2f})")
    print(f"  CLEAN stroke raster:  maxsim-LOO {loo_maxsim(RASTb, sigs):.3f}  kNN5 {loo_knn(RASTb, sigs):.3f}")
    print(f"  SAM ViT-L feature:    maxsim-LOO {loo_maxsim(SAM, sigs):.3f}  kNN5 {loo_knn(SAM, sigs):.3f}")
    print("  baselines (same anchors): MapReader ViTAEv2 backbone maxsim-LOO 0.63 | noisy raster ~0.19")
    np.savez_compressed(f"{SPOT}/hisam_font_desc.npz", sam=SAM, raster=RASTb, sigs=sigs.astype(object))
    print("HFPROBEDONE", flush=True)

if __name__ == "__main__":
    main()
