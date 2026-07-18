"""Train the font-style encoder (iteration 2): synthetic SupCon + unsupervised real-crop consistency.

total loss = supcon(synthetic, font-class labels) + lambda * nt_xent(real crop, two aug views)
The real term (lever c) shapes the encoder's REAL feature manifold so it doesn't collapse.
Reports separation on held-out synthetic crops (the go/no-go gate). Usage:
    python train.py --tiles DIR... --boxes 'GLOB' --out OUTDIR --steps 4000 --lam 0.5
"""
import argparse, os, time, json, numpy as np, torch
from sklearn.metrics import silhouette_score
from sklearn.neighbors import KNeighborsClassifier
import data as DATA, fonts as F
from model import StyleEncoder, supcon_loss, nt_xent

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiles", nargs="+", required=True)
    ap.add_argument("--boxes", required=True, help="real spotter boxes glob (consistency pool)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=4000)
    ap.add_argument("--bs", type=int, default=96)
    ap.add_argument("--real_bs", type=int, default=64)
    ap.add_argument("--real_n", type=int, default=2500)
    ap.add_argument("--lam", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rng = np.random.RandomState(a.seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("device:", dev, "classes:", F.CLASS_NAMES, flush=True)

    bg = DATA.load_bg_pool(a.tiles, limit=400, rng=rng)
    print("bg pool (flattened):", len(bg), flush=True)
    real = DATA.load_real_pool(a.boxes, a.tiles, a.real_n, rng)
    print("real consistency pool (flattened):", len(real), flush=True)

    net = StyleEncoder().to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=2e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, a.steps)

    net.train(); t0 = time.time()
    for step in range(a.steps):
        xs, ys = DATA.make_batch(a.bs, rng, bg)
        z = net(torch.from_numpy(xs).to(dev))
        lsup = supcon_loss(z, torch.from_numpy(ys).to(dev))
        if real and a.lam > 0:
            v1, v2 = DATA.real_pair_batch(real, a.real_bs, rng)
            zc = net(torch.from_numpy(np.concatenate([v1, v2])).to(dev))
            lcon = nt_xent(zc[:a.real_bs], zc[a.real_bs:])
        else:
            lcon = torch.zeros((), device=dev)
        loss = lsup + a.lam * lcon
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        if step % 200 == 0:
            print(f"step {step:5d} sup {lsup.item():.4f} con {float(lcon):.4f} "
                  f"({time.time()-t0:.0f}s)", flush=True)

    torch.save(net.state_dict(), os.path.join(a.out, "encoder.pt"))

    # ---- synthetic separation gate ----
    net.eval()
    vx, vy = DATA.make_batch(1200, np.random.RandomState(999), bg)
    with torch.no_grad():
        vz = net(torch.from_numpy(vx).to(dev)).cpu().numpy()
    n = len(vy); h = n // 2
    acc = float((KNeighborsClassifier(5).fit(vz[:h], vy[:h]).predict(vz[h:]) == vy[h:]).mean())
    sil = float(silhouette_score(vz, vy))
    rep = dict(knn_acc=acc, silhouette=sil, classes=F.CLASS_NAMES, steps=a.steps,
               lam=a.lam, real_pool=len(real))
    json.dump(rep, open(os.path.join(a.out, "synth_val.json"), "w"), indent=2)
    print("SYNTH VAL:", json.dumps(rep), flush=True)
    print(f"  knn_acc={acc:.3f} (chance={1/len(F.CLASS_NAMES):.3f}), silhouette={sil:.3f}", flush=True)

if __name__ == "__main__":
    main()
