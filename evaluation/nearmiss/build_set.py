"""Build the frozen near-miss evaluation set from the LIVE index (read-only). Run on the Pitt host.

    PYTHONPATH=/vast/ishi/elastic python -m evaluation.nearmiss.build_set --out evaluation/nearmiss/data

WHAT THE SET IS. Real populated places (GeoNames, population 200 to 3M, a genealogy-weighted country
mix), each with its real name replaced by a realistic mis-spelling (errors.py), plus nonsense strings
for which NO right answer exists. Ground truth is the place the name came from, and every place that
bears that exact name (a namesake is not a miss).

WHAT IT IS NOT. It is not real genealogy data and is not labelled by a person. It stands in for the
TreeAlive-style labelled set until that exists, and it is a TUNING AID: improvements measured on it must
be confirmed on the labelled set before being claimed. The error model's weights are a judgement.

FROZEN. Seeds and quotas are fixed and recorded in the manifest, together with the index document counts at
build time, so a re-run on the same index reproduces the same items; truth ids are only valid for that index.
Items are split 50/50 into ``dev`` (tune against it) and ``test`` (look at it rarely, at the end).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

import httpx

from evaluation.nearmiss import errors as E

SEED = 20261113
LATIN = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿĀ-ſ][A-Za-zÀ-ÖØ-öø-ÿĀ-ſ'’.\- ]{2,22}[A-Za-zÀ-ÖØ-öø-ÿĀ-ſ]$")
# country -> share of misspelling items. A judgement about where family-history data comes from.
QUOTAS = {"US": .20, "GB": .12, "DE": .08, "IE": .06, "FR": .06, "IT": .05, "CA": .04, "AU": .03, "NL": .03,
          "PL": .03, "SE": .02, "NO": .02, "DK": .02}
OTHER_SHARE = round(1 - sum(QUOTAS.values()), 2)      # everything else, one random draw
N_MISSPELLINGS = 1200
N_NONSENSE = 120
# TARGETED strata: classes that real genealogy data is full of but a random sample barely contains.
# Reported separately from the main set and never mixed into its headline figures.
DIACRITIC_COUNTRIES = ["DE", "FR", "PL", "SE", "NO", "DK", "IT", "ES", "PT", "CZ", "HU", "TR", "FI"]
N_DIACRITIC_PER_COUNTRY = 8
ABBREV_PREFIXES = ["saint", "st", "mount", "fort"]
N_ABBREV = 60
MAX_TRUTH = 200


def split_of(key: str) -> str:
    return "dev" if int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) % 2 == 0 else "test"


class ES:
    def __init__(self):
        sys.path.insert(0, "/vast/ishi/elastic")
        from gateway.es_helpers import es_auth, ES_HEADERS
        from gateway.config import ES_BACKEND, TOPONYMS_INDEX, PLACES_INDEX
        self.base, self.auth, self.h = ES_BACKEND, es_auth(), ES_HEADERS
        self.top, self.places = TOPONYMS_INDEX, PLACES_INDEX
        self.cl = httpx.Client(timeout=180)

    def post(self, path, body):
        r = self.cl.post(self.base + path, json=body, auth=self.auth, headers=self.h)
        r.raise_for_status()
        return r.json()

    def msearch(self, index, bodies):
        nd = "".join(json.dumps({"index": index}) + "\n" + json.dumps(b) + "\n" for b in bodies)
        r = self.cl.post(self.base + "/_msearch", content=nd, auth=self.auth,
                         headers={**self.h, "Content-Type": "application/x-ndjson"})
        r.raise_for_status()
        return r.json()["responses"]


def sample_places(es: ES, ccode: str | None, want: int, seed: int, extra_filter=None, pool_mult: int = 4) -> list[dict]:
    flt = [{"term": {"namespace": "gn"}}, {"range": {"population": {"gte": 200, "lte": 3_000_000}}}]
    if extra_filter:
        flt.append(extra_filter)
    must_not = []
    if ccode:
        flt.append({"term": {"ccodes": ccode}})
    else:
        must_not.append({"terms": {"ccodes": list(QUOTAS)}})
    body = {"size": want * pool_mult, "track_total_hits": False, "_source": ["place_id", "title", "ccodes", "population"],
            "query": {"function_score": {"query": {"bool": {"filter": flt, "must_not": must_not}},
                                         "random_score": {"seed": seed, "field": "_seq_no"}}}}
    out = []
    for h in es.post(f"/{es.places}/_search", body)["hits"]["hits"]:
        s = h["_source"]
        t = (s.get("title") or "").strip()
        if LATIN.match(t) and not re.search(r"\d", t):
            out.append({"place_id": s["place_id"], "original": t, "ccode": (s.get("ccodes") or [None])[0],
                        "population": s.get("population")})
    return out[:want]


def truths(es: ES, names: list[str]) -> list[list[str]]:
    res = es.msearch(es.top, [{"size": MAX_TRUTH, "_source": ["attestations"], "track_total_hits": False,
                                "query": {"terms": {"name.raw": [n.lower()]}}} for n in names])
    return [sorted({p for h in r.get("hits", {}).get("hits", []) for p in (h["_source"].get("attestations") or [])})
            for r in res]


def country_truths(es: ES, truth_lists: list[list[str]], ccodes: list[str | None]) -> list[list[str]]:
    """Restrict each truth list to places in the item's country: the headline reading of 'right'."""
    bodies, idx = [], []
    for n, (ids, cc) in enumerate(zip(truth_lists, ccodes)):
        if cc and ids:
            bodies.append({"size": min(len(ids), 400), "_source": ["place_id"], "track_total_hits": False,
                           "query": {"bool": {"filter": [{"terms": {"place_id": ids[:400]}}, {"term": {"ccodes": cc}}]}}})
            idx.append(n)
    out = [list(ids) for ids in truth_lists]       # no country known: fall back to the looser truth
    for n, r in zip(idx, es.msearch(es.places, bodies)):
        out[n] = sorted({h["_source"]["place_id"] for h in r.get("hits", {}).get("hits", [])})
    return out


