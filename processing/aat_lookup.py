# processing/aat_lookup.py

"""
Shared AAT-mapping helper module.

Provides fast in-memory reverse lookups from authority-native type identifiers
to AAT concept IDs by querying the ``types`` ES index.  Usable both during
ingestion (authority scripts) and as a standalone mapping-update process.

**Important:** The ``types`` index lives on the **production** ES instance
(it is maintained by the Django mapping UI), whereas ingestion writes to a
**staging** ES instance.  Functions in this module therefore accept separate
ES clients where needed:

- ``load_aat_mappings(es_types, vocabulary)`` — reads from production (types index)
- ``apply_aat_mappings_to_index(es_places, vocabulary, …, es_types=None)`` —
  reads types from production, writes updates to staging/places

Functions:
    load_aat_mappings(es_types, vocabulary)
        → dict mapping native identifiers to lists of AAT IDs

    apply_aat_mappings_to_index(es_places, vocabulary, places_index, …, es_types=None)
        → scrolls existing place docs for a given namespace and bulk-updates
          their types[] entries with current AAT mappings

Usage:
    from processing.aat_lookup import load_aat_mappings, apply_aat_mappings_to_index

    # Production ES client (for reading types)
    es_prod = Elasticsearch("http://localhost:9201")
    # Staging ES client (for reading/writing places)
    es_staging = Elasticsearch("http://compute-node:12345")

    mappings = load_aat_mappings(es_prod, 'osm')
    # mappings["place=city"] → [300008389]

    apply_aat_mappings_to_index(es_staging, 'osm', 'places', es_types=es_prod)
"""

from collections import defaultdict

from elasticsearch import Elasticsearch, helpers

from processing.settings import (
    TYPES_ES_HOST,
    TYPES_INDEX as SETTINGS_TYPES_INDEX,
    TYPES_ES_USER,
    TYPES_ES_PASSWORD,
)

TYPES_INDEX = SETTINGS_TYPES_INDEX or "types"

# Map vocabulary name → ES field on the types index that stores the
# cross-vocabulary references for that vocabulary.
_VOCAB_FIELD_MAP = {
    "gn": "gn_fcodes",         # GeoNames feature codes
    "geonames": "gn_fcodes",
    "wd": "wd_qids",           # Wikidata Q-items
    "wikidata": "wd_qids",
    "osm": "osm_tags",         # OSM sourceLabel tags
    "ohm": "ohm_tags",         # OHM sourceLabel tags
    "pleiades": "pleiades_types",
    "pl": "pleiades_types",
}

# Map vocabulary → namespace prefix used in place_id
_VOCAB_NAMESPACE = {
    "gn": "gn",
    "geonames": "gn",
    "wd": "wd",
    "wikidata": "wd",
    "osm": "osm",
    "ohm": "ohm",
    "pleiades": "pl",
    "pl": "pl",
}


def create_types_es_client() -> Elasticsearch:
    """Build an Elasticsearch client for production `types` lookups."""
    kwargs = {}
    if TYPES_ES_USER and TYPES_ES_PASSWORD:
        kwargs["basic_auth"] = (TYPES_ES_USER, TYPES_ES_PASSWORD)
    return Elasticsearch(TYPES_ES_HOST, **kwargs)


def preflight_types_index(es_types: Elasticsearch, index_name: str = TYPES_INDEX) -> dict:
    """Fail-fast preflight check for production types index availability."""
    info = es_types.info()
    if not es_types.indices.exists(index=index_name):
        raise RuntimeError(f"Types index '{index_name}' not found on {TYPES_ES_HOST}")

    sample = es_types.search(
        index=index_name,
        body={
            "size": 1,
            "_source": ["aat_id", "path", "is_place_type"],
            "query": {"match_all": {}},
        },
    )
    sample_hits = sample.get("hits", {}).get("hits", [])

    return {
        "cluster": info.get("cluster_name"),
        "version": info.get("version", {}).get("number"),
        "index": index_name,
        "sample_docs": len(sample_hits),
    }


def _normalize_native_id(vocabulary: str, source_label: str) -> str:
    """Normalize source labels to the identifier format stored in `types` mappings."""
    if not source_label:
        return source_label

    v = (vocabulary or "").lower()
    value = str(source_label).strip()

    # GeoNames values in the types index are feature codes (e.g. PPL),
    # while sourceLabel is often class+code (e.g. P.PPL).
    if v in {"gn", "geonames"}:
        if "." in value:
            value = value.split(".", 1)[1]
        return value.upper()

    # Wikidata should be canonical Q-IDs.
    if v in {"wd", "wikidata"}:
        return value.upper()

    # OSM/OHM tags are matched as exact key=value terms; normalize case/spacing.
    if v in {"osm", "ohm"}:
        if "=" in value:
            k, tag_v = value.split("=", 1)
            return f"{k.strip().lower()}={tag_v.strip().lower()}"
        return value.strip().lower()

    # Pleiades type identifiers are treated as case-insensitive tokens.
    if v in {"pl", "pleiades"}:
        return value.lower()

    return value


