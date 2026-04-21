# authorities/ohm-places.py

"""
High-Performance Single-Pass OHM (OpenHistoricalMap) Ingestion.

Adapted from osm-places.py. Key differences:
  - Namespace: ohm:{type_char}{id}  (not osm:)
  - Temporal: Parses start_date/end_date tags → timespans
  - Tag keys: Expanded set relevant to historical features
  - Label: type label = 'ohm'  (not 'osm')
"""

import json
import os
import re
import sys
import gc
import time
import signal
import subprocess
from pathlib import Path
from datetime import datetime

import osmium
import shapely.wkb as wkblib
from shapely.geometry import mapping

from elasticsearch import Elasticsearch, helpers
from processing.helpers import enrich_geometry
from processing.settings import ES_HOST, DATA_DIR, OHM_STATE_FILE

# ---------------- CONFIG ----------------
CHECKPOINT_INTERVAL = 50000
BULK_THREAD_COUNT = 8
QUEUE_SIZE = 12

# Tag keys to extract for type classification.
# OHM has much richer use of 'historic' and 'boundary' than current OSM.
TYPE_TAG_KEYS = [
    'place', 'historic', 'boundary', 'natural', 'water',
    'waterway', 'landuse', 'amenity', 'man_made', 'military',
    'building', 'leisure', 'tourism',
]

# Additional tags to capture (not used for type but for metadata)
EXTRA_TAG_KEYS = {'population', 'elevation', 'wikidata', 'admin_level',
                  'start_date', 'end_date'}


# ---------------- DATE PARSING ----------------
# OHM dates: ISO 8601 extended — 1850, 1850-03, 1850-03-15, -0500 (500 BCE),
# sometimes "before:1200", "after:1800", "~1700", "C19" etc.
_YEAR_RE = re.compile(
    r'^~?'                     # optional leading tilde (approximate)
    r'(?:(?:before|after|about|circa|ca)\s*:?\s*)?'  # optional qualifier
    r'(-?\d{1,5})'            # year (may be negative for BCE)
    r'(?:[-/]\d{1,2})?'       # optional month
    r'(?:[-/]\d{1,2})?'       # optional day
    r'(?:T.*)?$',             # optional time
    re.IGNORECASE,
)

_CENTURY_RE = re.compile(
    r'^C(\d{1,2})$',          # e.g. C19 → 1800
    re.IGNORECASE,
)


def parse_ohm_year(date_str):
    """
    Extract an integer year from an OHM date string.

    Returns int year or None if unparseable.
    """
    if not date_str:
        return None
    date_str = date_str.strip()

    # Try direct year extraction
    m = _YEAR_RE.match(date_str)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, OverflowError):
            return None

    # Try century notation: C19 → 1800
    m = _CENTURY_RE.match(date_str)
    if m:
        try:
            return (int(m.group(1)) - 1) * 100
        except ValueError:
            return None

    return None


def build_timespans(tags):
    """
    Build a timespans array from start_date/end_date tags.

    Returns list of timespan dicts, or a default [current] if no dates.
    """
    start_year = parse_ohm_year(tags.get('start_date'))
    end_year = parse_ohm_year(tags.get('end_date'))

    if start_year is not None or end_year is not None:
        ts = {}
        if start_year is not None:
            ts['start'] = {'in': start_year}
        if end_year is not None:
            ts['end'] = {'in': end_year}
        return [ts]

    # Fallback: no temporal info → mark as undated
    return []


# ---------------- STATE MANAGEMENT ----------------
class ProgressTracker:
    def __init__(self, state_file):
        self.state_file = state_file
        self.counts = {'node': 0, 'way': 0, 'relation': 0}
        self.targets = {'node': 0, 'way': 0, 'relation': 0}
        self.start_time = time.time()
        self.load_state()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.targets = data.get('counts', self.targets)
                    print(f"RESUMING from checkpoint: {self.targets}")
            except Exception as e:
                print(f"Warning: failed to read state file: {e}")

    def save_state(self):
        temp_file = f"{self.state_file}.tmp"
        with open(temp_file, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'counts': self.counts
            }, f)
        os.replace(temp_file, self.state_file)

    def should_skip(self, type_):
        # "Fast-forward" logic
        if self.counts[type_] < self.targets[type_]:
            self.counts[type_] += 1
            if self.counts[type_] % 1000000 == 0:
                print(f"\r  Skipping... {type_} {self.counts[type_]:,}/{self.targets[type_]:,}", end='', flush=True)
            return True
        return False

    def increment(self, type_):
        self.counts[type_] += 1
        if self.counts[type_] % CHECKPOINT_INTERVAL == 0:
            self.save_state()
            elapsed = time.time() - self.start_time
            rate = self.counts[type_] / elapsed if elapsed > 0 else 0
            print(f"\rProcessed {self.counts[type_]:,} {type_}s (Rate: {rate:.0f}/s)", end='', flush=True)


