# processing/nativeland-places.py
"""
Index Native Land Digital data.
"""
import json, os, sys
from pathlib import Path
from processing.helpers import (
    enrich_geometry,
    compute_area_km2,
    compute_h3_fields,
    select_h3_cover_geometry,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR, AUTHORITIES
from processing.temporal import attested_at
from datetime import datetime

# Native Land is a PRESENT-DAY snapshot of territory/language/treaty extents:
# it attests these exist now, and says nothing about when they began or ended.
# Was `{'start': {'in': 2025}, 'end': {'in': 2025}}` = "existed only in 2025",
# which excluded every nl record from any historical date range (place#164).
_NL_ATTESTATION_YEAR = datetime.now().year
_NL_ATTESTED = attested_at(_NL_ATTESTATION_YEAR)

NL_CONFIG = next((auth for auth in AUTHORITIES if auth['namespace'] == 'nl'), None)


def process_territory(feature, namespace='nl'):
    """Process Native Land territory."""
    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    name = props.get('Name', props.get('name', ''))
    if not name: return None

    slug = props.get('Slug', props.get('slug', name.lower().replace(' ', '-')))
    place_id = f"{namespace}:territory:{slug}"

    toponyms = []
    seen_lsts = set()

    lst = f"{name}@en"
    if lst not in seen_lsts:
        toponyms.append({'toponym_id': lst, 'timespans': _NL_ATTESTED})
        seen_lsts.add(lst)

    if 'FrenchName' in props and props['FrenchName']:
        lst = f"{props['FrenchName']}@fr"
        if lst not in seen_lsts:
            toponyms.append({'toponym_id': lst, 'timespans': _NL_ATTESTED})
            seen_lsts.add(lst)

    geom_entry = None
    area = None
    if geometry:
        timespans = _NL_ATTESTED
        geom_entry = enrich_geometry(geometry, timespans=timespans, geom_key=f"{place_id}_0")
        area = compute_area_km2(geometry)

    place_doc = {
        'place_id': place_id,
        'title': name,
        'toponyms': toponyms,
        'geometries': [geom_entry] if geom_entry else [],
        'types': [{'identifier': 'indigenous-territory', 'label': 'nativeland', 'sourceLabel': 'territory'}],
        'boundary': 'native',
    }


    if area: place_doc['area_km2'] = round(area, 2)
    if 'description' in props: place_doc['description'] = props['description']
    if 'color' in props: place_doc['display_color'] = props['color']

    return place_doc


def process_language(feature, namespace='nl'):
    """Process Native Land language."""
    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    name = props.get('Name', props.get('name', ''))
    if not name: return None

    slug = props.get('Slug', props.get('slug', name.lower().replace(' ', '-')))
    place_id = f"{namespace}:language:{slug}"

    toponyms = [{'toponym_id': f"{name}@en", 'timespans': _NL_ATTESTED}]

    geom_entry = None
    area = None
    if geometry:
        timespans = _NL_ATTESTED
        geom_entry = enrich_geometry(geometry, timespans=timespans, geom_key=f"{place_id}_0")
        area = compute_area_km2(geometry)

    place_doc = {
        'place_id': place_id,
        'title': name,
        'toponyms': toponyms,
        'geometries': [geom_entry] if geom_entry else [],
        'types': [{'identifier': 'indigenous-language-area', 'label': 'nativeland', 'sourceLabel': 'language'}],
        'boundary': 'native',
    }


    if area: place_doc['area_km2'] = round(area, 2)
    if 'color' in props: place_doc['display_color'] = props['color']

    return place_doc


def process_treaty(feature, namespace='nl'):
    """Process Native Land treaty."""
    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    name = props.get('Name', props.get('name', ''))
    if not name: return None

    slug = props.get('Slug', props.get('slug', name.lower().replace(' ', '-')))
    place_id = f"{namespace}:treaty:{slug}"

    toponyms = [{'toponym_id': f"{name}@en", 'timespans': _NL_ATTESTED}]

    geom_entry = None
    area = None
    if geometry:
        timespans = _NL_ATTESTED
        geom_entry = enrich_geometry(geometry, timespans=timespans, geom_key=f"{place_id}_0")
        area = compute_area_km2(geometry)

    place_doc = {
        'place_id': place_id,
        'title': name,
        'toponyms': toponyms,
        'geometries': [geom_entry] if geom_entry else [],
        'types': [{'identifier': 'treaty-area', 'label': 'nativeland', 'sourceLabel': 'treaty'}],
        'boundary': 'native',
    }


    if area: place_doc['area_km2'] = round(area, 2)
    if 'Date' in props: place_doc['treaty_date'] = props['Date']
    if 'color' in props: place_doc['display_color'] = props['color']

    return place_doc


def stage_nativeland_file(json_file, data_type):
    """Stage one Native Land GeoJSON file to {STAGED_BASE_DIR}/nl/extract/places.jsonl."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    print(f"Processing {data_type}: {json_file}")

    if not os.path.exists(json_file):
        standard_path = Path(DATA_DIR) / 'authorities' / 'nl' / Path(json_file).name
        if standard_path.exists():
            json_file = standard_path
        else:
            print(f"ERROR: Not found: {json_file}")
            return

    if data_type == 'territories':
        processor = process_territory
    elif data_type == 'languages':
        processor = process_language
    elif data_type == 'treaties':
        processor = process_treaty
    else:
        print(f"ERROR: Unknown type: {data_type}")
        return

    places_count = 0
    skipped = 0

    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERROR: {e}")
        return

    if data.get('type') == 'FeatureCollection':
        features = data.get('features', [])
    elif 'features' in data:
        features = data['features']
    else:
        print("ERROR: No features")
        return

    print(f"Found {len(features)} {data_type}")

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, f"nl_{data_type}") as gsw:
        configure_module_writer(gsw)
        for i, feature in enumerate(features):
            if (i + 1) % 100 == 0:
                print(f"\r  {i + 1}/{len(features)}...", end='', flush=True)

            try:
                place_doc = processor(feature, namespace='nl')
                if not place_doc:
                    skipped += 1
                    continue
                write_staged_place_doc(namespace='nl', doc=place_doc)
                places_count += 1
            except Exception as e:
                print(f"  ERROR {i}: {e}")
                skipped += 1
                continue

        configure_module_writer(None)

    print(f"\nComplete: {data_type}")
    print(f"Staged: {places_count:,}, Skipped: {skipped:,}, Geometries in VAST store: {gsw.count:,}")


def stage_all_nativeland():
    """Stage all Native Land files."""
    print("=" * 80)
    print("NATIVE LAND STAGING (SCHEMA V2)")
    print("=" * 80)

    nl_dir = Path(DATA_DIR) / 'authorities' / 'nl'
    if not nl_dir.exists():
        print(f"ERROR: Directory not found: {nl_dir}")
        return

    file_types = [('territories.json', 'territories'), ('languages.json', 'languages'), ('treaties.json', 'treaties')]

    for filename, data_type in file_types:
        file_path = nl_dir / filename
        if not file_path.exists():
            print(f"\nSkipping {data_type}: {filename} not found")
            continue
        print(f"\n--- {data_type.upper()} ---")
        stage_nativeland_file(str(file_path), data_type)

    print("\n" + "=" * 80)
    print("NATIVE LAND COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Stage Native Land data')
    parser.add_argument('--file', help='Path to GeoJSON')
    parser.add_argument('--type', choices=['territories', 'languages', 'treaties'], help='Data type')
    parser.add_argument('--all', action='store_true', help='Stage all')
    args = parser.parse_args()

    if args.all or (not args.file and not args.type):
        stage_all_nativeland()
    elif args.file and args.type:
        stage_nativeland_file(args.file, args.type)
    else:
        print("ERROR: Specify --file and --type, or use --all")
        parser.print_help()
