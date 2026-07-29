# processing/settings.py

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env (shared defaults, committed) and then .env.local (per-host
# overrides + secrets, gitignored) with override=True so .env.local wins.
# Mirrors the pattern in clustering/config.py — host-specific paths like
# TILESERVER_SSH_KEY (only valid on CRC) and PG_DB_PASSWORD belong in
# .env.local; .env is for shared, syncable defaults that are safe to
# commit.
_repo_root = Path(__file__).parent.parent
load_dotenv(_repo_root / ".env")
load_dotenv(_repo_root / ".env.local", override=True)

# Base paths
IX1_BASE = os.getenv("IX1_BASE", "/ix1/ishi")
IX3_BASE = os.getenv("IX3_BASE", "/vast/ishi")

# Data
DATA_DIR = os.getenv("DATA_DIR", f"{IX1_BASE}/data/authorities")

# Elasticsearch
STAGING_INFO_FILE = os.getenv("STAGING_INFO_FILE", f"{IX1_BASE}/esinfo/es-staging.env")


def get_es_host():
    """Return ES URL from environment, staging info file, or None."""
    # Check explicit environment variable first (set by sbatch scripts)
    env_host = os.getenv("ES_HOST")
    if env_host:
        return env_host
    # Fall back to staging info file
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

# Production types-index lookup (Batch 2 preflight)
TYPES_ES_HOST = os.getenv("TYPES_ES_HOST", ES_HOST_PRODUCTION)
TYPES_INDEX = os.getenv("TYPES_INDEX", "types")
TYPES_ES_USER = os.getenv("TYPES_ES_USER", "")
TYPES_ES_PASSWORD = os.getenv("TYPES_ES_PASSWORD", "")

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
# Wikidata → Getty AAT crosswalk (property P1014). Emitted as a side-output of the
# place-ingest dump scan (authorities/wikidata-places.py) and/or a standalone scan
# (typesystem/extract_wikidata_p1014.py); consumed by typesystem.aat_mapper wikidata.
# UN BNDA country boundaries (authoritative ISO 3166-1 alpha-2 country polygons)
# — the ccode reference used by processing.ccode_enrichment.UnCountryIndex.
# Committed to the repo (small, ~2.4 MB); see the companion README for provenance.
UN_BNDA_COUNTRIES_FILE = os.path.join(
    os.path.dirname(__file__), "data", "un_bnda_countries.geojson"
)

WIKIDATA_P1014_FILE = os.path.join(DATA_DIR, "wikidata", "wikidata_p1014.jsonl")
# Wikidata P279 (subclass-of) class graph. Standalone scan
# (typesystem/extract_wikidata_p279.py); consumed by typesystem.aat_mapper
# wikidata-p279 (Pass 2 — walk to nearest P1014-mapped ancestor).
WIKIDATA_P279_FILE = os.path.join(DATA_DIR, "wikidata", "wikidata_p279.jsonl")

# OSM/OHM processing state files — on /vast (IX3_BASE) with the clone, since the
# runtime relocated off /ix1 (2026-07-15). Override via env if needed.
OSM_STATE_FILE = os.getenv("OSM_STATE_FILE", f"{IX3_BASE}/elastic/osm_state.json")

# OHM processing state file
OHM_STATE_FILE = os.getenv("OHM_STATE_FILE", f"{IX3_BASE}/elastic/ohm_state.json")

# External geometry store (VAST filesystem)
# Full GeoJSON geometries are stored here instead of in Elasticsearch.
# GEOM_STORE_DIR       — consolidated shard files + index.json (read path)
# GEOM_STORE_STAGING_DIR — per-authority staging files written during ingestion
GEOM_STORE_DIR = os.getenv("GEOM_STORE_DIR", f"{IX3_BASE}/geom")
GEOM_STORE_STAGING_DIR = os.getenv("GEOM_STORE_STAGING_DIR", f"{IX3_BASE}/geom/staging")

# Staged ingestion artefacts (Batch 1 foundation)
# Canonical staging root and authority-selection control file.
REPO_ROOT = Path(__file__).resolve().parent.parent
STAGED_BASE_DIR = os.getenv("STAGED_BASE_DIR", f"{IX3_BASE}/staged")
STAGED_MANIFEST_FILENAME = os.getenv("STAGED_MANIFEST_FILENAME", "manifest.json")
AUTHORITY_SELECTION_FILE = os.getenv(
    "AUTHORITY_SELECTION_FILE",
    str(REPO_ROOT / "authority-selection.md"),
)

# Namespaced staged layout templates (formatted with namespace, stage)
STAGED_NAMESPACE_DIR_TEMPLATE = os.getenv(
    "STAGED_NAMESPACE_DIR_TEMPLATE",
    "{base}/{namespace}",
)
STAGED_STAGE_DIR_TEMPLATE = os.getenv(
    "STAGED_STAGE_DIR_TEMPLATE",
    "{base}/{namespace}/{stage}",
)
STAGED_RUNS_DIR = os.getenv("STAGED_RUNS_DIR", f"{STAGED_BASE_DIR}/runs")
STAGED_RUN_MANIFEST_FILE_TEMPLATE = os.getenv(
    "STAGED_RUN_MANIFEST_FILE_TEMPLATE",
    "{runs_dir}/{run_id}.json",
)

# Global gazetteers — coverage assumed worldwide; per-gazetteer H3 coverage
# compaction in Batch 6 is skipped for these and the inventory entry carries the
# sentinel "global" instead of an enumerated cell list (Master Plan §1.4.1).
# Re-exported here from staging_contract so authority scripts and orchestration
# code share a single value; do not hard-code elsewhere.
from processing.staging_contract import (  # noqa: E402
    GLOBAL_COVERAGE_NAMESPACES,
    H3_COVERAGE_GLOBAL_SENTINEL,
    RELATIONS_ONLY_NAMESPACES,
)

# Persistent cross-run per-namespace wall-time history.
# Used by Slurm array submission to estimate --time allocations from prior runs.
# Structure: {namespace: {script_id: [{run_id, started_at, finished_at,
#             wall_seconds, status, slurm_job_id?}]}}
NAMESPACE_RUNTIME_HISTORY_FILE = os.getenv(
    "NAMESPACE_RUNTIME_HISTORY_FILE",
    f"{STAGED_BASE_DIR}/namespace-runtime-history.json",
)

# ---------------------------------------------------------------------------
# Runtime Prerequisites (plan §"Runtime Prerequisites")
# ---------------------------------------------------------------------------

# WHG API base URL used by Batch 4 Phase 4 (whg-places.py) discovery / LPF
# fetch and by Batch 11 push_gazetteer_inventory.py for the gazetteer
# registry endpoint. Override per environment via env var.
WHG_API_BASE_URL = os.getenv("WHG_API_BASE_URL", "https://whgazetteer.org")

# Gazetteer-registry endpoint relative to WHG_API_BASE_URL. Used by
# push_gazetteer_inventory.py — overrideable via WHG_INVENTORY_ENDPOINT
# (full URL takes precedence over base + path composition).
WHG_INVENTORY_ENDPOINT = os.getenv(
    "WHG_INVENTORY_ENDPOINT",
    f"{WHG_API_BASE_URL.rstrip('/')}/api/registry/inventory",
)

# Dev-server inventory endpoint. The dev WHG runs its own Postgres, so
# every successful inventory push to prod is also mirrored here when the
# dev server is reachable. A failed reachability preflight or a failed
# push to dev does NOT abort the script — prod is the source of truth.
WHG_DEV_API_BASE_URL = os.getenv(
    "WHG_DEV_API_BASE_URL", "https://dev.whgazetteer.org",
)
WHG_DEV_INVENTORY_ENDPOINT = os.getenv(
    "WHG_DEV_INVENTORY_ENDPOINT",
    f"{WHG_DEV_API_BASE_URL.rstrip('/')}/api/registry/inventory",
)

# Token for authenticated WHG API calls. Read at request time; set via the
# environment or a sidecar file (WHG_API_TOKEN_FILE) — never commit literals.
WHG_API_TOKEN_FILE = os.getenv(
    "WHG_API_TOKEN_FILE",
    f"{IX1_BASE}/secrets/whg-api.token",
)
# Optional separate token for the dev server — falls back to WHG_API_TOKEN_FILE
# when this file doesn't exist (most deployments share the secret).
WHG_DEV_API_TOKEN_FILE = os.getenv(
    "WHG_DEV_API_TOKEN_FILE",
    f"{IX1_BASE}/secrets/whg-dev-api.token",
)
# Token may also be supplied directly via the environment (e.g. WHG_API_TOKEN in
# the gitignored .env.local) — preferred over the token file when set. NEVER put
# this in the tracked .env. WHG_DEV_API_TOKEN falls back to WHG_API_TOKEN.
WHG_API_TOKEN = os.getenv("WHG_API_TOKEN")
WHG_DEV_API_TOKEN = os.getenv("WHG_DEV_API_TOKEN")

# HTTP retry/backoff defaults (used by push_gazetteer_inventory + future
# whg-places.py discovery/fetch). Single source of truth so Slurm jobs and
# manual invocations behave identically.
WHG_HTTP_TIMEOUT = int(os.getenv("WHG_HTTP_TIMEOUT", "60"))
WHG_HTTP_MAX_RETRIES = int(os.getenv("WHG_HTTP_MAX_RETRIES", "4"))
WHG_HTTP_INITIAL_BACKOFF = float(os.getenv("WHG_HTTP_INITIAL_BACKOFF", "2.0"))

# Pitt-side filesystem path that hosts the live SQLite hard-link database.
# `processing/submit_hardlinks_slurm.py` ships into this directory; the
# gateway opens `<filename>` from this directory read-only at search time.
PITT_HARDLINK_DIR = os.getenv("PITT_HARDLINK_DIR", "/ix1/ishi/hardlinks")
PITT_HARDLINK_FILENAME = os.getenv("PITT_HARDLINK_FILENAME", "hard_links.sqlite")
PITT_HARDLINK_REMOTE_USER = os.getenv("PITT_HARDLINK_REMOTE_USER", "")
PITT_HARDLINK_REMOTE_HOST = os.getenv("PITT_HARDLINK_REMOTE_HOST", "")

# Persistent Symphonym embedding cache (Batch 9). Keyed on
# (toponym_id, model_version, checkpoint_hash), so a model-version bump or
# a checkpoint-file change automatically invalidates every prior entry. The
# cache file is shared across runs; ``phonetics/inference/update_es.py
# compute`` populates it on the first run and reads from it on subsequent
# runs to skip GPU work for unchanged toponyms.
#
# Lives on /vast (IX3_BASE) to avoid the /ix1 NFS contention that makes
# every-batch appends a throughput bottleneck. The May-2026 rebuild's
# 28h-on-/ix1 estimate was dominated by /ix1 latency; relocating to NVMe
# fast storage takes batch-flush time from seconds to sub-millisecond.
SYMPHONYM_CACHE_DB = os.getenv(
    "SYMPHONYM_CACHE_DB",
    f"{IX3_BASE}/models/phonetic/symphonym_cache.duckdb",
)

# Embeddings parquet output (Batch 9 → Batch 11 handoff). Same /vast
# rationale as SYMPHONYM_CACHE_DB: written sequentially during compute,
# read once by the index step, then can be archived to /ix1 if desired.
SYMPHONYM_EMBEDDINGS_DIR = os.getenv(
    "SYMPHONYM_EMBEDDINGS_DIR",
    f"{IX3_BASE}/models/phonetic/data",
)


# ---------------------------------------------------------------------------
# Tileserver deployment (Batch 10 final step)
#
# .mbtiles produced on /ix1 are pushed to the TileServer GL host. CRC compute
# nodes don't have an SSH key for the tileserver, so the push is routed via
# the Pitt VM (``TILESERVER_PROXY``) which does. The compute task SSHs to
# the proxy, which then SCPs the file from /ix1 (shared NFS mount) to the
# tileserver. Each per-bucket tile-gen task triggers its own push on
# success — restart of the tileserver service is intentionally NOT
# automatic; it's gated on ALL tilesets being deployed and verified.
# ---------------------------------------------------------------------------
TILESERVER_PROXY        = os.getenv("TILESERVER_PROXY",        "pitt")
TILESERVER_HOST         = os.getenv("TILESERVER_HOST",         "134.209.177.234")
TILESERVER_USER         = os.getenv("TILESERVER_USER",         "whgadmin")
TILESERVER_TILES_DIR    = os.getenv("TILESERVER_TILES_DIR",    "/srv/tileserver/tiles")
# Optional direct-mode SSH key. When set, ``push_mbtiles_to_tileserver``
# bypasses the proxy hop entirely and runs rsync from the local host
# straight to ``TILESERVER_USER@TILESERVER_HOST`` using this key. CRC
# compute nodes can reach the tileserver on port 22 but lack the ``pitt``
# alias, so the canonical CRC ``.env`` points this at a 0600-mode copy of
# stg135's id_ed25519 on /vast/ishi/secrets. Leave unset on the user's
# local box so the proxy path stays default there.
TILESERVER_SSH_KEY      = os.getenv("TILESERVER_SSH_KEY",      "")
# Absolute path to rsync ON THE PROXY (Pitt VM). rsync isn't on stg135's
# PATH there but is in the gazetteer/whg conda env, so we invoke it by
# absolute path. Set to empty to force scp fallback.
TILESERVER_PROXY_RSYNC  = os.getenv(
    "TILESERVER_PROXY_RSYNC",
    "/home/gazetteer/miniconda/envs/whg/bin/rsync",
)
# systemctl unit names (space-separated for env-var convenience). Both
# ``tiler.service`` and ``tileserver-gl-light.service`` need restarting
# after a fresh batch of mbtiles arrives.
TILESERVER_SERVICES     = os.getenv(
    "TILESERVER_SERVICES",
    "tiler.service tileserver-gl-light.service",
).split()


# ── Authority download / redistribution gating ─────────────────────────────
# Whether WHG may offer an authority as a **downloadable copy** (re-hosting /
# redistribution — distinct from indexing it for search & reconciliation, which
# every authority permits) is governed by TWO factors, combined into the
# ``downloadable`` flag the inventory push sends to the registry
# (``processing.push_gazetteer_inventory._download_fields``):
#
#   1. LEGALITY — the per-authority ``redistributable`` key below. Licence
#      permission is often implicit in ``license_spdx`` (CC0/CC-BY/CC-BY-SA/CC-BY-NC
#      and ODbL/ODC-By all allow redistribution — NC only non-commercially; UKDS
#      End User Licence / bespoke academic or data-sovereignty terms do not), but
#      it is recorded EXPLICITLY per authority so the determination is auditable
#      rather than inferred. Every authority sets it (unset ⇒ True downstream as a
#      safety default). Audited 2026-07-22: the only ``False`` cases are
#      ``chgis`` (Harvard academic — no redistribution), ``nl`` (Native Land
#      data-sovereignty — redistribution by permission) and ``kain_par`` (UKDS
#      EULA). ``dgsd`` is CC-BY-ND but True because its PI endorses WHG re-use.
#   2. VOLUME — a guard so a legally-redistributable but very large authority is
#      not offered as a bulk download that could overburden the server. An
#      authority whose ``record_count`` exceeds ``DOWNLOAD_MAX_RECORDS`` is not
#      marked downloadable regardless of licence (the mega crawled sources —
#      gn/osm/wd/ohm/gb — are excluded this way; curated historical gazetteers
#      sit well under the cap). Coarse by design (record count, not bytes);
#      raise/refine if a genuine need appears.
DOWNLOAD_MAX_RECORDS = 250_000


# ── Custom (non-SPDX) licence vocabulary ───────────────────────────────────
# Several authorities publish under bespoke terms with no SPDX identifier. Those
# are recorded against a ``custom-*`` id in ``license_spdx`` below, and DEFINED
# here — this dict is the source of truth for what each id means.
#
# WHY THIS EXISTS (place#157). The registry endpoint *skips and logs* a
# ``license_spdx`` its own License table doesn't know, leaving the row with NO
# licence at all. Silently. On 2026-07-29 that had left five authorities
# (chgis, kain_par, nl, dgsd, ukhc) rendering as "licence not specified" in the
# Atlas even though this file recorded terms for four of them — and had masked a
# WRONG value for a sixth: ``un`` still carried ``custom-public-domain``, a
# leftover from the Natural Earth era, while the source has been UN Geospatial
# BNDA (© United Nations, no public-domain grant) since 2026-07-14.
#
# Unrecorded terms are worse than restrictive ones: a non-commercial licence can
# be cleared or excluded deterministically; an absent one cannot be reasoned
# about at all. So: every custom id used below MUST appear here, the WHG License
# table must be seeded from these definitions, and
# ``processing.verify_licences`` re-reads the live registry after a push to
# prove each authority actually resolved (turning the silent skip into a loud
# failure).
#
# Field semantics match the registry's License model / the API's `license`
# object: permits_commercial, share_alike, attribution_required, no_derivatives
# are the compliance flags a programmatic consumer acts on; `custom` marks a
# non-SPDX row. `redistribution` is prose only — WHG's own re-hosting decision
# is the per-authority `redistributable` flag above.
CUSTOM_LICENCES = {
    'custom-nativeland-dst': {
        'label': 'Native Land Digital Data Sovereignty Treaty',
        'url': 'https://api-docs.native-land.ca/data-sovereignty-treaty',
        'permits_commercial': False,
        'share_alike': False,
        'attribution_required': True,
        'no_derivatives': False,
        'custom': True,
        'notes': ('OCAP®-aligned bespoke terms. Non-commercial use only; '
                  'redistribution by explicit permission; attribution plus '
                  'acknowledgement of Indigenous communities as the rightful '
                  'stewards of the data is mandatory.'),
    },
    'custom-historic-counties': {
        'label': 'Historic County Borders Project terms',
        'url': 'https://county-borders.co.uk/',
        'permits_commercial': True,
        'share_alike': False,
        # The project "would appreciate" credit — requested, not a condition of
        # use. Recorded truthfully as False; WHG attributes regardless.
        'attribution_required': False,
        'no_derivatives': False,
        'custom': True,
        'notes': ('Free for personal, educational, non-commercial AND commercial '
                  'use. Attribution requested but not required.'),
    },
    'custom-chgis-academic': {
        'label': 'CHGIS academic-use-only terms (Harvard / Fudan)',
        'url': 'https://chgis.fas.harvard.edu/data/chgis/v6/',
        'permits_commercial': False,
        'share_alike': False,
        'attribution_required': True,
        'no_derivatives': False,
        'custom': True,
        'notes': ('Academic research only — no commercial use, no resale, and no '
                  'redistribution (stricter than any CC licence). Indexing for '
                  'search/reconciliation only; re-hosting would need direct '
                  'permission from the rights holders.'),
    },
    'custom-ukds-eul': {
        'label': 'UK Data Service End User Licence',
        'url': 'https://ukdataservice.ac.uk/app/uploads/cd137-enduserlicence.pdf',
        'permits_commercial': False,
        'share_alike': False,
        'attribution_required': True,
        'no_derivatives': False,
        'custom': True,
        'notes': ('Registration-gated bespoke EULA. Use for research/teaching '
                  'only, no commercial use, no onward distribution — the data is '
                  'indexed in place and never offered for download. Each user '
                  'must obtain their own copy from the UK Data Service.'),
    },
    'custom-un-geodata': {
        'label': 'UN Geospatial Data terms',
        'url': 'https://www.un.org/geospatial/',
        # The UN publishes BNDA with NO explicit grant of rights — the item's
        # own licenseInfo (checked 2026-07-29 via the ArcGIS item metadata) is
        # purely the boundary-designation disclaimer. Commercial permission is
        # therefore NOT established; recorded as unknown (None), not as True.
        'permits_commercial': None,
        'share_alike': False,
        'attribution_required': True,
        'no_derivatives': None,
        'custom': True,
        'notes': ('No explicit licence grant from the UN. Attribution to the '
                  'United Nations is required and the boundary/designation '
                  'disclaimer must accompany any use. NOT public domain — the '
                  'registry\'s former "custom-public-domain" value was inherited '
                  'from the retired Natural Earth source.'),
    },
}


# Remote Dataset Configurations
AUTHORITIES = [
    {  # 2024: 37k+ places
        'dataset_name': 'Pleiades',
        'namespace': 'pl',
        'redistributable': True,  # CC-BY-3.0
        # Structured attribution (verified 2026-06-06 against pleiades.stoa.org/credits).
        'citation_text': 'Pleiades: A Gazetteer of Past Places, edited by the Institute for the Study of the Ancient World and the Ancient World Mapping Center.',
        'license_spdx': 'CC-BY-3.0',  # still 3.0 (confirmed — NOT 4.0)
        'license_url': 'https://creativecommons.org/licenses/by/3.0/',
        'rights_holder': 'Institute for the Study of the Ancient World (NYU) & Ancient World Mapping Center (UNC Chapel Hill)',
        'source_url': 'https://pleiades.stoa.org/',
        'contributors': [],
        'api_item': 'https://pleiades.stoa.org/places/<id>/json',
        'web_item': 'https://pleiades.stoa.org/places/<id>',  # place#121: source human page
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
        'redistributable': True,  # CC-BY-4.0
        # Verified 2026-06-06 against geonames.org/about.html.
        'citation_text': 'GeoNames geographical database, Unxos GmbH.',
        'license_spdx': 'CC-BY-4.0',
        'license_url': 'https://creativecommons.org/licenses/by/4.0/',
        'rights_holder': 'Unxos GmbH',
        'source_url': 'https://www.geonames.org/',
        'contributors': [],
        'api_item': 'http://api.geonames.org/getJSON?formatted=true&geonameId=<id>&username=<username>&style=full',
        'web_item': 'https://www.geonames.org/<id>',  # place#121: source human page
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
        'dataset_name': 'Getty TGN',
        'namespace': 'tgn',
        'redistributable': True,  # ODC-By-1.0 (attribution)
        # Verified 2026-06-06 against getty.edu/research/tools/vocabularies/obtain.
        'citation_text': 'Getty Thesaurus of Geographic Names (TGN), J. Paul Getty Trust.',
        'license_spdx': 'ODC-By-1.0',
        'license_url': 'https://opendatacommons.org/licenses/by/1-0/',
        'rights_holder': 'J. Paul Getty Trust',
        'source_url': 'https://www.getty.edu/research/tools/vocabularies/tgn/',
        'contributors': [],
        'api_item': 'https://vocab.getty.edu/tgn/<id>.jsonld',
        'web_item': 'https://vocab.getty.edu/tgn/<id>',  # place#121: source human page
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
        'redistributable': True,  # CC0
        # Verified 2026-06-06 against wikidata.org/wiki/Wikidata:Licensing (data is CC0).
        'citation_text': 'Wikidata, Wikimedia Foundation.',
        'license_spdx': 'CC0-1.0',
        'license_url': 'https://creativecommons.org/publicdomain/zero/1.0/',
        'rights_holder': 'Wikimedia Foundation',
        'source_url': 'https://www.wikidata.org/',
        'contributors': [],
        'api_item': 'https://www.wikidata.org/wiki/Special:EntityData/<id>.json',
        'web_item': 'https://www.wikidata.org/wiki/<id>',  # place#121: source human page
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
        'dataset_name': 'OpenStreetMap',
        'namespace': 'osm',
        'redistributable': True,  # ODbL (share-alike)
        # Verified 2026-06-06 against openstreetmap.org/copyright (data is ODbL).
        'citation_text': 'OpenStreetMap, © OpenStreetMap contributors.',
        'license_spdx': 'ODbL-1.0',
        'license_url': 'https://opendatacommons.org/licenses/odbl/1-0/',
        'rights_holder': 'OpenStreetMap contributors',
        'source_url': 'https://www.openstreetmap.org/',
        'contributors': [],
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
        'dataset_name': 'OpenHistoricalMap',
        'namespace': 'ohm',
        'redistributable': True,  # CC0
        # Verified 2026-06-06, RE-VERIFIED 2026-07-29 (place#157 queried this as a
        # suspected copy-paste of the OSM row): OHM is CC0 (public-domain
        # dedication), NOT ODbL like OSM. openhistoricalmap.org/copyright —
        # "Except where otherwise noted, OpenHistoricalMap data is dedicated to
        # the public domain under a Creative Commons CC0 dedication"; the OSM wiki
        # page (wiki.openstreetmap.org/wiki/OpenHistoricalMap/Copyright) agrees.
        # Attribution is appreciated but NOT required. This is a deliberate
        # determination, not an ingest error — do not "correct" it to ODbL.
        # CAVEAT (the "except where otherwise noted"): individual OHM elements may
        # carry a `license=*` tag placing them under CC-BY / CC-BY-SA. We do not
        # currently read that tag (authorities/ohm-places.py ingests name/type/date
        # tags only), so a per-element exception would be indexed as CC0. Revisit
        # if OHM's tagged exceptions become non-trivial in number.
        'citation_text': 'OpenHistoricalMap, by OpenHistoricalMap contributors.',
        'license_spdx': 'CC0-1.0',
        'license_url': 'https://creativecommons.org/publicdomain/zero/1.0/',
        'rights_holder': 'OpenHistoricalMap contributors',
        'source_url': 'https://www.openhistoricalmap.org/',
        'contributors': [],
        'api_item': '',
        # NB: this free-text blob is what the registry stores as the gazetteer
        # `description`. It previously claimed ODbL — copied verbatim from the OSM
        # row above — flatly contradicting the CC0 licence field and (per
        # place#157) misleading a reader into "correcting" the licence. Fixed
        # 2026-07-29; re-push the inventory to propagate.
        'citation': 'OpenHistoricalMap is open data, dedicated to the public domain under a Creative Commons CC0 dedication (except where individual elements are otherwise noted). https://www.openhistoricalmap.org/',
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
        'dataset_name': 'Library of Congress',
        'namespace': 'loc',
        'redistributable': True,  # relations-only — no data to redistribute
        'api_item': 'https://www.loc.gov/item/<id>/',
        'web_item': 'https://www.loc.gov/item/<id>/',  # place#121: source human page
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
        'dataset_name': 'Native Land',
        'namespace': 'nl',
        'redistributable': False,  # Native Land data-sovereignty: redistribution by explicit permission
        # Verified 2026-06-06: Native Land Digital "Data Sovereignty Treaty" (OCAP®)
        # — bespoke terms, NOT CC0/SPDX: NON-COMMERCIAL only, redistribution by
        # explicit permission, mandatory attribution + acknowledgement of Indigenous
        # communities as stewards. Bound to WHG custom License 'custom-nativeland-dst'
        # (commercial=False, custom=True; seeded on WHG atlas 2026-06-06).
        'citation_text': 'Indigenous territory, language, and treaty data provided by Native Land Digital (native-land.ca), used under the Native Land Digital Data Sovereignty Treaty; Indigenous communities are the rightful stewards of this data.',
        'license_spdx': 'custom-nativeland-dst',  # WHG custom License row (non-SPDX terms)
        'license_url': 'https://api-docs.native-land.ca/data-sovereignty-treaty',
        'rights_holder': 'Native Land Digital',
        'source_url': 'https://native-land.ca/',
        'contributors': [],
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
        'dataset_name': 'D-PLACE',
        'namespace': 'dp',
        'redistributable': True,  # CC-BY-NC: non-commercial redistribution permitted
        # Verified 2026-06-06 (d-place.org/about): CC-BY-NC-4.0 (NonCommercial),
        # NOT plain CC-BY-4.0. D-PLACE aggregates many upstream ethnographic
        # datasets, each with its own citation, in addition to Kirby et al. 2016.
        'citation_text': 'D-PLACE: The Global Database of Cultural, Linguistic and Environmental Diversity, Max Planck Institute for Evolutionary Anthropology.',
        'license_spdx': 'CC-BY-NC-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-nc/4.0/',
        'rights_holder': 'Max Planck Institute for Evolutionary Anthropology (D-PLACE)',
        'source_url': 'https://d-place.org/',
        'contributors': [],
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
        'redistributable': True,  # CC-BY-SA-4.0
        # Verified 2026-06-06: WHG ingests the *abridged* GB1900 gazetteer
        # (~1.17M rows, matching our count) = CC-BY-SA; only the raw dump is CC0.
        # Share-alike + attribution required (visionofbritain.org.uk/data).
        'citation_text': 'GB1900 Gazetteer, Great Britain Historical GIS and the GB1900 project partners and volunteers.',
        'license_spdx': 'CC-BY-SA-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
        'rights_holder': 'Great Britain Historical GIS & the GB1900 project partners and volunteers',
        'source_url': 'https://www.pastplace.org/data/',
        'contributors': [],
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
        'dataset_name': 'Index Villaris',
        'namespace': 'iv',
        'redistributable': True,  # CC-BY-SA-4.0
        # Verified 2026-06-06 via the repo LICENSE (GitHub licence API: CC-BY-SA-4.0).
        # The 1680 source (John Adams) is public domain; the digitised dataset is the
        # licensed layer. Share-alike + attribution to the digital editors required.
        'citation_text': 'Index Villaris (1680, John Adams; public domain); digital GIS edition by Stephen Gadd and Alexis Litvine (DOI 10.5281/zenodo.4748653).',
        'license_spdx': 'CC-BY-SA-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
        'rights_holder': 'Stephen Gadd & Alexis Litvine (digital edition); original 1680 work by John Adams (public domain)',
        'source_url': 'https://github.com/docuracy/IndexVillaris1680',
        'contributors': [],
        'api_item': '',
        'citation': 'Index Villaris, 1680',
        'files': [
            {
                'url': 'https://github.com/docuracy/IndexVillaris1680/raw/refs/heads/main/docs/data/IV-GB1900-OSM-WD.lp.json',
                'file_type': 'json',
            }
        ],
    },
    {  # UN countries and territories
        'dataset_name': 'UN Countries',
        'namespace': 'un',  # United Nations countries and territories
        'redistributable': True,  # UN geodata — already committed as reference geometry
        # Adopted 2026-07-14: UN Geospatial BNDA (Boundaries of Administrative
        # units, "BNDA_simplified"), replacing Natural Earth. The UN's own
        # authoritative, politically-neutral administrative boundaries, with
        # native ISO 3166-1 alpha-2 codes for every country and dependent
        # territory (no NE "-99" dropout). Committed as
        # processing/data/un_bnda_countries.geojson.
        'citation_text': 'Country and territory boundaries © United Nations, from UN Geospatial Data (BNDA), 2023.',
        'license_spdx': 'custom-un-geodata',
        'license_url': 'https://www.un.org/geospatial/',
        'rights_holder': 'United Nations',
        'source_url': 'https://geoportal.un.org/',
        'contributors': [],
        'api_item': '',
        'citation': ('United Nations Geospatial Data (Geodata). The designations '
                     'employed and the presentation of material on this map do not '
                     'imply the expression of any opinion whatsoever on the part of '
                     'the Secretariat of the United Nations concerning the legal '
                     'status of any country, territory, city or area or of its '
                     'authorities, or concerning the delimitation of its frontiers '
                     'or boundaries. https://geoportal.un.org/'),
        'files': [
            {
                'url': 'https://geoportal.un.org/arcgis/sharing/rest/content/items/6d6fb235f64d47248a5e3b78ef4b6273/data',
                'file_type': 'json',
                'item_path': 'features',
            }
        ],
    },
    {  # UK historic counties (92 polygons; regional containment geographies)
        'dataset_name': 'UK Historic Counties',
        'namespace': 'ukhc',
        'redistributable': True,  # county-borders.co.uk: free for all use, attribution requested
        # Verified 2026-06-06 (county-borders.co.uk terms): free for personal/
        # educational/non-commercial AND commercial use, attribution requested
        # ("would appreciate"). Bound to WHG custom License 'custom-historic-counties'
        # (commercial=True, custom=True; seeded on WHG atlas 2026-06-06).
        'citation_text': 'Historic county boundary data provided by the Historic County Borders Project (Historic Counties Trust), https://county-borders.co.uk.',
        'license_spdx': 'custom-historic-counties',  # WHG custom License row (non-SPDX terms)
        'license_url': 'https://county-borders.co.uk/',
        'rights_holder': 'Historic Counties Trust',
        'source_url': 'https://county-borders.co.uk/',
        'contributors': [],
        'api_item': '',
        'citation': 'Historic County Borders Project (Historic Counties Trust). https://county-borders.co.uk/',
        'files': [
            {
                # Definition A = whole historic counties (Yorkshire / Lincolnshire
                # as single counties, not split into ridings / parts), full-
                # resolution WGS84 polygons. Swap to *_Simplified.zip (~5 MB) for a
                # lighter set, or UKDefinitionB_* for the ridings / parts split.
                'url': 'https://county-borders.co.uk/UKDefinitionA_WG84_Full_Resolution.zip',
                'name': 'UKDefinitionA_WG84_Full_Resolution.zip',
                'file_type': 'shapefile-zip',
            }
        ],
    },
    # ─── Vision of Britain / GB Historical GIS boundary levels (place#135) ───
    # Four separate UKDS studies, each OPEN + CC-BY-SA-4.0 (attribution +
    # share-alike; commercial use AND redistribution permitted). Per-census-year
    # OSGB (EPSG:27700) shapefiles, modelled as one multi-snapshot place per
    # stable G_UNIT (see authorities/vob_common.py). The download is a one-shot
    # session link, so the .zip is placed manually in the namespace data dir
    # (files:[] → fetch_authorities is a no-op; the authority script globs it).
    # Parishes are the deliberate restricted exception — NOT here (see #135).
    {  # Registration districts (== poor-law unions; coterminous PR_DIST)
        'dataset_name': 'GBHGIS Registration Districts of England & Wales, 1851–1911',
        'namespace': 'vob_rd',
        'description': ('Registration districts (also the coterminous poor-law '
                        'unions) of England & Wales at each census 1851–1911, as '
                        'boundary polygons — historic containment geographies for '
                        'reconciliation. Great Britain Historical GIS Project.'),
        'citation_text': ('Great Britain Historical GIS Project (2023). Digital '
                          'Boundaries for Registration Districts of England and Wales, '
                          '1851–1911. University of Portsmouth. UK Data Service SN 9032, '
                          'CC BY-SA 4.0.'),
        'license_spdx': 'CC-BY-SA-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
        'rights_holder': 'Great Britain Historical GIS Project, University of Portsmouth',
        'source_url': 'https://doi.org/10.5255/UKDA-SN-9032-1',
        'redistributable': True,  # CC-BY-SA-4.0 permits re-hosting
        'contributors': [],
        'api_item': '',
        'citation': ('Southall, H.R., University of Portsmouth (2023). Great Britain '
                     'Historical Database: Digital Boundaries for Registration Districts '
                     'of England and Wales, 1851–1911. UK Data Service. SN 9032, '
                     'http://doi.org/10.5255/UKDA-SN-9032-1'),
        'files': [],  # manual placement — see note above
    },
    {  # Registration counties
        'dataset_name': 'GBHGIS Registration Counties of England & Wales, 1851–1911',
        'namespace': 'vob_rc',
        'description': ('Registration counties of England & Wales at each census '
                        '1851–1911, as boundary polygons. Great Britain Historical '
                        'GIS Project.'),
        'citation_text': ('Great Britain Historical GIS Project. Digital Boundaries for '
                          'Registration Counties of England and Wales, 1851–1911. '
                          'University of Portsmouth. UK Data Service SN 9033, '
                          'CC BY-SA 4.0.'),
        'license_spdx': 'CC-BY-SA-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
        'rights_holder': 'Great Britain Historical GIS Project, University of Portsmouth',
        'source_url': 'https://doi.org/10.5255/UKDA-SN-9033-1',
        'redistributable': True,  # CC-BY-SA-4.0 permits re-hosting
        'contributors': [],
        'api_item': '',
        'citation': ('Great Britain Historical Database: Digital Boundaries for '
                     'Registration Counties of England and Wales, 1851–1911. UK Data '
                     'Service. SN 9033, http://doi.org/10.5255/UKDA-SN-9033-1'),
        'files': [],  # manual placement
    },
    {  # Administrative counties
        'dataset_name': 'GBHGIS Administrative Counties of England & Wales, 1911–1971',
        'namespace': 'vob_cty',
        'description': ('Administrative counties of England & Wales at each census '
                        '1911–1971, as boundary polygons. Great Britain Historical '
                        'GIS Project.'),
        'citation_text': ('Great Britain Historical GIS Project (2024). Digital '
                          'Boundaries for the Administrative Counties of England and '
                          'Wales, 1911–1971. University of Portsmouth. UK Data Service '
                          'SN 9179, CC BY-SA 4.0.'),
        'license_spdx': 'CC-BY-SA-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
        'rights_holder': 'Great Britain Historical GIS Project, University of Portsmouth',
        'source_url': 'https://doi.org/10.5255/UKDA-SN-9179-1',
        'redistributable': True,  # CC-BY-SA-4.0 permits re-hosting
        'contributors': [],
        'api_item': '',
        'citation': ('Southall, H.R. (2024). Great Britain Historical Database: Digital '
                     'Boundaries for the Administrative Counties of England and Wales, '
                     '1911–1971. UK Data Service. SN 9179, '
                     'http://doi.org/10.5255/UKDA-SN-9179-1'),
        'files': [],  # manual placement
    },
    {  # Local-government districts
        'dataset_name': 'GBHGIS Local Government Districts of England & Wales, 1911–1971',
        'namespace': 'vob_lgd',
        'description': ('Local-government districts (rural/urban districts, municipal '
                        '& county boroughs, London boroughs) of England & Wales at each '
                        'census 1911–1971, as boundary polygons. Great Britain '
                        'Historical GIS Project.'),
        'citation_text': ('Great Britain Historical GIS Project. Digital Boundaries for '
                          'Local Government Districts of England and Wales, 1911–1971. '
                          'University of Portsmouth. UK Data Service SN 9321, '
                          'CC BY-SA 4.0.'),
        'license_spdx': 'CC-BY-SA-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
        'rights_holder': 'Great Britain Historical GIS Project, University of Portsmouth',
        'source_url': 'https://doi.org/10.5255/UKDA-SN-9321-1',
        'redistributable': True,  # CC-BY-SA-4.0 permits re-hosting
        'contributors': [],
        'api_item': '',
        'citation': ('Great Britain Historical Database: Digital Boundaries for Local '
                     'Government Districts of England and Wales, 1911–1971. UK Data '
                     'Service. SN 9321, http://doi.org/10.5255/UKDA-SN-9321-1'),
        'files': [],  # manual placement
    },
    {  # Ancient parishes & places of England & Wales, pre-1850 (place#135)
        # Kain & Oliver's historic-parish boundaries (SN 4348, 2001), converted
        # to a single ~28k-polygon GIS by Burton et al. (SN 4828, 2004) and
        # error-corrected + census-attributed by Satchell et al. (SN 852232 /
        # ReShare, 2023) — the version indexed here. RESTRICTED: obtained under
        # the UK Data Service End User Licence (registration-gated); NOT
        # redistributable (index-in-place only — no WHG-download) and commercial
        # use is prohibited. Hence redistributable=False (see DOWNLOAD_MAX_RECORDS
        # note above). No SPDX licence — the EUL is bespoke. Placed manually
        # (safeguarded, not auto-fetchable).
        'dataset_name': 'Ancient Parishes & Places of England & Wales (pre-1850)',
        'namespace': 'kain_par',
        'description': ('Boundaries of the ancient parishes, townships and places '
                        'of England & Wales before 1850 (~23k polygons), aligned to '
                        'the 1851 census. Kain & Oliver, via the Cambridge Group '
                        '(CAMPOP) enhanced GIS. Historic containment geographies for '
                        'reconciliation.'),
        'citation_text': ('Satchell, A.E.M, Kitson, P.K, Newton, G.H, Shaw-Taylor, L. '
                          'and Wrigley, E.A (2023). 1851 England and Wales census '
                          'parishes, townships and places. UK Data Service SN 852232. '
                          'Derived from Kain, R.J.P. & Oliver, R.R., Historic Parishes '
                          'of England and Wales (2001).'),
        # Recorded against the WHG custom License row 'custom-ukds-eul' (see
        # CUSTOM_LICENCES above). It was previously '' — which the push filters
        # out as falsy, so the registry received NO licence at all and the Atlas
        # showed "licence not specified" for the single most restricted authority
        # we hold (place#157). Bespoke terms are still terms: record them.
        'license_spdx': 'custom-ukds-eul',
        'license_url': 'https://ukdataservice.ac.uk/app/uploads/cd137-enduserlicence.pdf',
        'rights_holder': ('The Cambridge Group for the History of Population and Social '
                          'Structure; R.J.P. Kain & R.R. Oliver'),
        'source_url': 'https://doi.org/10.5255/UKDA-SN-852232',
        'redistributable': False,  # UKDS EULA — index-in-place only, no re-hosting
        'contributors': [],
        'api_item': '',
        'citation': ('Satchell, A.E.M et al. (2023). 1851 England and Wales census '
                     'parishes, townships and places. [Data Collection]. UK Data '
                     'Service. SN 852232, http://doi.org/10.5255/UKDA-SN-852232. '
                     'Underlying boundaries: Kain, R.J.P. & Oliver, R.R. (2001), SN 4348.'),
        'files': [],  # manual placement (safeguarded, EULA-gated)
    },
    {  # PeriodO temporal periods with spatial coverage
        'dataset_name': 'PeriodO',
        'namespace': 'po',
        'redistributable': True,  # CC0
        # Verified 2026-06-06 (perio.do/license): CC0 public-domain dedication.
        'citation_text': 'PeriodO: A Gazetteer of Period Definitions for Linking and Visualizing Data, PeriodO contributors.',
        'license_spdx': 'CC0-1.0',
        'license_url': 'https://creativecommons.org/publicdomain/zero/1.0/',
        'rights_holder': 'PeriodO contributors',
        'source_url': 'https://perio.do/',
        'contributors': [],
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
        'redistributable': True,  # CC-BY-4.0
        # Verified 2026-06-06 via repo LICENSE.md (CC-BY-4.0).
        'citation_text': 'Cliopatria: A Modular GIS-Ready Dataset of Historical Polity Boundaries, Seshat: Global History Databank.',
        'license_spdx': 'CC-BY-4.0',
        'license_url': 'https://creativecommons.org/licenses/by/4.0/',
        'rights_holder': 'Seshat: Global History Databank',
        'source_url': 'https://github.com/Seshat-Global-History-Databank/cliopatria',
        'contributors': [],
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
    {  # ~82K historical Chinese administrative places
        'dataset_name': 'China Historical GIS (CHGIS)',
        'namespace': 'chgis',
        'redistributable': False,  # Harvard academic-only: NO resale OR redistribution
        # Verified 2026-06-06 (chgis.fas.harvard.edu/data/chgis/v6): bespoke
        # "academic research only" terms — NO commercial use, resale, OR
        # redistribution (stricter than any CC licence). Bound to WHG custom License
        # 'custom-chgis-academic' (commercial=False, custom=True; seeded on WHG atlas
        # 2026-06-06). No-redistribution clause may warrant direct permission.
        'citation_text': 'CHGIS, Version 6. © Fairbank Center for Chinese Studies, Harvard University & Center for Historical Geographical Studies, Fudan University, 2016.',
        'license_spdx': 'custom-chgis-academic',  # WHG custom License row (non-SPDX terms)
        'license_url': 'https://chgis.fas.harvard.edu/data/chgis/v6/',
        'rights_holder': 'Fairbank Center for Chinese Studies, Harvard University & Center for Historical Geographical Studies, Fudan University',
        'source_url': 'https://chgis.fairbank.fas.harvard.edu/',
        'contributors': [],
        'api_item': '',
        'citation': 'China Historical GIS / Temporal Gazetteer (TGAZ). Harvard University & Fudan University. https://sites.fas.harvard.edu/~chgis/',
        'files': [
            {
                # Built locally by authorities/chgis/build_database.py
                # from 02-tgaz-dev-2018.sql (MySQL dump)
                'url': '',
                'name': 'tgaz.db',
                'file_type': 'sqlite',
            }
        ],
    },
    {  # ~3.8K Song dynasty administrative entities
        'dataset_name': 'Digital Gazetteer of the Song Dynasty',
        'namespace': 'dgsd',
        'redistributable': True,  # CC-BY-ND, but PI (Mostern) endorses WHG use incl. redistribution
        # Verified 2026-06-06 via the D-Scholarship record badge: CC-BY-ND-4.0
        # (NoDerivatives) — corrects an earlier CC-BY-NC-SA guess. NOT yet in the WHG
        # seeded SPDX set (WHG skips+logs until seeded → seed CC-BY-ND-4.0). The ND
        # term nominally restricts distributing DERIVATIVES, but this is NOT a blocker
        # for WHG: DGSD's author Ruth Mostern is WHG's PI and endorses all use here.
        # Licence recorded truthfully regardless (deposit also marks "Copyright Not
        # Evaluated"; data is factual/historical, from Hope Wright's 1958 index).
        'citation_text': 'Ruth Mostern and Elijah Meeks, The Digital Gazetteer of the Song Dynasty, Version 1.1, University of Pittsburgh D-Scholarship, 2022.',
        'license_spdx': 'CC-BY-ND-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-nd/4.0/',
        'rights_holder': 'Ruth Mostern & Elijah Meeks',
        'source_url': 'https://d-scholarship.pitt.edu/44108/',
        'contributors': [],
        'api_item': '',
        'citation': 'Digital Gazetteer of the Song Dynasty (DGSD) v1.1. Ruth Mostern & Elijah Meeks, UC Merced.',
        'files': [
            {
                # Built locally by authorities/dgsd/build_database.py
                # from dgsd11.sql (MySQL dump in 44108_dgsd11.zip)
                'url': '',
                'name': 'dgsd.db',
                'file_type': 'sqlite',
            }
        ],
    },
    {  # ~24K ancient/historical places with coordinates
        'dataset_name': 'Trismegistos',
        'namespace': 'tm',
        'redistributable': True,  # CC-BY-SA-4.0
        # Verified 2026-06-06 (trismegistos.org/dataservices): CC-BY-SA-4.0 (NOT
        # non-commercial, contrary to common assumption). Share-alike applies to
        # derivative datasets; registration only needed for premium features.
        'citation_text': 'Trismegistos (TM Geo / Places), Trismegistos, KU Leuven.',
        'license_spdx': 'CC-BY-SA-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
        'rights_holder': 'Trismegistos / KU Leuven',
        'source_url': 'https://www.trismegistos.org/',
        'contributors': [],
        'api_item': 'https://www.trismegistos.org/place/<id>',
        'web_item': 'https://www.trismegistos.org/place/<id>',  # place#121: source human page
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
    {  # HGIS de las Indias — Bourbon Spanish America 1701-1808 (lugares + territorios)
        'dataset_name': 'HGIS de las Indias',
        'namespace': 'hgis',
        'redistributable': True,  # CC-BY-SA-4.0
        'api_item': '',
        # Werner Stangl, HGIS de las Indias (University of Graz, FWF-funded
        # 2015-2017): lugares (settlements, points) + territorios (administrative,
        # polygons), combined into ONE authority. Imported from WHG LPF exports,
        # which are transient and NOT referenced in attribution — canonical source
        # is hgis-indias.net / Harvard Dataverse. CC-BY-SA 4.0 (verified 2026-06-07:
        # "All data within HGIS de las Indias ... CC Attribution-Share Alike 4.0").
        # Polygon-bearing → full geom-store/h3/ccode chain; see authorities/hgis-places.py.
        'description': 'A historical-geographic information system of Bourbon Spanish '
                       'America (1701-1808): administrative territorios (polygons) and '
                       'lugares/settlements (points), richly cross-referenced to GeoNames, '
                       'Wikidata, the Getty TGN, LoC and VIAF.',
        'citation_text': 'HGIS de las Indias: a historical-geographic information system of '
                         'Bourbon Spanish America (1701-1808). Werner Stangl, University of '
                         'Graz (FWF-funded). https://www.hgis-indias.net/',
        'license_spdx': 'CC-BY-SA-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
        'rights_holder': 'Werner Stangl (HGIS de las Indias) / University of Graz',
        'source_url': 'https://www.hgis-indias.net/',
        'contributors': [
            {'name': 'Werner Stangl', 'role': 'Data curation'},
        ],
        'files': [
            # Local LPF imports (lugares + territorios). Large; not committed —
            # place under DATA_DIR/authorities/hgis/. No fetch URL (the upstream
            # WHG export is transient; re-import from hgis-indias.net / Dataverse).
            {'url': '', 'name': 'whg_dataset_14.lpf', 'file_type': 'lpf'},
            {'url': '', 'name': 'whg_dataset_15.lpf', 'file_type': 'lpf'},
        ],
    },
    {  # ~17.5K places from Alcedo's 1786-89 Diccionario (ANR TopUrbi digitisation)
        'dataset_name': 'Alcedo',
        'namespace': 'alc',
        'redistributable': True,  # CC-BY-NC: non-commercial redistribution permitted
        'api_item': '',
        # Antonio de Alcedo, Diccionario geográfico-histórico de las Indias
        # Occidentales ó América (1786-89); TEI digital edition by Werner Stangl
        # under ANR TopUrbi (PI Jean-Paul Zúñiga; technical lead Carmen Brando,
        # EHESS). Point geometries only. CC-BY-NC 4.0; ANR mandates record-level
        # attribution of the project code (carried in citation_text). Linked to
        # HGIS de las Indias (in WHG as lugares/territorios) via gazetteermatch.
        # We ingest Werner's pristine structured export (pipe-delimited) from the
        # OFFICIAL gitlab repo — NOT Karl Grossner's derived LP-TSV, which dropped
        # the per-row AAT + confidence columns. See authorities/alcedo-places.py.
        'description': "Antonio de Alcedo's Diccionario geográfico-histórico de las Indias "
                       "Occidentales ó América (1786-89): ~18,600 settlements, regions, "
                       "peoples and features across colonial Spanish America, with full "
                       "Spanish entry texts and source-page links to the TEI edition.",
        'citation_text': "Antonio de Alcedo, Diccionario geográfico-histórico de las "
                         "Indias Occidentales ó América (1786-1789); digital edition by "
                         "Werner Stangl. ANR TopUrbi — Topographie de l'urbanisation "
                         "impériale hispanique (Projet-ANR-21-CE27-0023).",
        'license_spdx': 'CC-BY-NC-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-nc/4.0/',
        'rights_holder': 'ANR TopUrbi (Projet-ANR-21-CE27-0023); Werner Stangl',
        'source_url': 'https://gitlab.huma-num.fr/plateforme-geomatique-et-hn/topurbi-project',
        'contributors': [
            {'name': 'Werner Stangl', 'role': 'Data curation'},
            {'name': 'Jean-Paul Zúñiga', 'role': 'Project administration'},
            {'name': 'Carmen Brando', 'role': 'Software'},
        ],
        # Legacy free-text blob: the push sends it as `description`, which is all a
        # pre-Phase-4 prod stores until the atlas→main promotion (citation_text +
        # the structured fields are already live on dev).
        'citation': 'Antonio de Alcedo, Diccionario geográfico-histórico de las '
                    'Indias Occidentales ó América (1786-1789). Digital edition by '
                    'Werner Stangl under ANR TopUrbi (Projet-ANR-21-CE27-0023), '
                    'https://gitlab.huma-num.fr/plateforme-geomatique-et-hn/topurbi-project '
                    '(CC-BY-NC 4.0).',
        'files': [
            {
                # Werner's pristine structured export (pipe-delimited, ~19.3k rows)
                # from the official TopUrbi gitlab. The mapper parses '|' and keeps
                # only entrytype=='Toponym' (drops Referral/Correction/Term).
                'url': 'https://gitlab.huma-num.fr/plateforme-geomatique-et-hn/'
                       'topurbi-project/-/raw/main/UpdateDataWorkflow/csv/'
                       'Alcedo_structured.csv',
                'name': 'Alcedo_structured.csv',
                'file_type': 'csv',
            }
        ],
    },
    {  # ~16.3K Ottoman populated places from 19th-c. population registers (NFS.d.)
        'dataset_name': 'Ottoman NFS Gazetteer',
        'namespace': 'ofs',
        'redistributable': True,  # CC-BY-4.0
        # Verified 2026-06-06 (Zenodo record 7351936 Rights field): CC-BY-4.0.
        'citation_text': 'Kabadayı, M. Erdem, Akın Sefer, Grigor Boykov & Piet Gerrits (2022). Ottoman NFS Gazetteer. Zenodo. https://doi.org/10.5281/zenodo.7351936.',
        'license_spdx': 'CC-BY-4.0',
        'license_url': 'https://creativecommons.org/licenses/by/4.0/',
        'rights_holder': 'M. Erdem Kabadayı, Akın Sefer, Grigor Boykov & Piet Gerrits',
        'source_url': 'https://zenodo.org/records/7351936',
        'contributors': [],
        'api_item': '',
        'citation': 'Kabadayı, M.E., Boykov, G., Sefer, A. & Gerrits, P. (2022). '
                    'Ottoman NFS Gazetteer (16,296 populated places, 1830-1849). '
                    'Zenodo. https://doi.org/10.5281/zenodo.7351936',
        'files': [
            {
                # Stable Zenodo direct-asset endpoint: returns the .xlsx with a
                # content-length (octet-stream) and follows redirects, so
                # fetch_authorities downloads it automatically (NOT the HTML
                # record page at /records/7351936). Saved under the clean `name`
                # below. Staged by authorities/ottnfs-places.py (points only; see
                # its docstring runbook for the incremental single-namespace add).
                'url': 'https://zenodo.org/api/records/7351936/files/'
                       'Kabadayi_Boykov_Sefer_Gerrits_Ottoman_NFS_Gazetteer_'
                       '23112022_16296_populated_places_version_1.xlsx/content',
                'name': 'Kabadayi_Boykov_Sefer_Gerrits_Ottoman_NFS_Gazetteer.xlsx',
                'file_type': 'xlsx',
            }
        ],
    },
    {  # ~6.3K Ottoman administrative units (eyalet/vilayet/sancak/kaza/nahiye)
        'dataset_name': 'Ottoman Gazetteer (ottgaz)',
        'namespace': 'og',
        'redistributable': True,  # CC-BY-NC: non-commercial redistribution permitted
        # Verified 2026-06-06 via repo LICENSE + README badge: CC-BY-NC-4.0.
        'citation_text': 'Hanley, Will (2021). Ottoman Gazetteer (ottgaz.org), transformed from Tahir Sezen, Osmanlı Yer Adları.',
        'license_spdx': 'CC-BY-NC-4.0',
        'license_url': 'https://creativecommons.org/licenses/by-nc/4.0/',
        'rights_holder': 'Will Hanley (Florida State University)',
        'source_url': 'https://ottgaz.org/',
        'contributors': [],
        'api_item': 'https://ottgaz.org/wiki/Item:<id>',
        'web_item': 'https://ottgaz.org/wiki/Item:<id>',  # place#121: source human page
        # Licence: CC-BY-NC 4.0. Will Hanley (FSU), transformed from Tahir Sezen,
        # Osmanlı Yer Adları. No native coordinates — WHG computes convex-hull
        # geometry from ofs member points (source='ofs') or pulls points from
        # linked Wikidata records (source='wd'); see authorities/ottgaz-places.py.
        'citation': 'Hanley, W. (2021). Ottoman Gazetteer (ottgaz): a linkable '
                    'database of Ottoman administrative units, transformed from '
                    'Tahir Sezen, Osmanlı Yer Adları. https://ottgaz.org / '
                    'https://github.com/whanley/Ottoman-Gazetteer (CC-BY-NC 4.0)',
        'files': [
            {
                'url': 'https://raw.githubusercontent.com/whanley/Ottoman-Gazetteer/'
                       'master/data/archived-versions/ottgaz-data-9.tsv',
                'name': 'ottgaz-data-9.tsv',
                'file_type': 'tsv',
            }
        ],
    },
]
