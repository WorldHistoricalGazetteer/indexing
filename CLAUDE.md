# CLAUDE.md — Agent Briefing for WHG Indexing Repository

> **Repository:** `WorldHistoricalGazetteer/indexing`
> **Last updated:** 2 September 2026

---

## ⚠️ READ FIRST — work in progress (31 August 2026)

**[`developer/HANDOVER-2026-08-31-rebuild-audit.md`](developer/HANDOVER-2026-08-31-rebuild-audit.md)**

✅ **FOUND AND FIXED 2 Sep — no live `h3_cover` defect remains.** For most of
2 Sep production *was* serving hull-derived covers: `clio` 3,522 of 15,683
(22.5%) and `whg` 1,746 of 2,565 (68.1%), **5,268 geometries**, written by
`h3ccode-20260805T120000Z` — the run behind the live index — so unlike `un`'s
they shipped. **79% of the damage was UNDER-covering**, i.e. `containment=fuzzy`
silently omitting places that should have matched: a false negative nobody
reports. **Remediated the same day** (§2.11): 5,268 `_bulk` updates, 0 errors,
re-census **0 defective of 18,248 examined** — the whole frame, so no regression
among the 12,980 already correct. Rollback retained at
`/vast/ishi/elastic/logs/s9_rollback_geometries.json`.

⚠️ **Two things this did NOT fix**, both open in
[`plan-completion-2026-08-31.md`](developer/plan-completion-2026-08-31.md):
248 `whg` `point`-class geometries whose `geom_class` may be wrong (they now
have correct covers *for their stored polygons*); and a **top-level
`h3_cover`** that is stale, diverged from the nested truth, and read by nothing
— **`geometries.h3_cover` is the real one.**

The July/August re-ingestion is complete in production — 51.2 M
places, 72.7 M toponyms, Symphonym embeddings at 100%. Its *publication* half is
not: a partial retile on 7 August ran against the geom store while it was
destroyed, so **nine gazetteer boundary layers are on the live map today as
points with no polygons** (`clio`, `kain_par`, `po`, `vob_lgd`, `vob_rd`,
`hgis`, `vob_rc`, `vob_cty`, `ukhc`). That document is the authoritative
statement of what landed, what is left, and in what order — every line of it
measured against the live indices and the deployed tilesets rather than against
a manifest or a plan's own claim.

⚠️ **Before retiling anything, read its §3.1.** ~~The geom store holds **0**
`un` geometries, so retiling `un` today would replace the country boundaries
with points.~~ ✅ **FIXED 31 Aug and re-verified 2 Sep — the store now holds all
247 `un` geometries** (job 11074309; `index.sqlite` `un:` keys = 247 = the full
`un` corpus; bounds delta 0.0 against the live index). **`un` is no longer a
retile blocker.** The rule the warning encodes still stands, and §3.1 is still
required reading: **a tile job that reports success is not evidence it read any
geometry** — check the store's per-namespace key count before retiling a
boundary layer, rather than trusting the job's exit status.

Why these faults keep recurring, grouped into classes with the permanent code
fix for each, is
[`developer/postmortem-ingestion-faults.md`](developer/postmortem-ingestion-faults.md)
— **read it before writing pipeline code**, not only after something breaks.
Nine of the thirteen originally-registered faults are one fault — eleven of
sixteen once this campaign's are added: *a required input is absent,
something plausible is substituted, and the stage reports success.*

The ordered remediation is
[`developer/plan-completion-2026-08-31.md`](developer/plan-completion-2026-08-31.md).
It is meant to be worked **one session per row of its Session map** — find your
session there first, and update its status before the session ends.

Its predecessor, [`HANDOVER-2026-08-09-geom-store.md`](developer/HANDOVER-2026-08-09-geom-store.md),
remains the record of the geom-store loss and rebuild; where the two disagree
about the retile, the 31 August audit is the measured one.

The shortest version, if you read nothing else:

* **Never run `python -m unittest discover -s tests`** against real settings. It
  skips `tests/__init__.py`, so the sandbox is never installed and the writing
  tests target the real `/vast` paths — that is exactly what destroyed the store
  and stubbed `gn`/`wd` staging. Safe forms: `python -m unittest tests.test_module`
  (package-qualified), or `discover -s tests -t .`. A guard now refuses the
  dangerous case, but don't rely on it.
