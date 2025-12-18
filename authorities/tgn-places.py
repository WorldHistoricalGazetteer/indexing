# processing/tgn-places.py

"""
Index Getty Thesaurus of Geographic Names (TGN) data into Elasticsearch.

CORRECT VERSION - Based on verified RDF structure:
- Term URIs have skosxl:literalForm with the actual text
- Places link to term URIs via skosxl:prefLabel
- All in TGNOut_2Terms.nt file
- Ensures UTF-8 encoding for all strings

Updated to use temporal scoping design where temporal data lives with places.
TGN is current data, so all toponyms get 2025 temporal scope.
"""

import re
import zipfile
from collections import defaultdict

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)


def parse_ntriple(line):
    """Parse an N-Triple line."""
    line = line.strip()
    if not line or line.startswith('#'):
        return None

    match = re.match(r'<([^>]+)>\s+<([^>]+)>\s+(.+)\s+\.$', line)
    if not match:
        return None

    subject = match.group(1)
    predicate = match.group(2)
    obj_part = match.group(3).strip()

    # Parse object
    if obj_part.startswith('<') and obj_part.endswith('>'):
        obj_value = obj_part[1:-1]
        obj_type = 'uri'
    elif obj_part.startswith('"'):
        quote_end = 1
        while quote_end < len(obj_part):
            if obj_part[quote_end] == '"' and obj_part[quote_end - 1] != '\\':
                break
            quote_end += 1

        # Extract literal value and decode unicode escapes
        raw_value = obj_part[1:quote_end]
        # Decode unicode escape sequences (e.g., \u0411 -> Б)
        try:
            obj_value = raw_value.encode('utf-8').decode('unicode_escape')
        except:
            obj_value = raw_value

        obj_type = 'literal'

        remainder = obj_part[quote_end + 1:].strip()
        if remainder.startswith('@'):
            obj_type = remainder[1:]  # Language code
        elif remainder.startswith('^^'):
            obj_type = 'typed_literal'
    else:
        obj_value = obj_part
        obj_type = 'unknown'

    return (subject, predicate, obj_value, obj_type)


def load_coordinates_from_file(file_path):
    """
    Load all coordinates from TGNOut_Coordinates.nt into memory.
    Works with both ZIP archives and extracted .nt files.

    Returns: dict mapping coordinate URI to (lat, lon) tuple
    """
    print("Loading coordinates...")
    coordinates = {}

    # Determine if we're dealing with a ZIP or directory
    from pathlib import Path
    path = Path(file_path)

    if path.suffix == '.zip':
        # ZIP file - use original logic
        import zipfile
        with zipfile.ZipFile(file_path, 'r') as zf:
            with zf.open('TGNOut_Coordinates.nt', 'r') as f:
                lines = f
    else:
        # Extracted file - file_path should be to the directory or PlaceMap.nt
        if path.is_file():
            # Path is to PlaceMap.nt, get parent directory
            data_dir = path.parent
        else:
            # Path is to directory
            data_dir = path

        coords_file = data_dir / 'TGNOut_Coordinates.nt'
        if not coords_file.exists():
            print(f"ERROR: Coordinates file not found at {coords_file}")
            return {}

        lines = open(coords_file, 'rb')

    # Process lines
    for i, line in enumerate(lines):
        if i % 500000 == 0 and i > 0:
            print(f"  Loaded {len(coordinates):,} coordinates from {i:,} triples...")

        try:
            line_str = line.decode('utf-8')
            parsed = parse_ntriple(line_str)

            if not parsed:
                continue

            subject, predicate, obj_value, obj_type = parsed

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

    # Close file if we opened it
    if not path.suffix == '.zip':
        lines.close()

    # Clean up - remove incomplete coordinates
    complete_coords = {
        uri: (lat, lon)
        for uri, (lat, lon) in coordinates.items()
        if lat is not None and lon is not None
    }

    print(f"✓ Loaded {len(complete_coords):,} complete coordinate pairs")
    return complete_coords


