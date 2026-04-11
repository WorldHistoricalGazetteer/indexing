# CLAUDE.md — Agent Briefing for WHG Indexing Repository

> **Repository:** `WorldHistoricalGazetteer/indexing`
> **Last updated:** 6 April 2026

---

## Project Overview

This repository powers the **World Historical Gazetteer (WHG)** search
infrastructure — a system that indexes ~47 million place records from multiple
independent gazetteers into Elasticsearch, clusters co-referent records, and
serves search/reconciliation queries via a FastAPI gateway.

WHG is an NEH-funded digital humanities project. The production ES instance
runs on the University of Pittsburgh Center for Research Computing (CRC), with
a Django front-end on DigitalOcean proxying to the CRC gateway.

---

## Architecture

```
Browser → Django (DigitalOcean) → CRC Gateway (FastAPI) → Elasticsearch 9.x (CRC)
                                                            ├── places   (~47M docs)
                                                            ├── toponyms (~67M docs)
                                                            └── clusters (~20M docs)
```

- **Gateway** (`gateway/`): FastAPI app on port 9200 (the only port open
  through the CRC firewall). Routes Kibana traffic by `Host` header, proxies
  all other HTTP to ES on localhost:9201. Custom endpoints under `/api/`:
  - `POST /api/search` — full filtered search (3-step: discovery → filtering → enrichment)
  - `GET /api/suggest` — fast typeahead on toponyms index
  - `POST /api/reconcile` — reconciliation search (same 3-step architecture)
  - `GET /api/search/phonetic` — Symphonym KNN phonetic search
  - `GET /api/embed`, `POST /api/embed` — Symphonym embedding generation
  - `GET /api/health` — gateway + ES cluster health check
- **Processing** (`processing/`): Slurm-orchestrated scripts for fetching
  authority data, ingesting into ES, and production deployment.
- **Authorities** (`authorities/`): One script per data source, each producing
  documents conforming to the `places` index schema.
- **Clustering** (`clustering/`): 4-phase pipeline discovering co-referent
  place records across gazetteers (hard links → exact toponyms → phonetic
  similarity → composite scoring + graph clustering).
- **Phonetics** (`phonetics/`): Symphonym model training, inference, and
  toponym embedding extraction. The model produces 128-d int8 embeddings
  capturing phonetic similarity across scripts.

---

## `es.sh` — Operations Orchestrator (`scripts/es.sh`)

The `es.sh` script is the primary management interface for the entire WHG
Elasticsearch infrastructure. It is aliased as `es` in the user's shell
(set up by `es -install`) and covers installation, production services,
staging environments, the full Symphonym training pipeline, clustering,
and DigitalOcean migration.

### Script modules

`es.sh` is the single CLI entry point but sources domain-specific modules
for maintainability:

| File | Lines | Role |
|------|-------|------|
| `_common.sh` | ~70 | Shared bootstrap: paths, `.env`, conda, `activate_environment()`, `es_curl()` |
| `es.sh` | ~1400 | Core ES/Kibana/Gateway management, staging, forcemerge, case dispatcher + help |
| `symphonym.sh` | ~1300 | Symphonym training pipeline (toponym rebuild, training data, model training, embeddings) |
| `cluster.sh` | ~700 | Place clustering (snapshot exchange, Slurm/nohup execution, finalize) |
| `ingest.sh` | ~480 | Authority ingestion, boundary extraction, tile generation, ccode augmentation |

### Production services (run on CRC VM)

| Command | Description |
|---------|-------------|
| `es -start` | Start ES + Kibana + Gateway |
| `es -stop` | Stop all three |
| `es -restart` | Restart all three |
| `es es-start` / `es-stop` / `es-restart` | ES only |
| `es kibana-start` / `kibana-stop` / `kibana-restart` | Kibana only |
| `es gateway-start` / `gateway-stop` / `gateway-restart` | Gateway only |
| `es -health` | Production health check (cluster, indices, disk, memory) |

### Staging ES (Slurm compute node)

Staging runs a disposable single-node ES on ephemeral NVMe scratch via Slurm.
Connection info is written to `$STAGING_INFO_FILE` and sourced by downstream
jobs. The `htc` partition has four QOS tiers:

| QOS | Max wall time |
|-----|--------------|
| `htc-htc-s` | 1 day |
| `htc-htc-n` | 3 days |
| `htc-htc-l` | 6 days |
| `htc-htc-ll` | **21 days** (maximum available) |

