"""Build the labelling-HITL manifest: a stratified real-crop sample (grouped by the iter-2
cluster for fast labelling) + a clean synthetic REFERENCE exemplar per style class.

Output manifest_label.json = {classes, class_desc, references{cls:[dataURI]}, samples[{id,text,cluster,crop}]}
Human labels each real crop -> class (few-shot anchors for iteration 3). Runs on CRC (encoder.pt).
    python build_label_manifest.py --enc out/encoder.pt --boxes 'REGION/boxes/worker*.jsonl' \
        --tiles REGION/tiles /vast/.../tiles/16 --out out --nmax 2500 --per_cluster 45 --noise 90
"""
import argparse, os, glob, io, json, base64, numpy as np, torch
from collections import defaultdict
from PIL import Image
from scipy import ndimage as ndi
from sklearn.cluster import HDBSCAN
import data as DATA, fonts as F, degrade as D
from model import StyleEncoder

CLASS_DESC = {
    "serif_upright": "Upright serif, mixed-case — general place names",
    "serif_italic":  "Italic serif — water features, some names",
    "slab":          "Slab / Egyptian (uniform thick strokes)",
    "slab_italic":   "Italic slab / Egyptian (italic uniform strokes) - larger features",
    "sans":          "Sans-serif",
    "caps_spaced":   "WIDE letter-spaced capitals — parish / township names",
    "road_caps":     "Small solid capitals BETWEEN road casing lines — road/street names",
    "caps_outline":  "Outline / hollow capitals",
    "blackletter":   "Black-letter / Gothic — antiquities",
    "engraved_caps": "Inscriptional / engraved capitals",
}
REF_WORDS = {
    "serif_upright": ["Coppice", "Woodhall"], "serif_italic": ["Grange", "Meadow"],
    "slab": ["Whitton", "Bank"], "slab_italic": ["Acres", "Great"], "sans": ["Stanley", "Newmill"],
    "caps_spaced": ["LONGMOOR", "PENFIELD"], "road_caps": ["HIGH STREET", "MILL ROAD"],
    "caps_outline": ["COTON", "REDMERE"], "blackletter": ["Priory", "Abbey"],
    "engraved_caps": ["UPTON", "GREAT"],
}

def cap_height_m(gpoly):
    """box height in ground metres (z16 px x ~res(lat)) — the size axis, orthogonal to style."""
    import math
    ys = [p[1] for p in gpoly]
    yy = (min(ys) + max(ys)) / 2 / 256.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / (2**16)))))
    return (max(ys) - min(ys)) * 40075016.686 * math.cos(math.radians(lat)) / (2**24)

def to_datauri(a01, h=52):
    a = np.clip(a01, 0, 1)
    if a.shape[0] != h:
        a = ndi.zoom(a, (h / a.shape[0], h / a.shape[0]), order=1)
    im = Image.fromarray((a * 255).astype(np.uint8), "L")
    buf = io.BytesIO(); im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def clean_render(text, cls, rng):
    """clean, minimally-degraded exemplar of a class (the reference)."""
    ink = F.render_ink(text, F.CLASSES[cls], rng)
    ink = ndi.gaussian_filter(np.clip(ink, 0, 1), 0.6)
    H = int(ink.shape[0] * 1.5); W = ink.shape[1] + 24
    img = np.ones((H, W), np.float32)
    oy = (H - ink.shape[0]) // 2; ox = 12
    a = np.zeros_like(img); a[oy:oy + ink.shape[0], ox:ox + ink.shape[1]] = ink
    img = img * (1 - 0.85 * a)
    if cls == "road_caps":
        img = D._add_road_casing(img, oy, ink.shape[0], rng)
    return to_datauri(img)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", required=True); ap.add_argument("--boxes", required=True)
    ap.add_argument("--tiles", nargs="+", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--nmax", type=int, default=2500); ap.add_argument("--per_cluster", type=int, default=45)
    ap.add_argument("--noise", type=int, default=90); ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    net = StyleEncoder().to(dev); net.load_state_dict(torch.load(a.enc, map_location=dev)); net.eval()
    rng = np.random.RandomState(a.seed)

    boxes = []
    for f in glob.glob(a.boxes):
        for line in open(f):
            line = line.strip()
            if line: boxes.append(json.loads(line))
    rng.shuffle(boxes)
    kept, X = [], []
    for b in boxes:
        if len(X) >= a.nmax: break
        c = DATA.crop_box(b["gpoly"], a.tiles)             # flattened (for embedding)
        if c is None: continue
        kept.append(b); X.append(DATA.crop_to_fixed(c))
    X = np.stack(X)[:, None].astype(np.float32)
    with torch.no_grad():
        Z = np.concatenate([net(torch.from_numpy(X[i:i+256]).to(dev)).cpu().numpy()
                            for i in range(0, len(X), 256)])
    lab = HDBSCAN(min_cluster_size=max(15, len(X)//60), min_samples=5).fit_predict(Z)

    # stratified sample: per cluster + noise
    by = defaultdict(list)
    for i, cl in enumerate(lab): by[int(cl)].append(i)
    samples = []
    for cl, idxs in sorted(by.items()):
        take = a.noise if cl == -1 else a.per_cluster
        pick = [idxs[j] for j in rng.permutation(len(idxs))[:take]]
        for i in pick:
            raw = DATA.crop_box(kept[i]["gpoly"], a.tiles, do_flatten=False)   # natural, for display
            if raw is None: continue
            samples.append(dict(id=f"c{cl}_{i}", text=kept[i].get("text", ""),
                                cluster=("noise" if cl == -1 else str(cl)),
                                cap_h_m=round(cap_height_m(kept[i]["gpoly"]), 1), crop=to_datauri(raw)))
    rng.shuffle(samples)  # avoid the labeller anchoring on cluster order (cluster still shown as hint)

    refs = {c: [clean_render(w, c, np.random.RandomState(k)) for k, w in enumerate(REF_WORDS[c])]
            for c in F.CLASS_NAMES}
    manifest = dict(classes=F.CLASS_NAMES + ["numeral", "abbrev", "ambiguous"],
                    class_desc=CLASS_DESC, references=refs, samples=samples)
    outp = os.path.join(a.out, "manifest_label.json")
    json.dump(manifest, open(outp, "w"), ensure_ascii=False)
    print("WROTE", outp, os.path.getsize(outp), "bytes;", len(samples), "samples", flush=True)

if __name__ == "__main__":
    main()
