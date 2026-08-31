"""Is the CHEAP descriptor good enough? Mosaic-level ROI-align, measured against the same pool labels.

The corpus face pass has two possible shapes and they differ by ~50x in cost:

  per-word 512² crop      ~0.44 s/word measured  ->  16.5M words ~= 2,000 GPU-hours
  mosaic ROI-align        one 2048² forward per mosaic, pool every box from the same feature map
                          320k mosaics -> order 100 GPU-hours

`backbone_readout.py` names the second as the deployment intent but implements the first (a 512² context
window PER WORD). The difference is not only cost: on a 2048² mosaic at stride 16, a 40 px word covers 2-3
feature cells, where a 512² word crop gives it ~400 px. The cheap descriptor may simply not resolve the
letterforms — and typographic face is exactly a fine-detail property.

So this measures it rather than assuming either way: build mosaic ROI-align descriptors for the SAME pooled
signature labels scored at 0.674 (7-way LOO) / 0.833-equivalent coarse with per-word crops, and re-run the
identical LOO. If it holds up, the corpus pass is affordable. If it collapses, we know the cheap path is
unusable BEFORE spending on it, and the honest options are a smaller sample or a bigger budget.

    python roi_reference.py --out labels/roi_reference.npz
"""
import argparse, json, math, os, sys
from collections import Counter

import numpy as np
import torch

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")

HERE = "/vast/ishi/gb1900/probe/font"
SPOT2 = "/vast/ishi/gb1900/edition/spot2"
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
MOS = 8                                   # tiles per mosaic side, as spot_sheet used
TILE = 256


def load_model():
    from mapreader import MapTextRunner
    import pandas as pd
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    runner = MapTextRunner(pd.DataFrame(),
                           cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
                           weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
    model = runner.predictor.model
    feat = {}
    name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")],
                    key=lambda nm: nm[0].count("."))
    tgt.register_forward_hook(lambda m, i, o: feat.__setitem__("o", o))
    fmt = getattr(runner.predictor, "input_format", "BGR")
    print(f"backbone hooked at {name}, {dev}, {fmt}", flush=True)
    return model, feat, dev, fmt


def fmaps(model, feat, dev, fmt, img):
    """Forward one mosaic; return the list of [C,h,w] stage feature maps."""
    arr = img[:, :, ::-1] if fmt == "BGR" else img
    t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
    feat.clear()
    try:
        with torch.no_grad():
            model([{"image": t, "height": img.shape[0], "width": img.shape[1]}])
    except Exception:
        pass
    o = feat.get("o", {})
    out = []
    for k in sorted(o) if isinstance(o, dict) else []:
        v = o[k]
        if hasattr(v, "tensors"):
            v = v.tensors
        if torch.is_tensor(v) and v.dim() == 4:
            out.append(v[0])
    return out


def roi_concat(maps, box_xyxy, img_hw):
    """Mean-pool each stage over the word's sub-rectangle, then concat — the mosaic-level analogue of the
    whole-crop global pool, so the descriptor has the same dimensionality and layout."""
    H, W = img_hw
    x0, y0, x1, y1 = box_xyxy
    per = []
    for m in maps:
        C, h, w = m.shape
        a = max(0, int(math.floor(x0 / W * w))); b = min(w, max(a + 1, int(math.ceil(x1 / W * w))))
        c = max(0, int(math.floor(y0 / H * h))); d = min(h, max(c + 1, int(math.ceil(y1 / H * h))))
        per.append(m[:, c:d, a:b].mean(dim=(1, 2)).float().cpu().numpy())
    return np.concatenate(per) if per else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default=f"{HERE}/labels/roi_reference.npz")
    a = ap.parse_args()

    from face_level_test import load_pool_labels, region_index
    import spot_sheet as S

    lab = load_pool_labels()
    ri = region_index()
    want = {}
    for r in lab:
        tx, ty = int(r["gcx"] // 256), int(r["gcy"] // 256)
        for tag, (x0, y0, x1, y1) in ri.items():
            if x0 <= tx <= x1 and y0 <= ty <= y1:
                want.setdefault(tag, []).append(r); break
    print(f"{len(lab)} labels in {len(want)} regions", flush=True)

    model, feat, dev, fmt = load_model()
    D, Y = [], []
    miss = 0
    for n, (tag, rows) in enumerate(want.items()):
        bf = f"{SPOT2}/boxes_{tag}.jsonl"
        if not os.path.exists(bf):
            miss += len(rows); continue
        boxes = [json.loads(l) for l in open(bf, encoding="utf-8") if l.strip()]
        if not boxes:
            miss += len(rows); continue
        bx = np.array([[b["gcx"], b["gcy"]] for b in boxes])
        cache = {}
        for r in rows:
            d = np.hypot(bx[:, 0] - r["gcx"], bx[:, 1] - r["gcy"])
            j = int(np.argmin(d))
            if d[j] > 24:
                miss += 1; continue
            poly = np.array(boxes[j]["gpoly"], np.float64)
            gx0, gy0, gx1, gy1 = poly[:, 0].min(), poly[:, 1].min(), poly[:, 0].max(), poly[:, 1].max()
            # Mosaic window aligned to the tile grid, containing the box.
            mx0 = int(gx0 // TILE) - 1
            my0 = int(gy0 // TILE) - 1
            key = (mx0, my0)
            if key not in cache:
                img, _ = S.mosaic(mx0, my0, MOS)
                cache[key] = np.repeat(np.asarray(img.convert("L"), np.uint8)[:, :, None], 3, 2)
            img = cache[key]
            ox, oy = mx0 * TILE, my0 * TILE
            bb = (gx0 - ox, gy0 - oy, gx1 - ox, gy1 - oy)
            if bb[0] < 0 or bb[1] < 0 or bb[2] > img.shape[1] or bb[3] > img.shape[0]:
                miss += 1; continue
            mp = fmaps(model, feat, dev, fmt, img)
            desc = roi_concat(mp, bb, img.shape[:2])
            if desc is None:
                miss += 1; continue
            D.append(desc.astype(np.float32)); Y.append(r["sig"])
        if (n + 1) % 50 == 0:
            print(f"  {n+1}/{len(want)} regions, {len(D)} descriptors", flush=True)

    print(f"embedded {len(D)} ({miss} unmatched)")
    if len(D) < 40:
        sys.exit("too few to test")
    X = np.array(D, np.float32); X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    Y = np.array(Y)
    np.savez_compressed(a.out, desc=X.astype(np.float16), sig=Y)

    cnt = Counter(Y.tolist())
    S_ = X @ X.T; np.fill_diagonal(S_, -np.inf)
    idx = np.argsort(-S_, axis=1)[:, :a.k]
    pred = np.array([Counter(Y[j] for j in row).most_common(1)[0][0] for row in idx])
    acc = float((pred == Y).mean())
    coarse = np.array([s.split("·")[0] for s in Y])
    cpred = np.array([s.split("·")[0] for s in pred])
    print()
    print(f"  descriptor dim {X.shape[1]}   classes {len(cnt)}   chance {max(cnt.values())/len(Y):.3f}")
    print(f"  ROI-ALIGN signature LOO   {acc:.3f}     (per-word crops: 0.674)")
    print(f"  ROI-ALIGN coarse LOO      {float((cpred==coarse).mean()):.3f}")
    for c, n in cnt.most_common():
        m = Y == c
        print(f"    {c:26s} n={n:4d}  recall {float((pred[m]==c).mean()):.3f}")


if __name__ == "__main__":
    main()