def load_terms_and_labels_from_file(file_path):
    """
    Load term literals and place-to-term mappings from TGNOut_2Terms.nt.
    Works with both ZIP archives and extracted .nt files.

    Single pass extracts:
    1. term_uri -> literal text (from skosxl:literalForm)
    2. place_id -> term_uri (from skosxl:prefLabel where subject is a place)

    Returns: (term_literals, place_to_preferred_term, place_to_all_terms)
    """
    print("\nLoading terms and labels from TGNOut_2Terms.nt...")

    term_literals = {}  # term_uri -> (text, language)
    place_to_preferred_term = {}  # tgn_id -> term_uri (for prefLabelGVP)
    place_to_all_terms = defaultdict(list)  # tgn_id -> [term_uris]

    # Determine if we're dealing with a ZIP or directory
    from pathlib import Path
    path = Path(file_path)

    if path.suffix == '.zip':
        # ZIP file
        import zipfile
        with zipfile.ZipFile(file_path, 'r') as zf:
            with zf.open('TGNOut_2Terms.nt', 'r') as f:
                lines = f
    else:
        # Extracted file
        if path.is_file():
            data_dir = path.parent
        else:
            data_dir = path

        terms_file = data_dir / 'TGNOut_2Terms.nt'
        if not terms_file.exists():
            print(f"ERROR: Terms file not found at {terms_file}")
            return {}, {}, defaultdict(list)

        lines = open(terms_file, 'rb')

    # Process lines
    for i, line in enumerate(lines):
        if (i + 1) % 5000000 == 0:
            print(f"  Processed {i + 1:,} triples...")
            print(f"    Terms with literals: {len(term_literals):,}")
            print(f"    Places with preferred terms: {len(place_to_preferred_term):,}")

        try:
            line_str = line.decode('utf-8')
            parsed = parse_ntriple(line_str)

            if not parsed:
                continue

            subject, predicate, obj_value, obj_type = parsed

            # Extract term literals
            if predicate == 'http://www.w3.org/2008/05/skos-xl#literalForm':
                term_uri = subject
                if obj_type not in ['uri', 'typed_literal', 'literal', 'unknown']:
                    term_literals[term_uri] = (obj_value, obj_type)

            # Extract place -> preferred term mappings
            elif predicate == 'http://vocab.getty.edu/ontology#prefLabelGVP':
                if '/tgn/' in subject and obj_type == 'uri':
                    tgn_id = subject.split('/tgn/')[-1]
                    if tgn_id.replace('-', '').isdigit():
                        place_to_preferred_term[tgn_id] = obj_value

            # Track all prefLabels for additional names
            elif predicate == 'http://www.w3.org/2008/05/skos-xl#prefLabel':
                if '/tgn/' in subject and obj_type == 'uri':
                    tgn_id = subject.split('/tgn/')[-1]
                    if tgn_id.replace('-', '').isdigit():
                        place_to_all_terms[tgn_id].append(obj_value)

        except (UnicodeDecodeError, ValueError) as e:
            continue

    # Close file if we opened it
    if not path.suffix == '.zip':
        lines.close()

    print(f"✓ Loaded {len(term_literals):,} term literals")
    print(f"✓ Loaded {len(place_to_preferred_term):,} preferred term mappings")
    print(f"✓ Loaded {len(place_to_all_terms):,} places with all terms")

    return term_literals, place_to_preferred_term, place_to_all_terms


def stream_placemap_from_file(file_path, coordinates):
    """
    Stream TGNOut_PlaceMap.nt and yield (tgn_id, coord_uri) pairs.
    Works with both ZIP archives and extracted .nt files.
    """
    print("\nStreaming place-coordinate mappings...")

    # Determine if we're dealing with a ZIP or directory
    from pathlib import Path
    path = Path(file_path)

    if path.suffix == '.zip':
        # ZIP file
        import zipfile
        with zipfile.ZipFile(file_path, 'r') as zf:
            with zf.open('TGNOut_PlaceMap.nt', 'r') as f:
                lines = f
    else:
        # Extracted file
        if path.is_file() and path.name == 'TGNOut_PlaceMap.nt':
            # Path is directly to the PlaceMap file
            placemap_file = path
        elif path.is_file():
            # Path is to another file, use parent directory
            placemap_file = path.parent / 'TGNOut_PlaceMap.nt'
        else:
            # Path is to directory
            placemap_file = path / 'TGNOut_PlaceMap.nt'

        if not placemap_file.exists():
            print(f"ERROR: PlaceMap file not found at {placemap_file}")
            return

        lines = open(placemap_file, 'rb')

    # Process lines
    for i, line in enumerate(lines):
        if i % 500000 == 0 and i > 0:
            print(f"  Processed {i:,} place-map triples...")

        try:
            line_str = line.decode('utf-8')
            parsed = parse_ntriple(line_str)

            if not parsed:
                continue

            subject, predicate, obj_value, obj_type = parsed

            # <tgn:7011179> <foaf:focus> <tgn:7011179-place>
            if predicate == 'http://xmlns.com/foaf/0.1/focus':
                tgn_uri = subject
                coord_uri = obj_value

                # Check if we have coordinates for this
                if coord_uri in coordinates:
                    if '/tgn/' in tgn_uri:
                        tgn_id = tgn_uri.split('/tgn/')[-1]
                        yield (tgn_id, coord_uri)

        except (UnicodeDecodeError, ValueError) as e:
            continue

    # Close file if we opened it
    if not path.suffix == '.zip':
        lines.close()


