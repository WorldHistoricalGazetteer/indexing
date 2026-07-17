#!/usr/bin/env python
"""Stage-2 — structural line-enforcer (U-Net) on self-labelled z17 synthetic data.

Where Stage-1 (RF) classifies pixels and so confuses text/building BLOBS with the
boundary, Stage-2 sees spatial context: the TARGET is the continuous boundary
CORRIDOR (the path as a thick line), so the net learns to connect discrete mereing
marks into a line and reject isolated blobs. Trained purely on synthetic composites
(real boundary-free z17 crops + rendered dots/dashes/x's), tested on the REAL z17
boundary it never saw. Same role as SegFormer; swap the backbone later if wanted.
"""
import numpy as np, cv2, torch, torch.nn as nn
from PIL import Image

DEV = "cuda" if torch.cuda.is_available() else "cpu"
rng = np.random.default_rng(0)
SRC = "z17_stitch.png"
g_full = np.asarray(Image.open(SRC).convert("L"), np.uint8)
H, W = g_full.shape
P = 160
BOUNDARY_BOXES = [(800, 0, W, H), (0, 880, 560, 1250)]   # real-boundary corridors: exclude from bg


def boundary_free(x, y, s=P):
    return all(x + s < b[0] or x > b[2] or y + s < b[1] or y > b[3] for b in BOUNDARY_BOXES)


def sample_bg():
    for _ in range(300):
        x = rng.integers(0, W - P); y = rng.integers(0, H - P)
        if boundary_free(x, y) and g_full[y:y+P, x:x+P].mean() > 120:
            return g_full[y:y+P, x:x+P].copy()
    return g_full[:P, :P].copy()


def smooth_path(n=6):
    pts = rng.integers(6, P - 6, size=(n, 2)).astype(np.float32); pts = pts[np.argsort(pts[:, 0])]
    ts = np.linspace(0, 1, 300)
    xs = np.convolve(np.interp(ts, np.linspace(0, 1, n), pts[:, 0]), np.ones(11)/11, "same")
    ys = np.convolve(np.interp(ts, np.linspace(0, 1, n), pts[:, 1]), np.ones(11)/11, "same")
    return np.stack([xs, ys], 1)


def synth():
    """Return (img gray uint8, corridor mask float32 {0,1}).

    Glyphs are drawn on a separate INK-AMOUNT layer that is Gaussian-BLURRED before
    compositing, so their edges are as soft as real scanned ink (the key domain-gap
    fix). Plus domain randomisation: dot size/spacing/darkness, optional mere-along-a-
    solid-line, whole-image gamma/blur/contrast.
    """
    bg = sample_bg().astype(np.float32)
    corridor = np.zeros((P, P), np.uint8)
    ink_layer = np.zeros((P, P), np.float32)          # 0..1 ink amount, drawn hard then blurred
    if rng.random() < 0.9:                              # 90% have a boundary; 10% pure-negative
        path = smooth_path()
        d_pitch = rng.uniform(10, 24); x_pitch = rng.uniform(55, 150)
        dot_r = int(rng.choice([2, 2, 3])); dash = rng.random() < 0.3
        acc = 0.0; xa = rng.uniform(0, 40)
        poly = path.astype(np.int32)
        cv2.polylines(corridor, [poly], False, 1, int(rng.integers(6, 10)))   # continuous target
        if rng.random() < 0.45:                         # mere ALONG a solid feature line
            cv2.polylines(ink_layer, [poly], False, float(rng.uniform(0.5, 0.9)), int(rng.integers(1, 3)))
        for i in range(1, len(path)):
            p0, p1 = path[i-1], path[i]; seg = np.hypot(*(p1-p0)); acc += seg; xa += seg
            if acc >= d_pitch:
                acc = 0; c = p1.astype(int)
                if dash:
                    t = (p1-p0); t /= (np.linalg.norm(t)+1e-6)
                    cv2.line(ink_layer, tuple((p1-t*3).astype(int)), tuple((p1+t*3).astype(int)), 1.0, 2)
                else:
                    cv2.circle(ink_layer, tuple(c), dot_r, 1.0, -1)
            if xa >= x_pitch:
                xa = 0; x_pitch = rng.uniform(55, 150); off = rng.integers(-6, 7, 2); c = (p1+off).astype(int)
                a = int(rng.integers(9, 14)); th = int(rng.integers(2, 4))
                cv2.line(ink_layer, (c[0]-a, c[1]-a), (c[0]+a, c[1]+a), 1.0, th)
                cv2.line(ink_layer, (c[0]-a, c[1]+a), (c[0]+a, c[1]-a), 1.0, th)
    # SOFT edges: blur the ink layer to match scanned-ink anti-aliasing
    ink_layer = cv2.GaussianBlur(ink_layer, (0, 0), rng.uniform(0.6, 1.3))
    dark = float(rng.integers(5, 45))                   # ink darkness (near-black..mid-grey)
    img = bg * (1 - ink_layer) + dark * ink_layer
    # whole-image domain randomisation
    img = np.clip(img, 0, 255)
    g = rng.uniform(0.8, 1.25); img = 255.0 * (img / 255.0) ** g            # gamma
    if rng.random() < 0.5: img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.4, 0.9))
    img = np.clip(img + rng.normal(0, rng.uniform(2, 5), img.shape), 0, 255).astype(np.uint8)
    # augment: flips/rot90
    k = rng.integers(0, 4); img = np.rot90(img, k).copy(); corridor = np.rot90(corridor, k).copy()
    if rng.random() < 0.5: img = img[:, ::-1].copy(); corridor = corridor[:, ::-1].copy()
    return img, corridor.astype(np.float32)


