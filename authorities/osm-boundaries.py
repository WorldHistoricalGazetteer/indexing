# authorities/osm-boundaries.py

"""
OSM + OHM Administrative Boundary Extraction.

Extracts boundary=administrative relations from both OpenStreetMap and
OpenHistoricalMap PBF files into the `boundaries` ES index.  This index
serves the RegionSelector widget in the WHG search UI for spatial filtering.

Only relations are processed (nodes and ways are skipped entirely for speed).
OSM relations have permanent stable IDs.

Geometry handling:
  - `geom`: full-fidelity polygon for accurate ES geo_shape intersects filtering
  - `hull`: convex hull for fast spatial pre-screening
  - `bounds`: [west, south, east, north] envelope of the hull

Also produces:
  - GeoJSON Lines file for tippecanoe input
  - .mbtiles vector tileset (if tippecanoe is available)

Usage:
    python -m authorities.osm-boundaries                    # both OSM + OHM
    python -m authorities.osm-boundaries --source osm       # OSM only
    python -m authorities.osm-boundaries --source ohm       # OHM only
    python -m authorities.osm-boundaries --file /path/to.pbf --source osm
    python -m authorities.osm-boundaries --no-tiles          # skip mbtiles
"""

import json
import os
import re
import sys
import gc
import time
import shutil
import signal
import subprocess
from pathlib import Path
from datetime import datetime

import osmium
import orjson
import shapely.wkb as wkblib
from shapely.geometry import mapping
from shapely.validation import make_valid

from elasticsearch import Elasticsearch, helpers
from processing.helpers import compute_representative_point
from processing.settings import (
    ES_HOST, DATA_DIR,
    OSM_BOUNDARY_STATE_FILE, OHM_BOUNDARY_STATE_FILE,
    BOUNDARIES_INDEX,
)
from processing.utilities import create_checkpoint_snapshot

# ---------------- CONFIG ----------------
CHECKPOINT_INTERVAL = 10000
BULK_THREAD_COUNT = 4
QUEUE_SIZE = 8

# Valid admin_level range (OSM wiki: 2 = country, 10 = neighbourhood)
ADMIN_LEVELS = set(range(2, 11))  # 2..10

# Output directory for GeoJSON Lines and .mbtiles
BOUNDARIES_OUTPUT_DIR = Path(DATA_DIR) / 'boundaries'
GEOJSONL_FILE = BOUNDARIES_OUTPUT_DIR / 'boundaries.geojsonl'
MBTILES_FILE = BOUNDARIES_OUTPUT_DIR / 'boundaries.mbtiles'


# ---------------- DATE PARSING (OHM) ----------------
_YEAR_RE = re.compile(
    r'^~?'
    r'(?:(?:before|after|about|circa|ca)\s*:?\s*)?'
    r'(-?\d{1,5})'
    r'(?:[-/]\d{1,2})?'
    r'(?:[-/]\d{1,2})?'
    r'(?:T.*)?$',
    re.IGNORECASE,
)

_CENTURY_RE = re.compile(
    r'^C(\d{1,2})$',
    re.IGNORECASE,
)


def parse_year(date_str):
    """Extract an integer year from an OHM-style date string."""
    if not date_str:
        return None
    date_str = date_str.strip()

    m = _YEAR_RE.match(date_str)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, OverflowError):
            return None

    m = _CENTURY_RE.match(date_str)
    if m:
        try:
            return (int(m.group(1)) - 1) * 100
        except ValueError:
            return None

    return None


def build_timespans(tags):
    """Build timespans from start_date/end_date tags. Returns list or []."""
    start_year = parse_year(tags.get('start_date'))
    end_year = parse_year(tags.get('end_date'))

    if start_year is not None or end_year is not None:
        ts = {}
        if start_year is not None:
            ts['start'] = {'in': start_year}
        if end_year is not None:
            ts['end'] = {'in': end_year}
        return [ts]
    return []


