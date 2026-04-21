# processing/generate_tiles.py

"""
Standalone Tileset Generator.

Queries the ``places`` index for boundary-qualifying records (``boundary``
field exists), groups by namespace prefix, and generates per-authority
``.mbtiles`` vector tilesets via tippecanoe.

Multilingual labels are extracted from the ``toponyms[]`` nested array on
each place doc (each entry has ``toponym_id`` in ``name@lang`` format).

Usage:
    python -m processing.generate_tiles --es-host URL
    python -m processing.generate_tiles --es-host URL --authority osm
    python -m processing.generate_tiles --es-host URL --deploy
"""

import sys
import shutil
import subprocess
import time
from pathlib import Path
from collections import defaultdict

import orjson
from elasticsearch import Elasticsearch

from processing.feature_ids import (
    encode_feature_id,
    encode_misc_feature_id,
)
from processing.osm_boundary_geometry import (
    is_admin_boundary_value,
    is_misc_boundary_value,
)
from processing.settings import DATA_DIR

# Output directory
TILES_OUTPUT_DIR = Path(DATA_DIR) / 'tiles'

# Curated display language set
DISPLAY_LANGUAGES = {
    'en', 'fr', 'es', 'ar', 'zh', 'ru',  # UN official languages
    'de', 'pt', 'ja', 'ko', 'hi',         # widely-used languages
}

# Country → primary local language (ISO 3166 alpha-2 → ISO 639-1)
COUNTRY_LOCAL_LANG = {
    'FR': 'fr', 'DE': 'de', 'ES': 'es', 'PT': 'pt', 'IT': 'it',
    'NL': 'nl', 'PL': 'pl', 'RU': 'ru', 'JP': 'ja', 'KR': 'ko',
    'CN': 'zh', 'TW': 'zh', 'IN': 'hi', 'SA': 'ar', 'EG': 'ar',
    'BR': 'pt', 'MX': 'es', 'AR': 'es', 'CL': 'es', 'CO': 'es',
    'TR': 'tr', 'GR': 'el', 'TH': 'th', 'VN': 'vi', 'ID': 'id',
    'MY': 'ms', 'PH': 'tl', 'UA': 'uk', 'CZ': 'cs', 'SE': 'sv',
    'NO': 'no', 'DK': 'da', 'FI': 'fi', 'HU': 'hu', 'RO': 'ro',
    'BG': 'bg', 'HR': 'hr', 'RS': 'sr', 'SK': 'sk', 'SI': 'sl',
    'LT': 'lt', 'LV': 'lv', 'EE': 'et', 'IS': 'is', 'IE': 'ga',
    'GB': 'en', 'US': 'en', 'CA': 'en', 'AU': 'en', 'NZ': 'en',
    'IL': 'he', 'IR': 'fa', 'PK': 'ur', 'BD': 'bn', 'MM': 'my',
    'KH': 'km', 'LA': 'lo', 'GE': 'ka', 'AM': 'hy', 'AZ': 'az',
    'KZ': 'kk', 'UZ': 'uz', 'MN': 'mn', 'ET': 'am', 'KE': 'sw',
    'TZ': 'sw', 'ZA': 'zu', 'NG': 'ha',
}

# Admin level → tippecanoe minzoom
ADMIN_LEVEL_MINZOOM = {
    '0': 0, '1': 0, '2': 0, '3': 2, '4': 3, '5': 4,
    '6': 5, '7': 6, '8': 7, '9': 8, '10': 9, '11': 10,
}

def _is_admin_level(boundary_value: str) -> bool:
    """Check if a boundary value is a numeric admin level."""
    return is_admin_boundary_value(boundary_value)


def _is_misc_boundary(boundary_value: str) -> bool:
    """Check if a boundary value is a curated miscellaneous type."""
    return is_misc_boundary_value(boundary_value)


def _extract_source_id(place_id: str) -> int | str:
    """Extract the source ID from a place_id like 'osm:r12345'."""
    _, raw = place_id.split(':', 1)
    # Strip type prefix (n, w, r for OSM)
    if raw and raw[0] in 'nwr' and raw[1:].isdigit():
        return int(raw[1:])
    # Try direct int
    try:
        return int(raw)
    except ValueError:
        return raw


