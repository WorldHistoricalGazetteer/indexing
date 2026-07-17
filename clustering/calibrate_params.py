# clustering/calibrate_params.py
"""
Offline calibration for the browser-side clustering scorer.

Produces the two small artefacts the gateway ships as clustering *fuel*
(``developer/plan-outstanding-2026-07.md`` §1):

* ``clustering_params.json`` — the composite-score default **weights** (name /
  spatial / temporal / type / link) plus the decision **thresholds**
  (θ_query, θ_bridge, θ_synth, θ_synth_structural, τ_name, τ_link). These seed
  `clustering.js`'s Union-Find + synthetic-edge passes and the initial θ slider
  positions.
* ``toponym_stoplist.json`` — high-frequency, low-discrimination toponyms (e.g.
  generic administrative terms shared by thousands of places) that the name
  signal (`s.n`) should down-weight so a shared common name doesn't force a
  spurious merge.

Two levels of use:

1. ``--defaults`` — write the documented **uncalibrated defaults** with **no ES
   access**. Immediately shippable so the browser has params to run against
   before an empirical calibration exists.
2. ``--calibrate`` / ``--stoplist`` — **empirical** fit against production. The
   calibration samples authority hard-links (from the batch overlay) as
   ground-truth positives and random cross-namespace pairs as negatives,
   computes the five pair signals, and fits weights + thresholds via logistic
   regression. This salvages the signal math from the retired
   ``clustering/calibration.py`` (the surrounding HDBSCAN pipeline is gone — the
   new model clusters in the browser, so only the *math* is reused).

``numpy`` is imported lazily inside the fit path (no scikit-learn dependency), so
``--defaults`` and this module's import work anywhere.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import sqlite3
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("clustering.calibrate_params")

# Output location (tracked so the defaults ship with the repo; the gateway reads
# from here). Overridable via ``CALIBRATION_OUT_DIR`` / ``--out-dir`` so an
# empirical run on a prod host can write to a scratch dir instead of dirtying the
# tracked file (then the results are committed from the dev checkout).
DATA_DIR = Path(os.getenv("CALIBRATION_OUT_DIR", Path(__file__).parent / "data"))
PARAMS_FILE = DATA_DIR / "clustering_params.json"
STOPLIST_FILE = DATA_DIR / "toponym_stoplist.json"

# The batch overlay (authority hard-links = calibration positives). Matches
# ``processing.settings.PITT_HARDLINK_DIR`` / gateway ``hard_link_expansion``.
DEFAULT_BATCH_DB = os.getenv(
    "HARD_LINK_BATCH_DB",
    f"{os.getenv('IX1_BASE', '/ix1/ishi')}/hardlinks/hard_links.sqlite")


# ---------------------------------------------------------------------------
# Documented uncalibrated defaults
# ---------------------------------------------------------------------------
# Weights sum to 1.0. Rationale: the phonetic/name signal is the strongest
# co-reference cue across gazetteers, spatial next; temporal and type are
# corroborating; the link weight is modest because hard links are usually
# applied as *forced* Union-Find merges (τ_link ≈ 1.0), not scored. Thresholds
# are conservative starting points for the θ sliders — recalibrate against prod.
DEFAULT_PARAMS = {
    "version": 0,                     # 0 = uncalibrated defaults; bumped on fit
    "calibrated": False,
    "weights": {
        "name": 0.35,                 # s.n  — Symphonym int8 cosine
        "spatial": 0.20,              # s.sp — haversine / H3 proximity
        "temporal": 0.15,             # s.t  — interval overlap
        "type": 0.15,                 # s.ty — Wu-Palmer over AAT ancestors
        "link": 0.15,                 # s.l  — hard-link edge presence
    },
    "thresholds": {
        "theta_query": 0.55,          # default θ slider — composite ≥ this merges
        "theta_bridge": 0.65,         # bridge two components via an intermediary
        "theta_synth": 0.70,          # create a synthetic edge (name+spatial pass)
        "theta_synth_structural": 0.60,  # structural shared-baseline synth edge
        "tau_name": 0.75,             # min s.n for the name signal to count
        "tau_link": 1.0,              # hard-link confidence to force-merge
    },
}


# ---------------------------------------------------------------------------
# Salvaged pair-signal math (pure; no ES / heavy deps)
# ---------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def spatial_signal(km: Optional[float], *, half_life_km: float = 25.0) -> float:
    """Map a distance to [0,1]: 1 at 0 km, 0.5 at ``half_life_km``, →0 far away.

    None (a point missing on either side) → 0.0 (no spatial evidence)."""
    if km is None:
        return 0.0
    return 1.0 / (1.0 + km / half_life_km)


def temporal_overlap(a: Optional[list[int]], b: Optional[list[int]]) -> float:
    """Interval-overlap signal in [0,1] (Jaccard over year ranges).

    ``[start, end]`` inclusive. Either side undated (None) → 0.0. Two identical
    single-year points → 1.0; disjoint → 0.0."""
    if not a or not b:
        return 0.0
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    if hi < lo:
        return 0.0
    inter = hi - lo
    union = max(a[1], b[1]) - min(a[0], b[0])
    if union <= 0:
        return 1.0  # both the same single year
    return inter / union


def wu_palmer(path_a: str, path_b: str) -> float:
    """Wu-Palmer similarity between two materialised AAT paths (dot-separated
    root→leaf ids, e.g. ``"300264550.300008346.300132315"``).

    ``2·depth(LCA) / (depth(a) + depth(b))``. Depth is the node's 1-based
    position along its path; the LCA is the longest shared prefix."""
    ca = path_a.split(".")
    cb = path_b.split(".")
    lca = 0
    for x, y in zip(ca, cb):
        if x == y:
            lca += 1
        else:
            break
    da, db = len(ca), len(cb)
    if da + db == 0:
        return 0.0
    return (2.0 * lca) / (da + db)


def type_signal(paths_a: list[str], paths_b: list[str]) -> float:
    """Best Wu-Palmer over every AAT-path pair between two places (0 if either
    side has no AAT paths)."""
    if not paths_a or not paths_b:
        return 0.0
    return max(wu_palmer(pa, pb) for pa in paths_a for pb in paths_b)


def cosine_byte(a: list[int], b: list[int]) -> float:
    """Cosine similarity between two int8-quantised embeddings (pure-Python)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Artefact writers
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: dict, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)  # atomic
    logger.info("wrote %s → %s", label, path)


