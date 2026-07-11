# clustering/signal_features.py
"""
Feature-matrix builder for the offline clustering calibration
(``clustering.calibrate_params.calibrate``).

Given ground-truth positive pairs (authority hard-links) and a target sample
size, this fetches the per-place data needed to compute the **four inferred**
pair signals and returns ``(X, y)``:

* ``X`` — one row per pair, columns ``[name, spatial, temporal, type]`` (the
  link signal is *excluded* — see ``calibrate`` docstring for why).
* ``y`` — 1 for positives, 0 for random cross-namespace negatives.

Per place we need: a representative Symphonym embedding (from the ``toponyms``
index), a representative point, the AAT paths, and the temporal range (from the
``places`` index). All fetched in bulk via ``terms`` lookups. Negatives are
random cross-namespace place pairs drawn from a random-scored ``places`` sample,
which are almost never co-referent — a good negative class for this task.

Kept separate from ``calibrate_params`` so the pure math + default/stoplist
paths never import ``httpx`` or touch ES.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

import httpx

from .calibrate_params import (
    haversine_km, spatial_signal, temporal_overlap, type_signal, cosine_byte,
)

logger = logging.getLogger("clustering.signal_features")

_CHUNK = 1000


def _post(es_host: str, index: str, body: dict, auth) -> dict:
    resp = httpx.post(f"{es_host.rstrip('/')}/{index}/_search",
                      json=body, auth=auth, timeout=120)
    resp.raise_for_status()
    return resp.json()


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
            point = None
            for g in src.get("geometries", []) or []:
                rp = g.get("repr_point")
                if isinstance(rp, dict):
                    point = (rp.get("lat", 0), rp.get("lon", 0)); break
                if isinstance(rp, (list, tuple)) and len(rp) == 2:
                    point = (rp[1], rp[0]); break
            out[pid] = {
                "namespace": src.get("namespace", ""),
                "point": point,
                "aat_paths": fields["aat_paths"],
                "temporal_range": fields["temporal_range"],
            }
    return out


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


def _random_place_ids(es_host: str, n: int, rng: random.Random, auth) -> list[str]:
    """A random sample of place_ids (random_score) for negative-pair drawing."""
    body = {
        "size": n,
        "query": {"function_score": {"query": {"match_all": {}},
                                     "random_score": {"seed": rng.randint(1, 10**9),
                                                      "field": "_seq_no"}}},
        "_source": ["place_id", "namespace"],
    }
    hits = _post(es_host, "places_*", body, auth)["hits"]["hits"]
    return [h["_source"]["place_id"] for h in hits]


def build_feature_matrix(es_host: str, positives: list[tuple[str, str]],
                         sample: int, rng: random.Random, *, auth=None):
    """Return ``(X, y)`` — 4-column inferred-signal features + labels."""
    # Negatives: random cross-namespace pairs (rarely co-referent).
    pool = _random_place_ids(es_host, min(sample * 2, 10000), rng, auth)
    negatives: list[tuple[str, str]] = []
    tries = 0
    while len(negatives) < len(positives) and tries < len(pool) * 4 and len(pool) > 1:
        a, b = rng.choice(pool), rng.choice(pool)
        tries += 1
        if a >= b:
            continue
        if a.split(":")[0] == b.split(":")[0]:  # cross-namespace only
            continue
        negatives.append((a, b))

    pairs = [(p, 1) for p in positives] + [(n, 0) for n in negatives]
    all_pids = sorted({pid for (a, b), _ in pairs for pid in (a, b)})
    logger.info("fetching data for %d places (%d pairs)", len(all_pids), len(pairs))

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
        s_sp = spatial_signal(km)
        s_t = temporal_overlap(da["temporal_range"], db["temporal_range"])
        s_ty = type_signal(da["aat_paths"], db["aat_paths"])
        X.append([s_name, s_sp, s_t, s_ty])
        y.append(label)
    return X, y
