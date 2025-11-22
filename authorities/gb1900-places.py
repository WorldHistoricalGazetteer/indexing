# authorities/gb1900-places-fixed.py

"""
Index GB1900 gazetteer data into Elasticsearch.

GB1900 is a CSV file with place names transcribed from 1:10,560 Ordnance Survey maps
of Great Britain (England, Scotland, Wales) from around 1900.
"""

import csv
import zipfile
import io
from elasticsearch8 import Elasticsearch, helpers

from processing.settings import ES_HOST, BATCH_SIZE, DATA_DIR

es = Elasticsearch(ES_HOST)


def parse_gb1900_row(row):
    """
    Parse a GB1900 CSV row into place and toponym documents.

    CSV columns:
    - pin_id: unique identifier
    - final_text: place name
    - nation: England, Scotland, Wales
    - local_authority: administrative area
    - parish: parish name
    - osgb_east: Ordnance Survey easting
    - osgb_north: Ordnance Survey northing
    - latitude: WGS84 latitude
    - longitude: WGS84 longitude
    - notes: additional notes

    Returns: (place_doc, toponym_doc) tuple
    """
    # Get pin_id with multiple possible field names
    pin_id = row.get('pin_id', row.get('Pin_id', row.get('PIN_ID', ''))).strip()

    if not pin_id:
        return None, None

    # Remove BOM if present
    if pin_id.startswith('\ufeff') or pin_id.startswith('ÿþ'):
        pin_id = pin_id.lstrip('\ufeffÿþ')

    # Get name with multiple possible field names
    name = row.get('final_text', row.get('Final_text', row.get('FINAL_TEXT', ''))).strip()

    if not name:
        return None, None

    # Get coordinates with multiple possible field names
    try:
        lat = float(row.get('latitude', row.get('Latitude', row.get('LATITUDE', ''))))
        lon = float(row.get('longitude', row.get('Longitude', row.get('LONGITUDE', ''))))
    except (ValueError, TypeError):
        return None, None

    place_id = f"gb1900:{pin_id}"

    # Build place document
    place_doc = {
        'place_id': place_id,
        'label': name,
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
        'source': 'gb1900'
    }

    # Add country code
    nation = row.get('nation', row.get('Nation', row.get('NATION', ''))).strip()
    if nation in ['England', 'Scotland', 'Wales']:
        place_doc['ccodes'] = ['GB']

    # Add place type (all are named places from maps)
    place_doc['types'] = [{
        'identifier': 'named-place',
        'label': 'gb1900',
        'sourceLabel': 'map-label'
    }]

    # Build toponym document
    toponym_doc = {
        'place_id': place_id,
        'name': name,
        'name_lower': name.lower(),
        'lang': 'en',  # GB1900 names are in English
        'suggest': {
            'input': [name],
            'contexts': {
                'lang': ['en']
            }
        }
    }

    # Add temporal information (maps are from ca. 1900)
    # Most names are from 1888-1914 period
    toponym_doc['timespans'] = [{
        'start': 1888,
        'end': 1914
    }]

    return place_doc, toponym_doc


def index_gb1900(file_path, places_index, toponyms_index):
    """
    Read GB1900 CSV from ZIP archive and index places and toponyms.
    """
    place_batch = []
    toponym_batch = []

    place_count = 0
    toponym_count = 0
    skipped = 0

    print(f"Opening GB1900 archive: {file_path}")

    with zipfile.ZipFile(file_path, 'r') as zf:
        # Find the CSV file
        csv_file = None
        for name in zf.namelist():
            if name.endswith('.csv') and not name.startswith('__MACOSX'):
                csv_file = name
                break

        if not csv_file:
            print("No CSV file found in archive")
            return

        print(f"Reading CSV: {csv_file}")

        # Read the entire file into memory first
        with zf.open(csv_file, 'r') as f:
            raw_bytes = f.read()

        # Try different encodings - GB1900 is UTF-16 with BOM
        csv_text = None
        for encoding in ['utf-16', 'utf-16-le', 'utf-16-be', 'utf-8', 'latin-1', 'iso-8859-1', 'cp1252', 'utf-8-sig']:
            try:
                csv_text = raw_bytes.decode(encoding)
                print(f"Successfully decoded with {encoding}")
                break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if not csv_text:
            print("ERROR: Could not decode CSV with any encoding")
            return

        # Parse CSV
        print("Processing records...")
        reader = csv.DictReader(io.StringIO(csv_text))

        # Print header for debugging
        print(f"CSV columns: {reader.fieldnames}")

        for i, row in enumerate(reader):
            if (i + 1) % 10000 == 0:
                print(f"Processed {i + 1:,} rows... (places: {place_count:,}, skipped: {skipped:,})")

            try:
                place_doc, toponym_doc = parse_gb1900_row(row)

                if not place_doc or not toponym_doc:
                    skipped += 1
                    if skipped <= 5:  # Show first few skipped for debugging
                        print(f"  Skipped row {i + 1}: {row}")
                    continue

                place_id = place_doc['place_id']

                # Add to batches
                place_batch.append({
                    '_index': places_index,
                    '_id': place_id,
                    '_source': place_doc
                })

                toponym_batch.append({
                    '_index': toponyms_index,
                    '_id': place_id,  # One toponym per place for GB1900
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
                print(f"Error processing row {i + 1}: {str(e)}")
                if skipped < 5:  # Show details for first few errors
                    print(f"  Row data: {row}")
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
    GB1900_FILE = f"{DATA_DIR}/gb1900/GB1900_gazetteer_abridged_july_2018/GB1900_gazetteer_abridged_july_2018.zip"
    PLACES_INDEX = "places"
    TOPONYMS_INDEX = "toponyms"

    print(f"Starting to index GB1900 from {GB1900_FILE}")
    print(f"Target indices: {PLACES_INDEX}, {TOPONYMS_INDEX}\n")

    index_gb1900(GB1900_FILE, PLACES_INDEX, TOPONYMS_INDEX)