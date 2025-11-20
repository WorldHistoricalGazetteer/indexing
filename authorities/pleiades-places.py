# authorities/pleiades-places-streaming.py

"""
Index Pleiades places data into Elasticsearch with memory-efficient streaming.

This version uses ijson to stream parse the large JSON file instead of
loading it entirely into memory.
"""

import gzip
import ijson
from elasticsearch8 import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE, DATA_DIR
from authorities.helpers import compute_representative_point

es = Elasticsearch(ES_HOST)


def extract_locations(pleiades_record):
    """
    Extract location data from Pleiades locations array.
    Each location may have temporal attestations and geometry.

    Returns: list of location dicts for our schema
    """
    locations = []

    for loc in pleiades_record.get('locations', []):
        geometry = loc.get('geometry')
        if not geometry:
            continue

        # Compute representative point
        rep_point = compute_representative_point(geometry)

        location = {
            'geometry': geometry,
            'rep_point': rep_point
        }

        # Add temporal attestations
        attestations = loc.get('attestations', [])
        if attestations:
            timespans = []
            for att in attestations:
                # Pleiades has start/end directly on location
                start = loc.get('start')
                end = loc.get('end')
                if start is not None or end is not None:
                    timespan = {}
                    if start is not None:
                        timespan['start'] = start
                    if end is not None:
                        timespan['end'] = end
                    timespans.append(timespan)
                    break  # One timespan per location in Pleiades

            if timespans:
                location['timespans'] = timespans

        locations.append(location)

    return locations if locations else None


def extract_toponyms(pleiades_record):
    """
    Extract name data from Pleiades names array.
    Each name may have temporal attestations and language info.

    Returns: list of toponym dicts
    """
    toponyms = []
    place_id = f"pl:{pleiades_record['id']}"

    for name_obj in pleiades_record.get('names', []):
        romanized = name_obj.get('romanized', '')
        attested = name_obj.get('attested', '')

        # Use attested form if available, otherwise romanized
        name = attested if attested else romanized
        if not name:
            continue

        toponym = {
            'place_id': place_id,
            'name': name,
            'name_lower': name.lower()
        }

        # Add language if available
        lang = name_obj.get('language')
        if lang:
            toponym['lang'] = lang

        # Add temporal information
        attestations = name_obj.get('attestations', [])
        if attestations:
            # Get start/end from name object
            start = name_obj.get('start')
            end = name_obj.get('end')
            if start is not None or end is not None:
                timespan = {}
                if start is not None:
                    timespan['start'] = start
                if end is not None:
                    timespan['end'] = end
                toponym['timespans'] = [timespan]

        # Add to completion suggester
        suggest_input = [name]
        if lang:
            toponym['suggest'] = {
                'input': suggest_input,
                'contexts': {
                    'lang': [lang]
                }
            }
        else:
            toponym['suggest'] = {
                'input': suggest_input
            }

        toponyms.append(toponym)

    return toponyms


def extract_place_types(pleiades_record):
    """
    Extract place types from Pleiades.
    """
    types = []

    for place_type in pleiades_record.get('placeTypes', []):
        if place_type:
            types.append({
                'identifier': place_type,
                'label': 'pleiades',
                'sourceLabel': place_type
            })

    return types if types else None


def create_place_doc(pleiades_record):
    """
    Create a place document from a Pleiades record.

    Pleiades structure:
    - id: place ID (e.g., "413005")
    - title: place name
    - locations: array of historical locations with geometries and dates
    - names: array of historical names with attestations
    - placeTypes: array of place type strings
    - reprPoint: representative point [lon, lat]
    """
    place_id = f"pl:{pleiades_record['id']}"

    doc = {
        'place_id': place_id,
        'label': pleiades_record.get('title', ''),
        'source': 'pleiades'
    }

    # Extract locations
    locations = extract_locations(pleiades_record)
    if locations:
        doc['locations'] = locations
    elif pleiades_record.get('reprPoint'):
        # Fallback to reprPoint if no locations
        coords = pleiades_record['reprPoint']
        doc['locations'] = [{
            'geometry': {
                'type': 'Point',
                'coordinates': coords
            },
            'rep_point': {
                'lon': coords[0],
                'lat': coords[1]
            }
        }]

    # Extract place types
    types = extract_place_types(pleiades_record)
    if types:
        doc['types'] = types

    # Extract connections/relations
    connections = pleiades_record.get('connectsWith', [])
    if connections:
        relations = []
        for conn_uri in connections:
            # Extract Pleiades ID from URI
            if '/places/' in conn_uri:
                conn_id = conn_uri.split('/places/')[-1]
                relations.append({
                    'relationType': 'connectedTo',
                    'relationTo': f"pl:{conn_id}"
                })
        if relations:
            doc['relations'] = relations

    return doc


