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
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from elasticsearch import Elasticsearch
from elasticsearch import helpers as es_helpers

# Elasticsearch enforces a hard 512-byte limit on document ``_id``. Toponym
# ids are ``name@lang``; a handful of malformed source names blow past it.
_MAX_TOPONYM_ID_BYTES = 512


def _es_basic_auth() -> tuple[str, str] | None:
    """``(elastic, password)`` for the authed prod ES (or None for unauthed staging).

    Reads ``ELASTIC_PASSWORD`` then the on-disk password file the gateway uses, so
    credentials never have to be embedded in ``--es-host``."""
    pw = os.environ.get("ELASTIC_PASSWORD")
    if not pw:
        pw_file = os.environ.get(
            "ELASTIC_PASS_FILE",
            f"{os.environ.get('IX1_BASE', '/ix1/ishi')}/es/config/elastic.password")
        try:
            pw = Path(pw_file).read_text(encoding="utf-8").strip()
        except OSError:
            pw = None
    return ("elastic", pw) if pw else None

from processing.settings import STAGED_BASE_DIR
from processing.staged_parquet import drop_nulls_for_parquet, strip_hull

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
                    # ⚠ PARQUET ONLY, and deliberately not the JSONL branch below.
                    # A parquet struct column has ONE schema for the whole file,
                    # so reading it back materialises every key any row used, as
                    # an explicit null on the rows that lacked it. A timespan
                    # written `{"start": {"latest": 2026}}` returns
                    # `{"start": {"in": None, "latest": 2026}}` if any other doc
                    # in the same file carried `in` — which is why the null
                    # pattern in the live index is a fingerprint of what SHARED
                    # a staged file rather than of the record itself: osm and nl
                    # are clean, tgn carries `in: null`, wd carries all three,
                    # ohm carries a wholly null `end`.
                    #
                    # `drop_nulls_for_parquet` is applied on the WRITE side
                    # already; this is the missing half. The JSONL branch is
                    # left alone on purpose — `normalize_for_parquet` puts
                    # deliberate `None`s there (empty lists) and stripping them
                    # would be a different change with no reported defect.
                    yield drop_nulls_for_parquet(strip_hull(row))
    else:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield strip_hull(json.loads(line))


def _has_uncovered_geometry(doc: dict) -> bool:
    """True if any geometry is missing the fields the h3 stage should have added.

    Checks EVERY located geometry, not just ``has_geom`` ones. The narrower test
    (has_geom + no h3_cover) let point-only namespaces through with no h3 at all
    — which is how chgis/og/tm reached prod unindexed for containment
    (place#145). A geometry with a ``repr_point`` must carry an ``h3_centroid``
    and an ``h3_cover``; ``bounds`` comes from ``enrich_geometry`` and its
    absence means the geometry entry was hand-built rather than enriched.
    """
    for g in doc.get("geometries") or []:
        if not isinstance(g, dict):
            continue
        if g.get("has_geom") and not g.get("h3_cover"):
            return True
        if g.get("repr_point") and not (
                g.get("h3_centroid") and g.get("h3_cover") and g.get("bounds")):
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
    """Return per-toponym attestation records, consuming ``docs`` as a stream.

    Each record: ``{toponym_id, name, lang, lang_variant, script, place_ids}``.

    ⚠ This used to also return the place docs it had walked past, as a
    materialised list, so the caller could index places without re-reading the
    source. That was invisible at ukhc's 92 docs and untenable at tgn's
    2,991,143: the list passed 9.7 GB RSS still climbing at 2.6 GB/min on the VM
    that also hosts production Elasticsearch — the exact shape of the incident
    the project notes record. The places path now re-reads the staged file
    instead (``iter_place_docs`` reopens it, so a second pass is free), and only
    the toponym aggregation — inherently a whole-corpus group-by — is held, and
    only when toponyms are actually being written.

    Toponyms whose canonical ``_id`` exceeds Elasticsearch's hard 512-byte
    ``_id`` limit are skipped (with a warning) rather than aborting the whole
    augmentation — these are invariably malformed source names (e.g. a
    comma-joined variant-spelling apparatus dumped into one LPF ``name``
    field), not real single toponyms. The count is returned in the meta dict.
    """
    by_topid: dict[str, dict] = {}
    skipped_oversize = 0
    for doc in docs:
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
            if len(canonical_id.encode("utf-8")) > _MAX_TOPONYM_ID_BYTES:
                skipped_oversize += 1
                if skipped_oversize <= 3:
                    print(
                        f"[toponyms] SKIP oversize _id "
                        f"({len(canonical_id.encode('utf-8'))}B > "
                        f"{_MAX_TOPONYM_ID_BYTES}B) from {pid}: "
                        f"{canonical_id[:60]}…",
                        file=sys.stderr,
                    )
                continue
            rec = by_topid.get(canonical_id)
            if rec is None:
                rec = {
                    "toponym_id": canonical_id, "name": name, "lang": lang,
                    "lang_variant": lang_variant, "script": script,
                    "place_ids": set(),
                }
                by_topid[canonical_id] = rec
            rec["place_ids"].add(pid)
    if skipped_oversize:
        print(
            f"[toponyms] skipped {skipped_oversize:,} toponym(s) with _id "
            f"> {_MAX_TOPONYM_ID_BYTES}B (malformed source names)",
            file=sys.stderr,
        )
    return list(by_topid.values()), {"skipped_oversize": skipped_oversize}


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

