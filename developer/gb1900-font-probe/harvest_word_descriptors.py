"""Word-level backbone descriptors for the lexically-harvested anchors, in the SAME space as the human set.

`harvest_face_anchors.py` produced GLYPH rasters, which feed the same-letter kNN / SSL path — the instrument
the backbone probe superseded (LOO 0.42 concat vs 0.14 SSL). The deployed face instrument is a PER-WORD
descriptor: grayscale word crop, derotated to horizontal, white square-pad, 512², frozen ViTAEv2, stages
3-5 mean-pooled and concatenated. That is what `anchor_descriptors.py` builds for the 38 human-labelled
anchors, and a reference in any other space cannot be compared with them — its own docstring records an
earlier RGB-snippet bank being exactly that mistake.

SPACE COMPARABILITY IS THE RISK, and it is handled by measurement rather than assumption. The human anchors
are cropped from a stored snippet using their letter boxes and derotated by the first→last letter angle;
harvested words are cropped from the /ix1 tile corpus and derotated by the polygon's minimum-area rectangle.
Both yield a horizontal grayscale word squared onto 512², but they are not the identical code path. The
decisive test therefore reports a SPACE CONTROL alongside the result: if human anchors of the two harvested
faces do not retrieve harvested same-face neighbours above chance, the spaces differ and the accuracy figure
means nothing — a null must not be read as a weak signal when it may be a mismatched embedding.

STORAGE: descriptors only (float16), never crops. Tiles are read from /ix1; FCTILES stays unset so nothing
is fetched or cached.

    python harvest_word_descriptors.py --shard 0 --of 4 --out-dir labels/wdesc
"""
import argparse, glob, json, math, os, re, sys, time
from collections import Counter

import cv2
import numpy as np
import torch

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
from make_font_testset_v2 import derotate

SAFE = 512
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
FACE_LEX = {
    "Blackletter": {"tumulus", "tumuli", "cairn", "cairns", "barrow", "barrows",
                    "earthwork", "earthworks", "tumbrel"},
    "Upright-Solid-Serif": {"wood", "woods", "copse", "copses", "plantation", "plantations",
                            "covert", "coverts", "shaw"},
    # Italic is NOT starved (1,522 anchors) but is needed here so the reference can be tested on all three
    # coarse classes against the 225 human decisions. Lexicon is anchor_harvest.py's context-INDEPENDENT
    # italic set: water and minor-feature words whose OS category is italic regardless of neighbours.
    # Matching is on the whole normalised word, so "Bakewell" does not match "well".
    "Italic-Solid-Serif": {"spring", "springs", "well", "wells", "ford", "weir", "sluice",
                           "quarry", "quarries", "issues", "sinks", "site"},
}
W2F = {w: f for f, ws in FACE_LEX.items() for w in ws}

_MODEL = {"m": None}


def load_model():
    if _MODEL["m"]:
        return _MODEL["m"]
    from mapreader import MapTextRunner
    import pandas as pd
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    runner = MapTextRunner(pd.DataFrame(),
                           cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
                           weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
    model = runner.predictor.model
    feat = {}
    tgt_name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")],
                        key=lambda nm: nm[0].count("."))
    tgt.register_forward_hook(lambda m, i, o: feat.__setitem__("o", o))
    fmt = getattr(runner.predictor, "input_format", "BGR")
    _MODEL["m"] = (model, feat, dev, fmt)
    print(f"backbone hooked at {tgt_name}, device {dev}, input_format {fmt}", flush=True)
    return _MODEL["m"]


def backbone_concat(im3):
    model, feat, dev, fmt = load_model()
    arr = im3[:, :, ::-1] if fmt == "BGR" else im3
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
    return np.concatenate(per) if per else None


def square512(crop):
    """White square-pad then 512², matching anchor_descriptors.word_crop_gray's tail exactly."""
    if crop is None or crop.size == 0 or min(crop.shape[:2]) < 6:
        return None
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    m = max(crop.shape[:2])
    sq = np.full((m, m), 255, np.uint8)
    sq[:crop.shape[0], :crop.shape[1]] = crop
    g = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)
    return np.repeat(g[:, :, None], 3, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="/vast/ishi/gb1900/edition/gb_stamp_labels.jsonl")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--per-face", type=int, default=600, help="descriptors per face overall")
    ap.add_argument("--per-term-frac", type=float, default=0.34)
    ap.add_argument("--out-dir", default="labels/wdesc")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    budget = max(1, a.per_face // max(1, a.of))
    term_cap = max(1, int(budget * a.per_term_frac))
    kept = {f: [] for f in FACE_LEX}
    texts = {f: [] for f in FACE_LEX}
    per_term = Counter()
    t0 = time.time()
    tried = 0
    with open(a.labels, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i % a.of != a.shard:
                continue
            if all(len(v) >= budget for v in kept.values()):
                break
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for wd in rec.get("words", []):
                t = re.sub(r"[^a-z]", "", str(wd["text"]).lower())
                face = W2F.get(t)
                if not face or len(kept[face]) >= budget or per_term[(face, t)] >= term_cap:
                    continue
                tried += 1
                im = square512(derotate({"gpoly": wd["poly"]}))
                if im is None:
                    continue
                d = backbone_concat(im)
                if d is None:
                    continue
                kept[face].append(d.astype(np.float16))
                texts[face].append(wd["text"])
                per_term[(face, t)] += 1
            if (i + 1) % 200000 == 0:
                print(f"  [{a.shard}] line {i+1:,}: "
                      f"{ {k: len(v) for k, v in kept.items()} } ({time.time()-t0:.0f}s)", flush=True)

    D, F, T = [], [], []
    for face, ds in kept.items():
        D.extend(ds); F.extend([face] * len(ds)); T.extend(texts[face])
    outp = os.path.join(a.out_dir, f"wdesc_{a.shard:03d}.npz")
    np.savez_compressed(outp, desc=np.array(D, np.float16) if D else np.zeros((0, 1), np.float16),
                        face=np.array(F), text=np.array(T))
    print(f"WDESCDONE {a.shard}: { {k: len(v) for k, v in kept.items()} } from {tried} tried -> {outp} "
          f"({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
