"""Phase C — auto-labelled alphabet from MapReader SPOTTER boxes (CS-seed + co-occurrence fan), validated
against the held-out human labels. This is how scaled spotting pays off: font is assigned to boxes with NO
human labels and NO text-category (font ONLY from CS-visual seed, then fanned by co-occurrence), building a
large reference alphabet; the 184 human-labelled boxes are HELD OUT and used only to measure accuracy.

Boxes: de-rotated (polygon) + FORCE-SPLIT to N=len(MapReader text) glyphs (connected-comp can't segment
touching map letters). Reuses the seed/fan/matcher from build_alphabet.py.

    /vast/ishi/envs/boundary/bin/python build_alphabet_spot.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import glob, json, numpy as np
import concurrent.futures as cf
from collections import Counter, defaultdict
from build_alphabet import (cs_seed_templates, build_buckets, type_label, force_split,
                            STYLES, CAP, HIGH, MIN_GLYPHS, MAX_ROUNDS, SEED_PER_FONT, SEED_MARGIN)
from make_font_testset_v2 import load, stratified, derotate
from discrim_test import sims_row

SPOT = "/vast/ishi/gb1900/edition/spot"; DEC = "/vast/ishi/gb1900/probe/font/font_testset_decisions_1.json"

def box_align(r):
    patch = derotate(r)
    if patch is None: return None
    letters = [c for c in r["text"] if c.isalnum()]
    if len(letters) < 2: return None
    gs = force_split(patch, len(letters))
    if len(gs) != len(letters): return None
    return [(letters[i].upper(), letters[i].isupper(), gs[i]) for i in range(len(letters))]

def main():
    dec = json.load(open(DEC)); font_by_i = {x["i"]: x["font"] for x in dec if x.get("font")}
    samp = stratified(load())
    test = {}
    for i, r in enumerate(samp):
        f = font_by_i.get(i)
        if f in STYLES and r["text"] == dec[i]["text"]: test[(r["gcx"], r["gcy"])] = f
    allb = []
    for fp in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(fp):
            r = json.loads(line)
            if r["score"] >= 0.55 and len([c for c in r["text"] if c.isalnum()]) >= 2: allb.append(r)
    def work(r):
        al = box_align(r); return (r, al) if al else None
    pool = []; tests = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for res in ex.map(work, allb):
            if not res: continue
            r, al = res; key = (r["gcx"], r["gcy"])
            (tests if key in test else pool).append((test.get(key), al))
    pool_gl = [al for _, al in pool]
    print(f"boxes {len(allb)}; aligned pool={len(pool_gl)} + heldout human-test={len(tests)}", flush=True)

    alpha = []; cap_ct = Counter(); assigned = {}
    def harvest(gl, font, gen):
        for L, cap, g in gl:
            k = (L, cap, font)
            if cap_ct[k] < CAP: alpha.append(dict(L=L, cap=cap, style=font, glyph=g, gen=gen)); cap_ct[k] += 1

    csb, csrep = cs_seed_templates(); print("CS seg:", csrep, flush=True)
    csmat = {}
    for (L, cap), fonts in csb.items():
        rows, fl = [], []
        for f, gs in fonts.items():
            for g in gs: rows.append(g.astype(np.float32).ravel()); fl.append(f)
        Mn = np.array(rows, np.float32); Mn /= (np.linalg.norm(Mn, axis=1, keepdims=True) + 1e-6); csmat[(L, cap)] = (np.array(fl), Mn)
    cand = []
    for idx, gl in enumerate(pool_gl):
        for L, cap, g in gl:
            if not cap or (L, cap) not in csmat: continue
            fl, Mn = csmat[(L, cap)]; r = sims_row(g, Mn)
            best = {f: float(r[fl == f].max()) for f in set(fl.tolist())}
            rk = sorted(best.items(), key=lambda kv: -kv[1]); margin = rk[0][1] - (rk[1][1] if len(rk) > 1 else 0)
            if len(rk) >= 2 and margin < SEED_MARGIN: continue
            cand.append(((margin if len(rk) >= 2 else rk[0][1]), idx, rk[0][0]))
    for font in STYLES:
        for c, idx, f in sorted([c for c in cand if c[2] == font], key=lambda c: -c[0])[:SEED_PER_FONT]:
            if idx in assigned: continue
            assigned[idx] = f; harvest(pool_gl[idx], f, 0)
    print("SEED:", {f: sum(1 for v in assigned.values() if v == f) for f in STYLES}, flush=True)
    for rnd in range(1, MAX_ROUNDS + 1):
        B = build_buckets(alpha); new = 0
        for idx, gl in enumerate(pool_gl):
            if idx in assigned or len(gl) < MIN_GLYPHS: continue
            winner, conf, voters = type_label(gl, B)
            if not winner or conf < HIGH: continue
            vc = Counter(s for s, _, _ in voters); vw = defaultdict(float)
            for s, sc, m in voters: vw[s] += sc * m
            second = max((s for s in vw if s != winner), key=lambda s: vw[s], default=None)
            if second and vc[second] >= 2 and vw[second] > 0.55 * vw[winner]: continue
            if vc[winner] < 2: continue
            assigned[idx] = winner; harvest(pool_gl[idx], winner, rnd); new += 1
        print(f"round {rnd}: +{new} assigned={len(assigned)} alphabet={len(alpha)}", flush=True)
        if new == 0: break

    B = build_buckets(alpha); conf = Counter(); tot = Counter()
    for human_f, gl in tests:
        winner, c, voters = type_label(gl, B)
        if not winner: continue
        conf[(human_f, winner)] += 1; tot[human_f] += 1
    N = sum(tot.values()); acc = sum(conf[(s, s)] for s in STYLES) / max(1, N)
    print(f"\n=== VALIDATION: auto-labelled alphabet ({len(alpha)} glyphs, {len(assigned)} boxes) vs HUMAN labels ===")
    print(f"accuracy {acc:.3f} (N={N})   [human-reference LOO baseline = 0.776]")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>8s}" for s in STYLES) + "  recall")
    for s in STYLES:
        print(f"  {s:10s}" + "".join(f"{conf[(s,d)]:>8d}" for d in STYLES) + f"  {conf[(s,s)]/max(1,tot[s]):.2f}")

if __name__ == "__main__":
    main()
