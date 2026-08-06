#!/usr/bin/env python
"""Pre-promotion parity check between the live `places` index and a staged one.

Promotion swaps an alias, which is instant and total: whatever the new index
contains becomes the corpus. The question that must be answered first is not
"did the pipeline report success" — it reported success for `clio` and `ohm`
while they were being killed at their wall — but "does the new index hold the
same places as the live one, and did the country codes change the way we
intended?"

Two comparisons, both aggregation-only:

**Parity (blocking).** Per-namespace document counts must match the live index
exactly. The staged chain re-derived `h3_cover` and `ccodes` over unchanged
extracts, so it must neither gain nor lose a place. Any difference means a
namespace was partially indexed, and promoting would publish the loss.

**Country-code delta (informational).** Documents carrying at least one ccode,
and per-country totals, before and after. This is the point of the campaign, so
it should be large and explicable — and a country that LOSES a lot of places is
the signature of a boundary regression.

Aggregations rather than a 51M-document scroll: production serves live traffic,
and its heap is already sensitive to merge pressure. A true per-document
unchanged/gained/changed/lost diff needs both id sets streamed and is a
separate, throttled job.

Usage::

    python -m processing.verify_ccode_promotion \\
        --old-host http://localhost:9201 --old-index places \\
        --new-host $STAGING_URL      --new-index places_h3ccode-...
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from elasticsearch import Elasticsearch

# Same literal the other production-facing tools use (apply_ccode_patch,
# apply_update_patch, promote_to_production); it is not in settings.py.
DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"


def _client(host: str, password_file: str | None) -> Elasticsearch:
    kwargs: dict[str, Any] = {"request_timeout": 120}
    if password_file:
        try:
            with open(password_file, encoding="utf-8") as fh:
                kwargs["basic_auth"] = ("elastic", fh.read().strip())
        except OSError:
            pass
    return Elasticsearch(host, **kwargs)


def namespace_counts(es: Elasticsearch, index: str) -> dict[str, int]:
    res = es.search(index=index, size=0, body={
        "aggs": {"ns": {"terms": {"field": "namespace", "size": 200}}}})
    return {b["key"]: b["doc_count"]
            for b in res["aggregations"]["ns"]["buckets"]}


def ccode_stats(es: Elasticsearch, index: str) -> tuple[int, dict[str, int]]:
    res = es.search(index=index, size=0, body={
        "query": {"exists": {"field": "ccodes"}},
        "aggs": {"cc": {"terms": {"field": "ccodes", "size": 300}}}})
    with_ccodes = res["hits"]["total"]["value"]
    per_cc = {b["key"]: b["doc_count"]
              for b in res["aggregations"]["cc"]["buckets"]}
    return with_ccodes, per_cc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--old-host", default="http://localhost:9201")
    ap.add_argument("--old-index", default="places")
    ap.add_argument("--new-host", required=True)
    ap.add_argument("--new-index", required=True)
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    ap.add_argument("--new-es-password-file", default=None,
                    help="Password file for the staging cluster, if different "
                         "(staging usually runs without auth)")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    old = _client(args.old_host, args.es_password_file)
    new = _client(args.new_host, args.new_es_password_file)

    print(f"OLD  {args.old_host}  {args.old_index}")
    print(f"NEW  {args.new_host}  {args.new_index}\n")

    old_ns = namespace_counts(old, args.old_index)
    new_ns = namespace_counts(new, args.new_index)

    all_ns = sorted(set(old_ns) | set(new_ns))
    mismatches = []
    print(f"{'namespace':12s} {'live':>13s} {'staged':>13s} {'delta':>11s}")
    for ns in all_ns:
        o, n = old_ns.get(ns, 0), new_ns.get(ns, 0)
        d = n - o
        flag = "" if d == 0 else "  <-- MISMATCH"
        if d:
            mismatches.append((ns, o, n))
        print(f"{ns:12s} {o:13,d} {n:13,d} {d:+11,d}{flag}")
    ot, nt = sum(old_ns.values()), sum(new_ns.values())
    print(f"{'TOTAL':12s} {ot:13,d} {nt:13,d} {nt - ot:+11,d}")

    print("\n--- country codes ---")
    old_wc, old_cc = ccode_stats(old, args.old_index)
    new_wc, new_cc = ccode_stats(new, args.new_index)
    print(f"documents with >=1 ccode: {old_wc:,} -> {new_wc:,} "
          f"({new_wc - old_wc:+,})")

    deltas = sorted(
        ((cc, old_cc.get(cc, 0), new_cc.get(cc, 0)) for cc in
         set(old_cc) | set(new_cc)),
        key=lambda t: t[2] - t[1])
    losers = [t for t in deltas if t[2] - t[1] < 0][:15]
    gainers = list(reversed(deltas[-15:]))

    print("\nlargest gains:")
    for cc, o, n in gainers:
        print(f"  {cc:4s} {o:12,d} -> {n:12,d}  {n - o:+12,d}")
    print("\nlargest losses (a big loss is the boundary-regression signature):")
    for cc, o, n in losers:
        print(f"  {cc:4s} {o:12,d} -> {n:12,d}  {n - o:+12,d}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"old_ns": old_ns, "new_ns": new_ns,
                       "old_with_ccodes": old_wc, "new_with_ccodes": new_wc,
                       "old_cc": old_cc, "new_cc": new_cc}, fh, indent=2)
        print(f"\nReport written to {args.json_out}")

    print("\n" + "=" * 70)
    if mismatches:
        print(f"PARITY FAILED for {len(mismatches)} namespace(s) — "
              f"DO NOT PROMOTE:")
        for ns, o, n in mismatches:
            print(f"  {ns}: live {o:,} vs staged {n:,}")
        return 1
    print(f"Parity OK: {len(all_ns)} namespaces, {nt:,} documents, "
          f"identical to the live index.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
