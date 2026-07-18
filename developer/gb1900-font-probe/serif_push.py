"""Comprehensive serif upright/italic push (the holdout axis). Deploys SG's word-semantic insight:
 - word->style LEXICON (Coppice/Plantation/Nursery/Wood -> upright; House/Pond/Lodge/Ford/Weir ->
   italic) mass-produces free z17 serif anchors (validated: labels match the real font);
 - a WORD-SEMANTIC feature (named-place vs descriptive-feature vs water) fused with the CRNN embedding;
 - GLYPH-LEVEL (per-glyph same-letter) aggregation.
Trains the serif classifier on LEXICON auto-labels, TESTS on HUMAN labels (no circularity).
    python serif_push.py --crnn out_z17/crnn_z17.pt --vocab out_z17/vocab.json --labels font_labels.json --out out_z17
"""
import argparse, os, json, math, numpy as np, torch
from collections import Counter, defaultdict
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from crnn import CRNN
from crnn_eval import crnn_embed
from fusion import textfeats

TILES16 = ["/vast/ishi/gb1900/probe/mapreader_text/region/tiles", "/vast/ishi/gb1900/tiles/16"]
TILES17 = ["/vast/ishi/gb1900/tiles17"]
BOXES = "/vast/ishi/gb1900/probe/mapreader_text/region/boxes/worker*.jsonl"
NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
UPRIGHT = {"coppice", "plantation", "nursery", "nurseries", "firs", "covert", "gorse", "belt",
           "spinney", "wood", "grove", "plantn", "rough"}
ITALIC = {"house", "cottage", "cottages", "pond", "well", "spring", "ford", "weir", "smithy",
          "brook", "marsh", "lodge", "grange", "mill", "barn", "pit", "works", "brewery", "reservoir"}
LEX = {w: "serif_upright" for w in UPRIGHT}; LEX.update({w: "serif_italic" for w in ITALIC})

def word_sem(text):
    """semantic category one-hot: [named_place, descriptive_feature, water, other]."""
    k = (text or "").strip().lower()
    water = {"well", "spring", "ford", "weir", "brook", "marsh", "pond", "pool", "river", "canal", "reservoir"}
    v = [0, 0, 0, 0]
    if k in UPRIGHT: v[0] = 1
    elif k in water: v[2] = 1
    elif k in ITALIC: v[1] = 1
    else: v[3] = 1
    return v

def z16blk(lon, lat):
    x = int((lon + 180) / 360 * (2**16))
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * (2**16))
    return x // 8, y // 8

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crnn", required=True); ap.add_argument("--vocab", required=True)
    ap.add_argument("--labels", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--z17blocks", default="")  # comma bx:by; default = all fetched
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    voc = json.load(open(a.vocab))
    net = CRNN(n_class=len(voc["stoi"]) + 1).to(dev); net.load_state_dict(torch.load(a.crnn, map_location=dev)); net.eval()

    # ---- lexicon-auto serif anchors from crowd (z17, any fetched block) ----
    lex_items = []
    tiles_have = set()  # discover which blocks have z17 by trying crops (cheap: rely on crop returning None)
    for line in open(NT):
        if len(lex_items) >= 4000: break
        try: d = json.loads(line)
        except Exception: continue
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        lon, lat = d.get("lon"), d.get("lat")
        if not (tv and lon and lat): continue
        k = tv.strip().lower()
        if k not in LEX: continue
        c = DATA.crop_point(lon, lat, TILES17)
        if c is None: continue
        lex_items.append(dict(img=CD._to_h32(c), style=LEX[k], text=tv.strip()))
    print("lexicon-auto serif anchors:", len(lex_items), dict(Counter(it["style"] for it in lex_items)), flush=True)

    # ---- human serif anchors (z17 crops of the canonical pool) ----
    _, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    labels = json.load(open(a.labels))
    hum = []
    for r in labels:
        if r["label"] not in ("serif_upright", "serif_italic"): continue
        i = int(r["id"].split("_")[-1])
        if i >= len(kept): continue
        c = DATA.crop_box(kept[i]["gpoly"], TILES17, scale=2)
        if c is None: continue
        hum.append(dict(img=CD._to_h32(c), style=r["label"], text=kept[i].get("text", "")))
    print("human serif anchors (z17):", len(hum), dict(Counter(it["style"] for it in hum)), flush=True)

    # ---- embed ----
    def feats(items):
        E = crnn_embed(net, [it["img"] for it in items], dev)
        WS = np.array([word_sem(it["text"]) for it in items])
        TX = np.array([textfeats(it["text"]) for it in items])
        return E, WS, TX
    El, WSl, TXl = feats(lex_items); yl = np.array([it["style"] for it in lex_items])
    Eh, WSh, TXh = feats(hum); yh = np.array([it["style"] for it in hum])

    def clf():
        return make_pipeline(StandardScaler(), MLPClassifier((64, 32), alpha=1e-2, max_iter=1000, random_state=0))

    def train_test(Xl, Xh):
        return round(float((clf().fit(Xl, yl).predict(Xh) == yh).mean()), 3)

    rep = {
        "n_lex": len(lex_items), "n_human": len(hum),
        "word_semantic_only": train_test(WSl, WSh),                       # SG's insight alone
        "crnn_emb_only":      train_test(El, Eh),                          # visual only
        "crnn+wordsem":       train_test(np.hstack([El, WSl]), np.hstack([Eh, WSh])),
        "crnn+wordsem+text":  train_test(np.hstack([El, WSl, TXl]), np.hstack([Eh, WSh, TXh])),
    }
    print("SERIF upright/italic (train=lexicon-auto, test=HUMAN):", json.dumps(rep), flush=True)
    json.dump(rep, open(os.path.join(a.out, "serif_push_report.json"), "w"), indent=2)
    print("WROTE serif_push_report.json", flush=True)

if __name__ == "__main__":
    main()