* `crc0` is the **smp login node** — never run pipeline compute there. Submit to
  Slurm with `sbatch -M htc`; query with `squeue -M htc` / `sacct -M htc`.
* **A tile job that reports success is not evidence it read any geometry.** Check
  the `poly=` count in its log and the polygon count in the built tileset — the
  7 August run printed `geom-store: opened … (2 entries)`, streamed `poly=0` for
  every bucket, and deployed.

---

## Project Overview

This repository powers the **World Historical Gazetteer (WHG)** search
infrastructure — a system that indexes ~51.2 million place records from multiple
independent gazetteers into Elasticsearch, clusters co-referent records, and
serves search/reconciliation queries via a FastAPI gateway.

WHG is an NEH-funded digital humanities project. The production ES instance
runs on the University of Pittsburgh Center for Research Computing (CRC), with
a Django front-end on DigitalOcean proxying to the CRC gateway.

## Agent Runtime Paths (CRC)

When operating on CRC via `ssh crc0`, agents should use these canonical paths:

- **Repository root:** `/vast/ishi/elastic` (relocated 2026-05-01 from
  `/ix1/ishi/elastic` to put Python imports / log writes / Slurm WorkDir on
  flash storage; the `/ix1` NFS volume was buckling under the small-file
  read pattern from concurrent boundary/tile workers).
- **Conda activation script:** `/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh`
- **Conda env name:** `whg`

Example activation sequence:

```bash
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
cd /vast/ishi/elastic
```

**What stays on `/ix1`** (deliberately, to keep `/vast` free for ES):

- Authority data files: `/ix1/ishi/data/authorities/*` (the 92 GB OSM PBF, etc.)
- Credentials: `/ix1/ishi/secrets/`, `/ix1/ishi/es/config/`
- Snapshot exchange: `/ix1/ishi/snapshots/`

The boundary planner reads the OSM PBF from `/ix1` once per rebuild (its
prefilter output is persisted to `/vast` so workers never read `/ix1`).

**Kibana binaries live on `/vast`** (`KIBANA_HOME=${IX3_BASE}/kibana-bin`,
i.e. `/vast/ishi/kibana-bin`, moved there 2026-05-20). Loading Kibana's ~173
plugins is thousands of small-file reads — pathologically slow on the `/ix1`
NFSv4 mount (~30 min cold start, process pinned in `D`/I-O-wait) versus ~60s
from `/vast` flash. The same small-file pathology is why ES data lives on
`/vast`. Only `kibana-bin` moved; Kibana's `path.data` and PID file stay on
`/ix1` (small, infrequent I/O).

---

## Architecture

```
Browser → Django (DigitalOcean) → CRC Gateway (FastAPI) → Elasticsearch 9.x (CRC)
                                                            ├── places   (51.2M docs)
                                                            └── toponyms (72.7M docs)
                                                     (clusters: RETIRED, see below)
```