# ---------------- HELPERS ----------------
def create_doc(osm_id, osm_type, tags, geometry):
    place_id = f"ohm:{osm_type[0]}{osm_id}"

    timespans = build_timespans(tags)

    # Build toponyms array with timespans
    primary_toponym = {'toponym_id': f"{tags['name']}@und"}
    if timespans:
        primary_toponym['timespans'] = timespans
    toponyms = [primary_toponym]

    if 'names' in tags:
        for lang, val in tags['names'].items():
            entry = {'toponym_id': f"{val}@{lang}"}
            if timespans:
                entry['timespans'] = timespans
            toponyms.append(entry)

    # Base document
    doc = {
        'place_id': place_id,
        'title': tags['name'],
        'toponyms': toponyms
    }

    # Add geometry as geometries array
    if geometry:
        geom_entry = enrich_geometry(geometry, timespans=timespans or None)
        if geom_entry:
            doc['geometries'] = [geom_entry]

    # Types
    types = []
    for k in TYPE_TAG_KEYS:
        if k in tags:
            types.append({
                'identifier': tags[k],
                'label': 'ohm',
                'sourceLabel': f"{k}={tags[k]}"
            })
    if types:
        doc['types'] = types

    # Relations — link to Wikidata if present
    if 'wikidata' in tags:
        doc['relations'] = [{
            'relation_type': 'sameAs',
            'related_place_id': f"wd:{tags['wikidata']}",
            'label': 'Wikidata'
        }]

    # Population
    if 'population' in tags:
        try:
            doc['population'] = int(tags['population'])
        except (ValueError, TypeError):
            pass

    # Elevation
    if 'elevation' in tags:
        try:
            elev_str = tags['elevation'].replace('m', '').strip()
            doc['elevation'] = int(float(elev_str))
        except (ValueError, TypeError):
            pass

    return doc


def process_tags(tags):
    """Filters tags before processing geometry to save CPU."""
    if 'name' not in tags:
        return None

    # Check if we care about this feature — any recognised type tag key
    if not any(k in tags for k in TYPE_TAG_KEYS):
        return None

    # Extract tags
    result = {'name': tags['name']}
    result['names'] = {}

    wanted_keys = set(TYPE_TAG_KEYS) | EXTRA_TAG_KEYS

    for tag in tags:
        if tag.k.startswith('name:'):
            result['names'][tag.k[5:]] = tag.v
        elif tag.k in wanted_keys:
            result[tag.k] = tag.v

    return result


# ---------------- HANDLER ----------------
class OHMHandler(osmium.SimpleHandler):
    def __init__(self, tracker, buffer_callback):
        super().__init__()
        self.tracker = tracker
        self.buffer_callback = buffer_callback
        self.wkbfab = osmium.geom.WKBFactory()
        self.candidates = {'node': 0, 'way': 0, 'relation': 0}
        self.buffered = {'node': 0, 'way': 0, 'relation': 0}
        self.tag_rejected = {'node': 0, 'way': 0, 'relation': 0}
        self.geom_errors = {'way': 0, 'relation': 0}
        self.geom_invalid = {'relation': 0}

    def node(self, n):
        if not n.tags:
            return

        if self.tracker.should_skip('node'):
            return
        self.candidates['node'] += 1
        tags = process_tags(n.tags)
        if tags:
            geo = {'type': 'Point', 'coordinates': [n.location.lon, n.location.lat]}
            self.buffer_callback(create_doc(n.id, 'node', tags, geo))
            self.buffered['node'] += 1
        else:
            self.tag_rejected['node'] += 1
        self.tracker.increment('node')

    def way(self, w):
        if 'name' not in w.tags:
            return

        if self.tracker.should_skip('way'):
            return
        self.candidates['way'] += 1

        tags = process_tags(w.tags)
        if tags:
            try:
                wkb = self.wkbfab.create_linestring(w)
                geom = wkblib.loads(wkb, hex=False)


                geo = mapping(geom)
                self.buffer_callback(create_doc(w.id, 'way', tags, geo))
                self.buffered['way'] += 1
            except Exception:
                self.geom_errors['way'] += 1
        else:
            self.tag_rejected['way'] += 1
        self.tracker.increment('way')

    def relation(self, r):
        if 'name' not in r.tags:
            return

        if self.tracker.should_skip('relation'):
            return
        self.candidates['relation'] += 1

        tags = process_tags(r.tags)
        if tags:
            try:
                wkb = self.wkbfab.create_multipolygon(r)
                geom = wkblib.loads(wkb, hex=False)

                if geom.is_valid:
                    geo = mapping(geom)
                    self.buffer_callback(create_doc(r.id, 'relation', tags, geo))
                    self.buffered['relation'] += 1
                else:
                    self.geom_invalid['relation'] += 1
            except Exception:
                self.geom_errors['relation'] += 1
        else:
            self.tag_rejected['relation'] += 1
        self.tracker.increment('relation')


