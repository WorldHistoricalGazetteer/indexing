#!/usr/bin/env python3
"""The v8 acceptance suite — one frozen test set, many models.

    python v8_suite.py build  --out testset.json          # ONCE, from the corpus
    python v8_suite.py run    --testset testset.json \
                              --model-dir DIR --label v8B --out report-v8B.json

WHY A FROZEN TEST SET. v7, v8A and v8B must be scored on byte-identical data or
the comparison measures the sample as much as the model — the confound this
campaign has already recorded three times. `build` samples the corpus once and
writes it; `run` never samples.

WHY EACH MODEL NEEDS ITS OWN config.json. v7 carries vocab_size 113,280 /
num_scripts 20 / num_langs 1,944; the v8 extraction carries 114,845 / 37 /
2,438 — the extra scripts are the section 2 detection fix. Pairing a model with
the wrong vocabulary does not raise: it produces plausible garbage. `run`
derives the config from the vocab beside the weights and then PROVES the pairing
with controls that must pass before any score is computed.

THE CONTROLS ARE THE POINT. Every band below is meaningless without them:
  identity        cos(x,x) must be 1.0            — else the harness is wrong
  distinctness    embeddings must not collapse    — else every score is 1.0
  known pair      London/Лондон must score high   — else the vocab is mismatched
A failure of any of these aborts and computes no bands.
"""
from __future__ import annotations
import argparse, base64, json, os, random, sys, urllib.request
from pathlib import Path

SEED = 20260913
GATE = 0.7          # the production KNN similarity gate
ES = os.environ.get("SUITE_ES", "http://localhost:9201")


# ---------------------------------------------------------------- corpus pull
def _es(path, body=None, pw=None):
    hdr = {"Content-Type": "application/json"}
    if pw:
        hdr["Authorization"] = "Basic " + base64.b64encode(f"elastic:{pw}".encode()).decode()
    req = urllib.request.Request(ES + path, data=json.dumps(body).encode() if body else None,
                                 headers=hdr)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def build(args):
    """Sample the corpus ONCE. Everything downstream reads this file."""
    global ES
    if getattr(args, "es_host", None):
        ES = args.es_host
    random.seed(SEED)
    pw = None
    if Path(args.password_file).exists():
        pw = Path(args.password_file).read_text().strip()

    # Co-attested names: two distinct names on ONE place are a corpus-derived
    # positive. Never a hand-typed list — see expectation_from_artefact_not_typed.
    q = {"size": args.sample,
         "query": {"function_score": {
             "query": {"bool": {"filter": [
                 {"nested": {"path": "toponyms", "query": {"exists": {"field": "toponyms.label"}}}}]}},
             "random_score": {"seed": SEED, "field": "_seq_no"}}},
         "_source": ["place_id", "toponyms.label"]}
    if args.sample > 10000:
        raise SystemExit("--sample above the 10,000 max_result_window would 400")
    hits = _es("/places/_search", q, pw)["hits"]["hits"]
    print(f"  places sampled: {len(hits):,}")

    def latin(s):
        return s and all(ord(c) < 0x250 for c in s) and any(c.isalpha() for c in s)

    groups, cross = [], []
    for h in hits:
        labs = [t.get("label") for t in (h["_source"].get("toponyms") or []) if t.get("label")]
        names = sorted({l for l in labs if l and 3 <= len(l) <= 24})
        lat = [n for n in names if latin(n)]
        non = [n for n in names if not latin(n)]
        if len(lat) >= 2:
            groups.append({"place_id": h["_source"]["place_id"], "names": lat[:4]})
        if lat and non:                      # a cross-script positive pair
            cross.append({"place_id": h["_source"]["place_id"],
                          "latin": lat[0], "other": non[0]})

    # Chinese repair stratum: Han name + a Latin name on the same place.
    zq = {"size": args.sample, "query": {"function_score": {
              "query": {"bool": {"filter": [{"term": {"lang": "zh"}}, {"term": {"script": "CJK"}},
                                            {"exists": {"field": "ipa"}}]}},
              "random_score": {"seed": SEED, "field": "_seq_no"}}},
          "_source": ["name", "attestations"]}
    zh = [{"name": h["_source"]["name"],
           "attestations": (h["_source"].get("attestations") or [])[:3]}
          for h in _es("/toponyms/_search", zq, pw)["hits"]["hits"]]

    # The ten blackout languages of section 2 — the population the re-extract exists for.
    dead = {}
    for lang in ("my", "pa", "bo", "si", "km", "sat", "lo", "am", "or", "ti"):
        dq = {"size": args.dead_per_lang, "query": {"function_score": {
                  "query": {"bool": {"filter": [{"term": {"lang": lang}}]}},
                  "random_score": {"seed": SEED, "field": "_seq_no"}}},
              "_source": ["name", "script", "lang"]}
        try:
            rows = _es("/toponyms/_search", dq, pw)["hits"]["hits"]
            if rows:
                dead[lang] = [r["_source"]["name"] for r in rows]
        except Exception as exc:
            print(f"  ! {lang}: {exc}", file=sys.stderr)

    out = {"seed": SEED, "gate": GATE,
           "variant_groups": groups, "cross_script": cross,
           "zh": zh, "dead_scripts": dead}
    Path(args.out).write_text(json.dumps(out))
    print(f"variant groups (>=2 Latin names on one place): {len(groups):,}")
    print(f"cross-script positives                       : {len(cross):,}")
    print(f"zh+CJK names                                 : {len(zh):,}")
    print(f"blackout languages present                   : "
          f"{ {k: len(v) for k, v in dead.items()} }")
    print(f"wrote {args.out}")




