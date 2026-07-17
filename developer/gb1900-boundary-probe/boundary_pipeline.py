#!/usr/bin/env python
"""Consolidated GB1900 boundary-extraction pipeline + metric-driven sweep (CRC a100).

Two stages, chained:
  Stage-1  multi-class RF (bg/dot/dash/cross/arrow/solid) on preprocessed crops,
           trained on self-labelled synthetic composites (synth_glyphs + degrade).
  Stage-2  U-Net line-enforcer: consumes Stage-1's {dot,cross,arrow} evidence and
           emits the boundary corridor. Trained on Stage-1's ACTUAL outputs over
           synthetic images (faithful chain) with distractors (dot-only footpaths,
           text-crosses, stipple) so it requires the mereing signature.

Metric: boundary-F1 vs a hand-traced GT (predicted corridor vs GT line within tau px).

  python boundary_pipeline.py --src z17_stitch.png --gt gt_boundary.npy --sweep
  python boundary_pipeline.py --src ... --gt ... --rf-trees 200 --unet-depth 4 --unet-base 32
"""
import argparse, json, itertools, time, numpy as np, cv2, torch, torch.nn as nn
from PIL import Image
from skimage.feature import multiscale_basic_features
from sklearn.ensemble import RandomForestClassifier
from synth_glyphs import render_boundary, render_footpath, smooth_path
from degrade import degrade

DEV = "cuda" if torch.cuda.is_available() else "cpu"
P = 160
# component classes from synth_glyphs.comp: 1 dot, 3 cross, 4 arrow (+ we add 2 dash, 5 solid)


def preprocess(gray):
    paper = cv2.morphologyEx(gray, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))).astype(np.float32)
    flat = np.clip(gray.astype(np.float32) / np.maximum(paper, 1) * 200.0, 0, 255).astype(np.uint8)
    return cv2.createCLAHE(2.0, (8, 8)).apply(flat)


class Env:
    """Holds the preprocessed test stitch and samples boundary-free background crops."""
    def __init__(self, src, boxes):
        self.g = preprocess(np.asarray(Image.open(src).convert("L"), np.uint8))
        self.H, self.W = self.g.shape
        self.boxes = boxes

    def free(self, x, y, s=P):
        return all(x+s < b[0] or x > b[2] or y+s < b[1] or y > b[3] for b in self.boxes)

    def sample_bg(self, rng):
        for _ in range(300):
            x = rng.integers(0, self.W-P); y = rng.integers(0, self.H-P)
            if self.free(x, y) and self.g[y:y+P, x:x+P].mean() > 110:
                return self.g[y:y+P, x:x+P].copy()
        return self.g[:P, :P].copy()


# ---------------- Stage-1: multi-class RF ----------------

def synth_s1(env, rng):
    """Composite real bg + rendered components; return degraded img + label map (0..5)."""
    bg = env.sample_bg(rng).astype(np.float32)
    ink = np.zeros((P, P), np.float32); lab = np.zeros((P, P), np.uint8)
    for _ in range(rng.integers(1, 4)):
        typ = rng.choice(["boundary", "dash", "solid", "dotline", "footpath"])
        ik = np.zeros((P, P), np.float32); cp = np.zeros((P, P), np.uint8)
        path = smooth_path(P, rng)
        if typ == "boundary":
            render_boundary(P, path, rng, ik, cp)                 # dot=1 cross=3 arrow=4 (offset)
        elif typ == "footpath":                                   # double parallel dashed -> class 2
            render_footpath(P, path, rng, ik, cp)
        elif typ == "dotline":                                    # single dotted (e.g. path): dots only
            acc = 0; pit = rng.uniform(8, 18)
            for i in range(1, len(path)):
                acc += np.hypot(*(path[i]-path[i-1]))
                if acc >= pit: acc = 0; c = path[i].astype(int); cv2.circle(ik, tuple(c), 2, 1, -1); cv2.circle(cp, tuple(c), 2, 1, -1)
        elif typ == "dash":                                       # hedge
            acc = 0; pit = rng.uniform(8, 16)
            for i in range(1, len(path)):
                acc += np.hypot(*(path[i]-path[i-1]))
                if acc >= pit:
                    acc = 0; t = path[i]-path[i-1]; t /= (np.linalg.norm(t)+1e-6)
                    cv2.line(ik, tuple((path[i]-t*3).astype(int)), tuple((path[i]+t*3).astype(int)), 1, 2)
                    cv2.line(cp, tuple((path[i]-t*3).astype(int)), tuple((path[i]+t*3).astype(int)), 2, 2)
        else:                                                     # solid road
            cv2.polylines(ik, [path.astype(np.int32)], False, 1, int(rng.integers(1, 3)))
            cp[ik > 0.3] = 5
        lab[cp > 0] = cp[cp > 0]
        ink = np.maximum(ink, ik)
    img = degrade(bg, ink, float(rng.integers(6, 45)), rng)
    k = rng.integers(0, 4); return np.rot90(img, k).copy(), np.rot90(lab, k).copy()


