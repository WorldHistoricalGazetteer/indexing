# processing/settings.py

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from repository root
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Base paths
IX1_BASE = os.getenv("IX1_BASE", "/ix1/ishi")
IX3_BASE = os.getenv("IX3_BASE", "/vast/ishi")

# Data
DATA_DIR = os.getenv("DATA_DIR", f"{IX1_BASE}/data/authorities")

# Elasticsearch
STAGING_INFO_FILE = os.getenv("STAGING_INFO_FILE", f"{IX1_BASE}/esinfo/es-staging.env")


def get_es_host():
    """Return staging ES URL if a staging instance is running, else None."""
    if os.path.exists(STAGING_INFO_FILE):
        node = port = None
        with open(STAGING_INFO_FILE) as f:
            for line in f:
                if line.startswith("ES_NODE="):
                    node = line.strip().split("=", 1)[1]
                if line.startswith("ES_PORT="):
                    port = line.strip().split("=", 1)[1]
        if node and port:
            return f"http://{node}:{port}"
    return None


ES_HOST = get_es_host()
ES_HOST_PRODUCTION = os.getenv("PROD_ES_URL", "http://localhost:9200")

# Indexing
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5000"))

# Snapshots
STAGING_REPO_NAME = os.getenv("STAGING_REPO_NAME", "staging_repo")
STAGING_SNAPSHOT_DIR = os.getenv("STAGING_SNAPSHOT_DIR", f"{IX1_BASE}/es_snapshots/staging")
BACKUP_REPO_NAME = os.getenv("BACKUP_REPO_NAME", "backup_repo")
BACKUP_SNAPSHOT_DIR = os.getenv("BACKUP_SNAPSHOT_DIR", f"{IX1_BASE}/es_snapshots/backup")

# Index names
PLACES_INDEX = os.getenv("PLACES_INDEX", "places")
TOPONYMS_INDEX = os.getenv("TOPONYMS_INDEX", "toponyms")

# Wikidata processing logs
GEOSHAPE_REFS_FILE = os.path.join(DATA_DIR, "wikidata", "wikidata_geoshape_refs.jsonl")
GEOSHAPE_LOG_FILE = os.path.join(DATA_DIR, "wikidata", "geoshapes_downloaded.log")

# OSM processing state file
OSM_STATE_FILE = f"{IX1_BASE}/elastic/osm_state.json"

# OHM processing state file
OHM_STATE_FILE = f"{IX1_BASE}/elastic/ohm_state.json"


