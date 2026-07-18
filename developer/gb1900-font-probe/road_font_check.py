"""Prove road_caps is detected by FONT, not just the text hint. Train road_caps-vs-rest on the
VISUAL embedding ONLY (no text features), then surface high-confidence road_caps predictions whose
transcription has NO road word -> font-detected roads the text rule would miss. Montage them.
    python road_font_check.py --crnn out_z17/crnn_z17.pt --vocab out_z17/vocab.json --labels ... --out out_z17
"""
import argparse, os, json, re, numpy as np, torch
from collections import Counter
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from crnn import CRNN
from crnn_eval import crnn_embed
from embed_cluster import montage

TILES16 = ["/vast/ishi/gb1900/probe/mapreader_text/region/tiles", "/vast/ishi/gb1900/tiles/16"]
TILES17 = ["/vast/ishi/gb1900/tiles17"]
BOXES = "/vast/ishi/gb1900/probe/mapreader_text/region/boxes/worker*.jsonl"
ROADWORD = re.compile(r"\b(ROAD|STREET|LANE|TERRACE|AVENUE|ST|RD)\b", re.I)

def clf():
    return make_pipeline(StandardScaler(), MLPClassifier((32,), alpha=1e-2, max_iter=800, random_state=0))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crnn", required=True); ap.add_argument("--vocab", required=True)
    ap.add_argument("--labels", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    voc = json.load(open(a.vocab))
    net = CRNN(n_class=len(voc["stoi"]) + 1).to(dev); net.load_state_dict(torch.load(a.crnn, map_location=dev)); net.eval()

    pool, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    z17 = [DATA.crop_box(b["gpoly"], TILES17, scale=2) for b in kept]
    idx = [i for i, c in enumerate(z17) if c is not None]
    imgs = [CD._to_h32(z17[i]) for i in idx]
    Z = crnn_embed(net, imgs, dev)
    Zmap = {i: Z[k] for k, i in enumerate(idx)}

    # anchors: road_caps vs rest, VISUAL EMBEDDING ONLY
    labels = json.load(open(a.labels)); Xa, ya = [], []
    for r in labels:
        if r["label"] == "ambiguous": continue
        i = int(r["id"].split("_")[-1])
        if i in Zmap:
            Xa.append(Zmap[i]); ya.append("road_caps" if r["label"] == "road_caps" else "other")
    Xa = np.array(Xa); ya = np.array(ya)
    n_road = int((ya == "road_caps").sum())
    print("road anchors:", n_road, "other:", int((ya == "other").sum()), flush=True)
    # visual-only LOO recall/precision for road_caps
    pred = cross_val_predict(clf(), Xa, ya, cv=LeaveOneOut())
    tp = int(((pred == "road_caps") & (ya == "road_caps")).sum())
    fp = int(((pred == "road_caps") & (ya == "other")).sum())
    rec = round(tp / max(1, n_road), 3); prec = round(tp / max(1, tp + fp), 3)
    print(f"FONT-ONLY road_caps LOO: recall={rec} precision={prec}", flush=True)

    # apply font-only classifier to the whole pool; find road-style crops with NO road word
    model = clf().fit(Xa, ya)
    P = model.predict_proba(np.array([Zmap[i] for i in idx]))
    ci = list(model.classes_).index("road_caps")
    scored = sorted([(P[k, ci], idx[k]) for k in range(len(idx))], reverse=True)
    no_word = [(p, i) for p, i in scored if p > 0.5 and not ROADWORD.search(kept[i].get("text", "") or "")]
    print("font-detected road_caps with NO road word in text:", len(no_word), "of",
          int((P[:, ci] > 0.5).sum()), "total road_caps predictions", flush=True)
    sel = no_word[:40]
    montage([z17[i] for _, i in sel], ["%.2f|%s" % (p, kept[i].get("text", "")) for p, i in sel],
            os.path.join(a.out, "road_font_no_word.png"),
            "font-detected road_caps WITHOUT a road word (proves font, not text)")
    json.dump(dict(road_anchors=n_road, font_only_recall=rec, font_only_precision=prec,
                   road_predictions=int((P[:, ci] > 0.5).sum()), without_road_word=len(no_word)),
              open(os.path.join(a.out, "road_font_report.json"), "w"), indent=2)
    print("WROTE road_font_report.json", flush=True)

if __name__ == "__main__":
    main()
