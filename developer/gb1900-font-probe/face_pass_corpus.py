"""D1(b) — style and slant for every detected word, corpus-wide, via mosaic ROI-align.

One backbone forward per 2048px mosaic; every box inside that mosaic is ROI-pooled from the same feature
map. Measured at 0.655 signature LOO against 0.674 for per-word 512² crops (n=307, SE 0.027) — statistically
indistinguishable for ~50x less compute, which is what makes a corpus pass possible at all.

WHAT EACH RECORD CLAIMS, and how strongly:

  coarse            italic / upright / blackletter. The better-evidenced axis, and the one D2 needs: the
                    antiquity hand versus roman or italic is what disambiguates Camp, Castle, Cross, Stone.
  sig               the signature (base_style·fill·decor) == the face inventory. Measured 0.655 LOO over 7
                    classes against 0.410 chance — real, but per-class 0.64-0.76 on the three populated
                    classes and thinner elsewhere. Carried WITH alternatives and confidence so a consumer
                    can degrade to the coarse axis rather than trust a single verdict.
  slant_deg         the ITALIC SLANT of the strokes, from slant_v2.slant_deg, which deskews first — so it is
                    not the label's rotation on the map. That distinction is why D1(a) emitted no slant at
                    all rather than passing off the baseline angle as one.

Below the signature the OS engraving itself does not distinguish categories (one generic serif at
overlapping sizes: county_bridges 36px == woods_copses 36px, established 2026-07-23), so no finer target is
offered here. That ceiling belongs to the source, not the classifier.

    python face_pass_corpus.py --shard 0 --of 32 --ref labels/roi_reference.npz --out-dir .../style
"""
import argparse, glob, json, math, os, sys, time
from collections import Counter

import cv2
import numpy as np
import torch

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")

HERE = "/vast/ishi/gb1900/probe/font"
SPOT2 = "/vast/ishi/gb1900/edition/spot2"
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
TILE, MOS, OVERLAP, R = 256, 8, 1, 8


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
    print(f"backbone hooked at {name}, {dev}", flush=True)
    return model, feat, dev, getattr(runner.predictor, "input_format", "BGR")


def fmaps(model, feat, dev, fmt, img):
    arr = img[:, :, ::-1] if fmt == "BGR" else img
    t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
    feat.clear()
    try:
        with torch.no_grad():
            model([{"image": t, "height": img.shape[0], "width": img.shape[1]}])
    except Exception:
        pass
    o = feat.get("o", {})
    return [(v.tensors if hasattr(v, "tensors") else v)[0]
            for k in (sorted(o) if isinstance(o, dict) else [])
            for v in [o[k]]
            if torch.is_tensor(v.tensors if hasattr(v, "tensors") else v)
            and (v.tensors if hasattr(v, "tensors") else v).dim() == 4]


