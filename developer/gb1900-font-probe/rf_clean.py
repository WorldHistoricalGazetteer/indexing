"""Random-forest pixel classifier for map linework — the Ilastik workflow, run in-repo.

Trains on brush strokes painted in `make_paint_ui.py` over a multi-scale filter bank (intensity, gradients and
Hessian eigenvalues at several sigmas — the same feature family Ilastik uses), then labels every pixel of a
sheet as paper / text / line / hatch / solid and erases everything that is not text or paper.

Why this rather than more morphology: hatching is the case hand-written rules cannot reach. One hatch stroke
has the same width, length and darkness as one letter stroke, so no local measurement separates them; what
separates them is texture and context, which is precisely what a filter bank encodes and a few painted strokes
are enough to teach.

Reports held-out accuracy PER CLASS, split BY CROP rather than by pixel. Pixels inside one brush stroke are so
correlated that a random pixel split reports near-perfect accuracy for any feature set, which would tell us
nothing about a sheet the classifier has not seen.

    python rf_clean.py train --labels paint_labels_X.json --bbox W S E N --model rf.joblib
    python rf_clean.py apply --model rf.joblib --tag X --bbox W S E N --out-tiles DIR --diag D.png
"""
import argparse, base64, json, math, os, sys, time
import numpy as np, cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hisam_pins import N17
from sheet_clean import stitch, flat_field, lat_px

CLASSES = ["paper", "text", "dash", "dotline", "line", "rail", "hatch", "stipple", "solid"]
KEEP = {"paper", "text"}                       # everything else is noise to be erased
SIGMAS = (1, 2, 4, 8, 16)


def features(gray):
    """Multi-scale filter bank: intensity, edges and texture at several scales.

    Scales matter more than any single filter here — hatching is only distinguishable from type when the
    window is wide enough to see its regularity, while a serif needs the finest scale to survive at all.
    """
    from skimage.feature import multiscale_basic_features
    return multiscale_basic_features(gray, intensity=True, edges=True, texture=True,
                                     sigma_min=SIGMAS[0], sigma_max=SIGMAS[-1], channel_axis=None)