# ---------------- STATE MANAGEMENT ----------------
class ProgressTracker:
    def __init__(self, state_file):
        self.state_file = state_file
        self.count = 0
        self.target = 0
        self.start_time = time.time()
        self.load_state()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.target = data.get('count', 0)
                    print(f"RESUMING from checkpoint: {self.target:,} relations")
            except Exception as e:
                print(f"Warning: failed to read state file: {e}")

    def save_state(self):
        temp_file = f"{self.state_file}.tmp"
        with open(temp_file, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'count': self.count
            }, f)
        os.replace(temp_file, self.state_file)

    def should_skip(self):
        if self.count < self.target:
            self.count += 1
            if self.count % 100000 == 0:
                print(f"\r  Skipping... {self.count:,}/{self.target:,}", end='', flush=True)
            return True
        return False

    def increment(self):
        self.count += 1
        if self.count % CHECKPOINT_INTERVAL == 0:
            self.save_state()
            elapsed = time.time() - self.start_time
            rate = self.count / elapsed if elapsed > 0 else 0
            print(f"\rProcessed {self.count:,} relations ({rate:.0f}/s)", end='', flush=True)


# ---------------- DOCUMENT BUILDER ----------------
def create_boundary_doc(relation_id, tags, shapely_geom, namespace, source):
    """
    Build an ES document for the boundaries index.

    Args:
        relation_id: OSM/OHM relation ID (integer)
        tags: dict of extracted tags
        shapely_geom: Shapely geometry object (validated, non-empty)
        namespace: 'osm' or 'ohm'
        source: 'osm' or 'ohm'

    Returns:
        dict suitable for ES indexing, or None on failure
    """
    boundary_id = f"{namespace}:r{relation_id}"

    full_geom = mapping(shapely_geom)

    doc = {
        'boundary_id': boundary_id,
        'namespace': namespace,
        'name': tags['name'],
        'source': source,
        'admin_level': tags['admin_level'],
        'indexed_at': datetime.now().isoformat(),
    }

    # Full geometry for accurate spatial filtering
    doc['geom'] = full_geom

    # Convex hull for fast spatial pre-screening
    hull = shapely_geom.convex_hull
    if hull and not hull.is_empty:
        doc['hull'] = mapping(hull)

        # Bounds from the hull: [west, south, east, north]
        hb = hull.bounds  # (minx, miny, maxx, maxy)
        doc['bounds'] = [
            round(hb[0], 6),  # west  (minlon)
            round(hb[1], 6),  # south (minlat)
            round(hb[2], 6),  # east  (maxlon)
            round(hb[3], 6),  # north (maxlat)
        ]

    # Representative point
    rep_point = compute_representative_point(full_geom)
    if rep_point:
        doc['repr_point'] = rep_point

    # Local-language name (name tag without language qualifier)
    if 'name_local' in tags and tags['name_local'] != tags['name']:
        doc['name_local'] = tags['name_local']

    # Alternate names keyed by language code
    if tags.get('alt_names'):
        doc['alt_names'] = tags['alt_names']

    # Country codes from ISO3166 tags
    ccodes = []
    if 'iso3166_1' in tags:
        ccodes.append(tags['iso3166_1'].upper())
    if 'iso3166_2' in tags:
        # ISO3166-2 codes are like "GB-ENG"; extract country part
        parts = tags['iso3166_2'].split('-')
        if parts and len(parts[0]) == 2:
            cc = parts[0].upper()
            if cc not in ccodes:
                ccodes.append(cc)
    if ccodes:
        doc['ccodes'] = ccodes

    # Population
    if 'population' in tags:
        try:
            doc['population'] = int(tags['population'])
        except (ValueError, TypeError):
            pass

    # Wikidata link
    if 'wikidata' in tags:
        doc['wikidata_id'] = tags['wikidata']

    # Timespans (primarily OHM)
    timespans = build_timespans(tags)
    if timespans:
        doc['timespans'] = timespans

    return doc


