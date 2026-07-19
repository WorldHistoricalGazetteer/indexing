"""Phase C — LEARNED font-style discriminator on REAL MapReader crops + HUMAN labels (the untried path).

Earlier discriminators failed for known reasons: CRNN embed is recognition-INVARIANT (wrong features);
synthetic StyleEncoder hit the domain gap. This trains a small CNN DIRECTLY on real de-rotated spotter
GLYPHS with human font labels — no recognition-invariance, no synthetic gap. Glyph-level (not word-level)
so content is controlled: the same letter appears across fonts, forcing the net onto STYLE. Aggregated per
word, evaluated 5-fold BY WORD (no glyph of a test word in training). Compares to the 0.78 raster-kNN.

    /vast/ishi/envs/boundary/bin/python disc_train.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import json, numpy as np, cv2
from collections import Counter, defaultdict
import torch, torch.nn as nn
from make_font_testset_v2 import derotate

DEC = "/vast/ishi/gb1900/probe/font/font_testset_decisions_1.json"
BOXES = "/vast/ishi/gb1900/edition/spot/font_testset_v2_boxes.json"   # frozen sample (pool-independent)
VOC = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
def lidx(c): return VOC.find(c.upper()) if c.upper() in VOC else 36
STYLES = ["italic", "blackletter", "upright"]; SI = {s: i for i, s in enumerate(STYLES)}
G = 40   # glyph canvas

def fit(sub):
    """grayscale glyph -> ink-bbox crop -> longest side G, centre-pad to GxG (preserves slant/weight)."""
    _, ink = cv2.threshold(sub, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ys, xs = np.where(ink > 0)
    if len(ys) < 6: return None
    sub = sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = sub.shape; sc = (G - 6) / max(h, w)
    r = cv2.resize(sub, (max(1, int(w * sc)), max(1, int(h * sc))), interpolation=cv2.INTER_AREA)
    canvas = np.full((G, G), 255, np.uint8)
    y0, x0 = (G - r.shape[0]) // 2, (G - r.shape[1]) // 2
    canvas[y0:y0 + r.shape[0], x0:x0 + r.shape[1]] = r
    return canvas

def gray_glyphs(patch, text):
    """force-split a de-rotated word into K=len(text) grayscale glyph crops (valley cuts)."""
    letters = [c for c in text if c.isalnum()]; K = len(letters)
    if K < 2: return []
    _, ink = cv2.threshold(patch, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    prof = (ink > 0).sum(0).astype(float); nz = np.where(prof > prof.max() * 0.02)[0]
    if len(nz) < K: return []
    a, b = int(nz[0]), int(nz[-1]); seg = prof[a:b + 1]; Wd = len(seg)
    sm = np.convolve(seg, np.ones(3) / 3, "same"); minsep = max(3, int(Wd / K * 0.40))
    cand = sorted([x for x in range(1, Wd - 1) if sm[x] <= sm[x - 1] and sm[x] <= sm[x + 1]], key=lambda x: sm[x])
    cuts = []
    for x in cand:
        if all(abs(x - c) >= minsep for c in cuts): cuts.append(x)
        if len(cuts) == K - 1: break
    bounds = [0] + sorted(cuts) + [Wd]; out = []
    for i in range(len(bounds) - 1):
        g = fit(patch[:, a + bounds[i]:a + bounds[i + 1]])
        if g is not None: out.append((letters[i].upper(), g))    # (letter, glyph) for letter-conditioning
    return out

class Net(nn.Module):
    def __init__(self):
        super().__init__()
        self.c = nn.Sequential(
            nn.Conv2d(1, 16, 3, padding=1), nn.BatchNorm2d(16), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(16, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.AdaptiveAvgPool2d(1))
        self.emb = nn.Embedding(37, 16)                          # letter -> content control
        self.f = nn.Sequential(nn.Dropout(0.4), nn.Linear(64 + 16, 3))
    def forward(self, x, l): return self.f(torch.cat([self.c(x).flatten(1), self.emb(l)], 1))

def augment(x):
    """style-preserving aug: tiny rotate/translate/scale + weight jitter + noise. NO shear (that IS the signal)."""
    a = (np.random.rand() - 0.5) * 6                     # +-3 deg
    M = cv2.getRotationMatrix2D((G / 2, G / 2), a, 1 + (np.random.rand() - 0.5) * 0.12)
    M[:, 2] += (np.random.rand(2) - 0.5) * 4
    y = cv2.warpAffine(x, M, (G, G), borderValue=255)
    if np.random.rand() < 0.3:
        k = np.ones((2, 2), np.uint8)
        y = cv2.erode(y, k) if np.random.rand() < 0.5 else cv2.dilate(y, k)
    y = np.clip(y.astype(np.float32) + np.random.randn(G, G) * 6, 0, 255).astype(np.uint8)
    return y

def main():
    torch.manual_seed(0); np.random.seed(0)
    dec = json.load(open(DEC)); font_by_i = {x["i"]: x["font"] for x in dec if x.get("font")}
    samp = json.load(open(BOXES))                         # frozen labelled boxes (pool-independent)
    words = []                                            # (font, [glyphs])
    for i, r in enumerate(samp):
        f = font_by_i.get(i)
        if f not in STYLES or r["text"] != dec[i]["text"]: continue
        patch = derotate(r)
        if patch is None: continue
        gs = gray_glyphs(patch, r["text"])
        if gs: words.append((f, gs))
    print(f"words: {len(words)}  glyphs: {sum(len(g) for _, g in words)}  "
          f"fonts: {dict(Counter(f for f, _ in words))}", flush=True)

    idx = np.arange(len(words)); np.random.shuffle(idx)
    folds = np.array_split(idx, 5)
    conf = Counter(); tot = Counter()
    for fi, test_ix in enumerate(folds):
        tr = [words[j] for j in idx if j not in set(test_ix.tolist())]
        te = [words[j] for j in test_ix]
        Xtr = []; Ltr = []; ytr = []
        for f, gs in tr:
            for L, g in gs: Xtr.append(g); Ltr.append(lidx(L)); ytr.append(SI[f])
        net = Net(); opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
        lossf = nn.CrossEntropyLoss(); net.train()
        Xtr = np.array(Xtr); Ltr = np.array(Ltr); ytr = np.array(ytr)
        for ep in range(60):
            p = np.random.permutation(len(Xtr))
            for k in range(0, len(p), 64):
                bi = p[k:k + 64]
                xb = np.stack([augment(Xtr[j]) for j in bi]).astype(np.float32)[:, None] / 255.0
                xb = torch.tensor((xb - 0.8) / 0.3); yb = torch.tensor(ytr[bi]); lb = torch.tensor(Ltr[bi])
                opt.zero_grad(); loss = lossf(net(xb, lb), yb); loss.backward(); opt.step()
        net.eval()
        with torch.no_grad():
            for f, gs in te:
                xb = torch.tensor((np.stack([g for _, g in gs]).astype(np.float32)[:, None] / 255.0 - 0.8) / 0.3)
                lb = torch.tensor([lidx(L) for L, _ in gs])
                prob = torch.softmax(net(xb, lb), 1).mean(0).numpy()
                pred = STYLES[int(prob.argmax())]
                conf[(f, pred)] += 1; tot[f] += 1
        print(f"  fold {fi+1}/5 done", flush=True)
    N = sum(tot.values()); acc = sum(conf[(s, s)] for s in STYLES) / max(1, N)
    print(f"\n=== learned glyph-CNN discriminator (5-fold by word, N={N}) ===")
    print(f"accuracy {acc:.3f}   [raster-kNN baseline 0.776]")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>8s}" for s in STYLES) + "  recall")
    for s in STYLES:
        print(f"  {s:10s}" + "".join(f"{conf[(s,d)]:>8d}" for d in STYLES) + f"  {conf[(s,s)]/max(1,tot[s]):.2f}")

if __name__ == "__main__":
    main()
