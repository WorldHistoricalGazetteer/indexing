# authorities/tgn-places.py

"""
Index Getty Thesaurus of Geographic Names (TGN) data into Elasticsearch.

TGN is distributed as a ZIP archive containing multiple N-Triples files:
- TGNOut_PlaceMap.nt: Links TGN places to coordinates
- TGNOut_Coordinates.nt: Actual coordinate values
- TGNOut_2Terms.nt: Place names in multiple languages
- TGNOut_1Subjects.nt: Place metadata and hierarchy

We need to:
1. Load coordinates from TGNOut_Coordinates.nt into memory
2. Stream TGNOut_PlaceMap.nt to link places to coordinates
3. Index places with their coordinates
4. Optionally load terms for toponyms

Note: This is a simplified approach that only indexes georeferenced places.
For full TGN indexing including non-georeferenced places, additional files
would need to be processed.
"""

import re
import zipfile
from collections import defaultdict
from elasticsearch8 import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE, DATA_DIR

es = Elasticsearch(ES_HOST)


def parse_ntriple(line):
    """
    Parse an N-Triple line into (subject, predicate, object) tuple.

    Returns: (subject_uri, predicate_uri, object_value, object_type) or None
    """
    line = line.strip()
    if not line or line.startswith('#'):
        return None

    # Pattern: <subject> <predicate> <object> .
    match = re.match(r'<([^>]+)>\s+<([^>]+)>\s+(.+)\s+\.$', line)
    if not match:
        return None

    subject = match.group(1)
    predicate = match.group(2)
    obj_part = match.group(3).strip()

    # Parse object
    if obj_part.startswith('<') and obj_part.endswith('>'):
        # URI object
        obj_value = obj_part[1:-1]
        obj_type = 'uri'
    elif obj_part.startswith('"'):
        # Literal object - find closing quote
        quote_end = 1
        while quote_end < len(obj_part):
            if obj_part[quote_end] == '"' and obj_part[quote_end - 1] != '\\':
                break
            quote_end += 1

        obj_value = obj_part[1:quote_end]
        obj_type = 'literal'

        # Check for language tag or datatype
        remainder = obj_part[quote_end + 1:].strip()
        if remainder.startswith('@'):
            obj_type = remainder[1:]  # Language code
        elif remainder.startswith('^^'):
            obj_type = 'typed_literal'
    else:
        obj_value = obj_part
        obj_type = 'unknown'

    return (subject, predicate, obj_value, obj_type)


def load_coordinates_from_zip(zip_path):
    """
    Load all coordinates from TGNOut_Coordinates.nt into memory.

    Returns: dict mapping coordinate URI to (lat, lon) tuple
    """
    print("Loading coordinates...")
    coordinates = {}

    with zipfile.ZipFile(zip_path, 'r') as zf:
        with zf.open('TGNOut_Coordinates.nt', 'r') as f:
            for i, line in enumerate(f):
                if i % 500000 == 0 and i > 0:
                    print(f"  Loaded {len(coordinates):,} coordinates from {i:,} triples...")

                try:
                    line_str = line.decode('utf-8')
                    parsed = parse_ntriple(line_str)

                    if not parsed:
                        continue

                    subject, predicate, obj_value, obj_type = parsed

                    # Extract lat/lon
                    if predicate == 'http://www.w3.org/2003/01/geo/wgs84_pos#lat':
                        coord_uri = subject
                        if coord_uri not in coordinates:
                            coordinates[coord_uri] = [None, None]
                        coordinates[coord_uri][0] = float(obj_value)

                    elif predicate == 'http://www.w3.org/2003/01/geo/wgs84_pos#long':
                        coord_uri = subject
                        if coord_uri not in coordinates:
                            coordinates[coord_uri] = [None, None]
                        coordinates[coord_uri][1] = float(obj_value)

                except (UnicodeDecodeError, ValueError) as e:
                    continue

    # Clean up - remove incomplete coordinates
    complete_coords = {
        uri: (lat, lon)
        for uri, (lat, lon) in coordinates.items()
        if lat is not None and lon is not None
    }

    print(f"✓ Loaded {len(complete_coords):,} complete coordinate pairs")
    return complete_coords


