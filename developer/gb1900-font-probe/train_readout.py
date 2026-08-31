"""Step 3 — the READOUT classifier, framed as PRECISION vs COVERAGE (the honest deployment question). The
backbone separates distinctive signatures on curated examples (blackletter 0.85) but most of the pool is
generic descriptive text where the font signature is genuinely ambiguous. So the useful question isn't a
global label — it's: at a confidence threshold, what FRACTION of the pool can font type, and how ACCURATELY?
High-confidence predictions should be the categories OS made typographically distinct (antiquities=blackletter,
boundary/height figures=italic/numeral plain); the ambiguous serif mass defers to content/gazetteer downstream.

Deployment classifier = UNBALANCED distance-weighted kNN (realistic bank proportions + vote-fraction confidence).
Out-of-fold CV gives the precision-coverage curve; then applied to all 113k -> bank_predictions.npz.

    sbatch -M htc build_train_readout.sbatch      # GPU-free, ~1 min
"""
import glob, json, numpy as np
from collections import Counter
from sklearn.model_selection import cross_val_predict, StratifiedKFold
from sklearn.neighbors import KNeighborsClassifier
from sklearn.metrics import balanced_accuracy_score, accuracy_score, confusion_matrix

SPOT = "/vast/ishi/gb1900/edition/spot"; HERE = "/vast/ishi/gb1900/probe/font"

def load_bank():
    ds, meta = [], {k: [] for k in ("gcx", "gcy", "lon", "lat", "text", "score")}
    for s in sorted(glob.glob(f"{SPOT}/desc/shard_*.npz")):
        d = np.load(s, allow_pickle=True); ds.append(d["desc"].astype(np.float32))
        for k in meta: meta[k].append(d[k])
    X = np.concatenate(ds)
    for k in meta: meta[k] = np.concatenate(meta[k])
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    return X, meta

X, meta = load_bank(); N = len(X); print(f"bank {N} words", flush=True)
idx = {(round(float(meta["gcx"][i]), 1), round(float(meta["gcy"][i]), 1)): i for i in range(N)}
lab = json.load(open(f"{HERE}/labels/pool_labels.json"))
A, S = [], []
for l in lab:
    if not l.get("sig"): continue
    i = idx.get((round(float(l["gcx"]), 1), round(float(l["gcy"]), 1)))
    if i is not None: A.append(X[i]); S.append(l["sig"])
A = np.array(A); S = np.array(S); sl = sorted(set(S))
print(f"{len(A)} anchors / {len(sl)} sigs: {dict(Counter(S))}\n", flush=True)

cv = StratifiedKFold(5, shuffle=True, random_state=0)
clf = KNeighborsClassifier(15, metric="cosine", weights="distance")
proba = cross_val_predict(clf, A, S, cv=cv, method="predict_proba")
classes = np.array(sorted(sl))                                  # KNN sorts classes alphabetically
pred = classes[proba.argmax(1)]; conf = proba.max(1)
print(f"overall CV: acc {accuracy_score(S,pred):.3f}  macro-recall {balanced_accuracy_score(S,pred):.3f}\n", flush=True)

print("PRECISION vs COVERAGE (out-of-fold, distance-weighted kNN vote fraction):")
print(f"  {'conf≥':>6} {'coverage':>9} {'accuracy':>9}  {'n':>5}")
for t in (0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
    m = conf >= t
    if m.sum() == 0: continue
    print(f"  {t:>6.1f} {m.mean()*100:>8.1f}% {(pred[m]==S[m]).mean():>8.2f}  {m.sum():>5}", flush=True)

print("\nper-signature: recall + accuracy of ITS high-conf (≥0.6) predictions:")
for s in sl:
    tr = S == s; rec = (pred[tr] == s).mean()                   # recall
    ph = (pred == s) & (conf >= 0.6); prec = (S[ph] == s).mean() if ph.sum() else float("nan")
    print(f"  {s:26s} recall {rec:.2f}  |  hi-conf preds {ph.sum():>2}, precision {prec:.2f}", flush=True)

# ONE-VS-REST detectors — the right deployment model: each distinctive signature gets its OWN precision-
# calibrated threshold (independent of the majority), so blackletter/figures aren't out-voted by the mass.
from sklearn.linear_model import LogisticRegression
print("\nONE-VS-REST detectors (precision≥0.85 calibrated on CV out-of-fold):")
print(f"  {'signature':26s} {'recall@P.85':>11} {'thresh':>7} {'bank detections':>16}")
ovr_bank = {}
for c in sl:
    y = (S == c).astype(int)
    ovr = LogisticRegression(max_iter=2000, class_weight="balanced")
    pcv = cross_val_predict(ovr, A, y, cv=cv, method="predict_proba")[:, 1]
    best_t, best_r = None, 0.0
    for t in np.linspace(0.5, 0.99, 60):
        pm = pcv >= t
        if pm.sum() == 0: continue
        if y[pm].mean() >= 0.85 and y[pm].sum() / y.sum() > best_r: best_t, best_r = float(t), y[pm].sum() / y.sum()
    ovr.fit(A, y); bs = ovr.predict_proba(X)[:, 1]; ovr_bank[c] = bs.astype(np.float32)
    ndet = int((bs >= best_t).sum()) if best_t else 0
    tt = f"{best_t:.2f}" if best_t else "  —"
    print(f"  {c:26s} {best_r:>11.2f} {tt:>7} {ndet:>10} ({ndet/N*100:>4.1f}%)", flush=True)

# deploy: fit on all anchors, apply to the whole bank (multiclass kept for reference)
clf.fit(A, S); bp = clf.predict(X); bconf = clf.predict_proba(X).max(1)
np.savez_compressed(f"{SPOT}/bank_predictions.npz",
    gcx=meta["gcx"], gcy=meta["gcy"], lon=meta["lon"].astype(np.float32), lat=meta["lat"].astype(np.float32),
    text=meta["text"].astype(object), score=meta["score"].astype(np.float32),
    pred_sig=bp.astype(object), conf=bconf.astype(np.float32),
    **{f"ovr_{c.replace('·','_')}": ovr_bank[c] for c in sl})
print(f"\nBANK ({N} words) — full vs high-confidence (≥0.6) predicted-signature mix:")
hi = bconf >= 0.6
print(f"  high-conf covers {hi.mean()*100:.0f}% of bank ({hi.sum()} words); median conf {np.median(bconf):.2f}")
for s, n in Counter(bp).most_common():
    hn = ((bp == s) & hi).sum()
    print(f"  {n:>6} ({n/N*100:>4.1f}%) all | {hn:>5} hi-conf   {s}", flush=True)
print(f"wrote {SPOT}/bank_predictions.npz\nREADOUTDONE", flush=True)
