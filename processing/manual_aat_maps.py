# processing/manual_aat_maps.py
"""Curated native-type → Getty AAT mappings for authorities with a small,
hand-mappable place-type vocabulary.

Some sources don't have (and don't warrant) a full generated vocab file with
per-entry ``aat_mapping`` (`typesystem/data/<vocab>.json`); their native type
vocabulary is tiny or bespoke. This module holds a hand-curated
``{namespace: {native_identifier: [aat_id, …]}}`` table, keyed by the
``types[].identifier`` each authority script emits.

``processing.aat_enrich.augment_doc`` consults this table (by the doc's
namespace + each type entry's identifier) and injects ``aat_ids``; the existing
path-fill then attaches ``aat_paths`` from the AAT hierarchy. Because it runs
inside ``augment_doc``, the SAME table drives:

* the **live backfill** — ``python -m processing.apply_aat_enrich --namespace <ns> --execute``
* **future ingestion** — the ``aat_enrich`` stage during a rebuild.

No per-authority-script change is needed. All ids were validated against the
production ``types`` index (place-type concepts) 2026-07-12.
"""

from __future__ import annotations


# {namespace: {native types[].identifier: [aat_id, …]}}
MANUAL_AAT_MAPS: dict[str, dict[str, list[int]]] = {
    # UK Historic Counties — a single native type.
    "ukhc": {"historic-county": [300000771]},  # counties

    # Cliopatria / Seshat — historical polities (states/empires/chiefdoms).
    "clio": {"polity": [300417385]},  # polities

    # PeriodO — temporal periods with spatial coverage. Generic time-period type.
    "po": {"period": [300081446]},  # period (general)

    # Index Villaris — early-modern English settlements.
    "iv": {"settlement": [300008347], "market-town": [300008423]},  # inhabited places / market towns

    # Trismegistos — ancient-world places (common types mapped; ethnonyms /
    # land-allotments left untyped: 'people', 'kleros').
    "tm": {
        "place": [300008347],       # inhabited places (generic tm-geo)
        "topos": [300008347],       # inhabited places
        "village": [300008372],     # villages
        "city": [300008389],        # cities
        "river": [300008707],       # rivers
        "island": [300008791],      # islands (landforms)
        "district": [300000705],    # districts
        "region": [300182722],      # regions (geographic)
        "mountain": [300008795],    # mountains (landforms)
        "monastery": [300000641],   # monasteries (built complexes)
        "church": [300007466],      # churches (buildings)
        "camp": [300006909],        # forts (camp: castellum)
        "fundus": [300000206],      # farms (Roman estate)
    },

    # Digital Gazetteer of the Song Dynasty — Chinese administrative units.
    "dgsd": {
        "county": [300000771],             # counties (县)
        "zhou prefecture": [300235099],    # prefectures (州)
        "fu prefecture": [300235099],      # prefectures (府)
        "jun prefecture": [300235099],     # prefectures (軍)
        "jian prefecture": [300235099],    # prefectures (監)
        "industrial center": [300235099],  # prefectures (監 supervisorate)
        "circuit": [300236112],            # regions (administrative divisions) (路)
        "garrison market": [300008423],    # market towns (鎮)
        "market": [300008423],             # market towns (塲)
        "stockade": [300006914],           # stockades (寨)
        "walled town": [300008375],        # towns (城)
    },

    # Native Land — indigenous territories, language areas, treaty areas.
    "nl": {
        "indigenous-territory": [300135982],       # territories
        "indigenous-language-area": [300387171],   # cultural groups
        "treaty-area": [300182722],                # regions (geographic)
    },

    # D-PLACE — ethnographic society / language locations.
    "dp": {
        "language-location": [300387171],  # cultural groups
        "cultural-location": [300387171],  # cultural groups
    },
}


def manual_aat_ids(namespace: str | None, identifier: str | None) -> list[int] | None:
    """The curated AAT ids for a ``(namespace, identifier)``, or None."""
    if not namespace or not identifier:
        return None
    return MANUAL_AAT_MAPS.get(namespace, {}).get(identifier)
