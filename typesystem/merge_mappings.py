# types/merge_mappings.py

"""
**DEPRECATED** — Superseded by ``processing/aat_lookup.py`` and the Django
mapping UI.  The ``types`` index is now updated incrementally by the mapping
UI; batch re-application of data-file mappings is handled by
``processing.aat_lookup.apply_aat_mappings_to_index()``.

This module is retained for reference only.

---

Merge cross-vocabulary AAT mappings into the ES `types` index.

After `sync_aat_types.py` populates the base AAT hierarchy and
`aat_mapper.py` has augmented the data files with mappings, this script
reads the mappings from all types/data/*.json files and updates each
AAT type doc in ES with cross-vocabulary fields:
  - gn_fcodes:      GeoNames feature codes mapped to this AAT concept
  - wd_qids:        Wikidata Q-items mapped to this AAT concept
  - osm_tags:       OSM sourceLabel tags mapped to this AAT concept
  - ohm_tags:       OHM sourceLabel tags mapped to this AAT concept
  - pleiades_types: Pleiades type identifiers mapped to this AAT concept

Usage:
    python -m typesystem.merge_mappings --es-host URL
"""

import json
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
TYPES_INDEX = "types"


def collect_mappings():
    """
    Read all types/data/*.json files and collect reverse mappings:
    aat_id → {gn_fcodes: [...], wd_qids: [...], osm_tags: [...], ...}
    """
    reverse = defaultdict(lambda: {
        "gn_fcodes": set(),
        "wd_qids": set(),
        "osm_tags": set(),
        "ohm_tags": set(),
        "pleiades_types": set(),
    })

    # --- GeoNames ---
    try:
        with open(DATA_DIR / "geonames.json") as f:
            data = json.load(f)
        for fclass, fclass_data in data.items():
            if not isinstance(fclass_data, dict):
                continue
            for entry in fclass_data.get("values", []):
                mapping = entry.get("aat_mapping")
                if mapping and mapping.get("aat_id"):
                    source_label = f"{fclass}.{entry['value']}"
                    reverse[mapping["aat_id"]]["gn_fcodes"].add(source_label)
        print(f"  GeoNames: loaded")
    except FileNotFoundError:
        print(f"  GeoNames: skipped (not found)")

    # --- Wikidata ---
    try:
        with open(DATA_DIR / "wikidata.json") as f:
            data = json.load(f)
        for entry in data.get("values", []):
            mapping = entry.get("aat_mapping")
            if mapping and mapping.get("aat_id"):
                reverse[mapping["aat_id"]]["wd_qids"].add(entry["value"])
        print(f"  Wikidata: loaded")
    except FileNotFoundError:
        print(f"  Wikidata: skipped (not found)")

    # --- OSM ---
    try:
        with open(DATA_DIR / "osm.json") as f:
            data = json.load(f)
        for tag_key, tag_data in data.items():
            if tag_key.startswith("_") or not isinstance(tag_data, dict):
                continue
            for entry in tag_data.get("values", []):
                mapping = entry.get("aat_mapping")
                if mapping and mapping.get("aat_id"):
                    source_label = f"{tag_key}={entry['value']}"
                    reverse[mapping["aat_id"]]["osm_tags"].add(source_label)
        print(f"  OSM: loaded")
    except FileNotFoundError:
        print(f"  OSM: skipped (not found)")

    # --- OHM ---
    try:
        with open(DATA_DIR / "ohm.json") as f:
            data = json.load(f)
        for tag_key, tag_data in data.items():
            if tag_key.startswith("_") or not isinstance(tag_data, dict):
                continue
            for entry in tag_data.get("values", []):
                mapping = entry.get("aat_mapping")
                if mapping and mapping.get("aat_id"):
                    source_label = f"{tag_key}={entry['value']}"
                    reverse[mapping["aat_id"]]["ohm_tags"].add(source_label)
        print(f"  OHM: loaded")
    except FileNotFoundError:
        print(f"  OHM: skipped (not found)")

    # --- Pleiades ---
    try:
        with open(DATA_DIR / "pleiades.json") as f:
            data = json.load(f)
        for entry in data.get("values", []):
            mapping = entry.get("aat_mapping")
            if mapping and mapping.get("aat_id"):
                reverse[mapping["aat_id"]]["pleiades_types"].add(entry["value"])
        for entry in data.get("deprecated", []):
            mapping = entry.get("aat_mapping")
            if mapping and mapping.get("aat_id"):
                reverse[mapping["aat_id"]]["pleiades_types"].add(entry["value"])
        print(f"  Pleiades: loaded")
    except FileNotFoundError:
        print(f"  Pleiades: skipped (not found)")

    # Convert sets to sorted lists
    for aat_id in reverse:
        for key in reverse[aat_id]:
            reverse[aat_id][key] = sorted(reverse[aat_id][key])

    return dict(reverse)


def update_es(reverse_mappings, es_host):
    """Update ES types index with cross-vocabulary fields."""
    from elasticsearch import helpers
    from typesystem.es_client import create_client

    es = create_client(es_host)

    print(f"\nUpdating {len(reverse_mappings)} AAT concepts in ES ...")

    actions = []
    for aat_id, fields in reverse_mappings.items():
        # Only include non-empty fields
        doc = {k: v for k, v in fields.items() if v}
        if not doc:
            continue

        actions.append({
            "_op_type": "update",
            "_index": TYPES_INDEX,
            "_id": f"aat:{aat_id}",
            "doc": doc,
        })

    if not actions:
        print("  No updates to apply")
        return

    success = 0
    errors = 0
    for ok, info in helpers.streaming_bulk(
        es, actions, raise_on_error=False, max_retries=2
    ):
        if ok:
            success += 1
        else:
            errors += 1
            if errors <= 5:
                print(f"    Update error: {info}")

    print(f"  -> {success} updated, {errors} errors")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Merge cross-vocabulary AAT mappings into ES types index"
    )
    parser.add_argument("--es-host", required=True, help="ES host URL")
    args = parser.parse_args()

    print("Collecting mappings from types/data/*.json files ...")
    reverse_mappings = collect_mappings()

    total_xrefs = sum(
        sum(len(v) for v in fields.values())
        for fields in reverse_mappings.values()
    )
    print(f"\nFound {total_xrefs} cross-references across "
          f"{len(reverse_mappings)} AAT concepts")

    update_es(reverse_mappings, args.es_host)
    print("\nDone.")


if __name__ == "__main__":
    main()


