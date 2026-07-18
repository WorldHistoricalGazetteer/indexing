"""Font-style encoder training.
iter2: synthetic SupCon + unsupervised real-crop consistency.
iter3 (--labels): ALSO a supervised anchor term — human-labelled real crops enter the SupCon
batch under a shared label space, pulling real styles onto the synthetic class structure.

total = supcon(synthetic + real anchors, shared labels) + lam * nt_xent(unlabelled real, 2 views)
Usage:
  python train.py --tiles DIR... --boxes GLOB --out OUT --steps 4000 [--labels labels.json --lam 0.5]
"""
import argparse, os, time, json, numpy as np, torch
from sklearn.metrics import silhouette_score
from sklearn.neighbors import KNeighborsClassifier
import data as DATA, fonts as F
from model import StyleEncoder, supcon_loss, nt_xent

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiles", nargs="+", required=True)
    ap.add_argument("--boxes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--labels", default=None, help="font_labels.json -> few-shot real anchors (iter3)")
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

    CLASSES_ALL = F.CLASS_NAMES + ["numeral", "abbrev"]     # shared label space (synth 0..8 + real-only)
    idx = {c: i for i, c in enumerate(CLASSES_ALL)}
    print("device:", dev, "| label space:", CLASSES_ALL, flush=True)

    bg = DATA.load_bg_pool(a.tiles, limit=400, rng=rng)
    pool, kept = DATA.load_real_and_kept(a.boxes, a.tiles, a.real_n, rng)
    print("bg pool:", len(bg), "| real pool:", len(pool), flush=True)

    Xa = ya = None
    if a.labels:
        Xa_np, ya_names = DATA.load_anchors(a.labels, pool)
        if len(Xa_np):
            Xa = torch.from_numpy(Xa_np).to(dev)
            ya = torch.tensor([idx[n] for n in ya_names], device=dev)
            from collections import Counter
            print("ANCHORS:", len(Xa), dict(Counter(ya_names)), flush=True)

    net = StyleEncoder().to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=2e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, a.steps)

    net.train(); t0 = time.time()
    for step in range(a.steps):
        xs, ys = DATA.make_batch(a.bs, rng, bg)
        xs = torch.from_numpy(xs).to(dev); ys = torch.from_numpy(ys).to(dev)
        if Xa is not None:                                  # fold anchors into the SupCon batch
            z = net(torch.cat([xs, Xa]))
            lsup = supcon_loss(z, torch.cat([ys, ya]))
        else:
            lsup = supcon_loss(net(xs), ys)
        if pool and a.lam > 0:
            v1, v2 = DATA.real_pair_batch(pool, a.real_bs, rng)
            zc = net(torch.from_numpy(np.concatenate([v1, v2])).to(dev))
            lcon = nt_xent(zc[:a.real_bs], zc[a.real_bs:])
        else:
            lcon = torch.zeros((), device=dev)
        loss = lsup + a.lam * lcon
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        if step % 200 == 0:
            print(f"step {step:5d} sup {lsup.item():.4f} con {float(lcon):.4f} ({time.time()-t0:.0f}s)", flush=True)

    torch.save(net.state_dict(), os.path.join(a.out, "encoder.pt"))

    net.eval()
    vx, vy = DATA.make_batch(1200, np.random.RandomState(999), bg)
    with torch.no_grad():
        vz = net(torch.from_numpy(vx).to(dev)).cpu().numpy()
    n = len(vy); h = n // 2
    acc = float((KNeighborsClassifier(5).fit(vz[:h], vy[:h]).predict(vz[h:]) == vy[h:]).mean())
    sil = float(silhouette_score(vz, vy))
    rep = dict(knn_acc=acc, silhouette=sil, classes=F.CLASS_NAMES, steps=a.steps, lam=a.lam,
               real_pool=len(pool), anchors=(int(len(Xa)) if Xa is not None else 0))
    # anchor leave-one-out accuracy (does the embedding classify real styles?)
    if Xa is not None and len(Xa) > 6:
        with torch.no_grad():
            za = net(Xa).cpu().numpy()
        yn = np.array([idx[n] for n in ya_names])
        from sklearn.model_selection import LeaveOneOut
        from sklearn.neighbors import KNeighborsClassifier as KNN
        preds = []
        for tr, te in LeaveOneOut().split(za):
            k = min(3, len(tr))
            preds.append(KNN(k).fit(za[tr], yn[tr]).predict(za[te])[0])
        rep["anchor_loo_acc"] = float((np.array(preds) == yn).mean())
    json.dump(rep, open(os.path.join(a.out, "synth_val.json"), "w"), indent=2)
    print("VAL:", json.dumps(rep), flush=True)

if __name__ == "__main__":
    main()