def process_relation_tags(tags):
    """
    Filter and extract tags from a boundary=administrative relation.

    Returns dict of extracted tags, or None if this relation should be skipped.
    """
    # Must have: name, boundary=administrative, valid admin_level
    if 'name' not in tags:
        return None
    if tags.get('boundary') != 'administrative':
        return None

    # Parse and validate admin_level
    try:
        admin_level = int(tags.get('admin_level', ''))
    except (ValueError, TypeError):
        return None
    if admin_level not in ADMIN_LEVELS:
        return None

    result = {
        'name': tags['name'],
        'admin_level': admin_level,
        'alt_names': {},
    }

    # Extract all relevant tags
    for tag in tags:
        k, v = tag.k, tag.v
        if k.startswith('name:'):
            result['alt_names'][k[5:]] = v
        elif k == 'int_name':
            result['alt_names']['int'] = v
        elif k == 'official_name':
            result['name_local'] = v
        elif k == 'ISO3166-1:alpha2' or k == 'ISO3166-1':
            result['iso3166_1'] = v
        elif k == 'ISO3166-2':
            result['iso3166_2'] = v
        elif k in {'population', 'wikidata', 'start_date', 'end_date'}:
            result[k] = v

    return result


# ---------------- HANDLER ----------------
class BoundaryHandler(osmium.SimpleHandler):
    """
    Osmium handler that extracts only boundary=administrative relations.

    Skips nodes and ways entirely for maximum speed — only relation()
    is implemented.
    """

    def __init__(self, tracker, buffer_callback, namespace, geojsonl_fh=None):
        super().__init__()
        self.tracker = tracker
        self.buffer_callback = buffer_callback
        self.namespace = namespace
        self.geojsonl_fh = geojsonl_fh
        self.wkbfab = osmium.geom.WKBFactory()
        self.extracted = 0
        self.skipped_invalid = 0
        self.skipped_empty = 0

    def relation(self, r):
        if not r.tags:
            return

        if self.tracker.should_skip():
            return

        tags = process_relation_tags(r.tags)
        if not tags:
            self.tracker.increment()
            return

        try:
            wkb = self.wkbfab.create_multipolygon(r)
            geom = wkblib.loads(wkb, hex=False)

            if not geom.is_valid:
                geom = make_valid(geom)
                if not geom.is_valid:
                    self.skipped_invalid += 1
                    self.tracker.increment()
                    return

            if geom.is_empty:
                self.skipped_empty += 1
                self.tracker.increment()
                return

            doc = create_boundary_doc(
                r.id, tags, geom,
                namespace=self.namespace,
                source=self.namespace,
            )
            if doc:
                self.buffer_callback(doc)
                self.extracted += 1

                # Write GeoJSON Lines feature for tippecanoe
                if self.geojsonl_fh is not None:
                    feature = {
                        'type': 'Feature',
                        'properties': {
                            'id': doc['boundary_id'],
                            'name': doc['name'],
                            'admin_level': doc['admin_level'],
                            'namespace': doc['namespace'],
                        },
                        'geometry': doc['geom'],
                    }
                    if 'ccodes' in doc:
                        feature['properties']['ccodes'] = ','.join(doc['ccodes'])
                    self.geojsonl_fh.write(orjson.dumps(feature))
                    self.geojsonl_fh.write(b'\n')

        except Exception:
            pass  # Geometry construction failed (incomplete relation)

        self.tracker.increment()


# ---------------- FILE STAGING ----------------
def stage_file_to_scratch(source_path):
    """Copy PBF to local scratch (NVMe) if running on Slurm."""
    scratch_dir = os.environ.get('SLURM_SCRATCH')
    if not scratch_dir or not os.path.exists(scratch_dir):
        print("Notice: No scratch dir found, using network storage.")
        return source_path, False

    target_path = os.path.join(scratch_dir, os.path.basename(source_path))

    if os.path.exists(target_path):
        print(f"Using existing staged file: {target_path}")
        return target_path, True

    print(f"Staging to local scratch: {target_path}")
    subprocess.run(['rsync', '-ah', str(source_path), target_path], check=True)
    return target_path, True