def sheet_bbox(bbox):
    w, s, e, n = bbox
    tx0 = int(((w + 180.0) / 360.0 * N17 * 256) // 256)
    tx1 = int(((e + 180.0) / 360.0 * N17 * 256) // 256)
    ty0, ty1 = int(lat_px(n) // 256), int(lat_px(s) // 256)
    return tx0, ty0, tx1 - tx0 + 1, ty1 - ty0 + 1


def load_sheet(bbox, flatten=True):
    tx0, ty0, nx, ny = sheet_bbox(bbox)
    rgb, hit = stitch(tx0, ty0, nx, ny)
    if flatten:
        rgb = flat_field(rgb)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), (tx0, ty0, nx, ny), hit


def cmd_train(a):
    from sklearn.ensemble import RandomForestClassifier
    import joblib

    # Multiple label files merge, because painting rounds specialise: a round told to concentrate on text
    # will contain no solid at all, and dropping the earlier round would lose that class entirely. Crop ids
    # collide across files (each round numbers from zero over DIFFERENT crops), so they are namespaced by
    # file — otherwise leave-one-crop-out would hold out two unrelated crops as though they were one.
    rounds = [json.load(open(f)) for f in a.labels]
    drop = set(a.drop or [])
    if drop:
        print(f"dropping classes {sorted(drop)} from all rounds", flush=True)
    gray, (tx0, ty0, nx, ny), hit = load_sheet(a.bbox, a.flatten)
    print(f"sheet {gray.shape[1]}x{gray.shape[0]} ({hit} tiles); "
          f"{sum(len(r['crops']) for r in rounds)} painted crops from {len(rounds)} file(s)", flush=True)

    X, y, grp = [], [], []
    allcrops = [(fi, cr) for fi, r in enumerate(rounds) for cr in r["crops"]]
    for fi, cr in allcrops:
        S = cr["size"]
        L = np.frombuffer(base64.b64decode(cr["labels"]), np.uint8).reshape(S, S).copy()
        # Remap by NAME, not index. Rounds painted under an older class list would otherwise silently shift:
        # index 2 meant `line` then and means `dash` now, which would poison the very class being fixed.
        names = rounds[fi].get("classes") or CLASSES
        remap = np.full(256, 255, np.uint8)
        for i, nm in enumerate(names):
            if nm in drop:
                continue
            if nm in CLASSES:
                remap[i] = CLASSES.index(nm)
        L = remap[L]
        x0, y0 = cr["gx"] - tx0 * 256, cr["gy"] - ty0 * 256
        if x0 < 0 or y0 < 0 or y0 + S > gray.shape[0] or x0 + S > gray.shape[1]:
            print(f"  r{fi} crop {cr['id']} outside this sheet — skipped", flush=True)
            continue
        patch = gray[y0:y0 + S, x0:x0 + S]
        F = features(patch)
        m = L < len(CLASSES)                                   # 255 = unpainted
        # HIGHLIGHT, don't trace. At this scale nobody can brush along a 2 px letter stroke, so the brush is
        # meant to be swept across whole labels and blocks — and the ink under it is extracted here. Every
        # class except `paper` keeps only its INK pixels; `paper` keeps only its NON-ink pixels. This costs
        # nothing at apply time either, since only ink is ever erased, and it means a sloppy stroke over a
        # word contributes exactly the letter pixels and none of the paper between them.
        _, cbw = cv2.threshold(patch, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        cink = cbw > 0
        paper_i = CLASSES.index("paper")
        text_i = CLASSES.index("text")
        before = int(m.sum())
        m &= np.where(L == paper_i, ~cink, cink)
        # Highlighting has a cost: a sweep across a label also covers the casings, building outlines and
        # railway rules that run through and beside it, and the ink filter dutifully calls all of it text. On
        # round 2 that made the classifier label every thin stroke on the sheet as text. So ink under a TEXT
        # stroke that belongs to a long, smooth, thin traced run is dropped — not relabelled, just excluded,
        # since we cannot know which linework class it is. This is the one job the tracer is genuinely good
        # at: it never confidently traced a letter.
        if a.purge_lines and (L == text_i).any():
            from sheet_clean import trace_lines
            lm, _ = trace_lines(cbw, min_length=a.purge_length)
            bad = (L == text_i) & (lm > 0) & m
            if bad.any():
                m &= ~bad
                print(f"  r{fi} crop {cr['id']}: dropped {int(bad.sum())} text px that trace as linework",
                      flush=True)
        if before and not m.any():
            print(f"  r{fi} crop {cr['id']}: {before} painted px, none survived the ink filter", flush=True)
            continue
        if not m.any():
            continue
        X.append(F[m])
        y.append(L[m])
        grp.append(np.full(int(m.sum()), fi * 1000 + cr["id"]))
        print(f"  r{fi} crop {cr['id']}: {int(m.sum())} ink px (of {before} painted) "
              f"{{{', '.join(f'{CLASSES[c]}:{int((L[m]==c).sum())}' for c in np.unique(L[m]))}}}", flush=True)
    if not X:
        raise SystemExit("no painted pixels found on this sheet")
    X = np.concatenate(X).astype(np.float32)
    y = np.concatenate(y)
    grp = np.concatenate(grp)
    print(f"\n{len(X)} painted pixels, {X.shape[1]} features, "
          f"{len(np.unique(grp))} crops", flush=True)
    for c in np.unique(y):
        print(f"  {CLASSES[c]:6s} {int((y==c).sum()):>8d}", flush=True)

    # LEAVE ONE CROP OUT. Holding out a random quarter of the crops is worthless at this scale: with 8 crops
    # the draw can easily be two that were painted with a single class, and the resulting "overall 1.000"
    # measures nothing (it happened). LOCO uses every crop as test exactly once, and a class is scored only
    # over the crops where it was actually painted — so a class painted on two crops gets a number based on
    # two crops, and says so, instead of hiding behind an average.
    crops = np.unique(grp)
    hit = {c: [0, 0] for c in np.unique(y)}
    crops_with = {c: 0 for c in np.unique(y)}
    binhit = {"keep": [0, 0], "erase": [0, 0]}
    for held in crops:
        te = grp == held
        if not (~te).any():
            continue
        f = RandomForestClassifier(n_estimators=a.trees, max_depth=a.depth, n_jobs=-1,
                                   class_weight="balanced", random_state=0)
        f.fit(X[~te], y[~te])
        pred = f.predict(X[te])
        for c in np.unique(y[te]):
            m = y[te] == c
            hit[c][0] += int((pred[m] == c).sum())
            hit[c][1] += int(m.sum())
            crops_with[c] += 1
        kset = {CLASSES.index(c) for c in KEEP}
        yk = np.isin(y[te], list(kset))
        pk = np.isin(pred, list(kset))
        binhit["keep"][0] += int((pk & yk).sum()); binhit["keep"][1] += int(yk.sum())
        binhit["erase"][0] += int((~pk & ~yk).sum()); binhit["erase"][1] += int((~yk).sum())
    print(f"\nleave-one-crop-out over {len(crops)} crops:", flush=True)
    for c in sorted(hit, key=lambda z: -hit[z][1]):
        ok, n = hit[c]
        if not n:
            continue
        warn = "   << only 1 crop: not a generalisation test" if crops_with[c] < 2 else ""
        print(f"  {CLASSES[c]:6s} recall {ok/n:.3f}  (n={n} px over {crops_with[c]} crops){warn}", flush=True)
    # The 5-class recall overstates the damage: line and hatch are BOTH erased, so confusing one for the
    # other costs nothing. What the pipeline actually decides is keep-or-erase, and only two errors matter —
    # text called linework (a label is destroyed) and linework called text (noise survives).
    tk, tn = binhit["keep"]
    ek, en = binhit["erase"]
    print(f"  --- keep/erase, the decision actually made ---", flush=True)
    print(f"  KEEP  (paper+text) kept    {tk/max(1,tn):.3f}  (n={tn})", flush=True)
    print(f"  ERASE (line+hatch+solid)   {ek/max(1,en):.3f}  (n={en})", flush=True)

    clf = RandomForestClassifier(n_estimators=a.trees, max_depth=a.depth, n_jobs=-1,
                                 class_weight="balanced", random_state=0)

    clf.fit(X, y)                                              # refit on everything for the applied model
    joblib.dump(dict(clf=clf, classes=CLASSES, sigmas=SIGMAS, flatten=a.flatten), a.model)
    print(f"\nwrote {a.model}\nRFTRAINDONE", flush=True)


def cmd_apply(a):
    import joblib
    bundle = joblib.load(a.model)
    clf = bundle["clf"]
    gray, (tx0, ty0, nx, ny), hit = load_sheet(a.bbox, bundle.get("flatten", True))
    H, W = gray.shape
    print(f"{a.tag}: {W}x{H} ({hit} tiles)", flush=True)
    t0 = time.time()

    # Block-wise with a margin, because the widest filter needs context: a block edge would otherwise be
    # classified from a truncated neighbourhood and show as a seam.
    B, M = a.block, 4 * SIGMAS[-1]
    lab = np.zeros((H, W), np.uint8)
    for y0 in range(0, H, B):
        for x0 in range(0, W, B):
            ya, yb = max(0, y0 - M), min(H, y0 + B + M)
            xa, xb = max(0, x0 - M), min(W, x0 + B + M)
            F = features(gray[ya:yb, xa:xb])
            p = clf.predict(F.reshape(-1, F.shape[-1])).reshape(yb - ya, xb - xa)
            lab[y0:min(H, y0 + B), x0:min(W, x0 + B)] = \
                p[y0 - ya:min(H, y0 + B) - ya, x0 - xa:min(W, x0 + B) - xa]
        print(f"  row {y0}/{H} ({time.time()-t0:.0f}s)", flush=True)

    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = bw > 0
    drop = np.zeros((H, W), bool)
    for i, c in enumerate(CLASSES):
        if c not in KEEP:
            drop |= (lab == i)
    drop &= ink
    print(f"  removing {drop.sum()/max(1,ink.sum()):.1%} of ink", flush=True)
    for i, c in enumerate(CLASSES):
        print(f"    {c:6s} {(((lab==i)&ink).sum())/max(1,ink.sum()):.1%} of ink", flush=True)

    if a.diag:
        cols = {"paper": (255, 255, 255), "text": (30, 60, 255), "line": (0, 160, 0),
                "hatch": (255, 152, 0), "solid": (208, 32, 32)}
        rgbd = np.repeat(gray[:, :, None], 3, 2).copy()
        for i, c in enumerate(CLASSES):
            if c == "paper":
                continue
            rgbd[(lab == i) & ink] = cols[c]
        os.makedirs(os.path.dirname(a.diag), exist_ok=True)
        cv2.imwrite(a.diag, cv2.cvtColor(rgbd, cv2.COLOR_RGB2BGR))
        print(f"  diagnostic -> {a.diag}", flush=True)

    out = gray.copy()
    out[drop] = 255
    if a.clean_png:
        cv2.imwrite(a.clean_png, out)
        print(f"  cleaned sheet -> {a.clean_png}", flush=True)
    if a.out_tiles:
        for i in range(nx):
            for j in range(ny):
                d = f"{a.out_tiles}/{tx0+i}"
                os.makedirs(d, exist_ok=True)
                t = out[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256]
                cv2.imwrite(f"{d}/{ty0+j}.png", cv2.cvtColor(np.repeat(t[:, :, None], 3, 2), cv2.COLOR_RGB2BGR))
        print(f"  wrote {nx*ny} tiles -> {a.out_tiles}", flush=True)
    print("RFAPPLYDONE", flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("train", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("W", "S", "E", "N"))
        p.add_argument("--model", required=True)
        p.add_argument("--no-flatten", dest="flatten", action="store_false")
        if name == "train":
            p.add_argument("--labels", required=True, nargs="+", help="one or more painting rounds")
            p.add_argument("--trees", type=int, default=200)
            p.add_argument("--depth", type=int, default=None)
            p.add_argument("--no-purge-lines", dest="purge_lines", action="store_false",
                           help="keep linework ink that fell under a text highlight (it will poison text)")
            p.add_argument("--purge-length", type=int, default=60)
            p.add_argument("--drop", nargs="*", default=None,
                           help="class names to discard from the loaded rounds (e.g. a superseded `line`)")
        else:
            p.add_argument("--tag", required=True)
            p.add_argument("--out-tiles", default=None)
            p.add_argument("--diag", default=None)
            p.add_argument("--clean-png", default=None)
            p.add_argument("--block", type=int, default=1024)
    a = ap.parse_args()
    (cmd_train if a.cmd == "train" else cmd_apply)(a)


if __name__ == "__main__":
    main()
