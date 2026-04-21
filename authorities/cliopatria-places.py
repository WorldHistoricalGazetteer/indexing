# authorities/cliopatria-places.py

"""
Cliopatria Authority Script.

Fetches the Cliopatria GeoJSON dataset from the Seshat Global History Databank
GitHub repository, extracts historical polity boundaries, and indexes them
into the ``places`` index.

All Cliopatria records are boundary-type: ``boundary: "polity"``.

Namespace: ``clio:``

Usage:
    python -m authorities.cliopatria-places
    python -m authorities.cliopatria-places --file /path/to/cliopatria.geojson
"""

import json
import sys
import zipfile
from pathlib import Path
from datetime import datetime

import requests
from elasticsearch import Elasticsearch, helpers
from processing.helpers import enrich_geometry, compute_h3_fields
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

CLIOPATRIA_URL = (
    "https://github.com/Seshat-Global-History-Databank/cliopatria/"
    "raw/main/cliopatria.geojson.zip"
)
NAMESPACE = "clio"


def fetch_cliopatria_data(file_path=None):
    """Fetch and extract Cliopatria GeoJSON dataset."""
    if file_path and Path(file_path).exists():
        print(f"Loading Cliopatria data from: {file_path}")
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    # Try local cache
    cache_dir = Path(DATA_DIR) / 'authorities' / 'cliopatria'
    cache_dir.mkdir(parents=True, exist_ok=True)
    geojson_path = cache_dir / 'cliopatria.geojson'

    if geojson_path.exists():
        print(f"Loading cached Cliopatria data from: {geojson_path}")
        with open(geojson_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    # Download ZIP
    print(f"Downloading Cliopatria dataset from {CLIOPATRIA_URL} ...")
    zip_path = cache_dir / 'cliopatria.geojson.zip'

    resp = requests.get(CLIOPATRIA_URL, timeout=120, stream=True)
    resp.raise_for_status()

    with open(zip_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    print(f"  Downloaded: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")

    # Extract
    with zipfile.ZipFile(zip_path, 'r') as zf:
        geojson_names = [n for n in zf.namelist() if n.endswith('.geojson')]
        if not geojson_names:
            print("ERROR: No .geojson file in ZIP")
            sys.exit(1)

        target_name = geojson_names[0]
        zf.extract(target_name, cache_dir)
        extracted = cache_dir / target_name

        # Rename if needed
        if extracted != geojson_path:
            extracted.rename(geojson_path)

    print(f"  Extracted: {geojson_path}")

    with open(geojson_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _parse_year(value):
    """Parse a year value from Cliopatria properties."""
    if value is None:
        return None
    try:
        year = int(float(value))
        return year
    except (ValueError, TypeError):
        return None


def process_cliopatria_feature(feature):
    """Process a single Cliopatria GeoJSON feature into a place document."""
    props = feature.get('properties', {})
    geometry = feature.get('geometry')

    if not geometry:
        return None

    # Extract name/polity identifier
    name = (props.get('Name') or props.get('name') or
            props.get('PolID') or props.get('polity') or '')
    if not name:
        return None

    # Build place_id
    polity_id = props.get('PolID', props.get('polity', name))
    # Clean for use as ID
    clean_id = str(polity_id).lower().replace(' ', '_').replace('/', '_')
    place_id = f"{NAMESPACE}:{clean_id}"

    # Temporal extent
    start_year = _parse_year(props.get('from', props.get('start', props.get('Date'))))
    end_year = _parse_year(props.get('to', props.get('end')))

    timespans = []
    if start_year is not None or end_year is not None:
        ts = {}
        if start_year is not None:
            ts['start'] = {'in': start_year}
        if end_year is not None:
            ts['end'] = {'in': end_year}
        timespans = [ts]

    # If there's a date range in the ID, use it to make the place_id unique
    if start_year is not None:
        place_id = f"{NAMESPACE}:{clean_id}_{start_year}"
        if end_year is not None:
            place_id = f"{NAMESPACE}:{clean_id}_{start_year}_{end_year}"

    geom_entry = enrich_geometry(geometry, timespans=timespans or None,
                                 geom_key=f"{place_id}_0")
    if not geom_entry:
        return None

    # Build toponyms
    toponyms = [{'toponym_id': f"{name}@und"}]
    if timespans:
        toponyms[0]['timespans'] = timespans

    # Check for alternate names
    alt_name = props.get('OriginalName', props.get('original_name'))
    if alt_name and alt_name != name:
        entry = {'toponym_id': f"{alt_name}@und"}
        if timespans:
            entry['timespans'] = timespans
        toponyms.append(entry)

    doc = {
        'place_id': place_id,
        'namespace': NAMESPACE,
        'title': name,
        'toponyms': toponyms,
        'geometries': [geom_entry],
        'types': [{
            'identifier': 'polity',
            'label': 'cliopatria',
            'sourceLabel': 'historical-polity',
        }],
        'boundary': 'polity',
        'indexed_at': datetime.now().isoformat(),
    }
    if geom_entry.get('repr_point'):
        rp = geom_entry['repr_point']
        h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], geometry)
        if h3c:
            doc['h3_centroid'] = h3c
            doc['h3_cover'] = h3cover

    # Wikidata link if present
    wd_id = props.get('Wikipedia', props.get('wikidata'))
    if wd_id and wd_id.startswith('Q'):
        doc['relations'] = [{
            'relation_type': 'sameAs',
            'related_place_id': f"wd:{wd_id}",
            'label': 'Wikidata',
        }]

    # Description
    desc = props.get('Description', props.get('description'))
    if desc:
        doc['descriptions'] = [{'value': desc, 'lang': 'en'}]

    return doc


def index_cliopatria(file_path=None, places_index='places'):
    """Index Cliopatria polities into ES."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    print("=" * 80)
    print("CLIOPATRIA HISTORICAL POLITIES INGESTION")
    print("=" * 80)

    es = Elasticsearch(ES_HOST, request_timeout=180)
    data = fetch_cliopatria_data(file_path)

    features = data.get('features', [])
    if not features:
        print("ERROR: No features found in Cliopatria dataset")
        return

    print(f"Found {len(features)} features")

    batch = []
    total_indexed = 0
    total_skipped = 0
    seen_ids: set[str] = set()

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, "clio") as gsw:
        configure_module_writer(gsw)
        for i, feature in enumerate(features):
            try:
                doc = process_cliopatria_feature(feature)
                if not doc:
                    total_skipped += 1
                    continue

                # Handle duplicate IDs (same polity at different times)
                if doc['place_id'] in seen_ids:
                    counter = 1
                    base_id = doc['place_id']
                    while doc['place_id'] in seen_ids:
                        doc['place_id'] = f"{base_id}_v{counter}"
                        counter += 1
                seen_ids.add(doc['place_id'])

                batch.append({
                    '_index': places_index,
                    '_id': doc['place_id'],
                    '_source': doc,
                })

                if len(batch) >= BATCH_SIZE:
                    success, failed = helpers.bulk(
                        es, batch, raise_on_error=False, stats_only=True
                    )
                    total_indexed += success
                    batch = []

                    if total_indexed % 1000 == 0:
                        print(f"\r  Indexed: {total_indexed:,}", end='', flush=True)

            except Exception as e:
                total_skipped += 1
                continue

        configure_module_writer(None)

    # Flush remaining
    if batch:
        success, failed = helpers.bulk(
            es, batch, raise_on_error=False, stats_only=True
        )
        total_indexed += success

    print(f"\n\nCliopatria ingestion complete:")
    print(f"  Indexed: {total_indexed:,}")
    print(f"  Skipped: {total_skipped:,}")

    create_checkpoint_snapshot(es, 'cliopatria_places')


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Index Cliopatria polities')
    parser.add_argument('--file', help='Path to Cliopatria GeoJSON file')
    parser.add_argument('--places-index', default='places', help='Target index')
    args = parser.parse_args()

    index_cliopatria(file_path=args.file, places_index=args.places_index)