# ---------------- MBTILES GENERATION ----------------
def generate_mbtiles(geojsonl_path, mbtiles_path):
    """
    Generate .mbtiles vector tileset from GeoJSON Lines file using tippecanoe.

    tippecanoe is invoked with settings appropriate for admin boundaries:
    - Full detail at all zoom levels (no feature dropping)
    - Simplification appropriate for each zoom level
    - Layer name: 'boundaries'
    """
    tippecanoe = shutil.which('tippecanoe')
    if not tippecanoe:
        print("\nWARNING: tippecanoe not found — skipping .mbtiles generation")
        print("  Install with: conda install -c conda-forge tippecanoe")
        return False

    if not geojsonl_path.exists() or geojsonl_path.stat().st_size == 0:
        print("\nWARNING: GeoJSON Lines file is empty — skipping .mbtiles generation")
        return False

    print(f"\n{'=' * 80}")
    print("GENERATING .mbtiles VECTOR TILESET")
    print(f"{'=' * 80}")
    print(f"Input:  {geojsonl_path} ({geojsonl_path.stat().st_size / 1e9:.1f} GB)")
    print(f"Output: {mbtiles_path}")

    cmd = [
        tippecanoe,
        '--output', str(mbtiles_path),
        '--force',                          # Overwrite existing
        '--layer', 'boundaries',            # Layer name in the tileset
        '--name', 'WHG Admin Boundaries',
        '--description', 'OSM + OHM administrative boundaries for WHG spatial filtering',
        '--minimum-zoom', '0',
        '--maximum-zoom', '10',
        '--no-tile-size-limit',             # Don't drop features to fit tile size
        '--simplification', '10',           # Simplify at low zooms
        '--detect-shared-borders',          # Clean up shared boundaries between regions
        '--coalesce-densest-as-needed',     # Coalesce at low zooms rather than drop
        '--extend-zooms-if-still-dropping', # Extend max zoom if features are still being dropped
        '--read-parallel',                  # Parallel reading
        str(geojsonl_path),
    ]

    start = time.time()
    try:
        result = subprocess.run(
            cmd, stdout=sys.stdout, stderr=sys.stderr,
        )
        elapsed = time.time() - start

        if result.returncode == 0 and mbtiles_path.exists():
            size_mb = mbtiles_path.stat().st_size / 1e6
            print(f"\n✓ .mbtiles generated: {mbtiles_path} ({size_mb:.1f} MB) in {elapsed:.0f}s")
            return True
        else:
            print(f"\n✗ tippecanoe failed with exit code {result.returncode}")
            return False

    except Exception as e:
        print(f"\n✗ tippecanoe error: {e}")
        return False


