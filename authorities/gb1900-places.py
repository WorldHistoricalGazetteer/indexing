# authorities/gb1900-places.py

"""
Stage GB1900 gazetteer (~1.2 M map labels, 1888–1914) to the staged
extract directory used by the rebuild pipeline.

Output: ``{STAGED_BASE_DIR}/gb/extract/places.jsonl``

ES indexing for this authority happens later via ``index_from_stage`` —
this script no longer talks to Elasticsearch.
"""

import csv
import zipfile
import io

from processing.helpers import (
    enrich_geometry,
    compute_h3_fields,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR

NAMESPACE = "gb"


def parse_gb1900_row(row):
    """Parse a GB1900 CSV row into a place doc, or None if invalid."""
    pin_id = row.get('pin_id', row.get('Pin_id', row.get('PIN_ID', ''))).strip()

    if not pin_id:
        return None

    if pin_id.startswith('﻿') or pin_id.startswith('ÿþ'):
        pin_id = pin_id.lstrip('﻿ÿþ')

    name = row.get('final_text', row.get('Final_text', row.get('FINAL_TEXT', ''))).strip()

    if not name:
        return None

    try:
        lat = float(row.get('latitude', row.get('Latitude', row.get('LATITUDE', ''))))
        lon = float(row.get('longitude', row.get('Longitude', row.get('LONGITUDE', ''))))
    except (ValueError, TypeError):
        return None

    place_id = f"gb:{pin_id}"

    # GB1900 maps are from ca. 1900 (1888–1914 survey period)
    timespans = [{'start': {'in': 1888}, 'end': {'in': 1914}}]
    toponyms = [{
        'toponym_id': f"{name}@en",
        'timespans': timespans,
    }]

    point_geom = {'type': 'Point', 'coordinates': [lon, lat]}
    geom_entry = enrich_geometry(point_geom, timespans=timespans)
    geometries = [geom_entry] if geom_entry else []

    h3_centroid, h3_cover = (None, [])
    if geom_entry and geom_entry.get('repr_point'):
        rp = geom_entry['repr_point']
        h3_centroid, h3_cover = compute_h3_fields(rp['lon'], rp['lat'], point_geom)

    place_doc = {
        'place_id': place_id,
        'title': name,
        'toponyms': toponyms,
        'geometries': geometries,
    }
    if h3_centroid:
        place_doc['h3_centroid'] = h3_centroid
        place_doc['h3_cover'] = h3_cover

    nation = row.get('nation', row.get('Nation', row.get('NATION', ''))).strip()
    if nation in ('England', 'Scotland', 'Wales'):
        place_doc['ccodes'] = ['GB']

    place_doc['types'] = [{
        'identifier': 'named-place',
        'label': 'gb1900',
        'sourceLabel': 'map-label',
    }]

    return place_doc


def stage_gb1900(file_path):
    """Read GB1900 CSV from ZIP and write staged place docs."""
    place_count = 0
    skipped = 0

    print(f"Opening GB1900 archive: {file_path}")

    with zipfile.ZipFile(file_path, 'r') as zf:
        csv_file = None
        for name in zf.namelist():
            if name.endswith('.csv') and not name.startswith('__MACOSX'):
                csv_file = name
                break

        if not csv_file:
            print("No CSV file found in archive")
            return

        print(f"Reading CSV: {csv_file}")

        with zf.open(csv_file, 'r') as f:
            raw_bytes = f.read()

        csv_text = None
        for encoding in ('utf-16', 'utf-16-le', 'utf-16-be', 'utf-8',
                         'latin-1', 'iso-8859-1', 'cp1252', 'utf-8-sig'):
            try:
                csv_text = raw_bytes.decode(encoding)
                print(f"Successfully decoded with {encoding}")
                break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if not csv_text:
            print("ERROR: Could not decode CSV")
            return

        print("Processing records...")
        reader = csv.DictReader(io.StringIO(csv_text))
        print(f"CSV columns: {reader.fieldnames}")

        for i, row in enumerate(reader):
            if (i + 1) % 10000 == 0:
                print(
                    f"\rProcessed {i + 1:,} rows... "
                    f"(places: {place_count:,}, skipped: {skipped:,})",
                    end='', flush=True,
                )

            try:
                place_doc = parse_gb1900_row(row)
                if not place_doc:
                    skipped += 1
                    if skipped <= 5:
                        print(f"  Skipped row {i + 1}: {row}")
                    continue

                write_staged_place_doc(namespace=NAMESPACE, doc=place_doc)
                place_count += 1

            except Exception as e:
                print(f"Error processing row {i + 1}: {e}")
                if skipped < 5:
                    print(f"  Row data: {row}")
                skipped += 1
                continue

    print(f"\nStaging complete!")
    print(f"Places staged: {place_count:,}")
    print(f"Skipped: {skipped:,}")


if __name__ == "__main__":
    GB1900_FILE = (
        f"{DATA_DIR}/gb1900/"
        "GB1900_gazetteer_abridged_july_2018/"
        "GB1900_gazetteer_abridged_july_2018.zip"
    )

    print("=" * 80)
    print("GB1900 PLACES STAGING")
    print("=" * 80)
    print(f"Source: {GB1900_FILE}")
    print()

    stage_gb1900(GB1900_FILE)
