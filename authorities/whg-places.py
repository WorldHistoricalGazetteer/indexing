# authorities/whg-places.py

"""
Stage WHG-contributed Datasets/Collections (Batch 4c Phase 4).

Discovers datasets via the WHG Django reconcile API, fetches each one's
gzipped LPF FeatureCollection, and stages each feature as a place doc in
the ``whg`` namespace via ``processing.helpers.write_staged_place_doc``.

Per Master Plan + plan §"Batch 4c Phase 4":

* ``place_id = whg:<dataset_id>:<src_id>`` — the CONTRIBUTOR's own id for the
  record (LPF ``properties.src_id``), not WHG's Postgres key. The key changes
  when a dataset is re-ingested; src_id does not, and it is what WHG's own
  reconciliation service now emits, so the two sides mint the same identifier
  for the same place. src_id is not guaranteed unique within a dataset (a small
  number of legacy datasets carry duplicate records), so a repeat within one
  dataset takes a fourth segment — ``whg:<dataset_id>:<src_id>:<place_key>`` —
  rather than colliding on the ES document id and silently dropping a record.
  Features with no src_id at all fall back to ``whg:<dataset_id>:<place_key>``.
  See WorldHistoricalGazetteer/place#183, #172.
* ``dataset_id = whg:<dataset_id>`` — a per-Dataset/Collection sub-namespace
  so per-dataset re-staging and dataset_status filters are tractable.
* ``dataset_status = 'published'`` only for datasets that are indexable by
  ``processing.whg_selection.is_indexable`` (v4 ``lifecycle_state ==
  'published'`` AND ``public``, or the legacy ``authority AND public AND
  ds_status in {accessioning, indexed}`` when ``lifecycle_state`` is absent)
  **and** that Django's ``authority-datasets`` endpoint itself reports as
  ``published``. The client check only ever restricts what Django listed. A
  dataset that fails it is skipped and reported with its reason, or staged as
  ``dataset_status='pending'`` under ``--include-pending``; a ``--dataset``
  that names one is refused with exit 2 (place#341).
* ``--dataset <id>`` runs stage under ``staged/whg/datasets/<id>/extract/``
  with their own ``id_map.jsonl``, truncated first, so a one-dataset re-run
  replaces that dataset wholesale and never touches the full run's
  ``whg/extract/``.

Auth: token via the ``Authorization: Bearer <token>`` header, with
``?token=<token>`` URL fallback. Token sourced from
``processing.settings.WHG_API_TOKEN_FILE`` (default
``${IX1_BASE}/secrets/whg-api.token``) or the ``WHG_API_TOKEN`` env var.

The LPF stream is parsed feature-by-feature via ``ijson`` so multi-million
record datasets stay memory-bounded.

Two artefacts leave here besides the staged place docs:

* **the id map** (``extract/id_map.jsonl``) — ``place_key → place_id`` for every
  record staged. The id rule above is not derivable in SQL (the duplicate case
  turns on stream order), so anything else that has to name a whg place joins
  through this file rather than re-deriving the id. See
  ``processing.whg_id_map`` and ``developer/plan-completion-2026-08-31.md`` §2.3.
* **the full geometries** — written to the VAST geom store, like every other
  areal authority. Until 31 August 2026 this script passed ``geom_key`` to
  ``enrich_geometry`` without ever configuring a ``GeomStoreWriter``, so
  ``enrich_geometry`` returned ``has_geom=False`` and dropped the polygon on the
  floor: 1,248 ``area`` and 1,072 ``line`` whg geometries in production carry no
  retrievable shape. They were never written, not lost.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator

import requests

from processing.helpers import enrich_geometry, write_staged_place_doc
from processing.temporal import normalise_timespans
from processing.whg_id_map import ID_MAP_FILENAME, IdMapWriter, default_id_map_path
from processing.whg_selection import is_indexable, summarise
from processing.settings import (
    GEOM_STORE_STAGING_DIR,
    WHG_API_BASE_URL,
    WHG_API_TOKEN_FILE,
    WHG_HTTP_INITIAL_BACKOFF,
    WHG_HTTP_MAX_RETRIES,
    WHG_HTTP_TIMEOUT,
)

try:
    import ijson  # streaming JSON parser
    _IJSON_AVAILABLE = True
except ImportError:
    _IJSON_AVAILABLE = False


WHG_NAMESPACE = "whg"
DISCOVERY_PATH = "/reconcile/authority-datasets"
LPF_PATH_TEMPLATE = "/entity/dataset:{dataset_id}/api?filetype=lpf"

# Per-dataset staging (place#341). A ``--dataset`` run writes under
# ``{STAGED_BASE_DIR}/whg/datasets/<id>/extract/`` instead of the shared
# ``{STAGED_BASE_DIR}/whg/extract/``, so it can neither append to nor clobber
# the other datasets' ``places.jsonl`` and ``id_map.jsonl``. The full run keeps
# the shared paths exactly as before.
PER_DATASET_SUBDIR = "datasets"


def per_dataset_staged_namespace(dataset_sub_id: int | str) -> str:
    """The ``namespace`` argument handed to ``write_staged_place_doc`` for a
    per-dataset run. That helper resolves its output as
    ``{STAGED_BASE_DIR}/{namespace}/extract/places.jsonl``, so a relative
    path here lands the extract in the per-dataset directory while the docs
    themselves still carry ``namespace='whg'``."""
    return f"{WHG_NAMESPACE}/{PER_DATASET_SUBDIR}/{dataset_sub_id}"


def per_dataset_extract_dir(dataset_sub_id: int | str) -> Path:
    """``{STAGED_BASE_DIR}/whg/datasets/<id>/extract`` — the directory a
    per-dataset run owns outright and may truncate before writing."""
    return default_id_map_path(dataset=dataset_sub_id).parent


# ----------------------------------------------------------------------------
# Auth + HTTP
# ----------------------------------------------------------------------------


def _resolve_token(explicit: str | None = None) -> str | None:
    """Return the API token from (in order): CLI arg, env, token file."""
    if explicit:
        return explicit
    env_tok = os.environ.get("WHG_API_TOKEN")
    if env_tok:
        return env_tok.strip()
    try:
        return Path(WHG_API_TOKEN_FILE).expanduser().read_text(
            encoding="utf-8"
        ).strip()
    except FileNotFoundError:
        return None


def _make_session(token: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "WHG-Indexer/1.0 (https://whgazetteer.org/)",
        "Accept": "application/geo+json, application/json;q=0.9",
    })
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def _request_with_retry(
    session: requests.Session,
    url: str,
    *,
    stream: bool = False,
    timeout: int = WHG_HTTP_TIMEOUT,
    max_retries: int = WHG_HTTP_MAX_RETRIES,
    initial_backoff: float = WHG_HTTP_INITIAL_BACKOFF,
) -> requests.Response:
    import time as _time
    backoff = initial_backoff
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout, stream=stream)
            if resp.status_code >= 500 and attempt < max_retries:
                _time.sleep(backoff); backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            _time.sleep(backoff); backoff *= 2
    if last_exc:
        raise last_exc
    raise RuntimeError(f"unreachable: {url}")


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------


def discover_authority_datasets(
    session: requests.Session,
    *,
    base_url: str = WHG_API_BASE_URL,
    include_pending: bool = False,
) -> list[dict[str, Any]]:
    """Return the list of datasets from ``/reconcile/authority-datasets``.

    Per Django commit ``api/reconcile.py::AuthorityDatasetsView`` (this
    repo's Django side), each entry now carries::

        {"id": int, "title": str, "label": str|None, "description": str|None,
         "ds_status": str|None, "public": bool|None, "authority": bool|None,
         "owner_id": int|None, "place_count": int,
         "dataset_status": "published"|"pending"}

    ``dataset_status`` is the field this script consumes downstream — Django
    derives it from ``(authority and public and ds_status in {accessioning,
    indexed})`` so the rebuild doesn't have to re-implement the rule.

    Pass ``include_pending=True`` to add ``?include_pending=true`` to the
    URL — Django then includes datasets whose ``ds_status`` is anything
    other than ``seed`` / ``format_error`` (so half-uploaded / invalid
    datasets stay out of the index).
    """
    url = base_url.rstrip("/") + DISCOVERY_PATH
    if include_pending:
        url += "?include_pending=true"
    resp = _request_with_retry(session, url)
    body = resp.json()
    return list(body.get("result") or [])


# ----------------------------------------------------------------------------
# LPF stream → staged docs
# ----------------------------------------------------------------------------


def _open_lpf_stream(
    session: requests.Session,
    *,
    base_url: str,
    dataset_id: int,
):
    """Return a binary stream of *decompressed* LPF JSON.

    The Django endpoint sets ``Content-Encoding: gzip`` and may also gzip
    the body explicitly inside the response (single continuous gzip member
    written via zlib). ``requests`` with ``stream=True`` does NOT
    auto-decompress when ``Content-Encoding`` is present, because we read
    ``raw``. We wrap the raw stream in ``gzip.GzipFile`` to handle either
    case.
    """
    url = base_url.rstrip("/") + LPF_PATH_TEMPLATE.format(dataset_id=dataset_id)
    resp = _request_with_retry(session, url, stream=True, timeout=WHG_HTTP_TIMEOUT * 4)
    # Force raw bytes — let GzipFile decompress, regardless of whether
    # requests already decompressed via Content-Encoding handling.
    resp.raw.decode_content = False
    return gzip.GzipFile(fileobj=resp.raw)


def iter_features(stream) -> Iterator[dict[str, Any]]:
    """Yield each LPF Feature from the open binary stream."""
    if not _IJSON_AVAILABLE:
        raise RuntimeError(
            "ijson is required for streaming LPF parse. pip install ijson"
        )
    yield from ijson.items(stream, "features.item")


# ----------------------------------------------------------------------------
# Mapping LPF feature → staged place doc
# ----------------------------------------------------------------------------


def _toponyms_from_lpf(names: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map LPF ``properties.names`` to staged ``toponyms`` entries.

    LPF doesn't carry an explicit lang on every name, so we tag with
    ``und`` unless a citation supplies a ``lang`` field. The merger in
    Batch 9 (toponyms rebuild) deduplicates on ``toponym_id`` already.
    """
    out: list[dict[str, Any]] = []
    for n in names or []:
        if not isinstance(n, dict):
            continue
        toponym = n.get("toponym")
        if not isinstance(toponym, str) or not toponym:
            continue
        lang = "und"
        for cite in (n.get("citations") or []):
            if isinstance(cite, dict) and isinstance(cite.get("lang"), str):
                lang = cite["lang"]
                break
        entry: dict[str, Any] = {"toponym_id": f"{toponym}@{lang}"}
        when = n.get("when")
        if isinstance(when, dict) and when.get("timespans"):
            entry["timespans"] = when["timespans"]
        out.append(entry)
    return out


def _types_from_lpf(types: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map LPF ``properties.types`` to staged type entries.

    The shape already aligns with the staged schema (identifier/label/
    sourceLabel); we just normalise missing fields to keep the staged
    Parquet schema stable.
    """
    out: list[dict[str, Any]] = []
    for t in types or []:
        if not isinstance(t, dict):
            continue
        identifier = t.get("identifier")
        if not isinstance(identifier, str):
            continue
        out.append({
            "identifier": identifier,
            "label": t.get("label", ""),
            "sourceLabel": t.get("sourceLabel", ""),
        })
    return out


def _relations_from_lpf(related: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map LPF ``properties.related`` to staged ``relations`` entries.

    LPF uses ``relation_type`` + ``relation_to``; staged shape uses
    ``relation_type`` + ``related_place_id``.
    """
    out: list[dict[str, Any]] = []
    for r in related or []:
        if not isinstance(r, dict):
            continue
        rt = r.get("relation_type")
        rp = r.get("relation_to")
        if not isinstance(rt, str) or not isinstance(rp, str):
            continue
        out.append({
            "relation_type": rt,
            "related_place_id": rp,
            "label": r.get("label"),
        })
    return out


def _links_from_lpf(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for l in links or []:
        if not isinstance(l, dict):
            continue
        ident = l.get("identifier")
        if not isinstance(ident, str):
            continue
        out.append({
            "type": l.get("type", ""),
            "identifier": ident,
        })
    return out


def _timespans_from_whens(whens: list[Any]) -> list[dict[str, Any]]:
    """Flatten LPF ``properties.whens`` into a flat list of timespans for
    application to geometries (matches the staged-doc convention)."""
    out: list[dict[str, Any]] = []
    for w in whens or []:
        if not isinstance(w, dict):
            continue
        ts = w.get("timespans")
        if isinstance(ts, list):
            out.extend(t for t in ts if isinstance(t, dict))
    # LPF years arrive as bare numbers in some datasets and ISO date strings
    # in others. Passing that through verbatim is what mapped
    # `timespans.start.earliest` as `text` in the live index (dynamic mapping
    # from the first value seen), made 208,937 whg docs read as undated, and
    # broke the parquet sidecar's schema inference. Normalise at the write
    # path so only ints ever leave here. See place#164.
    return normalise_timespans(out)


def _mint_place_id(dataset_sub_id, src_id, entity_id, *, seen_src_ids=None, stats=None) -> str:
    """``whg:<dataset_id>:<src_id>`` — the identifier WHG's reconciliation service
    emits for the same record, so an id minted here dereferences there and vice versa.

    Two edge cases, both rare and both counted rather than swallowed:

    * **no src_id** → fall back to the WHG place key, which is always present.
    * **duplicate src_id within the dataset** → append the place key as a fourth
      segment. src_id is the contributor's identifier and a handful of legacy
      datasets carry genuinely duplicated records; without this the second one
      would overwrite the first on the ES document id and vanish from the index.
    """
    src = str(src_id or "").strip()
    if not src:
        if stats is not None:
            stats["no_src_id"] = stats.get("no_src_id", 0) + 1
        return f"{WHG_NAMESPACE}:{dataset_sub_id}:{entity_id}"
    if seen_src_ids is not None:
        if src in seen_src_ids:
            if stats is not None:
                stats["duplicate_src_id"] = stats.get("duplicate_src_id", 0) + 1
            return f"{WHG_NAMESPACE}:{dataset_sub_id}:{src}:{entity_id}"
        seen_src_ids.add(src)
    return f"{WHG_NAMESPACE}:{dataset_sub_id}:{src}"


def lpf_feature_to_staged_doc(
    feature: dict[str, Any],
    *,
    dataset_sub_id: int,
    dataset_status: str = "published",
    seen_src_ids: set[str] | None = None,
    stats: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Map one LPF feature to a staged place doc.

    Returns ``None`` when the feature lacks an ``id`` (rare; rejected at
    parse time so the staged corpus stays clean).

    ``seen_src_ids`` accumulates the src_ids already staged for THIS dataset, so a
    duplicate can be given a distinct id instead of overwriting its twin; pass None
    for one-off conversions where that check doesn't apply.
    """
    entity_id = feature.get("id")
    if entity_id is None:
        return None
    props = feature.get("properties") or {}
    geometry = feature.get("geometry")

    place_id = _mint_place_id(
        dataset_sub_id, props.get("src_id"), entity_id,
        seen_src_ids=seen_src_ids, stats=stats,
    )
    dataset_id = f"{WHG_NAMESPACE}:{dataset_sub_id}"
    timespans = _timespans_from_whens(props.get("whens") or [])

    doc: dict[str, Any] = {
        "place_id": place_id,
        "namespace": WHG_NAMESPACE,
        "dataset_id": dataset_id,
        "dataset_status": dataset_status,
        "title": props.get("title", ""),
        "toponyms": _toponyms_from_lpf(props.get("names") or []),
        "types": _types_from_lpf(props.get("types") or []),
        "relations": _relations_from_lpf(props.get("related") or []),
        "links": _links_from_lpf(props.get("links") or []),
    }
    ccodes = props.get("ccodes")
    if isinstance(ccodes, list):
        doc["ccodes"] = [c for c in ccodes if isinstance(c, str)]

    if isinstance(geometry, dict):
        # Strip GeometryCollections to their first geometry (keeps the
        # staged shape consistent with other authority scripts; LPF wraps
        # multi-geometry places this way).
        if geometry.get("type") == "GeometryCollection":
            geoms = geometry.get("geometries") or []
            if geoms:
                geometry = geoms[0]
            else:
                geometry = None

        if geometry:
            geom_entry = enrich_geometry(
                geometry,
                timespans=timespans or None,
                geom_key=f"{place_id}_0",
            )
            if geom_entry:
                doc["geometries"] = [geom_entry]

    return doc


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------


def stage_dataset(
    session: requests.Session,
    *,
    dataset_sub_id: int,
    base_url: str = WHG_API_BASE_URL,
    dataset_status: str = "published",
    limit: int | None = None,
    id_map: IdMapWriter | None = None,
    staged_namespace: str = WHG_NAMESPACE,
) -> dict[str, Any]:
    """Stage one WHG dataset to ``staged/{staged_namespace}/extract/places.jsonl``.

    ``id_map``, when given, records the ``place_key → place_id`` decision for
    every doc written, so consumers can join rather than re-mint. Optional only
    so a one-off conversion can skip it; the driver always supplies one.

    ``staged_namespace`` is where the docs land, not what they say: it is
    ``whg`` for the full run and :func:`per_dataset_staged_namespace` for a
    ``--dataset`` run; the docs carry ``namespace='whg'`` either way.

    Returns counts: ``{features_seen, docs_written, docs_skipped}``.
    """
    print(f"  Fetching LPF for dataset:{dataset_sub_id} ...")
    stream = _open_lpf_stream(session, base_url=base_url, dataset_id=dataset_sub_id)

    seen = 0
    written = 0
    skipped = 0
    seen_src_ids: set[str] = set()   # per dataset — src_ids are only unique within one
    id_stats: dict[str, int] = {}
    try:
        for feature in iter_features(stream):
            seen += 1
            if limit is not None and seen > limit:
                break
            try:
                doc = lpf_feature_to_staged_doc(
                    feature,
                    dataset_sub_id=dataset_sub_id,
                    dataset_status=dataset_status,
                    seen_src_ids=seen_src_ids,
                    stats=id_stats,
                )
            except Exception as exc:
                skipped += 1
                if skipped <= 5:
                    print(f"    skipped feature: {exc}", file=sys.stderr)
                continue
            if doc is None:
                skipped += 1
                continue
            write_staged_place_doc(staged_namespace, doc)
            if id_map is not None:
                # The WHG place key is the LPF feature id; that is what the
                # contributor-link tables in DO Postgres hold.
                id_map.record(dataset_sub_id, feature.get("id"), doc["place_id"])
            written += 1
            if written % 5_000 == 0:
                sys.stdout.write(
                    f"\r    [whg:{dataset_sub_id}] written {written:,} / seen {seen:,}"
                )
                sys.stdout.flush()
    finally:
        try:
            stream.close()
        except Exception:
            pass

    print()  # newline after progress line
    # Report the id edge cases per dataset — a run that quietly re-minted a chunk of
    # a dataset under fallback ids should say so, not look identical to a clean one.
    if id_stats:
        print(f"    [whg:{dataset_sub_id}] id notes: "
              + ", ".join(f"{k}={v:,}" for k, v in sorted(id_stats.items())))
    return {"features_seen": seen, "docs_written": written, "docs_skipped": skipped,
            **{k: v for k, v in id_stats.items()}}


class DatasetNotIndexable(SystemExit):
    """``--dataset`` named something this run will not stage as indexable.

    A ``SystemExit`` with code 2 so the sbatch / reingest caller sees a
    non-zero exit rather than a clean run that staged nothing.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(2)

    def __str__(self) -> str:
        return "refusing to stage: " + "; ".join(self.problems)


def select_datasets(
    datasets: list[dict[str, Any]],
    *,
    include_pending: bool = False,
) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, Any]]]:
    """Decide, per discovered dataset, whether and with which
    ``dataset_status`` it is staged.

    Returns ``(to_stage, rejected)``: ``to_stage`` is ``[(dataset,
    effective_status)]`` and ``rejected`` is a list of
    ``{"id", "title", "reason", "django_dataset_status"}``.

    The decision is ``processing.whg_selection.is_indexable`` on the record's
    own ``lifecycle_state`` / ``public`` / ``authority`` / ``ds_status``,
    **intersected** with Django's ``dataset_status``. The client check can only
    further restrict what Django listed, never admit a dataset Django called
    pending — and, equally, it must not reject a ``published`` non-authority
    dataset Django lists, which is why :func:`is_indexable`'s published branch
    does not require ``authority``.

    * indexable here **and** ``published`` by Django → staged as ``published``;
    * anything else → skipped with a reason, or, under ``include_pending``,
      staged with ``dataset_status='pending'``.
    """
    to_stage: list[tuple[dict[str, Any], str]] = []
    rejected: list[dict[str, Any]] = []
    for ds in datasets:
        if not isinstance(ds.get("id"), int):
            continue
        ok, reason = is_indexable(ds)
        # Django's AuthorityDatasetsView derives this; default to 'pending'
        # if the field is absent (older Django build).
        django_status = str(ds.get("dataset_status") or "pending")
        if ok and django_status != "published":
            ok, reason = False, f"django_dataset_status: {django_status}"
        if ok:
            to_stage.append((ds, "published"))
        elif include_pending:
            to_stage.append((ds, "pending"))
        else:
            rejected.append({
                "id": ds["id"],
                "title": ds.get("title", ""),
                "reason": reason,
                "django_dataset_status": django_status,
            })
    return to_stage, rejected


def _refuse_if_named_dataset_not_indexable(
    dataset_filter: list[int],
    discovered: list[dict[str, Any]],
    to_stage: list[tuple[dict[str, Any], str]],
    rejected: list[dict[str, Any]],
) -> None:
    """A ``--dataset`` run is a request to index *that* dataset, so a named id
    that is not discovered, or is discovered but not indexable, is an error
    (exit 2), not a quiet no-op. ``include_pending`` turns the second case into
    a ``pending`` staging and so never reaches here with it."""
    discovered_ids = {d.get("id") for d in discovered}
    staged_ids = {d.get("id") for d, _ in to_stage}
    reasons = {r["id"]: r["reason"] for r in rejected}
    problems: list[str] = []
    for ds_id in dataset_filter:
        if ds_id not in discovered_ids:
            problems.append(f"whg:{ds_id} not returned by {DISCOVERY_PATH}")
        elif ds_id not in staged_ids:
            problems.append(f"whg:{ds_id} not indexable ({reasons.get(ds_id, '?')})")
    if problems:
        raise DatasetNotIndexable(problems)


def stage_all_authority_datasets(
    *,
    base_url: str = WHG_API_BASE_URL,
    token: str | None = None,
    limit_features_per_dataset: int | None = None,
    dataset_filter: list[int] | None = None,
    include_pending: bool = False,
) -> dict[str, Any]:
    """Discover + stage every indexable dataset.

    Selection is :func:`select_datasets` — ``whg_selection.is_indexable``
    re-checked client-side beside Django's ``dataset_status``. When
    ``include_pending=True`` the discovery call also returns datasets whose
    ``ds_status`` is anything other than ``seed`` / ``format_error``, and the
    ones that are not indexable are staged with ``dataset_status='pending'``
    instead of being skipped.

    With ``dataset_filter`` the run is **per-dataset**: each named dataset is
    staged under ``staged/whg/datasets/<id>/extract/`` with its own
    ``id_map.jsonl``, both truncated first, so the run replaces that dataset
    wholesale (a repeated-``src_id`` suffix depends on stream order, so a
    partial patch could never be right) and leaves the shared full-run
    ``extract/`` untouched. A named id that is not indexable raises
    :class:`DatasetNotIndexable` before anything is written.
    """
    token = _resolve_token(token)
    session = _make_session(token)
    per_dataset_mode = bool(dataset_filter)

    print("=" * 78)
    print("WHG-PLACES — STAGED EXTRACTION")
    print("=" * 78)
    print(f"WHG API: {base_url}")
    print(f"Auth:    {'bearer-token' if token else 'unauthenticated'}")
    print(f"Pending: {'included' if include_pending else 'excluded'}")
    print(f"Layout:  {'per-dataset' if per_dataset_mode else 'full run'}")

    discovered = discover_authority_datasets(
        session, base_url=base_url, include_pending=include_pending,
    )
    datasets = discovered
    if dataset_filter:
        keep = set(dataset_filter)
        datasets = [d for d in discovered if d.get("id") in keep]
    print(f"Discovered {len(discovered)} datasets"
          + (f", {len(datasets)} after --dataset filter." if dataset_filter else "."))

    selection = summarise(datasets)
    to_stage, rejected = select_datasets(datasets, include_pending=include_pending)
    print(f"Selection: {json.dumps(selection, sort_keys=True)}")
    for r in rejected:
        print(f"  - skipping whg:{r['id']} ({r['title']}): {r['reason']}")
    if dataset_filter:
        _refuse_if_named_dataset_not_indexable(
            dataset_filter, discovered, to_stage, rejected)

    per_dataset: dict[str, dict[str, Any]] = {}
    totals = {"features_seen": 0, "docs_written": 0, "docs_skipped": 0}

    # Full geometries go to the VAST store, as for every other areal authority.
    # GEOM_STORE_STAGING_DIR is read from the environment at import time, so a
    # run that must keep its staging output away from a concurrent merge points
    # that variable somewhere private before starting.
    from processing.geom_store import GeomStoreWriter, configure_module_writer

    print(f"Geom staging: {GEOM_STORE_STAGING_DIR}")
    id_map_file: Path | None = None
    if per_dataset_mode:
        print("Id map:       per dataset, "
              f"{default_id_map_path(dataset='<id>')}")
    else:
        id_map_file = default_id_map_path()
        print(f"Id map:       {id_map_file}")

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, WHG_NAMESPACE) as gsw:
        configure_module_writer(gsw)
        try:
            if per_dataset_mode:
                _stage_each_per_dataset(
                    to_stage,
                    session=session,
                    base_url=base_url,
                    limit_features_per_dataset=limit_features_per_dataset,
                    per_dataset=per_dataset,
                    totals=totals,
                )
                id_map_entries = sum(
                    int(m.get("id_map_entries") or 0) for m in per_dataset.values())
            else:
                assert id_map_file is not None
                with IdMapWriter(id_map_file) as id_map:
                    _stage_each(
                        to_stage,
                        session=session,
                        base_url=base_url,
                        limit_features_per_dataset=limit_features_per_dataset,
                        id_map=id_map,
                        per_dataset=per_dataset,
                        totals=totals,
                    )
                id_map_entries = id_map.count
        finally:
            configure_module_writer(None)

    print(f"\nGeometries written to the VAST store: {gsw.count:,}")
    if per_dataset_mode:
        print(f"Id-map entries written: {id_map_entries:,} across "
              f"{len(per_dataset)} per-dataset maps")
    else:
        print(f"Id-map entries written: {id_map_entries:,} → {id_map_file}")

    summary = {
        "discovered": len(datasets),
        "layout": "per-dataset" if per_dataset_mode else "full",
        "selection": selection,
        "rejected": rejected,
        "per_dataset": per_dataset,
        "totals": totals,
        "id_map_path": (
            {ds_id: m.get("id_map_path") for ds_id, m in per_dataset.items()}
            if per_dataset_mode else str(id_map_file)
        ),
        "id_map_entries": id_map_entries,
        "geom_store_entries": gsw.count,
    }
    _write_datasets_sidecar(per_dataset, summary, merge=per_dataset_mode)
    return summary


