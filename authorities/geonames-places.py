# authorities/geonames_places.py

"""
Index GeoNames places data into Elasticsearch.
"""
import sys

from elasticsearch import Elasticsearch, helpers
from processing.helpers import enrich_geometry, compute_h3_fields
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import stream_file, create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)


def normalize_lst(name, lang='und'):
    """Ensure toponym is in LST format (name@lang)."""
    if not name:
        return None
    if '@' in name:
        return name
    return f"{name}@{lang}"


def parse_geonames_line(line):
    """
    Parse a single Geonames line.

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
    if fields[8]:
        ccodes.append(fields[8])
    if fields[9]:
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

    # Handle population
    population = None
    if fields[14] and fields[14] != '':
        try:
            population = int(fields[14])
        except ValueError:
            pass

    # Build toponyms array with timespans (GeoNames is current data - 2025)
    toponyms = []
    if fields[1]:
        lst = normalize_lst(fields[1], 'und')
        if lst:
            toponyms.append({
                "toponym_id": lst,
                "timespans": [{
                    "start": {"in": 2025},
                    "end": {"in": 2025}
                }]
            })

    # Build geometries array (GeoNames has single point per place)
    point_geom = {
        'type': 'Point',
        'coordinates': [float(fields[5]), float(fields[4])]  # lon, lat
    }
    timespans = [{'start': {'in': 2025}, 'end': {'in': 2025}}]
    geom_entry = enrich_geometry(point_geom, timespans=timespans)
    geometries = [geom_entry] if geom_entry else []

    # H3 spatial index (top-level place fields)
    h3_centroid, h3_cover = (None, [])
    if geom_entry and geom_entry.get('repr_point'):
        rp = geom_entry['repr_point']
        h3_centroid, h3_cover = compute_h3_fields(rp['lon'], rp['lat'], point_geom)

    # Build document
    doc = {
        "place_id": f"gn:{fields[0]}",
        "title": fields[1],
        "toponyms": toponyms,
        "ccodes": ccodes,
        "geometries": geometries,
        "types": [
            {
                "identifier": fields[7] or fields[6],
                "label": fields[6],
                "sourceLabel": f"{fields[6]}.{fields[7]}" if fields[7] else fields[6]
            }
        ]
    }

    if elevation is not None:
        doc["elevation"] = elevation
    if population is not None:
        doc["population"] = population
    if h3_centroid:
        doc["h3_centroid"] = h3_centroid
        doc["h3_cover"] = h3_cover

    return doc


def index_batches(file_path, index_name):
    """Read file and bulk index in batches."""
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