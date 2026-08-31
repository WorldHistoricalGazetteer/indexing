"""Stage A — read the big-cap detections, so the nest-match has a string to match.

Recognition and detection are separate failures. MapReader's spotter does not fire on widely letter-spaced
admin lettering, which is why these labels are absent from every boxes_*.jsonl; but handed the crop, its
recognition head reads them (SELBY, CARDIGAN, PONTEFRACT, CHANTRELL STREET on the first pass). So the labels
the detector never found are still readable, and that is what makes matching them against a gazetteer possible
without a human typing 8,000 strings.

The model must be called DIRECTLY. detectron2's DefaultPredictor re-resizes its input, which breaks ViTAEv2's
`assert N == H * W` no matter how the crop is padded beforehand — every predictor route fails and the
traceback points at the backbone rather than at the resize. Padding to a multiple of 32 and calling
`model([{image, height, width}])` is what already works elsewhere in this codebase.

    python bigcaps_read.py --amg '/vast/.../amg_line_sheet_*.jsonl' --out bigcaps_read.jsonl
"""
import argparse, glob, json, os, sys
from collections import Counter
import numpy as np
import cv2
import torch

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate
from bigfont_ui import shortside, bbox, covered, index_boxes
from propose_faces import INST


def pad32(g, scale=2.0, minw=256, maxw=2048):
    """Canvas whose sides are multiples of 32, lettering centred on paper white.

    ViTAEv2's reduction cells assert that the token count equals H*W, so an arbitrary word crop — which is
    never a multiple of 32 in either axis — fails inside the backbone.
    """
    up = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if up.shape[1] > maxw:
        s = maxw / up.shape[1]
        up = cv2.resize(up, (maxw, max(8, int(up.shape[0] * s))), interpolation=cv2.INTER_AREA)
    H = max(64, ((up.shape[0] + 31) // 32) * 32)
    W = max(minw, ((up.shape[1] + 31) // 32) * 32)
    cv = np.full((H, W), 255, np.uint8)
    oy, ox = (H - up.shape[0]) // 2, (W - up.shape[1]) // 2
    if oy < 0 or ox < 0:
        return None
    cv[oy:oy + up.shape[0], ox:ox + up.shape[1]] = up
    return cv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amg", default="/vast/ishi/gb1900/edition/amg/amg_line_sheet_*.jsonl")
    ap.add_argument("--mr", default="/vast/ishi/gb1900/edition/amg/cover/mr_*.jsonl")
    ap.add_argument("--min-cap", type=float, default=24.0)
    ap.add_argument("--max-cap", type=float, default=400.0)
    ap.add_argument("--cover", type=float, default=0.5)
    ap.add_argument("--min-score", type=float, default=0.5)
    ap.add_argument("--n", type=int, default=8000)
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/spot/bigcaps_read.jsonl")
    a = ap.parse_args()

    mr = []
    for f in sorted(glob.glob(a.mr)):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("gpoly"):
                mr.append(bbox(r["gpoly"]))
    midx = index_boxes(mr) if mr else {}
    print(f"{len(mr)} MapReader boxes for the coverage gate", flush=True)

    recs, drop = [], Counter()
    for f in sorted(glob.glob(a.amg)):
        sheet = os.path.basename(f).replace("amg_line_", "").replace(".jsonl", "")
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("gpoly") or r.get("score", 1.0) < a.min_score:
                drop["no polygon / low score"] += 1
                continue
            cap = shortside(r["gpoly"])
            if not (a.min_cap <= cap <= a.max_cap):
                drop["outside cap-height band"] += 1
                continue
            if midx and covered(r["gpoly"], midx, a.cover):
                drop["already found by MapReader"] += 1
                continue
            r["cap"], r["sheet"] = round(float(cap), 1), sheet
            recs.append(r)
    recs.sort(key=lambda r: -r["cap"])
    n_gated = len(recs)
    recs = recs[: a.n]
    for k, v in drop.most_common():
        print(f"  dropped {v:>6d}  {k}")
    if n_gated > len(recs):
        print(f"  dropped {n_gated - len(recs):>6d}  beyond --n {a.n}")
    print(f"{len(recs)} big-cap candidates to read", flush=True)

    import pandas as pd
    from mapreader import MapTextRunner
    runner = MapTextRunner(pd.DataFrame(),
                           cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
                           weights_file=f"{INST}/weights/rumsey-finetune.pth", device="cuda")
    model = runner.predictor.model
    model.eval()
    fmt = runner.predictor.input_format

    out, nread = [], 0
    with open(a.out, "w") as fh:
        for i, r in enumerate(recs):
            if i and i % 500 == 0:
                print(f"  read {i}/{len(recs)} ({nread} with text)", flush=True)
            crop = derotate(r)
            if crop is None or crop.size < 200:
                continue
            g = pad32(crop)
            if g is None:
                continue
            im = np.repeat(g[:, :, None], 3, 2)
            arr = im[:, :, ::-1] if fmt == "BGR" else im
            t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).cuda()
            try:
                with torch.no_grad():
                    o = model([{"image": t, "height": g.shape[0], "width": g.shape[1]}])
            except Exception:
                continue
            inst = o[0]["instances"] if isinstance(o[0], dict) else o[0]
            if not len(inst) or not hasattr(inst, "recs"):
                continue
            toks = []
            # Read order matters: the gazetteer name is a phrase ("Stocks Hill"), and the recogniser returns
            # its pieces in no particular order. Sorting by control-point x reassembles them left to right.
            try:
                cps = inst.ctrl_points.cpu().numpy() if hasattr(inst, "ctrl_points") else None
            except Exception:
                cps = None
            for j in range(len(inst)):
                try:
                    s = runner._ctc_decode_recognition(inst.recs[j])
                except Exception:
                    continue
                x = float(cps[j].reshape(-1, 2)[:, 0].mean()) if cps is not None else float(j)
                sc = float(inst.scores[j]) if hasattr(inst, "scores") else 0.0
                toks.append((x, s, round(sc, 3)))
            toks.sort()
            if not toks:
                continue
            nread += 1
            rec = dict(sheet=r["sheet"], gcx=r.get("gcx"), gcy=r.get("gcy"),
                       lon=r.get("lon"), lat=r.get("lat"), cap=r["cap"], gpoly=r.get("gpoly"),
                       tokens=[dict(text=s, score=sc) for _, s, sc in toks],
                       text=" ".join(s for _, s, _ in toks))
            out.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n{nread}/{len(recs)} crops produced text -> {a.out}")
    print("READDONE", flush=True)


if __name__ == "__main__":
    main()
