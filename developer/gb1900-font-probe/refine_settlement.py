"""Font-style refinement of the 'settlement' residual: proper-name places where UPRIGHT serif ->
settlement (village/township) and ITALIC serif -> farm/house (building_or_feature). Trains a serif
upright/italic classifier on human + spotter-box lexicon anchors ([CRNN embedding + slant]), then
re-types settlement-residual labels that fall in z17-covered blocks. Soft signal (~.65-.71) so the
refined confidence is modest; upright confirms settlement, italic flips to building_or_feature.
    python refine_settlement.py --crnn out_z17/crnn_z17.pt --vocab out_z17/vocab.json \
        --labels font_labels.json --types /vast/.../gb_stamp_types.jsonl --nt /vast/.../national_typed.jsonl --out out_z17
"""
import argparse, os, json, math, glob, numpy as np, torch
from collections import Counter
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
import data as DATA, crnn_data as CD
from crnn import CRNN
from crnn_eval import crnn_embed
from slant_v2 import slant_deg
from serif_push import LEX, TILES16, TILES17, BOXES

# z17-covered 8x8 blocks (antiquity + urban + region + agricultural)
BLOCKS = {(4083,2619),(4078,2628),(4037,2753),(4052,2727),(4054,2736),(4085,2619),
          (4044,2650),(4045,2650),(4044,2649),(4045,2649),
          (4093,2740),(4047,2744),(4034,2747),(4029,2751),(4105,2729),(4104,2729)}
for bx in range(4030,4036):
    for by in range(2677,2683): BLOCKS.add((bx,by))

def z16blk(lon, lat):
    x = int((lon+180)/360*(2**16))
    y = int((1-math.log(math.tan(math.radians(lat))+1/math.cos(math.radians(lat)))/math.pi)/2*(2**16))
    return x//8, y//8

def feats(net, dev, crops):
    E = crnn_embed(net, [CD._to_h32(c) for c in crops], dev)
    S = np.array([[slant_deg(c)] for c in crops])
    return np.hstack([E, S])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crnn", required=True); ap.add_argument("--vocab", required=True)
    ap.add_argument("--labels", required=True); ap.add_argument("--types", required=True)
    ap.add_argument("--nt", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--nmax", type=int, default=6000)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    voc = json.load(open(a.vocab))
    net = CRNN(n_class=len(voc["stoi"])+1).to(dev); net.load_state_dict(torch.load(a.crnn, map_location=dev)); net.eval()

    # --- train serif clf on human + spotter-box lexicon anchors ---
    _, kept = DATA.load_real_and_kept(BOXES, TILES16, 2500, np.random.RandomState(0))
    ac, ay = [], []
    for r in json.load(open(a.labels)):
        if r["label"] not in ("serif_upright","serif_italic"): continue
        i = int(r["id"].split("_")[-1])
        if i >= len(kept): continue
        c = DATA.crop_box(kept[i]["gpoly"], TILES17, scale=2)
        if c is not None: ac.append(c); ay.append(r["label"])
    boxes = [json.loads(l) for f in glob.glob(BOXES) for l in open(f) if l.strip()]
    for b in boxes:
        t = (b.get("text") or "").strip().lower()
        if t in LEX and LEX[t] in ("serif_upright","serif_italic"):
            c = DATA.crop_box(b["gpoly"], TILES17, scale=2)
            if c is not None: ac.append(c); ay.append(LEX[t])
    ay = np.array(ay); print("serif training anchors:", len(ac), dict(Counter(ay.tolist())), flush=True)
    Xa = feats(net, dev, ac)
    clf = make_pipeline(StandardScaler(), MLPClassifier((64,), alpha=1e-2, max_iter=1000, random_state=0)).fit(Xa, ay)

    # --- settlement-residual pin_ids ---
    settle = set()
    for line in open(a.types):
        d = json.loads(line)
        if d["types"] and d["types"][0][0] == "settlement": settle.add(d["pin_id"])
    print("settlement-residual labels total:", len(settle), flush=True)

    # --- refine those in z17 blocks ---
    refined = {}; crops = []; meta = []; scanned = 0
    for line in open(a.nt):
        if len(crops) >= a.nmax: break
        d = json.loads(line); pid = d.get("pin_id")
        if pid not in settle: continue
        lon, lat = d.get("lon"), d.get("lat")
        if not (lon and lat) or z16blk(lon, lat) not in BLOCKS: continue
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        c = DATA.crop_point(lon, lat, TILES17)
        if c is None: continue
        crops.append(c); meta.append((pid, tv)); scanned += 1
    print("settlement residuals in z17 blocks, cropped:", len(crops), flush=True)
    if crops:
        Xr = feats(net, dev, crops); P = clf.predict_proba(Xr); cls = list(clf.classes_)
        ii = cls.index("serif_italic")
        flips = 0
        for k, (pid, tv) in enumerate(meta):
            pit = float(P[k, ii])
            if pit >= 0.5:   # italic -> farm/house/building
                refined[pid] = {"pin_id": pid, "text": tv,
                                "types": [["building_or_feature", round(pit, 3)], ["settlement", round(1-pit, 3)]]}
                flips += 1
            else:            # upright -> settlement confirmed
                refined[pid] = {"pin_id": pid, "text": tv,
                                "types": [["settlement", round(1-pit, 3)], ["building_or_feature", round(pit, 3)]]}
        with open(os.path.join(a.out, "settlement_refined.jsonl"), "w") as w:
            for v in refined.values(): w.write(json.dumps(v, ensure_ascii=False)+"\n")
        print("refined:", len(refined), "| flipped settlement->building (italic):", flips,
              "| kept settlement (upright):", len(refined)-flips, flush=True)
        json.dump(dict(residual_total=len(settle), refined=len(refined), flipped_to_building=flips),
                  open(os.path.join(a.out,"refine_report.json"),"w"), indent=2)

if __name__ == "__main__":
    main()
