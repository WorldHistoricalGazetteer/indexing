"""Re-embed the 38 human-labelled anchor words through the SAME preprocessing as the pool descriptor bank
(grayscale word crop -> derotate to horizontal -> 512² -> frozen ViTAEv2 -> concat stage3-5), so anchor↔pool
kNN in the active-learning UI is meaningful (the earlier anchor_desc.npz used RGB snippet crops = a different
space). Emits anchor_desc_pool.npz {desc, face, sig, style, text} + reports LOO kNN to confirm the 0.42-ish
signal survives the grayscale-pool pipeline.

    sbatch anchor_descriptors.sbatch     # a100, ~1 min
"""
import json, os, base64, io, math, numpy as np, torch, cv2
from collections import Counter
from PIL import Image
import pandas as pd

HERE = "/vast/ishi/gb1900/probe/font"; INST = "/vast/ishi/gb1900/probe/mapreader_text/install"; SAFE = 512
TAX = {f["key"]: (f.get("base_style"), f.get("fill"), f.get("decor")) for f in json.load(open(f"{HERE}/font_taxonomy.json"))}
def sig(face):   return "·".join(str(x) for x in TAX.get(face, (face, "", "")))
def style(face): return str(TAX.get(face, (face, "", ""))[0])

from mapreader import MapTextRunner
dev = "cuda" if torch.cuda.is_available() else "cpu"
runner = MapTextRunner(pd.DataFrame(), cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
    weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
pred = runner.predictor; model = pred.model; model.eval(); input_format = pred.input_format
feat = {}
def _hook(m, i, o): feat["o"] = o
tgt_name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")], key=lambda nm: nm[0].count("."))
tgt.register_forward_hook(_hook)

def backbone_concat(im3):
    arr = im3[:, :, ::-1] if input_format == "BGR" else im3
    t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
    feat.clear()
    try:
        with torch.no_grad(): model([{"image": t, "height": SAFE, "width": SAFE}])
    except Exception: pass
    o = feat.get("o", {}); per = []
    for k in sorted(o) if isinstance(o, dict) else []:
        v = o[k]
        if hasattr(v, "tensors"): v = v.tensors
        if torch.is_tensor(v) and v.dim() == 4: per.append(v[0].mean(dim=(1, 2)).float().cpu().numpy())
    return np.concatenate(per) if per else None

def word_crop_gray(snip, L):
    """grayscale word crop (letter-box union), de-rotated to horizontal by the first→last letter angle."""
    x0 = min(l["x"] for l in L) - 4; y0 = min(l["y"] for l in L) - 4
    x1 = max(l["x"] + l["w"] for l in L) + 4; y1 = max(l["y"] + l["h"] for l in L) + 4
    x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(snip.shape[1], x1), min(snip.shape[0], y1)
    crop = snip[y0:y1, x0:x1]
    if crop.shape[0] < 6 or crop.shape[1] < 6: return None
    c = [(l["x"] + l["w"] / 2.0, l["y"] + l["h"] / 2.0) for l in L]
    ang = math.degrees(math.atan2(c[-1][1] - c[0][1], c[-1][0] - c[0][0])) if len(c) > 1 else 0.0
    if abs(ang) >= 3.0:
        d = int(math.hypot(*crop.shape[:2])) + 4; cv = np.full((d, d), 255, np.uint8)
        oy, ox = (d - crop.shape[0]) // 2, (d - crop.shape[1]) // 2; cv[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
        M = cv2.getRotationMatrix2D((d / 2, d / 2), ang, 1.0); crop = cv2.warpAffine(cv, M, (d, d), borderValue=255)
    m = max(crop.shape[:2]); sq = np.full((m, m), 255, np.uint8); sq[:crop.shape[0], :crop.shape[1]] = crop
    g = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)
    return np.repeat(g[:, :, None], 3, 2)

words = [w for w in json.load(open(f"{HERE}/labels/alphabet_labels_all.json")) if w.get("face")]
descs, faces = [], []
for w in words:
    snip = np.asarray(Image.open(io.BytesIO(base64.b64decode(w["img"]))).convert("L"), np.uint8)
    im = word_crop_gray(snip, w["letters"])
    if im is None: continue
    d = backbone_concat(im)
    if d is None: continue
    descs.append(d.astype(np.float16)); faces.append(w["face"])
X = np.array(descs, np.float32); Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
faces = np.array(faces); sigs = np.array([sig(f) for f in faces]); styles = np.array([style(f) for f in faces])
print(f"{len(X)} anchors | {len(set(sigs))} sigs (chance {max(Counter(sigs).values())/len(sigs):.2f})")

def loo(emb, lab, k=5):
    S = emb @ emb.T; np.fill_diagonal(S, -2); ok = 0
    for i in range(len(emb)):
        nn = np.argsort(-S[i])[:k]; ok += (Counter(lab[j] for j in nn).most_common(1)[0][0] == lab[i])
    return ok / len(emb)
print(f"grayscale-pool space LOO kNN(5): sig {loo(Xn,sigs):.3f} | style {loo(Xn,styles):.3f}   (RGB-snippet baseline was sig 0.42)")
np.savez_compressed(f"{HERE}/labels/anchor_desc_pool.npz",
    desc=np.array(descs), face=faces.astype(object), sig=sigs.astype(object), style=styles.astype(object),
    text=np.array([w.get("trans", "") for w in words[:len(faces)]], object))
print(f"wrote {HERE}/labels/anchor_desc_pool.npz\nANCHORDONE")