| Command | Description |
|---------|-------------|
| `source es.sh -staging-start` | Launch staging ES; restores latest snapshot |
| `source es.sh -staging-start --places-only` | Restore only the `places` index |
| `source es.sh -staging-start --no-snapshot` | Start with empty `places` + `toponyms` indices (no snapshot restore) |
| `source es.sh -staging-stop` | Cancel Slurm job, clean up |
| `es -staging-status` | Show node, port, index counts |
| `es -staging-health` | Cluster health + Slurm job status |
| `es -staging-logs` | Tail recent Slurm logs |

### Ingestion & data maintenance

| Command | Description |
|---------|-------------|
| `es -ingest [OPTIONS]` | Submit authority ingestion Slurm job (requires staging ES) |
| `es -augment-ccodes [OPTIONS]` | Spatial country code assignment (runs against production, nohup) |
| `es -forcemerge [INDEX]` | Purge deleted docs, iteratively merge segments |

### Symphonym pipeline

| Command | Description |
|---------|-------------|
| `es -rebuild-toponyms VERSION` | Extract toponyms + Epitran IPA + PanPhon → ES (Slurm) |
| `es -precompute-phonetics VERSION` | Neural G2P on GPU (CharsiuG2P + Phonikud) |
| `es -generate-training-data VERSION` | Generate training sets from ES toponyms |
| `es -train-model VERSION [START [END]]` | Train Teacher→Student→Fine-tune (GPU Slurm) |
| `es -update-embeddings VERSION` | Compute Symphonym embeddings (GPU) + auto-submit index job |
| `es -update-embeddings-index VERSION` | Index precomputed embeddings to ES (CPU Slurm) |
| `es -train-and-update VERSION` | Full pipeline: train + compute + index |
| `es -partial-update-es VERSION --languages LANG…` | Update specific languages without full rebuild |

### Clustering

| Command | Description |
|---------|-------------|
| `es -cluster --full` | Full clustering run (nohup on VM) |
| `es -cluster --full --slurm` | Snapshot prod → staging → Slurm job |
| `es -cluster --incremental` | Incremental clustering (nohup on VM) |
| `es -cluster --resume --slurm` | Resume crashed full run on Slurm |
| `es -cluster --stats` | Show clustering statistics |
| `es -cluster-finalize TIMESTAMP` | Restore cluster results to production (alias swap) |

### Setup & maintenance

| Command | Description |
|---------|-------------|
| `es -install` | Download + install ES and Kibana, set up alias |
| `es -update` | `git pull` latest code |
| `es -setup-security` | One-time TLS + password setup (after certbot) |

### DigitalOcean backend switching

| Command | Description |
|---------|-------------|
| `es -do-check` | Show current DO ES backend status |
| `es -do-switch pitt` / `local` | Switch DO Django app between Pitt and local ES |
| `es -do-revert` | Alias for `-do-switch local` |
| `es -do-stop-es` / `-do-start-es` | Stop/start bare-metal ES on DO |
| `es -do-clone` | Clone DO indexes to Pitt |

---

## `types.sh` — Type System Orchestrator (`scripts/types.sh`)

Slurm-based orchestration for the full type system pipeline. Submits
dependency-chained jobs for each step.

| Command | Description |
|---------|-------------|
| `bash scripts/types.sh --all --es-host URL` | Full pipeline: build → map → sync → merge |
| `bash scripts/types.sh --build-vocabs --es-host URL` | Build GeoNames/Wikidata/Pleiades vocabulary files |
| `bash scripts/types.sh --map` | Apply AAT mappings (static → Wikidata → SPARQL) |
| `bash scripts/types.sh --sync --es-host URL` | Sync AAT hierarchy → ES types index |
| `bash scripts/types.sh --merge --es-host URL` | Merge cross-vocabulary fields into ES |
| `bash scripts/types.sh --status` | Show running/recent type pipeline jobs |
| `bash scripts/types.sh --force --sync --es-host URL` | Force re-download AAT dump |
| `bash scripts/types.sh --wait --all --es-host URL` | Run synchronously (wait for each step) |

---

## Key Indices (ES 9.x)

