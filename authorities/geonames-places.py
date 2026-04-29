# authorities/geonames-places.py

"""
Stage GeoNames places data (~13 M points) to the staged extract
directory used by the rebuild pipeline.

Output: ``{STAGED_BASE_DIR}/gn/extract/places.jsonl``

ES indexing for this authority happens later via ``index_from_stage`` —
this script no longer talks to Elasticsearch.
"""
import sys

from processing.helpers import enrich_geometry, write_staged_place_doc
from processing.settings import DATA_DIR
from processing.utilities import stream_file

NAMESPACE = "gn"


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

    ccodes = []
    if fields[8]:
        ccodes.append(fields[8])
    if fields[9]:
        ccodes.extend([cc.strip() for cc in fields[9].split(",") if cc.strip()])

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

    population = None
    if fields[14] and fields[14] != '':
        try:
            population = int(fields[14])
        except ValueError:
            pass

    # GeoNames carries no temporal data, so toponyms and geometries are
    # emitted without timespans. The earlier 2025 placeholder collapsed
    # gazetteer_temporal_extent to ``[2025, 2025]`` for the whole 13M-doc
    # corpus, masking real historical coverage from other authorities.
    toponyms = []
    if fields[1]:
        lst = normalize_lst(fields[1], 'und')
        if lst:
            toponyms.append({"toponym_id": lst})

    point_geom = {
        'type': 'Point',
        'coordinates': [float(fields[5]), float(fields[4])],
    }
    geom_entry = enrich_geometry(point_geom)
    geometries = [geom_entry] if geom_entry else []

    doc = {
        "place_id": f"{NAMESPACE}:{fields[0]}",
        "title": fields[1],
        "toponyms": toponyms,
        "ccodes": ccodes,
        "geometries": geometries,
        "types": [
            {
                "identifier": fields[7] or fields[6],
                "label": fields[6],
                "sourceLabel": f"{fields[6]}.{fields[7]}" if fields[7] else fields[6],
            }
        ],
    }

    if elevation is not None:
        doc["elevation"] = elevation
    if population is not None:
        doc["population"] = population
    return doc


def stage_geonames(file_path):
    """Stream GeoNames file and write staged place docs."""
    count = 0
    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue
        try:
            doc = parse_geonames_line(line)
            write_staged_place_doc(NAMESPACE, doc)
            count += 1
            if count % 100_000 == 0:
                sys.stdout.write(f"\rStaged {count:,} places...")
                sys.stdout.flush()
        except Exception as e:
            print(f"\nError processing line: {e}")
            continue

    print(f"\nStaging complete. Total places staged: {count:,}")


if __name__ == "__main__":
    GEONAMES_FILE = f"{DATA_DIR}/authorities/gn/allCountries.zip"

    print("=" * 80)
    print("GEONAMES PLACES STAGING")
    print("=" * 80)
    print(f"Source: {GEONAMES_FILE}")
    print()

    stage_geonames(GEONAMES_FILE)

    print("\n" + "=" * 80)
    print("COMPLETE")
    print("=" * 80)
