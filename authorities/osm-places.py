# processing/osm-places.py

"""
High-Performance Single-Pass OSM Ingestion.

Features:
- Auto-staging to local scratch (NVMe)
- Inline complexity filtering (Triage)
- Robust resumption state tracking
- Schema-compliant field names (title, geom, repr_point, relation_type, related_place_id)
- Progress indicators with \r for clean output
"""

import json
import os
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
from processing.helpers import compute_representative_point, simplify_geometry
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

# ---------------- CONFIG ----------------
CHECKPOINT_INTERVAL = 50000
BULK_THREAD_COUNT = 8
QUEUE_SIZE = 12

# Complexity Thresholds (Triage)
COMPLEXITY_THRESHOLD_COORDS = 1000
SIMPLIFY_TOLERANCE_DEG = 0.001  # Approx 100m


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
        print(f"  [Checkpoint saved at {self.counts}]")

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
    """
    Creates ES document matching places.json schema.

    Schema field names:
    - title (not label)
    - geom (not locations[].geometry)
    - repr_point (not locations[].rep_point)
    - relation_type (not relationType)
    - related_place_id (not relationTo)
    """
    place_id = f"osm:{osm_type[0]}{osm_id}"

    # Build toponyms array (nested with toponym_id and timespan)
    toponyms = [{'toponym_id': f"{tags['name']}@und", 'timespan': {'start': {'in': 2025}, 'end': {'in': 2025}}}]
    if 'names' in tags:
        for lang, val in tags['names'].items():
            toponyms.append({'toponym_id': f"{val}@{lang}", 'timespan': {'start': {'in': 2025}, 'end': {'in': 2025}}})

    # Base document
    doc = {
        'place_id': place_id,
        'title': tags['name'],
        'toponyms': toponyms,
        'source': 'osm'
    }

    # Add geometry
    if geometry:
        try:
            rep_point = compute_representative_point(geometry)
            doc['geom'] = geometry
            doc['repr_point'] = rep_point
        except:
            pass  # Geometry invalid

    # Types
    types = []
    for k in ['place', 'natural', 'water', 'waterway', 'historic', 'landuse']:
        if k in tags:
            types.append({
                'identifier': tags[k],
                'label': 'osm',
                'sourceLabel': f"{k}={tags[k]}"
            })
    if types:
        doc['types'] = types

    # Relations
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
        except:
            pass

    # Elevation
    if 'elevation' in tags:
        try:
            elev_str = tags['elevation'].replace('m', '').strip()
            doc['elevation'] = int(float(elev_str))
        except:
            pass

    return doc


def process_tags(tags):
    """Filters tags before processing geometry to save CPU."""
    if 'name' not in tags: return None

    # Check if we care about this feature
    if 'place' in tags:
        pass
    elif any(k in tags for k in ['natural', 'water', 'waterway', 'historic', 'landuse']):
        pass
    else:
        return None

    # Extract tags
    result = {'name': tags['name']}
    result['names'] = {}

    for tag in tags:
        if tag.k.startswith('name:'):
            result['names'][tag.k[5:]] = tag.v
        elif tag.k in {'place', 'natural', 'water', 'waterway', 'historic', 'landuse', 'boundary', 'admin_level',
                       'population', 'elevation', 'wikidata'}:
            result[tag.k] = tag.v

    return result


# ---------------- HANDLER ----------------
class OSMHandler(osmium.SimpleHandler):
    def __init__(self, tracker, buffer_callback):
        super().__init__()
        self.tracker = tracker
        self.buffer_callback = buffer_callback
        self.wkbfab = osmium.geom.WKBFactory()

    def node(self, n):
        if not n.tags: return

        if self.tracker.should_skip('node'): return
        tags = process_tags(n.tags)
        if tags:
            geo = {'type': 'Point', 'coordinates': [n.location.lon, n.location.lat]}
            self.buffer_callback(create_doc(n.id, 'node', tags, geo))
        self.tracker.increment('node')

    def way(self, w):
        if 'name' not in w.tags: return

        if self.tracker.should_skip('way'): return

        tags = process_tags(w.tags)
        if tags:
            try:
                wkb = self.wkbfab.create_linestring(w)
                geom = wkblib.loads(wkb, hex=False)

                # INLINE TRIAGE: Check complexity
                if len(geom.coords) > COMPLEXITY_THRESHOLD_COORDS:
                    geom = geom.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)

                geo = mapping(geom)
                self.buffer_callback(create_doc(w.id, 'way', tags, geo))
            except:
                pass
        self.tracker.increment('way')

    def relation(self, r):
        if 'name' not in r.tags: return

        if self.tracker.should_skip('relation'): return

        tags = process_tags(r.tags)
        if tags:
            try:
                wkb = self.wkbfab.create_multipolygon(r)
                geom = wkblib.loads(wkb, hex=False)

                if geom.is_valid:
                    geom = geom.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
                    geo = mapping(geom)
                    self.buffer_callback(create_doc(r.id, 'relation', tags, geo))
            except:
                pass
        self.tracker.increment('relation')


# ---------------- STAGING & MAIN ----------------
def stage_file_to_scratch(source_path):
    scratch_dir = os.environ.get('SLURM_SCRATCH')
    if not scratch_dir or not os.path.exists(scratch_dir):
        print("Notice: No scratch dir found, using network storage.")
        return source_path, False

    target_path = os.path.join(scratch_dir, os.path.basename(source_path))
    print(f"Staging to local scratch: {target_path}")
    subprocess.run(['rsync', '-ah', str(source_path), target_path], check=True)
    return target_path, True


def index_osm_optimized(pbf_file):
    es = Elasticsearch(ES_HOST, request_timeout=180, max_retries=10, retry_on_timeout=True)
    state_file = Path.cwd() / 'osm_state.json'
    tracker = ProgressTracker(str(state_file))

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
        if not buffer_list: return

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
        print(f"Starting Single-Pass Ingestion: {active_pbf}")
        handler = OSMHandler(tracker, add_to_buffer)
        handler.apply_file(str(active_pbf), locations=True, idx='flex_mem')

        flush_buffer()
        tracker.save_state()

        print(f"\n\nIndexing complete:")
        print(f"  Documents indexed: {indexed_count:,}")
        print(f"  Documents failed: {failed_count:,}")

        create_checkpoint_snapshot(es, 'osm_places')
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

    parser = argparse.ArgumentParser()
    parser.add_argument('--file', help='Path to PBF')
    args = parser.parse_args()

    osm_file = args.file
    if not osm_file:
        osm_file = Path(DATA_DIR) / 'authorities' / 'osm' / 'planet-latest.osm.pbf'

    index_osm_optimized(osm_file)