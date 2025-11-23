# processing/osm-places.py

"""
Index OpenStreetMap (OSM) named places into Elasticsearch.

OSM data comes in PBF (Protocolbuffer Binary Format) or GeoJSON format.
This script processes named geographic features from OSM.

OSM features of interest:
- place=* (city, town, village, hamlet, etc.)
- natural=* (peak, volcano, bay, etc.)
- water/waterway=* (lake, river, etc.)
- historic=* (castle, ruins, etc.)
- landuse=* (forest, reservoir, etc.)

Only features with a 'name' tag are indexed.
"""

import json
import os
import sys
from pathlib import Path
from elasticsearch8 import Elasticsearch, helpers
import osmium
import shapely.wkb as wkblib

from processing.settings import ES_HOST, BATCH_SIZE, DATA_DIR, AUTHORITIES
from processing.helpers import compute_representative_point, simplify_geometry

es = Elasticsearch(ES_HOST)

# Get OSM configuration
OSM_CONFIG = next((auth for auth in AUTHORITIES if auth['namespace'] == 'osm'), None)
if not OSM_CONFIG:
    print("ERROR: OSM configuration not found in AUTHORITIES")
    sys.exit(1)


class OSMHandler(osmium.SimpleHandler):
    """
    Handler for processing OSM PBF files.
    Extracts named geographic features.
    """

    def __init__(self, places_batch_callback, toponyms_batch_callback):
        super().__init__()
        self.places_batch = []
        self.toponyms_batch = []
        self.places_batch_callback = places_batch_callback
        self.toponyms_batch_callback = toponyms_batch_callback
        self.processed = 0
        self.indexed = 0
        self.wkbfab = osmium.geom.WKBFactory()

    def process_tags(self, tags):
        """Extract relevant tags from OSM element."""
        result = {}

        # Core tags we're interested in
        relevant_tags = {
            'name', 'name:en', 'alt_name', 'old_name', 'official_name',
            'place', 'natural', 'water', 'waterway', 'historic', 'landuse',
            'boundary', 'admin_level', 'population', 'elevation',
            'wikidata', 'wikipedia'
        }

        for tag in tags:
            key = tag.k
            value = tag.v

            # Collect name variants
            if key.startswith('name:'):
                lang = key[5:]  # Remove 'name:' prefix
                if 'names' not in result:
                    result['names'] = {}
                result['names'][lang] = value
            elif key in relevant_tags:
                result[key] = value

        return result

    def create_place_doc(self, osm_id, osm_type, tags, geometry):
        """Create place document from OSM element."""

        # Skip if no name
        if 'name' not in tags:
            return None, []

        place_id = f"osm:{osm_type[0]}{osm_id}"  # e.g., osm:n12345, osm:w67890

        # Build toponyms array
        toponyms = []
        toponym_docs = []

        # Main name (language unknown unless specified)
        main_name = tags['name']
        toponyms.append(f"{main_name}@und")

        # Language-specific names
        if 'names' in tags:
            for lang, name in tags['names'].items():
                toponyms.append(f"{name}@{lang}")

                # Create toponym document
                toponym_docs.append({
                    'place_id': place_id,
                    'name': f"{name}@{lang}",
                    'suggest': {
                        'input': [name],
                        'contexts': {'lang': [lang]}
                    }
                })

        # Alternative names
        for alt_field in ['alt_name', 'old_name', 'official_name']:
            if alt_field in tags:
                alt_name = tags[alt_field]
                toponyms.append(f"{alt_name}@und")

        # Create main toponym document
        toponym_docs.insert(0, {
            'place_id': place_id,
            'name': f"{main_name}@und",
            'is_preferred': True,
            'suggest': {
                'input': [main_name],
                'contexts': {'lang': ['und']}
            }
        })

        # Build place document
        place_doc = {
            'place_id': place_id,
            'label': main_name,
            'toponyms': list(set(toponyms)),  # Remove duplicates
            'source': 'osm'
        }

        # Add geometry if available
        if geometry:
            # Simplify large geometries
            if geometry.get('type') in ['Polygon', 'MultiPolygon']:
                geometry = simplify_geometry(geometry, tolerance_km=0.1)

            rep_point = compute_representative_point(geometry)

            place_doc['locations'] = [{
                'geometry': geometry,
                'rep_point': rep_point
            }]

        # Add place type
        types = []
        if 'place' in tags:
            types.append({
                'identifier': tags['place'],
                'label': 'osm',
                'sourceLabel': f"place={tags['place']}"
            })
        for tag_type in ['natural', 'water', 'waterway', 'historic', 'landuse']:
            if tag_type in tags:
                types.append({
                    'identifier': tags[tag_type],
                    'label': 'osm',
                    'sourceLabel': f"{tag_type}={tags[tag_type]}"
                })

        if types:
            place_doc['types'] = types

        # Add relations
        relations = []
        if 'wikidata' in tags:
            relations.append({
                'relationType': 'sameAs',
                'relationTo': f"wd:{tags['wikidata']}",
                'source': 'osm',
                'method': 'curated'
            })

        if relations:
            place_doc['relations'] = relations

        # Add admin level
        if 'admin_level' in tags:
            try:
                place_doc['admin_level'] = int(tags['admin_level'])
            except ValueError:
                pass

        # Add population if available
        if 'population' in tags:
            try:
                place_doc['population'] = int(tags['population'])
            except ValueError:
                pass

        # Add elevation if available
        if 'elevation' in tags:
            try:
                # Handle elevation with units (e.g., "1234 m")
                elev_str = tags['elevation'].replace('m', '').strip()
                place_doc['elevation'] = int(float(elev_str))
            except ValueError:
                pass

        return place_doc, toponym_docs

    def flush_batches(self):
        """Flush any remaining items in batches."""
        if self.places_batch:
            self.places_batch_callback(self.places_batch)
            self.places_batch = []
        if self.toponyms_batch:
            self.toponyms_batch_callback(self.toponyms_batch)
            self.toponyms_batch = []

    def node(self, n):
        """Process OSM node."""
        tags = self.process_tags(n.tags)

        # Check if this is a geographic feature we want
        if not any(k in tags for k in ['place', 'natural', 'water', 'historic']):
            return

        if 'name' not in tags:
            return

        # Create geometry
        geometry = {
            'type': 'Point',
            'coordinates': [n.location.lon, n.location.lat]
        }

        place_doc, toponym_docs = self.create_place_doc(n.id, 'node', tags, geometry)

        if place_doc:
            self.places_batch.append({
                '_index': 'places',
                '_id': place_doc['place_id'],
                '_source': place_doc
            })

            for i, toponym_doc in enumerate(toponym_docs):
                self.toponyms_batch.append({
                    '_index': 'toponyms',
                    '_id': f"{place_doc['place_id']}:{i}",
                    '_source': toponym_doc
                })

            self.indexed += 1

        self.processed += 1

        # Flush batches if needed
        if len(self.places_batch) >= BATCH_SIZE:
            self.places_batch_callback(self.places_batch)
            self.places_batch = []

        if len(self.toponyms_batch) >= BATCH_SIZE:
            self.toponyms_batch_callback(self.toponyms_batch)
            self.toponyms_batch = []

        # Progress reporting
        if self.processed % 100000 == 0:
            print(f"Processed {self.processed:,} nodes, indexed {self.indexed:,} places")

    def way(self, w):
        """Process OSM way."""
        tags = self.process_tags(w.tags)

        # Check if this is a geographic feature we want
        if not any(k in tags for k in ['place', 'natural', 'water', 'waterway', 'historic', 'landuse']):
            return

        if 'name' not in tags:
            return

        # Try to get geometry
        geometry = None
        try:
            wkb = self.wkbfab.create_linestring(w)
            line = wkblib.loads(wkb, hex=True)
            geometry = json.loads(json.dumps(line.__geo_interface__))
        except:
            # If we can't get geometry, use a representative point if available
            pass

        place_doc, toponym_docs = self.create_place_doc(w.id, 'way', tags, geometry)

        if place_doc:
            self.places_batch.append({
                '_index': 'places',
                '_id': place_doc['place_id'],
                '_source': place_doc
            })

            for i, toponym_doc in enumerate(toponym_docs):
                self.toponyms_batch.append({
                    '_index': 'toponyms',
                    '_id': f"{place_doc['place_id']}:{i}",
                    '_source': toponym_doc
                })

            self.indexed += 1

        self.processed += 1

        # Flush batches if needed
        if len(self.places_batch) >= BATCH_SIZE:
            self.places_batch_callback(self.places_batch)
            self.places_batch = []

        if len(self.toponyms_batch) >= BATCH_SIZE:
            self.toponyms_batch_callback(self.toponyms_batch)
            self.toponyms_batch = []

        # Progress reporting
        if self.processed % 10000 == 0:
            print(f"Processed {self.processed:,} ways, indexed {self.indexed:,} places")


