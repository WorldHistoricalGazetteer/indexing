"""Phase B size analysis. Aggregate the crowd-MATCHED labels from the sheet-wide discovery (clean boxes,
real text), categorise each by its crowd text (reliable signals only), and report cap-height per category
in GROUND-METRES (latitude-invariant). Then regress the map ground-metre sizes against the Characteristic
Sheet CS-px ladder (reference/cap_heights.json) to (a) test whether cap-height DISCRIMINATES categories,
and (b) derive the CS-px -> ground-m calibration + whether the CS is drawn at true map scale.

    python analyze_sizes.py labels_dir/          # dir of labels_*.json from discover_sheet.py
"""
import sys, os, re, json, glob, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CS = {r["key"]: r["cap_h_native_px"] for r in json.load(open(os.path.join(HERE, "reference/cap_heights.json")))}

WATER = re.compile(r"\b(River|Riv|Brook|Burn|Canal|Stream|Beck|Water|Afon|Nant|Gill|Pool|Mere|Tarn|Lake)\b", re.I)
ANTIQ = re.compile(r"\b(Tumulus|Tumuli|Cairn|Camp|Earthwork|Barrow|Motte|Cist|Enclosure|Fort|Roman|Site of)\b", re.I)
BM = re.compile(r"^\s*B[. ]?M[. ]")
NUM = re.compile(r"^[\d\s.,]+$")

# category -> representative CS-px exemplar (its cap-height on the Characteristic Sheet)
ANCHOR = {"bm": "ex_contour_numeral", "numeral": "ex_contour_numeral", "water": "ex_small_rivers",
          "settlement": "ex_other_villages", "antiquity": "ex_antiq_saxon"}

def categorise(t):
    t = (t or "").strip()
    if not t: return None
    if BM.match(t): return "bm"
    if NUM.match(t) and any(c.isdigit() for c in t): return "numeral"
    if ANTIQ.search(t): return "antiquity"
    if WATER.search(t): return "water"
    al = [c for c in t if c.isalpha()]
    if len(al) >= 4 and t[:1].isupper() and t != t.upper(): return "settlement"   # title-case proper name
    return None

def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    labels = []
    for f in glob.glob(os.path.join(d, "labels_*.json")):
        labels += json.load(open(f))
    matched = [L for L in labels if L.get("crowd")]
    print(f"loaded {len(labels)} labels from {len(glob.glob(os.path.join(d,'labels_*.json')))} sheets; matched={len(matched)}")
    by = {}
    for L in matched:
        c = categorise(L["crowd"])
        if c: by.setdefault(c, []).append(L)
    print(f"\n{'category':12s} {'N':>5s} {'cap-h px':>18s} {'ground-m':>20s} {'paper-mm':>10s}")
    pts = []
    for cat in ["numeral", "bm", "water", "antiquity", "settlement"]:
        v = by.get(cat, [])
        if len(v) < 8: print(f"{cat:12s} {len(v):>5d}  (too few)"); continue
        px = np.array([L["caph"] for L in v]); gm = np.array([L["ground_m"] for L in v]); mm = gm / 10.56
        print(f"{cat:12s} {len(v):>5d}  med={np.median(px):4.0f} IQR=[{np.percentile(px,25):.0f},{np.percentile(px,75):.0f}]  "
              f"med={np.median(gm):5.2f} IQR=[{np.percentile(gm,25):.2f},{np.percentile(gm,75):.2f}]  {np.median(mm):.2f}")
        cs = CS.get(ANCHOR[cat])
        if cs: pts.append((cat, cs, float(np.median(gm))))

    # LARGE labels (size-based) — the big fonts the discovery captured; shows the true size RANGE on the map
    large = sorted([L for L in matched if L["caph"] > 42], key=lambda L: -L["caph"])
    if large:
        gm = np.array([L["ground_m"] for L in large])
        print(f"\nLARGE (cap-h>42px) N={len(large)}: ground-m median={np.median(gm):.1f} "
              f"IQR=[{np.percentile(gm,25):.0f},{np.percentile(gm,75):.0f}] max={gm.max():.0f}")
        print("  biggest 14 texts:", [str(L['crowd'])[:18] for L in large[:14]])

    # regression: ground-m = slope * CS_px (+ intercept)
    if len(pts) >= 3:
        X = np.array([p[1] for p in pts]); Y = np.array([p[2] for p in pts])
        A = np.vstack([X, np.ones_like(X)]).T
        (slope, icpt), res, *_ = np.linalg.lstsq(A, Y, rcond=None)
        yhat = slope * X + icpt; ss = 1 - ((Y - yhat) ** 2).sum() / max(1e-9, ((Y - Y.mean()) ** 2).sum())
        s0 = (X @ Y) / (X @ X)                        # through-origin slope (CS at true map scale)
        print(f"\n=== CS-px -> ground-m regression (anchors: {[p[0] for p in pts]}) ===")
        for cat, x, y in pts: print(f"  {cat:12s} CS={x:5.0f}px  ground-m={y:5.2f}  (fit {slope*x+icpt:5.2f})")
        print(f"  slope={slope:.4f} m/CS-px  intercept={icpt:.3f} m  R2={ss:.3f}")
        print(f"  through-origin slope={s0:.4f} m/CS-px  -> if intercept~0 & R2 high, CS is at TRUE MAP SCALE")
        print(f"\n  => projected ground-m size for EVERY category (CS-px x through-origin slope):")
        for k in sorted(CS, key=lambda k: -CS[k]):
            print(f"     {k:26s} CS={CS[k]:5.1f}px -> {CS[k]*s0:6.2f} m ground  ({CS[k]*s0/10.56:.2f} mm paper)")

if __name__ == "__main__":
    main()
