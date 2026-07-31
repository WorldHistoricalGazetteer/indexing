"""Phase B stage 2 — the pipeline's own discriminator number, on Hi-SAM crops.

Runs the frozen MapReader ViTAEv2 backbone (the validated font-style descriptor) over the three crop columns
from anchor_recrop.py and reports imbalance-robust maxsim-LOO plus plain kNN5 for each. The `mr` column is the
control: if it does not land near 0.63 then something other than the box convention has changed and the other
two columns mean nothing.

Two stages because the SAM and MapReader models live in separate conda envs; npz is the handoff (no pyarrow).

    python anchor_recrop_readout.py     # mapreader env, GPU, ~2 min
"""
import argparse, numpy as np, torch, cv2
import pandas as pd
from collections import Counter

INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
SPOT = "/vast/ishi/gb1900/edition/spot"
SAFE = 512

from mapreader import MapTextRunner

dev = "cuda" if torch.cuda.is_available() else "cpu"
runner = MapTextRunner(pd.DataFrame(),
                       cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
                       weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
pred = runner.predictor
model = pred.model
model.eval()
input_format = pred.input_format
feat = {}


def _hook(m, i, o):
    feat["o"] = o


# deepest module whose name ends in "backbone" == detection_transformer.backbone.0.backbone
tgt_name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")],
                    key=lambda nm: nm[0].count("."))
tgt.register_forward_hook(_hook)


def concat(crop_gray):
    """896-d descriptor: pad to square on white, 512², 3-channel, concat mean-pooled stages 3/4/5."""
    m = max(crop_gray.shape[:2])
    sq = np.full((m, m), 255, np.uint8)
    sq[:crop_gray.shape[0], :crop_gray.shape[1]] = crop_gray
    g = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)
    im = np.repeat(g[:, :, None], 3, 2)
    arr = im[:, :, ::-1] if input_format == "BGR" else im
    t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
    feat.clear()
    try:
        with torch.no_grad():
            model([{"image": t, "height": SAFE, "width": SAFE}])
    except Exception:
        pass
    o = feat.get("o", {})
    per = []
    for k in sorted(o) if isinstance(o, dict) else []:
        v = o[k]
        if hasattr(v, "tensors"):
            v = v.tensors
        if torch.is_tensor(v) and v.dim() == 4:
            per.append(v[0].mean(dim=(1, 2)).float().cpu().numpy())
    return np.concatenate(per) if per else np.zeros(896, np.float32)


def norm(X):
    X = X.astype(np.float32)
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)


def loo_maxsim(X, S):
    """Nearest per-class prototype — imbalance-robust, so a 0.47 majority class can't carry the score."""
    X = norm(X)
    sl = sorted(set(S))
    cols = {s: np.where(S == s)[0] for s in sl}
    sim = X @ X.T
    np.fill_diagonal(sim, -2)
    ok = 0
    for i in range(len(X)):
        sc = [sim[i, cols[s][cols[s] != i]].max() if len(cols[s][cols[s] != i]) else -9 for s in sl]
        ok += (sl[int(np.argmax(sc))] == S[i])
    return ok / len(X)


def loo_knn(X, S, k=5):
    X = norm(X)
    sim = X @ X.T
    np.fill_diagonal(sim, -2)
    ok = 0
    for i in range(len(X)):
        nn = np.argsort(-sim[i])[:k]
        ok += (Counter(S[j] for j in nn).most_common(1)[0][0] == S[i])
    return ok / len(X)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crops", default=f"{SPOT}/anchor_crops_hisam.npz")
    ap.add_argument("--out", default=f"{SPOT}/anchor_desc_hisam.npz")
    a = ap.parse_args()

    d = np.load(a.crops, allow_pickle=True)
    sigs = d["sigs"].astype(str)
    origin = d["origin"].astype(str) if "origin" in d.files else np.array(["mapreader"] * len(sigs))
    cnt = Counter(sigs)
    print(f"{len(sigs)} anchors / {len(cnt)} sigs (majority {max(cnt.values())/len(sigs):.2f})", flush=True)

    D = {}
    for col in ("mr", "word", "line"):
        D[col] = np.array([concat(c) for c in d[col]])
        print(f"  {col:5s} descriptors {D[col].shape}", flush=True)

    print("\nViTAEv2 backbone descriptor, same anchors, crop convention varied:")
    for col, label in (("mr", "MapReader box  (control)"), ("word", "Hi-SAM word mask       "),
                       ("line", "Hi-SAM LINE mask       ")):
        print(f"  {label}  maxsim-LOO {loo_maxsim(D[col], sigs):.3f}   kNN5 {loo_knn(D[col], sigs):.3f}")
    print("  reference: 0.63 maxsim-LOO on MapReader crops (legacy convention)")

    # Two crop conventions now share one bank: MapReader word boxes for the ordinary lettering, Hi-SAM LINE
    # masks for the big admin labels MapReader never spotted. Reported separately because a single headline
    # would hide the possibility that the new rows score well only among themselves — which would mean the
    # bank had learned the convention rather than the face.
    Xp = norm(D["mr"])
    simp = Xp @ Xp.T
    np.fill_diagonal(simp, -2)
    sl_all = sorted(set(sigs))
    cols_all = {t: np.where(sigs == t)[0] for t in sl_all}
    print("\nmaxsim-LOO split by crop origin (production column, one shared anchor pool):")
    for org in sorted(set(origin)):
        idx = np.where(origin == org)[0]
        if not len(idx):
            continue
        ok = 0
        for i in idx:
            sc = [simp[i, cols_all[t][cols_all[t] != i]].max() if len(cols_all[t][cols_all[t] != i]) else -9
                  for t in sl_all]
            ok += (sl_all[int(np.argmax(sc))] == sigs[i])
        print(f"  {org:12s} n={len(idx):4d}  {ok/len(idx):.3f}")

    # Per-signature accuracy on the production column — the balance question the paper actually needs, and
    # the thing a single headline number hides.
    Xl = norm(D["line"])
    sim = Xl @ Xl.T
    np.fill_diagonal(sim, -2)
    sl = sorted(cnt)
    cols = {s: np.where(sigs == s)[0] for s in sl}
    # NB when anchor_recrop runs without --hisam the mr/word/line columns hold the SAME crop, so this block
    # reports the production column under either setting rather than a separate Hi-SAM measurement.
    print("\nper-face maxsim-LOO (production crops):")
    for s in sl:
        idx = cols[s]
        ok = 0
        for i in idx:
            sc = [sim[i, cols[t][cols[t] != i]].max() if len(cols[t][cols[t] != i]) else -9 for t in sl]
            ok += (sl[int(np.argmax(sc))] == s)
        print(f"  {s:38s} n={len(idx):3d}  {ok/len(idx):.3f}")

    np.savez_compressed(a.out, sigs=sigs.astype(object), texts=d["texts"],
                        origin=origin.astype(object), gcx=d["gcx"], gcy=d["gcy"],
                        **{f"desc_{k}": v.astype(np.float16) for k, v in D.items()})
    print(f"\nwrote {a.out}", flush=True)
    print("READOUTDONE", flush=True)


if __name__ == "__main__":
    main()
