# processing/osm_boundary_geometry.py

"""
Shared OSM/OHM boundary geometry completion.

This module contains the importable logic that was previously implemented
entirely in ``authorities/osm-boundary-pass.py``. It is used by:

- ``authorities/osm-places.py``
- ``authorities/ohm-places.py``
- ``authorities/osm-boundary-pass.py`` (thin CLI wrapper)
- ``processing/generate_tiles.py`` (shared boundary classification helpers)

The main entry point is ``run_boundary_pass()`` which assembles full relation
multipolygon geometry from OSM/OHM PBFs and updates the ``places`` index.
"""

from __future__ import annotations

import gc
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from elasticsearch import Elasticsearch, helpers
from shapely.geometry import mapping, shape
from shapely.validation import make_valid

from processing.helpers import enrich_geometry, compute_h3_fields, select_h3_cover_geometry
from processing.settings import ES_HOST, DATA_DIR

BULK_THREAD_COUNT = 4
QUEUE_SIZE = 8

ADMIN_LEVELS = set(range(0, 12))  # 0..11

CURATED_MISC_BOUNDARY_TYPES = {
    'aboriginal_lands', 'barony', 'civil', 'civil_parish', 'climatic_zone',
    'cofi_parish', 'environment', 'geographic', 'indigenous_administration',
    'local_authority', 'native_reservation', 'obsolete_administrative',
    'old_administrative', 'parish', 'political', 'rc_parish', 'region',
}

HISTORIC_PREFIX = 'histori'
SPECIAL_BOUNDARY_TYPES = {'continent', 'country_border'}

_YEAR_RE = re.compile(
    r'^~?'
    r'(?:(?:before|after|about|circa|ca)\s*:?\s*)?'
    r'(-?\d{1,5})'
    r'(?:[-/]\d{1,2})?'
    r'(?:[-/]\d{1,2})?'
    r'(?:T.*)?$',
    re.IGNORECASE,
)

_CENTURY_RE = re.compile(r'^C(\d{1,2})$', re.IGNORECASE)


def _require_osmium():
    import osmium
    return osmium


def _require_wkblib():
    import shapely.wkb as wkblib
    return wkblib


def is_admin_boundary_value(boundary_value: str) -> bool:
    """Return True if the boundary value is a numeric admin level 0..11."""
    try:
        return int(boundary_value) in ADMIN_LEVELS
    except (ValueError, TypeError):
        return False


def is_misc_boundary_value(boundary_value: str) -> bool:
    """Return True if the boundary value is a curated miscellaneous boundary."""
    if not boundary_value:
        return False
    return (
        boundary_value in CURATED_MISC_BOUNDARY_TYPES
        or boundary_value.startswith(HISTORIC_PREFIX)
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
    """Build timespans from start_date/end_date tags."""
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


def process_relation_tags(tags):
    """
    Filter and extract tags from a boundary relation.

    Returns dict of extracted tags, or None if this relation should be skipped.
    Accepts both boundary=administrative and curated miscellaneous types.
    """
    if 'name' not in tags:
        return None

    boundary_value = tags.get('boundary', '')
    if not boundary_value:
        return None

    boundary_field = None
    admin_level_str = tags.get('admin_level', '')

    if boundary_value == 'administrative' and admin_level_str:
        try:
            admin_level = int(admin_level_str)
            if admin_level in ADMIN_LEVELS:
                boundary_field = str(admin_level)
        except (ValueError, TypeError):
            pass
    elif boundary_value == 'continent':
        boundary_field = '0'
    elif boundary_value == 'country_border' and not admin_level_str:
        boundary_field = '2'

    if boundary_field is None and is_misc_boundary_value(boundary_value):
        boundary_field = boundary_value

    if boundary_field is None:
        return None

    result = {
        'name': tags['name'],
        'boundary_field': boundary_field,
    }

    for tag in tags:
        k, v = tag.k, tag.v
        if k in {'population', 'wikidata', 'start_date', 'end_date'}:
            result[k] = v

    return result


def _iter_lon_values(geom):
    """Yield longitudes from nested GeoJSON coordinates."""
    if not isinstance(geom, dict):
        return
    coords = geom.get('coordinates')
    if coords is None:
        return

    stack = [coords]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (list, tuple)):
            if len(cur) >= 2 and isinstance(cur[0], (int, float)) and isinstance(cur[1], (int, float)):
                yield float(cur[0])
            else:
                for item in cur:
                    stack.append(item)


