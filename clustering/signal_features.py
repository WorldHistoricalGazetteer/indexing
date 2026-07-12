# clustering/signal_features.py
"""
Feature-matrix builder for the offline clustering calibration
(``clustering.calibrate_params.calibrate``).

Given ground-truth positive pairs (authority hard-links) and a target sample
size, this fetches the per-place data needed to compute the **four inferred**
pair signals and returns ``(X, y)``:

* ``X`` — one row per pair, columns ``[name, spatial, temporal, type]`` (the
  link signal is *excluded* — see ``calibrate`` docstring for why).
* ``y`` — 1 for positives, 0 for negatives.

Negative sampling — a **balanced mix of hard + random** (this is the crux of a
trustworthy fit)
------------------------------------------------------------------------------
Random cross-namespace negatives are almost always geographically scattered, so
a classifier can separate them from positives on *spatial alone* — which inflates
the spatial weight and starves name/type. So the negatives are a **mix**:

* **nearby** — a place within a few km of a seed place but not co-referent. These
  are spatially close yet negative, so spatial is *not* sufficient → the fit must
  lean on name/type to separate them (deflates the spatial over-weight).
* **same-name** — two places sharing a toponym form but not co-referent (two
  distinct "Springfield"s). Name is *not* sufficient → the fit must lean on
  spatial/type (calibrates the name weight against real confusion).
* **random** — cross-namespace random pairs (the easy baseline class).

Every candidate negative is de-duplicated against the **full batch overlay** (not
just the sampled positives) so a genuine but unsampled hard-link is never
mislabelled as a negative. Residual label noise (a co-reference missing from the
overlay entirely) is tolerated — it only weakens, never inverts, the signal.

Per place we fetch a representative Symphonym embedding (``toponyms`` index), a
representative point, the AAT paths, and the temporal range (``places`` index),
all in bulk via ``terms`` lookups.

Kept separate from ``calibrate_params`` so the pure math + default/stoplist paths
never import ``httpx`` or touch ES.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

import httpx

from .calibrate_params import (
    haversine_km, spatial_signal, temporal_overlap, type_signal, cosine_byte,
)

logger = logging.getLogger("clustering.signal_features")

_CHUNK = 1000

# Negative-mix targets (fractions of the negative budget). Any shortfall in a
# hard category is topped up from random, so these are aspirational maxima.
_MIX_RANDOM = 0.34
_MIX_SAME_NAME = 0.33
_MIX_NEARBY = 0.33

# "Nearby" radius for the spatial hard negatives. Small enough that the pair is
# genuinely close, large enough to find a neighbour for most seeds.
_NEARBY_KM = 3.0


def _post(es_host: str, index: str, body: dict, auth) -> dict:
    resp = httpx.post(f"{es_host.rstrip('/')}/{index}/_search",
                      json=body, auth=auth, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _msearch(es_host: str, index: str, lines: list[dict], auth) -> list[dict]:
    """Batched _msearch. ``lines`` is a flat list alternating header/body dicts."""
    import json as _json
    ndjson = "\n".join(_json.dumps(x) for x in lines) + "\n"
    resp = httpx.post(f"{es_host.rstrip('/')}/{index}/_msearch",
                      content=ndjson,
                      headers={"Content-Type": "application/x-ndjson"},
                      auth=auth, timeout=180)
    resp.raise_for_status()
    return resp.json().get("responses", [])


def _canon(a: str, b: str) -> tuple[str, str] | None:
    """Canonical-order a cross-namespace pair (a<b); None if same place/namespace."""
    if not a or not b or a == b:
        return None
    if a.split(":")[0] == b.split(":")[0]:  # cross-namespace only
        return None
    return (a, b) if a < b else (b, a)


# ---------------------------------------------------------------------------
# Per-place data (points, aat_paths, temporal_range) + embeddings
# ---------------------------------------------------------------------------


def _fetch_place_data(es_host: str, pids: list[str], auth) -> dict[str, dict]:
    """Bulk-fetch ``{place_id: {namespace, point, aat_paths, temporal_range}}``."""
    from gateway.clustering_payload import assemble_clustering_fields
    out: dict[str, dict] = {}
    for i in range(0, len(pids), _CHUNK):
        chunk = pids[i:i + _CHUNK]
        body = {
            "size": len(chunk),
            "query": {"terms": {"place_id": chunk}},
            "_source": ["place_id", "namespace", "geometries.repr_point",
                        "geometries.h3_centroid", "geometries.h3_cover",
                        "geometries.timespans", "types"],
        }
        for hit in _post(es_host, "places_*", body, auth)["hits"]["hits"]:
            src = hit["_source"]
            pid = src.get("place_id", "")
            fields = assemble_clustering_fields(src)  # reuse the gateway derivation
            out[pid] = {
                "namespace": src.get("namespace", ""),
                "point": _repr_point(src),
                "aat_paths": fields["aat_paths"],
                "temporal_range": fields["temporal_range"],
            }
    return out


def _repr_point(src: dict) -> tuple[float, float] | None:
    """``(lat, lon)`` from the first geometry's repr_point, or None."""
    for g in src.get("geometries", []) or []:
        rp = g.get("repr_point")
        if isinstance(rp, dict):
            return (rp.get("lat", 0), rp.get("lon", 0))
        if isinstance(rp, (list, tuple)) and len(rp) == 2:
            return (rp[1], rp[0])  # ES [lon,lat] → (lat,lon)
    return None


