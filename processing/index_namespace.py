#!/usr/bin/env python
"""Incremental single-namespace loader for the LIVE ``places`` + ``toponyms`` indices.

Unlike :mod:`processing.index_from_stage` — which builds a *new* index from every
namespace's final snapshot and swaps the ``places`` alias onto it (a full-rebuild
cutover) — this utility ADDS or REPLACES exactly one namespace in the EXISTING
live indices, in place, without creating a new index or touching the alias. It is
the safe way to fold a single (small) authority such as ``ukhc`` into production
between full rebuilds, and is generic across namespaces.

  places    Bulk-index the namespace's *enriched* docs (the staged ``final``
            snapshot, which already carries h3_cover / ccodes / geom_ref) into the
            concrete index behind the ``places`` alias, keyed ``_id = place_id``.

  toponyms  The toponyms index is a DEDUPLICATED store keyed ``_id = toponym_id``,
            whose docs carry attestation back-links to places plus a Symphonym
            ``embedding``. We therefore AUGMENT, never overwrite: for each toponym
            a namespace place attests, a scripted update appends the place_id to
            ``attestations`` and the namespace to ``namespaces`` and leaves the
            embedding, name and other namespaces' attestations untouched. Toponyms
            not yet present are created via ``upsert`` WITHOUT an embedding and
            reported — a follow-up phonetics backfill must compute their vectors
            (until then they are findable by exact/prefix/wildcard, not fuzzy KNN).

Safe by default: prints a plan and changes nothing unless ``--execute`` is given.

    # dry-run (default): show what would change
    python -m processing.index_namespace --namespace ukhc --es-host http://localhost:9201

    # apply (add / augment)
    python -m processing.index_namespace --namespace ukhc --es-host http://localhost:9201 --execute

    # re-ingest: delete the namespace's places first and strip its stale
    # toponym attestations before re-adding
    python -m processing.index_namespace --namespace ukhc --es-host ... --replace --execute
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from elasticsearch import Elasticsearch
from elasticsearch import helpers as es_helpers

from processing.settings import STAGED_BASE_DIR

try:
    from phonetics.utils.script_detection import detect_script
except Exception:  # pragma: no cover - script detection optional
    detect_script = None

PLACES_ALIAS = "places"
TOPONYMS_ALIAS = "toponyms"
# Stage snapshots preferred as the places source, most-enriched first. ``final``
# carries the merged h3 / ccode enrichment and is what index_from_stage loads.
SOURCE_STAGES = ("final", "ccode_merged", "h3_merged", "extract")
_LANG_NULLS = {"und", "zxx", "mis", "null", "none"}


# ---------------------------------------------------------------------------
# Index resolution
# ---------------------------------------------------------------------------

def resolve_concrete_index(es: Elasticsearch, alias: str) -> str:
    """Resolve the single concrete index behind ``alias`` (never creates one)."""
    try:
        mapping = es.indices.get_alias(name=alias)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Cannot resolve alias '{alias}': {exc}")
    names = list(mapping.keys())
    if len(names) != 1:
        raise SystemExit(
            f"Alias '{alias}' resolves to {len(names)} indices ({names}); refusing "
            f"to guess. Point --{alias}-index at the intended concrete index."
        )
    return names[0]


# ---------------------------------------------------------------------------
# Source places
# ---------------------------------------------------------------------------

def _source_path(namespace: str, stage: str | None) -> tuple[Path, str]:
    base = Path(STAGED_BASE_DIR) / namespace
    stages = (stage,) if stage else SOURCE_STAGES
    for st in stages:
        pq = base / st / "places.parquet"
        if pq.exists():
            return pq, st
        jl = base / st / "places.jsonl"
        if jl.exists():
            return jl, st
    raise SystemExit(
        f"No staged places source for '{namespace}' under {base} "
        f"(looked in stages: {', '.join(stages)})"
    )


def iter_place_docs(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=500):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row
    else:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)


def _has_uncovered_geometry(doc: dict) -> bool:
    """True if any has_geom geometry lacks an h3_cover (i.e. h3 stage not merged)."""
    for g in doc.get("geometries") or []:
        if isinstance(g, dict) and g.get("has_geom") and not g.get("h3_cover"):
            return True
    return False


# ---------------------------------------------------------------------------
# Toponym derivation (mirrors phonetics rebuild's canonical id / field rules)
# ---------------------------------------------------------------------------

def parse_toponym_id(top_id: str) -> tuple[str, str, str | None, str | None, str | None] | None:
    """``name@lang-variant`` → (canonical_id, name, lang, lang_variant, script).

    Mirrors ``rebuild_toponyms_index``: split on the last ``@``, ``lang-variant``
    on the first ``-``, drop null-ish langs, canonical id ``name@lang`` (or
    ``name@`` when lang is absent). Returns ``None`` for an unusable id.
    """
    if not top_id:
        return None
    if "@" in top_id:
        at = top_id.rfind("@")
        name = top_id[:at].strip()
        lang_part = top_id[at + 1:]
        if "-" in lang_part:
            lang, lang_variant = lang_part.split("-", 1)
        else:
            lang, lang_variant = lang_part, None
    else:
        name, lang, lang_variant = top_id.strip(), None, None
    if not name:
        return None
    if not lang or lang.lower() in _LANG_NULLS:
        lang = None
    script = None
    if detect_script is not None:
        try:
            script = detect_script(name)[0].value
        except Exception:  # noqa: BLE001
            script = None
    canonical_id = f"{name}@{lang}" if lang else f"{name}@"
    return canonical_id, name, lang, lang_variant, script


def collect_attestations(docs: Iterable[dict]) -> tuple[list[dict], dict]:
    """Return per-toponym attestation records and the place docs (materialised).

    Each record: ``{toponym_id, name, lang, lang_variant, script, place_ids}``.
    """
    place_docs: list[dict] = []
    by_topid: dict[str, dict] = {}
    for doc in docs:
        place_docs.append(doc)
        pid = doc.get("place_id")
        if not pid:
            continue
        for top in doc.get("toponyms") or []:
            if not isinstance(top, dict):
                continue
            parsed = parse_toponym_id(top.get("toponym_id"))
            if not parsed:
                continue
            canonical_id, name, lang, lang_variant, script = parsed
            rec = by_topid.get(canonical_id)
            if rec is None:
                rec = {
                    "toponym_id": canonical_id, "name": name, "lang": lang,
                    "lang_variant": lang_variant, "script": script,
                    "place_ids": set(),
                }
                by_topid[canonical_id] = rec
            rec["place_ids"].add(pid)
    return list(by_topid.values()), {"place_docs": place_docs}


# ---------------------------------------------------------------------------
# Painless scripts (augment-only — never overwrite embedding / name / others)
# ---------------------------------------------------------------------------

_AUGMENT_SCRIPT = (
    "if (ctx._source.attestations == null) { ctx._source.attestations = []; } "
    "for (pid in params.place_ids) { if (!ctx._source.attestations.contains(pid)) "
    "{ ctx._source.attestations.add(pid); } } "
    "if (ctx._source.namespaces == null) { ctx._source.namespaces = []; } "
    "if (!ctx._source.namespaces.contains(params.ns)) { ctx._source.namespaces.add(params.ns); } "
    "if (ctx._source.primary_namespace == null) { ctx._source.primary_namespace = params.ns; } "
    "ctx._source.indexed_at = params.indexed_at;"
)

# Replace-mode pre-pass: strip this namespace's stale attestations/namespace.
_STRIP_NS_SCRIPT = (
    "if (ctx._source.attestations != null) { "
    "ctx._source.attestations.removeIf(a -> a.startsWith(params.prefix)); } "
    "if (ctx._source.namespaces != null) { ctx._source.namespaces.removeIf(n -> n == params.ns); } "
    "if (params.ns.equals(ctx._source.primary_namespace)) { "
    "ctx._source.primary_namespace = (ctx._source.namespaces != null && ctx._source.namespaces.length > 0) "
    "? ctx._source.namespaces[0] : null; }"
)


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------

def plan_and_index_places(es, index, namespace, place_docs, *, replace, execute,
                          allow_missing_h3):
    uncovered = sum(1 for d in place_docs if _has_uncovered_geometry(d))
    print(f"\n[places] target index: {index}")
    print(f"[places] docs to index: {len(place_docs):,}")
    if uncovered:
        msg = (f"[places] WARNING: {uncovered:,} docs have has_geom geometries with "
               f"NO h3_cover — source is not the enriched 'final' stage. Such docs "
               f"will not work as fuzzy containment regions.")
        if not allow_missing_h3 and execute:
            raise SystemExit(msg + "\nRefusing to index; run the h3 stage first or "
                                   "pass --allow-missing-h3 to override.")
        print(msg)

    if replace:
        print(f"[places] REPLACE: delete_by_query place_id prefix '{namespace}:'")
        if execute:
            resp = es.options(request_timeout=3600).delete_by_query(
                index=index,
                body={"query": {"prefix": {"place_id": f"{namespace}:"}}},
                conflicts="proceed", refresh=False, slices="auto",
                wait_for_completion=True,
            )
            print(f"[places]   deleted {resp.get('deleted', 0):,}")

    if not execute:
        print("[places] (dry-run) would bulk-index the above docs (_id=place_id)")
        return

    def actions():
        for d in place_docs:
            pid = d.get("place_id")
            if not pid:
                continue
            yield {"_op_type": "index", "_index": index, "_id": pid, "_source": d}

    ok, errors = es_helpers.bulk(es, actions(), chunk_size=500, raise_on_error=False)
    print(f"[places] indexed ok={ok:,} errors={len(errors) if isinstance(errors, list) else errors}")
    if errors:
        for e in (errors or [])[:5]:
            print("   ", json.dumps(e)[:200], file=sys.stderr)


# ---------------------------------------------------------------------------
# Toponyms (augment, never overwrite)
# ---------------------------------------------------------------------------

def _existing_topids(es, index, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        resp = es.mget(index=index, body={"ids": chunk}, _source=False)
        for d in resp.get("docs", []):
            if d.get("found"):
                found.add(d["_id"])
    return found


def plan_and_index_toponyms(es, index, namespace, attest_records, *, replace, execute):
    indexed_at = datetime.now(timezone.utc).isoformat()
    topids = [r["toponym_id"] for r in attest_records]
    print(f"\n[toponyms] target index: {index}")
    print(f"[toponyms] distinct toponyms attested by {namespace}: {len(topids):,}")

    existing = _existing_topids(es, index, topids) if topids else set()
    new_ids = [t for t in topids if t not in existing]
    print(f"[toponyms]   already present (augment attestations): {len(existing):,}")
    print(f"[toponyms]   new (created WITHOUT embedding — need backfill): {len(new_ids):,}")
    if new_ids:
        print(f"[toponyms]   sample new: {', '.join(new_ids[:8])}")

    if replace:
        print(f"[toponyms] REPLACE: strip stale '{namespace}:' attestations first "
              f"(update_by_query, prefix match)")
        if execute:
            resp = es.options(request_timeout=3600).update_by_query(
                index=index,
                body={
                    "query": {"prefix": {"attestations": f"{namespace}:"}},
                    "script": {"source": _STRIP_NS_SCRIPT, "lang": "painless",
                               "params": {"prefix": f"{namespace}:", "ns": namespace}},
                },
                conflicts="proceed", refresh=False, slices="auto",
                wait_for_completion=True,
            )
            print(f"[toponyms]   stripped from {resp.get('updated', 0):,} toponyms")

    if not execute:
        print("[toponyms] (dry-run) would augment/upsert the above toponyms "
              "(append attestations + namespace; never overwrite embedding/name)")
        return

    def actions():
        for r in attest_records:
            place_ids = sorted(r["place_ids"])
            upsert_doc = {
                "name": r["name"], "lang": r["lang"], "lang_variant": r["lang_variant"],
                "script": r["script"], "namespaces": [namespace],
                "primary_namespace": namespace, "attestations": place_ids,
                "indexed_at": indexed_at,
            }  # NB: no 'embedding' — backfilled by the phonetics pipeline.
            yield {
                "_op_type": "update", "_index": index, "_id": r["toponym_id"],
                "script": {"source": _AUGMENT_SCRIPT, "lang": "painless",
                           "params": {"place_ids": place_ids, "ns": namespace,
                                      "indexed_at": indexed_at}},
                "upsert": upsert_doc,
            }

    ok, errors = es_helpers.bulk(es, actions(), chunk_size=500, raise_on_error=False)
    print(f"[toponyms] updated ok={ok:,} errors={len(errors) if isinstance(errors, list) else errors}")
    if errors:
        for e in (errors or [])[:5]:
            print("   ", json.dumps(e)[:200], file=sys.stderr)
    if new_ids:
        print(f"\n[toponyms] NOTE: {len(new_ids):,} new toponyms were created without "
              f"embeddings. Run the Symphonym backfill so they become fuzzy-searchable.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", required=True, help="Authority namespace, e.g. ukhc")
    ap.add_argument("--es-host", required=True, help="Elasticsearch URL (prod: http://localhost:9201)")
    ap.add_argument("--source-stage", choices=SOURCE_STAGES,
                    help="Staged source stage (default: most-enriched available)")
    ap.add_argument("--replace", action="store_true",
                    help="Re-ingest: delete the namespace's places and strip its stale "
                         "toponym attestations before adding")
    ap.add_argument("--places-only", action="store_true")
    ap.add_argument("--toponyms-only", action="store_true")
    ap.add_argument("--allow-missing-h3", action="store_true",
                    help="Index places even if some lack h3_cover (not recommended)")
    ap.add_argument("--execute", action="store_true",
                    help="Apply changes (default: dry-run, no writes)")
    args = ap.parse_args()

    es = Elasticsearch(args.es_host, request_timeout=120)
    info = es.info()
    print(f"ES {info['version']['number']} @ {args.es_host}  "
          f"mode={'EXECUTE' if args.execute else 'DRY-RUN'}  "
          f"{'REPLACE' if args.replace else 'ADD'}")

    src_path, stage = _source_path(args.namespace, args.source_stage)
    print(f"source: {src_path}  (stage={stage})")
    attest_records, ctx = collect_attestations(iter_place_docs(src_path))
    place_docs = ctx["place_docs"]

    if not args.toponyms_only:
        places_index = resolve_concrete_index(es, PLACES_ALIAS)
        plan_and_index_places(es, places_index, args.namespace, place_docs,
                              replace=args.replace, execute=args.execute,
                              allow_missing_h3=args.allow_missing_h3)
    if not args.places_only:
        toponyms_index = resolve_concrete_index(es, TOPONYMS_ALIAS)
        plan_and_index_toponyms(es, toponyms_index, args.namespace, attest_records,
                                replace=args.replace, execute=args.execute)

    if args.execute and not args.toponyms_only:
        es.indices.refresh(index=resolve_concrete_index(es, PLACES_ALIAS))
    if args.execute and not args.places_only:
        es.indices.refresh(index=resolve_concrete_index(es, TOPONYMS_ALIAS))
    print("\nDone." + ("" if args.execute else "  (dry-run — no changes written)"))


if __name__ == "__main__":
    main()
