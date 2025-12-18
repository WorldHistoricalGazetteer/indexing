# processing/indexvillaris-places.py

"""
Index Index Villaris (1680) historical place data into Elasticsearch.

Index Villaris is a gazetteer of English market towns and villages from 1680,
compiled by John Adams. This dataset provides:
- Historical place names from the 17th century
- Locations matched to modern coordinates via GB1900/OSM/Wikidata
- Parish and county information
- Market day information for market towns

The data has been reconciled with modern gazetteers to provide coordinates.

Updated to use temporal scoping design with 1680 as timespan.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime
from processing.helpers import compute_representative_point

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE, AUTHORITIES
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)

# Get Index Villaris configuration
IV_CONFIG = next((auth for auth in AUTHORITIES if auth['namespace'] == 'iv'), None)
if not IV_CONFIG:
    print("ERROR: Index Villaris configuration not found in AUTHORITIES")
    sys.exit(1)


def process_iv_entry(entry, namespace='iv'):
    """
    Process an Index Villaris entry.

    Expected structure (may vary):
    {
        "id": "IV_12345",
        "name": "Historical Place Name",
        "modern_name": "Modern Equivalent",
        "county": "Historical County",
        "parish": "Parish Name",
        "market_day": "Wednesday",
        "coordinates": [lon, lat],
        "gb1900_id": "GB1900 identifier",
        "osm_id": "OSM identifier",
        "wikidata_id": "Wikidata Q-number",
        "confidence": 0.95
    }

    Returns: place_doc dict (no separate toponym docs in new design)
    """

    # Handle both direct entries and GeoJSON features
    if 'properties' in entry:
        # GeoJSON feature
        props = entry.get('properties', {})
        geometry = entry.get('geometry')
    else:
        # Direct object
        props = entry
        geometry = None

        # Try to build geometry from coordinates
        if 'coordinates' in props:
            coords = props['coordinates']
            if isinstance(coords, list) and len(coords) == 2:
                geometry = {
                    'type': 'Point',
                    'coordinates': coords
                }
        elif 'longitude' in props and 'latitude' in props:
            try:
                geometry = {
                    'type': 'Point',
                    'coordinates': [
                        float(props['longitude']),
                        float(props['latitude'])
                    ]
                }
            except (ValueError, TypeError):
                pass

    # Extract core fields
    iv_id = props.get('id', props.get('iv_id', ''))
    historical_name = props.get('name', props.get('historical_name', ''))

    if not iv_id or not historical_name:
        return None

    # Create place ID
    place_id = f"{namespace}:{iv_id.replace('IV_', '')}"

    # Build toponyms with temporal scoping
    toponyms = []
    seen_lsts = set()

    # Historical name (1680)
    lst = f"{historical_name}@en"
    if lst not in seen_lsts:
        toponyms.append({
            'toponym_id': lst,
            'timespan': {
                'start': {'in': 1680},
                'end': {'in': 1680}
            }
        })
        seen_lsts.add(lst)

    # Modern name if different
    modern_name = props.get('modern_name', props.get('modern', ''))
    if modern_name and modern_name != historical_name:
        lst = f"{modern_name}@en"
        if lst not in seen_lsts:
            # Modern name gets current scope
            toponyms.append({
                'toponym_id': lst,
                'timespan': {
                    'start': {'in': 2000},
                    'end': {'in': 2025}
                }
            })
            seen_lsts.add(lst)

    # Alternative names
    if 'alternative_names' in props:
        alt_names = props['alternative_names']
        if isinstance(alt_names, str):
            alt_names = [alt_names]

        for alt_name in alt_names:
            if alt_name and alt_name not in [historical_name, modern_name]:
                lst = f"{alt_name}@en"
                if lst not in seen_lsts:
                    # Alternative names get 1680 scope
                    toponyms.append({
                        'toponym_id': lst,
                        'timespan': {
                            'start': {'in': 1680},
                            'end': {'in': 1680}
                        }
                    })
                    seen_lsts.add(lst)

    # Check for geometry
    if not geometry:
        return None  # Skip entries without location

    rep_point = compute_representative_point(geometry)

    # Build place document
    place_doc = {
        'place_id': place_id,
        'label': historical_name,
        'toponyms': toponyms,
        'source': 'indexvillaris',
        'locations': [{
            'geometry': geometry,
            'rep_point': rep_point,
            'timespans': [{
                'start': 1680,
                'end': 1680
            }]
        }],
        'ccodes': ['GB'],  # All Index Villaris places are in Great Britain
    }

    # Add place type
    types = []

    # Determine type from properties
    if 'market_day' in props and props['market_day']:
        types.append({
            'identifier': 'market-town',
            'label': 'indexvillaris',
            'sourceLabel': 'market town (1680)'
        })
    else:
        types.append({
            'identifier': 'settlement',
            'label': 'indexvillaris',
            'sourceLabel': 'village/town (1680)'
        })

    place_doc['types'] = types

    # Add relations to modern gazetteers
    relations = []

    # GB1900 link
    if 'gb1900_id' in props and props['gb1900_id']:
        relations.append({
            'relationType': 'sameAs',
            'relationTo': f"gb:{props['gb1900_id']}",
            'source': 'indexvillaris',
            'method': 'reconciled',
            'certainty': props.get('confidence', 0.8)
        })

    # OSM link
    if 'osm_id' in props and props['osm_id']:
        relations.append({
            'relationType': 'sameAs',
            'relationTo': f"osm:{props['osm_id']}",
            'source': 'indexvillaris',
            'method': 'reconciled',
            'certainty': props.get('confidence', 0.8)
        })

    # Wikidata link
    if 'wikidata_id' in props and props['wikidata_id']:
        wd_id = props['wikidata_id']
        if not wd_id.startswith('Q'):
            wd_id = f"Q{wd_id}"
        relations.append({
            'relationType': 'sameAs',
            'relationTo': f"wd:{wd_id}",
            'source': 'indexvillaris',
            'method': 'reconciled',
            'certainty': props.get('confidence', 0.8)
        })

    if relations:
        place_doc['relations'] = relations

    # Add administrative information
    if 'county' in props and props['county']:
        place_doc['historical_county'] = props['county']

    if 'parish' in props and props['parish']:
        place_doc['parish'] = props['parish']

    # Add market day for market towns
    if 'market_day' in props and props['market_day']:
        place_doc['market_day'] = props['market_day']

    # Add confidence score if available
    if 'confidence' in props:
        try:
            place_doc['location_confidence'] = float(props['confidence'])
        except (ValueError, TypeError):
            pass

    return place_doc


def index_iv_file(json_file, places_index='places'):
    """
    Process Index Villaris JSON file and index to Elasticsearch.

    Note: With new design, we only index places.
    Toponyms will be indexed separately by cross-authority deduplication.
    """

    print(f"Processing Index Villaris file: {json_file}")

    # Check file exists
    if not os.path.exists(json_file):
        # Try standard location
        standard_path = Path(DATA_DIR) / 'authorities' / 'iv' / Path(json_file).name
        if standard_path.exists():
            json_file = standard_path
        else:
            print(f"ERROR: File not found: {json_file}")
            print("\nTo download Index Villaris data, run:")
            print("  python -m processing.fetch_authorities -n iv")
            return

    places_batch = []
    places_count = 0
    skipped = 0
    no_coords = 0

    print(f"Reading Index Villaris data from {json_file}...")

    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {json_file}: {e}")
        return
    except Exception as e:
        print(f"ERROR: Could not read {json_file}: {e}")
        return

    # Handle different possible structures
    entries = []

    if isinstance(data, dict):
        if data.get('type') == 'FeatureCollection':
            entries = data.get('features', [])
        elif 'entries' in data:
            entries = data['entries']
        elif 'places' in data:
            entries = data['places']
        else:
            # Try to extract from nested structure
            for key in ['data', 'items', 'records']:
                if key in data:
                    entries = data[key]
                    break
    elif isinstance(data, list):
        entries = data

    if not entries:
        print(f"ERROR: No entries found in {json_file}")
        print(f"Data structure: {type(data)}")
        if isinstance(data, dict):
            print(f"Keys: {list(data.keys())[:10]}")
        return

    print(f"Found {len(entries)} Index Villaris entries to process")

    start_time = datetime.now()

    for i, entry in enumerate(entries):
        if (i + 1) % 500 == 0:
            elapsed = (datetime.now() - start_time).seconds
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  Processing entry {i + 1}/{len(entries)} "
                  f"({rate:.1f}/sec) - "
                  f"indexed: {places_count}, no coords: {no_coords}, skipped: {skipped}")

        try:
            place_doc = process_iv_entry(entry)

            if not place_doc:
                # Check if it was missing coordinates
                if 'name' in entry or (
                        isinstance(entry, dict) and 'properties' in entry and 'name' in entry['properties']):
                    no_coords += 1
                else:
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
                        print(f"    WARNING: {failed} places failed to index")
                    places_batch = []
                except Exception as e:
                    print(f"    ERROR indexing places batch: {e}")
                    places_batch = []

        except Exception as e:
            print(f"  ERROR processing entry {i}: {e}")
            if i < 3:  # Show first few problematic entries
                print(f"    Entry: {json.dumps(entry, indent=2)[:500]}...")
            skipped += 1
            continue

    # Index remaining batch
    if places_batch:
        try:
            success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
            places_count += success
        except Exception as e:
            print(f"ERROR indexing final places batch: {e}")

    elapsed = (datetime.now() - start_time).seconds

    print(f"\n{'=' * 80}")
    print(f"INDEX VILLARIS INDEXING COMPLETE")
    print(f"{'=' * 80}")
    print(f"Time elapsed: {elapsed} seconds")
    print(f"Places indexed: {places_count:,}")
    print(f"Missing coordinates: {no_coords:,}")
    print(f"Skipped (other): {skipped:,}")

    # Verify in Elasticsearch
    iv_count = es.count(
        index=places_index,
        body={'query': {'prefix': {'place_id': 'iv:'}}}
    )['count']

    print(f"\nTotal Index Villaris places now in index: {iv_count:,}")

    # Show sample of indexed places
    if iv_count > 0:
        print("\nSample of indexed places:")
        sample = es.search(
            index=places_index,
            body={
                'query': {'prefix': {'place_id': 'iv:'}},
                'size': 3,
                '_source': ['place_id', 'label', 'historical_county', 'market_day']
            }
        )

        for hit in sample['hits']['hits']:
            doc = hit['_source']
            info = f"  {doc['place_id']}: {doc['label']}"
            if 'historical_county' in doc:
                info += f" ({doc['historical_county']})"
            if 'market_day' in doc:
                info += f" - Market: {doc['market_day']}"
            print(info)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Index Index Villaris (1680) historical place data into Elasticsearch'
    )
    parser.add_argument(
        '--file',
        help='Path to Index Villaris JSON file (default: auto-detect from settings)'
    )
    parser.add_argument(
        '--places-index',
        default='places',
        help='Target places index name (default: places)'
    )

    args = parser.parse_args()

    if args.file:
        json_file = args.file
    else:
        # Get from configuration
        iv_files = IV_CONFIG.get('files', [])
        if not iv_files:
            print("ERROR: No Index Villaris files configured")
            sys.exit(1)

        # Extract filename from URL
        file_url = iv_files[0]['url']
        # The URL ends with the filename
        filename = Path(file_url).name
        if not filename:
            filename = 'IV-GB1900-OSM-WD.lp.json'

        json_file = Path(DATA_DIR) / 'authorities' / 'iv' / filename

    print(f"Starting Index Villaris ingestion")
    print(f"File: {json_file}")
    print(f"Target index: {args.places_index}")
    print(f"Historical period: 1680")
    print()

    index_iv_file(str(json_file), args.places_index)
    create_checkpoint_snapshot(es, "indexvillaris_places")