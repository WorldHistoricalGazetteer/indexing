"""GB-STAMP (b) v3 — the DISCRIMINATOR: gazetteer-match the reassembled large-fragment runs. v2 showed the
grouping recovers real strings ('SUTTON LE MAR(SH)') but also chains street noise ('SWANSEACARDIFF...'). The
VoB admin gazetteer resolves it: a run whose text fuzzy-matches a hundreds/parish/borough/county name is a
CONFIRMED admin-font label (street/settlement chains won't match an admin name). Scan every done region and
count how many admin labels we recover per face — the decisive yield test for whether route (b) is worth it.

    /vast/ishi/envs/boundary/bin/python detect_admin_v3.py --minh 45
"""
import argparse, os, json, glob, difflib
from collections import defaultdict
HERE = "/vast/ishi/gb1900/probe/font"; SPOT = "/vast/ishi/gb1900/edition/spot"

def norm(s): return "".join(c for c in (s or "") if c.isalnum()).upper()

def frag(box):
    g = box.get("gpoly")
    if not g: return None
    xs = [p[0] for p in g]; ys = [p[1] for p in g]
    h = max(ys) - min(ys); w = max(xs) - min(xs)
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, h, w, norm(box.get("text")))

def group_runs(frags):
    n = len(frags); parent = list(range(n))
    def find(a):
        while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for i in range(n):
        for j in range(i + 1, n):
            cxi, cyi, hi, wi, _ = frags[i]; cxj, cyj, hj, wj, _ = frags[j]
            if not (0.65 <= hi / hj <= 1.54): continue
            mh = (hi + hj) / 2
            if abs(cyi - cyj) > 0.5 * mh: continue
            gap = abs(cxi - cxj) - (wi + wj) / 2
            if not (-0.3 * mh <= gap <= 3.5 * mh): continue
            parent[find(i)] = find(j)
    g = defaultdict(list)
    for i in range(n): g[find(i)].append(i)
    out = []
    for idxs in g.values():
        idxs.sort(key=lambda i: frags[i][0])
        text = "".join(frags[i][4] for i in idxs)
        mh = sorted(frags[i][2] for i in idxs)[len(idxs) // 2]
        if len(text) >= 5: out.append((text, mh, len(idxs)))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minh", type=float, default=45.0); ap.add_argument("--score", type=float, default=0.5)
    ap.add_argument("--ratio", type=float, default=0.82, help="fuzzy match threshold")
    a = ap.parse_args()
    # gazetteer: normalized admin name -> face(s); keep names >=6 chars to avoid trivial matches
    vob = json.load(open(f"{HERE}/labels/vob_admin_names.json"))
    name2face = defaultdict(set)
    for face, names in vob.items():
        for nm in names:
            k = norm(nm)
            if len(k) >= 6: name2face[k].add(face)
    names = list(name2face); by_len = defaultdict(list)
    for k in names: by_len[len(k)].append(k)
    print(f"gazetteer: {len(names)} admin names (>=6 chars) across {len(vob)} faces", flush=True)

    confirmed = []; nreg = 0
    for bf in glob.glob(f"{SPOT}/boxes_gb_*.jsonl"):
        rows = [json.loads(l) for l in open(bf)]
        frags = [f for f in (frag(r) for r in rows if r.get("score", 0) >= a.score) if f and f[2] >= a.minh]
        if not frags: continue
        nreg += 1
        for text, mh, nf in group_runs(frags):
            best, bestr, bestface = None, 0.0, None
            for L in range(max(6, int(len(text) * 0.6)), int(len(text) * 1.15) + 1):
                for cand in by_len.get(L, []):
                    r = difflib.SequenceMatcher(None, text, cand).ratio()
                    if r > bestr: bestr, best, bestface = r, cand, name2face[cand]
            if bestr >= a.ratio:
                confirmed.append((bestr, best, sorted(bestface), text, int(mh), nf, os.path.basename(bf)[6:-6]))
    confirmed.sort(reverse=True)
    print(f"scanned {nreg} regions with large fragments -> {len(confirmed)} gazetteer-confirmed admin labels", flush=True)
    byface = defaultdict(int)
    for r, name, faces, text, mh, nf, tag in confirmed:
        for f in faces: byface[f] += 1
    print("by face:", dict(byface), flush=True)
    print("top matches (ratio, gazetteer-name, faces, run-text, cap-h):", flush=True)
    for r, name, faces, text, mh, nf, tag in confirmed[:40]:
        print(f"  {r:.2f}  {name:20} {faces}  <- '{text}' h={mh} ({nf}frag, {tag})", flush=True)

if __name__ == "__main__":
    main()
