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
* ``dataset_status = 'published'`` for everything returned by the
  ``authority-datasets`` discovery endpoint (it filters on
  ``Dataset.authority=True``, which is the eligibility flag for
  accessioned/approved datasets). Pending submissions are out of scope for
  this script — they are surfaced via a separate endpoint once Django adds
  it.

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
from processing.whg_id_map import IdMapWriter, default_id_map_path
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
) -> dict[str, Any]:
    """Stage one WHG dataset to ``staged/whg/extract/places.jsonl``.

    ``id_map``, when given, records the ``place_key → place_id`` decision for
    every doc written, so consumers can join rather than re-mint. Optional only
    so a one-off conversion can skip it; the driver always supplies one.

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
            write_staged_place_doc(WHG_NAMESPACE, doc)
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


def stage_all_authority_datasets(
    *,
    base_url: str = WHG_API_BASE_URL,
    token: str | None = None,
    limit_features_per_dataset: int | None = None,
    dataset_filter: list[int] | None = None,
    include_pending: bool = False,
) -> dict[str, Any]:
    """Discover + stage every authority-eligible dataset.

    When ``include_pending=True`` the discovery call also returns datasets
    whose ``ds_status`` is anything other than ``seed`` / ``format_error``;
    each entry's ``dataset_status`` (set by Django from
    ``(authority and public and ds_status in {accessioning, indexed})``)
    is propagated onto every staged place doc.
    """
    token = _resolve_token(token)
    session = _make_session(token)

    print("=" * 78)
    print("WHG-PLACES — STAGED EXTRACTION")
    print("=" * 78)
    print(f"WHG API: {base_url}")
    print(f"Auth:    {'bearer-token' if token else 'unauthenticated'}")
    print(f"Pending: {'included' if include_pending else 'excluded'}")

    datasets = discover_authority_datasets(
        session, base_url=base_url, include_pending=include_pending,
    )
    if dataset_filter:
        keep = set(dataset_filter)
        datasets = [d for d in datasets if d.get("id") in keep]
    print(f"Discovered {len(datasets)} datasets.")

    per_dataset: dict[str, dict[str, Any]] = {}
    totals = {"features_seen": 0, "docs_written": 0, "docs_skipped": 0}

    # Full geometries go to the VAST store, as for every other areal authority.
    # GEOM_STORE_STAGING_DIR is read from the environment at import time, so a
    # run that must keep its staging output away from a concurrent merge points
    # that variable somewhere private before starting.
    from processing.geom_store import GeomStoreWriter, configure_module_writer

    id_map_file = default_id_map_path()
    print(f"Geom staging: {GEOM_STORE_STAGING_DIR}")
    print(f"Id map:       {id_map_file}")

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, WHG_NAMESPACE) as gsw, \
            IdMapWriter(id_map_file) as id_map:
        configure_module_writer(gsw)
        try:
            _stage_each(
                datasets,
                session=session,
                base_url=base_url,
                limit_features_per_dataset=limit_features_per_dataset,
                id_map=id_map,
                per_dataset=per_dataset,
                totals=totals,
            )
        finally:
            configure_module_writer(None)

    print(f"\nGeometries written to the VAST store: {gsw.count:,}")
    print(f"Id-map entries written: {id_map.count:,} → {id_map_file}")

    summary = {
        "discovered": len(datasets),
        "per_dataset": per_dataset,
        "totals": totals,
        "id_map_path": str(id_map_file),
        "id_map_entries": id_map.count,
        "geom_store_entries": gsw.count,
    }
    _write_datasets_sidecar(per_dataset, summary)
    return summary


def _stage_each(
    datasets: list[dict[str, Any]],
    *,
    session: requests.Session,
    base_url: str,
    limit_features_per_dataset: int | None,
    id_map: IdMapWriter,
    per_dataset: dict[str, dict[str, Any]],
    totals: dict[str, int],
) -> None:
    """Stage every discovered dataset, accumulating metrics in place.

    Split out of ``stage_all_authority_datasets`` only so the geom-store and
    id-map writers can wrap the whole loop without burying it in indentation.
    """
    for ds in datasets:
        ds_id = ds.get("id")
        title = ds.get("title", "")
        if not isinstance(ds_id, int):
            continue
        # Django's AuthorityDatasetsView derives this; default to 'pending'
        # if the field is absent (older Django build).
        ds_status = str(ds.get("dataset_status") or "pending")
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
            )
        except Exception as exc:
            print(f"  ✗ {ds_id}: {exc}", file=sys.stderr)
            continue
        # Carry the discovery metadata onto the per-dataset metrics so the
        # Batch 11 inventory push has dataset_id-keyed name / description /
        # owner_user_id without a second discovery call.
        metrics["title"] = title
        metrics["description"] = ds.get("description")
        metrics["dataset_status"] = ds_status
        metrics["owner_user_id"] = ds.get("owner_id")
        # Owner-supplied per-place source-link template (place#121), carried
        # through to the inventory push so the registry (and Atlas popups) get it.
        metrics["web_item"] = ds.get("web_item")
        per_dataset[str(ds_id)] = metrics
        for k in ("features_seen", "docs_written", "docs_skipped",
                  "no_src_id", "duplicate_src_id"):
            totals[k] = totals.get(k, 0) + int(metrics.get(k) or 0)
        print(f"  ✓ {ds_id}: {metrics}")


def _write_datasets_sidecar(
    per_dataset: dict[str, dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    """Sidecar consumed by Batch 11 push_gazetteer_inventory: one inventory
    entry per WHG dataset (class='dataset', owner_user_id, status). Stable
    contract: ``staged/_aggregates/whg.datasets.json``."""
    try:
        from processing.settings import STAGED_BASE_DIR
        sidecar_path = (
            Path(STAGED_BASE_DIR) / "_aggregates" / "whg.datasets.json"
        )
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_payload = {
            "namespace": WHG_NAMESPACE,
            "datasets": [
                {
                    "id": f"{WHG_NAMESPACE}:{ds_id}",
                    "name": meta.get("title") or f"WHG dataset {ds_id}",
                    "description": meta.get("description"),
                    "dataset_status": meta.get("dataset_status") or "pending",
                    "owner_user_id": meta.get("owner_user_id"),
                    "record_count": int(meta.get("docs_written") or 0),
                    **({"web_item": meta["web_item"]} if meta.get("web_item") else {}),
                }
                for ds_id, meta in per_dataset.items()
            ],
        }
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
        print(json.dumps({"discovered": datasets}, indent=2))
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