def roi_concat(maps, bb, hw):
    H, W = hw
    x0, y0, x1, y1 = bb
    per = []
    for m in maps:
        C, h, w = m.shape
        a = max(0, int(math.floor(x0 / W * w))); b = min(w, max(a + 1, int(math.ceil(x1 / W * w))))
        c = max(0, int(math.floor(y0 / H * h))); d = min(h, max(c + 1, int(math.ceil(y1 / H * h))))
        per.append(m[:, c:d, a:b].mean(dim=(1, 2)).float().cpu().numpy())
    return np.concatenate(per) if per else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--centres", default=f"{HERE}/centres_all.txt")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--ref", default=f"{HERE}/labels/roi_reference_aug.npz")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out-dir", default="/vast/ishi/gb1900/edition/style")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    z = np.load(a.ref, allow_pickle=True)
    RX = z["desc"].astype(np.float32)
    RX /= (np.linalg.norm(RX, axis=1, keepdims=True) + 1e-9)
    RY = z["sig"].astype(str)
    # CLASS-PRIOR CORRECTION. The reference is unbalanced (italic·solid·serif is 126 of 307), and an
    # unweighted k-vote drifts to the majority class: the first diverse sample called 80% of words italic
    # against an independent human estimate of 59% (font_testset_decisions_1: 114 italic of 192). Weighting
    # each neighbour by 1/|class| makes the vote a likelihood rather than a popularity contest. The
    # reference stays as collected — active-learning sampling means its class mix reflects what was hard to
    # label, not what the corpus contains, so it is not a usable prior.
    RN = Counter(RY.tolist())
    # sqrt, not 1/n: full inverse-frequency let an n=5 class outvote an n=126 one and produced
    # 13% numerals and 14.6% blackletter, the latter with italic's slant signature (4.29 deg).
    RW = np.array([1.0 / math.sqrt(RN[y]) for y in RY], np.float32)
    print(f"reference {len(RY)} descriptors, {len(RN)} classes; "
          f"class-prior corrected (largest {max(RN.values())}, smallest {min(RN.values())})", flush=True)

    import spot_sheet as S
    from slant_v2 import slant_deg
    model, feat, dev, fmt = load_model()

    tags = [l.split()[2] for l in open(a.centres) if len(l.split()) >= 3]
    cent = {}
    for l in open(a.centres):
        p = l.split()
        if len(p) >= 3:
            cent[p[2]] = (float(p[0]), float(p[1]))
    mine = [t for i, t in enumerate(tags) if i % a.of == a.shard]
    if a.limit:
        mine = mine[:a.limit]
    print(f"shard {a.shard}/{a.of}: {len(mine)} regions", flush=True)

    outp = os.path.join(a.out_dir, f"style_{a.shard:03d}.jsonl")
    t0 = time.time()
    n_w = n_reg = 0
    with open(outp, "w", encoding="utf-8") as out:
        for ri, tag in enumerate(mine):
            bf = f"{SPOT2}/boxes_{tag}.jsonl"
            if not os.path.exists(bf):
                continue
            boxes = [json.loads(l) for l in open(bf, encoding="utf-8") if l.strip()]
            if not boxes:
                continue
            lon, lat = cent[tag]
            cx, cy = S.lonlat_to_px(lon, lat)
            ctx, cty = int(cx // TILE), int(cy // TILE)
            tx0, ty0 = ctx - R, cty - R
            side, step = 2 * R + 1, MOS - OVERLAP
            done = set()
            for i in range(0, side, step):
                for j in range(0, side, step):
                    mx0, my0 = tx0 + i, ty0 + j
                    ox, oy = mx0 * TILE, my0 * TILE
                    W = H = MOS * TILE
                    todo = []
                    for bi, b in enumerate(boxes):
                        if bi in done:
                            continue
                        p = np.array(b["gpoly"], np.float64)
                        x0, y0, x1, y1 = p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()
                        if x0 >= ox and y0 >= oy and x1 <= ox + W and y1 <= oy + H:
                            todo.append((bi, x0 - ox, y0 - oy, x1 - ox, y1 - oy))
                    if not todo:
                        continue
                    img, _ = S.mosaic(mx0, my0, MOS)
                    g = np.asarray(img.convert("L"), np.uint8)
                    im3 = np.repeat(g[:, :, None], 3, 2)
                    mp = fmaps(model, feat, dev, fmt, im3)
                    if not mp:
                        continue
                    for bi, bx0, by0, bx1, by1 in todo:
                        d = roi_concat(mp, (bx0, by0, bx1, by1), (H, W))
                        if d is None:
                            continue
                        d = d / (np.linalg.norm(d) + 1e-9)
                        s = RX @ d
                        top = np.argsort(-s)[:a.k]
                        w = {}
                        for t in top:
                            w[RY[t]] = w.get(RY[t], 0.0) + RW[t]
                        tot = sum(w.values()) or 1.0
                        ranked = sorted(w.items(), key=lambda kv: -kv[1])
                        sig, nv = ranked[0][0], ranked[0][1] / tot
                        alts = [[k_, round(v_ / tot, 3)] for k_, v_ in ranked[:3]]
                        cw = {}
                        for k_, v_ in w.items():
                            cw[k_.split("·")[0]] = cw.get(k_.split("·")[0], 0.0) + v_
                        coarse, cn = max(cw.items(), key=lambda kv: kv[1])
                        cn = cn / tot
                        sub = g[max(0, int(by0)):int(by1), max(0, int(bx0)):int(bx1)]
                        sl = None
                        if sub.size and min(sub.shape) >= 6:
                            try:
                                sl = float(slant_deg(sub.astype(np.float32) / 255.0))
                            except Exception:
                                sl = None
                        b = boxes[bi]
                        # Box size decides whether the descriptor can say anything at all. At mosaic
                        # stride ~16 a 2-3 character token spans one or two feature cells, so there is no
                        # letterform left to pool and the nearest anchor is arbitrary — that is how OS
                        # abbreviations (m. b. w f. p.) came back as 15% blackletter with italic's slant.
                        # Recorded so the threshold can be calibrated against the slant separation rather
                        # than guessed.
                        bw_px, bh_px = float(bx1 - bx0), float(by1 - by0)
                        out.write(json.dumps(dict(
                            region=tag, gcx=b["gcx"], gcy=b["gcy"], text=b["text"],
                            w_px=round(bw_px, 1), h_px=round(bh_px, 1),
                            cells=round(bw_px / 16.0, 2),
                            sig=sig, sig_conf=round(nv, 3), sig_alts=alts,
                            coarse=coarse, coarse_conf=round(cn, 3),
                            slant_deg=(round(sl, 2) if sl is not None else None)),
                            ensure_ascii=False) + "\n")
                        done.add(bi); n_w += 1
            n_reg += 1
            if n_reg % 100 == 0:
                print(f"  [{a.shard}] {n_reg}/{len(mine)} regions, {n_w:,} words "
                      f"({time.time()-t0:.0f}s)", flush=True)
    print(f"STYLEDONE {a.shard}: {n_w:,} words in {n_reg} regions -> {outp} ({time.time()-t0:.0f}s)",
          flush=True)


if __name__ == "__main__":
    main()
