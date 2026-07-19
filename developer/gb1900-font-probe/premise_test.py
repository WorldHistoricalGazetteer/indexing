"""Phase C PREMISE TEST — does font-classification work on CLEAN discovery boxes, where it failed on
crowd-point windows? The earlier crowd-point font-merge scored ~0.83 balanced but over-fired rare classes
in production because the crops were OOD (label off-centre + surrounding contour ink). Here we crop the
sheet-wide-discovery BOXES (clean, localized), auto-label letterform from crowd text, embed with the same
real-domain CRNN, and run the SAME 5-fold-CV classifier — reporting per-class precision/recall + confusion.
If clean boxes separate the classes better (esp. the serif upright-vs-italic axis that plateaued at ~0.72),
the premise holds and Phase C is unblocked.

    python premise_test.py            # reads discovery labels_*.json (with box_g)
"""
import os, sys, io, re, json, glob, math, urllib.request, numpy as np
import concurrent.futures as cf
from collections import Counter
from PIL import Image
import torch
sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import crnn_data as CD
from crnn import CRNN
from crnn_eval import crnn_embed
from fusion import textfeats
from sklearn.model_selection import cross_val_predict, StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.neural_network import MLPClassifier

TILES = "/vast/ishi/gb1900/tiles17"; S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
MODEL = "/vast/ishi/gb1900/probe/font/out_z17"; DISC = "/vast/ishi/gb1900/edition/discover"
WATER = re.compile(r"\b(River|Riv|Brook|Burn|Canal|Stream|Beck|Water|Afon|Nant|Gill|Pool|Mere|Tarn|Lake)\b", re.I)
ANTIQ = re.compile(r"\b(Tumulus|Tumuli|Cairn|Camp|Earthwork|Barrow|Motte|Cist|Enclosure|Fort|Roman|Site of)\b", re.I)
BM = re.compile(r"^\s*B[. ]?M[. ]"); NUM = re.compile(r"^[\d\s.,]+$")

def tile(tx, ty):
    p = f"{TILES}/{tx}/{ty}.png"
    if os.path.exists(p) and os.path.getsize(p) > 500:
        try: return np.asarray(Image.open(p).convert("L"), np.uint8)
        except Exception: return None
    return None

def crop_global(box_g, pad=4):
    x0, y0, x1, y1 = box_g; x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    tx0, tx1, ty0, ty1 = x0 // 256, x1 // 256, y0 // 256, y1 // 256
    canvas = np.full(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), 255, np.uint8); ok = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = tile(tx, ty)
            if t is not None: canvas[(ty - ty0) * 256:(ty - ty0) * 256 + 256, (tx - tx0) * 256:(tx - tx0) * 256 + 256] = t; ok = True
    if not ok: return None
    L, U = x0 - tx0 * 256, y0 - ty0 * 256
    c = canvas[U:U + (y1 - y0), L:L + (x1 - x0)]
    return c.astype(np.float32) / 255.0 if c.size > 40 else None

def letterform(t):
    t = (t or "").strip()
    if not t: return None
    if BM.match(t) or (NUM.match(t) and any(ch.isdigit() for ch in t)): return "numeral"
    if ANTIQ.search(t): return "blackletter"
    if WATER.search(t): return "italic"
    al = [c for c in t if c.isalpha()]
    if t == t.upper() and len(al) >= 4: return "caps"
    if t[:1].isupper() and t != t.upper() and len(al) >= 4: return "upright"
    return None

def main():
    dev = "cpu"
    labels = []
    for f in glob.glob(f"{DISC}/labels_*.json"): labels += json.load(open(f))
    items = []
    for L in labels:
        if not L.get("crowd") or "box_g" not in L: continue
        lf = letterform(L["crowd"])
        if lf: items.append((L["box_g"], lf, L["crowd"]))
    print("matched+labelled:", dict(Counter(l for _, l, _ in items)), flush=True)

    def load(it):
        box, lf, tv = it
        c = crop_global(box)
        if c is None or c.shape[0] < 6 or c.shape[1] < 8: return None
        return (CD._to_h32(c), lf, tv)
    crops = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(load, items):
            if r: crops.append(r)
    # balance: cap each class so the CV isn't dominated by 'upright'
    byc = {}
    for im, lf, tv in crops: byc.setdefault(lf, []).append((im, lf, tv))
    CAP = 300
    bal = [x for lf, v in byc.items() if len(v) >= 12 for x in v[:CAP]]
    print("usable crops:", dict(Counter(x[1] for x in bal)), flush=True)

    net = CRNN(n_class=len(json.load(open(f"{MODEL}/vocab.json"))["stoi"]) + 1).to(dev)
    net.load_state_dict(torch.load(f"{MODEL}/crnn_z17.pt", map_location=dev)); net.eval()
    Xi = [x[0] for x in bal]; y = np.array([x[1] for x in bal]); txt = [x[2] for x in bal]
    Zc = crnn_embed(net, Xi, dev); tx = np.array([textfeats(t) for t in txt])
    F = np.hstack([Zc, tx])

    clf = lambda: make_pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=(64, 32), alpha=1e-2, max_iter=1000, random_state=0))
    pred = cross_val_predict(clf(), F, y, cv=StratifiedKFold(5, shuffle=True, random_state=0))
    classes = sorted(set(y))
    print(f"\n=== PREMISE TEST: font-class on CLEAN discovery boxes (5-fold CV, N={len(y)}) ===")
    print(f"overall accuracy: {(pred==y).mean():.3f}")
    print(f"{'class':12s} {'N':>4s} {'recall':>7s} {'precision':>10s}")
    for c in classes:
        m = y == c; p = pred == c
        rec = (pred[m] == c).mean() if m.sum() else 0
        prec = (y[p] == c).mean() if p.sum() else 0
        print(f"  {c:10s} {m.sum():>4d} {rec:>7.2f} {prec:>10.2f}")
    print("\nconfusion (row=true):")
    print("            " + " ".join(f"{c[:5]:>6s}" for c in classes))
    for c in classes:
        row = [int(((y == c) & (pred == d)).sum()) for d in classes]
        print(f"  {c:10s} " + " ".join(f"{v:>6d}" for v in row))
    # the key axis: serif upright vs italic (the ~0.72 crowd-point plateau)
    m = np.isin(y, ["upright", "italic"])
    if m.sum() > 20:
        p2 = cross_val_predict(clf(), F[m], y[m], cv=StratifiedKFold(5, shuffle=True, random_state=0))
        print(f"\nupright-vs-italic ONLY (the crowd-point ~0.72 plateau): acc={(p2==y[m]).mean():.3f} (N={m.sum()})")

if __name__ == "__main__":
    main()
