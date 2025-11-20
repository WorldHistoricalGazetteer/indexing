# authorities/tgn-places.py

"""
Index Getty Thesaurus of Geographic Names (TGN) data into Elasticsearch.

TGN is distributed as RDF N-Triples, often in a ZIP archive. We need to:
1. Extract N-Triples from ZIP if necessary
2. Group triples by subject (TGN ID)
3. Extract relevant predicates (coordinates, names, types, hierarchy)
4. Build place and toponym documents

Expected predicates in TGN:
- <http://www.w3.org/2000/01/rdf-schema#label> - preferred name
- <http://www.w3.org/2003/01/geo/wgs84_pos#lat> - latitude
- <http://www.w3.org/2003/01/geo/wgs84_pos#long> - longitude
- <http://vocab.getty.edu/ontology#prefLabelGVP> - preferred label
- <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> - type
- <http://vocab.getty.edu/ontology#placeTypePreferred> - place type
- <http://vocab.getty.edu/ontology#parentString> - hierarchical path
"""

import re
import zipfile
from collections import defaultdict
from elasticsearch8 import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE, DATA_DIR
from authorities.utilities import stream_file

es = Elasticsearch(ES_HOST)


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


def open_tgn_file(file_path):
    """
    Open TGN file, handling both ZIP archives and plain .nt files.

    Returns: file-like object (text mode, line iterator)
    """
    # Check if it's a ZIP file
    if zipfile.is_zipfile(file_path):
        print(f"Detected ZIP archive: {file_path}")
        zf = zipfile.ZipFile(file_path, 'r')

        # Find the .nt file inside
        nt_files = [name for name in zf.namelist() if name.endswith('.nt')]

        if not nt_files:
            raise ValueError("No .nt file found in ZIP archive")

        nt_file = nt_files[0]
        print(f"Found N-Triples file in archive: {nt_file}")

        # Try different encodings
        for encoding in ['utf-8', 'latin-1', 'iso-8859-1']:
            try:
                # Open file from ZIP and wrap in text decoder
                raw_file = zf.open(nt_file, 'r')
                # Test reading first line
                first_line = raw_file.readline().decode(encoding)
                raw_file.seek(0)

                print(f"Successfully opened with encoding: {encoding}")

                # Return generator that yields decoded lines
                def line_generator():
                    for line in raw_file:
                        yield line.decode(encoding)

                return line_generator(), zf  # Return both so we can close zf later

            except UnicodeDecodeError:
                raw_file.close()
                continue

        raise ValueError("Could not decode file with any encoding")

    else:
        # Plain .nt file
        print(f"Detected plain N-Triples file: {file_path}")

        # Try different encodings
        for encoding in ['utf-8', 'latin-1', 'iso-8859-1']:
            try:
                f = open(file_path, 'r', encoding=encoding)
                # Test reading first line
                first_line = f.readline()
                f.seek(0)

                print(f"Successfully opened with encoding: {encoding}")
                return f, None  # No ZipFile to close

            except UnicodeDecodeError:
                f.close()
                continue

        raise ValueError("Could not decode file with any encoding")


def group_triples_by_subject(file_path, max_places=None):
    """
    Read N-Triples file (from ZIP or plain file) and group triples by subject.

    This is memory-intensive for large files. Consider yielding subjects
    one at a time for production use.

    Args:
        file_path: Path to .nt file or .zip file
        max_places: Optional limit for testing

    Yields: (subject_uri, triples_dict) where triples_dict is
            {predicate: [objects]} for that subject
    """
    current_subject = None
    current_triples = defaultdict(list)
    count = 0

    file_handle, zip_handle = open_tgn_file(file_path)

    try:
        for line_num, line in enumerate(file_handle):
            if line_num % 100000 == 0 and line_num > 0:
                print(f"Processed {line_num:,} triples, {count:,} subjects so far...")

            parsed = parse_ntriple(line)
            if not parsed:
                continue

            subject, predicate, obj_value, obj_type = parsed

            # Yield previous subject if we've moved to a new one
            if current_subject is not None and subject != current_subject:
                yield (current_subject, dict(current_triples))
                count += 1

                if max_places and count >= max_places:
                    return

                current_triples = defaultdict(list)

            current_subject = subject
            current_triples[predicate].append((obj_value, obj_type))

        # Yield final subject
        if current_subject:
            yield (current_subject, dict(current_triples))

    finally:
        # Clean up file handles
        if zip_handle:
            zip_handle.close()
        elif hasattr(file_handle, 'close'):
            file_handle.close()


