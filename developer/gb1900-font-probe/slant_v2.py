"""Slant v2 (SG's OpenCV edge-angle idea, done properly): remove linework, deskew the baseline,
then measure the gradient orientation of the near-VERTICAL strokes -> their deviation from 90 deg
is the italic slant. Cleaner than shear-projection on cluttered crops. Trains on lexicon-auto serif
(hundreds) + tests on HUMAN serif; reports slant-only and slant+word-sem+size.
    python slant_v2.py --labels font_labels.json --out out_z17
"""
import argparse, os, json, math, numpy as np, cv2
from collections import Counter
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from serif_push import UPRIGHT, ITALIC, word_sem, TILES16, TILES17, BOXES, NT, LEX

def deskew(bw):
    ys, xs = np.where(bw > 0)
    if len(xs) < 20: return bw
    ang = cv2.minAreaRect(np.column_stack([xs, ys]).astype(np.float32))[2]
    if ang < -45: ang += 90
    if abs(ang) > 30: return bw
    M = cv2.getRotationMatrix2D((bw.shape[1] / 2, bw.shape[0] / 2), ang, 1.0)
    return cv2.warpAffine(bw, M, (bw.shape[1], bw.shape[0]), flags=cv2.INTER_NEAREST)

def slant_deg(gray01):
    img = (np.clip(gray01, 0, 1) * 255).astype(np.uint8)
    _, bw = cv2.threshold(255 - img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # remove long horizontal linework (roads/rules)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(8, bw.shape[1] // 6), 1))
    bw = cv2.subtract(bw, cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk))
    bw = deskew(bw)
    gx = cv2.Sobel(bw.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(bw.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    stroke = (np.degrees(np.arctan2(gy, gx)) + 90.0) % 180.0     # stroke dir ⟂ gradient
    m = (mag > mag.max() * 0.2) & (np.abs(stroke - 90.0) < 35)   # near-vertical strokes only
    if m.sum() < 10: return 0.0
    return float(np.average(stroke[m] - 90.0, weights=mag[m]))   # deviation from vertical = slant

def harvest_serif(labels, kept):
    rows = []
    for r in labels:
        if r["label"] not in ("serif_upright", "serif_italic"): continue
        i = int(r["id"].split("_")[-1])
        if i >= len(kept): continue
        c = DATA.crop_box(kept[i]["gpoly"], TILES17, scale=2)
        if c is None: continue
        rows.append((r["label"], slant_deg(c), kept[i].get("text", ""), CD.cap_h_m(kept[i]["gpoly"]) * 2))
    return rows

def harvest_lex(n=1500):
    rows = []
    for line in open(NT):
        if len(rows) >= n: break
        try: d = json.loads(line)
        except Exception: continue
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        lon, lat = d.get("lon"), d.get("lat")
        if not (tv and lon and lat): continue
        k = tv.strip().lower()
        if k not in LEX: continue
        c = DATA.crop_point(lon, lat, TILES17)
        if c is None: continue
        rows.append((LEX[k], slant_deg(c), tv.strip(), 40.0))
    return rows

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--labels", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    _, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    hum = harvest_serif(json.load(open(a.labels)), kept)
    yh = np.array([r[0] for r in hum]); sh = np.array([r[1] for r in hum])
    up, it = sh[yh == "serif_upright"], sh[yh == "serif_italic"]
    print("human n=%d | slant upright %.1f±%.1f  italic %.1f±%.1f" % (len(hum), up.mean(), up.std(), it.mean(), it.std()), flush=True)

    def loo(X, y):
        p = cross_val_predict(make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, class_weight="balanced")),
                              X, y, cv=LeaveOneOut())
        return round(float((p == y).mean()), 3)
    Sh = sh.reshape(-1, 1); WSh = np.array([word_sem(r[2]) for r in hum])
    rep = dict(n_human=len(hum), upright_slant=round(float(up.mean()), 2), italic_slant=round(float(it.mean()), 2),
               slant_only_LOO=loo(Sh, yh), slant_wordsem_LOO=loo(np.hstack([Sh, WSh]), yh))

    # train slant classifier on abundant lexicon-auto, test on human (bigger training signal)
    lex = harvest_lex()
    if lex:
        yl = np.array([r[0] for r in lex]); sl = np.array([r[1] for r in lex]).reshape(-1, 1)
        clf = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, class_weight="balanced")).fit(sl, yl)
        rep["n_lex"] = len(lex)
        rep["slant_trainlex_testhuman"] = round(float((clf.predict(Sh) == yh).mean()), 3)
    print("SLANT v2:", json.dumps(rep), flush=True)
    json.dump(rep, open(os.path.join(a.out, "slant_v2_report.json"), "w"), indent=2)
    print("WROTE slant_v2_report.json", flush=True)

if __name__ == "__main__":
    main()
