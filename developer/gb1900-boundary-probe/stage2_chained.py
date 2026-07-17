#!/usr/bin/env python
"""Stage-2 CHAINED — refine Stage-1 component evidence into the boundary line.

Input = Stage-1's {dot, cross} probability channels (NOT raw pixels — so the raw-
image domain gap is already normalised away by Stage-1). Target = the boundary
corridor. Trained with DISTRACTORS so it learns the discriminator:
  * a dot-chain is a boundary ONLY IF crosses run along it (mereing);
  * dot-only lines (footpaths), scattered text-crosses, and stipple are rejected.
At inference we feed the REAL Stage-1 proba (s1mc_proba.npy: ch1=dot, ch3=cross).
"""
import numpy as np, cv2, torch, torch.nn as nn
from PIL import Image
from stage2_unet import UNet, cbr        # reuse arch (patch input channels below)

DEV = "cuda" if torch.cuda.is_available() else "cpu"
rng = np.random.default_rng(1)
P = 160


def smooth_path(n=6):
    pts = rng.integers(6, P-6, size=(n, 2)).astype(np.float32); pts = pts[np.argsort(pts[:, 0])]
    ts = np.linspace(0, 1, 300)
    xs = np.convolve(np.interp(ts, np.linspace(0, 1, n), pts[:, 0]), np.ones(11)/11, "same")
    ys = np.convolve(np.interp(ts, np.linspace(0, 1, n), pts[:, 1]), np.ones(11)/11, "same")
    return np.stack([xs, ys], 1)


def _dots(path, pitch, r=2):
    ev = np.zeros((P, P), np.float32); acc = 0.0
    for i in range(1, len(path)):
        seg = np.hypot(*(path[i]-path[i-1])); acc += seg
        if acc >= pitch:
            acc = 0; cv2.circle(ev, tuple(path[i].astype(int)), r, 1.0, -1)
    return ev


def _crosses(path, pitch):
    ev = np.zeros((P, P), np.float32); xa = 0.0; nxt = rng.uniform(40, 120)
    for i in range(1, len(path)):
        xa += np.hypot(*(path[i]-path[i-1]))
        if xa >= nxt:
            xa = 0; nxt = rng.uniform(40, 120); c = (path[i]+rng.integers(-5, 6, 2)).astype(int)
            a = int(rng.integers(9, 13))
            cv2.line(ev, (c[0]-a, c[1]-a), (c[0]+a, c[1]+a), 1.0, 3)
            cv2.line(ev, (c[0]-a, c[1]+a), (c[0]+a, c[1]-a), 1.0, 3)
    return ev


def synth():
    dot = np.zeros((P, P), np.float32); crs = np.zeros((P, P), np.float32)
    corridor = np.zeros((P, P), np.uint8)
    if rng.random() < 0.85:                                  # TRUE boundary: dots + crosses
        path = smooth_path()
        dot = np.maximum(dot, _dots(path, rng.uniform(10, 22)))
        crs = np.maximum(crs, _crosses(path, rng.uniform(45, 110)))
        cv2.polylines(corridor, [path.astype(np.int32)], False, 1, int(rng.integers(6, 10)))
    # DISTRACTORS (present in inputs, NOT corridor):
    for _ in range(rng.integers(0, 3)):                      # dot-only lines (footpaths) -> not boundary
        dot = np.maximum(dot, _dots(smooth_path(), rng.uniform(8, 20)))
    for _ in range(rng.integers(0, 25)):                     # scattered text-crosses
        c = rng.integers(8, P-8, 2); a = int(rng.integers(6, 12))
        cv2.line(crs, (c[0]-a, c[1]-a), (c[0]+a, c[1]+a), 1.0, 3)
        cv2.line(crs, (c[0]-a, c[1]+a), (c[0]+a, c[1]-a), 1.0, 3)
    for _ in range(rng.integers(0, 40)):                     # stipple dots
        c = rng.integers(4, P-4, 2); cv2.circle(dot, tuple(c), int(rng.choice([1, 2])), 1.0, -1)
    dot = cv2.GaussianBlur(dot, (0, 0), 1.0); crs = cv2.GaussianBlur(crs, (0, 0), 1.0)
    dot = np.clip(dot + rng.normal(0, 0.03, dot.shape), 0, 1); crs = np.clip(crs + rng.normal(0, 0.03, crs.shape), 0, 1)
    k = rng.integers(0, 4)
    return (np.rot90(np.stack([dot, crs]), k, axes=(1, 2)).copy(),
            np.rot90(corridor, k).copy().astype(np.float32))


class DS(torch.utils.data.Dataset):
    def __init__(s, n): s.n = n
    def __len__(s): return s.n
    def __getitem__(s, i):
        x, m = synth(); return torch.from_numpy(x.astype(np.float32)), torch.from_numpy(m[None].astype(np.float32))


class UNet2(UNet):                       # 2-channel input variant
    def __init__(s, b=32):
        super().__init__(b); s.d1 = cbr(2, b)


def dice_bce(logit, tgt):
    p = torch.sigmoid(logit)
    bce = nn.functional.binary_cross_entropy_with_logits(logit, tgt)
    dice = 1 - (2*(p*tgt).sum()+1)/(p.sum()+tgt.sum()+1)
    return bce + dice


def train(iters=700, bs=16):
    net = UNet2().to(DEV); opt = torch.optim.Adam(net.parameters(), 1e-3)
    dl = torch.utils.data.DataLoader(DS(iters*bs), batch_size=bs, num_workers=4); net.train()
    for i, (x, y) in enumerate(dl):
        x, y = x.to(DEV), y.to(DEV); opt.zero_grad()
        loss = dice_bce(net(x), y); loss.backward(); opt.step()
        if i % 100 == 0: print(f"[s2c] iter {i} loss {loss.item():.3f}")
    return net


@torch.no_grad()
def infer(net, dot, crs, stride=80):
    H, W = dot.shape; prob = np.zeros((H, W), np.float32); cnt = np.zeros((H, W), np.float32)
    net.eval()
    for y in range(0, H-P+1, stride):
        for x in range(0, W-P+1, stride):
            t = torch.from_numpy(np.stack([dot[y:y+P, x:x+P], crs[y:y+P, x:x+P]])[None].astype(np.float32)).to(DEV)
            p = torch.sigmoid(net(t))[0, 0].cpu().numpy(); prob[y:y+P, x:x+P] += p; cnt[y:y+P, x:x+P] += 1
    return prob/np.maximum(cnt, 1)


if __name__ == "__main__":
    print(f"[s2c] device {DEV}")
    net = train()
    proba = np.load("s1mc_proba.npy")            # (H,W,5): 0 bg,1 dot,2 dash,3 cross,4 solid
    dot, crs = proba[..., 1], proba[..., 3]
    prob = infer(net, dot, crs)
    g = np.asarray(Image.open("s1mc_preproc.png").convert("L"))
    Image.fromarray((np.clip(prob, 0, 1)*255).astype(np.uint8)).save("s2c_prob.png")
    rgb = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
    heat = cv2.applyColorMap((np.clip(prob, 0, 1)*255).astype(np.uint8), cv2.COLORMAP_JET)
    over = cv2.addWeighted(rgb, 0.5, heat, 0.5, 0); over[prob < 0.4] = rgb[prob < 0.4]
    cv2.imwrite("s2c_overlay.png", over)
    print("[s2c] wrote s2c_overlay.png s2c_prob.png")