| Index | Schema | Content |
|-------|--------|---------|
| `places` | `schemas/places.json` | ~47M place records with nested toponyms, geometries, types, relations |
| `toponyms` | `schemas/toponyms.json` | ~67M deduplicated toponym records with Symphonym embeddings and attestation lists |
| `clusters` | `schemas/clusters.json` | ~20M pairwise link docs + membership docs |
| `types` | `schemas/types.json` | AAT place-type hierarchy with cross-vocabulary mappings, fclasses, materialized paths, multilingual labels/notes |

### Places schema (key fields)

```
place_id     keyword    — namespaced ID (e.g. gn:2988507, wd:Q90, osm:n12345)
title        text       — primary name
toponyms[]   nested     — {toponym_id, timespans[]}
geometries[] nested     — {geom (geo_shape), repr_point (geo_point), timespans[]}
types[]      nested     — {identifier, label, sourceLabel}
ccodes[]     keyword    — ISO 3166-1 alpha-2
relations[]  nested     — {relation_type, related_place_id, label, timespans[]}
links[]      nested     — {type, identifier}
population   long
elevation    integer
```

---

## Gateway Architecture (`gateway/`)

### Module structure

| Module | Role |
|--------|------|
| `app.py` | FastAPI application, lifespan (pre-warms Symphonym), health endpoint, phonetic search + embed endpoints, catch-all HTTP/WebSocket proxy |
| `config.py` | Loads `.env`; exports `ES_BACKEND` (localhost:9201), `KIBANA_BACKEND` (localhost:5601), index name patterns (`places_*`, `toponyms_*`, `clusters`), Symphonym model dir |
| `search.py` | `POST /api/search` and `GET /api/suggest` — the main search router |
| `reconcile.py` | `POST /api/reconcile` — reconciliation search (same 3-step architecture as search) |
| `es_helpers.py` | Shared ES query builders: `build_toponym_query`, `build_phonetic_knn`, `collect_place_ids`, `build_places_filter`, `build_toponym_lookup`, `build_cluster_lookup`, `build_suggest_query` |
| `proxy.py` | Async reverse proxy (httpx + websockets) with connection pooling |
| `symphonym.py` | Lazy-loads Symphonym v7 UniversalEncoder; provides `embed()`, `embed_batch()`, `quantize_to_byte()`, `build_knn_query()` |

### Three-step search pipeline (`/api/search` and `/api/reconcile`)

Both endpoints share the same architecture via `es_helpers.py`:

1. **Discovery** — query the `toponyms` index to collect candidate `place_id`s:
   - `fuzzy`/`phonetic` modes → Symphonym KNN (`build_phonetic_knn`, 128-d int8 vectors, similarity ≥ 0.7)
   - `exact`/`starts`/`in` modes → BM25 text search (`build_toponym_query`)
   - Each toponym doc carries `attestations[]` (list of place_ids) — scores are accumulated per place_id

2. **Filtering + Aggregations** — fetch candidate places from the `places` index:
   - `terms` filter on `place_id` (inverted-index lookup, fast for thousands of IDs)
   - Optional filters: `ccodes` (country codes), `bounds` (geo_shape intersects), temporal range (nested timespans), `exclude_namespaces`
   - Aggregations on `types.identifier` (nested) and `ccodes` for faceted UI
   - `geom` parameter controls geometry detail: `"full"` or `"repr_point"` only

3. **Enrichment** — fetch full toponym inventory + cluster membership:
   - `build_toponym_lookup`: `terms` filter on `attestations` for surviving place_ids → full name inventory (label + lang)
   - `build_cluster_lookup`: membership docs from `clusters` index → `cluster_id` + `cluster_size` for prominence ranking

**Response ranking:** Results are sorted by normalised toponym-match score (0–100), with cluster_size as tiebreaker.

### Search modes

| Mode | Discovery method | Query fields |
|------|-----------------|--------------|
| `exact` | BM25 | `name.keyword` (term) |
| `starts` | BM25 | `name.keyword` (prefix) + `name.prefix` (edge_ngram) |
| `in` | BM25 | `name.raw` (wildcard) |
| `fuzzy` | Symphonym KNN | 128-d embedding, k=200, similarity≥0.7 |
| `phonetic` | Symphonym KNN | same as fuzzy |

### Suggest (`GET /api/suggest`)

Lightweight typeahead querying only the `toponyms` index via `name.prefix`
(edge_ngram) + `name.raw` (exact keyword boost). Returns deduplicated name
strings — no filters, no place lookups.

### Configuration (`gateway/config.py`)

