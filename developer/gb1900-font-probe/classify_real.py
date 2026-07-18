"""Proper iter-3 evaluation: classify every real crop by anchor-kNN (nearest human-labelled
anchors), not unsupervised clustering. Emits per-predicted-class montages + per-class anchor
leave-one-out accuracy + assignment counts. Meaningful only for classes with enough anchors.

    python classify_real.py --enc out3/encoder.pt --boxes GLOB --tiles DIR... \
        --labels font_labels.json --out out3 --nmax 2500
"""
import argparse, os, json, numpy as np, torch
from collections import Counter
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import LeaveOneOut
import data as DATA
from model import StyleEncoder
from embed_cluster import montage, embed

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", required=True); ap.add_argument("--boxes", required=True)
    ap.add_argument("--tiles", nargs="+", required=True); ap.add_argument("--labels", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--nmax", type=int, default=2500)
    ap.add_argument("--k", type=int, default=5); ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "classified"), exist_ok=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    net = StyleEncoder().to(dev); net.load_state_dict(torch.load(a.enc, map_location=dev)); net.eval()

    pool, kept = DATA.load_real_and_kept(a.boxes, a.tiles, a.nmax, np.random.RandomState(a.seed))
    Xa, ya = DATA.load_anchors(a.labels, pool)
    print("real:", len(pool), "anchors:", len(Xa), dict(Counter(ya)), flush=True)
    Za = embed(net, Xa.astype(np.float32), dev)
    Zr = embed(net, np.stack([DATA.norm1(p) for p in pool])[:, None].astype(np.float32), dev)

    # per-class anchor leave-one-out (only classes with >=3 anchors are meaningful)
    yn = np.array(ya); loo = {}
    for cls in sorted(set(ya)):
        m = yn == cls
        if m.sum() < 3: loo[cls] = None; continue
        preds = []
        for tr, te in LeaveOneOut().split(Za):
            k = min(a.k, len(tr))
            preds.append(KNeighborsClassifier(k).fit(Za[tr], yn[tr]).predict(Za[te])[0])
        preds = np.array(preds)
        loo[cls] = round(float((preds[m] == cls).mean()), 3)

    # classify all real crops by anchor-kNN + confidence (share of k that agree)
    k = min(a.k, len(Za))
    knn = KNeighborsClassifier(k).fit(Za, yn)
    pred = knn.predict(Zr)
    proba = knn.predict_proba(Zr).max(1)
    counts = dict(Counter(pred.tolist()))
    print("real assignment counts:", counts, flush=True)
    print("per-class anchor LOO:", loo, flush=True)

    # montage per predicted class (highest-confidence first)
    for cls in sorted(set(pred)):
        idx = [i for i in np.argsort(-proba) if pred[i] == cls][:60]
        montage([pool[i] for i in idx], ["%.2f|%s" % (proba[i], kept[i].get("text", "")) for i in idx],
                os.path.join(a.out, "classified", f"pred_{cls}.png"),
                f"predicted {cls}  (n={counts.get(cls,0)}, anchor-LOO={loo.get(cls)})")
    json.dump(dict(anchors=dict(Counter(ya)), assignment_counts=counts, anchor_loo=loo),
              open(os.path.join(a.out, "classify_report.json"), "w"), indent=2)
    print("WROTE classify_report.json", flush=True)

if __name__ == "__main__":
    main()