class DS(torch.utils.data.Dataset):
    def __init__(self, n): self.n = n
    def __len__(self): return self.n
    def __getitem__(self, i):
        img, m = synth()
        x = torch.from_numpy(img.astype(np.float32)[None] / 255.0)
        return x, torch.from_numpy(m[None])


def cbr(i, o): return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True),
                                    nn.Conv2d(o, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True))


class UNet(nn.Module):
    def __init__(s, b=32):
        super().__init__()
        s.d1 = cbr(1, b); s.d2 = cbr(b, b*2); s.d3 = cbr(b*2, b*4); s.d4 = cbr(b*4, b*8)
        s.pool = nn.MaxPool2d(2)
        s.u3 = nn.ConvTranspose2d(b*8, b*4, 2, 2); s.c3 = cbr(b*8, b*4)
        s.u2 = nn.ConvTranspose2d(b*4, b*2, 2, 2); s.c2 = cbr(b*4, b*2)
        s.u1 = nn.ConvTranspose2d(b*2, b, 2, 2); s.c1 = cbr(b*2, b)
        s.out = nn.Conv2d(b, 1, 1)
    def forward(s, x):
        e1 = s.d1(x); e2 = s.d2(s.pool(e1)); e3 = s.d3(s.pool(e2)); e4 = s.d4(s.pool(e3))
        d = s.c3(torch.cat([s.u3(e4), e3], 1)); d = s.c2(torch.cat([s.u2(d), e2], 1))
        d = s.c1(torch.cat([s.u1(d), e1], 1)); return s.out(d)


def dice_bce(logit, tgt):
    p = torch.sigmoid(logit)
    bce = nn.functional.binary_cross_entropy_with_logits(logit, tgt)
    dice = 1 - (2*(p*tgt).sum()+1) / (p.sum()+tgt.sum()+1)
    return bce + dice


def train(iters=800, bs=16):
    net = UNet().to(DEV); opt = torch.optim.Adam(net.parameters(), 1e-3)
    dl = torch.utils.data.DataLoader(DS(iters*bs), batch_size=bs, num_workers=4)
    net.train()
    for i, (x, y) in enumerate(dl):
        x, y = x.to(DEV), y.to(DEV); opt.zero_grad()
        loss = dice_bce(net(x), y); loss.backward(); opt.step()
        if i % 100 == 0: print(f"[s2] iter {i} loss {loss.item():.3f}")
    return net


@torch.no_grad()
def infer(net, img, stride=80):
    net.eval(); prob = np.zeros((H, W), np.float32); cnt = np.zeros((H, W), np.float32)
    for y in range(0, H-P+1, stride):
        for x in range(0, W-P+1, stride):
            t = torch.from_numpy(img[y:y+P, x:x+P].astype(np.float32)[None, None]/255.0).to(DEV)
            p = torch.sigmoid(net(t))[0, 0].cpu().numpy()
            prob[y:y+P, x:x+P] += p; cnt[y:y+P, x:x+P] += 1
    return prob / np.maximum(cnt, 1)


if __name__ == "__main__":
    print(f"[s2] device {DEV}")
    for j in range(4):                     # dump synthetic examples for realism check
        ex, exm = synth(); Image.fromarray(ex).save(f"z17_s2_synth{j}.png")
    net = train()
    prob = infer(net, g_full)
    Image.fromarray((np.clip(prob, 0, 1)*255).astype(np.uint8)).save("z17_s2_prob.png")
    rgb = cv2.cvtColor(g_full, cv2.COLOR_GRAY2BGR)
    heat = cv2.applyColorMap((np.clip(prob, 0, 1)*255).astype(np.uint8), cv2.COLORMAP_JET)
    over = cv2.addWeighted(rgb, 0.5, heat, 0.5, 0); over[prob < 0.4] = rgb[prob < 0.4]
    cv2.imwrite("z17_s2_overlay.png", over)
    torch.save(net.state_dict(), "stage2_unet.pt")
    print("[s2] wrote z17_s2_overlay.png z17_s2_prob.png stage2_unet.pt")