def load_preferred_terms_from_zip(zip_path, limit_to_ids=None):
    """
    Load preferred terms (place names) from TGNOut_2Terms.nt.

    Args:
        zip_path: Path to TGN ZIP archive
        limit_to_ids: Optional set of TGN IDs to limit loading

    Returns: dict mapping TGN ID to preferred name
    """
    print("Loading preferred terms...")
    terms = {}

    with zipfile.ZipFile(zip_path, 'r') as zf:
        with zf.open('TGNOut_2Terms.nt', 'r') as f:
            for i, line in enumerate(f):
                if i % 1000000 == 0 and i > 0:
                    print(f"  Processed {i:,} term triples, found {len(terms):,} preferred terms...")

                try:
                    line_str = line.decode('utf-8')
                    parsed = parse_ntriple(line_str)

                    if not parsed:
                        continue

                    subject, predicate, obj_value, obj_type = parsed

                    # Look for preferred labels (prefLabel in English)
                    if (predicate == 'http://www.w3.org/2004/02/skos/core#prefLabel'
                            and obj_type == 'en'):

                        # Extract TGN ID from term URI
                        # Format: http://vocab.getty.edu/tgn/term/12345-en
                        if '/tgn/term/' in subject:
                            term_id = subject.split('/tgn/term/')[-1]
                            tgn_id = term_id.rsplit('-', 1)[0]  # Remove language suffix

                            if limit_to_ids is None or tgn_id in limit_to_ids:
                                if tgn_id not in terms:  # Keep first occurrence
                                    terms[tgn_id] = obj_value

                except (UnicodeDecodeError, ValueError, IndexError) as e:
                    continue

    print(f"✓ Loaded {len(terms):,} preferred terms")
    return terms


def stream_placemap_from_zip(zip_path, coordinates):
    """
    Stream TGNOut_PlaceMap.nt and yield (tgn_id, coord_uri) pairs.

    PlaceMap links TGN places to coordinate URIs.
    """
    print("Streaming place-coordinate mappings...")

    with zipfile.ZipFile(zip_path, 'r') as zf:
        with zf.open('TGNOut_PlaceMap.nt', 'r') as f:
            for i, line in enumerate(f):
                if i % 500000 == 0 and i > 0:
                    print(f"  Processed {i:,} place-map triples...")

                try:
                    line_str = line.decode('utf-8')
                    parsed = parse_ntriple(line_str)

                    if not parsed:
                        continue

                    subject, predicate, obj_value, obj_type = parsed

                    # Look for foaf:focus linking place to coordinates
                    # <http://vocab.getty.edu/tgn/7002445> <http://xmlns.com/foaf/0.1/focus> <http://vocab.getty.edu/tgn/7002445-geometry>
                    if predicate == 'http://xmlns.com/foaf/0.1/focus':
                        tgn_uri = subject
                        coord_uri = obj_value

                        # Check if we have coordinates for this
                        if coord_uri in coordinates:
                            # Extract TGN ID
                            if '/tgn/' in tgn_uri:
                                tgn_id = tgn_uri.split('/tgn/')[-1]
                                yield (tgn_id, coord_uri)

                except (UnicodeDecodeError, ValueError) as e:
                    continue


def create_place_doc(tgn_id, coord_uri, coordinates, terms):
    """
    Create a place document for a TGN place.
    """
    lat, lon = coordinates[coord_uri]

    place_id = f"tgn:{tgn_id}"

    # Get label
    label = terms.get(tgn_id, f"TGN {tgn_id}")

    doc = {
        'place_id': place_id,
        'label': label,
        'locations': [{
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat]
            },
            'rep_point': {
                'lon': lon,
                'lat': lat
            }
        }],
        'source': 'tgn',
        'types': [{
            'identifier': 'place',
            'label': 'tgn',
            'sourceLabel': 'getty-tgn'
        }]
    }

    return doc


def create_toponym_doc(place_id, label):
    """
    Create a toponym document.
    """
    return {
        'place_id': place_id,
        'name': label,
        'name_lower': label.lower(),
        'lang': 'en',
        'suggest': {
            'input': [label],
            'contexts': {
                'lang': ['en']
            }
        }
    }


