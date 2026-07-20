"""Phase C HYBRID Stage 1 — self-supervised WITHIN-WORD contrastive pretraining on the 28k unlabelled
spotter words. A word's glyphs are same-font / different-letter, so pulling them together (and pushing
different words apart) teaches a style embedding that discards letter content — on REAL crops, no labels,
no synthetic domain gap. Stage 2 (ssl_eval.py) then runs the SAME same-letter kNN in this learned space.

Extracts + caches glyph bags (derotate -> force-split -> grayscale 40x40), trains a small CNN encoder with
a SupCon (within-word) loss, saves encoder.pt.

    /vast/ishi/envs/boundary/bin/python ssl_pretrain.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, glob, json, numpy as np
import concurrent.futures as cf
import torch, torch.nn as nn
from disc_train import gray_glyphs
from make_font_testset_v2 import derotate

SPOT = "/vast/ishi/gb1900/edition/spot"; CACHE = f"{SPOT}/ssl_glyphs.npz"; ENC = f"{SPOT}/encoder.pt"
G = 40

def extract():
    if os.path.exists(CACHE):
        d = np.load(CACHE)
        return d["glyphs"], d["letters"], d["wid"]
    boxes = []
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(f):
            r = json.loads(line)
            if len([c for c in r["text"] if c.isalnum()]) >= 2 and r["score"] >= 0.5: boxes.append(r)
    print(f"boxes to extract: {len(boxes)}", flush=True)
    def work(ib):
        i, r = ib; patch = derotate(r)
        if patch is None: return None
        gs = gray_glyphs(patch, r["text"])          # [(letter, glyph40)]
        return [(L, g, i) for L, g in gs] if gs else None
    out = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for k, res in enumerate(ex.map(work, list(enumerate(boxes)))):
            if res: out += res
            if k % 3000 == 0: print(f"  {k}/{len(boxes)} glyphs={len(out)}", flush=True)
    glyphs = np.array([g for _, g, _ in out], np.uint8)
    letters = np.array([L for L, _, _ in out]); wid = np.array([w for _, _, w in out])
    np.savez_compressed(CACHE, glyphs=glyphs, letters=letters, wid=wid)
    print(f"cached {len(glyphs)} glyphs from {len(set(wid))} words -> {CACHE}", flush=True)
    return glyphs, letters, wid

class Enc(nn.Module):
    def __init__(self, d=64):
        super().__init__()
        self.c = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(), nn.AdaptiveAvgPool2d(1))
        self.f = nn.Linear(128, d)
    def forward(self, x):
        z = self.f(self.c(x).flatten(1)); return z / (z.norm(dim=1, keepdim=True) + 1e-8)

def supcon(z, lab, temp=0.1):
    N = len(z); sim = (z @ z.T) / temp
    eye = torch.eye(N, dtype=torch.bool)
    sim = sim.masked_fill(eye, -1e9)
    logp = sim - torch.logsumexp(sim, 1, keepdim=True)
    pos = (lab[:, None] == lab[None, :]) & ~eye
    has = pos.sum(1) > 0
    return -(logp * pos).sum(1)[has].div(pos.sum(1)[has]).mean()

def augment(x):
    import cv2
    a = (np.random.rand() - 0.5) * 6
    M = cv2.getRotationMatrix2D((G / 2, G / 2), a, 1 + (np.random.rand() - 0.5) * 0.12)
    M[:, 2] += (np.random.rand(2) - 0.5) * 4
    y = cv2.warpAffine(x, M, (G, G), borderValue=255)
    return np.clip(y.astype(np.float32) + np.random.randn(G, G) * 5, 0, 255).astype(np.uint8)

def main():
    torch.manual_seed(0); np.random.seed(0)
    glyphs, letters, wid = extract()
    # keep only words with >=2 glyphs (need positives)
    from collections import Counter
    cnt = Counter(wid.tolist()); keep = np.array([cnt[w] >= 2 for w in wid])
    glyphs, wid = glyphs[keep], wid[keep]
    words = sorted(set(wid.tolist()))
    by = {w: np.where(wid == w)[0] for w in words}
    print(f"training glyphs {len(glyphs)} across {len(words)} words (>=2 glyphs)", flush=True)

    net = Enc(); opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4); net.train()
    WORDS_PER_BATCH = 48; EPOCHS = 12
    for ep in range(EPOCHS):
        np.random.shuffle(words); losses = []
        for b in range(0, len(words), WORDS_PER_BATCH):
            wb = words[b:b + WORDS_PER_BATCH]
            idx = np.concatenate([by[w] for w in wb]); lab = np.concatenate([[w] * len(by[w]) for w in wb])
            xb = np.stack([augment(glyphs[j]) for j in idx]).astype(np.float32)[:, None] / 255.0
            xb = torch.tensor((xb - 0.8) / 0.3); lb = torch.tensor(lab)
            opt.zero_grad(); loss = supcon(net(xb), lb); loss.backward(); opt.step()
            losses.append(float(loss))
        print(f"epoch {ep+1}/{EPOCHS} supcon={np.mean(losses):.4f}", flush=True)
    torch.save(net.state_dict(), ENC)
    print(f"saved encoder -> {ENC}", flush=True)

if __name__ == "__main__":
    main()
