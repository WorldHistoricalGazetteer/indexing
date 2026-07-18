"""Batch-2 labelling manifest: use the encoder to HUNT the rare styles that batch-1 missed.
For each class, rank real crops by cosine to that class's SYNTHETIC prototype and take the
nearest — oversampling the classes with no batch-1 anchors (outline/blackletter/engraved/spaced/sans).
Deterministic pool (seed 0, same as train) so ids c... /t_ map back to pool[i] for anchoring.

    python build_batch2.py --enc out/encoder.pt --boxes GLOB --tiles DIR... --out out \
        --exclude font_labels.json --nmax 2500
"""
import argparse, os, glob, json, numpy as np, torch
import data as DATA, fonts as F, degrade as D
import build_label_manifest as blm
from model import StyleEncoder

# how many candidates to surface per class (heavy on the missing ones)
TARGET_K = {"caps_outline": 40, "blackletter": 40, "engraved_caps": 40, "caps_spaced": 40,
            "sans": 35, "slab": 20, "serif_italic": 12, "serif_upright": 12, "road_caps": 15}
PRIORITY = ["caps_outline", "blackletter", "engraved_caps", "caps_spaced", "sans",
            "slab", "road_caps", "serif_upright", "serif_italic"]

def embed(net, X, dev, bs=256):
    out = []
    with torch.no_grad():
        for i in range(0, len(X), bs):
            out.append(net(torch.from_numpy(X[i:i+bs]).float().to(dev)).cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, 128), np.float32)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", required=True); ap.add_argument("--boxes", required=True)
    ap.add_argument("--tiles", nargs="+", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--exclude", default=None); ap.add_argument("--nmax", type=int, default=2500)
    ap.add_argument("--proto_n", type=int, default=120); ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    net = StyleEncoder().to(dev); net.load_state_dict(torch.load(a.enc, map_location=dev)); net.eval()
    rng = np.random.RandomState(a.seed)

    bg = DATA.load_bg_pool(a.tiles, limit=300, rng=rng)
    # synthetic class prototypes (mean embedding per class)
    protos = {}
    for c in F.CLASS_NAMES:
        Xs = np.zeros((a.proto_n, 1, D.TARGET_H, 192), np.float32)
        for k in range(a.proto_n):
            ink = F.render_ink(F.random_text(rng), F.CLASSES[c], rng)
            Xs[k, 0] = DATA.norm1(DATA.to_fixed01(D.composite(ink, bg[rng.randint(len(bg))], rng,
                                                              road=(c == "road_caps"))))
        z = embed(net, Xs, dev); m = z.mean(0); protos[c] = m / (np.linalg.norm(m) + 1e-9)
    print("prototypes:", list(protos), flush=True)

    pool, kept = DATA.load_real_and_kept(a.boxes, a.tiles, a.nmax, rng)
    Zr = embed(net, np.stack([DATA.norm1(p) for p in pool])[:, None].astype(np.float32), dev)
    print("real embedded:", len(Zr), flush=True)

    used = set()
    if a.exclude and os.path.exists(a.exclude):
        for r in json.load(open(a.exclude)):
            try: used.add(int(r["id"].split("_")[-1]))
            except Exception: pass
    print("excluded (already labelled):", len(used), flush=True)

    samples = []
    for c in PRIORITY:
        sims = Zr @ protos[c]; order = np.argsort(-sims); take = 0
        for i in order:
            i = int(i)
            if i in used: continue
            raw = DATA.crop_box(kept[i]["gpoly"], a.tiles, do_flatten=False)
            if raw is None: continue
            used.add(i)
            samples.append(dict(id=f"c_{i}", text=kept[i].get("text", ""), cluster=f"~{c}",
                                cap_h_m=round(blm.cap_height_m(kept[i]["gpoly"]), 1),
                                crop=blm.to_datauri(raw), sim=round(float(sims[i]), 3)))
            take += 1
            if take >= TARGET_K[c]: break
        print(f"  {c}: {take}", flush=True)
    rng.shuffle(samples)

    refs = {c: [blm.clean_render(w, c, np.random.RandomState(k)) for k, w in enumerate(blm.REF_WORDS[c])]
            for c in F.CLASS_NAMES}
    manifest = dict(classes=F.CLASS_NAMES + ["numeral", "abbrev", "ambiguous"],
                    class_desc=blm.CLASS_DESC, references=refs, samples=samples)
    outp = os.path.join(a.out, "manifest_label2.json")
    json.dump(manifest, open(outp, "w"), ensure_ascii=False)
    print("WROTE", outp, os.path.getsize(outp), "bytes;", len(samples), "samples", flush=True)

if __name__ == "__main__":
    main()
