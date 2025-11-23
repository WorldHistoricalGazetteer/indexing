# processing/geonames-places.py

"""
Index GeoNames places data into Elasticsearch.

Updated to use new file paths from settings.py
"""

from elasticsearch8 import Elasticsearch, helpers

from processing.settings import ES_HOST, BATCH_SIZE, DATA_DIR
from processing.utilities import stream_file

es = Elasticsearch(ES_HOST)


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

    # Build toponyms array - initially just the main name
    # Additional toponyms will be added from alternateNames file
    toponyms = []
    if fields[1]:
        # For the main name, we don't know the language, so we'll mark it as 'und' (undetermined)
        toponyms.append(f"{fields[1]}@und")

    # Also add the ASCII name if different
    if fields[2] and fields[2] != fields[1]:
        toponyms.append(f"{fields[2]}@und")

    # Build the document
    doc = {
        "place_id": f"gn:{fields[0]}",
        "toponyms": toponyms,
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
                # timespans will be added if/when we have historical location data
            }
        ],
        "types": [
            {
                "identifier": fields[7],  # feature code
                "label": fields[6],  # feature class
                "sourceLabel": f"{fields[6]}.{fields[7]}"
            }
        ],
        "source": "geonames"
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
                print(f"Indexed {count} places so far...")
                batch = []

        except Exception as e:
            print(f"Error processing line: {str(e)}")
            continue

    # Index remaining batch
    if batch:
        success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
        count += success

    print(f"Indexing complete. Total places indexed: {count}")


if __name__ == "__main__":
    GEONAMES_FILE = f"{DATA_DIR}authorities/gn/allCountries.zip"
    PLACES_INDEX = "places"

    print(f"Starting to index Geonames places from {GEONAMES_FILE}")
    print(f"Target index: {PLACES_INDEX}")

    index_batches(GEONAMES_FILE, PLACES_INDEX)