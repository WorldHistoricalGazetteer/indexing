"""Classify the font of every spotter box (the validated hybrid: SSL encoder + same-letter kNN vs the
human reference), writing boxes_font.jsonl {lon,lat,gcx,gcy,text,font,conf,nchar}. Feeds the GB-STAMP
fusion. Confidence is the margin-weighted per-box agreement; downstream keeps only conf>=gate."""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, glob, json, numpy as np, torch
import concurrent.futures as cf
from collections import Counter, defaultdict
from ssl_pretrain import Enc
from ssl_eval import cased, harvest, SETS, STYLES
from make_font_testset_v2 import derotate

SPOT = "/vast/ishi/gb1900/edition/spot"
ENC = os.environ.get("ENC", f"{SPOT}/encoder_full.pt")
OUT = f"{SPOT}/boxes_font.jsonl"

def main():
    net = Enc(); net.load_state_dict(torch.load(ENC, map_location="cpu")); net.eval()
    # reference: human-labelled glyphs -> embeddings
    ref = []
    for bf, df in SETS: ref += harvest(bf, df)                 # [(font, [(L,cap,glyph)])]
    rg = [(L, cap, g, f) for f, gl in ref for (L, cap, g) in gl]
    rl = np.array([c[0] for c in rg]); rc = np.array([c[1] for c in rg]); rf = np.array([c[3] for c in rg])
    with torch.no_grad():
        RX = np.stack([c[2] for c in rg]).astype(np.float32)[:, None] / 255.0
        RZ = net(torch.tensor((RX - 0.8) / 0.3)).numpy()
    print(f"reference glyphs: {len(rg)} fonts {dict(Counter(rf.tolist()))}", flush=True)

    boxes = []
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(f):
            r = json.loads(line)
            if r.get("score", 0) >= 0.55 and len([c for c in r["text"] if c.isalnum()]) >= 2: boxes.append(r)
    print(f"boxes to classify: {len(boxes)}", flush=True)

    def prep(r):
        patch = derotate(r)
        if patch is None: return None
        gl = cased(patch, r["text"])
        return (r, gl) if gl else None
    prepped = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for res in ex.map(prep, boxes):
            if res: prepped.append(res)
    print(f"prepped (glyphs extracted): {len(prepped)}", flush=True)

    fout = open(OUT, "w"); n = 0; dist = Counter()
    B = 512
    for i in range(0, len(prepped), B):
        chunk = prepped[i:i + B]
        allg = [(bi, L, cap, g) for bi, (r, gl) in enumerate(chunk) for (L, cap, g) in gl]
        if not allg: continue
        with torch.no_grad():
            X = np.stack([a[3] for a in allg]).astype(np.float32)[:, None] / 255.0
            Z = net(torch.tensor((X - 0.8) / 0.3)).numpy()
        per = defaultdict(lambda: defaultdict(float))
        for k, (bi, L, cap, g) in enumerate(allg):
            same = (rl == L) & (rc == cap)
            if not same.any() or len(set(rf[same])) < 2: continue
            sim = np.where(same, RZ @ Z[k], -2.0)
            best = sorted(((s, float(sim[(rf == s) & same].max())) for s in set(rf[same])), key=lambda kv: -kv[1])
            per[bi][best[0][0]] += best[0][1] - (best[1][1] if len(best) > 1 else 0)
        for bi, (r, gl) in enumerate(chunk):
            w = per.get(bi)
            if not w: continue
            tot = sum(w.values()) + 1e-9
            ranked = sorted(((fnt, round(float(sc / tot), 3)) for fnt, sc in w.items()), key=lambda kv: -kv[1])[:3]
            pred, conf = ranked[0]                          # winner; `fonts` keeps the full ranked shortlist
            fout.write(json.dumps(dict(lon=r["lon"], lat=r["lat"], gcx=r["gcx"], gcy=r["gcy"],
                       text=r["text"], font=pred, conf=conf, fonts=ranked, nchar=len(gl),
                       score=r.get("score"), gpoly=r.get("gpoly"))) + "\n")     # keep detection score + full outline
            n += 1; dist[pred] += 1
        if i % 4096 == 0: print(f"  {i}/{len(prepped)} classified={n}", flush=True)
    fout.close()
    print(f"FONTCLASSIFYDONE wrote {n} -> {OUT}; font dist {dict(dist)}", flush=True)

if __name__ == "__main__":
    main()
