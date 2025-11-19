# authorities/geonames-places.py

import gzip
from elasticsearch import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE, DATA_DIR

es = Elasticsearch(ES_HOST)


def stream_file(file_path):
    """
    Generator yielding lines from a potentially compressed file.
    """
    if file_path.endswith(".gz"):
        open_func = gzip.open
        mode = 'rt'
    else:
        open_func = open
        mode = 'r'

    with open_func(file_path, mode, encoding='utf-8') as f:
        for line in f:
            yield line.strip()


def parse_geonames_line(line):
    """
    Parse a single Geonames line into a dict suitable for the Place index.
    """
    fields = line.split("\t")
    return {
        "place_id": f"gn:{fields[0]}",
        "types": [fields[6]],  # feature class
        "geoms": {
            "location": {
                "type": "point",
                "coordinates": [float(fields[5]), float(fields[4])]
            }
        },
    }


def index_batches(file_path, index_name):
    """
    Read the file line by line and bulk index in batches.
    """
    batch = []
    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue
        doc = parse_geonames_line(line)
        batch.append({
            "_index": index_name,
            "_id": doc["place_id"],
            "_source": doc
        })
        if len(batch) >= BATCH_SIZE:
            helpers.bulk(es, batch)
            batch = []
    if batch:
        helpers.bulk(es, batch)


if __name__ == "__main__":
    GEONAMES_FILE = f"{DATA_DIR}geonames/allCountries/allCountries.txt"
    PLACES_INDEX = "places"
    index_batches(GEONAMES_FILE, PLACES_INDEX)