def create_place_doc(tgn_id, coord_uri, coordinates, term_literals, place_to_preferred_term, place_to_all_terms):
    """
    Create a place document for a TGN place.

    Uses new temporal scoping design with TGN as current (2025) data.
    """
    lat, lon = coordinates[coord_uri]
    place_id = f"tgn:{tgn_id}"

    # Collect toponyms array with temporal scoping
    toponyms = []
    seen_lsts = set()

    # Get preferred label
    label = None
    if tgn_id in place_to_preferred_term:
        term_uri = place_to_preferred_term[tgn_id]
        if term_uri in term_literals:
            text, lang = term_literals[term_uri]
            label = text
            # Add preferred toponym with temporal scope
            lst = f"{text}@{lang}"
            if lst not in seen_lsts:
                toponyms.append({
                    'toponym_id': lst,
                    'timespan': {
                        'start': {'in': 2025},
                        'end': {'in': 2025}
                    }
                })
                seen_lsts.add(lst)

    # Add other terms as toponyms
    if tgn_id in place_to_all_terms:
        for term_uri in place_to_all_terms[tgn_id]:
            if term_uri in term_literals:
                text, lang = term_literals[term_uri]
                lst = f"{text}@{lang}"
                if lst not in seen_lsts:
                    toponyms.append({
                        'toponym_id': lst,
                        'timespan': {
                            'start': {'in': 2025},
                            'end': {'in': 2025}
                        }
                    })
                    seen_lsts.add(lst)

    # Fallback label
    if not label:
        label = f"TGN {tgn_id}"

    doc = {
        'place_id': place_id,
        'label': label,
        'toponyms': toponyms,
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


def index_tgn(zip_path, places_index):
    """
    Index TGN places from ZIP archive.

    Note: With new design, we only index places.
    Toponyms will be indexed separately by cross-authority deduplication.
    """
    print("=" * 80)
    print("TGN INDEXING (UTF-8 CORRECTED VERSION)")
    print("=" * 80)
    print("Using new temporal scoping design")

    # Step 1: Load coordinates
    coordinates = load_coordinates_from_file(zip_path)
    if not coordinates:
        print("ERROR: No coordinates found!")
        return

    # Step 2: Load term literals and place-term mappings
    term_literals, place_to_preferred_term, place_to_all_terms = load_terms_and_labels_from_file(zip_path)

    # Step 3: Find places with coordinates
    print("\nFinding places with coordinates...")
    place_to_coord = {}
    for tgn_id, coord_uri in stream_placemap_from_file(zip_path, coordinates):
        place_to_coord[tgn_id] = coord_uri

    print(f"✓ Found {len(place_to_coord):,} TGN places with coordinates")

    # Count how many have labels
    places_with_labels = sum(1 for tgn_id in place_to_coord if tgn_id in place_to_preferred_term)
    print(
        f"✓ {places_with_labels:,} of these have preferred labels ({places_with_labels * 100 // len(place_to_coord)}%)")

    # Step 4: Index
    print("\nIndexing places...")

    place_batch = []
    place_count = 0

    for i, (tgn_id, coord_uri) in enumerate(place_to_coord.items()):
        if (i + 1) % 10000 == 0:
            print(f"  Indexed {place_count:,} places...")

        try:
            place_doc = create_place_doc(tgn_id, coord_uri, coordinates, term_literals, place_to_preferred_term,
                                         place_to_all_terms)
            place_id = place_doc['place_id']

            place_batch.append({
                '_index': places_index,
                '_id': place_id,
                '_source': place_doc
            })

            if len(place_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
                place_count += success
                place_batch = []

        except Exception as e:
            print(f"Error processing TGN {tgn_id}: {str(e)}")
            continue

    # Index remaining
    if place_batch:
        success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
        place_count += success

    print(f"\n{'=' * 80}")
    print("INDEXING COMPLETE")
    print('=' * 80)
    print(f"Places indexed: {place_count:,}")


if __name__ == "__main__":
    # Updated to match settings.py configuration
    # File path updated to match fetch_authorities.py structure
    # Note: settings.py URL points to explicit.zip which contains TGNOut_PlaceMap.nt
    TGN_FILE = f"{DATA_DIR}/tgn/explicit.zip"
    # TGN_FILE = f"{DATA_DIR}/tgn/TGNOut_PlaceMap/TGNOut_PlaceMap.nt"
    PLACES_INDEX = "places"

    print(f"Starting to index TGN from {TGN_FILE}")
    print(f"Target index: {PLACES_INDEX}\n")

    index_tgn(TGN_FILE, PLACES_INDEX)

    create_checkpoint_snapshot(es, "tgn_places")