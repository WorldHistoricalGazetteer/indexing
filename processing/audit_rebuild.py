#!/usr/bin/env python
"""Post-rebuild audit — measure the live indices, never the pipeline's report.

`developer/plan-temporal-model.md` step 5 carries this warning:

    Audit per-namespace coverage after the rebuild. The last one
    (`postbarrier-20260502`) silently skipped embeddings for ~25% of toponyms,
    the `wd` geoshapes merge, and `ccode` for `osm`/`ohm`. Verify each stage
    landed rather than assuming the pipeline reported honestly.

Every check here reads the **live indices** and compares against an independent
expectation. None of them consults a manifest stage status, because that is the
thing that has repeatedly been wrong: the linear-feature defect ran to
completion and recorded its own loss as `docs_no_match`; `un`'s `final/` was
three days stale while its manifest said `completed`; `clio` and `ohm` reported
`running` while being killed at their wall.

Aggregations and counts only — no scrolls. Production serves live traffic and
its heap is sensitive to merge pressure.

Usage::

    python -m processing.audit_rebuild --es-host <URL> [--json-out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from elasticsearch import Elasticsearch

DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"

# Namespaces that legitimately hold no geometry, so a geometry-coverage
# shortfall is a property of the source rather than a pipeline fault.
_RELATIONS_ONLY = {"loc"}


def _client(host: str, password_file: str | None) -> Elasticsearch:
    kwargs: dict[str, Any] = {"request_timeout": 180}
    if password_file:
        try:
            with open(password_file, encoding="utf-8") as fh:
                kwargs["basic_auth"] = ("elastic", fh.read().strip())
        except OSError:
            pass
    return Elasticsearch(host, **kwargs)


def _count(es, index, query=None) -> int:
    body = {"query": query} if query else None
    return int(es.count(index=index, body=body)["count"])


def _terms(es, index, field, size=60, query=None) -> dict[str, int]:
    body: dict[str, Any] = {
        "size": 0, "aggs": {"a": {"terms": {"field": field, "size": size}}}}
    if query:
        body["query"] = query
    res = es.search(index=index, body=body)
    return {b["key"]: b["doc_count"]
            for b in res["aggregations"]["a"]["buckets"]}


def audit(es: Elasticsearch, *, places="places", toponyms="toponyms") -> dict:
    report: dict[str, Any] = {}
    findings: list[str] = []

    total = _count(es, places)
    report["places_total"] = total
    by_ns = _terms(es, places, "namespace", size=80)
    report["places_by_namespace"] = by_ns

    # --- 1. ccodes, the defect that started place#173 -----------------------
    coded = _count(es, places, {"exists": {"field": "ccodes"}})
    report["ccodes"] = {"coded": coded, "total": total,
                        "pct": round(100 * coded / total, 2) if total else 0}

    # Per-namespace, so a single namespace missing entirely cannot hide in a
    # healthy corpus-wide percentage — which is exactly how osm/ohm were missed.
    per_ns_coded = _terms(es, places, "namespace", size=80,
                          query={"exists": {"field": "ccodes"}})
    ns_ccode = {}
    for ns, n in sorted(by_ns.items()):
        c = per_ns_coded.get(ns, 0)
        pct = round(100 * c / n, 1) if n else 0.0
        ns_ccode[ns] = {"docs": n, "coded": c, "pct": pct}
        if ns not in _RELATIONS_ONLY and c == 0 and n > 0:
            findings.append(f"ccodes: {ns} has {n:,} docs and ZERO coded")
    report["ccodes_by_namespace"] = ns_ccode

    # --- 2. geometry flags: the standing defect predicate -------------------
    # schemas/field-notes.md: geom_class in {area,line} AND NOT has_geom means
    # the full geometry was never stored — an incomplete ingestion.
    for gc in ("area", "line"):
        q = {"nested": {"path": "geometries", "query": {"bool": {
            "filter": [{"term": {"geometries.geom_class": gc}}],
            "must_not": [{"term": {"geometries.has_geom": True}}]}}}}
        n = _count(es, places, q)
        report[f"geom_class_{gc}_without_has_geom"] = n
        if n:
            findings.append(
                f"geometry: {n:,} docs have a {gc} geometry with has_geom=false "
                f"(incomplete ingestion predicate)")

    # --- 3. h3_cover presence ----------------------------------------------
    # Fuzzy containment (the default search mode) runs entirely off h3_cover,
    # so an areal geometry without one is invisible to it (#174).
    q = {"nested": {"path": "geometries", "query": {"bool": {
        "filter": [{"term": {"geometries.geom_class": "area"}}],
        "must_not": [{"exists": {"field": "geometries.h3_cover"}}]}}}}
    n = _count(es, places, q)
    report["areal_without_h3_cover"] = n
    if n:
        findings.append(f"h3: {n:,} docs have an areal geometry with no "
                        f"h3_cover — invisible to fuzzy containment")

    # --- 4. toponyms + embeddings ------------------------------------------
    # "silently skipped embeddings for ~25% of toponyms" — measure, don't ask.
    # The index field is `embedding`; `phon_emb` is the name the SEARCH
    # RESPONSE uses. Querying the response name reported 0.0% coverage of
    # 72.7M toponyms — a false alarm of exactly the kind this audit exists to
    # avoid, and the reason its own field names are checked against the schema.
    t_total = _count(es, toponyms)
    t_emb = _count(es, toponyms, {"exists": {"field": "embedding"}})
    report["toponyms"] = {
        "total": t_total, "with_embedding": t_emb,
        "pct": round(100 * t_emb / t_total, 2) if t_total else 0}
    if t_total and t_emb / t_total < 0.95:
        findings.append(
            f"embeddings: only {100 * t_emb / t_total:.1f}% of {t_total:,} "
            f"toponyms carry phon_emb")

    # --- 5. wd geoshapes + links -------------------------------------------
    # The wd geoshapes merge was silently skipped last rebuild; sitelinks must
    # be re-run after any wd rebuild.
    # `links` is a NESTED field, so a plain exists() never matches it.
    wd_links = _count(es, places, {"bool": {"filter": [
        {"term": {"namespace": "wd"}},
        {"nested": {"path": "links", "query": {
            "exists": {"field": "links.identifier"}}}}]}})
    report["wd_with_links"] = wd_links
    wd_total = by_ns.get("wd", 0)
    if wd_total and wd_links == 0:
        findings.append("links: no wd doc carries links — sitelinks never ran")

    wd_areal = _count(es, places, {"bool": {"filter": [
        {"term": {"namespace": "wd"}},
        {"nested": {"path": "geometries", "query": {
            "term": {"geometries.geom_class": "area"}}}}]}})
    report["wd_areal_geometries"] = wd_areal
    if wd_total and wd_areal == 0:
        findings.append("geoshapes: no wd doc has an areal geometry — the "
                        "Commons geoshapes merge did not land")

    # --- 6. types / AAT -----------------------------------------------------
    typed = _count(es, places, {"nested": {"path": "types", "query": {
        "exists": {"field": "types.identifier"}}}})
    report["places_with_types"] = typed

    return {"report": report, "findings": findings}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--es-host", required=True)
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    ap.add_argument("--places", default="places")
    ap.add_argument("--toponyms", default="toponyms")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    es = _client(args.es_host, args.es_password_file)
    out = audit(es, places=args.places, toponyms=args.toponyms)
    rep, findings = out["report"], out["findings"]

    print("=" * 74)
    print("POST-REBUILD AUDIT — measured from the live indices")
    print("=" * 74)
    print(f"places total          {rep['places_total']:>14,}")
    cc = rep["ccodes"]
    print(f"  with ccodes         {cc['coded']:>14,}  ({cc['pct']}%)")
    t = rep["toponyms"]
    print(f"toponyms total        {t['total']:>14,}")
    print(f"  with phon_emb       {t['with_embedding']:>14,}  ({t['pct']}%)")
    print(f"places with types     {rep['places_with_types']:>14,}")
    print(f"wd docs with links    {rep['wd_with_links']:>14,}")
    print(f"wd areal geometries   {rep['wd_areal_geometries']:>14,}")
    print()
    print(f"areal w/o h3_cover    {rep['areal_without_h3_cover']:>14,}")
    print(f"area, has_geom=false  {rep['geom_class_area_without_has_geom']:>14,}")
    print(f"line, has_geom=false  {rep['geom_class_line_without_has_geom']:>14,}")

    print("\nper-namespace ccode coverage (lowest 12):")
    rows = sorted(rep["ccodes_by_namespace"].items(),
                  key=lambda kv: kv[1]["pct"])[:12]
    for ns, v in rows:
        print(f"  {ns:10s} {v['docs']:>12,}  {v['coded']:>12,}  {v['pct']:>5.1f}%")

    print("\n" + "-" * 74)
    if findings:
        print(f"FINDINGS ({len(findings)}):")
        for f in findings:
            print(f"  * {f}")
    else:
        print("No findings — every measured expectation held.")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
        print(f"\nWritten to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
