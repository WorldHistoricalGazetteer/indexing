# processing/gb1900-places.py

"""
Index GB1900 gazetteer data into Elasticsearch.
"""

import csv
import zipfile
import io

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)


def parse_gb1900_row(row):
    """Parse a GB1900 CSV row."""
    # Get pin_id
    pin_id = row.get('pin_id', row.get('Pin_id', row.get('PIN_ID', ''))).strip()

    if not pin_id:
        return None

    # Remove BOM if present
    if pin_id.startswith('\ufeff') or pin_id.startswith('ÿþ'):
        pin_id = pin_id.lstrip('\ufeffÿþ')

    # Get name
    name = row.get('final_text', row.get('Final_text', row.get('FINAL_TEXT', ''))).strip()

    if not name:
        return None

    # Get coordinates
    try:
        lat = float(row.get('latitude', row.get('Latitude', row.get('LATITUDE', ''))))
        lon = float(row.get('longitude', row.get('Longitude', row.get('LONGITUDE', ''))))
    except (ValueError, TypeError):
        return None

    place_id = f"gb:{pin_id}"

    # Build toponyms array with timespans
    # GB1900 maps are from ca. 1900 (1888-1914 period)
    lst = f"{name}@en"
    toponyms = [{
        'toponym_id': lst,
        'timespans': [{
            'start': {'in': 1888},
            'end': {'in': 1914}
        }]
    }]

    # Build geometries array with historical timespans
    geometries = [{
        'geom': {
            'type': 'Point',
            'coordinates': [lon, lat]
        },
        'repr_point': {
            'lon': lon,
            'lat': lat
        },
        'timespans': [{
            'start': {'in': 1888},
            'end': {'in': 1914}
        }]
    }]

    # Build place document
    place_doc = {
        'place_id': place_id,
        'title': name,
        'toponyms': toponyms,
        'geometries': geometries
    }

    # Add country code
    nation = row.get('nation', row.get('Nation', row.get('NATION', ''))).strip()
    if nation in ['England', 'Scotland', 'Wales']:
        place_doc['ccodes'] = ['GB']

    # Add place type
    place_doc['types'] = [{
        'identifier': 'named-place',
        'label': 'gb1900',
        'sourceLabel': 'map-label'
    }]

    return place_doc


def index_gb1900(file_path, places_index):
    """Read GB1900 CSV from ZIP and index places."""
    place_batch = []
    place_count = 0
    skipped = 0

    print(f"Opening GB1900 archive: {file_path}")

    with zipfile.ZipFile(file_path, 'r') as zf:
        # Find CSV file
        csv_file = None
        for name in zf.namelist():
            if name.endswith('.csv') and not name.startswith('__MACOSX'):
                csv_file = name
                break

        if not csv_file:
            print("No CSV file found in archive")
            return

        print(f"Reading CSV: {csv_file}")

        # Read file into memory
        with zf.open(csv_file, 'r') as f:
            raw_bytes = f.read()

        # Try different encodings
        csv_text = None
        for encoding in ['utf-16', 'utf-16-le', 'utf-16-be', 'utf-8', 'latin-1', 'iso-8859-1', 'cp1252', 'utf-8-sig']:
            try:
                csv_text = raw_bytes.decode(encoding)
                print(f"Successfully decoded with {encoding}")
                break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if not csv_text:
            print("ERROR: Could not decode CSV")
            return

        # Parse CSV
        print("Processing records...")
        reader = csv.DictReader(io.StringIO(csv_text))

        print(f"CSV columns: {reader.fieldnames}")

        for i, row in enumerate(reader):
            if (i + 1) % 10000 == 0:
                print(f"\rProcessed {i + 1:,} rows... (places: {place_count:,}, skipped: {skipped:,})")

            try:
                place_doc = parse_gb1900_row(row)

                if not place_doc:
                    skipped += 1
                    if skipped <= 5:
                        print(f"  Skipped row {i + 1}: {row}")
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
                print(f"Error processing row {i + 1}: {str(e)}")
                if skipped < 5:
                    print(f"  Row data: {row}")
                skipped += 1
                continue

    if place_batch:
        success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
        place_count += success

    print(f"\nIndexing complete!")
    print(f"Places indexed: {place_count:,}")
    print(f"Skipped: {skipped:,}")


if __name__ == "__main__":
    GB1900_FILE = f"{DATA_DIR}/gb1900/GB1900_gazetteer_abridged_july_2018/GB1900_gazetteer_abridged_july_2018.zip"
    PLACES_INDEX = "places"

    print("=" * 80)
    print("GB1900 PLACES INGESTION")
    print("=" * 80)
    print(f"Source: {GB1900_FILE}")
    print(f"Target index: {PLACES_INDEX}")
    print()

    index_gb1900(GB1900_FILE, PLACES_INDEX)
    create_checkpoint_snapshot(es, "gb1900_places")