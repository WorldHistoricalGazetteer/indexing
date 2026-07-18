"""Definitive serif upright/italic experiment with the SETTLEMENT-NAME lever (SG):
features = word-semantic + is_settlement_name (text matches parish/district gazetteer) + SIZE
(settlement names run larger) + case + text; plus the CRNN visual model; and a proper proba ENSEMBLE
(out-of-fold LOO). Tests whether semantic+size resolves the settlement-vs-farm split the pixels can't.
    python serif_push2.py --crnn out_z17/crnn_z17.pt --vocab out_z17/vocab.json --labels ... \
        --names admin_names.json --out out_z17
"""
import argparse, os, json, math, re, numpy as np, torch
from collections import Counter
from sklearn.neural_network import MLPClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from crnn import CRNN
from crnn_eval import crnn_embed
from serif_push import UPRIGHT, ITALIC, word_sem, TILES16, TILES17, BOXES
from fusion import textfeats

WATER = {"well", "spring", "ford", "weir", "brook", "marsh", "pond", "pool", "river", "canal", "reservoir"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crnn", required=True); ap.add_argument("--vocab", required=True)
    ap.add_argument("--labels", required=True); ap.add_argument("--names", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    nm = json.load(open(a.names)); NAMES = set(nm["names"]); TOKS = set(nm["tokens"])
    voc = json.load(open(a.vocab))
    net = CRNN(n_class=len(voc["stoi"]) + 1).to(dev); net.load_state_dict(torch.load(a.crnn, map_location=dev)); net.eval()

    def is_settlement(t):
        k = (t or "").strip().lower()
        if k in NAMES: return 1
        return int(any(len(w) >= 4 and w in TOKS for w in re.split(r"[^a-z]+", k)))

    _, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    labels = json.load(open(a.labels))
    items = []
    for r in labels:
        if r["label"] not in ("serif_upright", "serif_italic"): continue
        i = int(r["id"].split("_")[-1])
        if i >= len(kept): continue
        c = DATA.crop_box(kept[i]["gpoly"], TILES17, scale=2)
        if c is None: continue
        t = kept[i].get("text", "")
        items.append(dict(img=CD._to_h32(c), y=r["label"], text=t,
                          cap=CD.cap_h_m(kept[i]["gpoly"]) * 2))
    y = np.array([it["y"] for it in items]); N = len(items)
    print("serif anchors:", N, dict(Counter(y.tolist())), "| settlement-name matches:",
          int(sum(is_settlement(it["text"]) for it in items)), flush=True)

    # semantic feature vector (no pixels)
    def sem(it):
        return word_sem(it["text"]) + [math.log(max(1.0, it["cap"])), int((it["text"] or "").isupper()),
                                       is_settlement(it["text"]),
                                       int(it["text"].strip().lower() in ITALIC or it["text"].strip().lower() in WATER)] + textfeats(it["text"])
    Xsem = np.array([sem(it) for it in items])
    Zc = crnn_embed(net, [it["img"] for it in items], dev)

    def loo_proba(clf, X):
        return cross_val_predict(clf, X, y, cv=LeaveOneOut(), method="predict_proba")
    sem_clf = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, class_weight="balanced"))
    crnn_clf = make_pipeline(StandardScaler(), MLPClassifier((64,), alpha=1e-2, max_iter=1000, random_state=0))
    Ps = loo_proba(sem_clf, Xsem); Pc = loo_proba(crnn_clf, Zc)
    cls = list(sem_clf.fit(Xsem, y).classes_)

    def acc(P): return round(float((np.array([cls[i] for i in P.argmax(1)]) == y).mean()), 3)
    ens = (Ps + Pc) / 2
    rep = dict(n=N,
               semantic_only=acc(Ps),          # word-sem + is_settlement + size + case + text
               crnn_visual=acc(Pc),
               ensemble=acc(ens),
               settlement_matches=int(sum(is_settlement(it["text"]) for it in items)))
    print("SERIF v2 (LOO):", json.dumps(rep), flush=True)
    json.dump(rep, open(os.path.join(a.out, "serif_push2_report.json"), "w"), indent=2)
    print("WROTE serif_push2_report.json", flush=True)

if __name__ == "__main__":
    main()
