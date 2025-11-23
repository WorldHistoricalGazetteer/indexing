# processing/nativeland-places.py

"""
Index Native Land Digital data into Elasticsearch.

Native Land provides geographic data about indigenous:
- Territories: Traditional indigenous territories
- Languages: Areas where indigenous languages are/were spoken
- Treaties: Historical treaty boundaries

Data comes as GeoJSON with polygon geometries.
Each feature represents a distinct cultural/political boundary.
"""

import json
import os
import sys
from pathlib import Path
from elasticsearch8 import Elasticsearch, helpers

from processing.settings import ES_HOST, BATCH_SIZE, DATA_DIR, AUTHORITIES
from processing.helpers import (
    compute_representative_point,
    compute_bbox,
    compute_area_km2,
    simplify_geometry
)

es = Elasticsearch(ES_HOST)

# Get Native Land configuration
NL_CONFIG = next((auth for auth in AUTHORITIES if auth['namespace'] == 'nl'), None)
if not NL_CONFIG:
    print("ERROR: Native Land configuration not found in AUTHORITIES")
    sys.exit(1)


def process_territory(feature, namespace='nl'):
    """Process a Native Land territory feature."""

    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    # Extract key properties
    name = props.get('Name', props.get('name', ''))
    if not name:
        return None, []

    # Generate place ID
    slug = props.get('Slug', props.get('slug', name.lower().replace(' ', '-')))
    place_id = f"{namespace}:territory:{slug}"

    # Build toponyms
    toponyms = [f"{name}@en"]  # Native Land data is primarily in English
    toponym_docs = []

    # Sometimes there are alternate names in description
    description = props.get('description', '')

    # Create primary toponym document
    toponym_docs.append({
        'place_id': place_id,
        'name': f"{name}@en",
        'is_preferred': True,
        'suggest': {
            'input': [name],
            'contexts': {'lang': ['en']}
        }
    })

    # Simplify geometry for large polygons
    if geometry:
        geometry = simplify_geometry(geometry, tolerance_km=1.0)
        rep_point = compute_representative_point(geometry)
        bbox = compute_bbox(geometry)
        area = compute_area_km2(geometry)
    else:
        return None, []

    # Build place document
    place_doc = {
        'place_id': place_id,
        'label': name,
        'toponyms': toponyms,
        'source': 'nativeland',
        'locations': [{
            'geometry': geometry,
            'rep_point': rep_point
        }],
        'types': [{
            'identifier': 'indigenous-territory',
            'label': 'nativeland',
            'sourceLabel': 'territory'
        }]
    }

    # Add bounding box if computed
    if bbox:
        place_doc['bbox'] = bbox

    # Add area if computed
    if area:
        place_doc['area_km2'] = round(area, 2)

    # Add description as a property
    if description:
        place_doc['description'] = description

    # Add color (used for map styling)
    if 'color' in props:
        place_doc['display_color'] = props['color']

    # Add any related links
    relations = []
    if 'FrenchName' in props and props['FrenchName']:
        toponyms.append(f"{props['FrenchName']}@fr")
        toponym_docs.append({
            'place_id': place_id,
            'name': f"{props['FrenchName']}@fr",
            'suggest': {
                'input': [props['FrenchName']],
                'contexts': {'lang': ['fr']}
            }
        })

    return place_doc, toponym_docs


