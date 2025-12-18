# processing/dplace-places.py

"""
Index D-PLACE (Database of Places, Language, Culture, and Environment) data.

D-PLACE aggregates data on cultural, linguistic, and environmental variation
across human societies. The geographic data primarily consists of:
- Language family locations
- Society/culture locations
- Environmental zones

Data comes as GeoJSON with point geometries representing society locations.

Updated to use temporal scoping design. D-PLACE is current data (2025).
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime
from processing.helpers import compute_representative_point, compute_bbox

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
    """
    Process a D-PLACE feature (language/society location).

    D-PLACE features typically include:
    - id: Unique identifier (e.g., "xd123")
    - name: Society/language name
    - language_family: Language family classification
    - glottocode: Glottolog identifier
    - iso_code: ISO 639-3 language code
    - latitude/longitude: Location coordinates
    - region: Geographic region

    Returns: place_doc dict
    """

    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    # D-PLACE stores language data nested in properties.language
    lang_obj = props.get('language', {})

    # Extract core identifiers - check multiple locations
    feature_id = (
            feature.get('id') or  # Feature-level ID
            lang_obj.get('xid') or
            lang_obj.get('id') or
            props.get('id') or
            props.get('xd_id', '')
    )

    # Name can be at multiple levels
    name = (
            props.get('name') or
            lang_obj.get('name') or
            lang_obj.get('language') or
            props.get('society_name') or
            props.get('language_name', '')
    )

    if not name or not feature_id:
        return None

    # Create place ID
    place_id = f"{namespace}:{feature_id}"

    # Build toponyms with temporal scoping
    toponyms = []
    seen_lsts = set()

    # Primary name
    # Check if we have a glottocode for language identification
    glottocode = lang_obj.get('glottocode', props.get('glottocode', ''))

    # For now, use 'und' (undetermined) as we don't have a glottocode->ISO639 mapping
    lang_code = 'und'

    lst = f"{name}@{lang_code}"
    if lst not in seen_lsts:
        # D-PLACE is current data - use 2025
        toponyms.append({
            'toponym_id': lst,
            'timespan': {
                'start': {'in': 2025},
                'end': {'in': 2025}
            }
        })
        seen_lsts.add(lst)

    # Add name_in_source if different
    name_in_source = lang_obj.get('name_in_source', '')
    if name_in_source and name_in_source != name:
        lst = f"{name_in_source}@{lang_code}"
        if lst not in seen_lsts:
            toponyms.append({
                'toponym_id': lst,
                'timespan': {
                    'start': {'in': 2025},
                    'end': {'in': 2025}
                }
            })
            seen_lsts.add(lst)

    # Add alternative names if present
    if 'alternate_names' in props and props['alternate_names']:
        for alt_name in props['alternate_names'].split(';'):
            alt_name = alt_name.strip()
            if alt_name and alt_name != name:
                lst = f"{alt_name}@und"
                if lst not in seen_lsts:
                    toponyms.append({
                        'toponym_id': lst,
                        'timespan': {
                            'start': {'in': 2025},
                            'end': {'in': 2025}
                        }
                    })
                    seen_lsts.add(lst)

    # Extract geometry
    if not geometry:
        # Try to build from nested language coordinates
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

    # Check for wrapped coordinates (> 180 or < -180)
    # D-PLACE sometimes uses 0-360 longitude instead of -180 to 180
    if geometry and geometry.get('type') == 'Point':
        coords = geometry.get('coordinates', [])
        if len(coords) == 2:
            lon, lat = coords
            if lon > 180:
                lon = lon - 360
                geometry['coordinates'] = [lon, lat]

    rep_point = compute_representative_point(geometry)

    # Build place document
    place_doc = {
        'place_id': place_id,
        'label': name,
        'toponyms': toponyms,
        'source': 'dplace',
        'locations': [{
            'geometry': geometry,
            'rep_point': rep_point
        }]
    }

    # Add place type
    types = []

    # Determine type based on properties
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
        # Default type
        types.append({
            'identifier': 'cultural-location',
            'label': 'dplace',
            'sourceLabel': 'dplace-location'
        })

    place_doc['types'] = types

    # Add relations
    relations = []

    # Link to Glottolog if available
    if glottocode:
        relations.append({
            'relationType': 'sameAs',
            'relationTo': f"glottolog:{glottocode}",
            'source': 'dplace',
            'method': 'curated'
        })

    # Link to ISO 639-3 if available
    iso_code = lang_obj.get('iso_code', props.get('iso_code'))
    if iso_code:
        relations.append({
            'relationType': 'hasIdentifier',
            'relationTo': f"iso639:{iso_code}",
            'label': f"ISO 639-3: {iso_code}",
            'source': 'dplace',
            'method': 'curated'
        })

    # Link to Ethnologue if available
    ethnologue_id = lang_obj.get('ethnologue_id', props.get('ethnologue_id'))
    if ethnologue_id:
        relations.append({
            'relationType': 'sameAs',
            'relationTo': f"ethnologue:{ethnologue_id}",
            'source': 'dplace',
            'method': 'curated'
        })

    # Link to HRAF if available
    hraf_id = lang_obj.get('hraf_id', props.get('hraf_id'))
    if hraf_id:
        relations.append({
            'relationType': 'sameAs',
            'relationTo': f"hraf:{hraf_id}",
            'label': f"HRAF: {lang_obj.get('hraf_name', hraf_id)}",
            'source': 'dplace',
            'method': 'curated'
        })

    if relations:
        place_doc['relations'] = relations

    # Add additional properties
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

    # Add time period if available
    year = lang_obj.get('year', props.get('year', props.get('time_period')))
    if year:
        try:
            year = int(year)
            place_doc['time_period'] = year
            # Add to location timespan
            place_doc['locations'][0]['timespans'] = [{
                'start': year,
                'end': year
            }]
        except (ValueError, TypeError):
            pass

    return place_doc


def index_dplace_file(geojson_file, places_index='places'):
    """
    Process D-PLACE GeoJSON file and index to Elasticsearch.

    Note: With new design, we only index places.
    Toponyms will be indexed separately by cross-authority deduplication.
    """

    print(f"Processing D-PLACE file: {geojson_file}")

    # Check file exists
    if not os.path.exists(geojson_file):
        # Try standard location
        standard_path = Path(DATA_DIR) / 'authorities' / 'dp' / Path(geojson_file).name
        if standard_path.exists():
            geojson_file = standard_path
        else:
            print(f"ERROR: File not found: {geojson_file}")
            print("\nTo download D-PLACE data, run:")
            print("  python -m processing.fetch_authorities -n dp")
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
        print(f"ERROR: Invalid JSON in {geojson_file}: {e}")
        return
    except Exception as e:
        print(f"ERROR: Could not read {geojson_file}: {e}")
        return

    # Extract features
    if isinstance(data, dict):
        if data.get('type') == 'FeatureCollection':
            features = data.get('features', [])
        elif 'features' in data:
            features = data['features']
        else:
            print("ERROR: No features found in GeoJSON")
            return
    elif isinstance(data, list):
        # Sometimes D-PLACE data is just an array of features
        features = data
    else:
        print(f"ERROR: Unexpected data structure in {geojson_file}")
        return

    print(f"Found {len(features)} D-PLACE features to process")

    start_time = datetime.now()

    for i, feature in enumerate(features):
        if (i + 1) % 100 == 0:
            elapsed = (datetime.now() - start_time).seconds
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  Processing feature {i + 1}/{len(features)} "
                  f"({rate:.1f}/sec) - "
                  f"indexed: {places_count}, skipped: {skipped}")

        try:
            place_doc = process_dplace_feature(feature)

            if not place_doc:
                skipped += 1
                continue

            place_id = place_doc['place_id']

            # Add to places batch
            places_batch.append({
                '_index': places_index,
                '_id': place_id,
                '_source': place_doc
            })

            # Bulk index when batch is full
            if len(places_batch) >= BATCH_SIZE:
                try:
                    success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
                    places_count += success
                    if failed > 0:
                        errors += failed
                        print(f"    WARNING: {failed} places failed to index")
                    places_batch = []
                except Exception as e:
                    print(f"    ERROR indexing places batch: {e}")
                    errors += len(places_batch)
                    places_batch = []

        except Exception as e:
            print(f"  ERROR processing feature {i}: {e}")
            if i < 5:  # Show details for first few errors
                print(f"    Feature: {json.dumps(feature, indent=2)[:500]}...")
            errors += 1
            continue

    # Index remaining batch
    if places_batch:
        try:
            success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
            places_count += success
            if failed > 0:
                errors += failed
        except Exception as e:
            print(f"ERROR indexing final places batch: {e}")
            errors += len(places_batch)

    elapsed = (datetime.now() - start_time).seconds

    print(f"\n{'=' * 80}")
    print(f"D-PLACE INDEXING COMPLETE")
    print(f"{'=' * 80}")
    print(f"Time elapsed: {elapsed} seconds")
    print(f"Places indexed: {places_count:,}")
    print(f"Skipped: {skipped:,}")
    print(f"Errors: {errors:,}")

    # Verify in Elasticsearch
    dp_count = es.count(
        index=places_index,
        body={'query': {'prefix': {'place_id': 'dp:'}}}
    )['count']

    print(f"\nTotal D-PLACE places now in index: {dp_count:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Index D-PLACE cultural and linguistic data into Elasticsearch'
    )
    parser.add_argument(
        '--file',
        help='Path to D-PLACE GeoJSON file (default: auto-detect from settings)'
    )
    parser.add_argument(
        '--places-index',
        default='places',
        help='Target places index name (default: places)'
    )

    args = parser.parse_args()

    if args.file:
        geojson_file = args.file
    else:
        # Get from configuration
        dplace_files = DPLACE_CONFIG.get('files', [])
        if not dplace_files:
            print("ERROR: No D-PLACE files configured")
            sys.exit(1)

        # Use first (and usually only) file
        file_url = dplace_files[0]['url']
        filename = Path(file_url).name
        if not filename:
            filename = 'languages.geojson'

        geojson_file = Path(DATA_DIR) / 'authorities' / 'dp' / filename

    print(f"Starting D-PLACE ingestion")
    print(f"File: {geojson_file}")
    print(f"Target index: {args.places_index}")
    print()

    index_dplace_file(str(geojson_file), args.places_index)
    create_checkpoint_snapshot(es, "dplace_data")