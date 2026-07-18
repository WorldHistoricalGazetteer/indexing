"""The payoff: embed REAL spotter crops with the synthetic-trained encoder, cluster, and
check whether the treatments the VLM confused (outline caps, italic, slab, blackletter,
spaced caps) fall into distinct clusters.

Outputs to --out:
  clusters/cluster_XX.png   montage per HDBSCAN cluster (real spotter crops)
  hitl_by_cluster.png       the 78 VLM-labelled crops, grouped by discovered cluster
  report.json               sizes, silhouette, and cluster x VLM-os_style cross-tab
Usage:
  python embed_cluster.py --enc OUT/encoder.pt --boxes 'REGION/boxes/worker*.jsonl' \
       --tiles REGION/tiles /vast/ishi/gb1900/tiles/16 --hitl HITL/manifest_clean.json \
       --out OUT --nmax 2500
"""
import argparse, os, glob, io, json, base64, numpy as np, torch
from collections import defaultdict, Counter
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from sklearn.cluster import HDBSCAN, KMeans
from sklearn.metrics import silhouette_score
import data as DATA, fonts as F
from model import StyleEncoder

# real spotter boxes are cropped + paper-flattened by data.crop_box (shared with train.py)

def montage(crops, texts, path, title, per_row_w=1500):
    LH = 64; rows = [[]]; rw = 0
    ims = []
    for c in crops:
        im = c if isinstance(c, Image.Image) else Image.fromarray((np.clip(c, 0, 1) * 255).astype(np.uint8))
        im = im.convert("L")
        if im.height != LH:
            im = im.resize((max(1, int(im.width * LH / im.height)), LH))
        ims.append(im)
    for im, t in zip(ims, texts):
        if rw + im.width + 8 > per_row_w and rows[-1]:
            rows.append([]); rw = 0
        rows[-1].append((im, t)); rw += im.width + 8
    Hh = 22 + len(rows) * (LH + 16)
    canvas = Image.new("RGB", (per_row_w, Hh), (255, 255, 255))
    d = ImageDraw.Draw(canvas); d.text((6, 4), title, fill=(180, 0, 0))
    y = 22
    for r in rows:
        x = 4
        for im, t in r:
            canvas.paste(im.convert("RGB"), (x, y))
            d.rectangle([x - 1, y - 1, x + im.width, y + LH], outline=(210, 210, 210))
            d.text((x, y + LH), str(t)[:16], fill=(0, 0, 170))
            x += im.width + 8
        y += LH + 16
    canvas.save(path)

def embed(net, X, dev, bs=256):
    out = []
    net.eval()
    with torch.no_grad():
        for i in range(0, len(X), bs):
            xb = torch.from_numpy(X[i:i + bs]).float().to(dev)
            out.append(net(xb).cpu().numpy())
    return np.concatenate(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", required=True)
    ap.add_argument("--boxes", required=True)
    ap.add_argument("--tiles", nargs="+", required=True)
    ap.add_argument("--hitl", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--nmax", type=int, default=2500)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "clusters"), exist_ok=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    net = StyleEncoder().to(dev)
    net.load_state_dict(torch.load(a.enc, map_location=dev))
    rng = np.random.RandomState(a.seed)

    # ---- real region boxes ----
    boxes = []
    for f in glob.glob(a.boxes):
        for line in open(f):
            line = line.strip()
            if line: boxes.append(json.loads(line))
    rng.shuffle(boxes)
    crops, texts, X = [], [], []
    for b in boxes:
        if len(X) >= a.nmax: break
        c = DATA.crop_box(b["gpoly"], a.tiles)      # flattened 0..1 array (or None)
        if c is None: continue
        crops.append(c); texts.append(b.get("text", ""))
        X.append(DATA.crop_to_fixed(c))
    X = np.stack(X)[:, None]
    print("real crops embedded:", len(X), flush=True)
    Z = embed(net, X, dev)

    # ---- cluster ----
    hdb = HDBSCAN(min_cluster_size=max(15, len(X) // 60), min_samples=5)
    lab = hdb.fit_predict(Z)
    km = KMeans(len(F.CLASS_NAMES), n_init=10, random_state=0).fit(Z)
    kl = km.labels_
    valid = lab >= 0
    sil = float(silhouette_score(Z[valid], lab[valid])) if valid.sum() > len(set(lab[valid])) > 1 else None
    sizes = Counter(lab.tolist())
    print("HDBSCAN clusters:", {int(k): v for k, v in sorted(sizes.items())}, "silhouette:", sil, flush=True)

    # montage per HDBSCAN cluster
    for cl in sorted(set(lab)):
        idx = np.where(lab == cl)[0][:60]
        name = "noise" if cl == -1 else f"{cl:02d}"
        montage([crops[i] for i in idx], [texts[i] for i in idx],
                os.path.join(a.out, "clusters", f"cluster_{name}.png"),
                f"HDBSCAN cluster {name}  (n={sizes[cl]})")

    # ---- HITL 78: embed, assign to KMeans centroid, cross-tab vs VLM os_style ----
    hx, htext, hstyle = DATA.load_hitl(a.hitl)
    hz = embed(net, hx.astype(np.float32), dev)
    hk = km.predict(hz)
    xtab = defaultdict(Counter)
    for st, k in zip(hstyle, hk):
        xtab[st][int(k)] += 1
    # montage of HITL grouped by discovered KMeans cluster
    order = np.argsort(hk)
    montage([hx[i, 0] for i in order], [f"{hstyle[i]}|{htext[i]}" for i in order],
            os.path.join(a.out, "hitl_by_cluster.png"),
            "HITL 78 crops grouped by discovered cluster (label = VLM os_style | text)")

    rep = dict(
        n_real=len(X), n_hdbscan_clusters=len([c for c in sizes if c >= 0]),
        n_noise=int(sizes.get(-1, 0)), silhouette=sil,
        hdbscan_sizes={int(k): int(v) for k, v in sizes.items()},
        vlm_style_to_kmeans={st: dict(c) for st, c in xtab.items()},
        classes=F.CLASS_NAMES)
    json.dump(rep, open(os.path.join(a.out, "report.json"), "w"), indent=2)
    print("REPORT:", json.dumps(rep, indent=2), flush=True)

if __name__ == "__main__":
    main()
