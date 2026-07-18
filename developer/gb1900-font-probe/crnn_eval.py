"""B' decisive eval: does the REAL-domain CRNN encoder separate font style better than the
synthetic StyleEncoder? Compares, on the 275 HITL anchors (leave-one-out MLP):
  synth+fusion vs crnn+fusion vs crnn-only vs synth-only  (per class),
and the key test: upright-vs-italic serif separability WITHIN a size band (controls the size confound).
    python crnn_eval.py --crnn out_bprime/crnn.pt --vocab out_bprime/vocab.json \
        --synenc out3/encoder.pt --boxes GLOB --tiles DIR... --labels font_labels.json --out out_bprime
"""
import argparse, os, json, math, numpy as np, torch
from collections import Counter
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from crnn import CRNN
from model import StyleEncoder
from embed_cluster import embed
from fusion import cap_h_m, textfeats, clf, per_class

def crnn_embed(net, imgs, dev, bs=128):
    out = []
    for i in range(0, len(imgs), bs):
        chunk = imgs[i:i + bs]
        W = max(im.shape[1] for im in chunk); W = max(W, 16)
        X = np.full((len(chunk), 1, CD.H, W), 1.0, np.float32)
        for j, im in enumerate(chunk):
            X[j, 0, :, :im.shape[1]] = im
        X = (X - X.mean(axis=(2, 3), keepdims=True)) / (X.std(axis=(2, 3), keepdims=True) + 1e-5)
        _, pooled = net.encode(torch.from_numpy(X).to(dev))
        out.append(pooled.cpu().numpy())
    return np.concatenate(out)

def loo(F, y):
    pred = cross_val_predict(clf(), F, y, cv=LeaveOneOut())
    return round(float((pred == y).mean()), 3), per_class(y, pred)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crnn", required=True); ap.add_argument("--vocab", required=True)
    ap.add_argument("--synenc", required=True); ap.add_argument("--boxes", required=True)
    ap.add_argument("--tiles", nargs="+", required=True); ap.add_argument("--labels", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--nmax", type=int, default=2500)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"

    vocab = json.load(open(a.vocab))
    crnn = CRNN(n_class=len(vocab["stoi"]) + 1).to(dev)
    crnn.load_state_dict(torch.load(a.crnn, map_location=dev)); crnn.eval()
    syn = StyleEncoder().to(dev); syn.load_state_dict(torch.load(a.synenc, map_location=dev)); syn.eval()

    pool, kept = DATA.load_real_and_kept(a.boxes, a.tiles, a.nmax, np.random.RandomState(0))
    imgs32 = [CD._to_h32(p) for p in pool]
    Zc = crnn_embed(crnn, imgs32, dev)                                   # real-domain (512)
    Zs = embed(syn, np.stack([DATA.norm1(p) for p in pool])[:, None].astype(np.float32), dev)  # synthetic (128)
    size = np.array([[math.log(max(1.0, cap_h_m(b["gpoly"])))] for b in kept])
    txt = np.array([textfeats(b.get("text", "")) for b in kept])
    caph = np.array([cap_h_m(b["gpoly"]) for b in kept])

    labels = json.load(open(a.labels))
    ai, ay = [], []
    for r in labels:
        if r["label"] == "ambiguous": continue
        i = int(r["id"].split("_")[-1])
        if i < len(pool): ai.append(i); ay.append(r["label"])
    ai = np.array(ai); ay = np.array(ay)
    print("anchors:", len(ai), dict(Counter(ay.tolist())), flush=True)

    Cc, Cs = Zc[ai], Zs[ai]; SZ, TX = size[ai], txt[ai]
    variants = {
        "synth_only":  Cs,
        "synth_fusion": np.hstack([Cs, SZ, TX]),
        "crnn_only":   Cc,
        "crnn_fusion": np.hstack([Cc, SZ, TX]),
    }
    rep = {}
    for name, F in variants.items():
        ov, pc = loo(F, ay); rep[name] = dict(overall=ov, per_class=pc)
        print(f"{name:14s} overall={ov}  serif_italic={pc.get('serif_italic')}  serif_upright={pc.get('serif_upright')}", flush=True)

    # KEY: upright vs italic serif, WITHIN each size band (control the size confound)
    band_report = {}
    m = np.isin(ay, ["serif_upright", "serif_italic"])
    for lo, hi, bname in [(0, 30, "small"), (30, 55, "medium"), (55, 1e9, "large"), (0, 1e9, "all")]:
        sel = m & (caph[ai] >= lo) & (caph[ai] < hi)
        yy = ay[sel]
        if len(set(yy)) < 2 or (yy == "serif_upright").sum() < 3 or (yy == "serif_italic").sum() < 3:
            band_report[bname] = None; continue
        entry = {"n": int(sel.sum())}
        for name, Z in [("synth", Zs), ("crnn", Zc)]:
            F = np.hstack([Z[ai][sel], size[ai][sel], txt[ai][sel]])
            pred = cross_val_predict(clf(), F, yy, cv=LeaveOneOut())
            entry[name] = round(float((pred == yy).mean()), 3)
        band_report[bname] = entry
    rep["upright_vs_italic_by_size"] = band_report
    print("upright-vs-italic within size band:", json.dumps(band_report), flush=True)

    json.dump(rep, open(os.path.join(a.out, "bprime_report.json"), "w"), indent=2)
    print("WROTE bprime_report.json", flush=True)

if __name__ == "__main__":
    main()