def _fetch_embeddings(es_host: str, pids: list[str], auth) -> dict[str, list[int]]:
    """One representative int8 embedding per place (its first attested toponym)."""
    out: dict[str, list[int]] = {}
    for i in range(0, len(pids), _CHUNK):
        chunk = pids[i:i + _CHUNK]
        body = {
            "size": len(chunk) * 4,
            "query": {"terms": {"attestations": chunk}},
            "_source": ["attestations", "embedding"],
        }
        for hit in _post(es_host, "toponyms_*", body, auth)["hits"]["hits"]:
            src = hit["_source"]
            emb = src.get("embedding")
            if not emb:
                continue
            for pid in src.get("attestations", []):
                out.setdefault(pid, emb)  # first one wins (representative)
    return out


# ---------------------------------------------------------------------------
# Negative-pair generators
# ---------------------------------------------------------------------------


def _random_place_ids(es_host: str, n: int, rng: random.Random, auth) -> list[str]:
    """A random sample of place_ids (random_score) for negative-pair drawing."""
    body = {
        "size": n,
        "query": {"function_score": {"query": {"match_all": {}},
                                     "random_score": {"seed": rng.randint(1, 10**9),
                                                      "field": "_seq_no"}}},
        "_source": ["place_id"],
    }
    hits = _post(es_host, "places_*", body, auth)["hits"]["hits"]
    return [h["_source"]["place_id"] for h in hits]


def _random_negatives(pool: list[str], n: int, rng: random.Random) -> list[tuple]:
    out, seen, tries = [], set(), 0
    while len(out) < n and tries < n * 20 and len(pool) > 1:
        tries += 1
        pair = _canon(rng.choice(pool), rng.choice(pool))
        if pair and pair not in seen:
            seen.add(pair); out.append(pair)
    return out