def extract_tgn_coordinates(triples):
    """
    Extract lat/lon from TGN triples.

    Common predicates:
    - http://www.w3.org/2003/01/geo/wgs84_pos#lat
    - http://www.w3.org/2003/01/geo/wgs84_pos#long
    - http://schema.org/latitude
    - http://schema.org/longitude
    """
    lat = None
    lon = None

    # Check various latitude predicates
    for pred in ['http://www.w3.org/2003/01/geo/wgs84_pos#lat',
                 'http://schema.org/latitude']:
        if pred in triples:
            try:
                lat = float(triples[pred][0][0])
                break
            except (ValueError, IndexError):
                pass

    # Check various longitude predicates
    for pred in ['http://www.w3.org/2003/01/geo/wgs84_pos#long',
                 'http://schema.org/longitude']:
        if pred in triples:
            try:
                lon = float(triples[pred][0][0])
                break
            except (ValueError, IndexError):
                pass

    return (lon, lat) if lat is not None and lon is not None else None


def extract_tgn_label(triples):
    """
    Extract preferred label from TGN triples.
    """
    # Try preferred label first
    for pred in ['http://vocab.getty.edu/ontology#prefLabelGVP',
                 'http://www.w3.org/2004/02/skos/core#prefLabel',
                 'http://www.w3.org/2000/01/rdf-schema#label']:
        if pred in triples:
            return triples[pred][0][0]  # First value

    return None


def extract_tgn_place_type(triples):
    """
    Extract place type from TGN triples.
    """
    type_pred = 'http://vocab.getty.edu/ontology#placeTypePreferred'
    if type_pred in triples:
        type_uri = triples[type_pred][0][0]
        # Extract last part of URI as type label
        type_label = type_uri.split('/')[-1] if '/' in type_uri else type_uri
        return type_label

    return None


def create_tgn_place_doc(tgn_uri, triples):
    """
    Create a place document from TGN triples.
    """
    # Extract TGN ID from URI
    # URI format: http://vocab.getty.edu/tgn/7002445
    tgn_id = tgn_uri.split('/')[-1]
    place_id = f"tgn:{tgn_id}"

    # Get label
    label = extract_tgn_label(triples)
    if not label:
        return None

    # Get coordinates
    coords = extract_tgn_coordinates(triples)
    if not coords:
        return None  # Skip places without coordinates

    doc = {
        'place_id': place_id,
        'label': label,
        'locations': [{
            'geometry': {
                'type': 'Point',
                'coordinates': list(coords)
            },
            'rep_point': {
                'lon': coords[0],
                'lat': coords[1]
            }
        }],
        'source': 'tgn'
    }

    # Add place type
    place_type = extract_tgn_place_type(triples)
    if place_type:
        doc['types'] = [{
            'identifier': place_type,
            'label': 'tgn',
            'sourceLabel': place_type
        }]

    return doc


def create_tgn_toponym_doc(place_id, label):
    """
    Create a toponym document from TGN place.
    """
    return {
        'place_id': place_id,
        'name': label,
        'name_lower': label.lower(),
        'suggest': {
            'input': [label]
        }
    }


def index_tgn(file_path, places_index, toponyms_index, max_places=None):
    """
    Read TGN N-Triples file and index places and toponyms.

    Args:
        file_path: Path to TGN .nt file
        places_index: Target places index
        toponyms_index: Target toponyms index
        max_places: Optional limit for testing (e.g., 10000)
    """
    place_batch = []
    toponym_batch = []

    place_count = 0
    toponym_count = 0
    skipped = 0

    print(f"Processing TGN from {file_path}")
    if max_places:
        print(f"Limiting to first {max_places:,} places for testing")

    for tgn_uri, triples in group_triples_by_subject(file_path, max_places):
        try:
            place_doc = create_tgn_place_doc(tgn_uri, triples)

            if not place_doc:
                skipped += 1
                continue

            place_id = place_doc['place_id']

            # Add to place batch
            place_batch.append({
                '_index': places_index,
                '_id': place_id,
                '_source': place_doc
            })

            # Create toponym
            toponym_doc = create_tgn_toponym_doc(place_id, place_doc['label'])
            toponym_batch.append({
                '_index': toponyms_index,
                '_id': place_id,
                '_source': toponym_doc
            })

            # Bulk index
            if len(place_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
                place_count += success
                print(f"Indexed {place_count:,} places, skipped {skipped:,}")
                place_batch = []

            if len(toponym_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
                toponym_count += success
                toponym_batch = []

        except Exception as e:
            print(f"Error processing {tgn_uri}: {str(e)}")
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
    print(f"Skipped: {skipped:,}")


if __name__ == "__main__":
    TGN_FILE = f"{DATA_DIR}tgn/TGNOut_PlaceMap/TGNOut_PlaceMap.nt"
    PLACES_INDEX = "places"
    TOPONYMS_INDEX = "toponyms"

    print(f"Starting to index TGN from {TGN_FILE}")
    print(f"Target indices: {PLACES_INDEX}, {TOPONYMS_INDEX}\n")

    # For testing, limit to first 10000 places
    # Remove max_places parameter for full indexing
    index_tgn(TGN_FILE, PLACES_INDEX, TOPONYMS_INDEX, max_places=None)