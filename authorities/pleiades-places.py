# processing/pleiades-places.py

"""
Index Pleiades places data into Elasticsearch with memory-efficient streaming.

This version uses ijson to stream parse the large JSON file instead of
loading it entirely into memory.
"""

import gzip
import ijson
from elasticsearch8 import Elasticsearch, helpers

from processing.settings import ES_HOST, BATCH_SIZE, DATA_DIR
from processing.helpers import compute_representative_point

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

    Returns: tuple of (toponyms_list, toponym_docs)
    """
    toponyms_list = []  # For places.toponyms array
    toponym_docs = []  # For toponyms index
    place_id = f"pl:{pleiades_record['id']}"

    for name_obj in pleiades_record.get('names', []):
        romanized = name_obj.get('romanized', '')
        attested = name_obj.get('attested', '')

        # Use attested form if available, otherwise romanized
        name_str = attested if attested else romanized
        if not name_str:
            continue

        # Split comma-separated names (common in Pleiades data)
        names = [n.strip() for n in name_str.split(',') if n.strip()]

        for name in names:
            # Get language
            lang = name_obj.get('language', 'und')
            if not lang:
                lang = 'und'

            # Build toponym in name@lang format
            toponym = f"{name}@{lang}"
            toponyms_list.append(toponym)

            # Create toponym document
            toponym_doc = {
                'place_id': place_id,
                'name': toponym  # Full name@lang format
            }

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
                    toponym_doc['timespans'] = [timespan]

            # Add to completion suggester
            toponym_doc['suggest'] = {
                'input': [name],
                'contexts': {
                    'lang': [lang.split('-')[0] if '-' in lang else lang]
                }
            }

            toponym_docs.append(toponym_doc)

    return toponyms_list, toponym_docs


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

    Returns: tuple of (place_doc, toponym_docs)
    """
    place_id = f"pl:{pleiades_record['id']}"

    # Extract toponyms
    toponyms_list, toponym_docs = extract_toponyms(pleiades_record)

    doc = {
        'place_id': place_id,
        'label': pleiades_record.get('title', ''),
        'toponyms': toponyms_list,
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
                    'relationTo': f"pl:{conn_id}",
                    'source': 'pleiades',
                    'method': 'curated',
                    'certainty': 0.9
                })
        if relations:
            doc['relations'] = relations

    return doc, toponym_docs


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
        # Detect if file is gzipped or plain JSON
        is_gzipped = file_path.endswith('.gz')

        if is_gzipped:
            # Try to open as gzipped
            try:
                file_obj = gzip.open(file_path, 'rb')
                # Test read
                test_bytes = file_obj.read(100)
                file_obj.seek(0)
            except gzip.BadGzipFile:
                print("Warning: File has .gz extension but is not gzipped, opening as plain JSON")
                file_obj = open(file_path, 'rb')
        else:
            file_obj = open(file_path, 'rb')

        with file_obj:
            # Read first bytes to detect format
            first_bytes = file_obj.read(1000)
            first_text = first_bytes.decode('utf-8', errors='ignore')
            is_graph = '@graph' in first_text

            # Reset to beginning
            file_obj.seek(0)

            if is_graph:
                # Parse @graph array
                parser = ijson.items(file_obj, '@graph.item')
                print("Detected JSON-LD format with @graph")
            else:
                # Parse top-level array
                parser = ijson.items(file_obj, 'item')
                print("Detected JSON array format")

            # Process each record as it's parsed
            for i, record in enumerate(parser):
                if (i + 1) % 1000 == 0:
                    print(f"Processed {i + 1:,} records... (places: {place_count:,}, toponyms: {toponym_count:,})")

                try:
                    # Create place document
                    place_doc, toponym_docs = create_place_doc(record)

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

                    # Add toponyms to batch
                    for j, toponym_doc in enumerate(toponym_docs):
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
            place_doc, toponym_docs = create_place_doc(record)

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

            # Add toponyms
            for j, toponym_doc in enumerate(toponym_docs):
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
    # Updated to match settings.py configuration
    # File path updated to match fetch_authorities.py structure
    PLEIADES_FILE = f"{DATA_DIR}/Pleiades/pleiades-places-latest.json.gz"
    PLACES_INDEX = "places"
    TOPONYMS_INDEX = "toponyms"

    print(f"Starting to index Pleiades from {PLEIADES_FILE}")
    print(f"Target indices: {PLACES_INDEX}, {TOPONYMS_INDEX}\n")

    index_pleiades_streaming(PLEIADES_FILE, PLACES_INDEX, TOPONYMS_INDEX)