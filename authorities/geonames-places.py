# authorities/geonames_places.py

"""
Index GeoNames places data into Elasticsearch.

This script ONLY indexes places. Toponyms are indexed separately
by geonames_toponyms.py to ensure uniqueness.
"""
import sys

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import stream_file, create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)


def normalize_lst(name, lang='und'):
    """
    Ensure toponym is in LST format (name@lang).

    Args:
        name: The toponym name
        lang: Language code (default: 'und' for undetermined)

    Returns:
        Normalized LST string
    """
    if not name:
        return None
    if '@' in name:
        return name
    return f"{name}@{lang}"


def parse_geonames_line(line):
    """
    Parse a single Geonames line into a dict suitable for the Place index.

    Field positions from Geonames readme:
    0: geonameid
    1: name
    2: asciiname
    3: alternatenames (comma-separated)
    4: latitude
    5: longitude
    6: feature class
    7: feature code
    8: country code
    9: cc2 (alternate country codes, comma-separated)
    10: admin1 code
    11: admin2 code
    12: admin3 code
    13: admin4 code
    14: population
    15: elevation
    16: dem
    17: timezone
    18: modification date
    """
    fields = line.split("\t")

    # Build country codes array
    ccodes = []
    if fields[8]:  # primary country code
        ccodes.append(fields[8])
    if fields[9]:  # alternate country codes
        ccodes.extend([cc.strip() for cc in fields[9].split(",") if cc.strip()])

    # Handle elevation - prefer elevation field, fall back to DEM
    elevation = None
    if fields[15] and fields[15] != '':
        try:
            elevation = int(fields[15])
        except ValueError:
            pass
    if elevation is None and fields[16] and fields[16] != '':
        try:
            elevation = int(fields[16])
        except ValueError:
            pass

    # Build toponyms array - will be populated by geonames_toponyms.py
    # We could add the main name here, but alternateNames file provides better
    # language-tagged versions, so we start with empty array
    toponyms = []

    # However, index the main name as a toponym if we want a fallback
    # This ensures every place has at least one name, even if alternateNames
    # doesn't include it (rare but possible for very new entries)
    if fields[1]:  # Main name
        lst = normalize_lst(fields[1], 'und')
        if lst:
            toponyms.append(lst)

    # Optionally add ASCII name if different
    # (alternateNames usually has this, so you could skip it)
    # if fields[2] and fields[2] != fields[1]:
    #     lst = normalize_lst(fields[2], 'und')
    #     if lst:
    #         toponyms.append(lst)

    # Build the document
    doc = {
        "place_id": f"gn:{fields[0]}",
        "toponyms": toponyms,  # Array of LST references (list, not set)
        "ccodes": ccodes,
        "locations": [
            {
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(fields[5]), float(fields[4])]  # lon, lat
                },
                "rep_point": {
                    "lon": float(fields[5]),
                    "lat": float(fields[4])
                }
            }
        ],
        "types": [
            {
                "identifier": fields[7],  # feature code
                "label": fields[6],  # feature class
                "sourceLabel": f"{fields[6]}.{fields[7]}"
            }
        ]
    }

    # Add elevation if available
    if elevation is not None:
        doc["elevation"] = elevation

    # Set label to the primary name
    if fields[1]:
        doc["label"] = fields[1]

    return doc


def index_batches(file_path, index_name):
    """
    Read the file line by line and bulk index in batches.
    """
    batch = []
    count = 0

    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue

        try:
            doc = parse_geonames_line(line)
            batch.append({
                "_index": index_name,
                "_id": doc["place_id"],
                "_source": doc
            })

            if len(batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
                count += success
                sys.stdout.write(f"\rProcessed {count:,} places...")
                sys.stdout.flush()
                batch = []

        except Exception as e:
            print(f"\nError processing line: {str(e)}")
            continue

    # Index remaining batch
    if batch:
        success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
        count += success

    print(f"\nIndexing complete. Total places indexed: {count:,}")


if __name__ == "__main__":
    GEONAMES_FILE = f"{DATA_DIR}/authorities/gn/allCountries.zip"
    PLACES_INDEX = "places"

    print("=" * 80)
    print("GEONAMES PLACES INGESTION")
    print("=" * 80)
    print(f"Source: {GEONAMES_FILE}")
    print(f"Target index: {PLACES_INDEX}")
    print()

    index_batches(GEONAMES_FILE, PLACES_INDEX)

    print("\nCreating checkpoint snapshot...")
    create_checkpoint_snapshot(es, "geonames_places")

    print("\n" + "=" * 80)
    print("COMPLETE")
    print("=" * 80)
    print("Note: Toponyms will be indexed separately by geonames_toponyms.py")