# processing/osm-places.py

"""
High-Performance Single-Pass OSM Ingestion.
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

from processing.helpers import enrich_geometry, write_staged_place_doc
from processing.settings import DATA_DIR, OSM_STATE_FILE
from processing.temporal import attested_at

# ---------------- CONFIG ----------------
CHECKPOINT_INTERVAL = 50000
BULK_THREAD_COUNT = 8
QUEUE_SIZE = 12


# ---------------- STATE MANAGEMENT ----------------
class ProgressTracker:
    def __init__(self, state_file):
        self.state_file = state_file
        self.counts = {'node': 0, 'way': 0, 'relation': 0}
        self.targets = {'node': 0, 'way': 0, 'relation': 0}
        self.start_time = time.time()
        self.load_state()

    def load_state(self):
        print(f"Checkpoint file: {self.state_file}")
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.targets = data.get('counts', self.targets)
                    print(f"RESUMING from checkpoint: {self.targets}")
            except Exception as e:
                print(f"Warning: failed to read state file: {e}")
        else:
            print("Starting fresh (no checkpoint file found)")

    def save_state(self):
        temp_file = f"{self.state_file}.tmp"
        with open(temp_file, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'counts': self.counts
            }, f)
        os.replace(temp_file, self.state_file)
        print(f"\nCheckpoint saved: {self.state_file}")

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


# ---------------- ATTESTATION YEAR ----------------
# OSM is a *snapshot*: the planet dump records places as they were on the day
# it was cut. That is an ATTESTATION, not a lifespan — see place#164. This
# used to be a hardcoded `{'start': {'in': 2025}, 'end': {'in': 2025}}`, which
# was wrong twice over: it claimed every OSM place "existed only in 2025" (so
# any historical date filter excluded all 8.86 M of them), and the literal
# 2025 went stale the moment a newer planet was fetched.
#
# Read the year from the dump itself so it can never drift again.
_ATTESTATION_YEAR: int | None = None


def resolve_attestation_year(pbf_path) -> int:
    """Year this planet dump attests, from its replication timestamp.

    Falls back to the file's mtime, then to the current year — a dump with no
    header timestamp is odd but not a reason to abort an ingest.
    """
    try:
        header = osmium.io.Reader(str(pbf_path)).header()
        stamp = header.get("osmosis_replication_timestamp")
        if stamp:
            return int(str(stamp)[:4])
    except Exception as exc:
        print(f"WARN: could not read replication timestamp from {pbf_path}: {exc}")
    try:
        return datetime.fromtimestamp(os.path.getmtime(pbf_path)).year
    except Exception:
        return datetime.now().year


def _attestation_timespans():
    """Timespans asserting 'attested alive in <dump year>'."""
    return attested_at(_ATTESTATION_YEAR)


# ---------------- HELPERS ----------------
def create_doc(osm_id, osm_type, tags, geometry):
    place_id = f"osm:{osm_type[0]}{osm_id}"

    # Build toponyms array with timespans (plural)
    toponyms = [{
        'toponym_id': f"{tags['name']}@und",
        'timespans': _attestation_timespans(),
    }]

    if 'names' in tags:
        for lang, val in tags['names'].items():
            toponyms.append({
                'toponym_id': f"{val}@{lang}",
                'timespans': _attestation_timespans(),
            })

    # Base document
    doc = {
        'place_id': place_id,
        'title': tags['name'],
        'toponyms': toponyms
    }

    # Add geometry as geometries array
    if geometry:
        geom_entry = enrich_geometry(
            geometry,
            timespans=_attestation_timespans(),
            geom_key=f"{place_id}_0",
        )
        if geom_entry:
            doc['geometries'] = [geom_entry]

    # Types
    types = []
    for k in ['place', 'natural', 'water', 'waterway', 'historic', 'landuse', 'boundary']:
        if k in tags:
            types.append({
                'identifier': tags[k],
                'label': 'osm',
                'sourceLabel': f"{k}={tags[k]}"
            })
    if types:
        doc['types'] = types

    # ISO 3166-1 alpha-2 country code straight from the OSM boundary tags, when
    # present (admin_level=2 countries + dependent territories, which carry their
    # own code). OSM borders are topologically noded, so this is authoritative
    # and gap-free where available — preferred over spatial ccode enrichment.
    iso2 = tags.get('ISO3166-1:alpha2') or tags.get('ISO3166-1')
    if isinstance(iso2, str) and len(iso2) == 2 and iso2.isalpha():
        doc['ccodes'] = [iso2.upper()]

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
    elif any(k in tags for k in ['natural', 'water', 'waterway', 'historic', 'landuse', 'boundary']):
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
                       'population', 'elevation', 'wikidata',
                       # ISO 3166-1 country codes carried by admin_level=2
                       # country relations AND dependent-territory relations
                       # (which carry their OWN code, e.g. PR, GU) — the
                       # topologically-noded source we want for accurate ccodes.
                       'ISO3166-1', 'ISO3166-1:alpha2'}:
            result[tag.k] = tag.v

    return result


# ---------------- HANDLER ----------------
class OSMHandler(osmium.SimpleHandler):
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
        self.relation_geom_fallbacks = 0

    def node(self, n):
        if not n.tags: return

        if self.tracker.should_skip('node'): return
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
        if 'name' not in w.tags: return

        if self.tracker.should_skip('way'): return
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
        if 'name' not in r.tags: return

        if self.tracker.should_skip('relation'): return
        self.candidates['relation'] += 1

        tags = process_tags(r.tags)
        if tags:
            # Always index a base relation doc so boundary pass has a target.
            doc = create_doc(r.id, 'relation', tags, None)
            try:
                wkb = self.wkbfab.create_multipolygon(r)
                geom = wkblib.loads(wkb, hex=False)

                if geom.is_valid:
                    geo = mapping(geom)
                    doc = create_doc(r.id, 'relation', tags, geo)
                    self.buffered['relation'] += 1
                else:
                    self.geom_invalid['relation'] += 1
                    self.relation_geom_fallbacks += 1
            except Exception:
                self.geom_errors['relation'] += 1
                self.relation_geom_fallbacks += 1
            self.buffer_callback(doc)
        else:
            self.tag_rejected['relation'] += 1
        self.tracker.increment('relation')


# ---------------- STAGING & MAIN ----------------
def stage_file_to_scratch(source_path, namespace='osm'):
    """Optionally rsync the PBF to local scratch for I/O speed.

    **Off by default.** The osmium nodes pass on OSM/OHM is CPU-bound
    (≈4000 nodes/s on htc), which translates to ~38 KB/s of PBF
    consumption — well below NFS bandwidth. So rsync'ing 86 GB up front
    (~2 h on htc) saves no real time during processing AND has to be
    repeated if the job restarts from checkpoint (scratch is ephemeral).

    To re-enable (e.g. if you observe NFS contention slowing the run),
    export ``WHG_OSM_STAGE_TO_SCRATCH=1`` before submitting.
    """
    if os.environ.get('WHG_OSM_STAGE_TO_SCRATCH', '').lower() not in ('1', 'true', 'yes'):
        print(f"Reading PBF directly from network storage: {source_path}")
        print(f"Staging host: {os.uname().nodename}")
        print(f"SLURM job: {os.environ.get('SLURM_JOB_ID', 'unknown')}")
        return source_path, False

    scratch_dir = os.environ.get('SLURM_SCRATCH')
    if not scratch_dir or not os.path.exists(scratch_dir):
        print("Notice: WHG_OSM_STAGE_TO_SCRATCH set but no scratch dir found; using network storage.")
        return source_path, False

    print(f"Staging host: {os.uname().nodename}")
    print(f"SLURM job: {os.environ.get('SLURM_JOB_ID', 'unknown')}")

    basename = os.path.basename(source_path)
    if namespace:
        basename = f"{namespace}_{basename}"
    target_path = os.path.join(scratch_dir, basename)

    # Check if already staged
    if os.path.exists(target_path):
        print(f"Using existing staged file: {target_path}")
        return target_path, True

    print(f"Staging to local scratch: {target_path}")
    print("Copying source file with rsync progress...")
    subprocess.run([
        'rsync', '-ah', '--info=progress2,stats2', str(source_path), target_path
    ], check=True)
    print(f"Staging complete: {target_path}")
    return target_path, True


def index_osm_optimized(pbf_file):
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    global _ATTESTATION_YEAR
    _ATTESTATION_YEAR = resolve_attestation_year(pbf_file)
    print(f"OSM attestation year (from dump replication timestamp): "
          f"{_ATTESTATION_YEAR}")

    tracker = ProgressTracker(OSM_STATE_FILE)

    # Signal Handling
    def signal_handler(sig, frame):
        print("\n!!! SIGNAL RECEIVED - SAVING STATE !!!")
        tracker.save_state()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Buffer of staged place docs (kept as a list for the same per-50k flush
    # cadence the previous bulk-index path used).
    buffer_list = []
    indexed_count = 0
    failed_count = 0

    def flush_buffer():
        nonlocal indexed_count
        if not buffer_list:
            return
        for action in buffer_list:
            write_staged_place_doc("osm", action['_source'])
            indexed_count += 1
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

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, "osm") as gsw:
        configure_module_writer(gsw)
        try:
            print("=" * 80)
            print("OSM PLACES INGESTION")
            print("=" * 80)
            print(f"Starting Single-Pass Ingestion: {active_pbf}")

            handler = OSMHandler(tracker, add_to_buffer)
            handler.apply_file(str(active_pbf), locations=True, idx='flex_mem')

            flush_buffer()
            tracker.save_state()

            # Boundary completion is the separate ``processing.boundary_stage``
            # Slurm chain — the integrated ES-based path that used to live
            # here is gone (this script no longer talks to ES).

            print(f"\n\nIndexing complete:")
            print(f"  Documents indexed: {indexed_count:,}")
            print(f"  Documents failed: {failed_count:,}")
            print(f"  Geometries in VAST store: {gsw.count:,}")
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
                f"geom_errors={handler.geom_errors['relation']:,}, "
                f"fallbacks={handler.relation_geom_fallbacks:,}"
            )

            print("Ingestion Complete.")

        except Exception as e:
            print(f"\nError: {e}")
            import traceback
            traceback.print_exc()
            tracker.save_state()
            raise
        finally:
            configure_module_writer(None)
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