"""44-class SPOT-AND-FAN over the full OS Characteristic Sheet taxonomy (font_taxonomy.json).

SEED  each font from its exemplar — word-split then letter-split (single-letter admin marks handled directly;
      punctuation blobs dropped) — seeding that font's alphabet at its (letter,cap) slots.
FAN   over the real spotter boxes (glyphs re-cropped from the /ix1 tile archive / fetched on miss): same-letter
      match to the current alphabet, assign a box its font only when >=2 known letters CONCUR, harvest its
      glyphs, iterate to convergence — growing every face's real-map alphabet.
SEPARABILITY comes FROM the fan: leave-one-out over the grown alphabet gives a font x font confusion matrix.
      Faces the fan cannot keep apart (mutual confusion high) are the INSEPARABLE ones — flagged with a
      side-by-side exemplar montage for a HUMAN to confirm 'same OS face' (merge) vs 'distinct-but-hard'.

    FCTILES=/vast/ishi/gb1900/fc_tiles /vast/ishi/envs/mapreader/bin/python build_alphabet_multi.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, json, math, numpy as np, cv2
import concurrent.futures as cf
from collections import Counter, defaultdict
from PIL import Image, ImageDraw
from build_alphabet import force_split, build_buckets, type_label, match_glyph, CAP, HIGH, GLYPH_MIN, MIN_GLYPHS, MAX_ROUNDS
from make_font_testset_v2 import load, derotate
from discrim_test import H, W, norm_glyph, sims_row

HERE = "/vast/ishi/gb1900/probe/font"; TAX = f"{HERE}/font_taxonomy.json"
OUT = "/vast/ishi/gb1900/edition/discover"; os.makedirs(OUT, exist_ok=True)
SEED_PER_FONT = 50; SEED_MARGIN = 0.02; IDENT = 0.90

# ALLCAPS faces (admin hierarchy etc.) can ONLY be all-caps labels; title-case faces can only be non-allcaps.
# This alone removes the mixed-case letter-attraction noise (a title-case 'Clock' is never a CAPS admin face).
_TX = {x["key"]: x for x in json.load(open(TAX))}
CAPS_OF = {k: bool(v.get("caps")) for k, v in _TX.items()}
def is_allcaps(text):
    a = [c for c in text if c.isalpha()]
    return bool(a) and all(c.isupper() for c in a)
# County names are a finite, known list -> seed the county face by LOOKING THEM UP (all-caps labels whose text
# is a county), giving genuine county-face examples instead of relying on a single ornate mark-letter.
COUNTIES = {c.replace(" ", "") for c in (
    "Bedford Berks Bucks Cambridge Chester Cornwall Cumberland Derby Devon Dorset Durham Essex Gloucester "
    "Hereford Hertford Huntingdon Kent Lancaster Leicester Lincoln Middlesex Monmouth Norfolk Northampton "
    "Northumberland Nottingham Oxford Rutland Salop Shropshire Somerset Stafford Suffolk Surrey Sussex Warwick "
    "Westmorland Wilts Worcester York Yorkshire Anglesey Brecon Cardigan Carmarthen Carnarvon Caernarvon Denbigh "
    "Flint Glamorgan Merioneth Montgomery Pembroke Radnor Aberdeen Argyll Ayr Banff Berwick Bute Caithness "
    "Clackmannan Dumfries Dunbarton Edinburgh Elgin Fife Forfar Haddington Inverness Kincardine Kinross "
    "Kirkcudbright Lanark Linlithgow Nairn Orkney Peebles Perth Renfrew Ross Roxburgh Selkirk Stirling "
    "Sutherland Wigtown Zetland Shetland").split()}

def one_glyph(gray):
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return norm_glyph(ink > 0)

PHRASE_FILE = f"{HERE}/labels/phrase_seeds.json"
PHRASE = {e["key"]: e for e in json.load(open(PHRASE_FILE))} if os.path.exists(PHRASE_FILE) else {}

def seed_from_cuts(gray, text, angle, cuts):
    """HITL seeds: de-slant by `angle`, cut at the human `cuts` (one segment per CHARACTER of `text`,
    including spaces/punctuation), keep the alnum segments as letter glyphs."""
    H0, W0 = gray.shape; t = math.tan(math.radians(angle or 0))
    if t:
        off = max(0.0, -t * H0); newW = int(math.ceil(W0 + abs(t) * H0))
        gray = cv2.warpAffine(gray, np.float32([[1, t, off], [0, 1, 0]]), (newW, H0), borderValue=255)
        cutpx = [c * W0 + t * (H0 / 2) + off for c in cuts]; Wt = newW
    else:
        cutpx = [c * W0 for c in cuts]; Wt = W0
    bounds = [0] + [int(round(x)) for x in cutpx] + [Wt]; chars = list(text); out = []
    if len(bounds) - 1 != len(chars): return out            # cut count must match text length
    for i, ch in enumerate(chars):
        if not ch.isalnum(): continue
        g = one_glyph(gray[:, max(0, bounds[i]):bounds[i + 1]])
        if g is not None: out.append((ch.upper(), ch.isupper(), g))
    return out

def word_split(gray):
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1] > 0
    prof = ink.sum(0); cols = np.where(prof > prof.max() * 0.02)[0]
    if len(cols) == 0: return []
    GAPW = max(6, gray.shape[0] // 3); groups = []; start = prev = cols[0]
    for c in cols[1:]:
        if c - prev >= GAPW: groups.append((start, prev)); start = c
        prev = c
    groups.append((start, prev))
    minw = gray.shape[0] * 0.18                      # drop narrow blobs: '.', boundary ticks
    return [gray[:, a:b + 1] for a, b in groups if (b - a) >= minw]

def seed_glyphs(gray, text):
    words_txt = [w for w in text.split() if any(c.isalnum() for c in w)]
    wimgs = word_split(gray); out = []
    def emit(img, wt):
        letters = [c for c in wt if c.isalnum()]
        if len(letters) == 1:
            g = one_glyph(img)
            return [(letters[0].upper(), letters[0].isupper(), g)] if g is not None else []
        gs = force_split(img, len(letters))
        if len(gs) != len(letters): return []
        return [(letters[i].upper(), letters[i].isupper(), gs[i]) for i in range(len(letters))]
    if len(wimgs) == len(words_txt):
        for wi, wt in zip(wimgs, words_txt): out += emit(wi, wt)
    else:                                            # counts disagree -> whole crop by total letters
        out += emit(gray, "".join(words_txt))
    return out

def cs_seed_multi():
    tax = [x for x in json.load(open(TAX)) if x.get("exemplar") and x.get("seed_text")]
    bank = defaultdict(lambda: defaultdict(list)); rep = []
    for x in tax:
        p = f"{HERE}/{x['exemplar']}"
        if not os.path.exists(p): continue
        gray = np.asarray(Image.open(p).convert("L"), np.uint8)
        if x["key"] in PHRASE:                                # HITL-curated cuts (word/phrase specimens)
            e = PHRASE[x["key"]]; gl = seed_from_cuts(gray, e["text"], e.get("angle", 0), e["cuts"])
        else:                                                # single-letter admin marks
            gl = seed_glyphs(gray, x["seed_text"])
        for L, cap, g in gl: bank[(L, cap)][x["key"]].append(g)
        rep.append(f"{x['key']:<22} {len(gl)}/{x['seed_letters']}{' [HITL]' if x['key'] in PHRASE else ''}")
    return bank, rep, {x["key"]: x for x in tax}

# ---- box glyphs (real map) ----
def box_align(r):
    patch = derotate(r)
    if patch is None: return None
    letters = [c for c in r["text"] if c.isalnum()]
    if len(letters) < 2: return None
    gs = force_split(patch, len(letters))
    if len(gs) != len(letters): return None
    return (r["text"], [(letters[i].upper(), letters[i].isupper(), gs[i]) for i in range(len(letters))], patch)

def main():
    bank, rep, tk = cs_seed_multi()
    fonts = sorted({f for d in bank.values() for f in d})
    print(f"SEED segmentation ({len(fonts)} fonts seeded):\n  " + "\n  ".join(rep), flush=True)

    import glob as _glob
    SPOT = "/vast/ishi/gb1900/edition/spot"; boxes = []
    for f in _glob.glob(f"{SPOT}/boxes_gb_*.jsonl"):          # full-coverage grind regions only
        for line in open(f):
            r = json.loads(line)
            if r.get("score", 0) >= 0.55 and len([c for c in r["text"] if c.isalnum()]) >= 3 and r.get("gpoly"):
                boxes.append(r)
    print(f"spotter boxes: {len(boxes)}", flush=True)
    data = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(box_align, boxes):
            if r: data.append(r)
    print(f"aligned box-labels: {len(data)}", flush=True)

    alpha = []; cap_ct = Counter(); assigned = {}; assign_meta = {}; seed_info = {}
    def harvest(idx, font, gen):
        assign_meta[idx] = (font, gen)
        for L, cap, g in data[idx][1]:
            if cap_ct[(L, cap, font)] < CAP:
                alpha.append(dict(L=L, cap=cap, style=font, glyph=g, gen=gen)); cap_ct[(L, cap, font)] += 1

    # SEED: match real cap glyphs to the CS seed bank
    csmat = {}
    for (L, cap), fd in bank.items():
        rows, fl = [], []
        for f, gs in fd.items():
            for g in gs: rows.append(g.astype(np.float32).ravel()); fl.append(f)
        M = np.array(rows, np.float32); M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)
        csmat[(L, cap)] = (np.array(fl), M)
    # LOOKUP SEED: seed admin faces from name gazetteers — genuine examples, not a single mark-letter. Only the
    # CLEAN, non-overlapping gazetteers (hardcoded counties + Wikidata civil parishes + cities); a name matching
    # more than one face is skipped as ambiguous. Only ALL-CAPS labels (admin names are set in caps).
    name2face = {}
    def addgaz(names, face):
        for nm in names:
            k = "".join(c for c in nm if c.isalpha()).upper()
            if len(k) >= 3: name2face.setdefault(k, set()).add(face)
    addgaz(COUNTIES, "county_names")
    gaz = json.load(open(f"{HERE}/labels/admin_names.json")) if os.path.exists(f"{HERE}/labels/admin_names.json") else {}
    addgaz(gaz.get("civil_parishes", []), "civil_parishes")
    addgaz(gaz.get("cities_nomp", []), "cities_nomp")
    nlook = Counter()
    for i, (text, gl, _p) in enumerate(data):
        if not is_allcaps(text): continue
        f = name2face.get("".join(c for c in text if c.isalpha()).upper())
        if f and len(f) == 1:
            face = next(iter(f)); assigned[i] = face; seed_info[i] = ("lookup", 1.0); harvest(i, face, 0); nlook[face] += 1
    print(f"LOOKUP SEED: {sum(nlook.values())} labels -> {dict(nlook)}", flush=True)

    # VISUAL SEED: match real cap glyphs to the CS bank, CASE-CONSISTENT (a CAPS face only matches an all-caps label)
    cand = []
    for i, (text, gl, _p) in enumerate(data):
        if i in assigned: continue
        ac = is_allcaps(text)
        for L, cap, g in gl:
            if (L, cap) not in csmat: continue
            fl, M = csmat[(L, cap)]; r = sims_row(g, M)
            best = {f: float(r[fl == f].max()) for f in set(fl.tolist()) if CAPS_OF.get(f, False) == ac}
            if not best: continue
            rk = sorted(best.items(), key=lambda kv: -kv[1])
            margin = rk[0][1] - (rk[1][1] if len(rk) > 1 else 0.0)
            if len(rk) >= 2 and margin < SEED_MARGIN: continue
            cand.append(((margin if len(rk) >= 2 else rk[0][1]), i, rk[0][0], L, round(float(rk[0][1]), 3)))
    for font in fonts:
        for conf, i, f, L, sc in sorted([c for c in cand if c[2] == font], key=lambda c: -c[0])[:SEED_PER_FONT]:
            if i in assigned: continue
            assigned[i] = f; seed_info[i] = (L, sc); harvest(i, f, 0)
    print(f"SEED: {len(assigned)} labels -> {len(alpha)} glyphs", flush=True)

    # FAN
    for rnd in range(1, MAX_ROUNDS + 1):
        B = build_buckets(alpha); new = 0
        for i, (text, gl, _p) in enumerate(data):
            if i in assigned or len(gl) < MIN_GLYPHS: continue
            ac = is_allcaps(text); voters = []
            for L, cap, g in gl:
                s, sc, m = match_glyph(g, L, cap, B)
                if s and sc >= GLYPH_MIN and m > 0 and CAPS_OF.get(s, False) == ac: voters.append((s, sc, m))
            if not voters: continue
            w = defaultdict(float)
            for s, sc, m in voters: w[s] += sc * m
            winner = max(w, key=w.get)
            conf = (w[winner] / (sum(w.values()) + 1e-9)) * float(np.mean([sc for s, sc, m in voters if s == winner]))
            if conf < HIGH or sum(1 for s, _, _ in voters if s == winner) < 2: continue
            assigned[i] = winner; harvest(i, winner, rnd); new += 1
        print(f"round {rnd}: +{new} labels; alphabet={len(alpha)}", flush=True)
        if new == 0: break

    if os.environ.get("DUMP_ASSIGN"):                    # per-label assignments + crops for the inspection UI
        import base64, io
        def b64p(patch):
            im = Image.fromarray(patch).convert("L"); h = 90
            im = im.resize((max(1, int(im.width * h / max(1, im.height))), h))
            bio = io.BytesIO(); im.save(bio, "PNG"); return base64.b64encode(bio.getvalue()).decode()
        with open(f"{OUT}/assignments.jsonl", "w") as fo:
            for i, (font, gen) in assign_meta.items():
                si = seed_info.get(i)
                fo.write(json.dumps({"font": font, "text": data[i][0], "gen": int(gen),
                                     "seed_letter": si[0] if si else None, "seed_score": si[1] if si else None,
                                     "crop": b64p(data[i][2])}) + "\n")
        print(f"wrote assignments.jsonl ({len(assign_meta)} assigned labels)", flush=True)

    # SEPARABILITY from the fanned alphabet: leave-one-out same-letter confusion between fonts
    conf = np.zeros((len(fonts), len(fonts))); tot = np.zeros(len(fonts))
    fi = {f: i for i, f in enumerate(fonts)}
    by = defaultdict(list)
    for t in alpha: by[(t["L"], t["cap"])].append(t)
    for key, items in by.items():
        styles = np.array([t["style"] for t in items])
        if len(set(styles)) < 2: continue
        M = np.array([t["glyph"].astype(np.float32).ravel() for t in items], np.float32)
        M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)
        for i in range(len(items)):
            r = np.where(np.arange(len(items)) != i, sims_row(items[i]["glyph"], M), -2.0)
            pred = styles[int(np.argmax(r))]
            conf[fi[items[i]["style"]], fi[pred]] += 1; tot[fi[items[i]["style"]]] += 1
    # SSL-embedding separability (pure visual, encoder-based) — compared head-to-head with raw-pixel
    ssl_acc = {}
    if os.environ.get("USE_SSL", "1") == "1":
        try:
            import torch
            from ssl_pretrain import Enc
            net = Enc(); net.load_state_dict(torch.load(f"{OUT.rsplit('/',1)[0]}/spot/encoder_full.pt", map_location="cpu")); net.eval()
            def emb(gs):
                X = np.stack(gs).astype(np.float32)[:, None]; X = (X * 255 if X.max() <= 1.0 else X) / 255.0
                with torch.no_grad(): return net(torch.tensor((X - 0.8) / 0.3)).numpy()
            cS = np.zeros((len(fonts), len(fonts))); tS = np.zeros(len(fonts))
            for key, items in by.items():
                styles = [t["style"] for t in items]
                if len(set(styles)) < 2: continue
                Z = emb([t["glyph"] for t in items]); Z /= (np.linalg.norm(Z, axis=1, keepdims=True) + 1e-6)
                for i in range(len(items)):
                    r = np.where(np.arange(len(items)) != i, Z @ Z[i], -2.0)
                    cS[fi[styles[i]], fi[styles[int(np.argmax(r))]]] += 1; tS[fi[styles[i]]] += 1
            ssl_acc = {fonts[i]: round(float(cS[i, i] / tS[i]), 2) for i in range(len(fonts)) if tS[i] >= 5}
            print(f"[SSL] embedding self-accuracy: {ssl_acc}", flush=True)
        except Exception as e:
            print(f"[SSL] skipped: {e}", flush=True)

    # cluster the confusion into MERGE-GROUPS: union-find over mutual mis-ID >= THETA (confusion SPREADS across
    # similar faces, so a flat pairwise bar misses them — clustering groups the faces the fan can't separate).
    THETA = 0.20; par = list(range(len(fonts)))
    def find(a):
        while par[a] != a: par[a] = par[par[a]]; a = par[a]
        return a
    edges = []
    for a in range(len(fonts)):
        for b in range(a + 1, len(fonts)):
            na, nb = tot[a], tot[b]
            if na < 5 or nb < 5: continue
            cross = (conf[a, b] / na + conf[b, a] / nb) / 2
            if cross >= THETA: edges.append((round(float(cross), 2), fonts[a], fonts[b])); par[find(a)] = find(b)
    grp = defaultdict(list)
    for i, f in enumerate(fonts): grp[find(i)].append(f)
    merge = sorted((sorted(g) for g in grp.values() if len(g) > 1), key=lambda g: -len(g))
    edges.sort(reverse=True)
    acc = {fonts[i]: round(float(conf[i, i] / tot[i]), 2) for i in range(len(fonts)) if tot[i] >= 5}
    json.dump({"fonts": fonts, "confusion": conf.tolist(), "support": tot.tolist(), "self_acc": acc,
               "ssl_self_acc": ssl_acc, "merge_groups": merge, "edges": edges, "theta": THETA},
              open(f"{OUT}/separability.json", "w"))
    print(f"\nper-font LOO self-accuracy (support>=5): {acc}", flush=True)
    print(f"MERGE-GROUPS (mutual mis-ID >= {THETA}) — HUMAN VERIFY same OS face:", flush=True)
    for g in merge: print("  { " + " , ".join(g) + " }", flush=True)
    print(f"top confusion edges: {[(c, a, b) for c, a, b in edges[:15]]}", flush=True)
    if merge:
        maxn = max(len(g) for g in merge); rowh = 150
        canvas = Image.new("RGB", (140 * maxn + 10, rowh * len(merge)), "white"); d = ImageDraw.Draw(canvas)
        for i, g in enumerate(merge):
            for j, f in enumerate(g):
                try:
                    im = Image.open(f"{HERE}/{tk[f]['exemplar']}").convert("L"); im.thumbnail((120, 120))
                    canvas.paste(im.convert("RGB"), (10 + j * 140, i * rowh + 10))
                except Exception: pass
            d.text((10, i * rowh + rowh - 18), " | ".join(g)[:72], fill=(150, 40, 30))
        canvas.save(f"{OUT}/merge_groups.png"); print(f"merge_groups.png -> {OUT}/merge_groups.png", flush=True)
    np.savez_compressed(f"{OUT}/alphabet_multi.npz",
                        glyphs=np.array([t["glyph"] for t in alpha], np.uint8),
                        letter=np.array([t["L"] for t in alpha]), cap=np.array([t["cap"] for t in alpha]),
                        style=np.array([t["style"] for t in alpha]), gen=np.array([t["gen"] for t in alpha]))
    print(f"saved alphabet_multi.npz: {len(alpha)} glyphs, {len(assigned)}/{len(data)} labels assigned", flush=True)

if __name__ == "__main__":
    main()