def _same_name_negatives(es_host: str, n: int, rng: random.Random, auth) -> list[tuple]:
    """Cross-namespace pairs of places that share a toponym form.

    Draw high/mid-frequency names (terms agg by summed attestations), fetch their
    toponym docs, and pair two attestations from *different* namespaces within a
    doc. Same name, (usually) different place → a hard negative for the name
    signal."""
    agg = {
        "size": 0,
        "aggs": {"names": {
            "terms": {"field": "name.keyword", "size": 4000, "shard_size": 8000,
                      "order": {"att": "desc"}},
            "aggs": {"att": {"sum": {"script": {"source": "doc['attestations'].size()"}}}},
        }},
    }
    buckets = _post(es_host, "toponyms_*", agg, auth)["aggregations"]["names"]["buckets"]
    names = [b["key"] for b in buckets]
    rng.shuffle(names)

    out, seen = [], set()
    for i in range(0, len(names), 500):
        if len(out) >= n:
            break
        chunk = names[i:i + 500]
        body = {"size": len(chunk) * 4,
                "query": {"terms": {"name.keyword": chunk}},
                "_source": ["attestations"]}
        for hit in _post(es_host, "toponyms_*", body, auth)["hits"]["hits"]:
            atts = [a for a in hit["_source"].get("attestations", []) if a]
            if len(atts) < 2:
                continue
            rng.shuffle(atts)
            # a few cross-namespace pairs per shared name
            for j in range(min(3, len(atts) - 1)):
                pair = _canon(atts[j], atts[j + 1])
                if pair and pair not in seen:
                    seen.add(pair); out.append(pair)
                    if len(out) >= n:
                        break
    return out


def _nearby_negatives(es_host: str, seeds: list[str], n: int,
                      rng: random.Random, auth) -> list[tuple]:
    """For each seed place, a cross-namespace place within ``_NEARBY_KM`` km.

    Spatially close but (after overlay de-dup) not co-referent → a hard negative
    for the spatial signal. Batched via _msearch over the seeds' repr_points."""
    seed_data = _fetch_place_data(es_host, seeds, auth)
    seeds_with_pt = [(p, d["point"]) for p, d in seed_data.items() if d["point"]]
    rng.shuffle(seeds_with_pt)

    out, seen = [], set()
    for i in range(0, len(seeds_with_pt), 200):
        if len(out) >= n:
            break
        batch = seeds_with_pt[i:i + 200]
        lines: list[dict] = []
        for _pid, (lat, lon) in batch:
            lines.append({})
            lines.append({
                "size": 6, "_source": ["place_id"],
                "query": {"nested": {"path": "geometries", "query": {"geo_distance": {
                    "distance": f"{_NEARBY_KM}km",
                    "geometries.repr_point": {"lat": lat, "lon": lon}}}}},
            })
        for (seed_pid, _pt), resp in zip(batch, _msearch(es_host, "places_*", lines, auth)):
            for h in resp.get("hits", {}).get("hits", []):
                pair = _canon(seed_pid, h["_source"]["place_id"])
                if pair and pair not in seen:
                    seen.add(pair); out.append(pair)
                    break  # one neighbour per seed
            if len(out) >= n:
                break
    return out


# ---------------------------------------------------------------------------
# Overlay de-dup (drop true links from the negative set)
# ---------------------------------------------------------------------------


def _overlay_links(batch_db: Path, pairs: list[tuple]) -> set[tuple]:
    """Return the subset of ``pairs`` that ARE present in the batch overlay
    (any relation) — these must be dropped from the negative set."""
    import sqlite3
    if not batch_db or not Path(batch_db).exists() or not pairs:
        return set()
    conn = sqlite3.connect(f"file:{batch_db}?mode=ro", uri=True)
    found: set[tuple] = set()
    try:
        # Chunk to stay under the SQLite bind-variable limit (2 vars/pair).
        for i in range(0, len(pairs), 400):
            chunk = pairs[i:i + 400]
            clause = " OR ".join(["(place_a=? AND place_b=?)"] * len(chunk))
            args = [v for pair in chunk for v in pair]
            for row in conn.execute(
                    f"SELECT place_a, place_b FROM hard_link_assertions WHERE {clause}",
                    args):
                found.add((row[0], row[1]))
    finally:
        conn.close()
    return found


# ---------------------------------------------------------------------------
# Pure assembler (unit-tested)
# ---------------------------------------------------------------------------