# Remote Dataset Configurations
AUTHORITIES = [
    {  # 2024: 37k+ places
        'dataset_name': 'Pleiades',
        'namespace': 'pl',
        'api_item': 'https://pleiades.stoa.org/places/<id>/json',
        'citation': 'Pleiades: A community-built gazetteer and graph of ancient places. Copyright © Institute for the Study of the Ancient World. Sharing and remixing permitted under terms of the Creative Commons Attribution 3.0 License (cc-by). https://pleiades.stoa.org/',
        'files': [
            {
                'url': 'https://atlantides.org/downloads/pleiades/json/pleiades-places-latest.json.gz',  # 104MB
                'file_type': 'json',
                'item_path': '@graph',
            }
        ],
    },
    {  # 2024: 12m+ places
        'dataset_name': 'GeoNames',
        'namespace': 'gn',
        'api_item': 'http://api.geonames.org/getJSON?formatted=true&geonameId=<id>&username=<username>&style=full',
        'citation': 'GeoNames geographical database. https://www.geonames.org/',
        'files': [
            {
                'url': 'https://download.geonames.org/export/dump/allCountries.zip',  # 405MB
                'fieldnames': [
                    'geonameid', 'name', 'asciiname', 'alternatenames', 'latitude', 'longitude', 'feature_class',
                    'feature_code', 'country_code', 'cc2', 'admin1_code', 'admin2_code', 'admin3_code', 'admin4_code',
                    'population', 'elevation', 'dem', 'timezone', 'modification_date',
                ],
                'file_type': 'csv',
                'delimiter': '\t',
            },
            {
                'url': 'https://download.geonames.org/export/dump/alternateNamesV2.zip',  # 193MB
                'update_place': True,  # Update existing place with alternate names
                'fieldnames': [
                    'alternateNameId', 'geonameid', 'isolanguage', 'alternate_name', 'isPreferredName',
                    'isShortName', 'isColloquial', 'isHistoric', 'from', 'to',
                ],
                'file_type': 'csv',
                'delimiter': '\t',
                'filters': [
                    lambda row: row.get('isolanguage') not in ['post', 'link', 'iata', 'icao', 'faac', 'tcid', 'unlc',
                                                               'abbr'],
                ]
            },
        ],
    },
    {  # 2024: 3m+ places
        'dataset_name': 'TGN',
        'namespace': 'tgn',
        'api_item': 'https://vocab.getty.edu/tgn/<id>.jsonld',
        'citation': 'The Getty Thesaurus of Geographic Names® (TGN) is provided by the J. Paul Getty Trust under the Open Data Commons Attribution License (ODC-By) 1.0. https://www.getty.edu/research/tools/vocabularies/tgn/',
        'files': [
            {
                'url': 'http://tgndownloads.getty.edu/VocabData/explicit.zip',
                'file_type': 'nt',
                'filters': [
                    # At least one of the `identified_by` list items must have "type": "crm:E47_Spatial_Coordinates"
                    lambda doc: any(
                        'crm:E47_Spatial_Coordinates' in identified_by.get('type', [])
                        for identified_by in doc.get('identified_by', [])
                    )
                ],
            },
        ],
    },
    {  # 2024: 8m+ items classified as places with geometry
        'dataset_name': 'Wikidata',
        'namespace': 'wd',
        'api_item': 'https://www.wikidata.org/wiki/Special:EntityData/<id>.json',
        'citation': 'Wikidata is a free and open knowledge base that can be read and edited by both humans and machines. https://www.wikidata.org/',
        'files': [
            {
                'url': 'https://dumps.wikimedia.org/wikidatawiki/entities/latest-all.json.gz',  # 148GB
                'file_type': 'wikidata',
                'item_count': 120_000_000,  # Approximate number of entities in the dump
                'filters': [
                    lambda doc: 'claims' in doc and 'P625' in doc['claims'],
                    # Filter to only include items with coordinates
                ]
            },
        ],
    },
    {  # 2024: >14.8m named places with multiple toponyms (file includes some unnamed features)
        'dataset_name': 'OSM',
        'namespace': 'osm',
        'api_item': 'https://nominatim.openstreetmap.org/details.php?osmtype=R&osmid=<id>&format=json',
        'citation': 'OpenStreetMap is open data, licensed under the Open Data Commons Open Database License (ODbL). https://www.openstreetmap.org/',
        'files': [
            {
                'url': 'https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf',  # 84.7GB
                'file_type': 'geojsonseq',  # GeoJSON Sequence with Record Separators
                'filters': [
                    lambda feature: 'name' in (properties := feature['properties']) and
                                    any(key in properties for key in
                                        ['geological', 'historic', 'place', 'water', 'waterway']),
                    # Filter to only include named features
                ]
            }
        ],
    },
    {  # ~800K+ historical places with temporal coverage
        'dataset_name': 'OHM',
        'namespace': 'ohm',
        'api_item': '',
        'citation': 'OpenHistoricalMap is open data, licensed under the Open Data Commons Open Database License (ODbL). https://www.openhistoricalmap.org/',
        'files': [
            {
                # OHM has no planet-latest symlink; daily dumps use dated names
                # e.g. planet/planet-260406_0302.osm.pbf
                # The fetch script resolves the latest via S3 bucket listing
                'url': 'OHM_PLANET_LATEST',
                'name': 'planet-latest.osm.pbf',
                'file_type': 'pbf',
            }
        ],
    },
    {  # Not useful as source of places or toponyms, but can provide links
        'dataset_name': 'LOC',
        'namespace': 'loc',
        'api_item': 'https://www.loc.gov/item/<id>/',
        'citation': 'Library of Congress. https://www.loc.gov/',
        'files': [
            {
                'url': 'http://id.loc.gov/download/authorities/names.madsrdf.jsonld.gz',
                'file_type': 'ndjson',  # Newline-delimited JSON
                'filters': [
                    lambda record: any(
                        "madsrdf:GeographicElement" in graph_item.get("@type", [])
                        for graph_item in record.get("@graph", [])
                    ) and any(
                        "madsrdf:identifiesRWO" in graph_item
                        or
                        "madsrdf:hasCloseExternalAuthority" in graph_item
                        or
                        (
                                "madsrdf:hasExactExternalAuthority" in graph_item
                                and (
                                        isinstance(graph_item["madsrdf:hasExactExternalAuthority"], list)
                                        or
                                        (
                                                isinstance(graph_item["madsrdf:hasExactExternalAuthority"], dict)
                                                and
                                                not graph_item["madsrdf:hasExactExternalAuthority"].get("@id",
                                                                                                        "").startswith(
                                                    "http://viaf.org/")
                                        )
                                )
                        )
                        for graph_item in record.get("@graph", [])
                    )
                ]
            }
        ],
    },
    {
        'dataset_name': 'NativeLand',
        'namespace': 'nl',
        'api_item': '',
        'citation': 'Native Land Digital. https://native-land.ca/',
        'files': [
            # Use Native Land API Key from https://native-land.ca/dashboard: append to url as ?key=YOUR_API_KEY
            {
                'url': f'https://native-land.ca/api/polygons/geojson/territories',
                'name': 'territories.json',
                'file_type': 'json',
                'item_path': 'features',
            },
            {
                'url': f'https://native-land.ca/api/polygons/geojson/languages',
                'name': 'languages.json',
                'file_type': 'json',
                'item_path': 'features',
            },
            {
                'url': f'https://native-land.ca/api/polygons/geojson/treaties',
                'name': 'treaties.json',
                'file_type': 'json',
                'item_path': 'features',
            }
        ],
    },
    {
        'dataset_name': 'DPlace',
        'namespace': 'dp',
        'api_item': '',
        'citation': 'D-PLACE: A Global Database of Cultural, Linguistic and Environmental Diversity. https://d-place.org/',
        'files': [
            {
                'url': f'https://d-place.org/languages.geojson',
                'file_type': 'json',
                'item_path': 'features',
            },
        ],
    },
    {
        'dataset_name': 'GB1900',
        'namespace': 'gb',
        'api_item': '',
        'citation': 'GB1900 Gazetteer: British place names, 1888-1914. https://www.pastplace.org/data/#tabgb1900',
        'files': [
            {
                'url': 'https://www.pastplace.org/downloads/GB1900_gazetteer_abridged_july_2018.zip',
                'file_type': 'csv',
                'delimiter': ',',
            }
        ],
    },
    {  # 24,000 place names
        'dataset_name': 'IndexVillaris',
        'namespace': 'iv',
        'api_item': '',
        'citation': 'Index Villaris, 1680',
        'files': [
            {
                'url': 'https://github.com/docuracy/IndexVillaris1680/raw/refs/heads/main/docs/data/IV-GB1900-OSM-WD.lp.json',
                'file_type': 'json',
            }
        ],
    },
    {  # ISO Countries
        'dataset_name': 'ISO3166',
        'namespace': 'un',  # United Nations countries and territories
        'api_item': '',
        'citation': 'Natural Earth Data. Public domain. https://www.naturalearthdata.com/',
        'files': [
            {
                'url': 'https://datahub.io/core/geo-countries/_r/-/data/countries.geojson',
                # Or maybe "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
                'file_type': 'json',
                'item_path': 'features',
            }
        ],
    },
    {  # PeriodO temporal periods with spatial coverage
        'dataset_name': 'PeriodO',
        'namespace': 'po',
        'api_item': '',
        'citation': 'PeriodO: A public domain gazetteer of historical, art-historical, and archaeological periods. https://perio.do/',
        'files': [
            {
                'url': 'https://data.perio.do/d.json',
                'name': 'p0d.json',
                'file_type': 'json',
            }
        ],
    },
    {  # Cliopatria historical polity boundaries
        'dataset_name': 'Cliopatria',
        'namespace': 'clio',
        'api_item': '',
        'citation': 'Cliopatria: Historical polity boundaries from the Seshat Global History Databank. https://github.com/Seshat-Global-History-Databank/cliopatria',
        'files': [
            {
                'url': 'https://github.com/Seshat-Global-History-Databank/cliopatria/raw/main/cliopatria.geojson.zip',
                'name': 'cliopatria.geojson.zip',
                'file_type': 'json',
                'item_path': 'features',
            }
        ],
    },
    {  # ~24K ancient/historical places with coordinates
        'dataset_name': 'Trismegistos',
        'namespace': 'tm',
        'api_item': 'https://www.trismegistos.org/place/<id>',
        'citation': 'Trismegistos: An interdisciplinary portal of the ancient world. https://www.trismegistos.org/',
        'files': [
            {
                # Built locally by authorities/trismegistos/build_database.py
                # from TM_geo.sql + TM GeoRelations API
                'url': '',
                'name': 'tm_geo.db',
                'file_type': 'sqlite',
            }
        ],
    },
]
