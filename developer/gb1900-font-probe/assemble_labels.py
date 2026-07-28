"""Join MapReader WORDS into whole LABELS, and test the join against GB1900.

The spotter emits words; GB1900 pins labels. Until the words are assembled, almost every comparison between
the two is structurally guaranteed to fail — a box reading "MOOR" can never equal a transcription reading
"Middleton Moor", so a low string-agreement figure measures the missing assembly step and not the spotter.
This is that step, and GB1900 is the test set for it: a correctly assembled label should reproduce the
volunteer's transcription exactly, which is a far stricter test than any box-overlap score.

THE JOIN. A word carries its own typography, so it can say where its continuation must be. From the word's
minimum-area rectangle come a direction, a cap height, and a character pitch (long side / characters). A
second word continues the first when it lies AHEAD along that direction, within a few pitches, off the
baseline by only a fraction of the cap height, and set at the same size and angle. Projecting forward along
the word's OWN direction and pitch is what makes this safe on a map: labels run at every angle, curve along
rivers and interleave with each other, so a fixed horizontal window would either miss the rotated ones or
sweep in a neighbour's.

MULTI-LINE. A label set on two or three lines is one label. Its lines share a font, a size and a direction,
sit about one line-height apart, and — the sharp part of the test — are CENTRED on a common perpendicular
axis. Requiring a shared orthogonal centre rejects the far commoner arrangement of two unrelated labels that
merely happen to lie one above the other.

FONT is optional and reported as an ablation, never assumed. If a descriptor is supplied, two words may only
join when their faces agree — which is also the first real test of whether the face instrument earns its
place in the pipeline rather than only in the paper.

    python assemble_labels.py --boxes '/vast/ishi/gb1900/edition/spot/boxes_gb_43*.jsonl' --validate
"""
import argparse, glob, json, math, os, re, sys
from collections import defaultdict
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

N17 = 2 ** 17