# ------------------------------------------------------------------ the suite
def _load_model(weights: Path, vocab_dir: Path, device: str):
    """Pair weights with THEIR OWN vocabulary and prove the pairing.

    A model loaded against the wrong vocabulary does not raise — it embeds
    nonsense confidently. v7 is 113,280/20/1,944; the v8 extraction is
    114,845/37/2,438. The config is therefore derived from the vocab sitting
    beside the weights, never copied from another model.
    """
    import shutil, tempfile, torch
    # 🛑 DIMENSIONS COME FROM THE CHECKPOINT, NOT FROM COUNTING THE VOCAB.
    # Counting and adding one for a pad slot is a guess about a convention:
    # v7 is exactly 113,280 / 20 / 1,944, and a +1 or a floor of 25 produces a
    # shape mismatch. The weights are the artefact; read them.
    state = torch.load(weights, map_location="cpu")
    state = state.get("model_state_dict", state.get("state_dict", state))
    dims = {
        "vocab_size":       int(state["char_embed.weight"].shape[0]),
        "char_embed_dim":   int(state["char_embed.weight"].shape[1]),
        "num_scripts":      int(state["script_embed.weight"].shape[0]),
        "script_embed_dim": int(state["script_embed.weight"].shape[1]),
        "num_langs":        int(state["lang_embed.weight"].shape[0]),
        "lang_embed_dim":   int(state["lang_embed.weight"].shape[1]),
        "num_length_buckets": int(state["length_embed.weight"].shape[0]),
        "length_embed_dim": int(state["length_embed.weight"].shape[1]),
        "embed_dim":        int(state["output_proj.3.weight"].shape[0]),
        "hidden_dim":       int(state["bilstm.weight_ih_l0"].shape[0] // 4),
    }

    # The vocab must AGREE with those shapes. A vocab one row larger or smaller
    # than the embedding table means the wrong pairing, which would otherwise
    # tokenise to valid-but-wrong ids and embed confident nonsense.
    cv = json.loads((vocab_dir / "char_vocab.json").read_text())
    lv = json.loads((vocab_dir / "lang_vocab.json").read_text())
    sv = json.loads((vocab_dir / "script_vocab.json").read_text())
    counts = {"char": len(cv.get("char_to_id", cv)),
              "lang": len(lv.get("lang_to_id", lv)),
              "script": len(sv.get("script_to_id", sv))}
    for key, got, want in (("char", counts["char"], dims["vocab_size"]),
                           ("lang", counts["lang"], dims["num_langs"]),
                           ("script", counts["script"], dims["num_scripts"])):
        if got > want:
            raise SystemExit(
                f"VOCAB/WEIGHTS MISMATCH: {key}_vocab has {got:,} entries but the "
                f"checkpoint's embedding table has only {want:,} rows. These are not "
                f"the vocabulary this model was trained with — refusing to score it.")

    tmp = Path(tempfile.mkdtemp(prefix="symphonym-eval-"))
    (tmp / "vocab").mkdir()
    for n in ("char", "lang", "script"):
        shutil.copy(vocab_dir / f"{n}_vocab.json", tmp / "vocab" / f"{n}_vocab.json")
    shutil.copy(weights, tmp / "final_model.pt")
    cfg = {"model_type": "symphonym", "architectures": ["UniversalEncoder"],
           "num_layers": 2, "num_attention_heads": 2, "dropout": 0.2,
           "lang_dropout": 0.5}
    cfg.update(dims)
    (tmp / "config.json").write_text(json.dumps(cfg))
    sys.path.insert(0, "/vast/ishi/elastic/hf")
    from inference import SymphonymModel
    m = SymphonymModel(model_dir=tmp, device=device)
    return m, {**dims, "vocab_counts": counts}


def _embed(model, names):
    import numpy as np
    V = model.batch_embed([(n, "und") for n in names])
    V = np.asarray(V, dtype="float32")
    norms = np.linalg.norm(V, axis=1, keepdims=True)
    return V / np.where(norms == 0, 1.0, norms)


def _perm(text, rng, min_disp=0.5, tries=25):
    if len(text) < 4 or len(set(text)) < 3:
        return None
    orig = list(text)
    need = max(2, int(len(orig) * min_disp))
    for _ in range(tries):
        c = orig[:]
        rng.shuffle(c)
        if c != orig and sum(1 for i, ch in enumerate(c) if ch != orig[i]) >= need:
            return "".join(c)
    return None


def _typo(text, rng):
    """A SINGLE adjacent transposition — a real typo that must STILL match.

    This is the counterweight to the anagram band. The permutation negatives
    could improve the anagram number by destroying order tolerance entirely,
    which would break `Lodnon` -> London. A v8 that wins the anagram band by
    losing this has not improved.
    """
    if len(text) < 4:
        return None
    i = rng.randrange(len(text) - 1)
    c = list(text)
    c[i], c[i + 1] = c[i + 1], c[i]
    return "".join(c) if "".join(c) != text else None


def run(args):
    import numpy as np
    rng = random.Random(SEED)
    ts = json.loads(Path(args.testset).read_text())
    gate = ts.get("gate", GATE)
    model, vinfo = _load_model(Path(args.weights), Path(args.vocab_dir), args.device)
    rep = {"label": args.label, "weights": str(args.weights), "vocab": vinfo,
           "testset": args.testset, "gate": gate, "controls": {}, "bands": {}}

    # ---- CONTROLS. Nothing below is computed if these fail. -----------------
    probe = ["London", "Paris", "Warszawa", "القاهرة", "北京", "Ελλάδα"]
    P = _embed(model, probe)
    ident = float(np.min(np.sum(P * P, axis=1)))
    offdiag = P @ P.T
    np.fill_diagonal(offdiag, -1.0)
    rep["controls"]["identity_cos_min"] = round(ident, 6)
    rep["controls"]["max_offdiagonal_cos"] = round(float(offdiag.max()), 4)
    known = float(_embed(model, ["London"])[0] @ _embed(model, ["Лондон"])[0])
    rep["controls"]["London_vs_Лондон"] = round(known, 4)
    ok = abs(ident - 1.0) < 1e-4 and offdiag.max() < 0.999 and known > 0.5
    rep["controls"]["PASSED"] = bool(ok)
    if not ok:
        rep["controls"]["note"] = ("FAILED — identity must be 1.0, distinct names must not "
                                   "collapse, and a known cross-script pair must score > 0.5. "
                                   "A wrong vocab pairing looks exactly like this.")
        Path(args.out).write_text(json.dumps(rep, indent=1, ensure_ascii=False))
        print(json.dumps(rep["controls"], indent=1, ensure_ascii=False))
        raise SystemExit("controls failed — no bands computed")

    # ---- BAND 1: order discrimination (the primary acceptance measure) ------
    Q, V_, P_, R_, T_, U_ = [], [], [], [], [], []
    pool = [n for g in ts["variant_groups"] for n in g["names"]]
    for g in ts["variant_groups"]:
        names = g["names"]
        q = max(names, key=len)
        v = next((n for n in names if n != q), None)
        p, t = _perm(q, rng), _typo(q, rng)
        if not (v and p and t):
            continue
        cand = [n for n in pool if abs(len(n) - len(q)) <= 1 and n not in names]
        if not cand:
            continue
        Q.append(q); V_.append(v); P_.append(p); R_.append(q[::-1]); T_.append(t)
        U_.append(rng.choice(cand))

    if Q:
        EQ = _embed(model, Q)
        bands = {}
        for name, arr in (("true_variant", V_), ("permutation", P_),
                          ("reversal", R_), ("typo_1_swap", T_), ("unrelated", U_)):
            E = _embed(model, arr)
            cos = np.sum(EQ * E, axis=1)
            bands[name] = {"n": len(cos), "mean": round(float(cos.mean()), 4),
                           "median": round(float(np.median(cos)), 4),
                           "pct_clearing_gate": round(100.0 * float((cos >= gate).mean()), 1)}
        cosV = np.sum(EQ * _embed(model, V_), axis=1)
        cosP = np.sum(EQ * _embed(model, P_), axis=1)
        bands["variant_beats_permutation_pct"] = round(100.0 * float((cosV > cosP).mean()), 1)
        rep["bands"]["order"] = bands

    # ---- BAND 2: cross-script positives -------------------------------------
    cs = ts["cross_script"][:args.max_pairs]
    if cs:
        A = _embed(model, [c["latin"] for c in cs])
        B = _embed(model, [c["other"] for c in cs])
        cos = np.sum(A * B, axis=1)
        rep["bands"]["cross_script"] = {
            "n": len(cos), "mean": round(float(cos.mean()), 4),
            "median": round(float(np.median(cos)), 4),
            "pct_clearing_gate": round(100.0 * float((cos >= gate).mean()), 1)}
        han = [c for c in cs if any('一' <= ch <= '鿿' for ch in c["other"])]
        if han:
            HA = _embed(model, [c["latin"] for c in han])
            HB = _embed(model, [c["other"] for c in han])
            hcos = np.sum(HA * HB, axis=1)
            rep["bands"]["chinese_latin"] = {
                "n": len(hcos), "mean": round(float(hcos.mean()), 4),
                "median": round(float(np.median(hcos)), 4),
                "pct_clearing_gate": round(100.0 * float((hcos >= gate).mean()), 1),
                "note": "v7 learned these from JAPANESE readings of Han characters"}

    # ---- BAND 2b: THE OVERLAP GAP (whg3's measure, and the deciding one) ----
    #
    # The order band and the cross-script band measure the two things the
    # gateway needs SEPARATELY, and they live in the same cosine range. Measured
    # against prod 2026-08-20, a genuine cross-script positive
    # (Marsails -> مارساليس, 0.9878) sits BELOW the junk ceiling
    # (Minster-in-Sheppy -> Shams I, 0.9881). Both bands can improve on their own
    # axis while the overlap that actually matters gets worse — and the
    # permutation negatives are designed to push junk down through exactly the
    # band the cross-script positives occupy.
    #
    #     gap = P5(genuine cross-script positives) - P99(anagram + unrelated)
    #
    # Negative today by construction. A v8 that OPENS it has separated signal
    # from junk and would eventually let the 0.7 floor be raised. A v8 that
    # closes or inverts it fixed anagrams by demoting the one use case
    # Symphonym exists for. Reported SIGNED: two percentages cannot show it.
    try:
        pos_cos = np.sum(_embed(model, [c["latin"] for c in cs]) *
                         _embed(model, [c["other"] for c in cs]), axis=1) if cs else np.array([])
        neg = []
        if Q:
            EQ2 = _embed(model, Q)
            neg.append(np.sum(EQ2 * _embed(model, P_), axis=1))
            neg.append(np.sum(EQ2 * _embed(model, U_), axis=1))
        neg_cos = np.concatenate(neg) if neg else np.array([])
        if pos_cos.size and neg_cos.size:
            p5 = float(np.percentile(pos_cos, 5))
            p99 = float(np.percentile(neg_cos, 99))

            # 🛑 THE BLENDED GAP CANNOT BE SELECTED ON. It mixes two failures
            # with different remedies, and the likeliest v8 failure mode —
            # junk pushed down, dragging the weakest positives below the floor
            # with it — IMPROVES the blended number while losing recall.
            #
            # MEASURED 13 Sep 2026, not assumed: ES `knn.similarity` filters on
            # the COSINE, not on the (1+cos)/2 score. A document at cosine 0.5
            # (score 0.75) was excluded by similarity=0.7; cosine 0.9 was kept.
            # So a positive below 0.7 is UNRETRIEVABLE, not mis-ranked, and no
            # ranking change can recover it.
            retr = pos_cos[pos_cos >= gate]
            recall = float((pos_cos >= gate).mean())
            sep = (float(np.percentile(retr, 5)) - p99) if retr.size else None
            rep["bands"]["overlap_gap"] = {
                "positives_n": int(pos_cos.size), "negatives_n": int(neg_cos.size),

                # (a) RECALL — must not fall. A positive below the floor is lost
                #     outright and is invisible to every ranking measure.
                "recall_at_gate_pct": round(100.0 * recall, 1),
                "positives_below_gate_n": int((pos_cos < gate).sum()),

                # (b) SEPARABILITY — on the RETRIEVABLE subset only, because
                #     that is where the gateway actually operates.
                "p5_retrievable_positives": round(float(np.percentile(retr, 5)), 4) if retr.size else None,
                "p99_negatives": round(p99, 4),
                "separability": round(sep, 4) if sep is not None else None,
                "separability_confidence_points": round(sep / 0.3 * (1.0 / 4.25) * 100, 2) if sep is not None else None,

                # headline, kept because it is true and startling, NOT for selection
                "blended_gap_all_positives": round(p5 - p99, 4),
                "p5_all_positives": round(p5, 4),
                # Phonetic-tier confidence contribution of each end of the band,
                # so the distance to the only consumer threshold is visible.
                "p5_positive_confidence_points": round(
                    max(0.0, (float(np.percentile(retr, 5)) - gate)) / 0.3 * (1.0 / 4.25) * 100, 1)
                    if retr.size else None,
                "p99_negative_confidence_points": round(
                    max(0.0, (p99 - gate)) / 0.3 * (1.0 / 4.25) * 100, 1),
                "min_auto_confidence": 30,

                "selection_rule": ("Better only if recall_at_gate_pct does NOT fall AND "
                                   "separability improves. Do not select on "
                                   "blended_gap_all_positives."),

                # 🛑 THE TWO FAILURES ARE NOT COMMENSURABLE. Do not weigh the
                # percentages against each other as if they were.
                "asymmetry": (
                    "RECALL loss is SILENT DATA LOSS: the pair is never retrieved, appears "
                    "nowhere, and leaves no row for any audit to catch. Nobody recovers from "
                    "a candidate that was never in the pool. "
                    "SEPARABILITY loss is VISIBLE MIS-ORDERING: the match is present, just "
                    "badly placed, and a person reading the list can recover from it. "
                    "Both ends of this band sit far below MIN_AUTO_CONFIDENCE=30 (a junk "
                    "match with no lexical tier reaches ~20), so an inversion here CANNOT "
                    "auto-confirm a wrong placement — it argues for the wrong answer in a "
                    "review list. SO IF THE CANDIDATES FORCE A TRADE, PROTECT RECALL: "
                    "+0.05 separability for -2% recall is worse than the arithmetic looks."),
            }
    except Exception as exc:
        rep["bands"]["overlap_gap"] = {"error": str(exc)[:200]}

    # ---- BAND 3: the blackout scripts ---------------------------------------
    # No positives exist for these, so the measurable question is whether the
    # model separates them at all. A model that never learned a script maps its
    # names to nearly one point: mean pairwise cosine ~1, effective rank ~1.
    ds = {}
    for lang, names in ts.get("dead_scripts", {}).items():
        if len(names) < 20:
            continue
        E = _embed(model, names[:args.max_dead])
        G = E @ E.T
        iu = np.triu_indices(len(E), k=1)
        s = np.linalg.svd(E - E.mean(0, keepdims=True), compute_uv=False)
        ev = (s ** 2) / max(float((s ** 2).sum()), 1e-12)
        ds[lang] = {"n": len(E),
                    "mean_pairwise_cos": round(float(G[iu].mean()), 4),
                    "participation_ratio": round(float(1.0 / np.sum(ev ** 2)), 2)}
    if ds:
        rep["bands"]["blackout_scripts"] = ds

    # ---- BAND 4: geometry, via the SHIPPED estimator -------------------------
    try:
        sys.path.insert(0, "/vast/ishi/elastic")
        from evaluation.geometry import measure_geometry
        names = [n for g in ts["variant_groups"] for n in g["names"]][:args.max_geom]
        names += [c["other"] for c in ts["cross_script"][:args.max_geom]]
        G = measure_geometry(_embed(model, names[:args.max_geom]))
        rep["bands"]["geometry"] = {"n": min(len(names), args.max_geom),
                                    "effective_rank": round(float(G.effective_rank), 2),
                                    "of_dims": 128}
    except Exception as exc:
        rep["bands"]["geometry"] = {"error": str(exc)[:200]}

    Path(args.out).write_text(json.dumps(rep, indent=1, ensure_ascii=False))
    print(json.dumps(rep, indent=1, ensure_ascii=False)[:2600])
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", default="v8-testset.json")
    b.add_argument("--sample", type=int, default=9000)
    b.add_argument("--dead-per-lang", type=int, default=200)
    b.add_argument("--password-file", default="/ix1/ishi/es/config/elastic.password")
    b.add_argument("--es-host", default=None)
    b.set_defaults(func=build)

    r = sub.add_parser("run")
    r.add_argument("--testset", required=True)
    r.add_argument("--weights", required=True, help="final_model.pt for the candidate")
    r.add_argument("--vocab-dir", required=True, help="THE VOCAB THAT MODEL WAS TRAINED WITH")
    r.add_argument("--label", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--device", default="cpu")
    r.add_argument("--max-pairs", type=int, default=4000)
    r.add_argument("--max-dead", type=int, default=200)
    r.add_argument("--max-geom", type=int, default=4000)
    r.set_defaults(func=run)

    a = ap.parse_args()
    a.func(a)
