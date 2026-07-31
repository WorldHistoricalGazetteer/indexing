"""Learn "do these two words belong to the same label?" from GB1900, instead of hand-setting thresholds.

The rule-based join stopped at 0.342 exact reproduction and would not move: barring numerals bought 0.001,
capping the multi-line stack 0.003, tightening the gap and shared-centre 0.013. Under-join (0.48 of labels
never receive all their words) and over-join (0.45 carry extra words) are BOTH large at the same settings,
which is what a single fixed threshold set looks like when it is asked to serve dense urban and open moor at
once. No amount of nudging fixes that; the decision has to depend on the local evidence.

GB1900 supplies the supervision for free. A volunteer's transcription names every word of a label, so words
near a pin whose readings account for exactly that label's tokens are a known-good group, and a pair drawn
from one group is a positive while a pair spanning two groups is a negative.

TRAINED ON THE PAIRS IT WILL BE ASKED ABOUT. Candidates come from a deliberately loose geometric filter — the
same one used at inference — rather than from all pairs. Training on all pairs would drown the model in
trivially-distant negatives, and its probabilities would be calibrated for a question nobody asks.

AND ON PAIRS INVOLVING WORDS GB1900 NEVER PINNED. The first version only used pairs where BOTH words sat in
a known-good group, but at inference most neighbours are words no volunteer pinned — unpinned labels,
misreadings, map furniture — and the model had never been asked about one. What licenses the extra negatives
is that a group is COMPLETE: every token of the transcription is accounted for, so the label has no room
left, and any other word beside it is definitely not part of it. That inference needs the group to be
complete, which is exactly what the strict construction guarantees.

HELD OUT BY PLACE, NOT BY PAIR. Labels on one sheet share a draughtsman, a survey date and a printing, so a
random split would let the model memorise a region and report it as skill. Regions are grouped into blocks
about 12km across — coarser than an OS sheet — and whole blocks are held out.

    python join_train.py --boxes '/vast/ishi/gb1900/edition/spot/boxes_gb_*.jsonl' --out join_rf.joblib
"""
import argparse, glob, json, math, os, re, sys
from collections import defaultdict
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assemble_labels import word_frame, norm, tokens, is_alpha, facing

FEATURES = ["gap_pitch", "gap_h", "lat_h", "ang_diff", "h_ratio", "pitch_ratio",
            "long_ratio", "centre_h", "both_upper", "case_match", "either_numeric",
            "len_ratio", "min_h",
            # The face signal. On these sheets typeface encodes feature CATEGORY rather than decoration, so
            # two words in different faces are two different labels however neatly they line up. Absence is
            # marked -1 rather than 0 so the tree can split "not classified" off instead of reading a missing
            # face as a disagreeing one.
            "font_dot", "font_same_top", "font_known", "font_conf_min",
            # How far each word's centre-line departs from straight, in cap heights. A curved word is one
            # whose continuation a single global axis would point away from, so the model can learn to
            # weigh the geometric evidence differently there.
            "curv_max", "curv_sum"]


def font_pair(fa, fb):
    """Agreement between two words' font distributions, as a soft score rather than a hard identity."""
    if not fa or not fb:
        return [-1.0, -1.0, 0.0, -1.0]
    dot = sum(p * fb.get(k, 0.0) for k, p in fa.items())
    ta = max(fa.items(), key=lambda kv: kv[1])
    tb = max(fb.items(), key=lambda kv: kv[1])
    return [float(dot), 1.0 if ta[0] == tb[0] else 0.0, 1.0, float(min(ta[1], tb[1]))]