def _extract_toponyms_by_lang(toponyms: list[dict]) -> dict[str, str]:
    """Extract name→lang mapping from toponyms array."""
    by_lang = {}
    for t in toponyms:
        tid = t.get('toponym_id', '')
        if '@' in tid:
            name, lang = tid.rsplit('@', 1)
            if lang and name:
                by_lang.setdefault(lang, name)
    return by_lang


def _build_feature(hit, namespace):
    """Build a GeoJSON Feature dict from an ES hit."""
    src = hit['_source']
    place_id = src['place_id']
    boundary = src.get('boundary', '')

    # Get geometry
    geom_list = src.get('geometries', [])
    if not geom_list:
        return None
    geom_data = geom_list[0].get('geom')
    if not geom_data:
        return None

    # Properties
    props = {
        'place_id': place_id,
        'boundary': boundary,
        'namespace': namespace,
    }

    # Minzoom
    if _is_admin_level(boundary):
        minzoom = ADMIN_LEVEL_MINZOOM.get(boundary, 0)
    else:
        minzoom = 3  # Default for non-admin boundaries
    if minzoom > 0:
        props['tippecanoe:minzoom'] = minzoom

    # Extract multilingual names
    toponyms = src.get('toponyms', [])
    names_by_lang = _extract_toponyms_by_lang(toponyms)

    # Default name (title)
    props['name'] = src.get('title', '')

    # Display language names
    for lang in DISPLAY_LANGUAGES:
        if lang in names_by_lang:
            props[f'name_{lang}'] = names_by_lang[lang]

    # Local name (based on country codes)
    ccodes = src.get('ccodes', [])
    if ccodes:
        primary_cc = ccodes[0]
        local_lang = COUNTRY_LOCAL_LANG.get(primary_cc)
        if local_lang and local_lang in names_by_lang:
            props['name_local'] = names_by_lang[local_lang]

    if 'name_local' not in props and 'und' in names_by_lang:
        props['name_local'] = names_by_lang['und']

    # Integer feature ID
    source_id = _extract_source_id(place_id)
    feature_id = encode_feature_id(namespace, source_id)

    return {
        'type': 'Feature',
        'id': feature_id,
        'properties': props,
        'geometry': geom_data,
    }


def _build_misc_feature(hit, namespace):
    """Build a feature for the miscellaneous boundary tileset."""
    feature = _build_feature(hit, namespace)
    if not feature:
        return None

    # Re-encode with misc encoding (1-bit namespace discrimination)
    source_id = _extract_source_id(hit['_source']['place_id'])
    if isinstance(source_id, int):
        feature['id'] = encode_misc_feature_id(namespace, source_id)
    else:
        # String ID — hash it, then apply misc encoding
        import hashlib
        h = hashlib.sha256(source_id.encode('utf-8')).digest()
        numeric_id = int.from_bytes(h[:7], 'big') & ((1 << 52) - 1)
        feature['id'] = encode_misc_feature_id(namespace, numeric_id)

    return feature


def scroll_boundary_docs(es, places_index, query, batch_size=500):
    """Generator yielding ES hits for boundary docs."""
    resp = es.search(index=places_index, body=query, scroll='10m', size=batch_size)
    scroll_id = resp['_scroll_id']

    while True:
        hits = resp['hits']['hits']
        if not hits:
            break
        yield from hits
        resp = es.scroll(scroll_id=scroll_id, scroll='10m')

    try:
        es.clear_scroll(scroll_id=scroll_id)
    except Exception:
        pass


