"""SG's idea: italic vs upright serif is STROKE SLANT — measure it directly instead of hoping the
embedding captures it. Shear-search deslant: shear the crop over a range of angles; the angle that
maximises the vertical-projection sharpness is the text slant (upright ~0 deg, italic ~10-18 deg).
Test slant alone + slant fused, LOO on human serif anchors.
    python slant_probe.py --labels font_labels.json --out out_z17
"""
import argparse, os, json, math, numpy as np
from collections import Counter
from scipy import ndimage as ndi
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from serif_push import UPRIGHT, ITALIC, word_sem, TILES16, TILES17, BOXES
from fusion import textfeats

def ink_of(gray01):
    a = 1.0 - np.clip(gray01, 0, 1)              # ink high
    a = a - np.median(a)                          # remove paper
    return np.clip(a, 0, None)

def shear_cols(ink, shear):
    H, W = ink.shape; yc = H / 2.0
    out = np.zeros_like(ink)
    for y in range(H):
        dx = int(round(shear * (y - yc)))
        out[y] = np.roll(ink[y], dx)
    return out

def slant_angle(gray01, lo=-8, hi=26, step=1):
    ink = ink_of(gray01)
    if ink.sum() < 1: return 0.0
    best_a, best_s = 0.0, -1
    for a in np.arange(lo, hi, step):
        col = shear_cols(ink, math.tan(math.radians(a))).sum(0)
        col = col / (col.sum() + 1e-6)
        s = (col ** 2).sum()                      # peakiness: sharp when vertical strokes align
        if s > best_s: best_s, best_a = s, a
    return float(best_a)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    _, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    labels = json.load(open(a.labels))
    rows = []
    for r in labels:
        if r["label"] not in ("serif_upright", "serif_italic"): continue
        i = int(r["id"].split("_")[-1])
        if i >= len(kept): continue
        c = DATA.crop_box(kept[i]["gpoly"], TILES17, scale=2)   # flattened 0..1
        if c is None: continue
        rows.append((r["label"], slant_angle(c), kept[i].get("text", ""),
                     CD.cap_h_m(kept[i]["gpoly"]) * 2))
    y = np.array([r[0] for r in rows]); slant = np.array([r[1] for r in rows])
    up = slant[y == "serif_upright"]; it = slant[y == "serif_italic"]
    print("n:", len(rows), "| slant deg  upright mean %.1f (sd %.1f)  italic mean %.1f (sd %.1f)"
          % (up.mean(), up.std(), it.mean(), it.std()), flush=True)

    def loo(X):
        p = cross_val_predict(make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, class_weight="balanced")),
                              X, y, cv=LeaveOneOut())
        return round(float((p == y).mean()), 3)
    S = slant.reshape(-1, 1)
    WS = np.array([word_sem(r[2]) for r in rows])
    SZ = np.array([[math.log(max(1.0, r[3]))] for r in rows])
    rep = dict(n=len(rows), upright_slant_mean=round(float(up.mean()), 2), italic_slant_mean=round(float(it.mean()), 2),
               slant_only=loo(S), slant_plus_size=loo(np.hstack([S, SZ])),
               slant_plus_wordsem=loo(np.hstack([S, WS])),
               slant_size_wordsem=loo(np.hstack([S, SZ, WS])))
    print("SLANT serif upright/italic (LOO):", json.dumps(rep), flush=True)
    json.dump(rep, open(os.path.join(a.out, "slant_report.json"), "w"), indent=2)
    print("WROTE slant_report.json", flush=True)

if __name__ == "__main__":
    main()