def is_antimeridian_risky(geom):
    """Heuristic: hulls spanning both sides of +/-180 often fail ES geo_shape parse."""
    lons = list(_iter_lon_values(geom))
    if not lons:
        return False
    min_lon = min(lons)
    max_lon = max(lons)
    return (min_lon < -170 and max_lon > 170) or (max_lon - min_lon > 300)


def build_h3_fields_for_geom_entry(geom_entry, raw_geom):
    """Build top-level H3 fields for a finalized boundary geometry entry."""
    if not isinstance(geom_entry, dict):
        return None

    rp = geom_entry.get('repr_point')
    if not isinstance(rp, dict) or 'lon' not in rp or 'lat' not in rp:
        return None

    h3_geom = select_h3_cover_geometry(geom_entry, raw_geom)
    h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], h3_geom)
    if not h3c:
        return None

    return {
        'h3_centroid': h3c,
        'h3_cover': h3cover,
    }


class BoundaryPassProcessor:
    """Process assembled Area objects and emit place updates."""

    def __init__(self, buffer_callback, namespace):
        osmium = _require_osmium()
        self.osmium = osmium
        self.buffer_callback = buffer_callback
        self.namespace = namespace
        self.wkbfab = osmium.geom.WKBFactory()
        self.extracted = 0
        self.skipped_invalid = 0
        self.skipped_empty = 0
        self.geom_errors = 0
        self.areas_seen = 0
        self.tag_rejected = 0
        self.raw_geom_fixed = 0
        self.raw_geom_fixed_by_buffer = 0
        self.raw_geom_fixed_by_hull = 0
        self.hull_kept = 0
        self.hull_fixed = 0
        self.hull_dropped_invalid = 0
        self.hull_dropped_antimeridian = 0
        self.start_time = time.time()

    def process_area(self, area):
        """Process a single assembled Area object."""
        wkblib = _require_wkblib()
        self.areas_seen += 1

        tags = process_relation_tags(area.tags)
        if not tags:
            self.tag_rejected += 1
            return

        try:
            wkb = self.wkbfab.create_multipolygon(area)
            geom = wkblib.loads(wkb, hex=False)

            if not geom.is_valid:
                original_geom = geom
                geom = make_valid(geom)
                if geom.is_valid:
                    self.raw_geom_fixed += 1
                else:
                    geom = original_geom.buffer(0)
                    if geom.is_valid:
                        self.raw_geom_fixed += 1
                        self.raw_geom_fixed_by_buffer += 1
                    else:
                        geom = original_geom.convex_hull
                        if geom.is_valid:
                            self.raw_geom_fixed += 1
                            self.raw_geom_fixed_by_hull += 1
                        else:
                            self.skipped_invalid += 1
                            return

            if geom.is_empty:
                self.skipped_empty += 1
                return

            relation_id = area.orig_id()
            place_id = f"{self.namespace}:r{relation_id}"
            timespans = build_timespans(tags)
            raw_geom = mapping(geom)
            # ``geom_key`` is required for the multipolygon to be written to
            # the geom store. Without it ``enrich_geometry`` only emits the
            # hull/repr_point preview and ``has_geom`` stays False — leaving
            # tile generation with nothing to tile (boundaries store full
            # geom externally; the doc keeps only the simplified preview).
            geom_entry = enrich_geometry(
                raw_geom,
                timespans=timespans or None,
                geom_key=f"{place_id}_0",
            )
            if not geom_entry:
                self.geom_errors += 1
                return

            if 'hull' in geom_entry:
                hull = geom_entry.get('hull')
                if hull:
                    try:
                        hull_shapely = shape(hull) if isinstance(hull, dict) else hull
                        was_valid = hull_shapely.is_valid
                        if not was_valid:
                            hull_shapely = make_valid(hull_shapely)
                            if not hull_shapely.is_valid:
                                hull_shapely = hull_shapely.buffer(0)
                            if not hull_shapely.is_valid or hull_shapely.is_empty:
                                hull_shapely = hull_shapely.envelope

                        if hull_shapely.is_valid and not hull_shapely.is_empty:
                            fixed_hull = mapping(hull_shapely)
                            if is_antimeridian_risky(fixed_hull):
                                geom_entry.pop('hull', None)
                                self.hull_dropped_antimeridian += 1
                            else:
                                geom_entry['hull'] = fixed_hull
                                if not was_valid:
                                    self.hull_fixed += 1
                                else:
                                    self.hull_kept += 1
                        else:
                            geom_entry.pop('hull', None)
                            self.hull_dropped_invalid += 1
                    except Exception:
                        geom_entry.pop('hull', None)
                        self.hull_dropped_invalid += 1
                else:
                    geom_entry.pop('hull', None)
                    self.hull_dropped_invalid += 1

            update_doc = {
                'geometries': [geom_entry],
                'boundary': tags['boundary_field'],
            }

            upsert_doc = {
                'place_id': place_id,
                'title': tags['name'],
                'toponyms': [{
                    'toponym_id': f"{tags['name']}@und",
                    **({'timespans': timespans} if timespans else {}),
                }],
                'geometries': [geom_entry],
                'boundary': tags['boundary_field'],
                'types': [{
                    'identifier': tags['boundary_field'],
                    'label': self.namespace,
                    'sourceLabel': f"boundary={tags['boundary_field']}",
                }],
            }

            h3_fields = build_h3_fields_for_geom_entry(geom_entry, raw_geom)
            if h3_fields:
                update_doc.update(h3_fields)
                upsert_doc.update(h3_fields)

            self.buffer_callback(place_id, update_doc, upsert_doc)
            self.extracted += 1

            if self.extracted % 1000 == 0:
                elapsed = time.time() - self.start_time
                rate = self.extracted / elapsed if elapsed > 0 else 0
                print(f"\rExtracted {self.extracted:,} boundaries ({rate:.0f}/s)", end='', flush=True)

        except Exception as exc:
            self.geom_errors += 1
            if self.geom_errors <= 5:
                print(f"\n  Geometry error (osmium assembly, relation {area.orig_id()}): {exc}")