def feats(g, sig_max, nsig):
    return multiscale_basic_features(g, channel_axis=None, intensity=True, edges=True,
                                     texture=True, sigma_min=1, sigma_max=sig_max, num_sigma=nsig)


def train_stage1(env, cfg, rng, n=70):
    X, Y = [], []
    for _ in range(n):
        img, lab = synth_s1(env, rng); f = feats(img, cfg["sig_max"], cfg["nsig"])
        for cls in range(6):
            idx = np.argwhere(lab == cls)
            if not len(idx): continue
            cap = 1500 if cls else 2500
            for (yy, xx) in idx[rng.choice(len(idx), min(len(idx), cap), replace=False)]:
                X.append(f[yy, xx]); Y.append(cls)
    clf = RandomForestClassifier(cfg["trees"], n_jobs=-1, min_samples_leaf=cfg["leaf"],
                                 class_weight="balanced", random_state=0)
    clf.fit(np.asarray(X), np.asarray(Y)); return clf


def stage1_proba(rf, img, cfg):
    f = feats(img, cfg["sig_max"], cfg["nsig"]).reshape(-1, rf.n_features_in_)
    return rf.predict_proba(f).reshape(img.shape[0], img.shape[1], -1)  # (H,W,6) if all classes seen


# ---------------- Stage-2: chained U-Net ----------------

def cbr(i, o): return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(True),
                                    nn.Conv2d(o, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(True))


class UNet(nn.Module):
    def __init__(s, cin=3, depth=4, base=32):
        super().__init__(); s.depth = depth; s.pool = nn.MaxPool2d(2)
        s.enc = nn.ModuleList(); ch = cin
        for d in range(depth):
            o = base*(2**d); s.enc.append(cbr(ch, o)); ch = o
        s.up = nn.ModuleList(); s.dec = nn.ModuleList()
        for d in range(depth-1, 0, -1):
            o = base*(2**(d-1)); s.up.append(nn.ConvTranspose2d(base*(2**d), o, 2, 2)); s.dec.append(cbr(base*(2**d), o))
        s.out = nn.Conv2d(base, 1, 1)

    def forward(s, x):
        skips = []
        for i, e in enumerate(s.enc):
            x = e(x)
            if i < s.depth-1: skips.append(x); x = s.pool(x)
        for j, (u, d) in enumerate(zip(s.up, s.dec)):
            x = u(x); x = d(torch.cat([x, skips[-(j+1)]], 1))
        return s.out(x)


def stage2_data(env, rf, cfg, rng, m=280):
    """Faithful chain: run trained Stage-1 on synthetic imgs -> evidence; target=corridor."""
    Xs, Ys = [], []
    ncls = rf.n_classes_
    for _ in range(m):
        bg = env.sample_bg(rng).astype(np.float32)
        ink = np.zeros((P, P), np.float32); corridor = np.zeros((P, P), np.uint8)
        if rng.random() < 0.85:
            path = smooth_path(P, rng); render_boundary(P, path, rng, ink, None)
            cv2.polylines(corridor, [path.astype(np.int32)], False, 1, int(rng.integers(6, 10)))
        # distractors
        for _ in range(rng.integers(0, 3)):                        # footpath dot-lines
            p = smooth_path(P, rng); acc = 0
            for i in range(1, len(p)):
                acc += np.hypot(*(p[i]-p[i-1]))
                if acc >= rng.uniform(8, 18): acc = 0; cv2.circle(ink, tuple(p[i].astype(int)), 2, 1, -1)
        img = degrade(bg, ink, float(rng.integers(6, 45)), rng)
        pr = stage1_proba(rf, img, cfg)
        # evidence = dot(1)+cross(3)+arrow(4) channels if present
        ev = np.stack([pr[..., min(1, ncls-1)], pr[..., min(3, ncls-1)], pr[..., min(4, ncls-1)]]).astype(np.float32)
        Xs.append(ev); Ys.append(corridor.astype(np.float32)[None])
    return np.array(Xs), np.array(Ys)


def dice_bce(logit, t):
    p = torch.sigmoid(logit)
    return (nn.functional.binary_cross_entropy_with_logits(logit, t)
            + 1 - (2*(p*t).sum()+1)/(p.sum()+t.sum()+1))


def train_stage2(X, Y, cfg, rng):
    net = UNet(3, cfg["depth"], cfg["base"]).to(DEV)
    opt = torch.optim.Adam(net.parameters(), 1e-3); net.train()
    Xt = torch.from_numpy(X); Yt = torch.from_numpy(Y); bs = 16
    for ep in range(cfg["epochs"]):
        perm = torch.randperm(len(Xt))
        for i in range(0, len(Xt), bs):
            b = perm[i:i+bs]; x = Xt[b].to(DEV); y = Yt[b].to(DEV)
            opt.zero_grad(); loss = dice_bce(net(x), y); loss.backward(); opt.step()
    return net