Index names use wildcard/alias patterns for dated indices:
`TOPONYMS_INDEX = "toponyms_*"`, `PLACES_INDEX = "places_*"`,
`CLUSTERS_INDEX = "clusters"`. ES password is read from
`{IX1_BASE}/es/config/elastic.password`.

---

## Authority Sources

| Namespace | Source | Records | Type vocabulary |
|-----------|--------|---------|-----------------|
| `gn` | GeoNames | ~13M | Feature codes (e.g. `PPL`, `ADM1`); label = feature class (`P`, `A`, etc.) |
| `osm` | OpenStreetMap | ~18M | OSM tag keys (`place`, `natural`, `water`, `waterway`, `historic`, `landuse`); identifier = tag value (e.g. `city`, `river`) |
| `wd` | Wikidata | ~11M | P31 Q-items (e.g. `Q515` = city) |
| `tgn` | Getty TGN | ~3M | Currently generic `place`; should carry AAT type IDs |
| `pl` | Pleiades | ~37K | Pleiades place type vocabulary (string labels like `settlement`, `fort`, `temple`) |
| `gb` | GB1900 | ~1.2M | None currently |
| `loc` | Library of Congress | Relations only | N/A |
| `nl` | Native Land | ~4K | Territory/language/treaty types |
| `dp` | D-PLACE | ~2.6K | Language point data |
| `iv` | Index Villaris | ~24K | Historical gazetteer |
| `un` | ISO3166 countries | ~257 | Country entities |
| `ohm` | OpenHistoricalMap | ~800K | Same tag schema as OSM; excellent temporal coverage (`start_date`/`end_date`) |

---

## Type System (`typesystem/`)

### Overview

The type system manages a canonical **Getty AAT** (Art & Architecture
Thesaurus) place-type hierarchy and cross-vocabulary mappings. The ES `types`
index serves the type-tree widget in the Django search UI and enables
hierarchical type filtering in search queries. Multilingual AAT labels and
scope notes are harvested and stored for future internationalisation.

### Module structure

| Module | Role |
|--------|------|
| `aat_config.py` | AAT entry points, excluded subtrees, fclass map, SPARQL/API URLs |
| `sync_aat_types.py` | Download AAT N-Triples dump → parse hierarchy → index to ES |
| `aat_mapper.py` | Augment `typesystem/data/*.json` with AAT mappings (static, SPARQL, Wikidata bridge) |
| `merge_mappings.py` | Write cross-vocabulary fields from data files into ES types docs |
| `tree_api.py` | FastAPI router for type-tree widget (`/api/types/tree`, search, descendants) |
| `build_geonames_types.py` | Build `typesystem/data/geonames.json` from featureCodes_en.txt |
| `build_wikidata_types.py` | Build `typesystem/data/wikidata.json` via ES P31 aggregation + Wikidata API |
| `build_pleiades_types.py` | Build `typesystem/data/pleiades.json` from Pleiades vocabulary (includes AAT same_as) |

### Type data files (`typesystem/data/`)

| File | Source | Structure |
|------|--------|-----------|
| `osm.json` | OSM TagInfo API | Keyed by tag key → values with counts |
| `ohm.json` | OHM Overpass API | Same structure as OSM |
| `geonames.json` | GeoNames featureCodes_en.txt | Keyed by feature class (A, H, P, etc.) → codes |
| `wikidata.json` | ES aggregation + Wikidata API | Flat list of P31 Q-items with labels |
| `pleiades.json` | Pleiades vocabulary API | Flat list with `same_as` AAT URIs |

Each value entry can carry an `aat_mapping` dict:
`{aat_id, aat_term, confidence, source}`.

### AAT mapping pipeline

```bash
# 1. Build type vocabulary files (one-time or after re-ingestion)
python -m typesystem.build_geonames_types
python -m typesystem.build_wikidata_types --es-host URL
python -m typesystem.build_pleiades_types

# 2. Apply curated static AAT mappings to all data files
python -m typesystem.aat_mapper static

# 3. Bridge Wikidata → AAT via P1014 property
python -m typesystem.aat_mapper wikidata

# 4. SPARQL label matching for remaining unmapped entries
python -m typesystem.aat_mapper sparql

# 5. Validate AAT IDs against the Getty API
python -m typesystem.aat_mapper validate

# 6. Report coverage
python -m typesystem.aat_mapper report
```

### ES types index pipeline

