"""Phase C HYBRID Stage 2 — same-letter kNN in the SSL-learned embedding space.

Loads the within-word-contrastive encoder (ssl_pretrain.py), embeds the human-labelled glyphs, and runs the
EXACT same leave-one-word-out same-letter matching as validate_combined.py — but with learned embeddings
instead of raw rasters. If the SSL features beat raw pixels, this is the first learned model to pass the
0.737 raster-kNN (it escaped scarcity via 28k-word pretraining).

    /vast/ishi/envs/boundary/bin/python ssl_eval.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, json, numpy as np, cv2, torch
from collections import Counter, defaultdict
from disc_train import fit
from make_font_testset_v2 import derotate
from ssl_pretrain import Enc, G, ENC as ENC_DEFAULT
ENC = os.environ.get("ENC", ENC_DEFAULT)

SPOT = "/vast/ishi/gb1900/edition/spot"; FD = "/vast/ishi/gb1900/probe/font"
SETS = [(f"{SPOT}/font_testset_v2_boxes.json", f"{FD}/font_testset_decisions_1.json"),
        (f"{SPOT}/font_testset_v3_boxes.json", f"{FD}/font_testset_v3_decisions.json")]
STYLES = ["italic", "blackletter", "upright"]

def cased(patch, text):
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
        if g is not None: out.append((letters[i].upper(), letters[i].isupper(), g))
    return out

def harvest(boxfile, decfile):
    dec = json.load(open(decfile)); font_by_i = {x["i"]: x["font"] for x in dec if x.get("font")}
    samp = json.load(open(boxfile)); words = []
    for i, r in enumerate(samp):
        f = font_by_i.get(i)
        if f not in STYLES or r["text"] != dec[i]["text"]: continue
        patch = derotate(r)
        if patch is None: continue
        gl = cased(patch, r["text"])
        if gl: words.append((f, gl))
    return words

def main():
    words = []
    for bf, df in SETS: words += harvest(bf, df)
    print(f"words {len(words)} fonts {dict(Counter(f for f, _ in words))}", flush=True)
    net = Enc(); net.load_state_dict(torch.load(ENC, map_location="cpu")); net.eval()

    caps = [(L, cap, g, wi) for wi, (_, gl) in enumerate(words) for (L, cap, g) in gl]
    letters = np.array([c[0] for c in caps]); caparr = np.array([c[1] for c in caps]); boxid = np.array([c[3] for c in caps])
    gfont = np.array([words[c[3]][0] for c in caps])
    with torch.no_grad():
        X = np.stack([c[2] for c in caps]).astype(np.float32)[:, None] / 255.0
        Z = net(torch.tensor((X - 0.8) / 0.3)).numpy()                # (N,64) normalised

    records = []
    for wi, (tf, gl) in enumerate(words):
        wvote = defaultdict(float)
        for L, cap, g in gl:
            same = (letters == L) & (caparr == cap) & (boxid != wi)
            if len(set(gfont[same])) < 2: continue
            with torch.no_grad():
                z = net(torch.tensor(((g.astype(np.float32)[None, None] / 255.0) - 0.8) / 0.3)).numpy()[0]
            sim = np.where(same, Z @ z, -2.0)
            best = sorted(((s, float(sim[(gfont == s) & same].max())) for s in set(gfont[same])), key=lambda kv: -kv[1])
            wvote[best[0][0]] += best[0][1] - (best[1][1] if len(best) > 1 else 0)
        if not wvote: continue
        pred = max(wvote, key=wvote.get); conf = wvote[pred] / (sum(wvote.values()) + 1e-9)
        records.append((tf, pred, conf))
    c = Counter(); t = Counter()
    for tf, pf, _ in records: c[(tf, pf)] += 1; t[tf] += 1
    N = sum(t.values()); acc = sum(c[(s, s)] for s in STYLES) / max(1, N)
    print(f"\n=== HYBRID: same-letter kNN in SSL embedding (LOO, N={N}) ===")
    print(f"accuracy {acc:.3f}   [raster same-letter kNN 0.737 combined / 0.776 round-1]")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>8s}" for s in STYLES) + "  recall")
    for s in STYLES:
        print(f"  {s:10s}" + "".join(f"{c[(s,d)]:>8d}" for d in STYLES) + f"  {c[(s,s)]/max(1,t[s]):.2f}")
    print("\nconfidence gating:")
    for tau in [0.0, 0.6, 0.7, 0.8, 0.9]:
        kept = [r for r in records if r[2] >= tau]
        if kept: print(f"  tau>={tau:.1f}  cov {len(kept)/len(records)*100:.0f}%  acc {sum(1 for a,b,_ in kept if a==b)/len(kept):.3f}")

if __name__ == "__main__":
    main()