- **Gateway** (`gateway/`): FastAPI app on port 9200 (the only port open
  through the CRC firewall). Routes Kibana traffic by `Host` header, proxies
  all other HTTP to ES on localhost:9201. Custom endpoints under `/api/`:
  - `POST /api/search` — full filtered search (3-step: discovery → filtering → enrichment).
    Supports **spatial containment** via `contained_in` (place_ids defining the region) or
    `bounds` (raw GeoJSON), with `containment` = `fuzzy` (H3) | `exact` (Shapely) and
    `relation` = `intersects` | `within`. `query` is optional — omit for a pure-spatial query.
    `fuzzy`/`phonetic` discovery blends TWO lexical passes with the phonetic KNN —
    see `/api/reconcile` below; search has no `variants`, so only `query` is looked up.
    **Scope fails closed here too, since 31 Aug 2026** — a `contained_in` that
    resolves to no usable geometry returns 0 hits plus a `scope` saying why,
    where it used to return the byte-identical *unscoped* result set with no
    `scope` at all (audit §2b). `ScopeInfo` and its builder live in
    `gateway/spatial.py` and are shared with `/api/reconcile`: the two endpoints
    answered this differently for four months because each owned a copy.
    ⚠ A scope can also apply at the **wrong precision**: `hit_matches` degrades
    `containment=exact` to the H3 test whenever no polygon is behind the region
    (geom-store miss, no reader, no Shapely, or an `h3-disc` radial region).
    That is now reported as `scope.approximate=true` with the reason — before
    31 Aug 2026 it was detectable only by noticing that exact and fuzzy returned
    identical counts, which is how `un` served cell-accurate country containment
    for three weeks unnoticed.
  - `GET /api/suggest` — fast typeahead on toponyms index
  - `POST /api/reconcile` — reconciliation search (same 3-step architecture; same
    `contained_in`/`containment`/`relation` spatial-containment params as `/api/search`).
    Optional `variants[]` (alt name forms) are tried alongside `query` in discovery.
    In `fuzzy`/`phonetic` modes each form is a **separate** KNN pass, so each pass is
    normalised by its own top score before the union takes the per-place max — raw
    cosines to different query vectors are not comparable, and comparing them let a
    variant's junk neighbours outrank (and evict) the correct match (place#197).
    Those modes also run **two lexical passes** over the primary + variants in the
    same round trip, because KNN answers only "what sounds like this":
    * **exact** (case-insensitive, on `name.raw`) — KNN demonstrably misses
      toponyms spelled *exactly* as asked (`Newton with Scales` is indexed yet
      never entered the 200-candidate KNN pool). Earns `LEXICAL_EXACT_BOOST`
      (2.5), deliberately above the phonetic ceiling of 1.0.
    * **near-miss** (`multi_match`, `fuzziness: AUTO`) — scored in Python on how
      much the retrieved name really resembles a queried form
      (`name_resemblance`, ≥ `LEXICAL_FUZZY_FLOOR`), earning up to
      `LEXICAL_FUZZY_BOOST` (0.75). Without it a *variant* could only ever help
      by being spelled exactly as indexed, which is the opposite of what variants
      are for now that Map your Data derives them (place#188/#199): `Broxbourn`
      finds Broxbourne perfectly as a query and contributed nothing at all as a
      variant.
    Both are **added** to any phonetic score, so a place found several ways
    outranks one found a single way and phonetic proximity survives as the
    within-tier tiebreak. The tiers are strictly ordered — exact (≥ 1.8) >
    near-miss + phonetic (≤ 1.75) > phonetic alone (≤ 1.0). Side-effect worth
    knowing: when an exact match exists the phonetic band falls to ~half of it, so
    junk drops well below Map your Data's auto-confirm threshold instead of
    riding at 99.
    **Bracketed and comma-qualified queries are re-read server-side**
    (`derive_name_forms`, capped at `MAX_DERIVED_FORMS`): a parenthetical
    qualifier is the asker's apparatus and no toponym is indexed with it, so
    `Broxbourn (St. Augustine)` also runs as `Broxbourn` and `Broxbourn St.
    Augustine`; and the `Place, County` shape of gazetteer columns also runs as
    its head word and its inversion — `Bury St. Edmunds, Suffolk` → `Bury St.
    Edmunds` + `Suffolk Bury St. Edmunds` (place#205). Both readings, always,
    because the string cannot say which it is (`Melford, Long` wants the
    inversion, `Bury St. Edmunds, Suffolk` wants the head). Each is a full pass
    (KNN + both lexical tiers), **graded by how much of the query it keeps**
    (`derived_form_weight`): a rearrangement that preserves every token is worth
    `VARIANT_SCORE_WEIGHT` (0.9), a truncation `DERIVED_LOSSY_WEIGHT` (0.8).
    Without that grading `Melford` took rank 1 from `Long Melford` — and a
    resemblance tiebreak makes it *worse*, since `difflib` rewards the shared
    prefix (0.70 vs 0.56) and cannot see that tokens were reordered not dropped.
    An **unpunctuated** trailing qualifier is handled too — `Bury St Edmunds
    Suffolk` → `Bury St Edmunds`, `Kingston Surrey` → `Kingston` — but only when
    the trailing phrase *names an administrative unit* (`gateway/data/
    place_qualifiers.json`, built by `processing.build_place_qualifiers` from the
    ukhc variants + ISO countries). ⚠ The morphological guard tried first ("don't
    drop the last word after *upon*/*on*/*le*/*St*") **fired on 82.7% of 1,178
    real 3+-word toponyms** — the index is global and `Tamarack Creek Spring` is
    an ordinary name. The vocabulary test fires on 0.4% and is strictly more
    capable. Known limit: British counties + countries only, so a trailing US
    state or French département is not yet recognised.
    Deduped against the caller's own `variants` (so Map
    your Data, which derives these client-side since place#188, pays nothing) and
    held inside the `MAX_VARIANTS` budget. Reported back as `derived_forms[]`,
    distinct from `variants_used[]`. `/api/search` has the same derivation —
    it is the caller that CAN'T send variants, so it needed it most.
    ⚠ Form weights have a hard **floor**, not a preference: the tier
    ordering needs `LEXICAL_EXACT_BOOST × weight > LEXICAL_FUZZY_BOOST + 1.0`,
    i.e. > 0.7 at the current boost. `LEXICAL_EXACT_BOOST` was raised 2.0 → 2.5
    (place#205) precisely to open room beneath 0.9 for grading derived forms —
    at 2.0 the floor was 0.875 and no discount was expressible.
    Every tier being absolute is what lets each hit carry **`confidence`** (0–100,
    fuzzy/phonetic only) beside the pool-relative `score`, which is ~100 for the
    top hit even when nothing matched (place#198). ⚠ Do NOT try to derive
    confidence from the KNN cosine: measured 2026-08-20, genuine cross-script
    matches (`Marsails → مارساليس` 0.9878) sit *inside* the junk band, so no
    cosine threshold separates them — see `knn_pass_quality`.
    Requested scope is **never silently dropped**: a point-only container borrows
    a `sameAs` co-referent's polygon, and a scope that cannot be applied at all
    **fails closed** (no hits) rather than answering unscoped — either way the
    response's `scope` object records exactly what was applied. Both the model
    and the builder (`spatial.ScopeInfo` / `spatial.build_scope_info`) are shared
    with `/api/search`; don't reintroduce a per-endpoint copy.
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

⚠️ **RETIRED — these commands no longer exist.** `-cluster`, `-cluster-finalize`
and `-augment-ccodes` were removed from the dispatcher (`scripts/es.sh:1113`),
and **`scripts/cluster.sh` is not in the repository.** Verified 2 Sep 2026:
`ls scripts/*.sh` returns 21 files without it, and grepping the case labels
returns nothing. What replaced them:

* **ccode enrichment** → staged: `processing/ccode_enrichment.py` via
  `processing/submit_ccode_slurm.py`.
* **clustering** → the SQLite hard-link overlay built by
  `processing/submit_hardlinks_slurm.py` and queried at search time, plus
  client-side Union-Find in the browser. The `clusters` **index** is legacy
  (see Key Indices) and `CLUSTERS_INDEX` is gone from `gateway/config.py`.

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

## Key Indices (ES 9.x)

| Index | Schema | Content |
|-------|--------|---------|
| `places` | `schemas/places.json` | **51,187,900** place records with nested toponyms, geometries, types, relations (measured 2 Sep 2026) |
| `toponyms` | `schemas/toponyms.json` | **72,703,777** deduplicated toponym records with Symphonym embeddings and attestation lists (measured 2 Sep 2026) |
| `clusters` | `schemas/clusters.json` | ⚠️ **LEGACY / being retired** — static offline cluster membership (`clusters_20260325`). Superseded by **client-side clustering**: the gateway ships hard-link edges + per-hit signal fuel and the browser (`clustering.js`) runs Union-Find at a user θ. See `developer/plan-outstanding-2026-07.md` §1. |
| `types` | `schemas/types.json` | AAT place-type hierarchy with cross-vocabulary mappings, fclasses, materialized paths, multilingual labels/notes |

### Places schema (key fields)

Field definitions are in `schemas/places.json`.

---

## Gateway Architecture (`gateway/`)

### Module structure

| Module | Role |
|--------|------|
| `app.py` | FastAPI application, lifespan (pre-warms Symphonym), health endpoint, phonetic search + embed endpoints, catch-all HTTP/WebSocket proxy |
| `reingest.py` | `POST /api/registry/reingest` + `GET /api/registry/reingest/{job_id}` — admin-triggered Slurm submission of `scripts/reingest.sbatch <namespace>` via SSH from the Pitt VM to a CRC login node. No bearer auth (Pitt firewall whitelists DO IP). Concurrency guard via `squeue -n reingest-<ns>` — returns 409 with the existing job_id so Django can adopt. |
| `config.py` | Loads `.env`; exports `ES_BACKEND` (localhost:9201), `KIBANA_BACKEND` (localhost:5601), index **aliases** (`places`, `toponyms` — not wildcards; `CLUSTERS_INDEX` is gone), Symphonym model dir |
| `search.py` | `POST /api/search` and `GET /api/suggest` — the main search router |
| `reconcile.py` | `POST /api/reconcile` — reconciliation search (same 3-step architecture as search) |
| `es_helpers.py` | Shared ES query builders: `build_toponym_query`, `build_phonetic_knn`, `collect_place_ids`, `build_places_filter`, `build_toponym_lookup`, `build_suggest_query`; geometry extractors `extract_place_geoms`/`extract_repr_point` |
| `spatial.py` | Spatial-containment engine (no reindex): `resolve_region` (place_ids → region), `region_from_geojson` (bounds → region), `apply_containment` (fuzzy H3 / exact Shapely). H3 prefilter + Shapely `prep()` refine ported from `processing/ccode_enrichment.py`; exploits `repr_point ∈ geom` guarantee. Used by `search.py` + `reconcile.py` Step 2.5. A container with no polygon of its own borrows the boundary of a `sameAs`/`exactMatch` co-referent (`source="linked-polygon"` — e.g. point-only `gn:3017382` France → `wd:Q142`, via `hard_link_expansion`); **no geometry is ever synthesised**, so when no real boundary exists `resolve_region` returns None and the caller must fail closed rather than run unconstrained (place#144). |
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

3. **Enrichment** — fetch full toponym inventory (+ legacy cluster membership):
   - `build_toponym_lookup`: `terms` filter on `attestations` for surviving place_ids → full name inventory (label + lang; optional per-name `phon_emb` when `include_embeddings`)
   - ~~`build_cluster_lookup`~~ **RETIRED 2026-07-12** — the `clusters`-index membership join (`cluster_id`/`cluster_size`) is removed from `search.py` + `reconcile.py` (client-side clustering replaces it; verified no consumer read those fields). Prominence tiebreaker is now name-variant count.

**Clustering fuel (opt-in, additive):** `include_hard_links` → result-set `edges[]`; `include_clustering_fields` → per-hit `h3`/`h3_cover`/`temporal_range`/`aat_ids`/`aat_paths`/`query_match` + top-level `clustering_params`/`toponym_stoplist`; `include_embeddings` → per-name int8 `phon_emb`. The browser (`clustering.js`) computes all pair signals + Union-Find. See `developer/plan-outstanding-2026-07.md` §1.

**Source-attribution echo (place#157):** every multi-record response
(`/api/search`, `/api/reconcile`, `/api/places`) carries `namespaces[]` — the
distinct authorities represented in its results — so Django resolves per-source
licence terms in one registry lookup instead of string-splitting every result id.
`/api/search` + `/api/reconcile` also carry `namespaces_searched[]` (the explicit
positive namespace scope of the request, echoed even on empty responses): the
only way to see that a namespace was queried but matched nothing, which
id-derivation cannot express. Both default to `[]`; `gateway.es_helpers.collect_namespaces`
is the shared builder. Licence data itself originates in `processing.settings.AUTHORITIES`
(+ `CUSTOM_LICENCES` for bespoke non-SPDX terms) — verify it actually landed in
the registry with `python -m processing.verify_licences`, because the registry
endpoint silently skips a `license_spdx` its own License table doesn't know.

**Response ranking:** Results are sorted by normalised toponym-match score (0–100), with `cluster_size` as tiebreaker (legacy — to be replaced when `build_cluster_lookup` retires).

### Search modes

| Mode | Discovery method | Query fields |
|------|-----------------|--------------|
| `exact` | BM25 | `name.keyword` (term) |
| `starts` | BM25 | `name.keyword` (prefix) + `name.prefix` (edge_ngram) |
| `in` | BM25 | `name.raw` (wildcard) |
| `fuzzy` | Symphonym KNN **+ exact + near-miss lexical passes** | 128-d embedding, k=200, similarity≥0.7; plus `name.raw` terms; plus `name`/`name_romanized`/`name.prefix` fuzzy |
| `phonetic` | Symphonym KNN **+ exact + near-miss lexical passes** | same as fuzzy |

### Suggest (`GET /api/suggest`)

Lightweight typeahead querying only the `toponyms` index via `name.prefix`
(edge_ngram) + `name.raw` (exact keyword boost). Returns deduplicated name
strings — no filters, no place lookups.

### Configuration (`gateway/config.py`)

The gateway queries **aliases**, not wildcards: `PLACES_INDEX = "places"`,
`TOPONYMS_INDEX = "toponyms"` (defaults in `gateway/config.py`, matching
`.env`). `CLUSTERS_INDEX` is **gone** — clustering is client-side.

This matters when retiring a superseded index. Under the old `places_*`
wildcard a second dated index would silently join every query; under an alias it
cannot, so several generations can coexist safely and a cutover is one atomic
alias re-point. Before deleting one, still confirm it holds no alias **and**
that a SUCCESS snapshot exists (`_snapshot/staging_repo/_all`) — deletion is
otherwise irreversible.

ES password is read from `{IX1_BASE}/es/config/elastic.password`.

---

## Authority Sources

| Namespace | Source | Records | Type vocabulary |
|-----------|--------|---------|-----------------|
| `gn` | GeoNames | ~13M | Feature codes (e.g. `PPL`, `ADM1`); label = feature class (`P`, `A`, etc.) |
| `osm` | OpenStreetMap | **20.6M** | OSM tag keys (`place`, `natural`, `water`, `waterway`, `historic`, `landuse`); identifier = tag value (e.g. `city`, `river`) |
| `wd` | Wikidata | ~11M | P31 Q-items (e.g. `Q515` = city) |
| `tgn` | Getty TGN | ~3M | Currently generic `place`; should carry AAT type IDs |
| `pl` | Pleiades | ~37K | Pleiades place type vocabulary (string labels like `settlement`, `fort`, `temple`) |
| `gb` | GB1900 | ~1.2M | None currently |
| `loc` | Library of Congress | Relations only | N/A |
| `nl` | Native Land | ~4K | Territory/language/treaty types |
| `ukhc` | UK Historic Counties (Historic County Borders Project) | 92 | `historic-county` (polygon boundaries; `boundary=historic-county`; end 1974, Welsh counties start 1542, others open) |
| `dp` | D-PLACE | ~2.6K | Language point data |
| `iv` | Index Villaris | ~24K | Historical gazetteer |
| `un` | ISO3166 countries | **247** | Country entities |
| `ohm` | OpenHistoricalMap | **945K** | Same tag schema as OSM; excellent temporal coverage (`start_date`/`end_date`) |
| `chgis` | CHGIS (China Historical GIS) | ~81K | CHGIS feature-type names (`identifier`); AAT-mapped |
| `tm` | Trismegistos | ~64K | Ancient-world place references |
| `ofs` | Ottoman NFS Gazetteer (Kabadayı et al. 2022) | ~16.3K | ~16.3K mid-19thC populated places from Ottoman population registers (1830–1849) |
| `clio` | Cliopatria (Seshat) | ~15.7K | Polities (`polity`); no AAT yet |
| `whg` | Contributed WHG datasets (`authority=True`) | **228,918** across **48 datasets** (both measured live 2 Sep 2026; was recorded here as ~14.2K / "7 datasets live" — a **16× understatement**) | Mixed LPF. ⚠️ The dataset count is **not pipeline-derived** — a contributor publishing an `authority=True` public dataset moves it with no run. Refresh from the index, not from here: `cardinality` on `dataset_id` over a `whg:` prefix (there is **no** `dataset` field; the id is `whg:<dataset_id>:<src_id>`) |
| `hgis` | HGIS de las Indias | ~14.1K | Spanish-American historical admin units |
| `po` | PeriodO | ~9.0K | Periods with spatial coverage (`period`); geo-enriched |
| `og` | Ottoman Gazetteer (ottgaz, Hanley/FSU) | ~6.3K | Ottoman admin units (eyalet/vilayet/sancak/kaza/nahiye) |
| `dgsd` | Digital Gazetteer of the Song Dynasty | ~3.8K | Song-dynasty (Chinese) places |
| `alc` | Alcedo / TopUrbi | ~18.2K | Colonial Spanish-American gazetteer |

---

## Type System

The AAT type-system pipeline (vocabulary building, AAT mapping, ES `types` index sync,
`scripts/types.sh`) is documented in the **type-system** skill — invoke it when working on
place types or AAT alignment.

---

## Environment & Paths

Configuration is in `.env` (root) and `processing/settings.py`.

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

# Incremental single-namespace add to the LIVE index (no full rebuild; see workflow below)
python -m processing.index_namespace --namespace ukhc --es-host URL --execute   # places + toponym augment (dry-run by default)
python -m processing.update_tileserver_config --bucket ukhc --execute           # safe tileserver config rewrite + restart + verify
python -m processing.push_gazetteer_inventory --namespace ukhc                   # Django registry upsert (gated on tileset serving)

# Repair h3_cover in the live index in place (one-off remediation)
python -m processing.recompute_h3_index compute --namespaces osm,ohm --of 4 --slice K --out FILE
python -m processing.recompute_h3_index apply   --patch '/path/part.*.jsonl' --rps 1500

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
  ES backend. Index names are dated, and the current generation is
  `*_postbarrier-20260502` behind the `places` / `toponyms` **aliases** — query
  the alias, not a dated name (the example above uses a stale one). Use `_cat/indices` to discover current names.
- **Python 3.11+** required (uses `str | None` union syntax, match statements).
- Key dependencies: `elasticsearch`, `httpx`, `fastapi`, `uvicorn`, `pydantic`,
  `osmium`, `ijson`, `orjson`, `shapely`, `torch` (for Symphonym).
- All authority scripts are designed to be idempotent (use `_id` = `place_id`).
- The `places` index uses `refresh_interval: -1` during bulk ingestion for
  performance; refresh manually after ingestion.
- The `hf/` directory contains the Symphonym model for HuggingFace deployment.
- **Tile-gen banding** reads `tileserver/styles/whg-context/style.json` from the
  sibling **tileboss** repo (`WorldHistoricalGazetteer/tileboss`, branch
  `production`) — clone it next to indexing (`<indexing>/../tileboss`) for
  local dev or set `WHG_STYLE_PATH`; on CRC `processing/tilegen_bands.load_bands`
  falls back to fetching the file from GitHub raw. The generator
  `scripts/build_whg_context_style.py` writes directly into the sibling
  tileboss clone — commit & push from there.

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
5. Rebuild the hard-link overlay (`processing/submit_hardlinks_slurm.py`) —
   **not** `es -cluster`, which no longer exists

### Incremental single-namespace add (one authority, between full rebuilds)

To fold **one** (small) authority into the **live** indices without a full
rebuild — e.g. `ukhc` (UK Historic Counties), added 2026-05-27. **Order matters:**

1. **Fetch + extract** — `fetch_authorities -n <ns>`, then the staging-aware
   authority script (→ staged `extract/` + geom-store staging).
2. **Merge geoms → main store** — `python -m processing.geom_store --merge
   --keep-staging` (incremental; existing shards untouched, `index.json` written
   atomically). Must precede H3 so `h3_stage` polyfills the *real* polygon, not
   the convex hull.
3. **Stage chain → `final/places.parquet`** — `h3_stage` → `h3_merge` →
   `ccode_merge` (an empty `ccode/places.ccode.jsonl` patch passes ccodes
   through untouched; `--allow-missing-patch` does the same with no patch file
   at all). ⚠️ **`ccode_merge` is the ONLY writer of `final/`, so a namespace
   whose ccode stage is skipped must still come through it** — else its
   re-extract stops at `h3_merged/` and the indexer keeps serving the previous
   run's `final/`, which is self-consistent and therefore invisible to the
   freshness gate. That is Fault 12; `submit_ccode_slurm._mark_un_skipped` now
   runs the pass-through for `un` rather than marking `ccode_merge` skipped.
4. **`processing.index_namespace --namespace <ns> --source-stage final
   --execute`** — bulk-indexes places into the concrete index **behind the
   `places` alias** (NOT `index_from_stage`, which builds a *new* index + swaps
   the alias = full-rebuild cutover), and **augments** toponyms (appends
   `place_id` to `attestations` + ns to `namespaces`; **never overwrites the
   embedding**). Dry-run by default; guards against indexing geometries with no
   `h3_cover`.
5. **Aggregates** — `gazetteer_h3_coverage` + `gazetteer_temporal_extent
   --namespace <ns>` (feed the registry push).
6. **Tiles** — register the bucket in `generate_tiles._PER_NAMESPACE_BUCKETS`,
   generate on a compute node, deploy, then **`processing.update_tileserver_config
   --bucket <ns> --execute`** — one safe rewrite of the tileserver `config.json`
   (preserves all other entries; atomic write + timestamped backup + restart +
   serving-verify + auto-rollback on failure).
7. **Registry (LAST)** — **`processing.push_gazetteer_inventory --namespace
   <ns>`** — cumulative single-namespace upsert to the Django gazetteer registry
   (prod + dev); a **preflight gate refuses to push unless the tileset serves**.
8. **`es gateway-restart`** so the gateway re-reads the geom-store index for
   exact containment.

Notes: the prod `places` index references the `extract_namespace` ingest pipeline
— recreate it from `schemas/places_pipeline.json` if a snapshot-restore dropped
it (otherwise writes 400). Secrets (`WHG_API_TOKEN`, `TILESERVER_SSH_KEY`) live in
the gitignored **`.env.local`**, never the tracked `.env`.

---

## Codebase Conventions

- Authority scripts live in `authorities/` and are named `{source}-places.py`
  or `{source}-{update-type}.py`.
- All ES documents use namespaced `place_id` values: `{namespace}:{source_id}`.
- Toponym IDs use LST format: `{name}@{lang}` (e.g. `London@en`, `Лондон@ru`).
- Type documents use `{identifier, label, sourceLabel}` where `label` indicates
  the source vocabulary (e.g. `osm`, `wikidata`, `pleiades`, `P` for GeoNames
  feature class).
- Full geometries live in the `/vast` geom store (keyed
  `{place_id}_{geometry_index}`), **not** in ES `_source`. The store's key→shard
  index is **`index.sqlite`** (`processing.geom_store.GeomStoreReader` prefers it,
  falling back to the legacy `index.json`). This matters: `json.load()`-ing the
  1.02 GB `index.json` cost ~5.4 GB of RSS, which is why `containment=exact`
  silently never loaded in prod until 2026-07-30 (place#165). `consolidate_geom_store`
  writes both; rebuild the SQLite index alone with
  `python -m processing.build_geom_index_sqlite build` (then `verify`). Each ES `geometries[]`
  entry carries `repr_point` (geo_point, guaranteed *within* the geometry),
  `bounds`, and `h3_cover` / `h3_centroid` — the latter computed from the real
  geom-store polygon (incl. GeometryCollections; large polygons are simplified
  before polyfill) by `h3_stage` / `helpers.compute_h3_fields`.
- Two orthogonal geometry flags (see `schemas/field-notes.md`): **`has_geom`** =
  *retrievability* (is the full geom in the `/vast` store right now?) — the
  original "is the point the full picture?" flag; **`geom_class`** ∈
  `{point,line,area}` = *shape* (computed once at ingest, GeometryCollections
  resolved by members). Key "is it areal / usable as a `contained_in` scope
  region" off `geom_class == "area"`, NOT `has_geom` (a LineString is
  `has_geom:true` but not areal). `geom_class ∈ {area,line} AND NOT has_geom` is
  a standing incomplete-ingestion defect predicate.
- All coordinates are rounded to **6 decimal places** (~0.11 m) at ingestion
  time per RFC 7946, via `round_coordinates()` / `enrich_geometry()` in
  `processing/helpers.py`.  This mitigates storage bloat from pseudo-precision
  in upstream sources.  The constant `COORDINATE_PRECISION` controls this.

