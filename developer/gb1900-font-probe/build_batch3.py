"""Batch-3 labelling manifest at z17: broad stratified sample (region common fonts + antiquity
blackletter + urban caps) with auto-style HINTS, for confirming auto-labels and adding anchors for
the thin classes (slab/slab_italic/outline) and strengthening the serif upright/italic axis.
    python build_batch3.py --out out_z17
"""
import argparse, os, json, math, numpy as np
import data as DATA, crnn_data as CD
import build_label_manifest as blm
import fonts as F

TILES17 = ["/vast/ishi/gb1900/tiles17"]
BOXES = "/vast/ishi/gb1900/probe/mapreader_text/region/boxes/worker*.jsonl"
NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
ANTIQ = [(4083, 2619), (4078, 2628), (4037, 2753), (4052, 2727), (4054, 2736), (4085, 2619)]
URBAN = [(4044, 2650), (4045, 2650), (4044, 2649), (4045, 2649)]

def z16blk(lon, lat):
    x = int((lon + 180) / 360 * (2**16))
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * (2**16))
    return x // 8, y // 8

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True)
    ap.add_argument("--n_region", type=int, default=90); ap.add_argument("--n_antiq", type=int, default=45)
    ap.add_argument("--n_urban", type=int, default=45); a = ap.parse_args()
    rng = np.random.RandomState(1); samples = []

    # region (raw z17 display crops from spotter boxes) — serif axis + variety
    _, kept = DATA.load_real_and_kept(BOXES, ["/vast/ishi/gb1900/probe/mapreader_text/region/tiles",
                                             "/vast/ishi/gb1900/tiles/16"], 2500, np.random.RandomState(0))
    idx = rng.permutation(len(kept))
    for i in idx:
        if sum(1 for s in samples if s["cluster"] == "region") >= a.n_region: break
        c = DATA.crop_box(kept[int(i)]["gpoly"], TILES17, scale=2, do_flatten=False)
        if c is None: continue
        t = kept[int(i)].get("text", "")
        samples.append(dict(id=f"c_{int(i)}", text=t, cluster="region",
                            cap_h_m=round(blm.cap_height_m(kept[int(i)]["gpoly"]) * 2, 1),
                            hint=CD.auto_style(t) or "", crop=blm.to_datauri(c)))

    # antiquity + urban crowd (raw z17 display crops) — blackletter + caps to confirm
    def crowd(blocks, tag, n):
        blocks = set(blocks); got = 0
        for line in open(NT):
            if got >= n: break
            try: d = json.loads(line)
            except Exception: continue
            tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
            lon, lat = d.get("lon"), d.get("lat")
            if not (tv and lon and lat) or z16blk(lon, lat) not in blocks: continue
            hint = CD.auto_style(tv.strip())
            if tag == "antiquity" and hint != "blackletter": continue
            c = DATA.crop_point(lon, lat, TILES17, do_flatten=False)
            if c is None: continue
            samples.append(dict(id=f"x_{tag}_{got}", text=tv.strip(), cluster=tag,
                                cap_h_m=None, hint=hint or "", crop=blm.to_datauri(c))); got += 1
    crowd(ANTIQ, "antiquity", a.n_antiq); crowd(URBAN, "urban", a.n_urban)
    rng.shuffle(samples)

    refs = {c: [blm.clean_render(w, c, np.random.RandomState(k)) for k, w in enumerate(blm.REF_WORDS[c])]
            for c in F.CLASS_NAMES}
    manifest = dict(classes=F.CLASS_NAMES + ["numeral", "abbrev", "ambiguous"],
                    class_desc=blm.CLASS_DESC, references=refs, samples=samples)
    outp = os.path.join(a.out, "manifest_label3.json")
    json.dump(manifest, open(outp, "w"), ensure_ascii=False)
    print("WROTE", outp, os.path.getsize(outp), "bytes;", len(samples), "samples", flush=True)

if __name__ == "__main__":
    main()