def scan_places(doc_iter) -> tuple[int, int]:
    """Stream the staged source once, counting docs and uncovered geometries.

    Separated from the write pass so the h3 refusal below still happens BEFORE
    anything is written, now that the docs are no longer held in memory. Both
    numbers are reported, because "0 uncovered" means nothing without the
    denominator it was drawn from.
    """
    n = uncovered = 0
    for doc in doc_iter:
        n += 1
        if _has_uncovered_geometry(doc):
            uncovered += 1
    return n, uncovered


def plan_and_index_places(es, index, namespace, doc_iter_factory, *, n_docs,
                          uncovered, replace, execute, allow_missing_h3):
    """Index one namespace's places, streaming from ``doc_iter_factory()``.

    ``doc_iter_factory`` is a zero-argument callable returning a FRESH iterator
    over the staged docs — not a list, and not a single spent iterator. The
    counts come from :func:`scan_places`, which has already made one pass.
    """
    print(f"\n[places] target index: {index}")
    print(f"[places] docs to index: {n_docs:,}")
    if uncovered:
        msg = (f"[places] WARNING: {uncovered:,} docs have geometries missing "
               f"h3_centroid / h3_cover / bounds — source is not the enriched "
               f"'final' stage. Such docs will not work as fuzzy containment "
               f"regions, and points are affected as well as polygons.")
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
        seen = skipped = 0
        for d in doc_iter_factory():
            pid = d.get("place_id")
            if not pid:
                skipped += 1
                continue
            seen += 1
            if seen % 250_000 == 0:
                print(f"[places]   ... {seen:,} / {n_docs:,} submitted", flush=True)
            yield {"_op_type": "index", "_index": index, "_id": pid, "_source": d}
        if skipped:
            print(f"[places] WARNING: {skipped:,} staged doc(s) had no place_id "
                  f"and were not indexed", file=sys.stderr)

    ok, errors = es_helpers.bulk(es, actions(), chunk_size=500, raise_on_error=False)
    n_err = len(errors) if isinstance(errors, list) else errors
    print(f"[places] indexed ok={ok:,} errors={n_err}")
    # A bulk that reports no errors is not evidence it indexed the corpus: a
    # short read of the staged file ends the generator early and still exits
    # clean. Compare against the pre-scan count, which came from a separate pass.
    if ok != n_docs:
        print(f"[places] ⚠ INDEXED {ok:,} BUT PRE-SCAN COUNTED {n_docs:,} "
              f"({n_docs - ok:+,}) — the write pass did not see the same corpus "
              f"as the scan pass. Do not treat this run as complete.",
              file=sys.stderr)
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


def plan_and_index_toponyms(es, index, namespace, attest_records, *, replace, execute,
                            emit_new_toponyms=None):
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

    # Capture the newly-created toponyms (the only ones lacking an embedding) at
    # index time — the zero-rescan input for the Symphonym backfill `compute`
    # phase (phonetics.inference.backfill_embeddings). Written whether dry-run or
    # execute: it's the set that WILL need embeddings once this run is applied.
    if emit_new_toponyms:
        new_set = set(new_ids)
        out = Path(emit_new_toponyms)
        out.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with out.open("w", encoding="utf-8") as fh:
            for r in attest_records:
                if r["toponym_id"] in new_set:
                    fh.write(json.dumps({"toponym_id": r["toponym_id"],
                                         "name": r["name"],
                                         "lang": r["lang"] or "und"}, ensure_ascii=False) + "\n")
                    n += 1
        print(f"[toponyms]   emitted {n:,} new-toponym records → {out}  "
              f"(feed to backfill_embeddings compute)")

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


