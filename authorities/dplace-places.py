# processing/dplace-places.py

"""
Index D-PLACE (Database of Places, Language, Culture, and Environment) data.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime
from processing.helpers import enrich_geometry, compute_bbox

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE, AUTHORITIES
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST)

# Get D-PLACE configuration
DPLACE_CONFIG = next((auth for auth in AUTHORITIES if auth['namespace'] == 'dp'), None)
if not DPLACE_CONFIG:
    print("ERROR: D-PLACE configuration not found in AUTHORITIES")
    sys.exit(1)


def process_dplace_feature(feature, namespace='dp'):
    """Process a D-PLACE feature."""
    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    lang_obj = props.get('language', {})

    feature_id = (
        feature.get('id') or
        lang_obj.get('xid') or
        lang_obj.get('id') or
        props.get('id') or
        props.get('xd_id', '')
    )

    name = (
        props.get('name') or
        lang_obj.get('name') or
        lang_obj.get('language') or
        props.get('society_name') or
        props.get('language_name', '')
    )

    if not name or not feature_id:
        return None

    place_id = f"{namespace}:{feature_id}"

    # Build toponyms with timespans array
    toponyms = []
    seen_lsts = set()

    glottocode = lang_obj.get('glottocode', props.get('glottocode', ''))
    lang_code = 'und'

    lst = f"{name}@{lang_code}"
    if lst not in seen_lsts:
        toponyms.append({
            'toponym_id': lst,
            'timespans': [{
                'start': {'in': 2025},
                'end': {'in': 2025}
            }]
        })
        seen_lsts.add(lst)

    name_in_source = lang_obj.get('name_in_source', '')
    if name_in_source and name_in_source != name:
        lst = f"{name_in_source}@{lang_code}"
        if lst not in seen_lsts:
            toponyms.append({
                'toponym_id': lst,
                'timespans': [{
                    'start': {'in': 2025},
                    'end': {'in': 2025}
                }]
            })
            seen_lsts.add(lst)

    if 'alternate_names' in props and props['alternate_names']:
        for alt_name in props['alternate_names'].split(';'):
            alt_name = alt_name.strip()
            if alt_name and alt_name != name:
                lst = f"{alt_name}@und"
                if lst not in seen_lsts:
                    toponyms.append({
                        'toponym_id': lst,
                        'timespans': [{
                            'start': {'in': 2025},
                            'end': {'in': 2025}
                        }]
                    })
                    seen_lsts.add(lst)

    # Extract geometry
    if not geometry:
        lat = lang_obj.get('latitude', props.get('latitude'))
        lon = lang_obj.get('longitude', props.get('longitude'))

        if lat is not None and lon is not None:
            try:
                lat = float(lat)
                lon = float(lon)
                geometry = {
                    'type': 'Point',
                    'coordinates': [lon, lat]
                }
            except (ValueError, TypeError):
                return None
        else:
            return None

    # Check for wrapped coordinates
    if geometry and geometry.get('type') == 'Point':
        coords = geometry.get('coordinates', [])
        if len(coords) == 2:
            lon, lat = coords
            if lon > 180:
                lon = lon - 360
                geometry['coordinates'] = [lon, lat]

    rep_point = None  # computed by enrich_geometry

    # Build place document
    timespans = [{'start': {'in': 2025}, 'end': {'in': 2025}}]
    geom_entry = enrich_geometry(geometry, timespans=timespans)
    place_doc = {
        'place_id': place_id,
        'title': name,
        'toponyms': toponyms,
        'geometries': [geom_entry] if geom_entry else [],
    }

    # Add place type
    types = []
    language_family = lang_obj.get('language_family', props.get('language_family'))
    if language_family:
        types.append({
            'identifier': 'language-location',
            'label': 'dplace',
            'sourceLabel': f"language:{language_family}"
        })

    society_type = props.get('society_type')
    if society_type:
        types.append({
            'identifier': 'society-location',
            'label': 'dplace',
            'sourceLabel': f"society:{society_type}"
        })

    if not types:
        types.append({
            'identifier': 'cultural-location',
            'label': 'dplace',
            'sourceLabel': 'dplace-location'
        })

    place_doc['types'] = types

    # Add relations
    relations = []

    if glottocode:
        relations.append({
            'relation_type': 'sameAs',
            'related_place_id': f"glottolog:{glottocode}",
            'label': 'Glottolog'
        })

    iso_code = lang_obj.get('iso_code', props.get('iso_code'))
    if iso_code:
        relations.append({
            'relation_type': 'hasIdentifier',
            'related_place_id': f"iso639:{iso_code}",
            'label': f"ISO 639-3: {iso_code}"
        })

    ethnologue_id = lang_obj.get('ethnologue_id', props.get('ethnologue_id'))
    if ethnologue_id:
        relations.append({
            'relation_type': 'sameAs',
            'related_place_id': f"ethnologue:{ethnologue_id}",
            'label': 'Ethnologue'
        })

    hraf_id = lang_obj.get('hraf_id', props.get('hraf_id'))
    if hraf_id:
        relations.append({
            'relation_type': 'sameAs',
            'related_place_id': f"hraf:{hraf_id}",
            'label': f"HRAF: {lang_obj.get('hraf_name', hraf_id)}"
        })

    if relations:
        place_doc['relations'] = relations

    if language_family:
        place_doc['language_family'] = language_family

    region = lang_obj.get('region', props.get('region'))
    if region:
        place_doc['region'] = region

    population = props.get('population')
    if population:
        try:
            place_doc['population'] = int(population)
        except (ValueError, TypeError):
            pass

    year = lang_obj.get('year', props.get('year', props.get('time_period')))
    if year:
        try:
            year = int(year)
            place_doc['time_period'] = year
            place_doc['geometries'][0]['timespans'] = [{
                'start': {'in': year},
                'end': {'in': year}
            }]
        except (ValueError, TypeError):
            pass

    return place_doc


def index_dplace_file(geojson_file, places_index='places'):
    """Process D-PLACE GeoJSON file and index to Elasticsearch."""
    print(f"Processing D-PLACE file: {geojson_file}")

    if not os.path.exists(geojson_file):
        standard_path = Path(DATA_DIR) / 'authorities' / 'dp' / Path(geojson_file).name
        if standard_path.exists():
            geojson_file = standard_path
        else:
            print(f"ERROR: File not found: {geojson_file}")
            return

    places_batch = []
    places_count = 0
    skipped = 0
    errors = 0

    print(f"Reading D-PLACE data from {geojson_file}...")

    try:
        with open(geojson_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}")
        return
    except Exception as e:
        print(f"ERROR: Could not read file: {e}")
        return

    if isinstance(data, dict):
        if data.get('type') == 'FeatureCollection':
            features = data.get('features', [])
        elif 'features' in data:
            features = data['features']
        else:
            print("ERROR: No features found")
            return
    elif isinstance(data, list):
        features = data
    else:
        print(f"ERROR: Unexpected data structure")
        return

    print(f"Found {len(features)} D-PLACE features")

    start_time = datetime.now()

    for i, feature in enumerate(features):
        if (i + 1) % 100 == 0:
            elapsed = (datetime.now() - start_time).seconds
            rate = i / elapsed if elapsed > 0 else 0
            print(f"\r  Processing {i + 1}/{len(features)} ({rate:.1f}/sec) - indexed: {places_count}", end='', flush=True)

        try:
            place_doc = process_dplace_feature(feature)

            if not place_doc:
                skipped += 1
                continue

            places_batch.append({
                '_index': places_index,
                '_id': place_doc['place_id'],
                '_source': place_doc
            })

            if len(places_batch) >= BATCH_SIZE:
                try:
                    success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
                    places_count += success
                    if failed > 0:
                        errors += failed
                    places_batch = []
                except Exception as e:
                    print(f"  ERROR: {e}")
                    errors += len(places_batch)
                    places_batch = []

        except Exception as e:
            print(f"  ERROR processing feature {i}: {e}")
            errors += 1
            continue

    if places_batch:
        try:
            success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
            places_count += success
            if failed > 0:
                errors += failed
        except Exception as e:
            print(f"ERROR: {e}")
            errors += len(places_batch)

    elapsed = (datetime.now() - start_time).seconds

    print(f"\n{'=' * 80}")
    print(f"D-PLACE INDEXING COMPLETE")
    print(f"{'=' * 80}")
    print(f"Time: {elapsed}s")
    print(f"Indexed: {places_count:,}")
    print(f"Skipped: {skipped:,}")
    print(f"Errors: {errors:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Index D-PLACE data')
    parser.add_argument('--file', help='Path to GeoJSON file')
    parser.add_argument('--places-index', default='places', help='Target index')

    args = parser.parse_args()

    if args.file:
        geojson_file = args.file
    else:
        dplace_files = DPLACE_CONFIG.get('files', [])
        if not dplace_files:
            print("ERROR: No D-PLACE files configured")
            sys.exit(1)

        file_url = dplace_files[0]['url']
        filename = Path(file_url).name
        if not filename:
            filename = 'languages.geojson'

        geojson_file = Path(DATA_DIR) / 'authorities' / 'dp' / filename

    print(f"Starting D-PLACE ingestion (SCHEMA V2)")
    print(f"File: {geojson_file}")
    print(f"Target: {args.places_index}\n")

    index_dplace_file(str(geojson_file), args.places_index)
    create_checkpoint_snapshot(es, "dplace_data")