def _stage_one(
    ds: dict[str, Any],
    ds_status: str,
    *,
    session: requests.Session,
    base_url: str,
    limit_features_per_dataset: int | None,
    id_map: IdMapWriter,
    staged_namespace: str,
) -> dict[str, Any] | None:
    """Stage one selected dataset and return its metrics (``None`` on failure,
    which is printed, not raised, so one bad feed does not abort the run)."""
    ds_id = ds["id"]
    title = ds.get("title", "")
    place_count = ds.get("place_count") or 0
    print(
        f"\n{title} (whg:{ds_id}, place_count={place_count:,}, "
        f"status={ds_status}):"
    )
    try:
        metrics = stage_dataset(
            session,
            dataset_sub_id=ds_id,
            base_url=base_url,
            dataset_status=ds_status,
            limit=limit_features_per_dataset,
            id_map=id_map,
            staged_namespace=staged_namespace,
        )
    except Exception as exc:
        print(f"  ✗ {ds_id}: {exc}", file=sys.stderr)
        return None
    # Carry the discovery metadata onto the per-dataset metrics so the
    # Batch 11 inventory push has dataset_id-keyed name / description /
    # owner_user_id without a second discovery call.
    metrics["title"] = title
    metrics["description"] = ds.get("description")
    metrics["dataset_status"] = ds_status
    metrics["lifecycle_state"] = ds.get("lifecycle_state")
    metrics["selection_reason"] = is_indexable(ds)[1]
    metrics["owner_user_id"] = ds.get("owner_id")
    # Owner-supplied per-place source-link template (place#121), carried
    # through to the inventory push so the registry (and Atlas popups) get it.
    metrics["web_item"] = ds.get("web_item")
    return metrics