def index_tgn(zip_path, places_index, toponyms_index):
    """
    Index TGN places from ZIP archive.

    Strategy:
    1. Load all coordinates into memory (~2GB of data → ~500MB in memory)
    2. Load preferred terms for places we'll index
    3. Stream PlaceMap and create place documents
    """
    # Step 1: Load coordinates
    coordinates = load_coordinates_from_zip(zip_path)

    if not coordinates:
        print("ERROR: No coordinates found!")
        return

    # Step 2: Stream PlaceMap to find which TGN IDs have coordinates
    print("\nFirst pass: Finding TGN IDs with coordinates...")
    tgn_ids_with_coords = set()
    place_to_coord = {}

    for tgn_id, coord_uri in stream_placemap_from_zip(zip_path, coordinates):
        tgn_ids_with_coords.add(tgn_id)
        place_to_coord[tgn_id] = coord_uri

    print(f"✓ Found {len(tgn_ids_with_coords):,} TGN places with coordinates")

    # Step 3: Load terms only for places with coordinates
    terms = load_preferred_terms_from_zip(zip_path, limit_to_ids=tgn_ids_with_coords)

    # Step 4: Create and index documents
    print("\nIndexing places...")

    place_batch = []
    toponym_batch = []
    place_count = 0
    toponym_count = 0

    for i, (tgn_id, coord_uri) in enumerate(place_to_coord.items()):
        if (i + 1) % 10000 == 0:
            print(f"  Indexed {place_count:,} places so far...")

        try:
            place_doc = create_place_doc(tgn_id, coord_uri, coordinates, terms)
            place_id = place_doc['place_id']

            # Add to batches
            place_batch.append({
                '_index': places_index,
                '_id': place_id,
                '_source': place_doc
            })

            toponym_doc = create_toponym_doc(place_id, place_doc['label'])
            toponym_batch.append({
                '_index': toponyms_index,
                '_id': place_id,
                '_source': toponym_doc
            })

            # Bulk index
            if len(place_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
                place_count += success
                place_batch = []

            if len(toponym_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
                toponym_count += success
                toponym_batch = []

        except Exception as e:
            print(f"Error processing TGN {tgn_id}: {str(e)}")
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


if __name__ == "__main__":
    # Note: The file is actually a ZIP, despite the .nt extension
    TGN_FILE = f"{DATA_DIR}tgn/TGNOut_PlaceMap/TGNOut_PlaceMap.nt"
    PLACES_INDEX = "places"
    TOPONYMS_INDEX = "toponyms"

    print(f"Starting to index TGN from {TGN_FILE}")
    print(f"Target indices: {PLACES_INDEX}, {TOPONYMS_INDEX}\n")

    index_tgn(TGN_FILE, PLACES_INDEX, TOPONYMS_INDEX)


def parse_ntriple(line):
    """
    Parse an N-Triple line into (subject, predicate, object) tuple.

    N-Triples format: <subject> <predicate> <object> .
    Object can be a URI <...>, literal "..."^^type, or literal "..."@lang

    Returns: (subject_uri, predicate_uri, object_value, object_type) or None
    """
    line = line.strip()
    if not line or line.startswith('#'):
        return None

    # Basic regex for N-Triples (simplified - doesn't handle all edge cases)
    # Pattern: <subject> <predicate> <object> .
    match = re.match(r'<([^>]+)>\s+<([^>]+)>\s+(.+)\s+\.$', line)
    if not match:
        return None

    subject = match.group(1)
    predicate = match.group(2)
    obj_part = match.group(3).strip()

    # Parse object
    if obj_part.startswith('<') and obj_part.endswith('>'):
        # URI object
        obj_value = obj_part[1:-1]
        obj_type = 'uri'
    elif obj_part.startswith('"'):
        # Literal object
        # Find the closing quote (handle escaped quotes)
        quote_end = 1
        while quote_end < len(obj_part):
            if obj_part[quote_end] == '"' and obj_part[quote_end - 1] != '\\':
                break
            quote_end += 1

        obj_value = obj_part[1:quote_end]
        obj_type = 'literal'

        # Check for language tag or datatype
        remainder = obj_part[quote_end + 1:].strip()
        if remainder.startswith('@'):
            obj_type = remainder[1:]  # Language code
        elif remainder.startswith('^^'):
            obj_type = 'typed_literal'
    else:
        obj_value = obj_part
        obj_type = 'unknown'

    return (subject, predicate, obj_value, obj_type)


if __name__ == "__main__":
    TGN_FILE = f"{DATA_DIR}tgn/TGNOut_PlaceMap/TGNOut_PlaceMap.nt"
    PLACES_INDEX = "places"
    TOPONYMS_INDEX = "toponyms"

    print(f"Starting to index TGN from {TGN_FILE}")
    print(f"Target indices: {PLACES_INDEX}, {TOPONYMS_INDEX}\n")

    # For testing, limit to first 10000 places
    # Remove max_places parameter for full indexing
    index_tgn(TGN_FILE, PLACES_INDEX, TOPONYMS_INDEX, max_places=None)