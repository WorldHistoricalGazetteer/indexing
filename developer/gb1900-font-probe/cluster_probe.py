"""RETHINK probe — can an UNSUPERVISED style embedding recover the 16 typographic signatures, so we label
CLUSTERS not crops? Embeds the ~90k-word spotter pool with (a) raw-raster and (b) the within-word contrastive
SSL encoder, MEAN-POOLS each word's glyphs (averages out letter identity -> font style), clusters, and scores
against SIGNATURE using the human alphabet labels as ground truth. Emits per-cluster montages so we can SEE
whether clusters are stylistically coherent (the real test).

    /vast/ishi/envs/boundary/bin/python cluster_probe.py --k 18
"""
import argparse, json, os, numpy as np
from collections import Counter, defaultdict
import torch, torch.nn as nn
from PIL import Image, ImageDraw

SPOT = "/vast/ishi/gb1900/edition/spot"; HERE = "/vast/ishi/gb1900/probe/font"
CACHE = os.environ.get("CACHE") or f"{SPOT}/ssl_glyphs.npz"
ENC = os.environ.get("ENC") or f"{SPOT}/encoder_full.pt"
TAX = {f["key"]: (f["base_style"], f.get("fill"), f.get("decor")) for f in json.load(open(f"{HERE}/font_taxonomy.json"))}
def sig(face): return "·".join(str(x) for x in TAX.get(face, (face, "", "")))

class Enc(nn.Module):
    def __init__(self, d=64):
        super().__init__()
        self.c = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(), nn.AdaptiveAvgPool2d(1))
        self.drop = nn.Dropout(0); self.f = nn.Linear(128, d)
    def forward(self, x):
        z = self.f(self.drop(self.c(x).flatten(1))); return z / (z.norm(dim=1, keepdim=True) + 1e-8)

def ssl_embed(glyphs):
    glyphs = fix_size(glyphs, 40, 40)                          # encoder trained on 40x40
    net = Enc(); net.load_state_dict(torch.load(ENC, map_location="cpu")); net.eval()
    dev = "cuda" if torch.cuda.is_available() else "cpu"; net.to(dev); out = []
    with torch.no_grad():
        for i in range(0, len(glyphs), 2048):
            xb = glyphs[i:i + 2048].astype(np.float32)[:, None] / 255.0
            out.append(net(torch.tensor((xb - 0.8) / 0.3).to(dev)).cpu().numpy())
    return np.concatenate(out)

import cv2
def fix_size(glyphs, H=44, W=36):                               # unify raster size (pool & labelled may differ)
    if glyphs.shape[1:] == (H, W): return glyphs
    return np.stack([cv2.resize(g, (W, H), interpolation=cv2.INTER_AREA) for g in glyphs])

def raster_embed(glyphs):                                       # baseline: flattened binarised raster
    g = (fix_size(glyphs) > 128).astype(np.float32).reshape(len(glyphs), -1)
    return g / (np.linalg.norm(g, axis=1, keepdims=True) + 1e-9)

def pool_words(emb, wid):
    ws = sorted(set(wid.tolist())); M, WL = [], []
    for w in ws:
        m = emb[wid == w].mean(0); M.append(m / (np.linalg.norm(m) + 1e-9)); WL.append(w)
    return np.array(M, np.float32), np.array(WL)

def loo_sig_knn(emb, sigs, k=5):                               # leave-one-out: do same-signature words neighbour?
    S = emb @ emb.T; np.fill_diagonal(S, -2); ok = 0
    for i in range(len(emb)):
        nn_ = np.argsort(-S[i])[:k]; ok += (Counter(sigs[j] for j in nn_).most_common(1)[0][0] == sigs[i])
    return ok / max(1, len(emb))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--k", type=int, default=18); a = ap.parse_args()
    d = np.load(CACHE, allow_pickle=True); glyphs, wid = d["glyphs"], d["wid"]
    print(f"pool glyphs {len(glyphs)} across {len(set(wid.tolist()))} words", flush=True)
    lab = np.load(f"{HERE}/labels/alphabet_glyphs.npz", allow_pickle=True)
    lg, lface, lword = lab["glyphs"], lab["faces"], lab["word"]
    lsig = np.array([sig(f) for f in lface])
    from sklearn.cluster import KMeans
    from sklearn.metrics import adjusted_rand_score, homogeneity_score
    for name, embfn in (("RASTER", raster_embed), ("SSL", ssl_embed)):
        gp = embfn(glyphs); Wp, WLp = pool_words(gp, wid)                       # pool word embeddings
        gl = embfn(lg); Wl, WLl = pool_words(gl, lword)                          # labelled word embeddings
        wl_sig = np.array([lsig[lword == w][0] for w in WLl])
        knn = loo_sig_knn(Wl, wl_sig, k=5)
        km = KMeans(n_clusters=a.k, n_init=6, random_state=0).fit(Wp)
        cl_lab = km.predict(Wl)
        ari = adjusted_rand_score(wl_sig, cl_lab); hom = homogeneity_score(wl_sig, cl_lab)
        print(f"\n=== {name} ===  LOO same-signature kNN(5) on labelled words: {knn:.3f}")
        print(f"    KMeans(k={a.k}) on pool -> labelled words: ARI {ari:.3f}  homogeneity {hom:.3f}")
        # montage: per cluster, a sample of pool-word FIRST glyph rasters (shows style coherence)
        if name == "SSL":
            cl_pool = km.labels_; by = defaultdict(list)
            first_glyph = {}
            for gi in range(len(glyphs)):                                       # one representative glyph per word
                w = wid[gi]
                if w not in first_glyph: first_glyph[w] = glyphs[gi]
            for wi2, w in enumerate(WLp): by[cl_pool[wi2]].append(first_glyph[w])
            ch = 40; cols = 24; rows = a.k
            cvs = Image.new("RGB", (140 + cols * (ch + 2), rows * (ch + 6)), (255, 255, 255)); dr = ImageDraw.Draw(cvs)
            for c in range(a.k):
                y = c * (ch + 6); dr.text((4, y + ch // 2), f"cl{c} ({len(by[c])})", fill=(0, 0, 0))
                for j, g in enumerate(by[c][:cols]):
                    im = Image.fromarray(g).convert("L"); im.thumbnail((ch, ch))
                    cvs.paste(im, (140 + j * (ch + 2), y))
            cvs.save(f"{SPOT}/cluster_montage.png"); print(f"    wrote {SPOT}/cluster_montage.png")

if __name__ == "__main__":
    main()
