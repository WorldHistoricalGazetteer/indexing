"""Does the MapReader ViTAEv2 backbone descriptor separate signatures BETTER when we control for letter
identity? The word-level descriptor (concat stage3-5, LOO sig 0.42) pools all letters, so word CONTENT leaks
in. Here we tight-crop each LETTER (human letter boxes), upscale to 512², run the frozen backbone, and do a
same-letter, leave-one-WORD-out kNN: a glyph is classified by its nearest glyphs OF THE SAME CHARACTER from
OTHER words — isolating typographic style from which letters the word happens to contain. Compares to the
raster same-letter kNN (0.35) and the word-level backbone (0.42).

    sbatch same_letter_backbone.sbatch      # a100, ~2 min (~230 letter forwards)
"""
import json, os, base64, io, math, numpy as np, torch, cv2
from collections import Counter, defaultdict
from PIL import Image
import pandas as pd

HERE = "/vast/ishi/gb1900/probe/font"; INST = "/vast/ishi/gb1900/probe/mapreader_text/install"; SAFE = 512
TAX = {f["key"]: (f.get("base_style"), f.get("fill"), f.get("decor")) for f in json.load(open(f"{HERE}/font_taxonomy.json"))}
def sig(face):   return "·".join(str(x) for x in TAX.get(face, (face, "", "")))
def style(face): return str(TAX.get(face, (face, "", ""))[0])

from mapreader import MapTextRunner
dev = "cuda" if torch.cuda.is_available() else "cpu"
print(f"device {dev}; loading spotter…", flush=True)
runner = MapTextRunner(pd.DataFrame(), cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
    weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
pred = runner.predictor; model = pred.model; model.eval(); input_format = pred.input_format
feat = {}
def _hook(m, i, o): feat["o"] = o
tgt_name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")], key=lambda nm: nm[0].count("."))
tgt.register_forward_hook(_hook); print(f"hooking {tgt_name}", flush=True)

def backbone_concat(im_rgb):
    arr = im_rgb[:, :, ::-1] if input_format == "BGR" else im_rgb
    t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
    feat.clear()
    try:
        with torch.no_grad(): model([{"image": t, "height": SAFE, "width": SAFE}])
    except Exception: pass
    o = feat.get("o", {}); vecs = []
    for k in sorted(o) if isinstance(o, dict) else []:
        v = o[k]
        if hasattr(v, "tensors"): v = v.tensors
        if torch.is_tensor(v) and v.dim() == 4: vecs.append(v[0].mean(dim=(1, 2)).float().cpu().numpy())
    return np.concatenate(vecs) if vecs else None

def tangent_angles(L):                                    # local baseline tangent per letter (de-rotate curved labels)
    c = [(l["x"] + l["w"] / 2.0, l["y"] + l["h"] / 2.0) for l in L]; n = len(c); ang = []
    for i in range(n):
        if n == 1: ang.append(0.0); continue
        j0, j1 = max(0, i - 1), min(n - 1, i + 1); dx, dy = c[j1][0] - c[j0][0], c[j1][1] - c[j0][1]
        ang.append(math.degrees(math.atan2(dy, dx)) if (dx or dy) else 0.0)
    return ang

def letter_512(snip, lt, ang):
    x, y, w, h = lt["x"], lt["y"], lt["w"], lt["h"]; p = 3
    crop = snip[max(0, y - p):y + h + p, max(0, x - p):x + w + p]
    if crop.shape[0] < 4 or crop.shape[1] < 4: return None
    if abs(ang) >= 3.0:                                   # upright the letter
        d = int(math.hypot(*crop.shape[:2])) + 4; cv = np.full((d, d, 3), 255, np.uint8)
        oy, ox = (d - crop.shape[0]) // 2, (d - crop.shape[1]) // 2; cv[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
        M = cv2.getRotationMatrix2D((d / 2, d / 2), ang, 1.0); crop = cv2.warpAffine(cv, M, (d, d), borderValue=(255, 255, 255))
    m = max(crop.shape[:2]); sq = np.full((m, m, 3), 255, np.uint8); sq[:crop.shape[0], :crop.shape[1]] = crop
    return cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)

words = [w for w in json.load(open(f"{HERE}/labels/alphabet_labels_all.json")) if w.get("face")]
descs, chars, sigs, styles, wids = [], [], [], [], []
for wi, w in enumerate(words):
    snip = np.asarray(Image.open(io.BytesIO(base64.b64decode(w["img"]))).convert("RGB"), np.uint8)
    ang = tangent_angles(w["letters"])
    for li, lt in enumerate(w["letters"]):
        im = letter_512(snip, lt, ang[li])
        if im is None: continue
        d = backbone_concat(im)
        if d is None: continue
        descs.append(d); chars.append(str(lt.get("char", "?"))); wids.append(wi)
        sigs.append(sig(w["face"])); styles.append(style(w["face"]))
X = np.array(descs, np.float32); X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
chars = np.array(chars); wids = np.array(wids); sigs = np.array(sigs); styles = np.array(styles)
print(f"{len(X)} letter-glyphs from {len(set(wids))} words | {len(set(sigs))} sigs (chance {max(Counter(sigs).values())/len(sigs):.2f})", flush=True)

def same_letter_knn(emb, lab, k=5):
    """Leave-one-WORD-out: neighbours restricted to the SAME character from OTHER words."""
    S = emb @ emb.T; ok = tot = 0
    for i in range(len(emb)):
        mask = (chars == chars[i]) & (wids != wids[i])
        if mask.sum() == 0: continue
        idx = np.where(mask)[0]; nn = idx[np.argsort(-S[i, idx])[:k]]
        ok += (Counter(lab[j] for j in nn).most_common(1)[0][0] == lab[i]); tot += 1
    return ok / max(1, tot), tot

def loo_knn(emb, lab, k=5):                               # plain glyph kNN (any letter), leave-one-word-out
    S = emb @ emb.T; ok = 0
    for i in range(len(emb)):
        mask = wids != wids[i]; idx = np.where(mask)[0]; nn = idx[np.argsort(-S[i, idx])[:k]]
        ok += (Counter(lab[j] for j in nn).most_common(1)[0][0] == lab[i])
    return ok / len(emb)

for k in (3, 5):
    sl_s, tot = same_letter_knn(X, sigs, k); sl_st, _ = same_letter_knn(X, styles, k)
    print(f"  k={k} SAME-LETTER kNN (n={tot}): sig {sl_s:.3f} | style {sl_st:.3f}")
    print(f"  k={k} any-glyph   kNN:          sig {loo_knn(X,sigs,k):.3f} | style {loo_knn(X,styles,k):.3f}")
print("\nbaselines: raster same-letter sig 0.35 | word-level backbone sig 0.42 | raster/SSL 0.19/0.14")
np.savez_compressed(f"{HERE}/labels/backbone_letter_desc.npz", desc=X, chars=chars, wids=wids, sigs=sigs, styles=styles)
print(f"wrote {HERE}/labels/backbone_letter_desc.npz\nSLBBDONE")