def index_pleiades_streaming(file_path, places_index, toponyms_index):
    """
    Stream parse Pleiades JSON to avoid loading entire file into memory.
    Uses ijson for incremental JSON parsing.
    """
    place_batch = []
    toponym_batch = []

    place_count = 0
    toponym_count = 0
    skipped = 0

    print(f"Streaming Pleiades data from {file_path}")
    print("Using ijson for memory-efficient parsing...")

    try:
        # Open gzipped file
        with gzip.open(file_path, 'rb') as gz_file:
            # Check if it's JSON-LD with @graph
            # We need to detect this first
            gz_file.seek(0)
            first_bytes = gzip.decompress(gz_file.read(1000)).decode('utf-8', errors='ignore')
            is_graph = '@graph' in first_bytes

            # Reset to beginning
            gz_file.seek(0)

            if is_graph:
                # Parse @graph array
                parser = ijson.items(gz_file, '@graph.item')
                print("Detected JSON-LD format with @graph")
            else:
                # Parse top-level array
                parser = ijson.items(gz_file, 'item')
                print("Detected JSON array format")

            # Process each record as it's parsed
            for i, record in enumerate(parser):
                if (i + 1) % 1000 == 0:
                    print(f"Processed {i + 1:,} records... (places: {place_count:,}, toponyms: {toponym_count:,})")

                try:
                    # Create place document
                    place_doc = create_place_doc(record)

                    # Skip if no spatial data
                    if 'locations' not in place_doc:
                        skipped += 1
                        continue

                    place_id = place_doc['place_id']

                    # Add to place batch
                    place_batch.append({
                        '_index': places_index,
                        '_id': place_id,
                        '_source': place_doc
                    })

                    # Extract toponyms
                    toponyms = extract_toponyms(record)
                    for j, toponym_doc in enumerate(toponyms):
                        toponym_batch.append({
                            '_index': toponyms_index,
                            '_id': f"{place_id}:{j}",
                            '_source': toponym_doc
                        })

                    # Bulk index places
                    if len(place_batch) >= BATCH_SIZE:
                        success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
                        place_count += success
                        place_batch = []

                    # Bulk index toponyms
                    if len(toponym_batch) >= BATCH_SIZE:
                        success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
                        toponym_count += success
                        toponym_batch = []

                except Exception as e:
                    print(f"Error processing record {record.get('id', 'unknown')}: {str(e)}")
                    skipped += 1
                    continue

    except ImportError:
        print("\nERROR: ijson library not installed")
        print("Install with: pip install ijson --break-system-packages")
        print("\nFalling back to standard (memory-intensive) method...")
        return index_pleiades_standard(file_path, places_index, toponyms_index)

    # Index remaining batches
    if place_batch:
        success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
        place_count += success

    if toponym_batch:
        success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
        toponym_count += success

    print(f"\nIndexing complete!")
    print(f"Places indexed: {place_count:,}")
    print(f"Toponyms indexed: {toponym_count:,}")
    print(f"Skipped (no location): {skipped:,}")


def index_pleiades_standard(file_path, places_index, toponyms_index):
    """
    Standard (non-streaming) method - loads entire file into memory.
    Only used as fallback if ijson not available.
    """
    import json

    place_batch = []
    toponym_batch = []

    place_count = 0
    toponym_count = 0
    skipped = 0

    print(f"Loading Pleiades data from {file_path}")
    print("WARNING: Loading entire file into memory...")

    with gzip.open(file_path, 'rt', encoding='utf-8') as f:
        data = json.load(f)

    # Handle @graph structure
    if isinstance(data, dict) and '@graph' in data:
        records = data['@graph']
    elif isinstance(data, list):
        records = data
    else:
        print("Unexpected Pleiades structure")
        return

    print(f"Found {len(records)} Pleiades records")
    print("Processing...")

    for i, record in enumerate(records):
        if (i + 1) % 1000 == 0:
            print(f"Processed {i + 1:,} records... (places: {place_count:,}, toponyms: {toponym_count:,})")

        try:
            # Create place document
            place_doc = create_place_doc(record)

            # Skip if no spatial data
            if 'locations' not in place_doc:
                skipped += 1
                continue

            place_id = place_doc['place_id']

            # Add to place batch
            place_batch.append({
                '_index': places_index,
                '_id': place_id,
                '_source': place_doc
            })

            # Extract toponyms
            toponyms = extract_toponyms(record)
            for j, toponym_doc in enumerate(toponyms):
                toponym_batch.append({
                    '_index': toponyms_index,
                    '_id': f"{place_id}:{j}",
                    '_source': toponym_doc
                })

            # Bulk index places
            if len(place_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
                place_count += success
                place_batch = []

            # Bulk index toponyms
            if len(toponym_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
                toponym_count += success
                toponym_batch = []

        except Exception as e:
            print(f"Error processing record {record.get('id', 'unknown')}: {str(e)}")
            skipped += 1
            continue

    # Index remaining batches
    if place_batch:
        success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
        place_count += success

    if toponym_batch:
        success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
        toponym_count += success

    print(f"\nIndexing complete!")
    print(f"Places indexed: {place_count:,}")
    print(f"Toponyms indexed: {toponym_count:,}")
    print(f"Skipped (no location): {skipped:,}")


if __name__ == "__main__":
    PLEIADES_FILE = f"{DATA_DIR}/pleiades/pleiades-places-latest/pleiades-places-latest.json.gz"
    PLACES_INDEX = "places"
    TOPONYMS_INDEX = "toponyms"

    print(f"Starting to index Pleiades from {PLEIADES_FILE}")
    print(f"Target indices: {PLACES_INDEX}, {TOPONYMS_INDEX}\n")

    index_pleiades_streaming(PLEIADES_FILE, PLACES_INDEX, TOPONYMS_INDEX)