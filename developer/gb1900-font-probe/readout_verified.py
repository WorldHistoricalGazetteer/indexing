"""The first discriminator number resting on HUMAN-VERIFIED labels in the production crop convention.

Phase B measured 0.596 maxsim-LOO on 188 anchors, but `upright·solid·serif` collapsed to 0.071 at n=14 —
swallowed by the 47% italic majority. A labelling round has now verified 231 cards drawn from that class's weak
candidates, which takes it to ~130 examples. This asks the only question that matters: does the weak axis
separate once it has data, or is it a genuine limit of the descriptor?

Reports BEFORE (Phase B anchors alone) and AFTER (anchors + verified) so the change is attributable, and always
per-signature, because a headline that moves while one class stays at zero is not progress.

Note what the labelling round itself already showed: of 240 cards the lexicon called `upright·solid·serif`,
the human confirmed 116. The weak labels were right about 48% of the time — which is exactly why they seed
sampling and never supply a number.

    python readout_verified.py --labels "pool_labels_round (4).json"
"""
import argparse, glob, json, os, numpy as np
from collections import Counter

BANK = "/vast/ishi/gb1900/edition/pins/desc/shard_*.npz"
ANCHORS = "/vast/ishi/gb1900/edition/spot/anchor_desc_hisam.npz"


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def norm(X):
    X = np.asarray(X, np.float32)
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)


def loo_maxsim(X, S, per_class=False):
    X = norm(X)
    sl = sorted(set(S))
    cols = {s: np.where(S == s)[0] for s in sl}
    sim = X @ X.T
    np.fill_diagonal(sim, -2)
    ok = 0
    hit = {s: 0 for s in sl}
    for i in range(len(X)):
        sc = [sim[i, cols[s][cols[s] != i]].max() if len(cols[s][cols[s] != i]) else -9 for s in sl]
        good = sl[int(np.argmax(sc))] == S[i]
        ok += good
        hit[S[i]] += good
    if per_class:
        return ok / len(X), {s: (hit[s], len(cols[s])) for s in sl}
    return ok / len(X)


def loo_knn(X, S, k=5):
    X = norm(X)
    sim = X @ X.T
    np.fill_diagonal(sim, -2)
    ok = 0
    for i in range(len(X)):
        nn = np.argsort(-sim[i])[:k]
        ok += (Counter(S[j] for j in nn).most_common(1)[0][0] == S[i])
    return ok / len(X)


def report(name, X, S):
    acc, per = loo_maxsim(X, S, per_class=True)
    print(f"\n{name}: {len(S)} labels / {len(set(S))} sigs "
          f"(majority {max(Counter(S).values())/len(S):.2f})", flush=True)
    print(f"  maxsim-LOO {acc:.3f}   kNN5 {loo_knn(X, S):.3f}", flush=True)
    for s in sorted(per, key=lambda z: -per[z][1]):
        h, n = per[s]
        print(f"    {s:32s} n={n:<4d} {h/n:.3f}", flush=True)
    return acc, per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, help="labelling export (records with gcx/gcy/sig)")
    ap.add_argument("--bank", default=BANK)
    ap.add_argument("--anchors", default=ANCHORS)
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/pins/readout_verified.json")
    a = ap.parse_args()

    lab = json.load(open(a.labels))
    verified = {key(x["gcx"], x["gcy"]): x["sig"] for x in lab if x.get("sig")}
    print(f"{len(lab)} cards labelled, {len(verified)} with a signature "
          f"({len(lab)-len(verified)} left unclear by the labeller)", flush=True)
    print("  labeller's verdict on the lexicon's proposal:", flush=True)
    for s, c in Counter(v for v in verified.values()).most_common():
        print(f"    {s:32s} {c:>4d}", flush=True)

    D, Sv = [], []
    for f in sorted(glob.glob(a.bank)):
        d = np.load(f, allow_pickle=True)
        for i in range(len(d["desc"])):
            k = key(d["gcx"][i], d["gcy"][i])
            if k in verified:
                D.append(d["desc"][i].astype(np.float32))
                Sv.append(verified[k])
    D = np.array(D)
    Sv = np.array(Sv)
    print(f"\njoined {len(D)}/{len(verified)} verified labels to bank descriptors", flush=True)

    z = np.load(a.anchors, allow_pickle=True)
    kd = "desc_line" if "desc_line" in z.files else "desc"
    A = z[kd].astype(np.float32)
    Sa = np.array([str(s) for s in z["sigs"]])

    before, per_b = report("BEFORE — Phase B anchors only", A, Sa)
    _ = report("VERIFIED ONLY — this round's 231", D, Sv)
    X = np.vstack([norm(A), norm(D)])
    S = np.concatenate([Sa, Sv])
    after, per_a = report("AFTER — anchors + verified", X, S)

    print(f"\nweak axis `upright·solid·serif`: "
          f"{per_b.get('upright·solid·serif', (0,0))[1]} -> {per_a.get('upright·solid·serif', (0,0))[1]} examples, "
          f"{per_b.get('upright·solid·serif', (0,1))[0]/max(1,per_b.get('upright·solid·serif',(0,1))[1]):.3f} -> "
          f"{per_a.get('upright·solid·serif', (0,1))[0]/max(1,per_a.get('upright·solid·serif',(0,1))[1]):.3f}",
          flush=True)
    print(f"overall maxsim-LOO {before:.3f} -> {after:.3f}", flush=True)
    print("NB the two runs are not directly comparable as headline numbers — the label mix changed. The "
          "per-signature rows are the honest comparison.", flush=True)

    json.dump(dict(before=before, after=after,
                   per_class_before={k: list(v) for k, v in per_b.items()},
                   per_class_after={k: list(v) for k, v in per_a.items()},
                   verified=len(D), unclear=len(lab) - len(verified),
                   lexicon_precision=Counter(verified.values()).get("upright·solid·serif", 0) / max(1, len(verified))),
              open(a.out, "w"), indent=2, ensure_ascii=False)
    print(f"\nwrote {a.out}\nVERIFIEDDONE", flush=True)


if __name__ == "__main__":
    main()
