"""RETHINK probe (MapReader half) — does the spotter's ViTAEv2 DETECTION BACKBONE give a font-STYLE embedding
that separates the 16 typographic signatures, where raw-raster (LOO 0.19) and within-word contrastive SSL
(0.14) both failed? The recognition head is font-invariant by design, but the backbone is trained to FIND text
on maps, so its feature maps should suppress the map-linework/clutter that wrecked unsupervised clustering.

For each human-labelled word we crop its map region, run the spotter's forward pass, capture the backbone
feature dict via a forward hook, adaptive-avg-pool each FPN level over the whole word crop -> one descriptor
per level (and a concat), then score leave-one-out same-SIGNATURE and same-STYLE kNN on the words. Directly
comparable to the raster/SSL numbers from cluster_probe.

    sbatch -M gpu --account=ishi backbone_probe.sbatch      # ~2 min, 38 crops
"""
import json, os, base64, io, numpy as np, torch
from collections import Counter, defaultdict
from PIL import Image
import pandas as pd

HERE = "/vast/ishi/gb1900/probe/font"
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
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
pred = runner.predictor
model = pred.model; model.eval()

feat = {}
def _hook(m, i, o): feat["o"] = o
# The detector is DETR-style: model.detection_transformer.backbone = Joiner(MaskedBackbone, pos_embed);
# MaskedBackbone.backbone is the ViTAEv2 whose forward returns the raw multi-scale feature dict. Auto-locate
# it as the deepest module named '…backbone' (raw FPN features, before mask wrapping).
cand = [(n, m) for n, m in model.named_modules() if n.endswith("backbone")]
print("backbone-like modules:", [n for n, _ in cand], flush=True)
tgt_name, tgt = max(cand, key=lambda nm: nm[0].count("."))
print(f"hooking: {tgt_name} ({type(tgt).__name__})", flush=True)
tgt.register_forward_hook(_hook)                                 # capture ViTAEv2 feature dict

import cv2
SAFE = 512                                                       # fixed square (÷64) — safe for ViTAE window/reduction tiling
input_format = pred.input_format
def backbone_levels(crop_rgb):
    """Feed a word crop as a fixed 512² square straight to the model (bypassing the cfg's 2000px aug, which
    blows tiny crops to extreme aspect ratios that break the ViTAE tiling); return {level: pooled-vec}."""
    h, w = crop_rgb.shape[:2]; m = max(h, w)
    sq = np.full((m, m, 3), 255, np.uint8); sq[:h, :w] = crop_rgb          # square-pad (keeps aspect)
    im = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)
    arr = im[:, :, ::-1] if input_format == "BGR" else im                  # to model's expected channel order
    t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
    feat.clear()
    try:
        with torch.no_grad():
            model([{"image": t, "height": SAFE, "width": SAFE}])           # fires backbone hook; heads may error after
    except Exception:
        pass
    if "o" not in feat:
        return {}
    o = feat["o"]
    if isinstance(o, dict):        items = list(o.items())
    elif isinstance(o, (list, tuple)): items = [(str(i), t) for i, t in enumerate(o)]
    else:                          items = [("feat", o)]
    lv = {}
    for k, t in items:
        if hasattr(t, "tensors"): t = t.tensors          # unwrap NestedTensor if present
        if not torch.is_tensor(t) or t.dim() != 4: continue
        lv[k] = torch.nn.functional.adaptive_avg_pool2d(t, 1).flatten().float().cpu().numpy()
    return lv

words = [w for w in json.load(open(f"{HERE}/labels/alphabet_labels_all.json")) if w.get("face")]
per_level = defaultdict(list); sigs = []; styles = []; kept = 0; struct = None
for w in words:
    img = np.asarray(Image.open(io.BytesIO(base64.b64decode(w["img"]))).convert("RGB"), np.uint8)
    L = w["letters"]
    x0 = max(0, min(l["x"] for l in L) - 4); y0 = max(0, min(l["y"] for l in L) - 4)
    x1 = min(img.shape[1], max(l["x"] + l["w"] for l in L) + 4)
    y1 = min(img.shape[0], max(l["y"] + l["h"] for l in L) + 4)
    crop = img[y0:y1, x0:x1]
    if crop.shape[0] < 8 or crop.shape[1] < 8: continue
    lv = backbone_levels(crop)
    if not lv:
        continue
    if struct is None:
        struct = {k: v.shape[0] for k, v in lv.items()}
        print("backbone levels -> dims:", struct, flush=True)
    for k, v in lv.items(): per_level[k].append(v)
    sigs.append(sig(w["face"])); styles.append(style(w["face"])); kept += 1

sigs = np.array(sigs); styles = np.array(styles)
print(f"{kept} labelled words | {len(set(sigs))} signatures | {len(set(styles))} styles", flush=True)
print(f"signature chance {max(Counter(sigs).values())/len(sigs):.2f} | style chance {max(Counter(styles).values())/len(styles):.2f}")

def loo_knn(emb, lab, k=5):
    emb = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9)
    S = emb @ emb.T; np.fill_diagonal(S, -2); ok = 0
    for i in range(len(emb)):
        nn = np.argsort(-S[i])[:k]
        ok += (Counter(lab[j] for j in nn).most_common(1)[0][0] == lab[i])
    return ok / max(1, len(emb))

levels = list(per_level.keys())
mats = {k: np.array(per_level[k], np.float32) for k in levels}
concat = np.concatenate([mats[k] for k in levels], axis=1)
print("\n=== LOO kNN (k=5) — MapReader ViTAEv2 backbone ===")
print(f"{'level':<10} {'dim':>6}   sig   style")
for k in levels:
    print(f"{k:<10} {mats[k].shape[1]:>6}   {loo_knn(mats[k], sigs):.3f}  {loo_knn(mats[k], styles):.3f}")
print(f"{'CONCAT':<10} {concat.shape[1]:>6}   {loo_knn(concat, sigs):.3f}  {loo_knn(concat, styles):.3f}")
print("\nbaselines for comparison: raster sig 0.19 / SSL sig 0.14  (cluster_probe)")
np.savez_compressed(f"{HERE}/labels/backbone_desc.npz",
                    concat=concat, sigs=sigs, styles=styles, **{f"lvl_{k}": mats[k] for k in levels})
print(f"wrote {HERE}/labels/backbone_desc.npz")
