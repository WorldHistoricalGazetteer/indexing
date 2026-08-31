"""Step-1 refinement of the MapReader-backbone font-style embedding (mapreader_backbone_probe.py established
frozen ViTAEv2 features beat hand-built ones: concat LOO sig 0.42 vs raster 0.19). Two changes:

  1. ROI-ALIGN, not global-pool. Feed a CONTEXT window (the word box expanded by a margin, i.e. real map
     around the word) through the backbone, then avg-pool each ViTAE stage feature map ONLY over the word's
     sub-rectangle. This (a) puts map context in the receptive field, (b) drops the white padding / neighbour
     ink the isolated-square global-pool included, and (c) MIRRORS DEPLOYMENT — during the full-GB spot we
     hook the same backbone and ROI-pool each detected box on the mosaic feature map, so a per-word style
     descriptor is ~free for all ~2.67M labels.
  2. Honest readout. LOO same-signature/style kNN (comparable to the isolated-square baseline) PLUS a
     PCA→kNN readout, swept over context margins. (38 labels / 9 sigs is p>>n, so a linear head would just
     overfit — kNN on the frozen embedding IS the readout at this data scale; PCA denoises it.)

    sbatch backbone_readout.sbatch          # a100, ~3 min
"""
import json, os, base64, io, numpy as np, torch, cv2
from collections import Counter, defaultdict
from PIL import Image
import pandas as pd

HERE = "/vast/ishi/gb1900/probe/font"
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
SAFE = 512
MARGINS = [0.3, 0.75, 1.5]                        # context = word box ± margin·(box size) per side
TAX = {f["key"]: (f.get("base_style"), f.get("fill"), f.get("decor"))
       for f in json.load(open(f"{HERE}/font_taxonomy.json"))}
def sig(face):   return "·".join(str(x) for x in TAX.get(face, (face, "", "")))
def style(face): return str(TAX.get(face, (face, "", ""))[0])

from mapreader import MapTextRunner
dev = "cuda" if torch.cuda.is_available() else "cpu"
print(f"device {dev}; loading spotter…", flush=True)
runner = MapTextRunner(pd.DataFrame(),
    cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
    weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
pred = runner.predictor; model = pred.model; model.eval()
input_format = pred.input_format

feat = {}
def _hook(m, i, o): feat["o"] = o
cand = [(n, m) for n, m in model.named_modules() if n.endswith("backbone")]
tgt_name, tgt = max(cand, key=lambda nm: nm[0].count("."))
print(f"hooking {tgt_name} ({type(tgt).__name__})", flush=True)
tgt.register_forward_hook(_hook)

def run_backbone(im_rgb):
    """Feed a 512² RGB image, return {stage: feature-map tensor [C,h,w]}."""
    arr = im_rgb[:, :, ::-1] if input_format == "BGR" else im_rgb
    t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
    feat.clear()
    try:
        with torch.no_grad(): model([{"image": t, "height": SAFE, "width": SAFE}])
    except Exception: pass
    o = feat.get("o", {})
    out = {}
    items = o.items() if isinstance(o, dict) else []
    for k, v in items:
        if hasattr(v, "tensors"): v = v.tensors
        if torch.is_tensor(v) and v.dim() == 4: out[k] = v[0]      # [C,h,w]
    return out

def context_input(snip, box, margin):
    """Crop the word box expanded by `margin`, square-pad, resize to 512²; return (img512, word-box-in-512)."""
    x0, y0, x1, y1 = box; bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    ex0 = max(0, int(x0 - margin * bw)); ey0 = max(0, int(y0 - margin * bh))
    ex1 = min(snip.shape[1], int(x1 + margin * bw)); ey1 = min(snip.shape[0], int(y1 + margin * bh))
    ctx = snip[ey0:ey1, ex0:ex1]
    if ctx.shape[0] < 4 or ctx.shape[1] < 4: return None, None
    m = max(ctx.shape[:2]); sq = np.full((m, m, 3), 255, np.uint8); sq[:ctx.shape[0], :ctx.shape[1]] = ctx
    im = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)
    s = SAFE / m
    wb = ((x0 - ex0) * s, (y0 - ey0) * s, (x1 - ex0) * s, (y1 - ey0) * s)
    return im, wb

def roi_pool(fmap, wb):
    """Avg-pool a [C,h,w] feature map over the word sub-rectangle wb (in 512² coords)."""
    C, h, w = fmap.shape; sx, sy = w / SAFE, h / SAFE
    fx0 = max(0, int(np.floor(wb[0] * sx))); fy0 = max(0, int(np.floor(wb[1] * sy)))
    fx1 = min(w, max(fx0 + 1, int(np.ceil(wb[2] * sx)))); fy1 = min(h, max(fy0 + 1, int(np.ceil(wb[3] * sy))))
    return fmap[:, fy0:fy1, fx0:fx1].mean(dim=(1, 2)).float().cpu().numpy()

