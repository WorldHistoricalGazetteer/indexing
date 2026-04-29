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
import time
from pathlib import Path
from datetime import datetime

import requests
from shapely.geometry import shape, mapping, MultiPolygon
from shapely.ops import unary_union
from processing.helpers import (
    enrich_geometry,
    compute_h3_fields,
    select_h3_cover_geometry,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR

PERIODO_URL = "https://data.perio.do/d.json"
NAMESPACE = "po"
GITHUB_API_BASE = "https://api.github.com/repos/periodo/periodo-places"
PROGRESS_LOG_INTERVAL_SECONDS = 15


def _log_progress(prefix, processed, *, total=None, start_ts=None, extra=""):
    """Emit a periodic progress line suitable for persisted Slurm logs."""
    elapsed = max(time.time() - start_ts, 0.0) if start_ts else 0.0
    rate = (processed / elapsed) if elapsed > 0 else 0.0
    total_part = f"/{total:,}" if total is not None else ""
    elapsed_part = f" elapsed={elapsed/60:.1f}m rate={rate:.1f}/s"
    extra_part = f" {extra}" if extra else ""
    print(f"  {prefix}: {processed:,}{total_part}{elapsed_part}{extra_part}")


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


def _parse_year(value, *, prefer='earliest'):
    """Parse a year from a PeriodO temporal-extent ``in`` value.

    PeriodO nests the year under ``start.in`` / ``stop.in`` as a dict:

    * ``{'year': '-0585'}`` — single point.
    * ``{'earliestYear': X, 'latestYear': Y}`` — uncertain range; ``prefer``
      selects which bound to return (``'earliest'`` for start nodes,
      ``'latest'`` for stop nodes), so the recorded timespan still covers
      the period's actual extent.

    Bare strings/ints are also accepted defensively. Returns None if no
    usable year can be extracted.
    """
    if value is None or value == '':
        return None
    if isinstance(value, dict):
        if 'year' in value:
            return _parse_year(value.get('year'))
        key = 'earliestYear' if prefer == 'earliest' else 'latestYear'
        return _parse_year(value.get(key))
    try:
        return int(str(value).lstrip('+'))
    except (ValueError, TypeError):
        return None


def _collect_polygons(geom, out_polygons):
    """Recursively collect valid polygon parts from any shapely geometry."""
    gtype = geom.geom_type
    if gtype == 'Polygon':
        candidate = geom
        if not candidate.is_valid:
            candidate = candidate.buffer(0)
        if candidate and not candidate.is_empty and candidate.is_valid:
            out_polygons.append(candidate)
        return

    if gtype in ('MultiPolygon', 'GeometryCollection'):
        for sub in geom.geoms:
            _collect_polygons(sub, out_polygons)


def _extract_and_validate_multipolygon(geometry_obj):
    """Convert arbitrary geometry to a clean MultiPolygon GeoJSON dict."""
    polygons = []
    _collect_polygons(geometry_obj, polygons)
    if not polygons:
        return None

    try:
        merged = unary_union(polygons)
    except Exception:
        return None

    # Union can still produce mixed collections; normalize again.
    normalized = []
    _collect_polygons(merged, normalized)
    if not normalized:
        return None

    try:
        final_geom = unary_union(normalized)
    except Exception:
        return None

    if final_geom.geom_type == 'Polygon':
        final_geom = MultiPolygon([final_geom])
    elif final_geom.geom_type != 'MultiPolygon':
        return None

    if final_geom.is_empty or not final_geom.is_valid:
        return None

    return mapping(final_geom)


def _extract_spatial_uris(spatial_coverage):
    """Extract spatial coverage URIs from PeriodO period spatialCoverage values."""
    if not spatial_coverage:
        return []

    coverages = spatial_coverage if isinstance(spatial_coverage, list) else [spatial_coverage]
    uris = []
    for cov in coverages:
        if isinstance(cov, dict):
            uri = cov.get('id') or cov.get('@id')
            if uri:
                uris.append(uri)
        elif isinstance(cov, str):
            uris.append(cov)
    return uris


def _merge_geojson_geometries(geometries):
    """Merge multiple GeoJSON geometries into one clean MultiPolygon geometry."""
    if not geometries:
        return None

    shapely_geoms = []
    for geom in geometries:
        try:
            shapely_geoms.append(shape(geom))
        except Exception:
            continue

    if not shapely_geoms:
        return None

    try:
        merged = unary_union(shapely_geoms)
    except Exception:
        return None

    return _extract_and_validate_multipolygon(merged)


def load_periodo_gazetteer_geometries():
    """Build a URI->geometry index from periodo-places gazetteer feature files."""
    print("Loading PeriodO gazetteer polygons from periodo-places...")

    try:
        resp = requests.get(f"{GITHUB_API_BASE}/contents/gazetteers", timeout=60)
        resp.raise_for_status()
        files = [
            item for item in resp.json()
            if item.get('type') == 'file' and str(item.get('name', '')).endswith('.json')
        ]
    except Exception as e:
        print(f"  Warning: failed to list gazetteers: {e}")
        return {}

    cache_dir = Path(DATA_DIR) / 'authorities' / 'periodo' / 'gazetteers'
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        cache_dir = Path(__file__).resolve().parent / 'periodo_gazetteers_cache'
        cache_dir.mkdir(parents=True, exist_ok=True)

    geometry_index = {}
    total_features = 0
    started = time.time()
    last_log = started
    total_files = len(files)

    for file_idx, file_info in enumerate(files, start=1):
        name = file_info.get('name')
        if not name:
            continue
        cache_file = cache_dir / name

        if cache_file.exists():
            try:
                gazetteer = json.loads(cache_file.read_text(encoding='utf-8'))
            except Exception:
                gazetteer = None
        else:
            gazetteer = None

        if gazetteer is None:
            try:
                fresp = requests.get(file_info.get('download_url', ''), timeout=120)
                fresp.raise_for_status()
                gazetteer = fresp.json()
                cache_file.write_text(json.dumps(gazetteer), encoding='utf-8')
            except Exception as e:
                print(f"  Warning: failed to fetch {name}: {e}")
                continue

        for feature in gazetteer.get('features', []):
            total_features += 1
            feature_id = feature.get('id')
            geometry_data = feature.get('geometry')
            if not feature_id or not geometry_data:
                continue

            try:
                clean_geom = _extract_and_validate_multipolygon(shape(geometry_data))
            except Exception:
                clean_geom = None

            if clean_geom:
                geometry_index[feature_id] = clean_geom

            now = time.time()
            if now - last_log >= PROGRESS_LOG_INTERVAL_SECONDS:
                _log_progress(
                    "Gazetteer features",
                    total_features,
                    start_ts=started,
                    extra=(
                        f"files={file_idx}/{total_files} "
                        f"usable={len(geometry_index):,}"
                    ),
                )
                last_log = now

    print(
        f"  Gazetteers: {len(files)} files, {total_features:,} features, "
        f"{len(geometry_index):,} usable polygon geometries"
    )
    return geometry_index


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


def process_periodo_period(period_id, period, authority_id, authority_label, spatial_geometry_index):
    """Process a single PeriodO period into a place document."""
    label = _extract_label(period)
    if not label:
        return None

    # Extract spatial coverage
    spatial = period.get('spatialCoverage', [])
    geometry = _extract_geometry(spatial)
    if not geometry:
        spatial_uris = _extract_spatial_uris(spatial)
        spatial_geoms = [spatial_geometry_index[uri] for uri in spatial_uris if uri in spatial_geometry_index]
        if len(spatial_geoms) >= 25:
            print(f"  Merging {len(spatial_geoms)} gazetteer geometries for period {period_id} ...")
        geometry = _merge_geojson_geometries(spatial_geoms)

    # Extract temporal extent
    start_year = None
    end_year = None

    start_node = period.get('start', {})
    end_node = period.get('stop', {})

    if start_node:
        start_year = _parse_year(start_node.get('in'), prefer='earliest')
    if end_node:
        end_year = _parse_year(end_node.get('in'), prefer='latest')

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
        geom_entry = enrich_geometry(geometry, timespans=timespans or None,
                                     geom_key=f"{place_id}_0")
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


def stage_periodo(file_path=None):
    """Stage PeriodO periods to {STAGED_BASE_DIR}/po/extract/places.jsonl."""
    print("=" * 80)
    print("PeriodO TEMPORAL PERIODS STAGING")
    print("=" * 80)

    data = fetch_periodo_data(file_path)
    spatial_geometry_index = load_periodo_gazetteer_geometries()

    authorities = data.get('authorities', {})
    if not authorities:
        graph = data.get('@graph', [])
        if graph:
            authorities = {}
            for item in graph:
                if 'periods' in item:
                    auth_id = item.get('id', item.get('@id', ''))
                    authorities[auth_id] = item

    if not authorities:
        print("ERROR: No authorities found in PeriodO dataset")
        return

    print(f"Found {len(authorities)} authorities")

    total_periods = sum(len(a.get('periods', {})) for a in authorities.values() if isinstance(a, dict))
    if total_periods:
        print(f"Found {total_periods:,} periods")

    total_staged = 0
    total_skipped = 0
    with_geometry = 0
    without_geometry = 0
    processed_periods = 0
    started = time.time()
    last_log = started

    for auth_id, auth_data in authorities.items():
        auth_label = auth_data.get('source', {}).get('title', '') if isinstance(auth_data.get('source'), dict) else ''
        periods = auth_data.get('periods', {})

        for period_id, period in periods.items():
            processed_periods += 1
            try:
                doc = process_periodo_period(
                    period_id, period, auth_id, auth_label, spatial_geometry_index
                )
                if not doc:
                    total_skipped += 1
                    continue

                write_staged_place_doc(namespace='po', doc=doc)
                total_staged += 1

                if doc.get('geometries'):
                    with_geometry += 1
                else:
                    without_geometry += 1

                now = time.time()
                if now - last_log >= PROGRESS_LOG_INTERVAL_SECONDS:
                    _log_progress(
                        "Periods processed",
                        processed_periods,
                        total=total_periods if total_periods else None,
                        start_ts=started,
                        extra=(
                            f"staged={total_staged:,} skipped={total_skipped:,} "
                            f"with_geom={with_geometry:,} without_geom={without_geometry:,}"
                        ),
                    )
                    last_log = now

            except Exception:
                total_skipped += 1
                continue

    print(f"\n\nPeriodO staging complete:")
    print(f"  Staged: {total_staged:,}")
    print(f"  Skipped: {total_skipped:,}")
    print(f"  With geometry: {with_geometry:,}")
    print(f"  Without geometry: {without_geometry:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Stage PeriodO periods')
    parser.add_argument('--file', help='Path to PeriodO JSON-LD file')
    args = parser.parse_args()

    stage_periodo(file_path=args.file)

