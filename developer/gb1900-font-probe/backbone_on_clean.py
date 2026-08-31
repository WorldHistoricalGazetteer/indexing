"""Stage 2: MapReader ViTAEv2 backbone descriptor on ORIGINAL vs Hi-SAM-CLEANED crops (same 189 anchors).
The ORIGINAL column re-validates the 0.63 baseline (control); CLEANED tests whether noise-free ink lifts the
best descriptor; FUSED concatenates both. Reads clean_crops.npz from Stage 1. Runs in the mapreader env, GPU.

    python backbone_on_clean.py
"""
import os, numpy as np, torch, cv2
import pandas as pd
from collections import Counter
HERE = "/vast/ishi/gb1900/probe/font"; INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
SPOT = "/vast/ishi/gb1900/edition/spot"; SAFE = 512

from mapreader import MapTextRunner
dev = "cuda" if torch.cuda.is_available() else "cpu"
runner = MapTextRunner(pd.DataFrame(), cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
    weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
pred = runner.predictor; model = pred.model; model.eval(); input_format = pred.input_format
feat = {}
def _hook(m, i, o): feat["o"] = o
tgt_name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")], key=lambda nm: nm[0].count("."))
tgt.register_forward_hook(_hook)

def concat(crop_gray):
    m = max(crop_gray.shape[:2]); sq = np.full((m, m), 255, np.uint8); sq[:crop_gray.shape[0], :crop_gray.shape[1]] = crop_gray
    g = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA); im = np.repeat(g[:, :, None], 3, 2)
    arr = im[:, :, ::-1] if input_format == "BGR" else im
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
    return np.concatenate(per) if per else np.zeros(896, np.float32)

def norm(X): X = X.astype(np.float32); return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
def loo_maxsim(X, S):
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

d = np.load(f"{SPOT}/clean_crops.npz", allow_pickle=True)
orig, clean, sigs = d["orig"], d["clean"], d["sigs"].astype(str)
print(f"{len(orig)} crops loaded", flush=True)
Do = np.array([concat(c) for c in orig]); Dc = np.array([concat(c) for c in clean])
print(f"\nViTAEv2 backbone, {len(sigs)} anchors / {len(set(sigs))} sigs (majority {max(Counter(sigs).values())/len(sigs):.2f})")
print(f"  ORIGINAL crops:   maxsim-LOO {loo_maxsim(Do, sigs):.3f}  kNN5 {loo_knn(Do, sigs):.3f}   (control ~0.63)")
print(f"  Hi-SAM CLEANED:   maxsim-LOO {loo_maxsim(Dc, sigs):.3f}  kNN5 {loo_knn(Dc, sigs):.3f}")
F = np.concatenate([norm(Do), norm(Dc)], 1)
print(f"  FUSED orig+clean: maxsim-LOO {loo_maxsim(F, sigs):.3f}  kNN5 {loo_knn(F, sigs):.3f}")
print("BACKBONECLEANDONE", flush=True)