def generate_tileset(geojsonl_path, mbtiles_path, layer_name, description=''):
    """Generate .mbtiles from GeoJSON Lines file using tippecanoe."""
    tippecanoe = shutil.which('tippecanoe')
    if not tippecanoe:
        print("  WARNING: tippecanoe not found — skipping .mbtiles generation")
        return False

    if not geojsonl_path.exists() or geojsonl_path.stat().st_size == 0:
        print("  WARNING: GeoJSON Lines file is empty — skipping")
        return False

    size_mb = geojsonl_path.stat().st_size / 1e6
    print(f"  Generating {mbtiles_path.name} from {size_mb:.1f} MB ...")

    cmd = [
        tippecanoe,
        '--output', str(mbtiles_path),
        '--force',
        '--layer', layer_name,
        '--name', f'WHG {layer_name}',
        '--description', description or f'WHG {layer_name} boundaries',
        '--minimum-zoom', '0',
        '--maximum-zoom', '10',
        '--simplification', '10',
        '--detect-shared-borders',
        '--coalesce-densest-as-needed',
        '--extend-zooms-if-still-dropping',
        '--no-tile-compression',
        '--read-parallel',
        str(geojsonl_path),
    ]

    start = time.time()
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr)
    elapsed = time.time() - start

    if result.returncode == 0 and mbtiles_path.exists():
        out_mb = mbtiles_path.stat().st_size / 1e6
        print(f"  ✓ {mbtiles_path.name}: {out_mb:.1f} MB ({elapsed:.0f}s)")
        return True
    else:
        print(f"  ✗ tippecanoe failed (exit code {result.returncode})")
        return False


def generate_tiles(es_host, places_index='places_*', authority=None,
                   output_dir=None, deploy=False):
    """Main tileset generation pipeline."""
    print("=" * 80)
    print("TILESET GENERATION")
    print("=" * 80)

    es = Elasticsearch(es_host, request_timeout=120, max_retries=5,
                       retry_on_timeout=True)

    out_dir = Path(output_dir) if output_dir else TILES_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Query all boundary docs
    base_query = {
        'query': {'exists': {'field': 'boundary'}},
        '_source': ['place_id', 'title', 'boundary', 'geometries',
                    'toponyms', 'ccodes', 'namespace'],
    }

    # Count by namespace
    count_query = {
        'size': 0,
        'query': {'exists': {'field': 'boundary'}},
        'aggs': {
            'by_namespace': {
                'terms': {'field': 'place_id', 'size': 100,
                          'include': '.*:.*'},
            }
        }
    }

    # Simpler: just scroll all and group in memory
    print("\nScanning boundary docs ...")

    # Group hits by namespace and boundary type category
    admin_by_ns = defaultdict(list)    # namespace → [hits with admin level boundaries]
    misc_by_ns = defaultdict(list)     # namespace → [hits with misc boundaries]
    other_by_ns = defaultdict(list)    # namespace → [hits with non-OSM boundaries]

    doc_count = 0
    for hit in scroll_boundary_docs(es, places_index, base_query):
        src = hit['_source']
        place_id = src.get('place_id', '')
        boundary = src.get('boundary', '')
        ns = place_id.split(':')[0] if ':' in place_id else 'unknown'

        if authority and ns != authority:
            continue

        doc_count += 1

        if ns in ('osm', 'ohm'):
            if _is_admin_level(boundary):
                admin_by_ns[ns].append(hit)
            elif _is_misc_boundary(boundary):
                misc_by_ns[ns].append(hit)
            else:
                # Non-curated misc — skip tileset
                pass
        else:
            other_by_ns[ns].append(hit)

        if doc_count % 10000 == 0:
            print(f"\r  Scanned {doc_count:,} docs ...", end='', flush=True)

    print(f"\r  Scanned {doc_count:,} boundary docs total")

    # Report
    print("\nBoundary docs by category:")
    for ns in sorted(set(list(admin_by_ns) + list(misc_by_ns) + list(other_by_ns))):
        admin_ct = len(admin_by_ns.get(ns, []))
        misc_ct = len(misc_by_ns.get(ns, []))
        other_ct = len(other_by_ns.get(ns, []))
        total = admin_ct + misc_ct + other_ct
        parts = []
        if admin_ct: parts.append(f"{admin_ct:,} admin")
        if misc_ct: parts.append(f"{misc_ct:,} misc")
        if other_ct: parts.append(f"{other_ct:,} other")
        print(f"  {ns}: {total:,} ({', '.join(parts)})")

    tilesets_generated = []

    # Generate admin tilesets per namespace
    for ns in sorted(admin_by_ns):
        hits = admin_by_ns[ns]
        if not hits:
            continue

        geojsonl_path = out_dir / f'{ns}_admin.geojsonl'
        mbtiles_path = out_dir / f'{ns}_admin.mbtiles'

        print(f"\nWriting {ns} admin GeoJSON Lines ({len(hits):,} features) ...")
        with open(geojsonl_path, 'wb') as f:
            for hit in hits:
                feature = _build_feature(hit, ns)
                if feature:
                    f.write(orjson.dumps(feature))
                    f.write(b'\n')

        if generate_tileset(geojsonl_path, mbtiles_path,
                            f'{ns}_admin', f'{ns.upper()} admin boundaries'):
            tilesets_generated.append(mbtiles_path)

    # Generate miscellaneous tileset (OSM + OHM combined)
    misc_hits = []
    for ns in ('osm', 'ohm'):
        misc_hits.extend([(hit, ns) for hit in misc_by_ns.get(ns, [])])

    if misc_hits:
        geojsonl_path = out_dir / 'osm_misc.geojsonl'
        mbtiles_path = out_dir / 'osm_misc.mbtiles'

        print(f"\nWriting OSM/OHM misc GeoJSON Lines ({len(misc_hits):,} features) ...")
        with open(geojsonl_path, 'wb') as f:
            for hit, ns in misc_hits:
                feature = _build_misc_feature(hit, ns)
                if feature:
                    f.write(orjson.dumps(feature))
                    f.write(b'\n')

        if generate_tileset(geojsonl_path, mbtiles_path,
                            'osm_misc', 'OSM/OHM miscellaneous boundaries'):
            tilesets_generated.append(mbtiles_path)

    # Generate tilesets for other authorities
    for ns in sorted(other_by_ns):
        hits = other_by_ns[ns]
        if not hits:
            continue

        geojsonl_path = out_dir / f'{ns}.geojsonl'
        mbtiles_path = out_dir / f'{ns}.mbtiles'

        print(f"\nWriting {ns} GeoJSON Lines ({len(hits):,} features) ...")
        with open(geojsonl_path, 'wb') as f:
            for hit in hits:
                feature = _build_feature(hit, ns)
                if feature:
                    f.write(orjson.dumps(feature))
                    f.write(b'\n')

        if generate_tileset(geojsonl_path, mbtiles_path,
                            ns, f'{ns} boundaries'):
            tilesets_generated.append(mbtiles_path)

    # Summary
    print(f"\n{'=' * 80}")
    print(f"TILESET GENERATION COMPLETE")
    print(f"{'=' * 80}")
    print(f"  Tilesets generated: {len(tilesets_generated)}")
    for p in tilesets_generated:
        size_mb = p.stat().st_size / 1e6 if p.exists() else 0
        print(f"    {p.name}: {size_mb:.1f} MB")

    # Deploy if requested
    if deploy and tilesets_generated:
        deploy_tilesets(tilesets_generated)

    return tilesets_generated