```bash
# 1. Sync AAT hierarchy → ES types index
python -m typesystem.sync_aat_types --es-host URL              # bulk N-Triples
python -m typesystem.sync_aat_types --es-host URL --api        # JSON API fallback

# 2. Merge cross-vocabulary mappings into ES
python -m typesystem.merge_mappings --es-host URL
```

### Legacy code (`typesystem/AAT_legacy/`)

The `AAT_legacy/` directory preserves the original Django app (`placetypes`)
used on the DigitalOcean VM. Key files:
- `management/commands/sync_aat_types.py` — 780-line Django management command
  (ported to standalone `typesystem/sync_aat_types.py`)
- `aat_utils.py` — tree widget and search utilities (ported to `typesystem/tree_api.py`)
- `models.py` — Django `Type` model with materialized path
- `views.py` — JSON endpoints for tree widget

### Native type vocabularies (per authority)

- **GeoNames**: `{identifier: "PPL", label: "P", sourceLabel: "P.PPL"}`
- **Wikidata**: `{identifier: "Q515", label: "wikidata", sourceLabel: "Q515"}`
- **OSM**: `{identifier: "city", label: "osm", sourceLabel: "place=city"}`
  - Currently extracts from 6 tag keys: `place`, `natural`, `water`, `waterway`, `historic`, `landuse`
  - 11 additional keys identified for expansion: `amenity`, `tourism`, `leisure`, `man_made`, `boundary`, `military`, `building`, `aeroway`, `railway`, `geological`, `power`
  - `boundary=administrative` will be **dual-indexed**: into the `places` index
    (searchable as places) **and** a separate `boundaries` index (feeding the
    Space filter in the search UI)
  - Raw TagInfo data in `typesystem/data/osm.json`; detailed inventory in
    `developer/osm-types-inventory.md`
- **OHM**: `{identifier: "city", label: "ohm", sourceLabel: "place=city"}`
  — Same tag schema as OSM; excellent temporal coverage (`start_date`/`end_date`).
  Raw data in `typesystem/data/ohm.json`; inventory in `developer/ohm-types-inventory.md`.
- **Pleiades**: `{identifier: "settlement", label: "pleiades", sourceLabel: "settlement"}`
  — ~151 types already have AAT same_as URIs from the Pleiades vocabulary.
- **TGN**: `{identifier: "place", label: "tgn", sourceLabel: "getty-tgn"}` (not extracting AAT types yet)


---

## Environment & Paths

Configuration is in `.env` (root) and `processing/settings.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `IX1_BASE` | `/ix1/ishi` | Primary CRC storage |
| `IX3_BASE` | `/vast/ishi` | Fast CRC storage |
| `DATA_DIR` | `${IX1_BASE}/data` | Authority data files |
| `ES_HOST` | Auto-detected from staging info file | Staging ES URL |
| `PROD_ES_URL` | `http://localhost:9200` | Production ES URL |
| `BATCH_SIZE` | `5000` | ES bulk indexing batch |

---

## Key Commands

```bash
# Fetch/update authority source files
python -m processing.fetch_authorities                    # skip files < 1 year old
python -m processing.fetch_authorities --age 0            # force refresh all
python -m processing.fetch_authorities -n osm --age 0     # force refresh OSM only

# Ingest authorities into ES
python -m processing.ingest_all_authorities               # all (skip existing)
python -m processing.ingest_all_authorities -n osm -r     # replace OSM
python -m processing.ingest_all_authorities --check-only  # dry run

# Staging ES (via es.sh)
source es.sh -staging-start                               # restore latest snapshot
source es.sh -staging-start --places-only                 # restore only places index
source es.sh -staging-start --no-snapshot                 # empty indices, no snapshot
source es.sh -staging-stop                                # stop staging

# Rebuild toponyms index (PanPhon embeddings)
python -m phonetics.extraction.rebuild_toponyms_index     # full 5-step pipeline

# Symphonym embeddings (GPU)
python -m phonetics.inference.update_es compute ...       # GPU inference → Parquet
python -m phonetics.inference.update_es index ...         # DuckDB + embeddings → ES

# Run clustering
es -cluster --full --slurm          # full run via Slurm
es -cluster --incremental           # incremental on VM
es -cluster --stats                 # show statistics

# Gateway
python -m gateway                   # production
uvicorn gateway.app:app --reload    # development

# Type system — build vocabulary files
python -m typesystem.build_geonames_types                      # fetch GeoNames feature codes
python -m typesystem.build_wikidata_types --es-host URL        # aggregate P31 Q-items from ES
python -m typesystem.build_pleiades_types                      # fetch Pleiades vocabulary

# Type system — AAT mapping
python -m typesystem.aat_mapper static                         # curated static mappings
python -m typesystem.aat_mapper wikidata                       # Wikidata → AAT via P1014
python -m typesystem.aat_mapper sparql                         # SPARQL label matching
python -m typesystem.aat_mapper report                         # coverage stats

# Type system — ES types index
python -m typesystem.sync_aat_types --es-host URL              # AAT hierarchy → ES
python -m typesystem.merge_mappings --es-host URL              # cross-vocab fields → ES

# Fetch OSM/OHM tag statistics
python scripts/fetch_osm_taginfo.py                       # OSM TagInfo → typesystem/data/osm.json

# Slurm jobs
sbatch processing/refresh_authorities.slurm
sbatch processing/es_staging.sbatch
```