@torch.no_grad()
def chain_infer(env, rf, net, cfg, stride=80):
    pr = stage1_proba(rf, env.g, cfg); ncls = rf.n_classes_
    dot, crs, arr = pr[..., min(1, ncls-1)], pr[..., min(3, ncls-1)], pr[..., min(4, ncls-1)]
    H, W = env.H, env.W; prob = np.zeros((H, W), np.float32); cnt = np.zeros((H, W), np.float32)
    net.eval()
    for y in range(0, H-P+1, stride):
        for x in range(0, W-P+1, stride):
            ev = np.stack([dot[y:y+P, x:x+P], crs[y:y+P, x:x+P], arr[y:y+P, x:x+P]])[None].astype(np.float32)
            p = torch.sigmoid(net(torch.from_numpy(ev).to(DEV)))[0, 0].cpu().numpy()
            prob[y:y+P, x:x+P] += p; cnt[y:y+P, x:x+P] += 1
    return prob/np.maximum(cnt, 1)


def boundary_f1(pred, gt, tau=10, thr=0.4):
    pb = (pred > thr).astype(np.uint8)
    gtd = cv2.dilate(gt.astype(np.uint8), np.ones((2*tau+1, 2*tau+1), np.uint8))
    pbd = cv2.dilate(pb, np.ones((2*tau+1, 2*tau+1), np.uint8))
    prec = (pb & gtd).sum()/max(pb.sum(), 1)
    rec = (gt.astype(np.uint8) & pbd).sum()/max(gt.sum(), 1)
    f1 = 2*prec*rec/max(prec+rec, 1e-6)
    return dict(f1=round(float(f1), 3), precision=round(float(prec), 3), recall=round(float(rec), 3))


def run_config(env, gt, cfg, rng):
    t0 = time.time()
    rf = train_stage1(env, cfg, rng)
    X, Y = stage2_data(env, rf, cfg, rng)
    net = train_stage2(X, Y, cfg, rng)
    prob = chain_infer(env, rf, net, cfg)
    best = max((dict(thr=thr, **boundary_f1(prob, gt, cfg["tau"], thr)) for thr in (0.3, 0.4, 0.5, 0.6)),
               key=lambda d: d["f1"])
    best["secs"] = round(time.time()-t0); return best, prob


# z17 Conwy stitch: boundary corridors to exclude from bg sampling
BOXES = [(800, 0, 1536, 1536), (0, 880, 560, 1250)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True); ap.add_argument("--gt", required=True)
    ap.add_argument("--out", default="sweep_results.json"); ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--trees", type=int, default=180); ap.add_argument("--leaf", type=int, default=3)
    ap.add_argument("--sig-max", type=float, default=12); ap.add_argument("--nsig", type=int, default=5)
    ap.add_argument("--depth", type=int, default=4); ap.add_argument("--base", type=int, default=32)
    ap.add_argument("--epochs", type=int, default=12); ap.add_argument("--tau", type=int, default=10)
    a = ap.parse_args()
    env = Env(a.src, BOXES); gt = np.load(a.gt); rng = np.random.default_rng(0)
    print(f"[pipe] device {DEV}  stitch {env.g.shape}  gt px {int(gt.sum())}")
    if not a.sweep:
        cfg = dict(trees=a.trees, leaf=a.leaf, sig_max=a.sig_max, nsig=a.nsig,
                   depth=a.depth, base=a.base, epochs=a.epochs, tau=a.tau)
        best, prob = run_config(env, gt, cfg, rng)
        print("[pipe] result", json.dumps(best)); np.save("chain_prob.npy", prob); return
    # --- efficient first sweep: vary the levers that matter most ---
    grid = [
        dict(trees=150, leaf=3, sig_max=8,  nsig=4, depth=4, base=32, epochs=12, tau=10),
        dict(trees=150, leaf=3, sig_max=12, nsig=5, depth=4, base=32, epochs=12, tau=10),
        dict(trees=300, leaf=2, sig_max=12, nsig=5, depth=4, base=32, epochs=12, tau=10),
        dict(trees=150, leaf=3, sig_max=16, nsig=6, depth=5, base=32, epochs=12, tau=10),  # deeper U-Net (far x's)
        dict(trees=300, leaf=2, sig_max=12, nsig=5, depth=5, base=48, epochs=16, tau=10),
    ]
    results = []
    for i, cfg in enumerate(grid):
        best, prob = run_config(env, gt, cfg, rng)
        row = dict(cfg=cfg, **best); results.append(row)
        print(f"[sweep {i+1}/{len(grid)}] {json.dumps(row)}")
        np.save(f"chain_prob_{i}.npy", prob)
    results.sort(key=lambda r: r["f1"], reverse=True)
    json.dump(results, open(a.out, "w"), indent=2)
    print("[sweep] BEST:", json.dumps(results[0]))


if __name__ == "__main__":
    main()