def norm(s):
    s = (s or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", s)


def tokens(s):
    return [t for t in (norm(w) for w in re.split(r"[\s\-/]+", s or "")) if t]


def is_alpha(s):
    return bool(re.search(r"[a-z]", (s or "").lower()))


def centreline(poly, u, k=11, trim=1):
    """Recover the word's centre-line from its outline.

    MapTextPipeline emits a dense contour (~50 points) around each word: an upper and a lower boundary
    joined by a cap at each end. Splitting it at the extremes along the long axis and averaging the two
    sides gives the centre-line — the same thing the model's own pixel_line carries, recovered for
    detections spotted before that field was kept.

    Two details decide whether this works at all.

    Sides are paired by POSITION ALONG THE READING DIRECTION, not by fractional arc length. The two sides
    are rarely the same length, so arc-length pairing matches points that are not opposite each other and
    bends a straight word.

    The ends are TRIMMED. At the extremes both sides converge on the same cap tip, so the average there is
    pulled to the tip rather than the centre — which on a straight word manufactured a curve of a third of a
    cap height and threw the end tangent out by tens of degrees. The interior is sound; the caps are not.
    """
    P = np.asarray(poly, np.float64)
    if len(P) < 8:
        return None
    uu = np.asarray(u, np.float64)
    t = P @ uu
    i0, i1 = int(np.argmin(t)), int(np.argmax(t))
    if i0 == i1:
        return None
    a, b = (i0, i1) if i0 < i1 else (i1, i0)
    side1, side2 = P[a:b + 1], np.vstack([P[b:], P[:a + 1]])
    if len(side1) < 3 or len(side2) < 3:
        return None
    lo, hi = float(max(t[a], t[b]) * 0 + min(t[i0], t[i1])), float(max(t[i0], t[i1]))
    if hi - lo < 1e-6:
        return None
    grid = np.linspace(lo, hi, k)

    def sample(C):
        tc = C @ uu
        o = np.argsort(tc)
        tc, C = tc[o], C[o]
        keep = np.concatenate([[True], np.diff(tc) > 1e-9])   # np.interp needs strictly increasing x
        tc, C = tc[keep], C[keep]
        if len(tc) < 2:
            return None
        return np.stack([np.interp(grid, tc, C[:, j]) for j in (0, 1)], 1)

    r1, r2 = sample(side1), sample(side2)
    if r1 is None or r2 is None:
        return None
    cl = (r1 + r2) / 2.0
    return cl[trim:len(cl) - trim] if trim and len(cl) > 2 * trim + 2 else cl


def word_frame(poly, text, line=None):
    """Direction, cap height and character pitch, read off the word's own geometry.

    Direction comes from the CENTRE-LINE, not from the minimum-area rectangle. On a curved label the
    rectangle's long axis is a chord: it points across the curve rather than along it, and worst at the ends
    where a continuation has to be looked for. The line gives a tangent at each end instead, so a
    continuation is sought along the word's actual heading. The model's own pixel_line is used when present;
    otherwise the line is recovered from the outline, which carries enough points to do it.
    """
    import cv2
    p = np.asarray(poly, np.float32)
    (cx, cy), (w, h), ang = cv2.minAreaRect(p)
    if w < h:
        w, h, ang = h, w, ang + 90.0
    th = math.radians(ang)
    u = (math.cos(th), math.sin(th))
    n = max(1, len(re.sub(r"\s+", "", text or "")))
    cl = np.asarray(line, np.float64) if line is not None and len(line) >= 2 else centreline(poly, u)
    u_start = u_end = u
    curv = 0.0
    if cl is not None and len(cl) >= 3:
        if (cl[0] @ np.asarray(u)) > (cl[-1] @ np.asarray(u)):
            cl = cl[::-1]
        def tangent(seg, outward):
            # Least squares over a third of the line, not the final segment: one segment of a resampled
            # contour is mostly noise, and the tangent is the thing the whole join hangs on.
            c = seg - seg.mean(0)
            v = np.linalg.svd(c, full_matrices=False)[2][0]
            if (v @ (seg[-1] - seg[0])) < 0:
                v = -v
            v = v * outward
            return (float(v[0]), float(v[1]))
        m = max(2, len(cl) // 3)
        u_end = tangent(cl[-m:], 1.0)            # pointing ON out of the word
        u_start = tangent(cl[:m], -1.0)          # pointing BACK out of the word
        # How far the centre-line departs from straight, in cap heights: 0 for a plain word, and the
        # measure of how wrong a single global axis would have been.
        d = cl[-1] - cl[0]
        L = float(np.linalg.norm(d))
        if L > 1e-6:
            nrm = np.array([-d[1], d[0]]) / L
            curv = float(np.abs((cl - cl[0]) @ nrm).max() / max(1.0, h))
    return dict(cx=float(cx), cy=float(cy), long=float(w), h=float(h),
                ang=float(ang), u=u, u_start=u_start, u_end=u_end, curv=curv,
                pitch=float(w) / n)


def facing(A, dx, dy):
    """A's tangent at the end nearest the candidate."""
    ue, us = A.get("u_end", A["u"]), A.get("u_start", A["u"])
    return ue if (dx * ue[0] + dy * ue[1]) >= (dx * -us[0] + dy * -us[1]) else (-us[0], -us[1])


def joins(A, B, max_gap_pitch, lat_tol, h_tol, ang_tol):
    """Does B continue A? Returns the signed along-direction gap, or None."""
    hm = max(A["h"], B["h"])
    if abs(A["h"] - B["h"]) / max(1e-6, hm) > h_tol:
        return None
    da = abs((A["ang"] - B["ang"] + 90) % 180 - 90)
    if da > ang_tol:
        return None
    dx, dy = B["cx"] - A["cx"], B["cy"] - A["cy"]
    # The tangent at whichever end of A faces B. A continuation follows the word's heading where it ends,
    # which on a curved label is not the direction of the label as a whole.
    ux, uy = facing(A, dx, dy)
    along = dx * ux + dy * uy
    lat = abs(-dx * uy + dy * ux)
    if lat > lat_tol * hm:
        return None
    gap = abs(along) - (A["long"] + B["long"]) / 2.0
    if gap > max_gap_pitch * max(A["pitch"], B["pitch"]):
        return None
    if gap < -0.6 * min(A["long"], B["long"]):      # heavily overlapping boxes are one word twice
        return None
    return along


def assemble(words, max_gap_pitch=2.5, lat_tol=0.6, h_tol=0.32, ang_tol=12.0,
             line_gap=2.4, centre_tol=0.4, desc=None, face_min=0.0,
             max_lines=3, join_numerals=False, model=None, model_thr=0.5):
    """Words -> lines -> multi-line labels."""
    n = len(words)
    # A numeric-only word is map furniture — a parcel number, a spot height, a bench mark — set beside the
    # lettering rather than as part of it. Joining one to a label produced "224 Burial Ground" and
    # "BELL STREET 97". The map's own convention says they are separate things, and GB1900 transcribes none
    # of them, so a numeral never continues a label. Switchable, so the rule is an ablation not an article
    # of faith.
    numeric = [not is_alpha(w["text"]) for w in words]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    F = [w["f"] for w in words]
    P = np.array([[f["cx"], f["cy"]] for f in F])
    order = np.argsort(P[:, 0])
    # The scan stops on x-separation, so the bound must not depend on the candidate: half of the LONGEST
    # word is the only safe allowance, since a long word's centre can sit far away while its near end is
    # still in reach. Using the candidate's own length would make the cutoff non-monotone and silently
    # skip exactly the long words a label most needs.
    maxhalf = max(f["long"] for f in F) / 2.0
    pend = []
    # With a model in play the geometric test becomes a RECALL ENVELOPE, not the decision: candidates are
    # gathered loosely and the model rules on them. Keeping the tight rules here as well would cap the
    # model at the rules' own recall, which is the thing it exists to improve on.
    if model is not None:
        max_gap_pitch, lat_tol, h_tol, ang_tol = 6.0, 1.4, 0.55, 28.0
    for oi in range(n):
        i = int(order[oi])
        reach = max_gap_pitch * F[i]["pitch"] + F[i]["long"] / 2.0 + maxhalf
        for oj in range(oi + 1, n):
            j = int(order[oj])
            if P[j, 0] - P[i, 0] > reach:
                break
            if not join_numerals and (numeric[i] or numeric[j]):
                continue
            if model is not None:
                pend.append((i, j))          # scored in one batch below; per-pair calls dominate otherwise
                continue
            if joins(F[i], F[j], max_gap_pitch, lat_tol, h_tol, ang_tol) is None:
                continue
            if desc is not None and face_min > 0:
                a, b = desc.get(words[i]["id"]), desc.get(words[j]["id"])
                if a is not None and b is not None:
                    if float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9) < face_min:
                        continue
            union(i, j)

    if model is not None and pend:
        # A LINE OF TEXT IS A SEQUENCE, NOT A CLIQUE.
        #
        # Unioning every pair over threshold is what destroyed the first attempt: the classifier is right on
        # 93% of pairs, but union-find takes the TRANSITIVE CLOSURE, so roughly one bad link in fourteen is
        # enough to weld neighbouring labels together and then weld the welds. It produced 31 components
        # swallowing 9,819 of 16,243 words, one of them 1,548 words long, and exact reproduction collapsed
        # from 0.381 to 0.083. The hand-set rules escaped this only because their low recall left the graph
        # too sparse to percolate — which means their apparent robustness was an accident of missing half
        # the true joins, not a virtue.
        #
        # So the graph is constrained to the shape text actually has. Reading along its own direction, a word
        # has at most ONE predecessor and at most ONE successor, and a link is made only where both words
        # agree that the other is their best neighbour in that direction. A false pair now has to beat every
        # true rival on BOTH sides to do any damage, and it can never fan out into a blob.
        from join_train import pair_features
        M, thr = model["model"], model_thr
        Xp = np.array([pair_features(F[i], F[j], words[i]["text"], words[j]["text"],
                                     words[i].get("font"), words[j].get("font"))
                       for i, j in pend], np.float32)
        want = len(model.get("features") or [])
        if want and Xp.shape[1] != want:
            raise SystemExit(f"this model wants {want} features but the vectors carry {Xp.shape[1]} — "
                             f"pass --fonts if it was trained with the face features, or retrain")
        pr = M.predict_proba(Xp)[:, 1]
        # Links are taken GREEDILY, best score first, each accepted only if both words still have a free
        # slot on the side it uses. Mutual-best-neighbour was the first attempt and it silently drops true
        # links: where two words both rank the same third word best, the loser keeps nothing even when its
        # own second choice was correct and unclaimed. Greedy assignment lets the strongest evidence settle
        # first and the rest fall in behind it, which is the same reason it beats mutual-best in matching
        # problems generally.
        side_used = [[False, False] for _ in range(n)]        # [behind_taken, ahead_taken]

        def side_of(u, v):
            dx, dy = F[v]["cx"] - F[u]["cx"], F[v]["cy"] - F[u]["cy"]
            ux, uy = F[u]["u"]
            return 1 if (dx * ux + dy * uy) >= 0 else 0

        cand = [(float(q), i, j) for (i, j), q in zip(pend, pr) if q >= thr]
        cand.sort(key=lambda t: -t[0])
        for q, i, j in cand:
            si, sj = side_of(i, j), side_of(j, i)
            if side_used[i][si] or side_used[j][sj]:
                continue
            if find(i) == find(j):                            # already same label: would close a cycle
                continue
            side_used[i][si] = side_used[j][sj] = True
            union(i, j)

    lines = defaultdict(list)
    for i in range(n):
        lines[find(i)].append(i)

    def line_of(members):
        f = [F[i] for i in members]
        hh = float(np.median([x["h"] for x in f]))
        ang = float(np.median([x["ang"] for x in f]))
        th = math.radians(ang)
        u = (math.cos(th), math.sin(th))
        c = np.mean([[x["cx"], x["cy"]] for x in f], axis=0)
        proj = [(F[i]["cx"] - c[0]) * u[0] + (F[i]["cy"] - c[1]) * u[1] for i in members]
        o = np.argsort(proj)
        mem = [members[k] for k in o]
        if len(mem) > 1 and words[mem[0]]["f"]["cx"] > words[mem[-1]]["f"]["cx"]:
            mem = mem[::-1]                                  # left-to-right is the reading order here
        return dict(members=mem, h=hh, ang=ang, u=u, c=c,
                    length=float(max(proj) - min(proj)) + f[0]["long"],
                    text=" ".join(words[i]["text"] for i in mem))

    L = [line_of(m) for m in lines.values()]

    # Multi-line: same size and direction, about a line apart, and CENTRED on a shared perpendicular axis.
    used, out = set(), []
    for i, a in enumerate(L):
        if i in used:
            continue
        stack = [i]
        used.add(i)
        changed = True
        while changed:
            changed = False
            for j, b in enumerate(L):
                if j in used or len(stack) >= max_lines:
                    continue
                for k in stack:
                    g = L[k]
                    hm = max(g["h"], b["h"])
                    if abs(g["h"] - b["h"]) / hm > h_tol:
                        continue
                    if abs((g["ang"] - b["ang"] + 90) % 180 - 90) > ang_tol:
                        continue
                    dx, dy = b["c"][0] - g["c"][0], b["c"][1] - g["c"][1]
                    ux, uy = g["u"]
                    along = abs(dx * ux + dy * uy)
                    across = abs(-dx * uy + dy * ux)
                    if not (0.6 * hm <= across <= line_gap * hm):
                        continue
                    # the shared orthogonal centre — this is what separates a two-line label from two
                    # unrelated labels that merely sit one above the other
                    if along > centre_tol * max(g["length"], b["length"], hm):
                        continue
                    stack.append(j)
                    used.add(j)
                    changed = True
                    break
        st = sorted(stack, key=lambda k: (-L[k]["c"][0] * L[stack[0]]["u"][1]
                                          + L[k]["c"][1] * L[stack[0]]["u"][0]))
        mem = [i2 for k in st for i2 in L[k]["members"]]
        out.append(dict(members=mem, lines=len(st),
                        h=float(np.median([L[k]["h"] for k in st])),
                        ang=float(np.median([L[k]["ang"] for k in st])),
                        text=" ".join(L[k]["text"] for k in st),
                        cx=float(np.mean([words[m]["f"]["cx"] for m in mem])),
                        cy=float(np.mean([words[m]["f"]["cy"] for m in mem]))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boxes", nargs="+", required=True, help="boxes_*.jsonl (globs allowed)")
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--validate", action="store_true", help="score the assembly against GB1900")
    ap.add_argument("--radius", type=float, default=64.0, help="px slack matching a pin to a label")
    ap.add_argument("--max-gap-pitch", type=float, default=2.5)
    ap.add_argument("--lat-tol", type=float, default=0.6)
    ap.add_argument("--h-tol", type=float, default=0.32)
    ap.add_argument("--ang-tol", type=float, default=12.0)
    ap.add_argument("--line-gap", type=float, default=2.4)
    ap.add_argument("--centre-tol", type=float, default=0.4)
    ap.add_argument("--max-lines", type=int, default=3,
                    help="a map label runs to two or three lines; a seven-line stack is a runaway merge")
    ap.add_argument("--model", default=None, help="join_rf.joblib from join_train.py")
    ap.add_argument("--fonts", default=None, help="font_v2 dir; must be supplied if the model was trained "
                                                  "with face features, or the vectors will not line up")
    ap.add_argument("--blocks-from", default=None,
                    help="join_rf.test_blocks.json — score ONLY regions in blocks the model never saw. "
                         "Kept separate from --model so the rule baseline can be scored on the very same "
                         "regions without the model being loaded and silently doing the joining")
    ap.add_argument("--model-thr", type=float, default=None,
                    help="override the threshold chosen on held-out pairs")
    ap.add_argument("--join-numerals", action="store_true",
                    help="allow parcel numbers and spot heights to join labels (ablation; off by default)")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--out", default="assembled_labels.jsonl")
    a = ap.parse_args()

    FONTS = {}
    if a.fonts:
        from join_train import load_fonts
        FONTS = load_fonts(a.fonts)
        print(f"{len(FONTS)} classified boxes carry a face")
    files = [f for pat in a.boxes for f in sorted(glob.glob(pat))]
    if a.blocks_from:
        from join_train import block_of
        tb = set(json.load(open(a.blocks_from)))
        keep = [f for f in files if block_of(os.path.basename(f)[6:-6]) in tb]
        print(f"held-out only: {len(keep)} of {len(files)} region files "
              f"fall in the {len(tb)} blocks the model never saw")
        files = keep
    words, seen = [], set()
    for f in files:
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            p = r.get("gpoly")
            txt = str(r.get("text", "")).strip()
            if not p or not txt or float(r.get("score", 1.0)) < a.min_score:
                continue
            cx = r.get("gcx") or sum(q[0] for q in p) / len(p)
            cy = r.get("gcy") or sum(q[1] for q in p) / len(p)
            k = (round(cx / 4), round(cy / 4), norm(txt))     # regions overlap; one word is one word
            if k in seen:
                continue
            seen.add(k)
            words.append(dict(id=len(words), text=txt, poly=p,
                              f=word_frame(p, txt, r.get("gline")),
                              font=FONTS.get((round(cx / 4), round(cy / 4), norm(txt)))))
    print(f"{len(files)} files -> {len(words)} distinct words")
    if not words:
        raise SystemExit("nothing to assemble")

    MODEL = None
    if a.model:
        import joblib
        MODEL = joblib.load(a.model)
        print(f"join model {a.model}: held-out pair AUC {MODEL.get('auc'):.4f}, "
              f"threshold {a.model_thr if a.model_thr is not None else MODEL['threshold']:.2f}")
    labs = assemble(words, a.max_gap_pitch, a.lat_tol, a.h_tol, a.ang_tol, a.line_gap, a.centre_tol,
                    max_lines=a.max_lines, join_numerals=a.join_numerals, model=MODEL,
                    model_thr=(a.model_thr if a.model_thr is not None
                               else (MODEL["threshold"] if MODEL else 0.5)))
    nw = np.array([len(l["members"]) for l in labs])
    print(f"assembled {len(labs)} labels: {(nw==1).mean():.1%} single-word, "
          f"{(nw>1).mean():.1%} joined, {(np.array([l['lines'] for l in labs])>1).mean():.1%} multi-line; "
          f"max {nw.max()} words")

    with open(a.out, "w") as fh:
        for l in labs:
            fh.write(json.dumps(dict(text=l["text"], words=len(l["members"]), lines=l["lines"],
                                     h=round(l["h"], 1), ang=round(l["ang"], 1),
                                     gcx=round(l["cx"], 1), gcy=round(l["cy"], 1)),
                                ensure_ascii=False) + "\n")
    print(f"wrote {a.out}")

    if a.validate:
        from build_pin_index import load_pins, pins_in_box
        from shapely.geometry import Point
        from shapely.strtree import STRtree
        P = load_pins(a.pins)
        xs = np.array([l["cx"] for l in labs])
        ys = np.array([l["cy"] for l in labs])
        x0, x1, y0, y1 = xs.min() - 500, xs.max() + 500, ys.min() - 500, ys.max() + 500
        idx = pins_in_box(P, x0, y0, x1, y1)
        # Words alone are the baseline the assembly has to beat.
        wtree = STRtree([Point(w["f"]["cx"], w["f"]["cy"]) for w in words])
        ltree = STRtree([Point(l["cx"], l["cy"]) for l in labs])
        # A label's box is its words', so match on any member word rather than the label centroid: a pin
        # sits at the START of a label, which is far from the centroid of a long one.
        w2l = {}
        for li, l in enumerate(labs):
            for m in l["members"]:
                w2l[m] = li
        # An exact-string test scores the RECOGNISER and the ASSEMBLER at once, and on this material the
        # recogniser is the weaker of the two: "###ILSON STREET", "KING TREET" and "WALLS ST." are correctly
        # grouped labels that a character error keeps from matching. So the text figures are also reported
        # on the CLEAN subset — labels every one of whose words was read without an unknown-character
        # marker — where a mismatch really is a grouping mistake. Token COUNT is reported over everything,
        # since getting the right NUMBER of words right is a grouping question a misread letter cannot spoil.
        def clean(t):
            return "#" not in (t or "")
        n_pin = base_exact = asm_exact = asm_cover = asm_over = 0
        n_clean = clean_base = clean_exact = 0
        ntok_right = 0
        examples = []
        for k in idx:
            truth = str(P["text"][k])
            if not is_alpha(truth):
                continue
            px_, py_ = float(P["gx"][k]), float(P["gy"][k])
            cand = [int(c) for c in wtree.query(Point(px_, py_).buffer(a.radius))]
            cand = [c for c in cand
                    if math.hypot(words[c]["f"]["cx"] - px_, words[c]["f"]["cy"] - py_) <= a.radius]
            if not cand:
                continue
            n_pin += 1
            cand.sort(key=lambda c: math.hypot(words[c]["f"]["cx"] - px_, words[c]["f"]["cy"] - py_))
            nt = norm(truth)
            if norm(words[cand[0]]["text"]) == nt:
                base_exact += 1
            li = w2l[cand[0]]
            at = norm(labs[li]["text"])
            tt, at_toks = set(tokens(truth)), set(tokens(labs[li]["text"]))
            if at == nt:
                asm_exact += 1
            if tt and tt <= at_toks:
                asm_cover += 1
            if tt and at_toks - tt:
                asm_over += 1
            if len(tokens(labs[li]["text"])) == len(tokens(truth)):
                ntok_right += 1
            if all(clean(words[m]["text"]) for m in labs[li]["members"]) and clean(truth):
                n_clean += 1
                clean_base += norm(words[cand[0]]["text"]) == nt
                clean_exact += at == nt
            if len(examples) < 40 and at != nt:
                examples.append(dict(pin=truth, assembled=labs[li]["text"],
                                     words=len(labs[li]["members"]), lines=labs[li]["lines"]))
        if n_pin:
            print(f"\nvalidated on {n_pin} GB1900 labels with at least one word detected:")
            print(f"  nearest WORD alone reproduces the transcription   {base_exact/n_pin:.3f}")
            print(f"  ASSEMBLED label reproduces it exactly             {asm_exact/n_pin:.3f}")
            print(f"  assembled label CONTAINS every word of it         {asm_cover/n_pin:.3f}")
            print(f"  assembled label carries EXTRA words (over-join)   {asm_over/n_pin:.3f}")
            print(f"  assembled label has the RIGHT NUMBER of words     {ntok_right/n_pin:.3f}")
            if n_clean:
                print(f"  -- on the {n_clean} labels read without a character error "
                      f"({n_clean/n_pin:.0%} of the set), where a mismatch is a GROUPING mistake:")
                print(f"     nearest word alone {clean_base/n_clean:.3f}   assembled {clean_exact/n_clean:.3f}")
            json.dump(dict(pins=n_pin, word_exact=round(base_exact / n_pin, 3),
                           asm_exact=round(asm_exact / n_pin, 3),
                           asm_cover=round(asm_cover / n_pin, 3),
                           asm_over=round(asm_over / n_pin, 3),
                           asm_ntokens=round(ntok_right / n_pin, 3),
                           clean_n=n_clean,
                           clean_word_exact=round(clean_base / n_clean, 3) if n_clean else None,
                           clean_asm_exact=round(clean_exact / n_clean, 3) if n_clean else None,
                           examples=examples),
                      open(a.out.replace(".jsonl", "") + ".validate.json", "w"), indent=1,
                      ensure_ascii=False)
            print(f"  wrote {a.out.replace('.jsonl','')}.validate.json")
    print("ASSEMBLEDONE", flush=True)


if __name__ == "__main__":
    main()