def _refresh_registry_aggregates(namespace: str) -> None:
    """Regenerate this namespace's registry aggregates after an incremental add.

    CLAUDE.md's incremental single-namespace workflow lists the aggregates as a
    MANUAL step, which is the gap written down rather than closed. Both
    generators are wired only into full-run paths — ``temporal_extent`` into
    Batch 9, ``h3_coverage`` into the h3 stage — so a targeted add like this one
    updates the staged tree and the live index while leaving the aggregates
    describing the corpus as it was before. They then feed ``record_count`` and
    ``temporal_extent`` onto the public gazetteer page, so the stale reading is
    user-visible.

    Best-effort by design: the index write has already succeeded and must not be
    reported as failed because an aggregate could not be rebuilt. But it is
    reported LOUDLY rather than swallowed — an unnoticed failure here is exactly
    how 18 namespaces came to be pushing figures computed from data months old.
    ``push_gazetteer_inventory`` refuses a stale aggregate independently, so a
    failure here is caught again at push time rather than reaching the registry.
    """
    from processing.staging_contract import GLOBAL_COVERAGE_NAMESPACES, is_relations_only

    if is_relations_only(namespace):
        return

    run_id = f"incr-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    jobs = [("processing.gazetteer_temporal_extent", True)]
    # Global namespaces publish the sentinel and legitimately have no file.
    jobs.append(("processing.gazetteer_h3_coverage",
                 namespace not in GLOBAL_COVERAGE_NAMESPACES))

    for module, applicable in jobs:
        if not applicable:
            continue
        print(f"\nRefreshing {module.rsplit('.', 1)[-1]} for {namespace} ...")
        rc = subprocess.call([sys.executable, "-m", module,
                              "--run-id", run_id, "--namespace", namespace])
        if rc != 0:
            print(
                f"⚠ {module} FAILED (exit {rc}) for {namespace}. The index write "
                f"succeeded; the registry aggregate is now STALE. Rerun it before "
                f"pushing the inventory — push_gazetteer_inventory will refuse it.",
                file=sys.stderr,
            )


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
    ap.add_argument("--emit-new-toponyms", metavar="FILE",
                    help="Write {toponym_id,name,lang} JSONL for the newly-created "
                         "(embedding-less) toponyms — the zero-rescan input for "
                         "phonetics.inference.backfill_embeddings compute")
    args = ap.parse_args()

    es = Elasticsearch(args.es_host, basic_auth=_es_basic_auth(), request_timeout=120)
    info = es.info()
    print(f"ES {info['version']['number']} @ {args.es_host}  "
          f"mode={'EXECUTE' if args.execute else 'DRY-RUN'}  "
          f"{'REPLACE' if args.replace else 'ADD'}")

    src_path, stage = _source_path(args.namespace, args.source_stage)
    print(f"source: {src_path}  (stage={stage})")

    if not args.toponyms_only:
        n_docs, uncovered = scan_places(iter_place_docs(src_path))
        print(f"[places] scanned {n_docs:,} staged docs, {uncovered:,} with "
              f"uncovered geometry")
        places_index = resolve_concrete_index(es, PLACES_ALIAS)
        plan_and_index_places(es, places_index, args.namespace,
                              lambda: iter_place_docs(src_path),
                              n_docs=n_docs, uncovered=uncovered,
                              replace=args.replace, execute=args.execute,
                              allow_missing_h3=args.allow_missing_h3)
    if not args.places_only:
        # Built only when toponyms are actually written — this aggregation is a
        # whole-corpus group-by and is the remaining memory cost of the run.
        attest_records, _ctx = collect_attestations(iter_place_docs(src_path))
        toponyms_index = resolve_concrete_index(es, TOPONYMS_ALIAS)
        plan_and_index_toponyms(es, toponyms_index, args.namespace, attest_records,
                                replace=args.replace, execute=args.execute,
                                emit_new_toponyms=args.emit_new_toponyms)

    if args.execute and not args.toponyms_only:
        es.indices.refresh(index=resolve_concrete_index(es, PLACES_ALIAS))
    if args.execute and not args.places_only:
        es.indices.refresh(index=resolve_concrete_index(es, TOPONYMS_ALIAS))
    if args.execute and not args.toponyms_only:
        _refresh_registry_aggregates(args.namespace)

    print("\nDone." + ("" if args.execute else "  (dry-run — no changes written)"))


if __name__ == "__main__":
    main()
