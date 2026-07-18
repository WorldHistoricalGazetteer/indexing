"""Serif upright/italic ROUTING ENSEMBLE (production logic) — LOO on human anchors:
 - if the word is a known named-place/descriptive/water term -> word-semantic RULE;
 - else (novel proper name) -> GLYPH-LEVEL vote (per-glyph classifier, word-level majority).
Compares to each signal alone. This is how the serif axis actually gets typed.
    python serif_ensemble.py --crnn out_z17/crnn_z17.pt --vocab out_z17/vocab.json --labels ... --out out_z17
"""
import argparse, os, json, numpy as np, torch
from collections import Counter
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
import data as DATA, crnn_data as CD
from crnn import CRNN
from serif_push import UPRIGHT, ITALIC, word_sem, TILES16, TILES17, BOXES

WATER = {"well", "spring", "ford", "weir", "brook", "marsh", "pond", "pool", "river", "canal", "reservoir"}

def word_rule(text):
    k = (text or "").strip().lower()
    if k in UPRIGHT: return "serif_upright"
    if k in ITALIC or k in WATER: return "serif_italic"
    return None                       # unknown -> defer to glyph

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
    words = []      # (text, style, [glyph_embs])
    for r in labels:
        if r["label"] not in ("serif_upright", "serif_italic"): continue
        i = int(r["id"].split("_")[-1])
        if i >= len(kept): continue
        c = DATA.crop_box(kept[i]["gpoly"], TILES17, scale=2)
        if c is None: continue
        img = CD._to_h32(c); X = ((img - img.mean()) / (img.std() + 1e-5))[None, None].astype(np.float32)
        gl = net.per_glyph(torch.from_numpy(X).to(dev))[0]
        words.append((kept[i].get("text", ""), r["label"], [e for _, e in gl]))
    y = np.array([w[1] for w in words]); N = len(words)
    print("serif words:", N, dict(Counter(y.tolist())), flush=True)

    # glyph-level word-vote, LOO
    def glyph_pred(hold):
        Xtr = np.array([e for j, w in enumerate(words) if j != hold for e in w[2]])
        ytr = np.array([w[1] for j, w in enumerate(words) if j != hold for _ in w[2]])
        if not len(words[hold][2]): return "serif_italic"
        m = clf().fit(Xtr, ytr)
        votes = Counter(m.predict(np.array(words[hold][2])))
        return votes.most_common(1)[0][0]

    glyph_ok = word_ok = ens_ok = 0; known = 0
    for j in range(N):
        gp = glyph_pred(j)
        wr = word_rule(words[j][0])
        ep = wr if wr is not None else gp     # routing ensemble
        if wr is not None: known += 1; word_ok += int(wr == y[j])
        glyph_ok += int(gp == y[j]); ens_ok += int(ep == y[j])
    rep = dict(n=N, known_word=known,
               glyph_only_acc=round(glyph_ok / N, 3),
               word_rule_acc_on_known=round(word_ok / max(1, known), 3),
               routing_ensemble_acc=round(ens_ok / N, 3))
    print("SERIF ENSEMBLE:", json.dumps(rep), flush=True)
    json.dump(rep, open(os.path.join(a.out, "serif_ensemble_report.json"), "w"), indent=2)
    print("WROTE serif_ensemble_report.json", flush=True)

if __name__ == "__main__":
    main()
