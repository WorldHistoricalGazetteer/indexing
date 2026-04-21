# processing/pleiades-places.py

"""
Index Pleiades places data into Elasticsearch with memory-efficient streaming.
"""

import gzip
import ijson
from processing.helpers import enrich_geometry, compute_h3_fields, select_h3_cover_geometry

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)


def extract_geometries(pleiades_record):
    """
    Extract all geometries from Pleiades locations array with temporal attestations.
    Returns: list of geometry objects with optional timespans
    """
    place_id = f"pl:{pleiades_record['id']}"
    geometries = []

    locations = pleiades_record.get('locations', [])

    for idx, loc in enumerate(locations):
        geometry = loc.get('geometry')
        if not geometry:
            continue

        # Add temporal attestations if present
        # Pleiades has start/end directly on location
        start = loc.get('start')
        end = loc.get('end')
        timespans = None
        if start is not None or end is not None:
            timespan = {}
            if start is not None:
                timespan['start'] = {'in': start}
            if end is not None:
                timespan['end'] = {'in': end}
            timespans = [timespan]

        geom_entry = enrich_geometry(geometry, timespans=timespans,
                                     geom_key=f"{place_id}_{idx}")
        if geom_entry:
            geometries.append(geom_entry)

    # Fallback to reprPoint if no locations
    if not geometries and pleiades_record.get('reprPoint'):
        coords = pleiades_record['reprPoint']
        geom_entry = enrich_geometry({'type': 'Point', 'coordinates': coords},
                                     geom_key=f"{place_id}_0")
        if geom_entry:
            geometries.append(geom_entry)

    return geometries


def extract_toponyms(pleiades_record):
    """Extract name data with temporal scoping using timespans array."""
    toponyms = []
    seen_lsts = set()

    for name_obj in pleiades_record.get('names', []):
        romanized = name_obj.get('romanized', '')
        attested = name_obj.get('attested', '')

        name_str = attested if attested else romanized
        if not name_str:
            continue

        # Split comma-separated names
        names = [n.strip() for n in name_str.split(',') if n.strip()]

        for name in names:
            lang = name_obj.get('language', 'und') or 'und'
            toponym_id = f"{name}@{lang}"

            if toponym_id in seen_lsts:
                continue

            seen_lsts.add(toponym_id)

            toponym_entry = {'toponym_id': toponym_id}

            # Add temporal information as timespans array
            start = name_obj.get('start')
            end = name_obj.get('end')
            if start is not None or end is not None:
                timespan = {}
                if start is not None:
                    timespan['start'] = {'in': start}
                if end is not None:
                    timespan['end'] = {'in': end}
                toponym_entry['timespans'] = [timespan]

            toponyms.append(toponym_entry)

    return toponyms


