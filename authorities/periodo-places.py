# authorities/periodo-places.py

"""
PeriodO Authority Script.

Fetches the PeriodO JSON-LD dataset, extracts periods with spatial coverage
polygons and temporal extents, and indexes them into the ``places`` index.

All PeriodO records are boundary-type: ``boundary: "period"``.

Namespace: ``po:``

Usage:
    python -m authorities.periodo-places
    python -m authorities.periodo-places --file /path/to/p0d.json
"""

import json
import sys
from pathlib import Path
from datetime import datetime

import requests
from elasticsearch import Elasticsearch, helpers
from processing.helpers import enrich_geometry
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

PERIODO_URL = "https://data.perio.do/d.json"
NAMESPACE = "po"


def fetch_periodo_data(file_path=None):
    """Fetch PeriodO JSON-LD dataset."""
    if file_path and Path(file_path).exists():
        print(f"Loading PeriodO data from: {file_path}")
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    # Try local cache first
    cache_path = Path(DATA_DIR) / 'authorities' / 'periodo' / 'p0d.json'
    if cache_path.exists():
        print(f"Loading cached PeriodO data from: {cache_path}")
        with open(cache_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    # Download
    print(f"Downloading PeriodO dataset from {PERIODO_URL} ...")
    resp = requests.get(PERIODO_URL, timeout=120)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        # Some endpoints serve zstd-compressed bytes with Content-Encoding: zstd.
        raw = resp.content
        if raw.startswith(b"\x28\xb5\x2f\xfd"):
            try:
                import zstandard as zstd
                data = json.loads(zstd.ZstdDecompressor().decompress(raw))
            except Exception as e:
                raise RuntimeError(
                    "Failed to decode zstd-compressed PeriodO response. "
                    "Install python package 'zstandard'."
                ) from e
        else:
            raise

    # Cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    print(f"  Cached to: {cache_path}")

    return data


def _parse_year(value):
    """Parse a year from PeriodO temporal extent."""
    if not value:
        return None
    try:
        # PeriodO uses ISO8601 year strings like "0500", "-0300", etc.
        return int(str(value).lstrip('+'))
    except (ValueError, TypeError):
        return None


def _extract_geometry(spatial_coverage):
    """Extract GeoJSON geometry from PeriodO spatial coverage."""
    if not spatial_coverage:
        return None

    # PeriodO spatial coverage can be a dict or list of dicts
    coverages = spatial_coverage if isinstance(spatial_coverage, list) else [spatial_coverage]

    for cov in coverages:
        if not isinstance(cov, dict):
            continue

        # Some records nest geometry under representative/feature-like nodes
        for nested_key in ('geometry', 'geojson', 'feature'):
            nested = cov.get(nested_key)
            if isinstance(nested, dict):
                if nested.get('type') == 'Feature' and isinstance(nested.get('geometry'), dict):
                    geom = nested['geometry']
                    if 'type' in geom and 'coordinates' in geom:
                        return geom
                if 'type' in nested and 'coordinates' in nested:
                    return nested

        # Check for inline geometry
        if 'geo:hasGeometry' in cov:
            geo_node = cov['geo:hasGeometry']
            nodes = geo_node if isinstance(geo_node, list) else [geo_node]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                wkt_node = node.get('geo:asWKT', '')
                if isinstance(wkt_node, dict):
                    wkt = wkt_node.get('@value', '')
                else:
                    wkt = wkt_node

                if wkt:
                    # Convert WKT to GeoJSON (basic support for common types)
                    try:
                        from shapely import wkt as shapely_wkt
                        from shapely.geometry import mapping
                        # Handle optional SRID prefixes, e.g. "SRID=4326;POLYGON(...)"
                        if ';' in wkt and wkt.upper().startswith('SRID='):
                            wkt = wkt.split(';', 1)[1]
                        geom = shapely_wkt.loads(wkt)
                        if geom and not geom.is_empty:
                            return mapping(geom)
                    except Exception:
                        pass

        # Check for GeoJSON geometry directly
        if 'type' in cov and 'coordinates' in cov:
            return cov

        # Check nested geometry
        if 'geometry' in cov:
            geom = cov['geometry']
            if isinstance(geom, dict) and 'type' in geom and 'coordinates' in geom:
                return geom

    return None


def _extract_label(period):
    """Extract a representative label from PeriodO period metadata."""
    label = period.get('label', '')
    if isinstance(label, dict):
        label = label.get('@value') or ''
    if isinstance(label, list):
        label = next((str(v) for v in label if v), '')
    if label:
        return str(label)

    localized = period.get('localizedLabels', {})
    if isinstance(localized, dict):
        for labels in localized.values():
            if isinstance(labels, list):
                for item in labels:
                    if isinstance(item, dict):
                        value = item.get('@value')
                        if value:
                            return str(value)
                    elif item:
                        return str(item)
            elif labels:
                return str(labels)
    return ''


def process_periodo_period(period_id, period, authority_id, authority_label):
    """Process a single PeriodO period into a place document."""
    label = _extract_label(period)
    if not label:
        return None

    # Extract spatial coverage
    spatial = period.get('spatialCoverage', [])
    geometry = _extract_geometry(spatial)

    # Extract temporal extent
    start_year = None
    end_year = None

    start_node = period.get('start', {})
    end_node = period.get('stop', {})

    if start_node:
        start_year = _parse_year(start_node.get('in', start_node.get('earliestYear')))
    if end_node:
        end_year = _parse_year(end_node.get('in', end_node.get('latestYear')))

    timespans = []
    if start_year is not None or end_year is not None:
        ts = {}
        if start_year is not None:
            ts['start'] = {'in': start_year}
        if end_year is not None:
            ts['end'] = {'in': end_year}
        timespans = [ts]

    # Build place_id from period URI
    # PeriodO IDs are like "p0trgkvfmd8" — use as-is
    clean_id = period_id.split('/')[-1] if '/' in period_id else period_id
    place_id = f"{NAMESPACE}:{clean_id}"

    # Build toponyms
    toponyms = [{'toponym_id': f"{label}@en"}]
    if timespans:
        toponyms[0]['timespans'] = timespans

    # Add localized labels
    localized = period.get('localizedLabels', {})
    seen = {f"{label}@en"}
    for lang, labels in localized.items():
        if not isinstance(labels, list):
            labels = [labels]
        for lbl in labels:
            if isinstance(lbl, dict):
                lbl = lbl.get('@value', '')
            if not lbl:
                continue
            lst = f"{lbl}@{lang}"
            if lst not in seen:
                entry = {'toponym_id': lst}
                if timespans:
                    entry['timespans'] = timespans
                toponyms.append(entry)
                seen.add(lst)

    doc = {
        'place_id': place_id,
        'namespace': NAMESPACE,
        'title': label,
        'toponyms': toponyms,
        'types': [{
            'identifier': 'period',
            'label': 'periodo',
            'sourceLabel': 'temporal-period',
        }],
        'boundary': 'period',
        'indexed_at': datetime.now().isoformat(),
    }

    if geometry:
        geom_entry = enrich_geometry(geometry, timespans=timespans or None)
        if geom_entry:
            doc['geometries'] = [geom_entry]

    # Add spatial coverage description
    spatial_labels = []
    for cov in (spatial if isinstance(spatial, list) else [spatial]):
        if isinstance(cov, dict):
            sl = cov.get('label', cov.get('id', ''))
            if sl:
                spatial_labels.append(sl)
    if spatial_labels:
        doc['descriptions'] = [{
            'value': f"Spatial coverage: {'; '.join(spatial_labels)}",
            'lang': 'en',
        }]

    # Authority info as relation
    if authority_id:
        doc['relations'] = [{
            'relation_type': 'partOf',
            'related_place_id': f"{NAMESPACE}:authority:{authority_id}",
            'label': authority_label or authority_id,
        }]

    return doc


def index_periodo(file_path=None, places_index='places'):
    """Index PeriodO periods into ES."""
    print("=" * 80)
    print("PeriodO TEMPORAL PERIODS INGESTION")
    print("=" * 80)

    es = Elasticsearch(ES_HOST, request_timeout=180)
    data = fetch_periodo_data(file_path)

    # PeriodO data structure: authorities → periods
    authorities = data.get('authorities', {})
    if not authorities:
        # Try alternate structure (d.json wraps in @graph)
        graph = data.get('@graph', [])
        if graph:
            # The dataset is a flat list; filter for period definitions
            authorities = {}
            for item in graph:
                if 'periods' in item:
                    auth_id = item.get('id', item.get('@id', ''))
                    authorities[auth_id] = item

    if not authorities:
        print("ERROR: No authorities found in PeriodO dataset")
        return

    print(f"Found {len(authorities)} authorities")

    batch = []
    total_indexed = 0
    total_skipped = 0
    with_geometry = 0
    without_geometry = 0

    for auth_id, auth_data in authorities.items():
        auth_label = auth_data.get('source', {}).get('title', '') if isinstance(auth_data.get('source'), dict) else ''
        periods = auth_data.get('periods', {})

        for period_id, period in periods.items():
            try:
                doc = process_periodo_period(
                    period_id, period, auth_id, auth_label
                )
                if not doc:
                    total_skipped += 1
                    continue

                batch.append({
                    '_index': places_index,
                    '_id': doc['place_id'],
                    '_source': doc,
                })

                if doc.get('geometries'):
                    with_geometry += 1
                else:
                    without_geometry += 1

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

    # Flush remaining
    if batch:
        success, failed = helpers.bulk(
            es, batch, raise_on_error=False, stats_only=True
        )
        total_indexed += success

    print(f"\n\nPeriodO ingestion complete:")
    print(f"  Indexed: {total_indexed:,}")
    print(f"  Skipped: {total_skipped:,}")
    print(f"  With geometry: {with_geometry:,}")
    print(f"  Without geometry: {without_geometry:,}")

    create_checkpoint_snapshot(es, 'periodo_places')


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Index PeriodO periods')
    parser.add_argument('--file', help='Path to PeriodO JSON-LD file')
    parser.add_argument('--places-index', default='places', help='Target index')
    args = parser.parse_args()

    index_periodo(file_path=args.file, places_index=args.places_index)