class _ProgressReporter:
    """Background thread for progress reports."""

    def __init__(self, processor, interval=30):
        self.processor = processor
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(self.interval):
            p = self.processor
            elapsed = time.time() - p.start_time
            mins = elapsed / 60
            print(
                f"\r  [{mins:.0f}m] areas seen: {p.areas_seen:,}  "
                f"boundaries: {p.extracted:,}  "
                f"geom errors: {p.geom_errors:,}",
                end='', flush=True,
            )

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def _find_osmium():
    """Locate a working ``osmium`` CLI binary."""
    home = os.path.expanduser('~')
    candidates = list(filter(None, [
        shutil.which('osmium'),
        os.path.join(home, '.local', 'bin', 'osmium'),
        os.path.join(home, 'miniconda3', 'bin', 'osmium'),
        os.path.join(home, 'anaconda3', 'bin', 'osmium'),
        '/usr/local/bin/osmium',
        '/usr/bin/osmium',
    ]))

    extra_lib_dirs = [
        d for d in [
            os.path.join(home, 'miniconda3', 'lib'),
            os.path.join(home, 'anaconda3', 'lib'),
            os.path.join(home, '.local', 'lib'),
        ]
        if os.path.isdir(d)
    ]

    env = os.environ.copy()
    if extra_lib_dirs:
        existing = env.get('LD_LIBRARY_PATH', '')
        env['LD_LIBRARY_PATH'] = ':'.join(extra_lib_dirs + ([existing] if existing else []))

    for candidate in candidates:
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        try:
            result = subprocess.run(
                [candidate, '--version'],
                capture_output=True, timeout=10, env=env,
            )
            if result.returncode == 0:
                return candidate, env
        except (OSError, subprocess.TimeoutExpired):
            continue

    return None, None