def _accumulate(metrics: dict[str, Any], ds_id: int,
                per_dataset: dict[str, dict[str, Any]],
                totals: dict[str, int]) -> None:
    per_dataset[str(ds_id)] = metrics
    for k in ("features_seen", "docs_written", "docs_skipped",
              "no_src_id", "duplicate_src_id"):
        totals[k] = totals.get(k, 0) + int(metrics.get(k) or 0)
    print(f"  ✓ {ds_id}: {metrics}")


def _stage_each(
    to_stage: list[tuple[dict[str, Any], str]],
    *,
    session: requests.Session,
    base_url: str,
    limit_features_per_dataset: int | None,
    id_map: IdMapWriter,
    per_dataset: dict[str, dict[str, Any]],
    totals: dict[str, int],
) -> None:
    """Full-run loop: every selected dataset into the shared ``whg/extract/``
    and the one shared id map, accumulating metrics in place."""
    for ds, ds_status in to_stage:
        metrics = _stage_one(
            ds, ds_status, session=session, base_url=base_url,
            limit_features_per_dataset=limit_features_per_dataset,
            id_map=id_map, staged_namespace=WHG_NAMESPACE,
        )
        if metrics is not None:
            _accumulate(metrics, ds["id"], per_dataset, totals)


def _truncate_per_dataset_extract(dataset_sub_id: int) -> Path:
    """Remove the per-dataset ``places.jsonl`` and ``id_map.jsonl`` so the run
    that follows replaces the dataset wholesale. Only this dataset's directory
    is touched; the shared ``whg/extract/`` is never on this path."""
    extract_dir = per_dataset_extract_dir(dataset_sub_id)
    extract_dir.mkdir(parents=True, exist_ok=True)
    for name in ("places.jsonl", ID_MAP_FILENAME):
        try:
            (extract_dir / name).unlink()
        except FileNotFoundError:
            pass
    return extract_dir