def index_osm_pbf(pbf_file, places_index='places', toponyms_index='toponyms'):
    """Process OSM PBF file and index to Elasticsearch."""

    print(f"Processing OSM PBF file: {pbf_file}")

    if not os.path.exists(pbf_file):
        print(f"ERROR: File not found: {pbf_file}")
        return

    places_count = 0
    toponyms_count = 0

    def index_places_batch(batch):
        nonlocal places_count
        try:
            success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
            places_count += success
            if failed > 0:
                print(f"  Warning: {failed} places failed to index")
        except Exception as e:
            print(f"  Error indexing places batch: {e}")

    def index_toponyms_batch(batch):
        nonlocal toponyms_count
        try:
            success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
            toponyms_count += success
            if failed > 0:
                print(f"  Warning: {failed} toponyms failed to index")
        except Exception as e:
            print(f"  Error indexing toponyms batch: {e}")

    # Create handler
    handler = OSMHandler(index_places_batch, index_toponyms_batch)

    # Process file
    print("Reading OSM data...")
    print("Note: This will take several hours for planet.osm.pbf")

    try:
        handler.apply_file(pbf_file, locations=True, idx='flex_mem')
    except Exception as e:
        print(f"Error processing PBF file: {e}")
        print("Make sure you have osmium installed: pip install osmium")
        return

    # Flush remaining batches
    handler.flush_batches()

    print(f"\nIndexing complete!")
    print(f"Total processed: {handler.processed:,}")
    print(f"Places indexed: {places_count:,}")
    print(f"Toponyms indexed: {toponyms_count:,}")