def prefilter_boundaries(input_pbf, output_pbf):
    """Pre-filter PBF for boundary relations (administrative + curated misc types)."""
    osmium_tool, osmium_env = _find_osmium()
    if not osmium_tool:
        print("  osmium-tool not found — will process full PBF (much slower)")
        return None

    print(f"  Using: {osmium_tool}")
    input_size_gb = os.path.getsize(str(input_pbf)) / 1e9
    print("Pre-filtering PBF for boundary relations...")
    print(f"  Input:  {input_pbf} ({input_size_gb:.1f} GB)")
    start = time.time()

    # 6 h: the 92 GB OSM planet PBF needs ~3-5 h on busy SMP nodes — the
    # earlier 2 h cap timed out and left an empty scratch file that crashed
    # every downstream worker.
    try:
        result = subprocess.run(
            [
                osmium_tool, 'tags-filter',
                str(input_pbf),
                'r/boundary',
                '-o', str(output_pbf),
                '--overwrite',
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=osmium_env,
            timeout=21600,
        )
    except subprocess.TimeoutExpired:
        print("  Pre-filter timed out after 6 hours")
        return None
    except FileNotFoundError:
        print("  osmium command failed to execute")
        return None

    elapsed = time.time() - start
    if result.returncode != 0:
        print(f"  Pre-filter failed (exit code {result.returncode})")
        return None
    if not os.path.exists(str(output_pbf)):
        print("  Pre-filter produced no output")
        return None

    output_size_gb = os.path.getsize(str(output_pbf)) / 1e9
    ratio = input_size_gb / output_size_gb if output_size_gb > 0 else 0
    print(f"  Filtered: {input_size_gb:.1f} GB → {output_size_gb:.1f} GB ({ratio:.0f}× smaller, {elapsed:.0f}s)")
    return str(output_pbf)


def run_boundary_pass(pbf_file, namespace, places_index='places'):
    """Assemble OSM/OHM relation geometry and update the places index."""
    osmium = _require_osmium()
    es = Elasticsearch(ES_HOST, request_timeout=180, max_retries=10, retry_on_timeout=True)

    processor = None

    def signal_handler(sig, frame):
        print("\n!!! SIGNAL RECEIVED !!!")
        if processor:
            print(f"  Extracted so far: {processor.extracted:,}")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    buffer_list = []
    updated_count = 0
    failed_count = 0
    missing_doc_count = 0
    missing_doc_samples = []

    def flush_buffer():
        nonlocal updated_count, failed_count, missing_doc_count
        if not buffer_list:
            return
        for success, info in helpers.parallel_bulk(
            es,
            buffer_list,
            thread_count=BULK_THREAD_COUNT,
            queue_size=QUEUE_SIZE,
            raise_on_error=False,
        ):
            if success:
                updated_count += 1
            else:
                failed_count += 1
                op_info = next(iter(info.values())) if info else {}
                op_error = op_info.get('error', {}) if isinstance(op_info, dict) else {}
                if (
                    isinstance(op_error, dict)
                    and op_info.get('status') == 404
                    and op_error.get('type') == 'document_missing_exception'
                ):
                    missing_doc_count += 1
                    if len(missing_doc_samples) < 10:
                        missing_doc_samples.append(op_info.get('_id', 'unknown'))
                if failed_count <= 10:
                    print(f"\n  Bulk update error: {info}")
        buffer_list.clear()
        if updated_count % 5000 == 0:
            gc.collect()

    def add_to_buffer(place_id, update_doc, upsert_doc):
        buffer_list.append({
            '_op_type': 'update',
            '_index': places_index,
            '_id': place_id,
            'doc': update_doc,
            'upsert': upsert_doc,
            'doc_as_upsert': False,
        })
        if len(buffer_list) >= 500:
            flush_buffer()

    filtered_pbf_path = None

    try:
        source_label = 'OSM' if namespace == 'osm' else 'OHM'
        print(f"\n{'=' * 80}")
        print(f"{source_label} BOUNDARY PASS (geometry assembly + update)")
        print(f"{'=' * 80}")
        print(f"Source: {pbf_file}")
        print(f"Target index: {places_index}")
        print(f"Namespace: {namespace}")
        print()

        try:
            ns_count = es.count(index=places_index, body={'query': {'prefix': {'place_id': f"{namespace}:"}}})['count']
            rel_count = es.count(index=places_index, body={'query': {'prefix': {'place_id': f"{namespace}:r"}}})['count']
            print(f"Preflight counts in '{places_index}': {namespace}:*={ns_count:,}, {namespace}:r*={rel_count:,}")
        except Exception as exc:
            print(f"Preflight count check failed: {exc}")
        print()

        scratch = os.environ.get('SLURM_SCRATCH') or os.environ.get('TMPDIR')
        processing_pbf = str(pbf_file)
        filter_dir = scratch if (scratch and os.path.isdir(scratch)) else os.environ.get('TMPDIR', '/tmp')
        filtered_path = os.path.join(filter_dir, f'{namespace}_boundary_pass_filtered.osm.pbf')
        result = prefilter_boundaries(pbf_file, filtered_path)
        if result:
            processing_pbf = result
            filtered_pbf_path = result
        else:
            print("  Falling back to full PBF (expect long processing time)")
            print()

        processor = BoundaryPassProcessor(add_to_buffer, namespace)

        if scratch and os.path.isdir(scratch):
            node_cache = os.path.join(scratch, f'node_locations_bp_{namespace}.idx')
            idx_type = f'dense_file_array,{node_cache}'
            print(f"Using file-backed node location index: {node_cache}")
        else:
            idx_type = 'flex_mem'
            print("Using in-memory node location index (flex_mem)")

        pbf_size_gb = os.path.getsize(processing_pbf) / 1e9
        print(f"Starting two-pass area assembly on {pbf_size_gb:.1f} GB PBF...")
        fp = osmium.FileProcessor(processing_pbf).with_locations(idx_type).with_areas()

        with _ProgressReporter(processor, interval=30):
            for obj in fp:
                if isinstance(obj, osmium.osm.Area) and not obj.from_way():
                    processor.process_area(obj)

        flush_buffer()

        print(f"\n\n{source_label} boundary pass complete:")
        print(f"  Relation areas seen:  {processor.areas_seen:,}")
        print(f"  Boundaries extracted: {processor.extracted:,}")
        print(f"  Tag-filter rejected:  {processor.tag_rejected:,}")
        print(f"  Documents updated:    {updated_count:,}")
        print(f"  Documents failed:     {failed_count:,}")
        print(f"  Missing docs (404):   {missing_doc_count:,}")
        print(f"\n  Geometry errors (osmium assembly failures, unrecoverable):")
        print(f"    Count:              {processor.geom_errors:,}")
        print(f"\n  WKB geometry repairs (got WKB but Shapely found it invalid):")
        print(f"    Fixed (total):      {processor.raw_geom_fixed:,}")
        print(f"    Fixed by buffer(0): {processor.raw_geom_fixed_by_buffer:,}")
        print(f"    Fixed by hull:      {processor.raw_geom_fixed_by_hull:,}")
        print(f"    Still invalid:      {processor.skipped_invalid:,}")
        print(f"\n  Hull processing:")
        print(f"    Kept:               {processor.hull_kept:,}")
        print(f"    Fixed:              {processor.hull_fixed:,}")
        print(f"    Dropped invalid:    {processor.hull_dropped_invalid:,}")
        print(f"    Dropped anti-mer:   {processor.hull_dropped_antimeridian:,}")
        print(f"\n  Other:")
        print(f"    Skipped (empty):    {processor.skipped_empty:,}")
        if missing_doc_samples:
            print(f"    Missing doc samples: {', '.join(missing_doc_samples)}")

        return updated_count

    finally:
        if filtered_pbf_path and os.path.exists(filtered_pbf_path):
            try:
                os.remove(filtered_pbf_path)
                print(f"  Cleaned up filtered PBF: {filtered_pbf_path}")
            except OSError:
                pass


def main(argv=None):
    """CLI entry point for standalone boundary replay / repair."""
    import argparse

    parser = argparse.ArgumentParser(
        description="OSM/OHM boundary second-pass: assemble full geometry and update places"
    )
    parser.add_argument('--source', choices=['osm', 'ohm'], required=True, help='Which PBF source to process')
    parser.add_argument('--file', help='Override PBF file path')
    parser.add_argument('--places-index', default='places', help='Target places index')
    args = parser.parse_args(argv)

    if args.file:
        pbf_path = Path(args.file)
    elif args.source == 'osm':
        pbf_path = Path(DATA_DIR) / 'authorities' / 'osm' / 'planet-latest.osm.pbf'
    else:
        pbf_path = Path(DATA_DIR) / 'authorities' / 'ohm' / 'planet-latest.osm.pbf'

    if not pbf_path.exists():
        print(f"ERROR: PBF file not found: {pbf_path}")
        raise SystemExit(1)

    run_boundary_pass(pbf_path, namespace=args.source, places_index=args.places_index)


if __name__ == '__main__':
    main()