def _stage_each_per_dataset(
    to_stage: list[tuple[dict[str, Any], str]],
    *,
    session: requests.Session,
    base_url: str,
    limit_features_per_dataset: int | None,
    per_dataset: dict[str, dict[str, Any]],
    totals: dict[str, int],
) -> None:
    """``--dataset`` loop: each dataset into its own
    ``whg/datasets/<id>/extract/`` with its own id map, opened fresh (truncated,
    not appended) per dataset."""
    for ds, ds_status in to_stage:
        ds_id = ds["id"]
        extract_dir = _truncate_per_dataset_extract(ds_id)
        map_path = default_id_map_path(dataset=ds_id)
        with IdMapWriter(map_path) as id_map:
            metrics = _stage_one(
                ds, ds_status, session=session, base_url=base_url,
                limit_features_per_dataset=limit_features_per_dataset,
                id_map=id_map, staged_namespace=per_dataset_staged_namespace(ds_id),
            )
        if metrics is None:
            continue
        metrics["extract_dir"] = str(extract_dir)
        metrics["id_map_path"] = str(map_path)
        metrics["id_map_entries"] = id_map.count
        _accumulate(metrics, ds_id, per_dataset, totals)


def _sidecar_entry(ds_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{WHG_NAMESPACE}:{ds_id}",
        "name": meta.get("title") or f"WHG dataset {ds_id}",
        "description": meta.get("description"),
        "dataset_status": meta.get("dataset_status") or "pending",
        "owner_user_id": meta.get("owner_user_id"),
        "record_count": int(meta.get("docs_written") or 0),
        **({"web_item": meta["web_item"]} if meta.get("web_item") else {}),
    }


