"""Train the B' CRNN on REAL (crop, transcript) pairs. Saves crnn.pt + vocab.json.
    python train_crnn.py --boxes GLOB --tiles DIR... --out out_bprime --epochs 40
"""
import argparse, os, json, time, numpy as np, torch, torch.nn as nn
import crnn_data as CD
from crnn import CRNN

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boxes", required=True); ap.add_argument("--tiles", nargs="+", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--bs", type=int, default=64); ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    rng = np.random.RandomState(a.seed)

    items = CD.harvest(a.boxes, a.tiles, rng=rng)
    print("harvested pairs:", len(items), flush=True)
    stoi, itos = CD.build_vocab(items)
    json.dump({"stoi": stoi, "itos": {str(k): v for k, v in itos.items()}},
              open(os.path.join(a.out, "vocab.json"), "w"))
    print("vocab:", len(stoi), "chars", flush=True)
    nval = max(200, len(items) // 10)
    val, train = items[:nval], items[nval:]

    net = CRNN(n_class=len(stoi) + 1).to(dev)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    ctc = nn.CTCLoss(blank=0, zero_infinity=True)

    t0 = time.time()
    for ep in range(a.epochs):
        net.train(); rng.shuffle(train); tot = 0.0; nb = 0
        for i in range(0, len(train), a.bs):
            batch = train[i:i + a.bs]
            X, tgt, tlen, inp_len = CD.collate(batch, stoi)
            X = torch.from_numpy(X).to(dev)
            logp = net(X)                                   # (T,B,C)
            T = logp.size(0)
            ilens = torch.full((X.size(0),), T, dtype=torch.long)
            loss = ctc(logp, torch.from_numpy(tgt), ilens, torch.from_numpy(tlen))
            opt.zero_grad(); loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), 5.0); opt.step()
            tot += float(loss); nb += 1
        # quick val greedy CER-ish: fraction of exact-word matches on a sample
        if ep % 5 == 0 or ep == a.epochs - 1:
            net.eval(); ok = 0; tot_c = 0
            with torch.no_grad():
                for i in range(0, min(len(val), 500), a.bs):
                    batch = val[i:i + a.bs]
                    X, _, _, _ = CD.collate(batch, stoi)
                    arg = net(torch.from_numpy(X).to(dev)).argmax(2).permute(1, 0).cpu().numpy()  # (B,T)
                    for b, it in enumerate(batch):
                        prev = -1; s = ""
                        for x in arg[b]:
                            if x != 0 and x != prev: s += itos.get(int(x), "")
                            prev = x
                        ok += int(s == it["text"]); tot_c += 1
            print(f"ep {ep:3d} loss {tot/max(1,nb):.3f} val_exact {ok}/{tot_c} ({time.time()-t0:.0f}s)", flush=True)

    torch.save(net.state_dict(), os.path.join(a.out, "crnn.pt"))
    print("saved crnn.pt", flush=True)

if __name__ == "__main__":
    main()