def extract_place_types(pleiades_record):
    """Extract place types."""
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

    Uses new schema:
    - geometries: array of {geom, repr_point, timespans[]} for temporal geometries
    - toponyms with timespans array
    - relations with timespans array

    Returns: place_doc dict or None
    """
    place_id = f"pl:{pleiades_record['id']}"

    # Extract toponyms with timespans array
    toponyms = extract_toponyms(pleiades_record)

    # Skip if no name data
    if not toponyms:
        return None

    # Extract all geometries with temporal attestations
    geometries = extract_geometries(pleiades_record)

    # Build document (geometry is optional)
    doc = {
        'place_id': place_id,
        'title': pleiades_record.get('title', ''),
        'toponyms': toponyms,
        'geometries': geometries
    }

    # Extract place types
    types = extract_place_types(pleiades_record)
    if types:
        doc['types'] = types

    # Extract connections with schema field names
    connections = pleiades_record.get('connectsWith', [])
    if connections:
        relations = []
        for conn_uri in connections:
            if '/places/' in conn_uri:
                conn_id = conn_uri.split('/places/')[-1]
                relations.append({
                    'relation_type': 'connectedTo',
                    'related_place_id': f"pl:{conn_id}",
                    'label': 'Pleiades Connection'
                    # Note: Pleiades connections don't have temporal data
                })
        if relations:
            doc['relations'] = relations

    # H3 spatial index from primary geometry
    primary = geometries[0] if geometries else None
    if primary and primary.get('repr_point'):
        rp = primary['repr_point']
        locs = pleiades_record.get('locations', [])
        raw_geom = locs[0].get('geometry') if locs else None
        if not raw_geom and pleiades_record.get('reprPoint'):
            raw_geom = {'type': 'Point', 'coordinates': pleiades_record['reprPoint']}
        h3_geom = select_h3_cover_geometry(primary, raw_geom)
        h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], h3_geom)
        if h3c:
            doc['h3_centroid'] = h3c
            doc['h3_cover'] = h3cover

    return doc


def index_pleiades_streaming(file_path, places_index):
    """Stream parse Pleiades JSON to avoid loading entire file into memory."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    place_batch = []
    place_count = 0
    skipped = 0

    print(f"Streaming Pleiades data from {file_path}")
    print("Using ijson for memory-efficient parsing...")
    print("SCHEMA COMPLIANT VERSION")

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, "pl") as gsw:
        configure_module_writer(gsw)
        try:
            is_gzipped = file_path.endswith('.gz')

            if is_gzipped:
                try:
                    file_obj = gzip.open(file_path, 'rb')
                    test_bytes = file_obj.read(100)
                    file_obj.seek(0)
                except gzip.BadGzipFile:
                    print("Warning: File has .gz extension but is not gzipped")
                    file_obj = open(file_path, 'rb')
            else:
                file_obj = open(file_path, 'rb')

            with file_obj:
                first_bytes = file_obj.read(1000)
                first_text = first_bytes.decode('utf-8', errors='ignore')
                is_graph = '@graph' in first_text

                file_obj.seek(0)

                if is_graph:
                    parser = ijson.items(file_obj, '@graph.item')
                    print("Detected JSON-LD format with @graph")
                else:
                    parser = ijson.items(file_obj, 'item')
                    print("Detected JSON array format")

                for i, record in enumerate(parser):
                    if (i + 1) % 1000 == 0:
                        print(f"\rProcessed {i + 1:,} records... (places: {place_count:,})", end='', flush=True)

                    try:
                        place_doc = create_place_doc(record)

                        if not place_doc:
                            skipped += 1
                            continue

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
                        print(f"\nError processing record {record.get('id', 'unknown')}: {str(e)}")
                        skipped += 1
                        continue

        except ImportError:
            configure_module_writer(None)
            print("\nERROR: ijson library not installed")
            return index_pleiades_standard(file_path, places_index)
        finally:
            configure_module_writer(None)

    if place_batch:
        success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
        place_count += success

    print(f"\n\nIndexing complete!")
    print(f"Places indexed: {place_count:,}")
    print(f"Skipped (no name): {skipped:,}")
    print(f"Geometries in VAST store: {gsw.count:,}")


def index_pleiades_standard(file_path, places_index):
    """Standard method - loads entire file into memory. Fallback only."""
    import json

    place_batch = []
    place_count = 0
    skipped = 0

    print(f"Loading Pleiades data from {file_path}")
    print("WARNING: Loading entire file into memory...")

    with gzip.open(file_path, 'rt', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, dict) and '@graph' in data:
        records = data['@graph']
    elif isinstance(data, list):
        records = data
    else:
        print("Unexpected Pleiades structure")
        return

    print(f"Found {len(records)} Pleiades records")

    for i, record in enumerate(records):
        if (i + 1) % 1000 == 0:
            print(f"\rProcessed {i + 1:,} records... (places: {place_count:,})", end='', flush=True)

        try:
            place_doc = create_place_doc(record)

            if not place_doc:
                skipped += 1
                continue

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
            print(f"\nError processing record {record.get('id', 'unknown')}: {str(e)}")
            skipped += 1
            continue

    if place_batch:
        success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
        place_count += success

    print(f"\n\nIndexing complete!")
    print(f"Places indexed: {place_count:,}")
    print(f"Skipped (no name): {skipped:,}")


if __name__ == "__main__":
    PLEIADES_FILE = f"{DATA_DIR}/pleiades/pleiades-places-latest/pleiades-places-latest.json.gz"
    PLACES_INDEX = "places"

    print(f"Starting to index Pleiades from {PLEIADES_FILE}")
    print(f"Target index: {PLACES_INDEX}\n")

    index_pleiades_streaming(PLEIADES_FILE, PLACES_INDEX)
    create_checkpoint_snapshot(es, "pleiades_places")