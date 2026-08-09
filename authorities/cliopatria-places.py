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
from processing.helpers import (
    enrich_geometry,
    compute_h3_fields,
    select_h3_cover_geometry,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR
from processing.temporal import lifespan

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


def process_cliopatria_feature(feature, seen_ids=None):
    """Process a single Cliopatria GeoJSON feature into a place document.

    ``seen_ids`` is the mutable set of place_ids already emitted in this run.
    Pass it so duplicate polities are disambiguated **here**, before the
    geometry is keyed — see the ``_v{n}`` block below for why that ordering
    matters.
    """
    props = feature.get('properties', {})
    geometry = feature.get('geometry')


    # Extract name/polity identifier. Source uses ``Name`` + ``SeshatID``
    # in the current Seshat-Global-History-Databank/cliopatria GeoJSON;
    # the legacy ``PolID`` / ``polity`` keys are kept as fallbacks for older
    # snapshots.
    name = (props.get('Name') or props.get('name') or
            props.get('SeshatID') or props.get('PolID') or
            props.get('polity') or '')
    if not name:
        return None

    polity_id = (props.get('SeshatID') or props.get('PolID') or
                 props.get('polity') or name)
    clean_id = str(polity_id).lower().replace(' ', '_').replace('/', '_')
    place_id = f"{NAMESPACE}:{clean_id}"

    # Temporal extent — current Cliopatria source uses ``FromYear`` /
    # ``ToYear``; legacy snapshots used ``from`` / ``start`` / ``Date``
    # and ``to`` / ``end``.
    start_year = _parse_year(
        props.get('FromYear') or props.get('from')
        or props.get('start') or props.get('Date')
    )
    end_year = _parse_year(
        props.get('ToYear') or props.get('to') or props.get('end')
    )

    # Polity lifespans — `in` is correct (place#164 class C). `lifespan` also
    # closes a polity known only by its ToYear, which otherwise tests as
    # definitely alive at no year at all.
    timespans = lifespan(start_year, end_year)

    # If there's a date range in the ID, use it to make the place_id unique
    if start_year is not None:
        place_id = f"{NAMESPACE}:{clean_id}_{start_year}"
        if end_year is not None:
            place_id = f"{NAMESPACE}:{clean_id}_{start_year}_{end_year}"

    # Same polity at different times shares an id, so disambiguate with a
    # ``_v{n}`` suffix. This MUST happen before enrich_geometry below: that call
    # writes the polygon into the geom store under ``{place_id}_0``, while the
    # staged doc's ``geom_ref`` is derived from the FINAL place_id. Renaming
    # afterwards (as this did until place#176) left every duplicate's geometry
    # filed under the base id and its ref pointing at a key nothing ever wrote —
    # 2,986 dangling refs, whose places consequently never appeared in a tile.
    if seen_ids is not None and place_id in seen_ids:
        base_id, counter = place_id, 1
        while place_id in seen_ids:
            place_id = f"{base_id}_v{counter}"
            counter += 1
    if seen_ids is not None:
        seen_ids.add(place_id)

    geom_entry = enrich_geometry(geometry, timespans=timespans or None,
                                 geom_key=f"{place_id}_0") if geometry else None

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
        'geometries': [geom_entry] if geom_entry else [],
        'types': [{
            'identifier': 'polity',
            'label': 'cliopatria',
            'sourceLabel': 'historical-polity',
        }],
        'boundary': 'polity',
        'indexed_at': datetime.now().isoformat(),
    }
    if geom_entry and geom_entry.get('repr_point'):
        rp = geom_entry['repr_point']
        h3_geom = select_h3_cover_geometry(geom_entry, geometry)
        h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], h3_geom)
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


def stage_cliopatria(file_path=None):
    """Stage Cliopatria polities to {STAGED_BASE_DIR}/clio/extract/places.jsonl."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    print("=" * 80)
    print("CLIOPATRIA HISTORICAL POLITIES STAGING")
    print("=" * 80)

    data = fetch_cliopatria_data(file_path)

    features = data.get('features', [])
    if not features:
        print("ERROR: No features found in Cliopatria dataset")
        return

    print(f"Found {len(features)} features")

    total_staged = 0
    total_skipped = 0
    seen_ids: set[str] = set()

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, "clio") as gsw:
        configure_module_writer(gsw)
        for i, feature in enumerate(features):
            try:
                # Duplicate ids are disambiguated inside, before the geometry is
                # keyed — doing it out here renamed the doc after its polygon
                # had already been filed under the base id (place#176).
                doc = process_cliopatria_feature(feature, seen_ids=seen_ids)
                if not doc:
                    total_skipped += 1
                    continue

                write_staged_place_doc(namespace=NAMESPACE, doc=doc)
                total_staged += 1
                if total_staged % 1000 == 0:
                    print(f"\r  Staged: {total_staged:,}", end='', flush=True)

            except Exception:
                total_skipped += 1
                continue

        configure_module_writer(None)

    print(f"\n\nCliopatria staging complete:")
    print(f"  Staged: {total_staged:,}")
    print(f"  Skipped: {total_skipped:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Stage Cliopatria polities')
    parser.add_argument('--file', help='Path to Cliopatria GeoJSON file')
    args = parser.parse_args()

    stage_cliopatria(file_path=args.file)