def gpool(fmap): return fmap.mean(dim=(1, 2)).float().cpu().numpy()

words = [w for w in json.load(open(f"{HERE}/labels/alphabet_labels_all.json")) if w.get("face")]
def crop_box(w):
    L = w["letters"]
    return (min(l["x"] for l in L), min(l["y"] for l in L),
            max(l["x"] + l["w"] for l in L), max(l["y"] + l["h"] for l in L))

def loo_knn(emb, lab, k=5):
    emb = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9)
    S = emb @ emb.T; np.fill_diagonal(S, -2); ok = 0
    for i in range(len(emb)):
        nn = np.argsort(-S[i])[:k]
        ok += (Counter(lab[j] for j in nn).most_common(1)[0][0] == lab[i])
    return ok / max(1, len(emb))

def pca_knn(emb, sigs, styles, d=16):
    from sklearn.decomposition import PCA
    Xc = emb - emb.mean(0)
    Z = PCA(n_components=min(d, Xc.shape[0] - 1, Xc.shape[1])).fit_transform(Xc)
    return loo_knn(Z, sigs), loo_knn(Z, styles)

# collect per-word feature maps once per margin (backbone re-run per margin since the input window differs)
results = {}
for margin in MARGINS:
    roi_lv = defaultdict(list); glob_lv = defaultdict(list); sigs = []; styles = []
    for w in words:
        snip = np.asarray(Image.open(io.BytesIO(base64.b64decode(w["img"]))).convert("RGB"), np.uint8)
        im, wb = context_input(snip, crop_box(w), margin)
        if im is None: continue
        fm = run_backbone(im)
        if not fm: continue
        for k, t in fm.items():
            roi_lv[k].append(roi_pool(t, wb)); glob_lv[k].append(gpool(t))
        sigs.append(sig(w["face"])); styles.append(style(w["face"]))
    sigs = np.array(sigs); styles = np.array(styles)
    levels = list(roi_lv.keys())
    roi_cat = np.concatenate([np.array(roi_lv[k]) for k in levels], 1)
    glob_cat = np.concatenate([np.array(glob_lv[k]) for k in levels], 1)
    results[margin] = (levels, roi_lv, glob_lv, roi_cat, glob_cat, sigs, styles)

lv0 = results[MARGINS[0]][0]
n = len(results[MARGINS[0]][5])
print(f"\n{n} words | {len(set(results[MARGINS[0]][5]))} sigs (chance "
      f"{max(Counter(results[MARGINS[0]][5]).values())/n:.2f}) | stages {lv0}")
print("\n=== LOO kNN(5) sig / style — ROI-align vs global-pool, by context margin ===")
print(f"{'margin':>6}  {'ROI-concat':>18}  {'glob-concat':>18}  {'ROI-stage5':>14}  {'PCA16 ROI':>16}")
for margin in MARGINS:
    levels, roi_lv, glob_lv, roi_cat, glob_cat, sigs, styles = results[margin]
    s5 = [k for k in levels if "5" in k]
    roi5 = np.array(roi_lv[s5[0]]) if s5 else roi_cat
    pca_s, pca_st = pca_knn(roi_cat, sigs, styles)
    print(f"{margin:>6.2f}  {loo_knn(roi_cat,sigs):.3f}/{loo_knn(roi_cat,styles):.3f}       "
          f"{loo_knn(glob_cat,sigs):.3f}/{loo_knn(glob_cat,styles):.3f}       "
          f"{loo_knn(roi5,sigs):.3f}/{loo_knn(roi5,styles):.3f}     {pca_s:.3f}/{pca_st:.3f}")
print("\nbaseline (isolated 512² global-pool): concat sig 0.42 / style 0.66  |  raster 0.19 / SSL 0.14")

# save the best-margin ROI descriptors for downstream use
best = max(MARGINS, key=lambda mg: loo_knn(results[mg][3], results[mg][5]))
levels, roi_lv, glob_lv, roi_cat, glob_cat, sigs, styles = results[best]
np.savez_compressed(f"{HERE}/labels/backbone_roi_desc.npz",
                    roi_concat=roi_cat, sigs=sigs, styles=styles, margin=best,
                    **{f"roi_{k}": np.array(roi_lv[k]) for k in levels})
print(f"\nwrote {HERE}/labels/backbone_roi_desc.npz (best margin {best})")
print("BBREADOUTDONE")