def process_language(feature, namespace='nl'):
    """Process a Native Land language area feature."""

    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    # Extract key properties
    name = props.get('Name', props.get('name', ''))
    if not name:
        return None, []

    # Generate place ID
    slug = props.get('Slug', props.get('slug', name.lower().replace(' ', '-')))
    place_id = f"{namespace}:language:{slug}"

    # Build toponyms
    toponyms = [f"{name}@en"]
    toponym_docs = []

    # Create primary toponym document
    toponym_docs.append({
        'place_id': place_id,
        'name': f"{name}@en",
        'is_preferred': True,
        'suggest': {
            'input': [name],
            'contexts': {'lang': ['en']}
        }
    })

    # Simplify geometry
    if geometry:
        geometry = simplify_geometry(geometry, tolerance_km=1.0)
        rep_point = compute_representative_point(geometry)
        area = compute_area_km2(geometry)
    else:
        return None, []

    # Build place document
    place_doc = {
        'place_id': place_id,
        'label': name,
        'toponyms': toponyms,
        'source': 'nativeland',
        'locations': [{
            'geometry': geometry,
            'rep_point': rep_point
        }],
        'types': [{
            'identifier': 'indigenous-language-area',
            'label': 'nativeland',
            'sourceLabel': 'language'
        }]
    }

    # Add area if computed
    if area:
        place_doc['area_km2'] = round(area, 2)

    # Add color for map styling
    if 'color' in props:
        place_doc['display_color'] = props['color']

    return place_doc, toponym_docs


def process_treaty(feature, namespace='nl'):
    """Process a Native Land treaty boundary feature."""

    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    # Extract key properties
    name = props.get('Name', props.get('name', ''))
    if not name:
        return None, []

    # Generate place ID
    slug = props.get('Slug', props.get('slug', name.lower().replace(' ', '-')))
    place_id = f"{namespace}:treaty:{slug}"

    # Build toponyms
    toponyms = [f"{name}@en"]
    toponym_docs = []

    # Create primary toponym document
    toponym_docs.append({
        'place_id': place_id,
        'name': f"{name}@en",
        'is_preferred': True,
        'suggest': {
            'input': [name],
            'contexts': {'lang': ['en']}
        }
    })

    # Simplify geometry
    if geometry:
        geometry = simplify_geometry(geometry, tolerance_km=1.0)
        rep_point = compute_representative_point(geometry)
        area = compute_area_km2(geometry)
    else:
        return None, []

    # Build place document
    place_doc = {
        'place_id': place_id,
        'label': name,
        'toponyms': toponyms,
        'source': 'nativeland',
        'locations': [{
            'geometry': geometry,
            'rep_point': rep_point
        }],
        'types': [{
            'identifier': 'treaty-area',
            'label': 'nativeland',
            'sourceLabel': 'treaty'
        }]
    }

    # Add area if computed
    if area:
        place_doc['area_km2'] = round(area, 2)

    # Add treaty date if available
    if 'Date' in props:
        place_doc['treaty_date'] = props['Date']

    # Add color for map styling
    if 'color' in props:
        place_doc['display_color'] = props['color']

    return place_doc, toponym_docs