def deploy_tilesets(mbtiles_paths, remote_host='134.209.177.234',
                    remote_user='whgadmin',
                    remote_dir='/data/tileserver/mbtiles'):
    """Deploy .mbtiles to TileServer GL light via rsync."""
    print(f"\nDeploying {len(mbtiles_paths)} tilesets to {remote_user}@{remote_host} ...")

    for path in mbtiles_paths:
        if not path.exists():
            continue
        target = f"{remote_user}@{remote_host}:{remote_dir}/{path.name}"
        print(f"  rsync {path.name} → {target}")
        result = subprocess.run(
            ['rsync', '-az', '--progress', str(path), target],
            stdout=sys.stdout, stderr=sys.stderr,
        )
        if result.returncode != 0:
            print(f"  ✗ rsync failed for {path.name}")

    print("  Deploy complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate .mbtiles tilesets from boundary places in ES"
    )
    parser.add_argument('--es-host', required=True, help='Elasticsearch URL')
    parser.add_argument('--places-index', default='places_*',
                        help='Places index pattern')
    parser.add_argument('--authority', help='Filter to a single authority namespace')
    parser.add_argument('--output-dir', help='Output directory for tilesets')
    parser.add_argument('--deploy', action='store_true',
                        help='Deploy tilesets to TileServer GL')
    args = parser.parse_args()

    generate_tiles(
        es_host=args.es_host,
        places_index=args.places_index,
        authority=args.authority,
        output_dir=args.output_dir,
        deploy=args.deploy,
    )


if __name__ == '__main__':
    main()