def load_fonts(d):
    """box key -> {font: probability}, from the sharded font_classify backfill."""
    import glob as _g
    out = {}
    for f in _g.glob(os.path.join(d, "*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = (round(r["gcx"] / 4), round(r["gcy"] / 4), norm(r.get("text", "")))
            out[k] = {a: b for a, b in r.get("fonts", [])}
    return out


def font_key(f):
    return (round(f["cx"] / 4), round(f["cy"] / 4))


def pair_features(A, B, ta, tb, fa=None, fb=None):
    """Symmetric description of a candidate join, in units the typography itself supplies.

    Everything that can be is expressed relative to the words' OWN cap height and character pitch, so the
    same feature values mean the same thing for a 12px descriptive name and a 60px parish heading.
    """
    hm, hmin = max(A["h"], B["h"]), min(A["h"], B["h"])
    dx, dy = B["cx"] - A["cx"], B["cy"] - A["cy"]
    d = math.hypot(dx, dy)
    outs = []
    for F, G in ((A, B), (B, A)):
        ux, uy = facing(F, dx if F is A else -dx, dy if F is A else -dy)
        along = abs(dx * ux + dy * uy)
        lat = abs(-dx * uy + dy * ux)
        outs.append((along - (A["long"] + B["long"]) / 2.0, lat))
    gap = float(np.mean([o[0] for o in outs]))
    lat = float(np.mean([o[1] for o in outs]))
    pm = max(1e-6, (A["pitch"] + B["pitch"]) / 2.0)
    ua, ub = ta.isupper(), tb.isupper()
    return [gap / pm, gap / max(1e-6, hm), lat / max(1e-6, hm),
            abs((A["ang"] - B["ang"] + 90) % 180 - 90),
            hmin / max(1e-6, hm),
            min(A["pitch"], B["pitch"]) / max(1e-6, max(A["pitch"], B["pitch"])),
            min(A["long"], B["long"]) / max(1e-6, max(A["long"], B["long"])),
            d / max(1e-6, hm),
            1.0 if (ua and ub) else 0.0,
            1.0 if (ua == ub) else 0.0,
            1.0 if (not is_alpha(ta) or not is_alpha(tb)) else 0.0,
            min(len(ta), len(tb)) / max(1.0, max(len(ta), len(tb))),
            hmin] + font_pair(fa, fb) + [max(A.get("curv", 0.0), B.get("curv", 0.0)),
                                         A.get("curv", 0.0) + B.get("curv", 0.0)]


def candidates(words, max_gap_pitch=6.0, lat_tol=1.4, h_tol=0.55, ang_tol=28.0):
    """The recall envelope. Deliberately loose: the model decides, this only bounds what it is shown."""
    F = [w["f"] for w in words]
    P = np.array([[f["cx"], f["cy"]] for f in F])
    order = np.argsort(P[:, 0])
    maxhalf = max(f["long"] for f in F) / 2.0
    out = []
    for oi in range(len(order)):
        i = int(order[oi])
        reach = max_gap_pitch * F[i]["pitch"] + F[i]["long"] / 2.0 + maxhalf
        for oj in range(oi + 1, len(order)):
            j = int(order[oj])
            if P[j, 0] - P[i, 0] > reach:
                break
            A, B = F[i], F[j]
            hm = max(A["h"], B["h"])
            if min(A["h"], B["h"]) / hm < 1 - h_tol:
                continue
            if abs((A["ang"] - B["ang"] + 90) % 180 - 90) > ang_tol:
                continue
            dx, dy = B["cx"] - A["cx"], B["cy"] - A["cy"]
            ux, uy = A["u"]
            if abs(-dx * uy + dy * ux) > lat_tol * hm:
                continue
            gap = abs(dx * ux + dy * uy) - (A["long"] + B["long"]) / 2.0
            if gap > max_gap_pitch * max(A["pitch"], B["pitch"]):
                continue
            out.append((i, j))
    return out


def label_groups(words, P, pins_in_box, radius=260.0):
    """Known-good label groups: words whose readings account for exactly one pin's transcription.

    Deliberately strict. A group is only kept when every token of the transcription is matched by a distinct
    nearby word, so a half-detected label never becomes a training example teaching the model that a label
    ends where the spotter stopped. Words claimed by two pins are dropped along with both groups rather than
    guessed at — "STREET" recurs constantly, and a wrong assignment is a wrong LABEL, not a wrong feature.
    """
    from shapely.geometry import Point
    from shapely.strtree import STRtree
    xs = np.array([w["f"]["cx"] for w in words])
    ys = np.array([w["f"]["cy"] for w in words])
    tree = STRtree([Point(x, y) for x, y in zip(xs, ys)])
    idx = pins_in_box(P, xs.min() - 500, ys.min() - 500, xs.max() + 500, ys.max() + 500)
    claim, groups = defaultdict(list), []
    for k in idx:
        truth = str(P["text"][k])
        if not is_alpha(truth):
            continue
        tk = tokens(truth)
        if not tk:
            continue
        px_, py_ = float(P["gx"][k]), float(P["gy"][k])
        cand = [int(c) for c in tree.query(Point(px_, py_).buffer(radius))]
        cand = [c for c in cand if math.hypot(xs[c] - px_, ys[c] - py_) <= radius]
        by = defaultdict(list)
        for c in cand:
            by[norm(words[c]["text"])].append(c)
        chosen, ok = [], True
        for t in tk:
            pool = [c for c in by.get(t, []) if c not in chosen]
            if not pool:
                ok = False
                break
            pool.sort(key=lambda c: math.hypot(xs[c] - px_, ys[c] - py_))
            chosen.append(pool[0])
        if not ok or len(set(chosen)) != len(tk):
            continue
        gi = len(groups)
        groups.append(dict(pin=str(P["pin_id"][k]), text=truth, members=chosen))
        for c in chosen:
            claim[c].append(gi)
    contested = {gi for c, gs in claim.items() if len(gs) > 1 for gi in gs}
    kept = [g for i, g in enumerate(groups) if i not in contested]
    w2g = {}
    for gi, g in enumerate(kept):
        for c in g["members"]:
            w2g[c] = gi
    return kept, w2g, len(contested)


def block_of(tag, size=4):
    """Regions are ~3km; a block of 4 is ~12km, coarser than an OS sheet, so a held-out block is a place
    the model has genuinely never seen rather than the far side of one."""
    m = re.match(r"gb_(\d+)_(\d+)", tag or "")
    if not m:
        return tag
    return f"{int(m.group(1))//size}_{int(m.group(2))//size}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boxes", nargs="+", required=True)
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--max-files", type=int, default=1200,
                    help="regions to draw from. With the full sweep this is the whole series; breadth "
                         "matters more than depth, so prefer many regions lightly sampled")
    ap.add_argument("--sample-per-region", type=int, default=0,
                    help="cap pairs taken from any ONE region (0 = no cap). Taking everything from a few "
                         "regions is what biases the model toward whatever lettering those regions happen "
                         "to use; a cap spreads the same budget over far more of the country, which is "
                         "where the rare faces live")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--min-lines", type=int, default=200, help="skip near-empty region files")
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--fonts", default=None,
                    help="font_v2 dir from the sharded font_classify backfill; adds the face features")
    ap.add_argument("--out", default="join_rf.joblib")
    a = ap.parse_args()

    from build_pin_index import load_pins, pins_in_box
    files = [f for pat in a.boxes for f in sorted(glob.glob(pat))]
    files = [f for f in files if sum(1 for _ in open(f)) >= a.min_lines]
    # Largest-first was right when only a sample had been spotted; across the full series it would take
    # every pair from the densest towns and none from the countryside. Shuffle, so the draw is national.
    rng0 = np.random.default_rng(a.seed)
    files = list(files)
    rng0.shuffle(files)
    files = files[: a.max_files]
    print(f"{len(files)} region files with >= {a.min_lines} detections")
    P = load_pins(a.pins)
    FONTS = load_fonts(a.fonts) if a.fonts else {}
    if a.fonts:
        print(f"{len(FONTS)} classified boxes carry a face")

    X, Y, B = [], [], []
    n_words = n_groups = n_contested = 0
    for f in files:
        tag = os.path.basename(f)[6:-6]
        words, seen = [], set()
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            p, txt = r.get("gpoly"), str(r.get("text", "")).strip()
            if not p or not txt:
                continue
            cx = r.get("gcx") or sum(q[0] for q in p) / len(p)
            cy = r.get("gcy") or sum(q[1] for q in p) / len(p)
            k = (round(cx / 4), round(cy / 4), norm(txt))
            if k in seen:
                continue
            seen.add(k)
            wf = word_frame(p, txt, r.get("gline"))
            if wf is None:
                continue                                   # degenerate outline — see word_frame
            words.append(dict(text=txt, f=wf,
                              font=FONTS.get((round(cx / 4), round(cy / 4), norm(txt)))))
        if len(words) < 30:
            continue
        n_words += len(words)
        groups, w2g, nc = label_groups(words, P, pins_in_box)
        n_groups += len(groups)
        n_contested += nc
        if not groups:
            continue
        blk = block_of(tag)
        cand = candidates(words)
        if a.sample_per_region and len(cand) > a.sample_per_region:
            pick = rng0.choice(len(cand), a.sample_per_region, replace=False)
            cand = [cand[int(k)] for k in pick]
        for i, j in cand:
            gi, gj = w2g.get(i), w2g.get(j)
            if gi is None and gj is None:
                continue          # neither word is accounted for: nothing can be said about this pair
            X.append(pair_features(words[i]["f"], words[j]["f"], words[i]["text"], words[j]["text"],
                                   words[i].get("font"), words[j].get("font")))
            # Same complete group -> positive. Otherwise at least one side belongs to a label whose words
            # are all already spoken for, so the pair cannot be a join.
            Y.append(1 if (gi is not None and gi == gj) else 0)
            B.append(blk)
    X, Y, B = np.array(X, np.float32), np.array(Y, np.int8), np.array(B)
    print(f"{n_words} words, {n_groups} known-good label groups ({n_contested} dropped as contested)")
    if not len(X):
        raise SystemExit("no labelled pairs")
    print(f"{len(X)} labelled candidate pairs, {Y.mean():.1%} positive, over {len(set(B))} blocks")

    blocks = sorted(set(B))
    rng = np.random.default_rng(0)
    rng.shuffle(blocks)
    ntest = max(1, int(len(blocks) * a.test_frac))
    test_blocks = set(blocks[:ntest])
    te = np.array([b in test_blocks for b in B])
    tr = ~te
    print(f"  train {tr.sum()} pairs / {len(blocks)-ntest} blocks, "
          f"test {te.sum()} pairs / {ntest} blocks (held out by place)")

    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score, precision_recall_curve
    clf = RandomForestClassifier(n_estimators=300, min_samples_leaf=8, n_jobs=-1,
                                 class_weight="balanced_subsample", random_state=0)
    clf.fit(X[tr], Y[tr])
    p = clf.predict_proba(X[te])[:, 1]
    auc = roc_auc_score(Y[te], p)
    print(f"\nheld-out pair AUC {auc:.4f}")
    prec, rec, thr = precision_recall_curve(Y[te], p)
    f1 = 2 * prec * rec / np.maximum(1e-9, prec + rec)
    b = int(np.argmax(f1))
    best_thr = float(thr[min(b, len(thr) - 1)])
    print(f"  best F1 {f1[b]:.3f} at threshold {best_thr:.2f} "
          f"(precision {prec[b]:.3f}, recall {rec[b]:.3f})")
    for t in (0.3, 0.5, 0.7, 0.9):
        q = p >= t
        if q.sum():
            print(f"  threshold {t:.1f}: precision {Y[te][q].mean():.3f}, "
                  f"recall {Y[te][q].sum()/max(1,Y[te].sum()):.3f}")
    # The rule-based join is the thing to beat, scored on the same held-out pairs.
    rule = ((X[te][:, 0] <= 2.5) & (X[te][:, 2] <= 0.6) & (X[te][:, 3] <= 12.0) & (X[te][:, 4] >= 0.68))
    if rule.sum():
        print(f"  the hand-set rules, on these pairs: precision {Y[te][rule].mean():.3f}, "
              f"recall {Y[te][rule].sum()/max(1,Y[te].sum()):.3f}")
    imp = sorted(zip(FEATURES, clf.feature_importances_), key=lambda t: -t[1])
    print("  what it uses: " + ", ".join(f"{k} {v:.2f}" for k, v in imp[:7]))

    import joblib
    # The held-out blocks travel WITH the model. Otherwise the next person to score the assembler will
    # reach for the biggest region files, which are the ones it trained on, and report a memorised number.
    joblib.dump(dict(model=clf, features=FEATURES, threshold=best_thr, auc=float(auc),
                     test_blocks=sorted(test_blocks), block_size=4), a.out)
    json.dump(sorted(test_blocks), open(a.out.replace(".joblib", "") + ".test_blocks.json", "w"))
    print(f"wrote {a.out}\nJOINTRAINDONE", flush=True)


if __name__ == "__main__":
    main()