def write_defaults() -> dict:
    """Write the uncalibrated default params (no ES needed). Returns them."""
    _write_json(PARAMS_FILE, DEFAULT_PARAMS, label="default clustering_params")
    return DEFAULT_PARAMS


# ---------------------------------------------------------------------------
# Stoplist (ES aggregation — high-frequency toponyms)
# ---------------------------------------------------------------------------


def build_stoplist(es_host: str, *, top_k: int = 500, min_places: int = 50,
                   auth: tuple[str, str] | None = None) -> list[str]:
    """Build the toponym stoplist: the ``top_k`` name forms shared by the most
    places.

    The ``toponyms`` index is **deduplicated** (one doc per ``name@lang``), so a
    plain ``doc_count`` measures language variants, not how many *places* share a
    name. The discriminating signal is the length of each doc's ``attestations``
    list — so we order a ``name.keyword`` terms aggregation by the **sum of
    ``attestations`` size** (a script metric), keeping names attested by ≥
    ``min_places`` places. Returns the list and writes ``toponym_stoplist.json``.
    """
    import httpx  # local import so --defaults never needs it

    body = {
        "size": 0,
        "aggs": {
            "common_names": {
                "terms": {"field": "name.keyword", "size": top_k,
                          "shard_size": top_k * 4,  # accuracy for sub-metric order
                          "order": {"attested": "desc"}},
                "aggs": {"attested": {"sum": {
                    "script": {"source": "doc['attestations'].size()"}}}},
            }
        },
    }
    url = f"{es_host.rstrip('/')}/toponyms_*/_search"
    try:
        resp = httpx.post(url, json=body, auth=auth, timeout=300)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("stoplist aggregation failed: %s", exc)
        raise

    buckets = resp.json().get("aggregations", {}).get("common_names", {}).get("buckets", [])
    stop = [b["key"] for b in buckets
            if b.get("attested", {}).get("value", 0) >= min_places][:top_k]
    _write_json(STOPLIST_FILE,
                {"version": 1, "top_k": top_k, "min_places": min_places,
                 "stoplist": stop},
                label=f"toponym_stoplist ({len(stop)} names)")
    return stop


# ---------------------------------------------------------------------------
# Calibration (empirical fit — positives from overlay, negatives random)
# ---------------------------------------------------------------------------