def exact_exists(es: ES, names: list[str]) -> list[bool]:
    res = es.msearch(es.top, [{"size": 0, "track_total_hits": 1, "query": {"terms": {"name.raw": [n.lower()]}}}
                              for n in names])
    return [(r.get("hits", {}).get("total", {}).get("value", 0) or 0) > 0 for r in res]


def build(out_dir: Path) -> dict:
    es = ES()
    t0 = time.time()
    cands: list[dict] = []
    for i, (cc, share) in enumerate(list(QUOTAS.items()) + [(None, OTHER_SHARE)]):
        want = round(N_MISSPELLINGS * share * 1.3)            # oversample: some fail to corrupt or collide
        got = sample_places(es, cc, want, SEED + i)
        print(f"  {cc or 'OTHER'}: sampled {len(got)}/{want}", file=sys.stderr)
        for g in got:
            g["quota"] = cc or "OTHER"
        cands += got
    seen, uniq = set(), []
    for c in cands:
        if c["place_id"] not in seen and E.fold(c["original"]) not in seen:
            seen.add(c["place_id"]); seen.add(E.fold(c["original"])); uniq.append(c)
    # targeted candidates (not in the main sample's seen-set on purpose; de-duplicated against it below)
    targeted: list[dict] = []
    for j, cc in enumerate(DIACRITIC_COUNTRIES):
        got = [g for g in sample_places(es, cc, 200, SEED + 100 + j, pool_mult=6)
               if E.strip_diacritics(g["original"]) != g["original"]][:N_DIACRITIC_PER_COUNTRY]
        for g in got:
            targeted.append({**g, "quota": "diacritic", "force": "diacritic"})
    ab_pool = []
    for j, pre in enumerate(ABBREV_PREFIXES):
        for g in sample_places(es, None, 400, SEED + 200 + j, extra_filter={"match_phrase_prefix": {"title": pre}}, pool_mult=2):
            if g["original"].lower().startswith(pre + " "):
                ab_pool.append({**g, "quota": "abbrev", "force": "abbrev"})
    random.Random(SEED + 7).shuffle(ab_pool)
    targeted += ab_pool[:N_ABBREV]
    print(f"  targeted candidates: diacritic={sum(1 for x in targeted if x['force']=='diacritic')} abbrev={sum(1 for x in targeted if x['force']=='abbrev')}", file=sys.stderr)
    # corrupt (seeded per place so re-runs agree)
    items = []
    for c in uniq + [x for x in targeted if x["place_id"] not in seen]:
        rng = random.Random(f"{SEED}:{c['place_id']}")
        r = E.corrupt(c["original"], rng, force=c.get("force"))
        if r:
            items.append({**c, "query": r[0], "classes": r[1], "targeted": bool(c.get("force"))})
    tr = truths(es, [i["original"] for i in items])
    ex = exact_exists(es, [i["query"] for i in items])
    tr = [sorted(set(t) | {it["place_id"]}) for it, t in zip(items, tr)]
    tcc = country_truths(es, tr, [i["ccode"] for i in items])
    final = []
    for it, t, tc, exists in zip(items, tr, tcc, ex):
        final.append({"id": f"m{len(final):04d}", "kind": "misspelling", "split": split_of(it["place_id"]),
                      "query": it["query"], "original": it["original"], "place_id": it["place_id"],
                      "truth_pids": t, "truth_cc": sorted(set(tc) | {it["place_id"]}), "targeted": it["targeted"], "ccode": it["ccode"], "population": it["population"], "quota": it["quota"],
                      "classes": it["classes"], "edit_distance": E.edit_distance(it["original"], it["query"]),
                      "folds_to_original": E.fold(it["query"]) == E.fold(it["original"]),
                      "collides": bool(exists)})
    # keep the quota proportions: trim each quota to its share
    by_q: dict[str, list[dict]] = {}
    for f in final:
        by_q.setdefault(f["quota"], []).append(f)
    kept = []
    for q, share in list(QUOTAS.items()) + [("OTHER", OTHER_SHARE)]:
        lst = [f for f in by_q.get(q, []) if not f["collides"]]
        kept += lst[: round(N_MISSPELLINGS * share)]
    for q in ("diacritic", "abbrev"):
        kept += [f for f in by_q.get(q, []) if not f["collides"]]
    colliding = [f for f in final if f["collides"]][:100]        # kept, flagged, excluded from main metrics
    # nonsense
    rng = random.Random(SEED + 99)
    nons, tries = [], 0
    while len(nons) < N_NONSENSE and tries < 2000:
        tries += 1
        s = E.nonsense(rng)
        nons.append(s)
    exist = exact_exists(es, nons)
    nonsense = [{"id": f"n{i:04d}", "kind": "nonsense", "split": "dev" if i % 2 == 0 else "test", "query": s,
                 "original": s, "place_id": None, "truth_pids": [], "truth_cc": [], "targeted": False, "ccode": None, "classes": [],
                 "edit_distance": 0, "collides": False}
                for i, (s, e) in enumerate((s, e) for s, e in zip(nons, exist) if not e)]
    allitems = kept + colliding + nonsense
    for n, it in enumerate(allitems):
        it["id"] = f"{'n' if it['kind'] == 'nonsense' else 'm'}{n:04d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nearmiss_v1.jsonl").write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in allitems))
    counts = es.post(f"/{es.top}/_count", {}), es.post(f"/{es.places}/_count", {})
    concrete = {}
    for alias in (es.top, es.places):
        r = es.cl.get(f"{es.base}/_alias/{alias}", auth=es.auth, headers=es.h)
        concrete[alias] = sorted(r.json()) if r.status_code == 200 else [alias]
    manifest = {"version": 1, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seed": SEED,
                "toponyms_index": es.top, "places_index": es.places, "concrete_indexes": concrete,
                "toponyms_docs": counts[0]["count"], "places_docs": counts[1]["count"],
                "quotas": QUOTAS, "other_share": OTHER_SHARE,
                "counts": {"misspelling_main": sum(1 for k in kept if not k["targeted"]), "targeted": sum(1 for k in kept if k["targeted"]), "misspelling_colliding": len(colliding), "nonsense": len(nonsense),
                           "dev": sum(1 for i in allitems if i["split"] == "dev"), "test": sum(1 for i in allitems if i["split"] == "test")},
                "seconds": round(time.time() - t0)}
    (out_dir / "nearmiss_v1.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evaluation/nearmiss/data")
    args = ap.parse_args()
    print(json.dumps(build(Path(args.out)), indent=2))
