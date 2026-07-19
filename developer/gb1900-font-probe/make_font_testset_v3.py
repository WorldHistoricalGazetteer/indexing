"""Phase C (2) — round-2 HITL font labeller on the ENLARGED spotter pool (~20 sheets).

Grows the human reference. Samples NEW boxes (excludes everything already labelled in round 1), boosts the
thin classes (blackletter via antiquity terms, upright via church/station/civic terms) — sampling is for
COVERAGE only; the font label comes from the reviewer. De-rotated 220px crops. Freezes the sample to
font_testset_v3_boxes.json so round-2 validation is pool-independent.

    /vast/ishi/envs/boundary/bin/python make_font_testset_v3.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, re, glob, json, random
import concurrent.futures as cf
from collections import Counter
from make_font_testset import HTML
from make_font_testset_v2 import make_crop, SPOT

OUT = f"{SPOT}/font_testset_v3.html"; BOXES_OUT = f"{SPOT}/font_testset_v3_boxes.json"
V2_BOXES = f"{SPOT}/font_testset_v2_boxes.json"
random.seed(7)
ANTIQ = re.compile(r"(Tumul|Cairn|Camp|Barrow|Earthwork|Enclosure|Castle|Moat|Priory|Abbey|Fort|Stone|Cross|Tower|Britsuh|British|Roman|Site)", re.I)
UPRIGHT = re.compile(r"(Church|Chapel|\bCh\b|Station|\bSta\b|Hospital|School|College|Works|Mill|Wood|Copse|Bay|Harbour)", re.I)
WATER = re.compile(r"(River|Brook|Burn|Beck|Nant|Afon|Stream|Canal|Well|Pool|Mere|Lake|Ford)", re.I)
N_ANTIQ, N_UP, N_WATER, N_RAND = 85, 80, 45, 50

def main():
    labelled = {tuple(r[k] for k in ("gcx", "gcy")) for r in json.load(open(V2_BOXES))}
    boxes = []
    for fp in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(fp):
            r = json.loads(line)
            if r["score"] >= 0.55 and len([c for c in r["text"] if c.isalnum()]) >= 3 \
               and (r["gcx"], r["gcy"]) not in labelled:
                boxes.append(r)
    print(f"unlabelled boxes: {len(boxes)}", flush=True)
    antiq = [r for r in boxes if ANTIQ.search(r["text"])]
    upr = [r for r in boxes if UPRIGHT.search(r["text"])]
    water = [r for r in boxes if WATER.search(r["text"])]
    picked = {}
    for pool, n in [(antiq, N_ANTIQ), (upr, N_UP), (water, N_WATER)]:
        random.shuffle(pool)
        for r in pool[:n]: picked[(r["gcx"], r["gcy"])] = r
    rest = [r for r in boxes if (r["gcx"], r["gcy"]) not in picked]
    random.shuffle(rest)
    for r in rest[:N_RAND]: picked[(r["gcx"], r["gcy"])] = r
    samp = list(picked.values()); random.shuffle(samp)
    print(f"sampled: {len(samp)}", flush=True)

    crops = []; keep = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for r, c in zip(samp, ex.map(make_crop, samp)):
            if c: crops.append(c); keep.append(r)
    json.dump(keep, open(BOXES_OUT, "w"))                 # frozen sample (matches crop order)
    html = (HTML.replace("data:image/png", "data:image/jpeg")
                .replace("minmax(230px,1fr)", "minmax(560px,1fr)")
                .replace("min-height:70px", "min-height:250px")
                .replace(".imgwrap img{{image-rendering:auto;max-width:100%}}", ".imgwrap img{{image-rendering:auto}}"))
    open(OUT, "w").write(html.format(crops=json.dumps(crops)))
    print(f"cropped: {len(crops)}; wrote {OUT} ({os.path.getsize(OUT)//1024} KB) + froze {BOXES_OUT}", flush=True)

if __name__ == "__main__":
    main()
