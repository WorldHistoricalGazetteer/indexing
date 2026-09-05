"""Build the retrieval/discrimination corpus from the live index. Runs on pitt.

    python -m evaluation.build_corpus \
        --out-dir /vast/ishi/symphonym-eval/<run-id> \
        --haystack 1000000 --places 400000 --seed 20260905

Writes, and writes a manifest that makes every number in the report re-derivable:

    haystack.jsonl     the searchable corpus, one toponym per line
    positives.jsonl    cross-script name pairs of the same place
    pairs.jsonl        positives + matched negatives, for the discrimination gate
    manifest.json      seeds, quotas, denominators, injected count, exclusions,
                       and the per-script-pair census of what could NOT be built

EVERY COUNT IS PAIRED WITH ITS DENOMINATOR. "0 unmatched" over a script pair
that produced no positives at all is not a success, and the manifest is written
so the two cannot be confused: each cell carries positives, negatives and
unmatched separately.

⚠ RUNS AGAINST PRODUCTION, which is serving live search and has no working
watchdog behind it. `--throttle` paces every page; the default is not zero.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from evaluation.corpus import (
    SCRIPT_COUNTS, Positive, cross_script_pairs, proportional_quota,
    sample_script, script_distribution, summarise_positives)
from evaluation.negatives import (
    HardLinks, HaystackIndex, build_negatives, drop_unpaired)

DEFAULT_HARDLINK_DB = "/vast/ishi/hardlinks/hard_links.sqlite"


def _es(host: str, password_file: str):
    from elasticsearch import Elasticsearch
    return Elasticsearch(host, basic_auth=("elastic", Path(password_file).read_text().strip()),
                         request_timeout=120, retry_on_timeout=True, max_retries=3)


def sample_places(es, n: int, seed: int, index: str = "places",
                  page: int = 2_000, throttle: float = 0.05):
    """Random places carrying at least two toponyms, paged deterministically."""
    out, after = [], None
    while len(out) < n:
        body = {
            "size": min(page, n - len(out)),
            "track_total_hits": False,
            "_source": ["place_id", "toponyms.toponym_id"],
            "query": {"function_score": {
                "query": {"bool": {"filter": [
                    {"nested": {"path": "toponyms",
                                "query": {"exists": {"field": "toponyms.toponym_id"}}}}]}},
                "random_score": {"seed": seed, "field": "_seq_no"},
                "boost_mode": "replace"}},
            "sort": [{"_score": "desc"}, {"_doc": "asc"}],
        }
        if after is not None:
            body["search_after"] = after
        res = es.search(index=index, body=body)
        hits = res["hits"]["hits"]
        if not hits:
            break
        for h in hits:
            src = h["_source"]
            src.setdefault("place_id", h["_id"])
            out.append(src)
        after = hits[-1]["sort"]
        if throttle:
            time.sleep(throttle)
    return out


def names_of_places(es, place_ids: list[str], index: str = "places",
                    chunk: int = 500) -> dict[str, set[str]]:
    """Every toponym name of each place, for the co-reference exclusion."""
    from evaluation.corpus import _split_toponym_id
    out: dict[str, set[str]] = {}
    ids = list(dict.fromkeys(place_ids))
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        res = es.mget(index=index, body={"ids": batch},
                      _source=["place_id", "toponyms.toponym_id"])
        for d in res["docs"]:
            if not d.get("found"):
                continue
            src = d["_source"]
            out[d["_id"]] = {
                _split_toponym_id(t["toponym_id"])[0].strip()
                for t in (src.get("toponyms") or []) if t.get("toponym_id")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--es-host", default="http://localhost:9201")
    ap.add_argument("--es-password-file",
                    default="/ix1/ishi/es/config/elastic.password")
    ap.add_argument("--haystack", type=int, default=1_000_000)
    ap.add_argument("--places", type=int, default=400_000,
                    help="places sampled to mine cross-script pairs from")
    ap.add_argument("--max-pairs-per-place", type=int, default=6,
                    help="cap, not filter: wd emits dozens of machine-generated "
                         "labels per place and would otherwise dominate. The "
                         "number capped away is reported.")
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--throttle", type=float, default=0.05,
                    help="seconds between pages; prod ES is live")
    ap.add_argument("--hardlink-db", default=DEFAULT_HARDLINK_DB)
    ap.add_argument("--floor", type=int, default=2_000,
                    help="minimum haystack docs per script")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    es = _es(args.es_host, args.es_password_file)
    started = time.time()

    # --- 1. the frame, re-measured and checked for completeness -------------
    counts = script_distribution(es)
    drift = {k: (SCRIPT_COUNTS.get(k), v) for k, v in counts.items()
             if SCRIPT_COUNTS.get(k) != v}
    quota = proportional_quota(counts, args.haystack, floor=args.floor)
    print(f"[corpus] frame: {sum(counts.values()):,} toponyms over "
          f"{len(counts)} scripts; {len(drift)} scripts differ from the "
          f"recorded distribution", flush=True)

    # --- 2. haystack --------------------------------------------------------
    haystack, per_script = [], {}
    for script, want in sorted(quota.items(), key=lambda kv: -kv[1]):
        got = sample_script(es, script, want, args.seed, throttle=args.throttle)
        per_script[script] = {"requested": want, "returned": len(got),
                              "available": counts.get(script, 0)}
        haystack.extend(got)
        print(f"[corpus]   {script:<12} {len(got):>8,} of {want:,} requested "
              f"({counts.get(script, 0):,} available)", flush=True)

    # --- 3. positives -------------------------------------------------------
    places = sample_places(es, args.places, args.seed, throttle=args.throttle)
    positives, capped_total, places_with_pairs = [], 0, 0
    for pl in places:
        pairs, capped = cross_script_pairs(pl, max_per_place=args.max_pairs_per_place,
                                           rng=rng)
        capped_total += capped
        if pairs:
            places_with_pairs += 1
            positives.extend(pairs)
    print(f"[corpus] positives: {len(positives):,} cross-script pairs from "
          f"{places_with_pairs:,} of {len(places):,} places sampled "
          f"({capped_total:,} pairs capped away)", flush=True)

    # --- 4. inject the partners the proportional draw did not choose --------
    have = {d["name"] for d in haystack}
    injected = []
    for p in positives:
        if p.partner not in have:
            have.add(p.partner)
            injected.append({"name": p.partner, "lang": p.partner_lang,
                             "script": p.partner_script, "injected": True})
    haystack.extend(injected)
    print(f"[corpus] haystack: {len(haystack) - len(injected):,} sampled + "
          f"{len(injected):,} injected partners = {len(haystack):,}", flush=True)

    # --- 5. co-reference closure, then matched negatives --------------------
    links = HardLinks(args.hardlink_db)
    place_ids = sorted({p.place_id for p in positives})
    closure = {pid: links.neighbours(pid) for pid in place_ids}
    n_with_links = sum(1 for v in closure.values() if v)
    co_ids = sorted({q for v in closure.values() for q in v})
    co_names = names_of_places(es, co_ids) if co_ids else {}
    own_names = names_of_places(es, place_ids)
    print(f"[corpus] co-reference: {n_with_links:,} of {len(place_ids):,} places "
          f"carry a hard link; {len(co_ids):,} co-referents, "
          f"{len(co_names):,} resolved", flush=True)

    def forbidden_for(pos: Positive) -> set[str]:
        # `own_names` is keyed by a place that WAS resolved from the index; a
        # miss means the mget did not return it, not that the place has no
        # names. Distinguishing the two matters because a silent empty set here
        # disables the exclusion for that pair and nothing downstream can tell.
        if pos.place_id not in own_names:
            raise SystemExit(
                f"ABORT: place {pos.place_id!r} produced a positive but did not "
                f"come back from the places index, so its own names cannot be "
                f"excluded from the negative pool. Proceeding would draw "
                f"unfiltered negatives for it and report a normal-looking "
                f"census. Re-run the build, or drop that place explicitly.")
        names = set(own_names[pos.place_id])
        for q in closure.get(pos.place_id, ()):
            names |= co_names.get(q, set())
        return names

    hay_index = HaystackIndex(haystack)
    pairs, census = build_negatives(positives, hay_index, forbidden_for, rng)
    pairs, dropped = drop_unpaired(pairs)
    print(f"[corpus] pairs: {len(pairs):,} after dropping {dropped:,} unpaired "
          f"({census['unmatched_positives']:,} positives had no matched negative)",
          flush=True)

    # --- 6. write -----------------------------------------------------------
    with (out / "haystack.jsonl").open("w", encoding="utf-8") as fh:
        for d in haystack:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    with (out / "positives.jsonl").open("w", encoding="utf-8") as fh:
        for p in positives:
            fh.write(json.dumps(p.__dict__, ensure_ascii=False) + "\n")
    with (out / "pairs.jsonl").open("w", encoding="utf-8") as fh:
        for p in pairs:
            fh.write(json.dumps(p.__dict__, ensure_ascii=False) + "\n")

    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seed": args.seed,
        "es_host": args.es_host,
        "elapsed_s": round(time.time() - started, 1),
        "haystack": {
            "sampled": len(haystack) - len(injected),
            "injected_partners": len(injected),
            "total": len(haystack),
            "floor_per_script": args.floor,
            "per_script": per_script,
        },
        "frame": {"total": sum(counts.values()), "counts": counts,
                  "drift_from_recorded": drift},
        "places": {"sampled": len(places), "with_cross_script_pairs": places_with_pairs},
        "positives": summarise_positives(positives) | {
            "capped_away": capped_total, "cap_per_place": args.max_pairs_per_place},
        "co_reference": {
            "places": len(place_ids), "with_hard_link": n_with_links,
            "co_referents": len(co_ids), "co_referents_resolved": len(co_names),
            "hops": 1,
            "note": "one hop only; places linked solely through a third party "
                    "are NOT excluded from the negative pool",
        },
        "negatives": census | {"dropped_unpaired": dropped},
        "pairs_written": len(pairs),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"[corpus] manifest → {out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