def assemble_negatives(random_pairs, same_name_pairs, nearby_pairs, *,
                       target: int, exclude: set, rng: random.Random) -> list[tuple]:
    """Merge the three negative sources into a deduped set of ``target`` pairs.

    Hard negatives (same-name, nearby) are taken first up to their mix caps; the
    remainder is filled from random. ``exclude`` (positives + overlay links) is
    dropped from every source. Pure — no ES/IO — so it is unit-testable."""
    seen = set(exclude)
    out: list[tuple] = []

    def take(pairs, cap):
        for p in pairs:
            if len(out) >= target or (cap is not None and cap <= 0):
                break
            if p in seen:
                continue
            seen.add(p); out.append(p)
            if cap is not None:
                cap -= 1

    take(list(same_name_pairs), int(target * _MIX_SAME_NAME))
    take(list(nearby_pairs), int(target * _MIX_NEARBY))
    # Fill the rest (random budget + any hard shortfall) from random, then any
    # leftover hard pairs so we hit the target when random underdelivers.
    leftover = list(random_pairs) + list(same_name_pairs) + list(nearby_pairs)
    rng.shuffle(random_pairs)
    take(random_pairs, None)
    take(leftover, None)
    return out[:target]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_feature_matrix(es_host: str, positives: list[tuple[str, str]],
                         sample: int, rng: random.Random, *, auth=None,
                         batch_db: Path = None):
    """Return ``(X, y)`` — 4-column inferred-signal features + labels, using a
    balanced hard/random negative mix."""
    n_neg = len(positives)
    exclude = {tuple(p) for p in positives}

    # Draw more candidates than needed per source (dedup + overlay drop shrink them).
    pool = _random_place_ids(es_host, min(sample * 2, 10000), rng, auth)
    rand_neg = _random_negatives(pool, int(n_neg * (_MIX_RANDOM + 0.3)), rng)
    try:
        same_neg = _same_name_negatives(es_host, int(n_neg * (_MIX_SAME_NAME + 0.15)), rng, auth)
    except Exception as exc:
        logger.warning("same-name negatives failed (%s); topping up from random", exc)
        same_neg = []
    try:
        nearby_neg = _nearby_negatives(es_host, pool[:min(len(pool), 3000)],
                                       int(n_neg * (_MIX_NEARBY + 0.15)), rng, auth)
    except Exception as exc:
        logger.warning("nearby negatives failed (%s); topping up from random", exc)
        nearby_neg = []

    # Drop any candidate negative that is actually a link in the overlay.
    all_candidates = list({*rand_neg, *same_neg, *nearby_neg})
    linked = _overlay_links(batch_db, all_candidates) if batch_db else set()
    exclude |= linked
    logger.info("negatives: random=%d same_name=%d nearby=%d (dropped %d overlay links)",
                len(rand_neg), len(same_neg), len(nearby_neg), len(linked))

    negatives = assemble_negatives(rand_neg, same_neg, nearby_neg,
                                   target=n_neg, exclude=exclude, rng=rng)

    pairs = [(p, 1) for p in positives] + [(np, 0) for np in negatives]
    all_pids = sorted({pid for (a, b), _ in pairs for pid in (a, b)})
    logger.info("fetching data for %d places (%d pairs: %d pos / %d neg)",
                len(all_pids), len(pairs), len(positives), len(negatives))

    pdata = _fetch_place_data(es_host, all_pids, auth)
    embs = _fetch_embeddings(es_host, all_pids, auth)

    X, y = [], []
    for (a, b), label in pairs:
        da, db = pdata.get(a), pdata.get(b)
        if not da or not db:
            continue
        ea, eb = embs.get(a), embs.get(b)
        s_name = cosine_byte(ea, eb) if ea and eb else 0.0
        km = (haversine_km(da["point"][0], da["point"][1],
                           db["point"][0], db["point"][1])
              if da["point"] and db["point"] else None)
        X.append([s_name, spatial_signal(km),
                  temporal_overlap(da["temporal_range"], db["temporal_range"]),
                  type_signal(da["aat_paths"], db["aat_paths"])])
        y.append(label)
    return X, y