def index_nativeland_file(json_file, data_type, places_index='places', toponyms_index='toponyms'):
    """
    Process a Native Land GeoJSON file and index to Elasticsearch.

    Args:
        json_file: Path to GeoJSON file
        data_type: One of 'territories', 'languages', 'treaties'
    """

    print(f"Processing Native Land {data_type} file: {json_file}")

    if not os.path.exists(json_file):
        # Check standard location
        standard_path = Path(DATA_DIR) / 'authorities' / 'nl' / Path(json_file).name
        if standard_path.exists():
            json_file = standard_path
        else:
            print(f"ERROR: File not found: {json_file}")
            return

    # Select processor based on type
    if data_type == 'territories':
        processor = process_territory
    elif data_type == 'languages':
        processor = process_language
    elif data_type == 'treaties':
        processor = process_treaty
    else:
        print(f"ERROR: Unknown data type: {data_type}")
        return

    places_batch = []
    toponyms_batch = []
    places_count = 0
    toponyms_count = 0
    skipped = 0

    print(f"Reading {data_type} data...")

    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {json_file}: {e}")
        return
    except Exception as e:
        print(f"ERROR: Could not read {json_file}: {e}")
        return

    # Handle both direct FeatureCollection and nested structure
    if data.get('type') == 'FeatureCollection':
        features = data.get('features', [])
    elif 'features' in data:
        features = data['features']
    else:
        print(f"ERROR: No features found in {json_file}")
        return

    print(f"Found {len(features)} {data_type} to process")

    for i, feature in enumerate(features):
        if (i + 1) % 100 == 0:
            print(f"  Processing {data_type} {i + 1}/{len(features)}...")

        try:
            place_doc, toponym_docs = processor(feature, namespace='nl')

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

            # Add toponyms
            for j, toponym_doc in enumerate(toponym_docs):
                toponyms_batch.append({
                    '_index': toponyms_index,
                    '_id': f"{place_id}:{j}",
                    '_source': toponym_doc
                })

            # Bulk index when batch is full
            if len(places_batch) >= BATCH_SIZE:
                try:
                    success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
                    places_count += success
                    places_batch = []
                except Exception as e:
                    print(f"  Error indexing places: {e}")
                    places_batch = []

            if len(toponyms_batch) >= BATCH_SIZE:
                try:
                    success, failed = helpers.bulk(es, toponyms_batch, raise_on_error=False, stats_only=True)
                    toponyms_count += success
                    toponyms_batch = []
                except Exception as e:
                    print(f"  Error indexing toponyms: {e}")
                    toponyms_batch = []

        except Exception as e:
            print(f"  Error processing feature {i}: {e}")
            skipped += 1
            continue

    # Index remaining batches
    if places_batch:
        try:
            success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
            places_count += success
        except Exception as e:
            print(f"Error indexing final places batch: {e}")

    if toponyms_batch:
        try:
            success, failed = helpers.bulk(es, toponyms_batch, raise_on_error=False, stats_only=True)
            toponyms_count += success
        except Exception as e:
            print(f"Error indexing final toponyms batch: {e}")

    print(f"\nIndexing complete for {data_type}!")
    print(f"Places indexed: {places_count:,}")
    print(f"Toponyms indexed: {toponyms_count:,}")
    print(f"Skipped: {skipped:,}")


def index_all_nativeland():
    """Index all Native Land data files."""

    print("=" * 80)
    print("NATIVE LAND DATA INDEXING")
    print("=" * 80)

    nl_dir = Path(DATA_DIR) / 'authorities' / 'nl'

    # Check if directory exists
    if not nl_dir.exists():
        print(f"ERROR: Native Land data directory not found: {nl_dir}")
        print("\nTo download Native Land data, you need an API key from https://native-land.ca/")
        print("Then run:")
        print("  python -m processing.fetch_authorities -n nl --nl-api-key YOUR_KEY")
        return

    # Process each file type
    file_types = [
        ('territories.json', 'territories'),
        ('languages.json', 'languages'),
        ('treaties.json', 'treaties')
    ]

    total_places = 0
    total_toponyms = 0

    for filename, data_type in file_types:
        file_path = nl_dir / filename

        if not file_path.exists():
            print(f"\nSkipping {data_type}: {filename} not found")
            continue

        print(f"\n--- Processing {data_type.upper()} ---")

        # Get counts before indexing
        before_places = es.count(
            index='places',
            body={'query': {'prefix': {'place_id': 'nl:'}}}
        )['count']

        index_nativeland_file(str(file_path), data_type)

        # Get counts after indexing
        after_places = es.count(
            index='places',
            body={'query': {'prefix': {'place_id': 'nl:'}}}
        )['count']

        added = after_places - before_places
        total_places += added
        print(f"Added {added} new places from {data_type}")

    print("\n" + "=" * 80)
    print("NATIVE LAND INDEXING COMPLETE")
    print("=" * 80)
    print(f"Total Native Land places in index: {total_places:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Index Native Land Digital data into Elasticsearch'
    )
    parser.add_argument(
        '--file',
        help='Path to specific Native Land GeoJSON file'
    )
    parser.add_argument(
        '--type',
        choices=['territories', 'languages', 'treaties'],
        help='Type of data in file'
    )
    parser.add_argument(
        '--all',
        action='store_true',
        help='Index all Native Land files'
    )

    args = parser.parse_args()

    if args.all or (not args.file and not args.type):
        index_all_nativeland()
    elif args.file and args.type:
        index_nativeland_file(args.file, args.type)
    else:
        print("ERROR: Must specify both --file and --type, or use --all")
        parser.print_help()