# ---------------- STAGING & MAIN ----------------
def stage_file_to_scratch(source_path, namespace='ohm'):
    scratch_dir = os.environ.get('SLURM_SCRATCH')
    if not scratch_dir or not os.path.exists(scratch_dir):
        print("Notice: No scratch dir found, using network storage.")
        return source_path, False

    basename = os.path.basename(source_path)
    if namespace:
        basename = f"{namespace}_{basename}"
    target_path = os.path.join(scratch_dir, basename)

    # Check if already staged
    if os.path.exists(target_path):
        print(f"Using existing staged file: {target_path}")
        return target_path, True

    print(f"Staging to local scratch: {target_path}")
    subprocess.run(['rsync', '-ah', str(source_path), target_path], check=True)
    return target_path, True


def index_ohm_optimized(pbf_file):
    es = Elasticsearch(ES_HOST, request_timeout=180, max_retries=10, retry_on_timeout=True)
    tracker = ProgressTracker(OHM_STATE_FILE)

    # Signal Handling
    def signal_handler(sig, frame):
        print("\n!!! SIGNAL RECEIVED - SAVING STATE !!!")
        tracker.save_state()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Parallel Bulk Buffer
    buffer_list = []
    indexed_count = 0
    failed_count = 0

    def flush_buffer():
        nonlocal indexed_count, failed_count
        if not buffer_list:
            return

        # Track results
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
                if failed_count <= 5:  # Show first few errors
                    print(f"\n  Bulk index error: {info}")

        buffer_list.clear()

        if tracker.counts['node'] % 500000 == 0:
            gc.collect()

    def add_to_buffer(doc):
        buffer_list.append({
            '_index': 'places',
            '_id': doc['place_id'],
            '_source': doc
        })
        if len(buffer_list) >= 2000:
            flush_buffer()

    # Staging
    active_pbf, is_staged = stage_file_to_scratch(pbf_file)

    try:
        print("=" * 80)
        print("OHM (OpenHistoricalMap) PLACES INGESTION")
        print("=" * 80)
        print(f"Starting Single-Pass Ingestion: {active_pbf}")

        handler = OHMHandler(tracker, add_to_buffer)
        handler.apply_file(str(active_pbf), locations=True, idx='flex_mem')

        flush_buffer()
        tracker.save_state()

        print(f"\n\nIndexing complete:")
        print(f"  Documents indexed: {indexed_count:,}")
        print(f"  Documents failed: {failed_count:,}")
        print("  Handler diagnostics:")
        print(
            f"    Nodes: candidates={handler.candidates['node']:,}, "
            f"buffered={handler.buffered['node']:,}, rejected={handler.tag_rejected['node']:,}"
        )
        print(
            f"    Ways: candidates={handler.candidates['way']:,}, "
            f"buffered={handler.buffered['way']:,}, rejected={handler.tag_rejected['way']:,}, "
            f"geom_errors={handler.geom_errors['way']:,}"
        )
        print(
            f"    Relations: candidates={handler.candidates['relation']:,}, "
            f"buffered={handler.buffered['relation']:,}, rejected={handler.tag_rejected['relation']:,}, "
            f"geom_invalid={handler.geom_invalid['relation']:,}, "
            f"geom_errors={handler.geom_errors['relation']:,}"
        )

        print("Ingestion Complete.")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        tracker.save_state()
        raise
    finally:
        if is_staged and os.path.exists(active_pbf):
            os.remove(active_pbf)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Index OHM places into Elasticsearch")
    parser.add_argument('--file', help='Path to OHM PBF file')
    args = parser.parse_args()

    ohm_file = args.file
    if not ohm_file:
        ohm_file = Path(DATA_DIR) / 'authorities' / 'ohm' / 'planet-latest.osm.pbf'

    index_ohm_optimized(ohm_file)