def _confidence_for(hit_source: dict, field: str, native_id: str) -> str | None:
    conf_map = (hit_source.get("mapping_conf") or {}).get(field) or {}
    return conf_map.get(native_id)


def lookup_aat_candidates(
    es_types: Elasticsearch,
    vocabulary: str,
    native_id: str,
    *,
    size: int = 10,
) -> list[dict]:
    """Reverse lookup non-AAT identifier to candidate AAT docs with path/confidence."""
    field = _VOCAB_FIELD_MAP.get(vocabulary)
    if not field:
        raise ValueError(
            f"Unknown vocabulary '{vocabulary}'. "
            f"Known: {sorted(_VOCAB_FIELD_MAP.keys())}"
        )

    normalized = _normalize_native_id(vocabulary, native_id)
    resp = es_types.search(
        index=TYPES_INDEX,
        body={
            "size": size,
            "_source": [
                "aat_id",
                "term",
                "path",
                "is_place_type",
                "mapping_conf",
                field,
            ],
            "query": {"term": {field: normalized}},
        },
    )

    results = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        aat_id = src.get("aat_id")
        if aat_id is None:
            continue
        results.append(
            {
                "aat_id": int(aat_id),
                "aat_term": src.get("term"),
                "aat_path": src.get("path"),
                "is_place_type": bool(src.get("is_place_type", False)),
                "confidence": _confidence_for(src, field, normalized),
            }
        )
    return results


def load_aat_mappings(es_types: Elasticsearch, vocabulary: str) -> dict[str, list[int]]:
    """
    Query the ``types`` ES index and build an in-memory reverse lookup dict.

    Args:
        es_types:   Elasticsearch client pointing at the **production** instance
                    (where the ``types`` index lives)
        vocabulary: vocabulary name (e.g. 'osm', 'gn', 'wd', 'pleiades', 'ohm')

    Returns:
        dict mapping native identifier strings to lists of AAT IDs (ints).
        For example: {"place=city": [300008389], "P.PPL": [300008347]}
    """
    field = _VOCAB_FIELD_MAP.get(vocabulary)
    if not field:
        raise ValueError(
            f"Unknown vocabulary '{vocabulary}'. "
            f"Known: {sorted(_VOCAB_FIELD_MAP.keys())}"
        )

    # Query all types docs where the cross-vocab field is non-empty
    query = {
        "query": {
            "exists": {"field": field}
        },
        "_source": ["aat_id", field],
        "size": 1000,
    }

    reverse: dict[str, list[int]] = defaultdict(list)
    count = 0

    resp = es_types.search(index=TYPES_INDEX, body=query, scroll="5m")
    scroll_id = resp["_scroll_id"]

    while True:
        hits = resp["hits"]["hits"]
        if not hits:
            break
        for hit in hits:
            src = hit["_source"]
            aat_id_raw = src.get("aat_id")
            xrefs = src.get(field, [])
            if not aat_id_raw or not xrefs:
                continue

            # aat_id may be stored as int or string
            try:
                aat_id = int(aat_id_raw)
            except (ValueError, TypeError):
                continue

            for native_id in xrefs:
                reverse[native_id].append(aat_id)
                count += 1

        resp = es_types.scroll(scroll_id=scroll_id, scroll="5m")

    try:
        es_types.clear_scroll(scroll_id=scroll_id)
    except Exception:
        pass

    print(f"  AAT lookup for '{vocabulary}': {count} mappings across "
          f"{len(reverse)} native identifiers")
    return dict(reverse)


def load_aat_mapping_records(es_types: Elasticsearch, vocabulary: str) -> dict[str, list[dict]]:
    """Load reverse lookups with path/term/confidence metadata for staging output."""
    field = _VOCAB_FIELD_MAP.get(vocabulary)
    if not field:
        raise ValueError(
            f"Unknown vocabulary '{vocabulary}'. "
            f"Known: {sorted(_VOCAB_FIELD_MAP.keys())}"
        )

    query = {
        "query": {"exists": {"field": field}},
        "_source": ["aat_id", "term", "path", "is_place_type", "mapping_conf", field],
        "size": 1000,
    }

    reverse: dict[str, list[dict]] = defaultdict(list)
    resp = es_types.search(index=TYPES_INDEX, body=query, scroll="5m")
    scroll_id = resp.get("_scroll_id")

    while True:
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            src = hit.get("_source", {})
            try:
                aat_id = int(src.get("aat_id"))
            except (TypeError, ValueError):
                continue
            for native_id in src.get(field, []) or []:
                reverse[native_id].append(
                    {
                        "aat_id": aat_id,
                        "aat_term": src.get("term"),
                        "aat_path": src.get("path"),
                        "is_place_type": bool(src.get("is_place_type", False)),
                        "confidence": _confidence_for(src, field, native_id),
                    }
                )

        resp = es_types.scroll(scroll_id=scroll_id, scroll="5m")

    if scroll_id:
        try:
            es_types.clear_scroll(scroll_id=scroll_id)
        except Exception:
            pass

    return dict(reverse)


