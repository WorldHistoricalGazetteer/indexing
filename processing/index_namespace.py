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

One contributed WHG dataset (place#341) — ``--dataset`` is valid only with
``--namespace whg``. The source is the per-dataset staged tree
``staged/whg/datasets/<id>/<stage>/places.*`` written by
``authorities/whg-places.py --dataset <id>``; the place_id prefix is
``whg:<id>:`` WITH the trailing colon, so ``whg:1`` can never match ``whg:12``:

    # add / replace one dataset
    python -m processing.index_namespace --namespace whg --dataset 1234 --es-host ... --replace --execute

    # retract (unpublish / delete): delete + strip only, never re-add. Refuses
    # while the staged sidecar still says the dataset is indexable.
    python -m processing.index_namespace --namespace whg --dataset 1234 --es-host ... --retract --execute

The write step REFUSES any dataset whose staged sidecar (per-dataset
``staged/whg/datasets/<id>/dataset.json``, else the namespace-wide
``staged/_aggregates/whg.datasets.json``) does not say ``dataset_status ==
"published"`` — never-index-pending, enforced here as well as at selection —
and, when ``processing.whg_selection.is_indexable`` is importable, that
predicate must agree (it is handed the sidecar entry as-is).
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

def _source_path(namespace: str, stage: str | None,
                 dataset: str | None = None) -> tuple[Path, str]:
    base = (dataset_staged_dir(dataset) if dataset
            else Path(STAGED_BASE_DIR) / namespace)
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


# ---------------------------------------------------------------------------
# Contributed WHG datasets (place#341): per-dataset source, prefix, sidecar
# ---------------------------------------------------------------------------

WHG_NAMESPACE = "whg"
_DATASET_ID_RE = re.compile(r"^\d+$")
# Per-dataset sidecar filenames tried, in order, inside ``dataset_staged_dir``.
# Either shape is accepted: a flat entry ``{"id": "whg:12", "dataset_status":
# ...}`` or the namespace-wide shape ``{"datasets": [entry, ...]}``.
DATASET_SIDECAR_CANDIDATES = ("dataset.json", "whg.datasets.json")
_WHG_DATASETS_SIDECAR = "whg.datasets.json"


def validate_dataset_id(dataset: str) -> str:
    """A dataset id is a decimal integer and nothing else.

    Checked before it is used in a path, a place_id prefix or a query, so a
    stray ``;`` or ``..`` can never reach any of them."""
    ds = str(dataset).strip()
    if not _DATASET_ID_RE.fullmatch(ds):
        raise SystemExit(f"--dataset must be a decimal integer id, got {dataset!r}")
    return ds


def dataset_place_prefix(dataset: str) -> str:
    """``whg:<id>:`` — the TRAILING COLON is the point: ``whg:1:`` matches
    ``whg:1:abc`` and not ``whg:12:abc``."""
    return f"{WHG_NAMESPACE}:{validate_dataset_id(dataset)}:"


def dataset_staged_dir(dataset: str, staged_base: str | os.PathLike | None = None) -> Path:
    """``staged/whg/datasets/<id>/`` — the per-dataset staged tree."""
    base = Path(staged_base) if staged_base is not None else Path(STAGED_BASE_DIR)
    return base / WHG_NAMESPACE / "datasets" / validate_dataset_id(dataset)


def _entry_for(payload: Any, wanted_id: str) -> dict | None:
    """Pick the sidecar entry for ``whg:<id>`` out of either sidecar shape."""
    if isinstance(payload, dict) and isinstance(payload.get("datasets"), list):
        for ds in payload["datasets"]:
            if isinstance(ds, dict) and str(ds.get("id")) == wanted_id:
                return ds
        return None
    if isinstance(payload, dict) and ("dataset_status" in payload or "id" in payload):
        ds_id = payload.get("id")
        if ds_id is None or str(ds_id) in (wanted_id, wanted_id.split(":", 1)[1]):
            return payload
    return None


def load_whg_dataset_meta(dataset: str,
                          staged_base: str | os.PathLike | None = None) -> dict | None:
    """The staged sidecar entry for one dataset, or ``None`` if no sidecar has it.

    Looks in the per-dataset tree first (``DATASET_SIDECAR_CANDIDATES``), then
    the namespace-wide ``staged/_aggregates/whg.datasets.json`` written by a
    full ``whg-places.py`` run. An unreadable file counts as absent — the
    callers fail closed on ``None``, which is the safe direction.
    """
    ds = validate_dataset_id(dataset)
    base = Path(staged_base) if staged_base is not None else Path(STAGED_BASE_DIR)
    wanted = f"{WHG_NAMESPACE}:{ds}"
    candidates = [dataset_staged_dir(ds, base) / name for name in DATASET_SIDECAR_CANDIDATES]
    candidates.append(base / "_aggregates" / _WHG_DATASETS_SIDECAR)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entry = _entry_for(payload, wanted)
        if entry is not None:
            return entry
    return None


def _import_is_indexable():
    """``processing.whg_selection.is_indexable`` if it exists, else ``None``.

    The selection module is being written alongside this one; its absence must
    not stop the sidecar's own ``dataset_status`` rule from being enforced."""
    try:
        from processing.whg_selection import is_indexable  # type: ignore
    except Exception:  # noqa: BLE001 — missing module or import-time error
        return None
    return is_indexable


_SELECTION_FIELDS = ("authority", "public", "ds_status")


def _carries_selection_fields(meta: dict) -> bool:
    """Does this sidecar entry carry what ``whg_selection.is_indexable`` reads?

    The sidecar ``whg-places.py`` writes today carries ``dataset_status`` —
    the RESULT of that selection, made at staging time over the discovery
    record — and none of the inputs (``lifecycle_state``, ``public``,
    ``authority``, ``ds_status``). Running the predicate over that shape is a
    second derivation of a stored field, and it answers "no" for every
    dataset because the inputs are absent, not because the dataset is
    ineligible. So the predicate is consulted only when the inputs are there.
    """
    return "lifecycle_state" in meta or all(k in meta for k in _SELECTION_FIELDS)


def dataset_indexable(meta: dict | None, *, indexable_fn=None) -> tuple[bool, str]:
    """Is this dataset currently selected for indexing? ``(bool, reason)``.

    The sidecar's ``dataset_status`` must be ``published`` (never-index-
    pending). When the entry also carries the selection inputs (see
    :func:`_carries_selection_fields`), ``is_indexable`` — injected, or
    imported from ``processing.whg_selection`` when present — must agree as
    well, so a v4 ``lifecycle_state`` recorded on the entry outranks a stale
    ``dataset_status``. A predicate that raises is treated as "no": the only
    safe reading of an exception in a gate that decides whether contributed
    data goes live.
    """
    if not isinstance(meta, dict):
        return False, "no staged sidecar entry for this dataset"
    status = str(meta.get("dataset_status") or "pending")
    if status != "published":
        return False, f"dataset_status={status!r}"
    fn = indexable_fn if indexable_fn is not None else _import_is_indexable()
    if fn is not None and _carries_selection_fields(meta):
        try:
            ok, reason = fn(meta)
        except Exception as exc:  # noqa: BLE001
            return False, f"is_indexable raised {exc!r}"
        if not ok:
            return False, f"whg_selection.is_indexable: {reason}"
    return True, "dataset_status='published'"


def assert_dataset_writable(dataset: str, meta: dict | None, *, indexable_fn=None) -> None:
    """The write-step gate (plan item 7): refuse to index a dataset the
    sidecar does not say is published."""
    ok, reason = dataset_indexable(meta, indexable_fn=indexable_fn)
    if not ok:
        raise SystemExit(
            f"REFUSING to index whg:{dataset}: {reason}. Only a dataset whose "
            f"staged sidecar says dataset_status='published' may be written to "
            f"the live index (never-index-pending)."
        )


def assert_dataset_retractable(dataset: str, meta: dict | None, *, indexable_fn=None) -> None:
    """``--retract`` is for unpublish / delete: refuse while the sidecar still
    selects the dataset for indexing, because a retraction of a live, published
    dataset is a re-ingest's job (``--replace``), not a deletion."""
    ok, reason = dataset_indexable(meta, indexable_fn=indexable_fn)
    if ok:
        raise SystemExit(
            f"REFUSING to retract whg:{dataset}: it is currently indexable "
            f"({reason}). Retraction is for a dataset that has been unpublished "
            f"or deleted; re-stage it so the sidecar reflects that, or use "
            f"--replace to re-ingest."
        )
    if meta is None:
        print(f"[retract] whg:{dataset}: no staged sidecar entry — treating as "
              f"not indexable (a deleted dataset has none)")
    else:
        print(f"[retract] whg:{dataset}: not indexable ({reason}) — retraction allowed")


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

# Replace/retract pre-pass: strip stale attestations under ``params.prefix``.
#
# Parametrised on TWO prefixes so one script serves both scopes. ``prefix`` is
# what is stripped: the whole namespace (``ukhc:``) or one contributed dataset
# (``whg:12:``). ``ns_prefix`` (``whg:``) decides whether the namespace entry
# and ``primary_namespace`` go too — only when NO attestation under the
# namespace survives the strip. For a namespace-wide strip the two prefixes
# are equal, so nothing survives and the behaviour is exactly the old one; for
# a dataset strip a toponym still attested by another whg dataset keeps ``whg``
# in ``namespaces``, which is what a dataset-scoped removal must do.
_STRIP_NS_SCRIPT = (
    "if (ctx._source.attestations != null) { "
    "ctx._source.attestations.removeIf(a -> a.startsWith(params.prefix)); } "
    "boolean still = false; "
    "if (ctx._source.attestations != null) { "
    "for (a in ctx._source.attestations) { "
    "if (a.startsWith(params.ns_prefix)) { still = true; break; } } } "
    "if (!still) { "
    "if (ctx._source.namespaces != null) { ctx._source.namespaces.removeIf(n -> n == params.ns); } "
    "if (params.ns.equals(ctx._source.primary_namespace)) { "
    "ctx._source.primary_namespace = (ctx._source.namespaces != null && ctx._source.namespaces.length > 0) "
    "? ctx._source.namespaces[0] : null; } }"
)


def strip_script_params(namespace: str, place_prefix: str) -> dict[str, str]:
    """The ``params`` for :data:`_STRIP_NS_SCRIPT` — one place to build them,
    so the namespace-wide and dataset-scoped callers cannot drift."""
    return {"prefix": place_prefix, "ns_prefix": f"{namespace}:", "ns": namespace}


# ---------------------------------------------------------------------------
# Prefix-scoped delete / strip, reporting counts WITH their denominators
# ---------------------------------------------------------------------------

def count_prefix(es, index: str, field: str, prefix: str) -> tuple[int, int]:
    """``(matching, total)`` for a prefix query on ``field`` in ``index``.

    Both numbers are returned because "0 matching" says nothing on its own: the
    same figure comes from an empty index, a wrong index and a clean one."""
    matching = es.count(index=index, body={"query": {"prefix": {field: prefix}}})["count"]
    total = es.count(index=index)["count"]
    return int(matching), int(total)


def delete_places_by_prefix(es, index: str, place_prefix: str, *, execute: bool) -> int | None:
    """delete_by_query on ``place_id`` prefix, counting before and after.

    Returns the number deleted when executing, else ``None``. The prefix is
    used verbatim — a dataset prefix must already carry its trailing colon."""
    before, total = count_prefix(es, index, "place_id", place_prefix)
    print(f"[places] place_id prefix {place_prefix!r}: {before:,} of {total:,} docs (before)")
    if not execute:
        print(f"[places] (dry-run) would delete_by_query those {before:,} docs")
        return None
    resp = es.options(request_timeout=3600).delete_by_query(
        index=index,
        body={"query": {"prefix": {"place_id": place_prefix}}},
        conflicts="proceed", refresh=False, slices="auto",
        wait_for_completion=True,
    )
    deleted = int(resp.get("deleted", 0))
    es.indices.refresh(index=index)
    after, total_after = count_prefix(es, index, "place_id", place_prefix)
    print(f"[places]   deleted {deleted:,}; prefix now matches {after:,} of "
          f"{total_after:,} docs (after)")
    if after:
        print(f"[places] ⚠ {after:,} doc(s) under {place_prefix!r} survived the "
              f"delete — do not treat this run as complete", file=sys.stderr)
    return deleted


def strip_attestations_by_prefix(es, index: str, namespace: str, place_prefix: str,
                                 *, execute: bool) -> int | None:
    """update_by_query stripping attestations under ``place_prefix`` from the
    toponyms index, counting the affected toponyms before and after."""
    before, total = count_prefix(es, index, "attestations", place_prefix)
    print(f"[toponyms] attestation prefix {place_prefix!r}: {before:,} of {total:,} "
          f"toponyms (before)")
    if not execute:
        print(f"[toponyms] (dry-run) would strip {place_prefix!r} attestations from "
              f"those {before:,} toponyms")
        return None
    resp = es.options(request_timeout=3600).update_by_query(
        index=index,
        body={
            "query": {"prefix": {"attestations": place_prefix}},
            "script": {"source": _STRIP_NS_SCRIPT, "lang": "painless",
                       "params": strip_script_params(namespace, place_prefix)},
        },
        conflicts="proceed", refresh=False, slices="auto",
        wait_for_completion=True,
    )
    updated = int(resp.get("updated", 0))
    es.indices.refresh(index=index)
    after, total_after = count_prefix(es, index, "attestations", place_prefix)
    print(f"[toponyms]   stripped from {updated:,} toponyms; prefix now matches "
          f"{after:,} of {total_after:,} (after)")
    if after:
        print(f"[toponyms] ⚠ {after:,} toponym(s) still attest {place_prefix!r} "
              f"after the strip — do not treat this run as complete", file=sys.stderr)
    return updated


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
                          uncovered, replace, execute, allow_missing_h3,
                          place_prefix=None, retract=False):
    """Index one namespace's places, streaming from ``doc_iter_factory()``.

    ``doc_iter_factory`` is a zero-argument callable returning a FRESH iterator
    over the staged docs — not a list, and not a single spent iterator. The
    counts come from :func:`scan_places`, which has already made one pass.

    ``place_prefix`` scopes the replace/retract delete (default the whole
    namespace, ``<ns>:``; a contributed dataset passes ``whg:<id>:``).
    ``retract`` performs the delete only and never re-adds.
    """
    place_prefix = place_prefix or f"{namespace}:"
    print(f"\n[places] target index: {index}")
    if retract:
        print(f"[places] RETRACT: delete_by_query place_id prefix {place_prefix!r} (no re-add)")
        delete_places_by_prefix(es, index, place_prefix, execute=execute)
        return

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
        print(f"[places] REPLACE: delete_by_query place_id prefix {place_prefix!r}")
        delete_places_by_prefix(es, index, place_prefix, execute=execute)

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
                            emit_new_toponyms=None, place_prefix=None, retract=False):
    """Augment the toponyms index with one namespace's (or dataset's) attestations.

    ``place_prefix`` scopes the replace/retract strip (default ``<ns>:``);
    ``retract`` strips and never re-adds — ``attest_records`` is ignored.
    """
    place_prefix = place_prefix or f"{namespace}:"
    indexed_at = datetime.now(timezone.utc).isoformat()
    print(f"\n[toponyms] target index: {index}")
    if retract:
        print(f"[toponyms] RETRACT: strip {place_prefix!r} attestations (no re-add)")
        strip_attestations_by_prefix(es, index, namespace, place_prefix, execute=execute)
        return

    topids = [r["toponym_id"] for r in attest_records]
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
        print(f"[toponyms] REPLACE: strip stale {place_prefix!r} attestations first "
              f"(update_by_query, prefix match)")
        strip_attestations_by_prefix(es, index, namespace, place_prefix, execute=execute)

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


