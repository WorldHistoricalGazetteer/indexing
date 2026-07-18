"""Definitive serif test: attack the CLEAN-ANCHOR bottleneck using the SPOTTER boxes (tight, same
crop style as the human anchors) auto-labelled by the word->style lexicon. Train the serif classifier
(crnn embedding + slant + word-semantics + size) on these abundant clean anchors, TEST on human labels.
If it generalises (>~.75), the plateau was anchor quantity and serif can be mass-produced from spotter
boxes; if it stays ~.7, the axis is intrinsically subtle at this scale.
    python serif_final.py --crnn out_z17/crnn_z17.pt --vocab out_z17/vocab.json --labels ... --out out_z17
"""
import argparse, os, json, math, glob, numpy as np, torch
from collections import Counter
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from crnn import CRNN
from crnn_eval import crnn_embed
from slant_v2 import slant_deg
from serif_push import word_sem, LEX, TILES16, TILES17, BOXES

def feats_for(net, dev, crops, texts, caps):
    E = crnn_embed(net, [CD._to_h32(c) for c in crops], dev)
    S = np.array([[slant_deg(c)] for c in crops])
    WS = np.array([word_sem(t) for t in texts])
    SZ = np.array([[math.log(max(1.0, cp))] for cp in caps])
    return np.hstack([E, S, WS, SZ]), E, S, WS, SZ

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crnn", required=True); ap.add_argument("--vocab", required=True)
    ap.add_argument("--labels", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    voc = json.load(open(a.vocab))
    net = CRNN(n_class=len(voc["stoi"]) + 1).to(dev); net.load_state_dict(torch.load(a.crnn, map_location=dev)); net.eval()

    # abundant CLEAN serif anchors from SPOTTER boxes (tight) via lexicon
    boxes = []
    for f in glob.glob(BOXES):
        for line in open(f):
            line = line.strip()
            if line: boxes.append(json.loads(line))
    lc, lt, lca, ly = [], [], [], []
    for b in boxes:
        t = (b.get("text") or "").strip()
        if t.lower() not in LEX: continue
        c = DATA.crop_box(b["gpoly"], TILES17, scale=2)
        if c is None: continue
        lc.append(c); lt.append(t); lca.append(CD.cap_h_m(b["gpoly"]) * 2); ly.append(LEX[t.lower()])
    ly = np.array(ly)
    print("clean lexicon serif anchors (spotter boxes):", len(lc), dict(Counter(ly.tolist())), flush=True)

    # human serif anchors (tight spotter boxes, z17)
    _, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    labels = json.load(open(a.labels)); hc, ht, hca, hy = [], [], [], []
    for r in labels:
        if r["label"] not in ("serif_upright", "serif_italic"): continue
        i = int(r["id"].split("_")[-1])
        if i >= len(kept): continue
        c = DATA.crop_box(kept[i]["gpoly"], TILES17, scale=2)
        if c is None: continue
        hc.append(c); ht.append(kept[i].get("text", "")); hca.append(CD.cap_h_m(kept[i]["gpoly"]) * 2); hy.append(r["label"])
    hy = np.array(hy)
    print("human serif anchors:", len(hc), dict(Counter(hy.tolist())), flush=True)

    Xl, El, Sl, WSl, SZl = feats_for(net, dev, lc, lt, lca)
    Xh, Eh, Sh, WSh, SZh = feats_for(net, dev, hc, ht, hca)

    def clf():
        return make_pipeline(StandardScaler(), MLPClassifier((64, 32), alpha=1e-2, max_iter=1000, random_state=0))
    def tt(Xtr, Xte):
        return round(float((clf().fit(Xtr, ly).predict(Xte) == hy).mean()), 3)
    rep = dict(
        n_clean=len(lc), n_human=len(hc),
        full_trainclean_testhuman=tt(Xl, Xh),
        crnn_only=tt(El, Eh),
        slant_wordsem_size=tt(np.hstack([Sl, WSl, SZl]), np.hstack([Sh, WSh, SZh])),
        crnn_slant=tt(np.hstack([El, Sl]), np.hstack([Eh, Sh])),
    )
    print("SERIF FINAL (train=clean spotter-box lexicon, test=HUMAN):", json.dumps(rep), flush=True)
    json.dump(rep, open(os.path.join(a.out, "serif_final_report.json"), "w"), indent=2)
    print("WROTE serif_final_report.json", flush=True)

if __name__ == "__main__":
    main()
