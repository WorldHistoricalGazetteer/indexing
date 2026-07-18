"""THE PUSH at z17: train a real-domain CRNN on z17 crops (region common fonts + antiquity
blackletter + urban caps, auto-labelled), then evaluate font-style with the rare-font anchors
that z17 targeting finally provides. Compares to the synthetic encoder; per class + within size band.

    python z17_run.py --out out_z17 --synenc out3/encoder.pt --epochs 40
"""
import argparse, os, json, time, math, numpy as np, torch, torch.nn as nn
from collections import Counter
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from crnn import CRNN
from model import StyleEncoder
from embed_cluster import embed as syn_embed
from crnn_eval import crnn_embed
from fusion import textfeats, clf, per_class

TILES16 = ["/vast/ishi/gb1900/probe/mapreader_text/region/tiles", "/vast/ishi/gb1900/tiles/16"]
TILES17 = ["/vast/ishi/gb1900/tiles17"]
BOXES = "/vast/ishi/gb1900/probe/mapreader_text/region/boxes/worker*.jsonl"
NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
ANTIQ = [(4083, 2619), (4078, 2628), (4037, 2753), (4052, 2727), (4054, 2736), (4085, 2619)]
URBAN = [(4044, 2650), (4045, 2650), (4044, 2649), (4045, 2649)]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--synenc", required=True)
    ap.add_argument("--labels", default="/vast/ishi/gb1900/probe/font/font_labels.json")
    ap.add_argument("--epochs", type=int, default=40); ap.add_argument("--bs", type=int, default=64)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"; rng = np.random.RandomState(0)

    # ---- harvest z17 training data ----
    reg = CD.harvest(BOXES, TILES17, scale=2, rng=rng)
    antiq = CD.harvest_crowd(NT, ANTIQ, TILES17, nmax=4000)
    urban = CD.harvest_crowd(NT, URBAN, TILES17, nmax=4000)
    train = [it for it in reg + antiq + urban if it["text"]]
    print("z17 train:", len(train), "(reg %d, antiq %d, urban %d)" % (len(reg), len(antiq), len(urban)), flush=True)
    print("auto-style in crowd:", dict(Counter(it["style"] for it in antiq + urban)), flush=True)
    stoi, itos = CD.build_vocab(train)
    json.dump({"stoi": stoi}, open(os.path.join(a.out, "vocab.json"), "w"))

    # ---- train CRNN ----
    net = CRNN(n_class=len(stoi) + 1).to(dev)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3); ctc = nn.CTCLoss(blank=0, zero_infinity=True)
    t0 = time.time()
    for ep in range(a.epochs):
        net.train(); rng.shuffle(train); tot = 0.0; nb = 0
        for i in range(0, len(train), a.bs):
            X, tgt, tlen, _ = CD.collate(train[i:i + a.bs], stoi)
            X = torch.from_numpy(X).to(dev); logp = net(X); T = logp.size(0)
            loss = ctc(logp, torch.from_numpy(tgt), torch.full((X.size(0),), T, dtype=torch.long), torch.from_numpy(tlen))
            opt.zero_grad(); loss.backward(); nn.utils.clip_grad_norm_(net.parameters(), 5.0); opt.step()
            tot += float(loss); nb += 1
        if ep % 10 == 0 or ep == a.epochs - 1:
            print(f"ep {ep} loss {tot/max(1,nb):.3f} ({time.time()-t0:.0f}s)", flush=True)
    torch.save(net.state_dict(), os.path.join(a.out, "crnn_z17.pt")); net.eval()

    # ---- eval: z17 crops of the canonical z16 pool (anchor ids align) ----
    _, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    z17 = [DATA.crop_box(b["gpoly"], TILES17, scale=2) for b in kept]
    caph = [CD.cap_h_m(b["gpoly"]) * 2 for b in kept]
    med = float(np.median([c for c in caph]))
    labels = json.load(open(a.labels))
    Xi, ya, sz, tx = [], [], [], []
    def add(img, lab, cap, text):
        Xi.append(CD._to_h32(img) if img.shape[0] != CD.H else img)
        ya.append(lab); sz.append([math.log(max(1.0, cap))]); tx.append(textfeats(text))
    for r in labels:
        if r["label"] == "ambiguous": continue
        i = int(r["id"].split("_")[-1])
        if i < len(z17) and z17[i] is not None:
            add(z17[i], r["label"], caph[i], kept[i].get("text", ""))
    # add auto rare-font anchors (blackletter from antiquities; caps_spaced from urban)
    bl = [it for it in antiq if it["style"] == "blackletter"][:80]
    cs = [it for it in urban if it["style"] == "caps_spaced"][:50]
    for it in bl: add(it["img"], "blackletter", med, it["text"])
    for it in cs: add(it["img"], "caps_spaced", med, it["text"])
    ya = np.array(ya); sz = np.array(sz); tx = np.array(tx)
    print("z17 anchors:", len(ya), dict(Counter(ya.tolist())), flush=True)

    Zc = crnn_embed(net, Xi, dev)
    F = np.hstack([Zc, sz, tx])
    pred = cross_val_predict(clf(), F, ya, cv=LeaveOneOut())
    rep = dict(overall=round(float((pred == ya).mean()), 3), per_class=per_class(ya, pred),
               train_n=len(train), auto_style=dict(Counter(it["style"] for it in antiq + urban)))
    # within-size-band upright vs italic (human anchors only)
    caph_arr = np.array([math.exp(s[0]) for s in sz])
    band = {}
    m = np.isin(ya, ["serif_upright", "serif_italic"])
    for lo, hi, bn in [(0, 60, "small"), (60, 110, "medium"), (0, 1e9, "all")]:
        sel = m & (caph_arr >= lo) & (caph_arr < hi)
        yy = ya[sel]
        if (yy == "serif_upright").sum() >= 3 and (yy == "serif_italic").sum() >= 3:
            p = cross_val_predict(clf(), F[sel], yy, cv=LeaveOneOut())
            band[bn] = dict(n=int(sel.sum()), acc=round(float((p == yy).mean()), 3))
        else: band[bn] = None
    rep["upright_vs_italic_by_size"] = band
    json.dump(rep, open(os.path.join(a.out, "z17_report.json"), "w"), indent=2)
    print("Z17 RESULT overall", rep["overall"], flush=True)
    print("per_class:", json.dumps(rep["per_class"]), flush=True)
    print("upright/italic by size:", json.dumps(band), flush=True)

if __name__ == "__main__":
    main()