# ---------------- MAIN INGESTION ----------------
def ingest_boundaries(pbf_file, namespace, state_file, geojsonl_fh=None):
    """
    Extract and index administrative boundaries from a single PBF file.

    Args:
        pbf_file: Path to the OSM/OHM PBF file
        namespace: 'osm' or 'ohm'
        state_file: Path to the progress state file
        geojsonl_fh: Open binary file handle for GeoJSON Lines output (or None)
    """
    es = Elasticsearch(ES_HOST, request_timeout=180, max_retries=10, retry_on_timeout=True)
    tracker = ProgressTracker(state_file)

    # Signal handling
    def signal_handler(sig, frame):
        print("\n!!! SIGNAL RECEIVED - SAVING STATE !!!")
        tracker.save_state()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Parallel bulk buffer
    buffer_list = []
    indexed_count = 0
    failed_count = 0

    def flush_buffer():
        nonlocal indexed_count, failed_count
        if not buffer_list:
            return

        for success, info in helpers.parallel_bulk(
                es, buffer_list,
                thread_count=BULK_THREAD_COUNT,
                queue_size=QUEUE_SIZE,
                raise_on_error=False
        ):
            if success:
                indexed_count += 1
            else:
                failed_count += 1
                if failed_count <= 10:
                    print(f"\n  Bulk index error: {info}")

        buffer_list.clear()

        if indexed_count % 5000 == 0:
            gc.collect()

    def add_to_buffer(doc):
        buffer_list.append({
            '_index': BOUNDARIES_INDEX,
            '_id': doc['boundary_id'],
            '_source': doc
        })
        if len(buffer_list) >= 500:  # Smaller batches — boundary docs are large
            flush_buffer()

    # Stage PBF to local scratch if available
    active_pbf, is_staged = stage_file_to_scratch(pbf_file)

    try:
        source_label = 'OSM' if namespace == 'osm' else 'OHM'
        print(f"\n{'=' * 80}")
        print(f"{source_label} ADMINISTRATIVE BOUNDARY EXTRACTION")
        print(f"{'=' * 80}")
        print(f"Source: {active_pbf}")
        print(f"Target index: {BOUNDARIES_INDEX}")
        print(f"Namespace: {namespace}")
        print()

        handler = BoundaryHandler(tracker, add_to_buffer, namespace, geojsonl_fh=geojsonl_fh)
        handler.apply_file(str(active_pbf), locations=True, idx='flex_mem')

        flush_buffer()
        tracker.save_state()

        print(f"\n\n{source_label} extraction complete:")
        print(f"  Boundaries extracted: {handler.extracted:,}")
        print(f"  Documents indexed:    {indexed_count:,}")
        print(f"  Documents failed:     {failed_count:,}")
        print(f"  Skipped (invalid):    {handler.skipped_invalid:,}")
        print(f"  Skipped (empty):      {handler.skipped_empty:,}")

        return indexed_count

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        tracker.save_state()
        raise
    finally:
        # Don't remove staged PBF — the places script may need it too
        pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract OSM/OHM admin boundaries into the boundaries ES index"
    )
    parser.add_argument(
        '--source', choices=['osm', 'ohm', 'both'], default='both',
        help='Which PBF source(s) to process (default: both)'
    )
    parser.add_argument('--file', help='Override PBF file path (use with --source osm or ohm)')
    parser.add_argument('--no-tiles', action='store_true',
                        help='Skip .mbtiles generation (even if tippecanoe is available)')
    args = parser.parse_args()

    sources = ['osm', 'ohm'] if args.source == 'both' else [args.source]

    # Ensure output directory exists
    BOUNDARIES_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    total_indexed = 0

    # Open GeoJSON Lines file for the full run (both sources append)
    with open(GEOJSONL_FILE, 'wb') as geojsonl_fh:
        for source in sources:
            if args.file:
                pbf_path = Path(args.file)
            elif source == 'osm':
                pbf_path = Path(DATA_DIR) / 'authorities' / 'osm' / 'planet-latest.osm.pbf'
            else:  # ohm
                pbf_path = Path(DATA_DIR) / 'authorities' / 'ohm' / 'planet-latest.osm.pbf'

            if not pbf_path.exists():
                print(f"WARNING: PBF file not found: {pbf_path}")
                print(f"  Skipping {source.upper()} boundaries.")
                continue

            state_file = OSM_BOUNDARY_STATE_FILE if source == 'osm' else OHM_BOUNDARY_STATE_FILE
            count = ingest_boundaries(
                pbf_path, namespace=source, state_file=state_file,
                geojsonl_fh=geojsonl_fh,
            )
            total_indexed += count

    if total_indexed > 0:
        print(f"\n{'=' * 80}")
        print(f"TOTAL BOUNDARIES INDEXED: {total_indexed:,}")
        print(f"{'=' * 80}")

        geojsonl_size = GEOJSONL_FILE.stat().st_size if GEOJSONL_FILE.exists() else 0
        print(f"GeoJSON Lines: {GEOJSONL_FILE} ({geojsonl_size / 1e6:.1f} MB)")

        # Generate .mbtiles
        if not args.no_tiles:
            generate_mbtiles(GEOJSONL_FILE, MBTILES_FILE)

        # Create checkpoint snapshot
        es = Elasticsearch(ES_HOST, request_timeout=180)
        create_checkpoint_snapshot(es, 'boundaries')

    print("\nBoundary extraction complete.")


