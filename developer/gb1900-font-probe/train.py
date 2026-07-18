"""Train the synthetic font-style encoder, report separation on held-out synthetic crops.

If the encoder can't even separate the synthetic classes, the approach is dead and we stop
before touching the real corpus. Usage:
    python train.py --tiles DIR [DIR...] --out OUTDIR --steps 4000
"""
import argparse, os, time, json, numpy as np, torch
from sklearn.metrics import silhouette_score
from sklearn.neighbors import KNeighborsClassifier
import data as DATA
import fonts as F
from model import StyleEncoder, supcon_loss

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiles", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=4000)
    ap.add_argument("--bs", type=int, default=96)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rng = np.random.RandomState(a.seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("device:", dev, "classes:", F.CLASS_NAMES, flush=True)

    bg = DATA.load_bg_pool(a.tiles, limit=400, rng=rng)
    print("bg pool:", len(bg), flush=True)

    net = StyleEncoder().to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=2e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, a.steps)

    net.train(); t0 = time.time()
    for step in range(a.steps):
        xs, ys = DATA.make_batch(a.bs, rng, bg)
        xs = torch.from_numpy(xs).to(dev); ys = torch.from_numpy(ys).to(dev)
        z = net(xs)
        loss = supcon_loss(z, ys)
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        if step % 200 == 0:
            print(f"step {step:5d} loss {loss.item():.4f} ({time.time()-t0:.0f}s)", flush=True)

    torch.save(net.state_dict(), os.path.join(a.out, "encoder.pt"))

    # ---- synthetic validation: separation of held-out crops ----
    net.eval()
    vx, vy = DATA.make_batch(1200, np.random.RandomState(999), bg)
    with torch.no_grad():
        vz = net(torch.from_numpy(vx).to(dev)).cpu().numpy()
    # 5-NN leave-in accuracy (train on half, test on half)
    n = len(vy); h = n // 2
    knn = KNeighborsClassifier(5).fit(vz[:h], vy[:h])
    acc = float((knn.predict(vz[h:]) == vy[h:]).mean())
    sil = float(silhouette_score(vz, vy))
    rep = dict(knn_acc=acc, silhouette=sil, classes=F.CLASS_NAMES,
               steps=a.steps, n_val=n)
    json.dump(rep, open(os.path.join(a.out, "synth_val.json"), "w"), indent=2)
    print("SYNTH VAL:", json.dumps(rep), flush=True)
    print(f"  knn_acc={acc:.3f} (chance={1/len(F.CLASS_NAMES):.3f}), silhouette={sil:.3f}", flush=True)

if __name__ == "__main__":
    main()