# Positive-source SQL. ``authority`` = cross-gazetteer authority co-references
# (coordinate near-duplicates → spatially separable, the class that made earlier
# fits spatial-heavy). ``contributor`` = user-reconciliation hard links (the
# legacy v3.2 links harvested into the overlay) — these link a contributed record
# to authority records at *different* coordinates, so they are the name-forward,
# cross-coordinate positives the browser's broader task actually resembles. Their
# reliability was the original deferral reason; run with ``contributor`` on the
# assumption they are reliable (re-run for fine-tuning as more accumulate).
_POSITIVE_SQL = {
    "authority": ("relation_type IN ('sameAs','exactMatch') "
                  "AND source_category='authority'"),
    "contributor": "source_category='contributor'",
    "both": ("(source_category='contributor') OR "
             "(relation_type IN ('sameAs','exactMatch') "
             "AND source_category='authority')"),
}


def _load_positive_pairs(batch_db: Path, *, limit: int, rng: random.Random,
                         source: str = "authority") -> list[tuple[str, str]]:
    """Sample ground-truth positive pairs from the batch overlay.

    ``source`` selects the positive class — see ``_POSITIVE_SQL``. Pairs whose
    endpoints are not in the live index are dropped downstream by
    ``build_feature_matrix`` (missing-endpoint skip), so a dangling-heavy source
    (e.g. legacy contributor links to un-ingested whg datasets) still yields a
    clean, in-index subset.
    """
    if not batch_db.exists():
        raise FileNotFoundError(f"batch overlay not found: {batch_db}")
    where = _POSITIVE_SQL.get(source)
    if where is None:
        raise ValueError(f"unknown positives source {source!r}; "
                         f"pick one of {sorted(_POSITIVE_SQL)}")
    conn = sqlite3.connect(f"file:{batch_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            f"SELECT place_a, place_b FROM hard_link_assertions WHERE {where}"
        ).fetchall()
    finally:
        conn.close()
    rng.shuffle(rows)
    return rows[:limit]


