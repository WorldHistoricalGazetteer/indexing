"""Fusion head (SG idea): type each label from MULTIPLE signals, not the font embedding alone.
Features = [font embedding 128 | log cap-height (size) | allcaps (case) | text-features].
(os_style is empty in tier-0, so the "hints" are the signals that actually exist: size, case,
text — derivable from the spotter box itself, no crowd join needed.)

Evaluates fusion vs embedding-only by leave-one-out on the anchors (per class), then fits on
all anchors and classifies every real crop -> per-predicted-class montages + counts.
    python fusion.py --enc out3/encoder.pt --boxes GLOB --tiles DIR... --labels font_labels.json --out out3
"""
import argparse, os, json, math, numpy as np, torch
from collections import Counter
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA
from model import StyleEncoder
from embed_cluster import montage, embed

def cap_h_m(gpoly):
    ys = [p[1] for p in gpoly]
    yy = (min(ys) + max(ys)) / 2 / 256.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / (2**16)))))
    return (max(ys) - min(ys)) * 40075016.686 * math.cos(math.radians(lat)) / (2**24)

def textfeats(t):
    t = t or ""
    al = [c for c in t if c.isalpha()]
    return [len(t), int(any(c.isdigit() for c in t)),
            int(t.isupper() and len(al) > 0),                       # allcaps (case)
            sum(1 for c in t if not c.isalnum() and not c.isspace()) / max(1, len(t)),
            int(len(al) <= 2)]                                      # short mark

def clf():
    return make_pipeline(StandardScaler(),
                         MLPClassifier(hidden_layer_sizes=(64, 32), alpha=1e-2,
                                       max_iter=1000, random_state=0))

def per_class(y, pred):
    out = {}
    for c in sorted(set(y)):
        m = y == c
        out[c] = dict(n=int(m.sum()), acc=round(float((pred[m] == c).mean()), 3))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", required=True); ap.add_argument("--boxes", required=True)
    ap.add_argument("--tiles", nargs="+", required=True); ap.add_argument("--labels", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--nmax", type=int, default=2500)
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "fusion"), exist_ok=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    net = StyleEncoder().to(dev); net.load_state_dict(torch.load(a.enc, map_location=dev)); net.eval()

    pool, kept = DATA.load_real_and_kept(a.boxes, a.tiles, a.nmax, np.random.RandomState(0))
    Zr = embed(net, np.stack([DATA.norm1(p) for p in pool])[:, None].astype(np.float32), dev)
    size = np.array([[math.log(max(1.0, cap_h_m(b["gpoly"])))] for b in kept])
    txt = np.array([textfeats(b.get("text", "")) for b in kept])
    Ffull = np.hstack([Zr, size, txt])                              # fusion features
    print("real:", len(pool), "feat dim:", Ffull.shape[1], flush=True)

    labels = json.load(open(a.labels))
    ai, ay = [], []
    for r in labels:
        if r["label"] == "ambiguous": continue
        i = int(r["id"].split("_")[-1])
        if i < len(pool): ai.append(i); ay.append(r["label"])
    ai = np.array(ai); ay = np.array(ay)
    print("anchors:", len(ai), dict(Counter(ay.tolist())), flush=True)

    # leave-one-out: fusion vs embedding-only
    loo = LeaveOneOut()
    pred_fusion = cross_val_predict(clf(), Ffull[ai], ay, cv=loo)
    pred_embed = cross_val_predict(clf(), Zr[ai], ay, cv=loo)
    rep = dict(
        overall_fusion=round(float((pred_fusion == ay).mean()), 3),
        overall_embed=round(float((pred_embed == ay).mean()), 3),
        per_class_fusion=per_class(ay, pred_fusion),
        per_class_embed=per_class(ay, pred_embed))
    print("OVERALL  fusion %.3f  vs embed-only %.3f" % (rep["overall_fusion"], rep["overall_embed"]), flush=True)
    print("per-class fusion:", json.dumps(rep["per_class_fusion"]), flush=True)
    print("per-class embed :", json.dumps(rep["per_class_embed"]), flush=True)

    # fit on all anchors, classify every real crop
    model = clf().fit(Ffull[ai], ay)
    pred = model.predict(Ffull)
    proba = model.predict_proba(Ffull).max(1)
    counts = dict(Counter(pred.tolist()))
    rep["assignment_counts"] = counts
    print("assignment counts:", counts, flush=True)
    for c in sorted(set(pred)):
        idx = [i for i in np.argsort(-proba) if pred[i] == c][:60]
        montage([pool[i] for i in idx], ["%.2f|%s" % (proba[i], kept[i].get("text", "")) for i in idx],
                os.path.join(a.out, "fusion", f"fpred_{c}.png"), f"FUSION predicted {c} (n={counts.get(c,0)})")
    json.dump(rep, open(os.path.join(a.out, "fusion_report.json"), "w"), indent=2)
    print("WROTE fusion_report.json", flush=True)

if __name__ == "__main__":
    main()
