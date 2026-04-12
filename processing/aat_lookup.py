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

TYPES_INDEX = "types"

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


def _aat_types_for_doc(source_types: list[dict], mappings: dict[str, list[int]]) -> list[dict] | None:
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
        aat_ids = mappings.get(source_label, [])
        for aat_id in aat_ids:
            if aat_id not in existing_aat_ids:
                new_types.append({
                    "identifier": str(aat_id),
                    "label": "aat",
                    "sourceLabel": source_label,
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
    if es_types is None:
        es_types = es_places

    namespace = _VOCAB_NAMESPACE.get(vocabulary, vocabulary)
    print(f"\nLoading AAT mappings for '{vocabulary}' ...")
    mappings = load_aat_mappings(es_types, vocabulary)
    if not mappings:
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

            new_types = _aat_types_for_doc(source_types, mappings)
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