def calibrate(es_host: str, *, batch_db: Path = None, sample: int = 20_000,
              seed: int = 13, auth: tuple[str, str] | None = None,
              positives_source: str = "authority") -> dict:
    """Empirically fit weights + thresholds and write ``clustering_params.json``.

    Positives = authority hard-links (overlay); negatives = random
    cross-namespace pairs. Signals computed with the salvaged math above; a
    numpy-only logistic regression yields normalised weights, and the operating
    thresholds are read off the fitted score distribution. Requires ``numpy``.

    **Only the four *inferred* signals — name / spatial / temporal / type — are
    fitted.** The link signal (``s.l``) is deliberately excluded: the positives
    *are* hard links, so including link presence as a feature is circular (it
    trivially separates the classes and steals all the weight). Hard links are
    applied as *forced* Union-Find merges at ``τ_link`` in the browser, not
    weighted-scored — so the fixed default link weight is retained and the four
    fitted weights are scaled to fill the remaining ``1 − w_link``.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "calibrate needs numpy; install it or use --defaults for the "
            f"uncalibrated params ({exc})")

    from .signal_features import build_feature_matrix  # heavy ES joins, lazy

    rng = random.Random(seed)
    batch_db = batch_db or Path(DEFAULT_BATCH_DB)
    positives = _load_positive_pairs(batch_db, limit=sample, rng=rng,
                                     source=positives_source)
    logger.info("sampled %d positive pairs (source=%s)", len(positives),
                positives_source)

    # X columns are the four inferred signals in this fixed order (NO link).
    X, y = build_feature_matrix(es_host, positives, sample, rng, auth=auth,
                                batch_db=batch_db)
    if len(y) < 1000 or sum(y) < 200:
        raise SystemExit(f"insufficient calibration data (n={len(y)}, pos={sum(y)})")

    coef, proba = _fit_logistic(X, y)         # numpy-only; no sklearn dep
    raw = np.abs(coef)
    if raw.sum() == 0:
        raise SystemExit("degenerate fit (all-zero coefficients)")
    inferred = ["name", "spatial", "temporal", "type"]
    w_link = DEFAULT_PARAMS["weights"]["link"]
    scaled = (raw / raw.sum()) * (1.0 - w_link)  # fill the non-link budget
    weights = {k: float(w) for k, w in zip(inferred, scaled)}
    weights["link"] = w_link

    # Operating point on the COMPOSITE scale the browser actually thresholds —
    # Σ w_i·s_i over the four inferred signals (range [0, 1−w_link]), NOT the
    # logistic probability. Youden's-J-optimal cut on that composite.
    composite = np.asarray(X, dtype=float) @ scaled
    theta_query = _best_threshold(list(composite), y)

    params = json.loads(json.dumps(DEFAULT_PARAMS))  # deep copy of the shape
    params.update({"version": DEFAULT_PARAMS["version"] + 1, "calibrated": True,
                   "n_pairs": len(y), "n_positive": int(sum(y)),
                   "positives_source": positives_source})
    params["weights"] = weights
    params["thresholds"]["theta_query"] = round(theta_query, 3)
    # Keep the synthetic/bridge thresholds relative to the fitted operating point.
    params["thresholds"]["theta_bridge"] = round(min(0.99, theta_query + 0.10), 3)
    params["thresholds"]["theta_synth"] = round(min(0.99, theta_query + 0.15), 3)
    _write_json(PARAMS_FILE, params, label="calibrated clustering_params")
    return params


def _fit_logistic(X, y, *, l2: float = 1.0, lr: float = 0.5, iters: int = 3000):
    """Minimal numpy-only logistic regression (avoids a scikit-learn dependency).

    Batch gradient descent on the L2-regularised logistic loss with an
    (unpenalised) intercept. Feature columns are the four inferred signals,
    already on a comparable [0,1] scale, so no standardisation is needed.
    Returns ``(coef[4], predict_proba(Xn)->prob_of_1)``."""
    import numpy as np
    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float)
    n, d = Xa.shape
    Xb = np.hstack([np.ones((n, 1)), Xa])   # prepend intercept column
    w = np.zeros(d + 1)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(Xb @ w)))
        reg = np.concatenate([[0.0], w[1:]])  # don't penalise the intercept
        w -= lr * (Xb.T @ (p - ya) / n + l2 * reg / n)

    def proba(Xn):
        Xn = np.asarray(Xn, dtype=float)
        Xnb = np.hstack([np.ones((len(Xn), 1)), Xn])
        return 1.0 / (1.0 + np.exp(-(Xnb @ w)))

    return w[1:], proba


def _best_threshold(scores, y) -> float:
    """Youden's-J optimal threshold over candidate cut points."""
    best_t, best_j = 0.5, -1.0
    pos = sum(y)
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        return 0.5
    for t in [i / 100 for i in range(1, 100)]:
        tp = sum(1 for s, label in zip(scores, y) if s >= t and label == 1)
        fp = sum(1 for s, label in zip(scores, y) if s >= t and label == 0)
        j = (tp / pos) - (fp / neg)
        if j > best_j:
            best_j, best_t = j, t
    return best_t


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _auth_from_env() -> tuple[str, str] | None:
    pw_file = os.getenv("ELASTIC_PASS_FILE",
                        f"{os.getenv('IX1_BASE', '/ix1/ishi')}/es/config/elastic.password")
    try:
        return ("elastic", Path(pw_file).read_text().strip())
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--defaults", action="store_true",
                   help="write uncalibrated default params (no ES needed)")
    p.add_argument("--stoplist", action="store_true",
                   help="build the toponym stoplist (needs --es-host)")
    p.add_argument("--calibrate", action="store_true",
                   help="empirically fit params (needs --es-host, numpy, sklearn)")
    p.add_argument("--es-host", default=os.getenv("PROD_ES_URL", "http://localhost:9201"))
    p.add_argument("--batch-db", default=DEFAULT_BATCH_DB)
    p.add_argument("--sample", type=int, default=20_000)
    p.add_argument("--positives", choices=("authority", "contributor", "both"),
                   default="authority",
                   help="ground-truth positive class for --calibrate "
                        "(default: authority co-references)")
    p.add_argument("--top-k", type=int, default=500)
    p.add_argument("--out-dir", default=None,
                   help="write artefacts here instead of clustering/data/ "
                        "(keeps a prod run from dirtying the tracked file)")
    args = p.parse_args(argv)

    if not (args.defaults or args.stoplist or args.calibrate):
        p.error("pick at least one of --defaults / --stoplist / --calibrate")

    if args.out_dir:
        # Rebind the module-level output paths for this run.
        global DATA_DIR, PARAMS_FILE, STOPLIST_FILE
        DATA_DIR = Path(args.out_dir)
        PARAMS_FILE = DATA_DIR / "clustering_params.json"
        STOPLIST_FILE = DATA_DIR / "toponym_stoplist.json"

    auth = _auth_from_env()
    if args.defaults:
        write_defaults()
    if args.stoplist:
        build_stoplist(args.es_host, top_k=args.top_k, auth=auth)
    if args.calibrate:
        calibrate(args.es_host, batch_db=Path(args.batch_db),
                  sample=args.sample, auth=auth,
                  positives_source=args.positives)
    return 0


if __name__ == "__main__":
    sys.exit(main())