def main(argv: list[str] | None = None, *, es_factory=None, indexable_fn=None) -> None:
    """CLI entry point.

    ``es_factory`` (``(host) -> client``) and ``indexable_fn`` (the
    ``processing.whg_selection.is_indexable`` predicate) are injection points
    for tests; production uses :class:`Elasticsearch` and the imported module.
    """
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", required=True, help="Authority namespace, e.g. ukhc")
    ap.add_argument("--dataset", metavar="ID",
                    help="One contributed WHG dataset (numeric id); only with "
                         "--namespace whg. Source = staged/whg/datasets/<ID>/, "
                         "scope = place_id prefix 'whg:<ID>:'")
    ap.add_argument("--es-host", required=True, help="Elasticsearch URL (prod: http://localhost:9201)")
    ap.add_argument("--source-stage", choices=SOURCE_STAGES,
                    help="Staged source stage (default: most-enriched available)")
    ap.add_argument("--replace", action="store_true",
                    help="Re-ingest: delete the namespace's (or dataset's) places and "
                         "strip its stale toponym attestations before adding")
    ap.add_argument("--retract", action="store_true",
                    help="Delete + strip ONLY, never re-add (unpublish / delete). "
                         "Requires --dataset; refuses while the staged sidecar still "
                         "says the dataset is indexable")
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
    args = ap.parse_args(argv)

    # --- contributed-dataset scope (place#341) --------------------------------
    dataset = place_prefix = None
    if args.dataset is not None:
        if args.namespace != WHG_NAMESPACE:
            raise SystemExit("--dataset is only valid with --namespace whg")
        dataset = validate_dataset_id(args.dataset)
        place_prefix = dataset_place_prefix(dataset)
    if args.retract:
        if dataset is None:
            raise SystemExit("--retract requires --dataset (it is a per-dataset "
                             "unpublish / delete, not a namespace operation)")
        if args.replace:
            raise SystemExit("--retract and --replace are mutually exclusive")

    es = (es_factory(args.es_host) if es_factory is not None
          else Elasticsearch(args.es_host, basic_auth=_es_basic_auth(), request_timeout=120))
    info = es.info()
    mode = "RETRACT" if args.retract else ("REPLACE" if args.replace else "ADD")
    print(f"ES {info['version']['number']} @ {args.es_host}  "
          f"mode={'EXECUTE' if args.execute else 'DRY-RUN'}  {mode}"
          + (f"  dataset=whg:{dataset} prefix={place_prefix!r}" if dataset else ""))

    # The sidecar gate. Consulted in BOTH modes so a dry-run shows the refusal
    # the execute run would make; it only aborts before a write.
    if dataset is not None:
        meta = load_whg_dataset_meta(dataset)
        if args.retract:
            assert_dataset_retractable(dataset, meta, indexable_fn=indexable_fn)
        else:
            ok, reason = dataset_indexable(meta, indexable_fn=indexable_fn)
            print(f"[gate] whg:{dataset} indexable={ok} ({reason})")
            if not ok and not args.execute:
                print(f"[gate] (dry-run) --execute would REFUSE: {reason}")

    if args.retract:
        # No staged source is read: a retracted dataset may have no tree at all.
        src_path = None
    else:
        src_path, stage = _source_path(args.namespace, args.source_stage, dataset)
        print(f"source: {src_path}  (stage={stage})")

    if not args.toponyms_only:
        if args.retract:
            n_docs = uncovered = 0
        else:
            n_docs, uncovered = scan_places(iter_place_docs(src_path))
            print(f"[places] scanned {n_docs:,} staged docs, {uncovered:,} with "
                  f"uncovered geometry")
        if dataset is not None and args.execute and not args.retract:
            # THE WRITE STEP: nothing has been written yet, and nothing will be
            # for a dataset the sidecar does not say is published.
            assert_dataset_writable(dataset, meta, indexable_fn=indexable_fn)
        places_index = resolve_concrete_index(es, PLACES_ALIAS)
        plan_and_index_places(es, places_index, args.namespace,
                              (lambda: iter_place_docs(src_path)) if src_path else (lambda: iter(())),
                              n_docs=n_docs, uncovered=uncovered,
                              replace=args.replace, execute=args.execute,
                              allow_missing_h3=args.allow_missing_h3,
                              place_prefix=place_prefix, retract=args.retract)
    if not args.places_only:
        if args.retract:
            attest_records = []
        else:
            if dataset is not None and args.execute:
                assert_dataset_writable(dataset, meta, indexable_fn=indexable_fn)
            # Built only when toponyms are actually written — this aggregation is a
            # whole-corpus group-by and is the remaining memory cost of the run.
            attest_records, _ctx = collect_attestations(iter_place_docs(src_path))
        toponyms_index = resolve_concrete_index(es, TOPONYMS_ALIAS)
        plan_and_index_toponyms(es, toponyms_index, args.namespace, attest_records,
                                replace=args.replace, execute=args.execute,
                                emit_new_toponyms=args.emit_new_toponyms,
                                place_prefix=place_prefix, retract=args.retract)

    if args.execute and not args.toponyms_only:
        es.indices.refresh(index=resolve_concrete_index(es, PLACES_ALIAS))
    if args.execute and not args.places_only:
        es.indices.refresh(index=resolve_concrete_index(es, TOPONYMS_ALIAS))
    if args.execute and not args.toponyms_only:
        _refresh_registry_aggregates(args.namespace)

    print("\nDone." + ("" if args.execute else "  (dry-run — no changes written)"))


if __name__ == "__main__":
    main()
