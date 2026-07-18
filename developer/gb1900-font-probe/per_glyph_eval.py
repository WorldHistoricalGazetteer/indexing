"""Last-axis lever: does comparing the SAME LETTER across fonts (glyph-level, content-controlled)
separate upright vs italic serif better than the whole-word embedding (~0.63)? Uses the z17 CRNN's
per-glyph embeddings (CTC alignment). SG's letter-level instinct, tested on the one holdout axis.
    python per_glyph_eval.py --crnn out_z17/crnn_z17.pt --vocab out_z17/vocab.json --labels ... --out out_z17
"""
import argparse, os, json, numpy as np, torch
from collections import defaultdict, Counter
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict, LeaveOneOut
import data as DATA, crnn_data as CD
from crnn import CRNN

TILES16 = ["/vast/ishi/gb1900/probe/mapreader_text/region/tiles", "/vast/ishi/gb1900/tiles/16"]
TILES17 = ["/vast/ishi/gb1900/tiles17"]
BOXES = "/vast/ishi/gb1900/probe/mapreader_text/region/boxes/worker*.jsonl"

def clf():
    return make_pipeline(StandardScaler(), MLPClassifier((32,), alpha=1e-2, max_iter=800, random_state=0))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crnn", required=True); ap.add_argument("--vocab", required=True)
    ap.add_argument("--labels", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    voc = json.load(open(a.vocab)); itos = {v: k for k, v in voc["stoi"].items()}
    net = CRNN(n_class=len(voc["stoi"]) + 1).to(dev); net.load_state_dict(torch.load(a.crnn, map_location=dev)); net.eval()

    _, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    labels = json.load(open(a.labels))
    serif = {int(r["id"].split("_")[-1]): r["label"] for r in labels
             if r["label"] in ("serif_upright", "serif_italic")}

    # per-letter glyph embeddings + per-word glyph list
    letters = defaultdict(list)          # letter -> [(emb, style)]
    word_glyphs = []                     # (style, [emb,...])
    for i, style in serif.items():
        if i >= len(kept): continue
        c = DATA.crop_box(kept[i]["gpoly"], TILES17, scale=2)
        if c is None: continue
        img = CD._to_h32(c)
        X = ((img - img.mean()) / (img.std() + 1e-5))[None, None].astype(np.float32)
        gl = net.per_glyph(torch.from_numpy(X).to(dev))[0]
        embs = []
        for cid, emb in gl:
            ch = itos.get(cid, "?"); letters[ch.lower()].append((emb, style)); embs.append(emb)
        if embs: word_glyphs.append((style, embs))
    print("serif anchors with glyphs:", len(word_glyphs), flush=True)

    # 1. per-letter same-letter separability (upright vs italic), letters with >=5 each
    per_letter = {}
    for ch, items in sorted(letters.items()):
        y = np.array([s for _, s in items])
        if (y == "serif_upright").sum() >= 5 and (y == "serif_italic").sum() >= 5:
            E = np.array([e for e, _ in items])
            pred = cross_val_predict(clf(), E, y, cv=LeaveOneOut())
            per_letter[ch] = dict(n=len(y), acc=round(float((pred == y).mean()), 3))
    print("per-letter upright/italic acc:", json.dumps(per_letter), flush=True)

    # 2. pooled glyph-level (all letters together) vs 3. word-level (mean of glyphs)
    Eg = np.array([e for _, its in [(s, es) for s, es in word_glyphs] for e in its])
    yg = np.array([s for s, es in word_glyphs for _ in es])
    glyph_acc = None
    if len(set(yg)) == 2:
        p = cross_val_predict(clf(), Eg, yg, cv=LeaveOneOut())
        glyph_acc = round(float((p == yg).mean()), 3)
    Ew = np.array([np.mean(es, 0) for s, es in word_glyphs]); yw = np.array([s for s, es in word_glyphs])
    word_acc = None
    if len(set(yw)) == 2:
        p = cross_val_predict(clf(), Ew, yw, cv=LeaveOneOut())
        word_acc = round(float((p == yw).mean()), 3)
    rep = dict(n_words=len(word_glyphs), per_letter=per_letter,
               glyph_level_pooled=glyph_acc, word_level_meanglyph=word_acc)
    print("POOLED glyph-level acc:", glyph_acc, " word-level(mean-glyph) acc:", word_acc, flush=True)
    json.dump(rep, open(os.path.join(a.out, "per_glyph_report.json"), "w"), indent=2)
    print("WROTE per_glyph_report.json", flush=True)

if __name__ == "__main__":
    main()