def _write_datasets_sidecar(
    per_dataset: dict[str, dict[str, Any]],
    summary: dict[str, Any],
    *,
    merge: bool = False,
) -> None:
    """Sidecar consumed by Batch 11 push_gazetteer_inventory: one inventory
    entry per WHG dataset (class='dataset', owner_user_id, status). Stable
    contract: ``staged/_aggregates/whg.datasets.json``.

    ``merge=True`` (per-dataset runs) upserts this run's entries into the
    existing file and keeps every other dataset's entry — a one-dataset run
    must not replace the 48-entry sidecar with a 1-entry one. The full run
    rewrites the file, as it always has.
    """
    try:
        from processing.settings import STAGED_BASE_DIR
        sidecar_path = (
            Path(STAGED_BASE_DIR) / "_aggregates" / "whg.datasets.json"
        )
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        entries = {ds_id: _sidecar_entry(ds_id, meta)
                   for ds_id, meta in per_dataset.items()}
        if merge and sidecar_path.is_file():
            existing = json.loads(sidecar_path.read_text(encoding="utf-8"))
            kept = [
                e for e in (existing.get("datasets") or [])
                if isinstance(e, dict)
                and str(e.get("id", "")).split(":", 1)[-1] not in entries
            ]
            datasets_out = kept + list(entries.values())
        else:
            datasets_out = list(entries.values())
        sidecar_payload = {"namespace": WHG_NAMESPACE, "datasets": datasets_out}
        sidecar_path.write_text(
            json.dumps(sidecar_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        summary["sidecar_path"] = str(sidecar_path)
    except Exception as exc:
        print(f"WARNING: failed to write whg datasets sidecar: {exc}",
              file=sys.stderr)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Stage WHG-contributed datasets via the Django reconcile API"
    )
    parser.add_argument("--base-url", default=WHG_API_BASE_URL,
                        help=f"WHG API base URL (default {WHG_API_BASE_URL})")
    parser.add_argument("--token",
                        help="Bearer token (default: WHG_API_TOKEN env or "
                             "WHG_API_TOKEN_FILE)")
    parser.add_argument("--dataset", action="append", type=int,
                        help="Restrict to one or more numeric dataset ids "
                             "(default: all authority datasets)")
    parser.add_argument("--limit", type=int,
                        help="Cap features per dataset (smoke testing only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover only; do not fetch LPF or stage docs")
    parser.add_argument("--include-pending", action="store_true",
                        help="Also stage datasets that aren't authority=True yet "
                             "(any ds_status except seed/format_error). Their "
                             "staged docs carry dataset_status='pending'.")
    args = parser.parse_args()

    if args.dry_run:
        token = _resolve_token(args.token)
        datasets = discover_authority_datasets(
            _make_session(token), base_url=args.base_url,
            include_pending=args.include_pending,
        )
        if args.dataset:
            keep = set(args.dataset)
            datasets = [d for d in datasets if d.get("id") in keep]
        to_stage, rejected = select_datasets(
            datasets, include_pending=args.include_pending)
        print(json.dumps({
            "discovered": datasets,
            "selection": summarise(datasets),
            "would_stage": [
                {"id": d["id"], "dataset_status": status} for d, status in to_stage
            ],
            "rejected": rejected,
        }, indent=2))
        return

    summary = stage_all_authority_datasets(
        base_url=args.base_url,
        token=args.token,
        limit_features_per_dataset=args.limit,
        dataset_filter=args.dataset,
        include_pending=args.include_pending,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