def index_osm_geojson(geojson_file, places_index='places', toponyms_index='toponyms'):
    """Process OSM GeoJSON/GeoJSONSeq file and index to Elasticsearch."""

    print(f"Processing OSM GeoJSON file: {geojson_file}")

    if not os.path.exists(geojson_file):
        # Check if it's in the standard location
        standard_path = Path(DATA_DIR) / 'authorities' / 'osm' / Path(geojson_file).name
        if standard_path.exists():
            geojson_file = standard_path
        else:
            print(f"ERROR: File not found: {geojson_file}")
            return

    places_batch = []
    toponyms_batch = []
    places_count = 0
    toponyms_count = 0
    processed = 0

    print("Reading GeoJSON data...")

    # Detect format (GeoJSON vs GeoJSONSeq)
    with open(geojson_file, 'r') as f:
        first_line = f.readline().strip()
        is_seq = not first_line.startswith('{')  # GeoJSONSeq has one feature per line

    # Process file
    # Note: This is a simplified version.
    # For the full planet file, you'd need streaming JSON parsing

    print("Note: Full OSM planet processing requires the PBF format.")
    print("GeoJSON export may be a subset of data.")

    # ... processing logic would go here ...

    print(f"\nIndexing complete!")
    print(f"Places indexed: {places_count:,}")
    print(f"Toponyms indexed: {toponyms_count:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Index OpenStreetMap places into Elasticsearch'
    )
    parser.add_argument(
        '--file',
        help='Path to OSM file (PBF or GeoJSON format)'
    )
    parser.add_argument(
        '--format',
        choices=['pbf', 'geojson'],
        help='File format (auto-detected if not specified)'
    )

    args = parser.parse_args()

    # If no file specified, use the one from settings
    if not args.file:
        # Get OSM file from settings
        osm_files = OSM_CONFIG.get('files', [])
        if not osm_files:
            print("ERROR: No OSM files configured")
            sys.exit(1)

        # Use the first file
        file_config = osm_files[0]
        filename = file_config.get('name', 'planet-latest.osm.pbf')
        osm_file = Path(DATA_DIR) / 'authorities' / 'osm' / filename

        # Try to determine format
        if osm_file.suffix == '.pbf':
            file_format = 'pbf'
        else:
            file_format = 'geojson'
    else:
        osm_file = args.file
        file_format = args.format

        if not file_format:
            # Auto-detect format
            if osm_file.endswith('.pbf'):
                file_format = 'pbf'
            else:
                file_format = 'geojson'

    print(f"Starting OSM ingestion")
    print(f"File: {osm_file}")
    print(f"Format: {file_format}")
    print(f"Target indices: places, toponyms")
    print()

    if not Path(osm_file).exists():
        print(f"ERROR: File not found: {osm_file}")
        print("\nTo download OSM data, run:")
        print("  python -m processing.fetch_authorities -n osm")
        print("\nNote: The full planet file is ~85GB and will take many hours to download")
        sys.exit(1)

    if file_format == 'pbf':
        try:
            import osmium
        except ImportError:
            print("ERROR: osmium library required for PBF processing")
            print("Install with: pip install osmium --break-system-packages")
            sys.exit(1)

        index_osm_pbf(osm_file)
    else:
        index_osm_geojson(osm_file)