---

## Development Notes

- **SSH access to production ES**: The production ES instance runs on the CRC
  VM (`ssh pitt`). ES listens on `localhost:9200` (gateway) / `localhost:9201`
  (direct) and requires HTTP Basic auth (`elastic` user). The password is
  stored at `/ix1/ishi/es/config/elastic.password`. Example ad-hoc query:
  ```bash
  ssh pitt 'ES_PASS=$(cat /ix1/ishi/es/config/elastic.password); \
    curl -s -u "elastic:${ES_PASS}" "http://localhost:9200/places_20260317/_search" \
    -H "Content-Type: application/json" -d '"'"'{"size":1}'"'"''
  ```
  Note: the gateway on port 9200 also requires auth; port 9201 is the direct
  ES backend. Index names are dated (e.g. `places_20260317`,
  `toponyms_20260317`). Use `_cat/indices` to discover current names.
- **Python 3.11+** required (uses `str | None` union syntax, match statements).
- Key dependencies: `elasticsearch`, `httpx`, `fastapi`, `uvicorn`, `pydantic`,
  `osmium`, `ijson`, `orjson`, `shapely`, `torch` (for Symphonym).
- All authority scripts are designed to be idempotent (use `_id` = `place_id`).
- The `places` index uses `refresh_interval: -1` during bulk ingestion for
  performance; refresh manually after ingestion.
- The `hf/` directory contains the Symphonym model for HuggingFace deployment.

### Toponym pipeline (two-stage embedding)

The toponyms index is built in two separate stages, each producing different
embeddings:

1. **PanPhon 192-d embeddings** — computed by
   `phonetics/extraction/rebuild_toponyms_index.py` (5-step pipeline):
   ES places → DuckDB → vocabulary → JSONL with IPA + PanPhon embeddings → ES.
   IPA backends: Epitran (many scripts), Phonikud (Hebrew), CharsiuG2P
   (Mandarin, Korean, Cantonese, Gan, Wu).

2. **Symphonym 128-d int8 embeddings** — computed separately by
   `phonetics/inference/update_es.py` in two modes:
   - `compute`: training Parquet → GPU inference → embeddings Parquet
   - `index`: DuckDB + embeddings Parquet → full ES toponyms index rebuild

After authority re-ingestion, both stages must be re-run for a clean rebuild.

### Re-ingestion workflow (e.g. OSM expansion)

To cleanly re-ingest an authority (e.g. adding new OSM tag keys):
1. Delete existing `osm:` docs from the `places` index
2. Re-ingest with the updated authority script
3. Rebuild the toponyms index (stage 1: PanPhon) — this also cleans up
   orphaned attestations
4. Recompute Symphonym embeddings (stage 2)
5. Re-run clustering

---

## Codebase Conventions

- Authority scripts live in `authorities/` and are named `{source}-places.py`
  or `{source}-{update-type}.py`.
- All ES documents use namespaced `place_id` values: `{namespace}:{source_id}`.
- Toponym IDs use LST format: `{name}@{lang}` (e.g. `London@en`, `Лондон@ru`).
- Type documents use `{identifier, label, sourceLabel}` where `label` indicates
  the source vocabulary (e.g. `osm`, `wikidata`, `pleiades`, `P` for GeoNames
  feature class).
- Geometry documents include both full `geom` (geo_shape) and `repr_point`
  (geo_point centroid) for efficient spatial queries.