def _aat_types_for_doc(
    source_types: list[dict],
    vocabulary: str | None = None,
    mappings: dict[str, list[int]] | None = None,
    mapping_records: dict[str, list[dict]] | None = None,
) -> list[dict] | None:
    """
    Given a place doc's existing types[] and a mapping dict, return an
    augmented types[] with AAT entries appended, or None if no changes.
    """
    existing_aat_ids: set[int] = set()
    for t in source_types:
        if t.get("label") == "aat":
            try:
                existing_aat_ids.add(int(t["identifier"]))
            except (ValueError, TypeError):
                pass

    new_types = list(source_types)
    changed = False

    for t in source_types:
        source_label = t.get("sourceLabel", "")
        if not source_label:
            continue

        vocab_for_norm = vocabulary or t.get("label") or ""
        native_key = _normalize_native_id(vocab_for_norm, source_label)

        records = []
        if mapping_records is not None:
            records = mapping_records.get(native_key, [])
        elif mappings is not None:
            records = [{"aat_id": aat_id} for aat_id in mappings.get(native_key, [])]

        for rec in records:
            aat_id = rec["aat_id"]
            if aat_id not in existing_aat_ids:
                aat_path = rec.get("aat_path")

                new_types.append({
                    "identifier": str(aat_id),
                    "label": "aat",
                    "sourceLabel": source_label,
                    "aat_id": aat_id,
                    "aat_path": aat_path,
                })
                existing_aat_ids.add(aat_id)
                changed = True

    return new_types if changed else None


def apply_aat_mappings_to_index(
    es_places: Elasticsearch,
    vocabulary: str,
    places_index: str = "places",
    batch_size: int = 500,
    es_types: Elasticsearch | None = None,
):
    """
    Scroll existing place docs for a given namespace and bulk-update their
    ``types[]`` entries with current AAT mappings from the ``types`` index.

    Args:
        es_places:     Elasticsearch client for the **places** index
                       (staging or production — wherever the places live)
        vocabulary:    vocabulary name (e.g. 'osm', 'gn')
        places_index:  name of the places index
        batch_size:    bulk update batch size
        es_types:      Elasticsearch client for the **types** index
                       (production).  If None, ``es_places`` is used for both
                       (appropriate when both indices are on the same instance).
    """
    types_client: Elasticsearch = es_types if es_types is not None else es_places

    namespace = _VOCAB_NAMESPACE.get(vocabulary, vocabulary)
    print(f"\nLoading AAT mappings for '{vocabulary}' ...")
    mapping_records = load_aat_mapping_records(types_client, vocabulary)
    if not mapping_records:
        print("  No mappings found — nothing to do.")
        return

    print(f"Scrolling {namespace}:* docs in '{places_index}' ...")

    query = {
        "query": {"prefix": {"place_id": f"{namespace}:"}},
        "_source": ["types"],
        "size": batch_size,
    }

    updated = 0
    scanned = 0
    errors = 0

    resp = es_places.search(index=places_index, body=query, scroll="10m")
    scroll_id = resp["_scroll_id"]

    while True:
        hits = resp["hits"]["hits"]
        if not hits:
            break

        actions = []
        for hit in hits:
            scanned += 1
            source_types = hit["_source"].get("types", [])
            if not source_types:
                continue

            new_types = _aat_types_for_doc(
                source_types,
                vocabulary=vocabulary,
                mapping_records=mapping_records,
            )
            if new_types is not None:
                actions.append({
                    "_op_type": "update",
                    "_index": places_index,
                    "_id": hit["_id"],
                    "doc": {"types": new_types},
                })

        if actions:
            for ok, info in helpers.streaming_bulk(
                es_places, actions, raise_on_error=False, max_retries=2,
            ):
                if ok:
                    updated += 1
                else:
                    errors += 1
                    if errors <= 5:
                        print(f"    Update error: {info}")

        if scanned % 10000 == 0:
            print(f"  Scanned {scanned:,} docs, updated {updated:,} ...",
                  flush=True)

        resp = es_places.scroll(scroll_id=scroll_id, scroll="10m")

    try:
        es_places.clear_scroll(scroll_id=scroll_id)
    except Exception:
        pass

    print(f"\n  Done: scanned {scanned:,}, updated {updated:,}, errors {errors:,}")





