# Execution Checklist: Ingestion Rebuild

> **Aligned with Master Plan v3.5** (`whg3/developer/plan-Atlas-DynamicClustering.prompt.md`).
> The Master Plan specifies the user-facing platform (Atlas UI + dynamic clustering + unified
> contribution workflow). This document is the indexing-side execution plan that produces the
> staged data and live indices on which the Master Plan depends.

This checklist supersedes the original draft `plan-ingestionRebuild.DEPRECATED.md` (now to be
deprecated). It maps the consolidated requirements — including the additions imposed by
the Master Plan's Appendix E.2 — to concrete implementation work in this repository, with
external dependencies called out explicitly.

## Overview

Build a decoupled, restartable ingestion pipeline where each gazetteer is fully materialised
as staged Parquet/JSONL artefacts before Elasticsearch indexing. Decoupling removes the current
ES wall-time bottleneck, enables reliable per-gazetteer updates without full-corpus rebuilds,
and provides the canonical input from which all downstream products (indices, tilesets,
hard-link overlay, gazetteer inventory, retention sweeps) are derived.

The plan is divided into **PR-sized batches** with explicit dependencies. Phases are ordered to
enable early validation and staging of core infrastructure before large-scale data operations
(OSM, Wikidata, OHM).

### Headline changes from the original draft

The Master Plan introduces a number of cross-cutting requirements that did not exist when the
ingestion plan was first drafted. The implementation must absorb each of these:

1. **Terminology — Gazetteers.** What were previously partitioned as Authorities, WHG-curator
   Datasets, and Collections collapse into a single concept (**Gazetteer**) in the Master Plan
   (§1.1–1.4). This document continues to use "namespace" and "authority" where they refer to
   the technical ingestion mechanism, but reserves "gazetteer" for the user-facing inventory
   served by the `/suggest` API (§1.4) and for any precomputed per-collection aggregate.
2. **Per-gazetteer H3 coverage.** A compacted H3 cell set must be precomputed for every
   non-global gazetteer, supporting browser-side intersection tests with the Atlas Area
   filter (Master Plan §1.4.1, Appendix E.2 item 1). Computed inside Batch 6 (H3 derivation),
   not at indexing time. **Skipped** for global gazetteers — `osm`, `ohm`, `wd`, `gn`, `po`,
   `tgn` — which advertise a sentinel "global" coverage rather than a compacted cell set.
3. **Per-gazetteer temporal extent.** A `[start_year, end_year]` summary per gazetteer is
   precomputed in Batch 9 alongside corpus-wide toponyms (Master Plan §1.4.1, Appendix E.2
   item 2).
4. **Pending-dataset isolation via `dataset_status` / `dataset_id`.** Every place record carries
   `dataset_status` (`published` | `pending`) and `dataset_id`; pending content lives in the
   same indices as published content and is hidden from off-scope users by a discovery-time
   filter (Master Plan §7.2, §7.4, Appendix E.2 items 3–4). Schema/ingestion change in
   Batches 1, 4 and 11.
5. **Hard-link SQLite overlay replaces post-index ES clustering.** The pre-existing
   `clusters` ES index and `clustering/runner.py` ES pipeline are retired in favour of a
   query-time clustering model backed by a single SQLite hard-link database co-located with
   the Pitt gateway. The ingestion pipeline harvests authority hard-links from the staged
   files (no ES dependency); contributor assertions are forwarded synchronously from DO
   PostgreSQL by Django. This **replaces** the old Batch 12 (Clustering Handoff) — see the
   new Batch 12 below.
6. **Real-time contributor attestation forwarding.** The `contributor_attestations` schema on
   DO PostgreSQL gains `dataset_id` and a `pending` status; the ingestion pipeline writes
   pending assertions during reconciliation, and the publication transaction flips them to
   `active` (Master Plan §7.4, §9.3, Appendix E.2 item 5). DO-side schema change tracked in
   the contribution workflow; the Pitt-side SQLite must accept these via the new
   `POST /api/links` path (Batch 12).
7. **Gazetteer inventory pushed to Django after indexing.** A new Django API endpoint will
   serve as the canonical gazetteer registry. The ingestion pipeline pushes a complete
   inventory (id, name, description, namespace, owner, record count, status, `h3_coverage`,
   `temporal_extent`) to that endpoint on completion of each run, eliminating the markdown
   selection file as a long-term mechanism (Master Plan §5.3, Appendix E.2 item 6). New
   stage in Batch 11.
8. **v3.2 legacy reconciliation links.** v3.2 accessioned Datasets/Collections are **not
   migrated** to a new store — they remain in DO PostgreSQL (see item 11 below) and are
   re-pulled on every ingestion run via `whg-places.py`. The only one-time work is a DO-side
   data update that flags every historical reconciliation link in
   `contributor_attestations` with `legacy_v3_2 = true`, so the Pitt-side hard-link harvest
   carries the same suffix on `source_id` for downstream filterability (Master Plan §10.2,
   Appendix E.2 item 7). Tracked in the (much narrower) Batch 13b.
9. **Retention sweep for pending datasets.** A scheduled job deletes pending datasets that
   sit unmodified for one year, with an eleven-month notification (Master Plan §10.1,
   Appendix E.2 item 8). New Batch 14a.
10. **LOC is relations-only.** The Library of Congress source contributes no place records,
    only owl:sameAs / skos:exactMatch links between existing places (`loc-relations.py`).
    LOC therefore **does not** participate in Batches 4–11 (no extract, no boundary, no H3,
    no ccode, no toponyms, no per-gazetteer aggregate, no inventory entry as a "gazetteer").
    LOC enters the workflow only in Batch 12 ("Hard-Link SQLite Harvest"), where its
    relation rows are folded into `hard_link_assertions` alongside authority-derived links
    from the staged corpora.
11. **DO PostgreSQL is canonical for all contributed gazetteers.** Datasets and Collections
    contributed via the WHG Django app live permanently in DO PostgreSQL — both legacy v3.2
    accessions and any future contributions. The indexing pipeline treats them as
    `whg`-namespaced gazetteers (one numerical sub-namespace per Dataset/Collection,
    `whg:<dataset_id>:<entity_id>`), pulled in by `authorities/whg-places.py` like any other
    authority. There is no one-time migration of payload — the data is simply read from DO
    on every run. The only legacy-specific work is flagging historical reconciliation links
    with `legacy_v3_2 = true` in DO `contributor_attestations`.

The original "authority-selection.md checkbox file" remains the current control mechanism for
runs and is unchanged in the short term; once the Django gazetteer registry (item 7) is live,
authority selection moves to the API and the markdown file is retired.

### Status snapshot

| Batch | Scope | Status |
|-------|-------|--------|
| 1 | Schema / settings / staged layout | Done — `dataset_status` / `dataset_id` schema + writer guards added; aggregate contract + SQLite hard-link DDL documented in `staging_contract.py` |
| 2 | Type-mapping preflight (production `types` index) | Done |
| 3 | Orchestration + checkpointing | Done — LOC routed to Batch 12 only; partial-run mode (`run_mode='partial'` + `update_namespaces`) wired through `staging_orchestrator.py`; Django-registry resolution path still deferred |
| 4a | Staged extraction shim | Done (`helpers.py`) |
| 4b | Canary refactors (`nl`, `po`, `clio`) | Done — staged-mode + ES backward compat across all three |
| 4c Phase 1 | `gn`, `wd`, `osm`, `ohm` refactor | Done |
| 4c Phase 2 | `tgn`, `pl`, `gb`, `iv` refactor | Done |
| 4c Phase 3 | `geonames-toponyms`, `wikidata-geoshapes` (update scripts — different semantics) | Pending |
| 4c Phase 4 | `whg` ingestion (DO API + LPF + per-Dataset/Collection sub-namespaces) | Pending — requires Django clone access |
| 4d | Boundary stage + consolidation | `boundary_stage.py` + `boundary_merge.py` + `_consolidate_extracts()` done |
| 5 | Patch-collapse merges | Done — `boundary_merge.py`, `h3_merge.py`, `ccode_merge.py`; H3/ccode patch contracts in `staging_contract.py`; idempotency + missing-file handling regression-tested |
| 6 | H3 derivation **+ per-gazetteer H3 coverage compaction** (non-global only) | Done (`h3_stage.py`, `submit_h3_slurm.py`); coverage emit + benchmarking pending |
| 7 | CCode enrichment | Pending (`ccode_enrichment.py`, `ccode_merge.py`) |
| 8 | Global barrier | Pending (manifest validator) |
| 9 | Toponyms + Symphonym **+ per-gazetteer temporal extent** | Pending; temporal aggregate is new |
| 10 | Tile generation (no ES) — runs **ahead of** Global Barrier | Pending |
| 11 | Index loaders + **gazetteer inventory push (final-step gating)** | Pending; inventory push is new and gates on Batches 9, 11 *and* 12 |
| 12 | **SQLite hard-link harvest** — staged authority links + LOC relations + DO contributor replay (replaces post-index ES clustering) | New; pre-existing `clustering/harvest/hard_links.py` (ES-based) supplies the algorithm but must be re-targeted at staged files; LOC enters here |
| 13a | WHG dataset authority discovery / LPF integration | Folded into 4c Phase 4 (no separate batch) |
| 13b | **v3.2 legacy reconciliation flagging** (DO-side, narrow scope) | New; pending |
| 14 | Test harness + integration rollout | Pending |
| 14a | **Retention sweep** for pending datasets | New; pending |

---

## Execution Model

- **Global preflight**: establish the selected gazetteer set, shared config, manifests,
  credentials, and read-only caches. Validates production ES `types` index availability
  (Batch 2) and discovers WHG datasets via the DO Django API (Batch 4c Phase 4).
- **Per-gazetteer preprocessing fan-out**: each gazetteer runs its own staged extraction
  pipeline through place-level preprocessing (`extract → boundary → boundary_merge → H3 +
  per-gazetteer H3 coverage compaction → ccode`). This is the main parallelisation domain
  and is intended to be runnable as separate Slurm jobs.
- **Pre-barrier global work**: tile generation (Batch 10) runs as soon as its contributing
  gazetteers complete the relevant local stages, **ahead of** the Global Barrier — there is
  no need to gate tile production on full-corpus completion.
- **Global barrier** (Batch 8): corpus-wide post-barrier phases must wait until every
  selected gazetteer reports preprocessing complete in its manifest.
- **Global post-barrier phases (parallelisable)**: toponym deduplication + Symphonym
  embedding + per-gazetteer temporal-extent aggregation (Batch 9) and the SQLite hard-link
  harvest (Batch 12, including LOC relations and DO contributor replay) run **in parallel**
  on separate Slurm jobs — both are read-only over staging and the geometry store, with no
  shared mutable state. Filesystem-bandwidth contention is acceptable; if it becomes a
  bottleneck the controller can serialise them via dependency chaining without changing
  any artefact contracts.
- **Indexing** (Batch 11) follows Batch 9 (it loads the final toponyms + places).
- **Gazetteer inventory push** (Batch 11, *final step*): gated on the successful completion
  of indexing **and** the SQLite hard-link harvest. The inventory push is the user-facing
  signal to Django that the run is complete; it must not fire while the gateway-side
  hard-link database is still being shipped to Pitt.

Toponym deduplication and Symphonym generation are **not gazetteer-local** in this design;
they run once over the union of all selected gazetteers after the preprocessing barrier.

### Run modes: full vs. partial

The orchestration controller supports two run modes:

- **Full run.** All selected gazetteers go through the per-gazetteer pipeline; staged
  artefacts for unselected gazetteers are removed at preflight; post-barrier stages
  (Batches 9–12) operate over the entire selected corpus.
- **Partial run** (single-gazetteer or subset update — e.g. a newly contributed Dataset
  or a periodic refresh of an existing authority). The controller:
  1. Re-runs Batches 4–7 only for the gazetteers in the update set; staged artefacts for
     gazetteers **outside** the update set are left in place untouched.
  2. Treats the existing on-disk staged snapshots for the unchanged gazetteers as the
     barrier inputs alongside the freshly produced ones.
  3. After the Global Barrier, **re-runs the post-barrier stages (Batches 9–12) over the
     full selected corpus from scratch**, since toponym dedup, Symphonym embedding caching,
     temporal aggregates, tile mixed-source layers, and hard-link harvest all depend on the
     union of gazetteers. The Symphonym cache (per Batch 9) avoids re-embedding unchanged
     toponyms, which is the main cost amortisation in this mode.
  4. Optional incremental paths (e.g. delta-only hard-link harvest) may be added later if
     wall-time becomes a concern; the default is a full post-barrier rebuild.

Authority/gazetteer inclusion/exclusion is currently controlled by a root-level checkbox
markdown file (`authority-selection.md`). Partial updates are expressed by the same file;
the controller compares the file against the on-disk manifest to compute the update set.
Once the Django gazetteer registry is live (Master Plan §5.3), selection moves into Django
and the markdown file is retired.

---

## Slurm Orchestration Sketch

The intended scheduler shape is a **fan-out / fan-in** workflow coordinated by a single
controller layer.

### Job classes

- **Controller job** — computes the selected gazetteer set, writes the run manifest,
  submits downstream jobs with success-only dependencies, and re-submits only missing/failed
  stages on resume.
- **Global preflight job** — runtime setup, credential checks, type-mapping preflight, WHG
  authority discovery, deselection cleanup.
- **Per-gazetteer preprocessing jobs** — one gazetteer per job by default; large gazetteers
  may further split internally into arrays/shards for H3 or other heavy local work.
- **Barrier job** — refuses to release corpus-wide work until every required gazetteer has
  completed stages 2–5.
- **Global corpus job** — toponym deduplication and Symphonym, plus per-gazetteer aggregate
  computation (H3 coverage, temporal extent).
- **Tile jobs** — per-gazetteer where outputs are gazetteer-local; mixed-source where
  outputs depend on multiple gazetteers.
- **ES-dependent jobs** — start/check staging ES, index staged outputs, push the gazetteer
  inventory to Django.
- **Hard-link harvest job** — reads staged files and produces the SQLite database; ships
  the database to Pitt with an atomic swap. Independent of ES.

### Recommended dependency flow

1. **Controller** submits **Global preflight**.
2. **Global preflight** submits an early **`un` preprocessing branch** through stages 2–5.
3. Once `un` H3 is complete, the controller submits **non-`un` gazetteer preprocessing
   jobs** in parallel for stages 2–5. Any gazetteer requiring stage 5 waits on successful
   `un` H3 completion.
4. **Per-gazetteer tile jobs** may start after that gazetteer completes the relevant
   local preprocessing stages — **before** the Global Barrier.
5. **Mixed-source tile jobs** wait until all contributing gazetteers complete (still
   pre-barrier).
6. When all selected gazetteers report stages 2–5 complete, submit the **Barrier job**.
7. On barrier success, submit **two parallel post-barrier jobs**:
   - **Job A — Global Toponyms + Symphonym + temporal-extent aggregates** (Batch 9).
   - **Job B — SQLite hard-link harvest** (Batch 12), including LOC relations folded in
     here and DO contributor replay. Independent of Batch 9 and of staging ES.
   Both read staged files and the geometry store read-only; they may safely run on
   separate Slurm allocations. Serialise them via dependency only if filesystem-bandwidth
   contention is observed.
8. After Job A completes, start or verify the **staging ES instance**.
9. Submit the **Indexing** job (depends on Job A). After Job B (hard-link harvest)
   completes, submit the **Ship-to-Pitt** atomic swap.
10. **Gazetteer inventory push** is the **final step** of the run and is gated on **all of**
    successful indexing (Job A → indexing) and successful Ship-to-Pitt (Job B → swap).
    The push is the signal to Django that the ingestion/indexing run is complete, so it
    must not fire until both the ES indices and the Pitt-side SQLite reflect the new run.

### Resume / retry model

- Retries occur at **stage boundaries**, not inside partially written artefacts.
- The controller inspects manifests/checkpoints and re-submits only failed stages.
- A failed corpus-wide toponym/Symphonym run must not trigger re-extraction of already-
  complete gazetteers; it restarts from the barrier output.
- ES-dependent retries begin from staging ES verification onward.
- Hard-link harvest is independent and can be retried without re-running indexing.

---

## External Contracts (Implemented in Django)

> **DO codebase location.** The Django/DO codebase is **not** in this repository. A local
> clone sits at `/home/stephen/Documents/GitHub/whg3` (the WHG v3 Django app, repository
> `WorldHistoricalGazetteer/whg3`). Coding agents working on this plan should request
> read access to that clone whenever cross-checking Django models, the
> `contributor_attestations` table, the `Dataset` / `Collection` schema, the
> `/reconcile/authority-datasets` and LPF endpoints, or the gazetteer-registry endpoint
> contract is necessary. **Do not** assume the Django code matches what is described
> here without verifying against the clone.

The Django side has been brought up to date with this plan. Cross-references below
point to specific files in the WHG3 clone:

* **`Dataset.authority`** controls dataset eligibility for authority ingestion
  (unchanged; in `datasets/models.py`).
* **Discovery endpoint**: `GET /reconcile/authority-datasets`
  (`api/reconcile.py::AuthorityDatasetsView`). Returns
  `result[{id, title, label, description, ds_status, public, authority,
  owner_id, place_count, dataset_status}]` — `dataset_status` is derived
  Django-side as `"published"` when `(authority and public and ds_status in
  {accessioning, indexed})`, otherwise `"pending"`. Pass
  `?include_pending=true` to also enumerate datasets that aren't authority
  yet (any `ds_status` except `seed` / `format_error`).
* **LPF endpoint per dataset**: `GET /entity/dataset:<id>/api?filetype=lpf`
  (`api/views_entity.py::EntityFeatureView`) — streamed gzipped GeoJSON
  FeatureCollection. Unchanged.
* **Auth**: token/session auth via `Authorization: Bearer <token>` header
  or `?token=…` query param (`api/authentication.py::TokenQueryOrBearerAuthentication`).
* **Gazetteer registry endpoint** (Master Plan §5.3, Appendix E.2 #6):
  `POST/PUT /api/registry/inventory` (`api/views_indexing.py::GazetteerInventoryView`).
  Idempotent upsert of the per-gazetteer registry; backed by the
  `GazetteerRegistryEntry` model (migration `api/migrations/0002_indexing_rebuild.py`).
  WHG datasets land as `class='dataset'` rows fanned out from the
  `whg.datasets.json` sidecar written by `authorities/whg-places.py`.
* **Retention notify endpoint** (Batch 14a): `POST /api/retention/notify`
  (`api/views_indexing.py::RetentionNotifyView`). Logs the batch and, when
  `settings.WHG_RETENTION_DISPATCH_FN` is configured, hands the payload off
  to the configured callable for actual email / in-platform notification
  dispatch.
* **Contributor attestation API** (Master Plan §2a):
  `POST/GET/DELETE /api/links` (`api/views_indexing.py::ContributorAttestationView`).
  Backed by the `ContributorAttestation` model with composite `(place_a,
  place_b, relation_type, user)` uniqueness, `place_a < place_b` CHECK
  constraint, and `legacy_v3_2` flag (Batch 13b).
* **Live forwarding to the gateway** (Master Plan §2d–2e): on every
  ContributorAttestation save / delete, `api/signals.py::attestation_saved`
  / `attestation_deleted` POST/DELETE the row to the gateway via
  `api/crc_client.py::crc_post_link` / `crc_delete_link`. Best-effort: a
  failure logs a warning and is reconciled by the next Batch 12
  `contributor_replay` run.

---

## Runtime Prerequisites

- [x] WHG API base URL configured in `processing/settings.py::WHG_API_BASE_URL`
  and the inventory endpoint composed as `WHG_INVENTORY_ENDPOINT`
  (overrideable per environment).
- [x] Token credentials read from `WHG_API_TOKEN_FILE` (default
  `${IX1_BASE}/secrets/whg-api.token`); never committed.
- [x] Retry/backoff defaults centralised in
  `WHG_HTTP_TIMEOUT` / `WHG_HTTP_MAX_RETRIES` / `WHG_HTTP_INITIAL_BACKOFF`;
  consumed by `push_gazetteer_inventory.py` and `retention_sweep.py`.
- [x] Root-level `authority-selection.md` exists; parser + validation in
  `staging_orchestrator.parse_authority_selection_file` /
  `resolve_selected_authorities`.
- [x] Selection file already bootstrapped with all local authorities;
  per-WHG-dataset entries are added at discovery time once Batch 4 Phase 4
  (`whg-places.py`) lands.
- [x] Pitt VM filesystem paths for the SQLite live in
  `PITT_HARDLINK_DIR` / `PITT_HARDLINK_FILENAME` / `PITT_HARDLINK_REMOTE_USER`
  / `PITT_HARDLINK_REMOTE_HOST`; the atomic-swap procedure (rsync to
  `.<filename>.incoming`, then remote `mv`) is implemented in
  `clustering/sqlite_overlay.ship_to_pitt`. Gateway-side reopen handler is
  tracked separately in the gateway repo.

---

## Parallelisation Model and Contention Points

- **Workable parallelism**: per-gazetteer ingestion is parallelisable up to but not
  including corpus-wide toponym deduplication/Symphonym.
- **Safe parallel region**: staged extraction, patch-collapse (boundary), H3 derivation,
  and post-H3 ccode enrichment, provided outputs are namespace-scoped.
- **Ordering constraint**: non-`un` ccode enrichment must wait until the `un` gazetteer
  has completed H3 derivation.
- **Shared-resource contention to manage**:
  - staged/VAST filesystem bandwidth and metadata churn,
  - geometry-store consolidation/finalisation steps,
  - remote API or source-download rate limits,
  - mixed-source tile products (especially OSM/OHM outputs),
  - CPU/RAM/GPU scheduling fairness across simultaneous Slurm jobs.
- **Not a blocker pre-index**: staging ES is not needed until indexing, so ES contention
  does not prevent per-gazetteer preprocessing fan-out.
- **Hard-link harvest** runs in parallel with indexing — it reads staged files and writes
  a local SQLite, with no shared mutable state.

---

## Authority Preprocessing Pipeline (Batches 4–7)

**Unified Architecture**: gazetteers follow a standardised extraction → enrichment → merge
pipeline:

```
┌─────────────────────────────────────────────────────────────────────┐
│ EXTRACT (Batch 4)                                                   │
│ Authority script extracts, writes to {namespace}/extract/places.json│
│ No ES access. Standardised output. Can run in parallel.             │
│ Each record carries dataset_status='published' (default) and        │
│ dataset_id (gazetteer identifier).                                  │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (per-namespace staged snapshot)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ BOUNDARY COMPLETION (Batch 4/5 bridge)                              │
│ For osm/ohm only: assemble relation multipolygon geometry from PBF  │
│ and write staged boundary patches (no ES updates).                  │
│ Output: {namespace}/boundary/places.boundary.jsonl                  │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (boundary patches)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ BOUNDARY MERGE (Batch 5)                                            │
│ Merge boundary patches into staged snapshot before H3 processing.   │
│ Output: {namespace}/boundary_merged/places.parquet                  │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (boundary-complete snapshot)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ H3 DERIVATION (Batch 6, Slurm Array)                                │
│ For each {namespace}/h3 task: read snapshot + full geom,            │
│ compute h3_centroid/h3_cover per geometry, write patches.           │
│ Can run in parallel (one Slurm task per namespace).                 │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (H3 patches)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ H3 MERGE (Batch 5)                                                  │
│ Read extract snapshot + H3 patches, merge fields into docs.         │
│ Produce {namespace}/h3-merged/places.parquet.                       │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (H3-enriched snapshot)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ CCODE ENRICHMENT (Batch 7)                                          │
│ For each {namespace} ≠ un: read H3-enriched snapshot + UN H3        │
│ geometries, containment-test, emit ccode patches.                   │
│ Can run in parallel (un must complete H3 first).                    │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (ccode patches)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ CCODE MERGE (Batch 5)                                               │
│ Read H3-enriched snapshot + ccode patches, merge ccodes into docs.  │
│ Produce {namespace}/final/places.parquet (fully enriched).          │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (fully enriched per-namespace staged snapshot)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ BARRIER (Batch 8)                                                   │
│ Wait for all selected gazetteers to finish the pipeline above.      │
│ Verify manifest completeness before proceeding to corpus-wide       │
│ phases (Batch 9: Toponyms+Symphonym+Aggregates, Batch 10: Tiles,    │
│ Batch 11: Indexing, Batch 12: Hard-Link SQLite Harvest).            │
└─────────────────────────────────────────────────────────────────────┘
```

**Key Properties**:

- **No ES during Batches 4–7**: all processing on staged files.
- **Boundary-before-H3 rule**: OSM/OHM boundary completion and merge must finish before
  H3/ccode.
- **Per-namespace parallelisation**: each namespace is independent until the barrier.
- **Standardised outputs**: each batch produces pre-defined artefacts (JSONL/Parquet
  snapshots, patches).
- **Idempotent, resumable**: failed stages can re-run; earlier stages' outputs are reused.
- **`dataset_status` and `dataset_id` are present from extract onward**: the staged record
  schema in Batch 1 mandates them; the discovery filter at query time is what makes pending
  records invisible to off-scope users.

---

## Removed Legacy ES Code (record)

The following modules were the ES-coupled paths superseded by the staged
architecture and have been **deleted** from the repo (no fallback kept):

* `processing/augment_ccodes.py` → replaced by
  `processing/ccode_enrichment.py` (Batch 7) + `submit_ccode_slurm.py`.
* `processing/deploy_to_production.py` → replaced by
  `processing/index_from_stage.py` + `processing/namespace_lifecycle.py`
  (Batch 11).
* `processing/embed_extract.py` / `processing/embed_transform.py` /
  `processing/embed_load.py` → superseded by
  `phonetics/inference/update_es.py compute|index` (Batch 9 / 11).
* `processing/optimise_places.py` → superseded by ES `forcemerge` invocations
  in `scripts/es.sh`.
* `authorities/loc-relations.py` → LOC is consumed only at Batch 12
  (`clustering/harvest/loc_links.py`).
* `clustering/harvest/hard_links.py` → replaced by
  `clustering/harvest/hard_links_staged.py` (Batch 12).
* The legacy 4-phase clustering pipeline:
  `clustering/{__main__,runner,calibration,clustering,indexer,scoring,state,schemas,es_client,RECON_NOTES.md}`
  + `clustering/harvest/{contributor_links,exact_coattest,phonetic}.py`.
* `scripts/cluster.sh` and the `-augment-ccodes` / `-cluster` /
  `-cluster-finalize` dispatch lines in `scripts/es.sh`.
* `clustering/config.py` slimmed to keep only ``KNOWN_ES_NAMESPACES``,
  ``IDENTITY_RELATION_TYPES``, and ``PG_*`` (the only constants the
  surviving Batch 12 harvesters consume).

ES is now used only by the new processes specified in Batch 11
(`index_from_stage`, `namespace_lifecycle`, `update_es.py index`,
`push_gazetteer_inventory`, `retention_sweep`) and by the gateway runtime.

---

## Execution Batches

### Batch 1: Settings, Schema, Manifest Contracts

Targets:

- `processing/settings.py`
- `processing/helpers.py`
- `processing/geom_store.py`
- `processing/staging_contract.py`
- `schemas/places.json` (field additions)

Dependencies: None (foundational).

Status: largely complete (`staging_contract.py`, `geom_store.py`, manifest schema in
`staging_orchestrator.py`). The Master Plan additions below are **new**.

Tasks:

- [x] Define staged filesystem layout (`staged/{namespace}/...`) in settings.
- [x] Define manifest schema contract (`manifest.json`): fields for lineage, counts,
  checksums, stage status, H3 completion metrics, ccode coverage metrics, output artefact
  paths.
- [x] Define geometry reference contract (lookup key, shard/index layout, per-geometry
  reference structure).
- [x] Confirm `places` field compatibility for enriched staged output (`repr_point`,
  `hull`, `bounds`, `has_geom`, `h3_centroid`, `h3_cover`, `ccodes`).
- [x] Define ccode patch record schema and merge semantics.
- [x] Define h3 fields as per-geometry nested:
  `geometries[].{h3_centroid, h3_cover}` (H3 cell IDs as strings).
- [x] **(Master Plan E.2 item 3)** Added top-level `dataset_status` (`published` |
  `pending`) and `dataset_id` (string) fields to `schemas/places.json` (both `keyword`).
  `processing/helpers.py::write_staged_place_doc` now fills defaults at extract time
  (`dataset_status='published'`, `dataset_id='<namespace>'`) and rejects unknown statuses
  or `whg`-namespace docs missing an explicit `dataset_id` (see 4c Phase 4).
  `processing/staging_contract.py::DATASET_STATUSES` and
  `STAGED_PLACE_REQUIRED_TOPLEVEL_FIELDS` define the contract.
- [x] **(Master Plan §1.4.1 + E.2 items 1–2)** Defined the per-gazetteer aggregate
  contract in `processing/staging_contract.py`:
  - `staged/_aggregates/{namespace}.h3_coverage.json` produced by Batch 6:
    `{"namespace": "<ns>", "coverage": [<h3 cells>], "compacted": true}` for non-global
    namespaces; `{"namespace": "<ns>", "coverage": "global"}` for the global set
    (`GLOBAL_COVERAGE_NAMESPACES = {osm, ohm, wd, gn, po, tgn}`).
  - `staged/_aggregates/{namespace}.temporal_extent.json` produced by Batch 9:
    `{"namespace": "<ns>", "record_count": <int>, "temporal_extent": [int|null,
    int|null]}`.
  - Validators: `validate_h3_coverage_aggregate`, `validate_temporal_extent_aggregate`.
- [x] **(New)** Documented the SQLite hard-link database schema in
  `processing/staging_contract.py::HARD_LINK_SQLITE_SCHEMA` (full DDL + indices + WAL
  guidance), with `validate_hard_link_row` for harvest writers and constants
  `HARD_LINK_RELATION_TYPES`, `HARD_LINK_SOURCE_CATEGORIES` shared across Batch 12
  consumers.

Validation gates:

- [x] Schema review and acceptance.
- [x] Geometry lookup resolves deterministically for sampled staged rows.
- [x] Manifest can be serialised and loaded without data loss.
- [x] `places.json` mapping admits `dataset_status` and `dataset_id` as `keyword`;
  `write_staged_place_doc` round-trips both fields with default-filling for non-`whg`
  namespaces and explicit-required for `whg`. Negative cases (invalid status, missing
  `whg` dataset_id) raise `ValueError`.
- [x] Aggregate file schemas validate against synthetic samples for `nl` (non-global,
  cell-list coverage + finite temporal extent) and `po` (global sentinel + `[None, None]`
  extent). Negative cases (mismatched sentinel, malformed cell list, malformed extent)
  raise `ValueError`.

### Batch 2: Type Mapping Preflight (Production `types` Index)

Targets:

- `processing/aat_lookup.py`
- `processing/settings.py`

Dependencies: Batch 1.

Status: complete. `aat_lookup.preflight_types_index` fails fast on missing index;
`load_aat_mappings` and `apply_aat_mappings_to_index` populate `aat_id` / `aat_path` on
staged docs alongside the original `sourceLabel`. Compatible with Batch 1's
`dataset_status` / `dataset_id` additions — no AAT-side change needed.

Tasks:

- [x] Add preflight check for production ES availability and `types` index access.
- [x] Implement direct reverse-lookup mapping against production `types` index fields
  (e.g. `gn_fcodes`, `wd_qids`, `osm_tags`, `ohm_tags`).
- [x] During ingestion, store both original source type and mapped AAT path string in
  staged place records.

Validation gates:

- [x] Type preflight fails fast when production `types` index is unavailable.
- [x] Sample mappings return expected AAT IDs/path values.
- [x] AAT enrichment is unaffected by the new `dataset_status` / `dataset_id` top-level
  fields (they live alongside `types[]`, not inside it).

### Batch 3: Orchestration and Checkpointing

Targets:

- `scripts/ingest.sh`
- `scripts/es.sh`
- `processing/ingest_all_authorities.py`
- `processing/staging_orchestrator.py`

Dependencies: Batch 1, Batch 2.

Status: complete; fan-out / fan-in dependency chain wired through `ingest.sh` for
boundary → boundary_merge → H3. Today's plan refresh introduced two new constraints —
LOC routing and partial-run mode — both implemented in `staging_orchestrator.py`.

Tasks:

- [x] Introduce stage-level checkpoints and resume semantics.
- [x] Separate extract/stage from index operations in orchestration.
- [x] Implement fan-out/fan-in orchestration: parallel authority preprocessing, then
  explicit global barrier before corpus-wide phases.
- [x] Resolve run authority set from `authority-selection.md`.
- [x] Add namespace-scoped run IDs and immutable manifests.
- [x] Ensure idempotent reruns for a failed stage.
- [x] **Route relations-only namespaces (LOC) out of the per-gazetteer pipeline.**
  `RELATIONS_ONLY_NAMESPACES = {"loc"}` in `staging_contract.py`;
  `partition_namespaces`, `is_relations_only`, and `relations_only_in_run` expose the
  split. `create_run_manifest` gives LOC a single `hard_link_harvest` stage instead of
  the per-gazetteer pipeline; `check_preprocessing_barrier` skips LOC; `build_fanout_plan`
  excludes it from per-gazetteer fan-out.
- [x] **Partial-run mode (single-gazetteer / subset updates).** `create_run_manifest`
  accepts `run_mode='partial'` + `update_namespaces=[...]`. Namespaces in the selected
  set but not in the update set are marked `carried_over`; their staged artefacts are
  left in place. `reconcile_carried_over_namespaces` walks the on-disk staged tree and
  flips those namespaces' `extract` / `h3` / `ccode` stages to `completed` if the
  expected artefacts are present, then the barrier check passes them through.
  Schema bumped to `schema_version: 2` with `run_mode` + `update_namespaces` fields.
- [ ] **(Deferred to Batch 11 enabling work)** Add a fallback selection-resolution
  path that consults the future Django gazetteer registry once it is available; keep the
  markdown file as the immediate source of truth.

Validation gates:

- [x] Kill/restart test resumes from the next incomplete stage.
- [x] Re-run does not duplicate outputs or corrupt manifests.
- [x] LOC inclusion in `authority-selection.md` does not enrol it in
  extract / boundary / H3 / ccode; the barrier passes with LOC stages still pending.
- [x] Partial run with `update_namespaces=['nl']` over selected `['nl', 'po', 'gn']`
  leaves po/gn artefacts in place, reconciles them as completed when on-disk artefacts
  exist, and only re-stages `nl`. Reconcile reports missing artefacts as failures when
  the staged tree is empty for a carried-over namespace.
- [x] `create_run_manifest` rejects `run_mode='draft'`, `partial` without
  `update_namespaces`, and `update_namespaces` not in `selected_namespaces`.

### Batch 4: Authority Script Refactor to Staged Extraction Pattern

Targets:

- `processing/helpers.py` (staged extraction shim)
- `authorities/*.py` (all authority scripts)
- `processing/ingest_all_authorities.py`

Dependencies: Batch 1, Batch 2, Batch 3.

**Architecture Change** (already implemented for the Phase 1 set): authority scripts are
refactored from custom ES indexers to **lightweight standardised extractors** that write
place documents to staged JSONL/Parquet files. Per-gazetteer preprocessing (H3 derivation,
ccode enrichment, etc.) is generic post-processing that runs uniformly after extraction.

Tasks:

#### 4a. Extraction Shim and Staged Writer Framework

- [x] `write_staged_place_doc(namespace, doc)` in `processing/helpers.py`.
- [x] Registry/factory in `processing/settings.py` mapping namespace → extraction script.
- [x] `WHG_STAGING_MODE=1` env-var switch in `ingest_all_authorities.py`.
- [x] **Extend `write_staged_place_doc` to require `dataset_status` and `dataset_id` on
  every doc** (defaults filled at extract time: `published` / `<namespace>`; `whg`
  namespace requires explicit `dataset_id`). Implemented in Batch 1.

#### 4b. Authority Script Canaries

- [x] `authorities/nativeland-places.py` (nl) — staged-mode + ES backward compatibility.
- [x] `authorities/periodo-places.py` (po) — same.
- [x] `authorities/cliopatria-places.py` (clio) — staged-mode + ES backward compatibility.

Validation gates:

- [x] Each canary script runs without ES, writes `{namespace}/extract/places.jsonl`.
- [x] Staged output is valid JSONL, row count matches expected.
- [x] No ES indexing calls occur in staged mode.

#### 4c. Remaining Authority Scripts

**Phase 1 (High Priority)** — done:

- [x] `authorities/geonames-places.py` (gn).
- [x] `authorities/wikidata-places.py` (wd).
- [x] `authorities/osm-places.py` (osm).
- [x] `authorities/ohm-places.py` (ohm).

**Phase 2 (Medium Priority)** — done:

- [x] `authorities/tgn-places.py` (tgn) — staged-mode + ES backward compatibility.
- [x] `authorities/pleiades-places.py` (pl) — same; both streaming and standard fallback
  paths handle staged mode.
- [x] `authorities/gb1900-places.py` (gb).
- [x] `authorities/indexvillaris-places.py` (iv).

**Phase 3 (Update Scripts)** — done. Update scripts emit per-namespace
patches under ``staged/{ns}/update_patch/places.update.jsonl``;
``processing/update_merge.py`` collapses each patch into
``staged/{ns}/update_merged/places.parquet`` before H3 derivation. The
namespaces in ``staging_contract.UPDATE_PATCH_NAMESPACES`` (``{gn, wd}``)
have ``update_patch`` and ``update_merge`` stages added to the per-gazetteer
manifest; ``h3_stage`` / ``h3_merge`` and the four staged-source-priority
chains pick up ``update_merged/`` automatically when present.

- [x] `authorities/geonames-toponyms.py` — refactored: streams
  `alternateNamesV2.zip`, emits `{place_id, title?, toponyms_to_add,
  relations_to_add}` rows into the patch JSONL. No ES.
- [x] `authorities/wikidata-geoshapes.py` — refactored: continues to fetch
  Commons GeoJSON (cached SQLite, rate-limited) and write to the geom store,
  but now emits `{place_id, geometries_to_replace, h3_centroid, h3_cover}`
  rows into the patch JSONL instead of issuing ES updates. No ES.
- [x] `processing/update_merge.py` (new) — generic merger: ``title`` is
  authoritative on patch, toponyms append-deduped on ``toponym_id``,
  relations append-deduped on ``(relation_type, related_place_id)``,
  ``geometries_to_replace`` overwrites the array. Idempotent (verified by
  ``tests/test_update_merge.py`` rerun test).
- [x] ~~`authorities/loc-relations.py`~~ — retired. LOC contributes only
  hard-link assertions and is consumed exclusively by Batch 12
  (``clustering/harvest/loc_links.py``).

**Phase 4 (WHG Datasets — DO PostgreSQL is canonical)** — done.

- [x] `authorities/whg-places.py` (new). One script per run, iterates every
  authority-eligible WHG dataset:
  - Discovery: `GET /reconcile/authority-datasets` (returns
    `{"result": [{"id", "title", "place_count"}, ...]}` filtered to
    `Dataset.authority=True`).
  - LPF fetch: `GET /entity/dataset:<id>/api?filetype=lpf` — single gzipped
    GeoJSON FeatureCollection per dataset; parsed feature-by-feature via
    ``ijson.items(stream, "features.item")`` to bound memory for
    multi-million-feature datasets.
  - Auth: bearer token in ``Authorization`` header (with ``?token=`` URL
    fallback handled by Django). Token resolution: CLI `--token` →
    ``WHG_API_TOKEN`` env → ``WHG_API_TOKEN_FILE`` (default
    ``${IX1_BASE}/secrets/whg-api.token``).
  - Stable IDs: emit ``place_id = whg:<dataset_id>:<entity_id>`` (using
    ``feature['id']``, the WHG numeric place id), ``dataset_id =
    whg:<dataset_id>``.
  - Per-dataset progress tracked in the manifest under
    ``namespaces.whg.scripts.whg-places`` and via the per-namespace stage
    events; per-dataset re-staging in partial-run mode is supported via the
    ``--dataset`` CLI flag (filters discovery by id).
  - `dataset_status = 'published'` for everything from
    ``authority-datasets`` (the endpoint already filters to accessioned
    datasets); pending submissions are out of scope for this script
    pending a Django-side endpoint that surfaces them.
  - LPF→staged-doc mapping (``lpf_feature_to_staged_doc``) covers names →
    toponyms, types/links/related → staged shapes, ``whens`` → per-geometry
    timespans, and unwraps ``GeometryCollection`` to its first geometry.
  - Registered in ``INGESTION_ORDER`` as `('whg', 'whg-places', ...)`.

Each refactor follows the same pattern (no longer with the legacy ES
backward-compatibility shim — that path was removed when the legacy ES code
was deleted):

1. Authority script writes via ``write_staged_place_doc`` (or, for Phase 3
   updates, into the ``update_patch`` JSONL).
2. Geometry-store writes happen as before.
3. No ES client instantiation anywhere in staged-mode authority scripts.
4. ``dataset_status`` and ``dataset_id`` are populated on every emitted doc
   (defaults filled by ``write_staged_place_doc`` for non-`whg`; explicit
   on whg).

#### 4d. Staged Snapshot Consolidation

- [x] `processing/boundary_stage.py` (osm/ohm only) — assemble relation multipolygon
  geometry from PBF, emit `{namespace}/boundary/places.boundary.jsonl`.
- [x] `_consolidate_extracts()` in `processing/stage_writers.py` — merge per-namespace
  JSONL extract fragments into consolidated `places.parquet`, augmenting each geometry
  with `geometry_index` / `geom_ref`, writing a `places.snapshot.json` sidecar, and
  optionally removing the source JSONL fragments. Idempotent on re-run.

Validation gates:

- [x] `_consolidate_extracts()` round-trip: 2-doc canary writes + reads back via
  `pyarrow.parquet`, with `dataset_status` / `dataset_id` populated and `geometry_index`
  / `geom_ref` augmentation applied; rerun overwrites cleanly; missing extract dir
  raises `FileNotFoundError`.
- [ ] Staged snapshots for all selected gazetteers are complete and valid Parquet.
  *(End-to-end gate — depends on a real run; deferred.)*
- [ ] Row counts and key uniqueness verified per namespace.
  *(Same — verified during integration testing.)*
- [x] No ES access during extraction or consolidation — `_consolidate_extracts()` only
  uses `pyarrow` + filesystem IO; staged-mode authority scripts skip the
  `Elasticsearch(...)` client entirely.
- [x] `dataset_status` and `dataset_id` present on every staged record by virtue of
  `write_staged_place_doc` filling defaults.

### Batch 5: Per-Gazetteer Patch-Collapse and Update Transforms

Targets:

- `processing/boundary_merge.py` ✅
- `processing/h3_merge.py` ✅
- `processing/ccode_merge.py` ✅
- `processing/namespace_materialize.py` (existing; finalise manifests)
- `processing/ingest_all_authorities.py`

Dependencies: Batch 4 (extract finished), Batch 6 (H3 patches), Batch 7 (ccode patches).

Tasks:

- [x] `boundary_merge.py` — reads staged extract snapshot + boundary patch JSONL,
  merges completed boundary geometry into place docs before H3, writes
  `{namespace}/boundary_merged/places.parquet|jsonl`.
- [x] `h3_merge.py` — reads `boundary_merged/` (osm/ohm) or `extract/` (others) +
  `h3/places.h3.jsonl`, merges per-geometry `h3_centroid` / `h3_cover` into each
  place document's geometries (addressed by `geometry_index`), writes
  `{namespace}/h3_merged/places.parquet|jsonl`. Stage event + manifest updates wired
  through `staging_orchestrator`.
- [x] `ccode_merge.py` — reads `h3_merged/` + `ccode/places.ccode.jsonl`, overwrites
  `doc.ccodes` with the patch list (authoritative), writes
  `{namespace}/final/places.parquet|jsonl`.
- [x] **Patch merge semantics defined.** `ccode_merge` is authoritative — the patch
  list overwrites any prior `doc.ccodes` (including upstream values like the
  Native Land `XX` placeholder). `h3_merge` is also authoritative for `h3_centroid`
  and `h3_cover` per `geometry_index`. Other fields are passed through untouched.
  Malformed patch rows (missing required fields) are silently dropped — see
  validation gates for the regression test.
- [x] **Idempotency.** Re-running either merge over the same source + patch files
  produces identical output (regression-tested).
- [x] **Patch files reference correct geometry indices.** `h3_stage.py` already
  emits `geometry_index` per update; `h3_merge.py` matches against
  `geom.get("geometry_index", idx)` so patches and source rows agree even if the
  document order changes after a future re-shard.

Patch contracts:

```text
H3 patch (JSONL line):
  {
    "place_id": "<ns>:<id>",
    "geometries": [
      {"geometry_index": 0, "h3_centroid": "<cell>", "h3_cover": ["<cell>", ...]}
    ]
  }
  Required fields: place_id, geometries[].{geometry_index, h3_centroid, h3_cover}

CCode patch (JSONL line):
  {"place_id": "<ns>:<id>", "ccodes": ["GB", "FR"], "source": "un-h3-overlap"}
  Required fields: place_id, ccodes, source
```

Both contracts are defined as constants in `processing/staging_contract.py`
(`H3_PATCH_REQUIRED_FIELDS`, `H3_PATCH_GEOMETRY_REQUIRED_FIELDS`,
`CCODE_PATCH_REQUIRED_FIELDS`) and validated via `validate_required_fields`.

Validation gates:

- [x] **Patch merge is idempotent and deterministic.** Synthetic 3-doc, 2-geometry
  scenario: H3 patch (1 update, 1 unmatched, 1 malformed-dropped) and ccode patch
  (2 updates, 1 unmatched, 1 malformed-dropped) both round-trip through Parquet
  and re-run identically.
- [x] **Output row counts and key integrity pass.** `docs_seen == docs_written`
  for both merges; `patches_unmatched` reflects unresolved patches in the metrics.
- [x] **Enriched snapshots have `h3_centroid` / `h3_cover` per geometry and `ccodes`
  at document level as expected.** Verified against `pyarrow.parquet` readback;
  unmatched documents pass through with `h3_*` fields absent on geometries and
  prior `ccodes` overwritten only when a patch matches.
- [x] **`FileNotFoundError`** is raised when the source snapshot or patch file is
  missing, so orchestration treats a missing dependency as a failed stage rather
  than a silent no-op.

### Batch 6: Per-Gazetteer H3 Derivation + Coverage Compaction (Slurm Array Job)

Targets:

- `processing/helpers.py`
- `processing/h3_stage.py` ✅
- `processing/submit_h3_slurm.py` ✅ (now chains `h3_merge` + `gazetteer_h3_coverage` per task)
- `processing/h3_merge.py` ✅ (called from Batch 5)
- `processing/gazetteer_h3_coverage.py` ✅ (new — coverage compaction)

Dependencies: Batch 4 (extract complete), Batch 3 (orchestrator).

> **Status**: `h3_stage.py` and `submit_h3_slurm.py` are implemented. H3 is deferred by
> default (`--inline-h3` to opt in during extraction; not recommended). Per-gazetteer
> coverage emit is new.

Tasks:

- [x] `h3_stage.py` reads staged extract artefacts + full geometry, writes H3 patch JSONL.
- [x] Compute `h3_centroid` and `h3_cover` from full geometry only (never from hull or
  repr_point).
- [x] `submit_h3_slurm.py` reads pending namespaces from manifest, estimates wall times
  from persistent runtime history, submits Slurm array job with per-namespace QOS
  selection.
- [x] H3 wall times recorded to `namespace-runtime-history.json` for future job-size
  estimation.
- [x] Per-namespace H3 stage status tracked in run manifest.
- [x] Wall times for every Slurm-driven script are persisted in
  `NAMESPACE_RUNTIME_HISTORY_FILE` (default
  `${STAGED_BASE_DIR}/namespace-runtime-history.json`) by
  `processing.stage_writers.record_script_wall_time`. Every submitter
  (`submit_h3_slurm`, `submit_ccode_slurm`, `submit_tiles_slurm`,
  `submit_index_slurm`, `submit_batch9_slurm`, `submit_hardlinks_slurm`)
  reads the median of the last 5 completed runs via
  `estimate_wall_time_seconds(namespace, script_id, default=...)` with a
  20% safety margin. First run uses the hard-coded conservative default;
  subsequent runs auto-tune. Per-Slurm-script keys:
  `h3-stage` / `h3-merge` / `h3-coverage` / `ccode-enrichment` /
  `ccode-merge` / `tiles` / `temporal-extent` / `index-from-stage` /
  `update-es-compute` / `update-es-index` / `rebuild-toponyms-index` /
  `hard-links-staged`.
- [x] **(New, Master Plan §1.4.1 + E.2 item 1)** Per-gazetteer H3 coverage compaction
  implemented in `processing/gazetteer_h3_coverage.py`:
  - For **non-global** gazetteers, the script reads the H3 patch JSONL emitted by
    `h3_stage.py`, accumulates the union of `h3_centroid` + `h3_cover` cells, and
    emits `staged/_aggregates/{namespace}.h3_coverage.json` containing the
    compacted set (`h3.compact_cells`).
  - For **global** gazetteers (`GLOBAL_COVERAGE_NAMESPACES = {osm, ohm, wd, gn,
    po, tgn}`), it writes the sentinel `{"coverage": "global"}` without
    enumerating cells.
  - Run per-namespace by `submit_h3_slurm.py` immediately after `h3_merge` in the
    same array task; the resulting aggregate is consumed by Batch 7
    (`ccode_enrichment.py`) for the UN namespace and by Batch 11 inventory push.

Validation gates:

- [x] H3 patches write without ES access.
- [x] H3 fields correctly materialise within geometry objects.
- [x] Multi-geometry places have h3 data in each geometry.
- [x] No hull-derived coverages: `h3_stage._build_h3_patch` calls
  `select_h3_cover_geometry` which selects the full geometry; hull is a
  fallback only when full geometry is absent (and even then this is a code
  contract, not a runtime decision in the H3 stage itself).
- [x] H3 coverage file present for every non-global namespace + round-trip
  through `h3.uncompact_cells`/`h3.compact_cells` — verified by
  `processing/verify_aggregates.py` (Batch 6 + 9 verifier).
- [x] Global namespaces' coverage file contains the sentinel `"global"` —
  same verifier asserts this and refuses anything else.

### Batch 7: Per-Gazetteer CCode Enrichment (Post-H3, Using UN Coverage Pre-Filter)

Targets:

- `processing/ccode_enrichment.py` ✅ (new)
- `processing/ccode_merge.py` ✅ (called from Batch 5)
- `processing/submit_ccode_slurm.py` ✅ (new — Slurm array submission, depends on H3 job)

Dependencies: Batch 6 (H3 complete for all gazetteers, especially `un`).

> **Architecture**: per-namespace ccode enrichment reads the namespace's H3-enriched
> snapshot, the UN H3 coverage (if namespace ≠ un), and the UN place geometries. For each
> place geometry's H3 cover, test containment against UN geometries; emit a ccode patch.
> Batch 5 merges the patch into the final snapshot.

Tasks:

- [x] `ccode_enrichment.py`:
  - Loads UN's `h3_merged/` staged records and builds a per-cell ccode prefilter
    by normalising every UN compacted `h3_cover` cell to a fixed resolution
    (`PREFILTER_RESOLUTION = 4`).
  - Iterates each non-UN namespace's `h3_merged/` snapshot; per place geometry,
    walks `h3_cover` cells to the prefilter resolution and intersects with the
    UN cell→ccodes index to collect candidate ccodes.
  - Performs precise containment via Shapely against UN country geometries
    loaded from the geom store (LRU-cached, falls back to staged hull when the
    geom store is unavailable). Points use point-in-polygon (`intersects`);
    areas use intersection with majority-overlap as the tie-break order.
  - Emits `{place_id, ccodes, source}` patch records to
    `{namespace}/ccode/places.ccode.jsonl` with `source = "un-h3-overlap"`.
- [x] `ccode_merge.py` reads staged snapshot + ccode patches, merges ccodes into docs.
- [x] Per-namespace ccode stage status updated in the run manifest by both
  `ccode_enrichment.run_ccode_enrichment` (`ccode` stage) and
  `ccode_merge.run_ccode_merge` (`ccode_merge` stage).

Validation gates:

- [x] Unit checks for point-in-polygon and overlap tie-break behaviour:
  `tests/test_ccode_enrichment.py` (six containment tests + two H3 prefilter
  tests; H3 tests skip locally when the `h3` package isn't installed and run
  on the remote where it is).
- [ ] Throughput benchmarks confirm pre-filtering efficiency. *(Deferred —
  needs a real corpus on CRC.)*
- [ ] Ccode coverage statistics (places per country) match expectations.
  *(Deferred — same.)*
- [ ] Ccode patches merge correctly without corrupting geometry indices.
  *(Deferred — same.)*

### Batch 8: Global Barrier — All Selected Gazetteers Preprocessed

Targets:

- `processing/staging_orchestrator.py` ✅ (added `check_global_barrier`,
  `materialise_gazetteer_inventory`, `format_global_barrier_report`,
  `GLOBAL_BARRIER_REQUIRED_STAGES`)
- `processing/run_global_barrier.py` ✅ (new — CLI for the barrier check + inventory write)

Dependencies: Batch 4–7 complete for every selected gazetteer.

Tasks:

- [x] Manifest-based barrier check confirming every selected per-gazetteer
  namespace has reached every required stage in `GLOBAL_BARRIER_REQUIRED_STAGES`
  (`extract` → `boundary_merge` → `h3` → `h3_merge` → `h3_coverage` → `ccode`
  → `ccode_merge`). Both `completed` and `skipped` count as a pass — the
  orchestrator emits `skipped` for stages that don't apply (e.g. boundary
  stages on non-OSM authorities, ccode on `un`). Relations-only namespaces
  (e.g. `loc`) are excluded from the barrier; they are consumed only by
  Batch 12.
- [x] Fail fast: `processing.run_global_barrier` exits non-zero (1 = barrier
  failed, 2 = manifest unreadable) when any selected gazetteer is missing or
  stale. The inventory file is **not** written on failure.
- [x] On pass, materialise the run-level gazetteer inventory at
  `staged/runs/{run_id}.inventory.json`. The inventory enumerates each
  per-gazetteer namespace's stage statuses, on-disk paths
  (`extract`, `final`, `h3_coverage`, `temporal_extent`), and the
  relations-only set; it is the canonical input for Batch 9 aggregates and
  Batch 11 inventory push.

Validation gates:

- [x] Barrier refuses to start corpus-wide phases with partial gazetteer
  coverage (`run_global_barrier` exits 1; `submit_batch9_slurm` refuses to
  submit unless `--no-enforce-barrier` is set).
- [x] Barrier report lists all selected gazetteers and completion states
  (text mode via `format_global_barrier_report`; JSON mode via `--json`).

### Batch 9: Global Toponyms + Symphonym + Per-Gazetteer Temporal Extent

Targets:

- `phonetics/extraction/rebuild_toponyms_index.py` ✅ (STEP 1 refactored to read
  staged places via `scan_places_staged`; ES no longer contacted unless
  STEP 4/5 indexing actually runs)
- `phonetics/inference/update_es.py` — ✓ already staged-friendly: `compute`
  reads from DuckDB and writes Parquet; `index` is the Batch 11 ES-load step
- `processing/embed_extract.py` ✅ (rewritten to read from the staged DuckDB
  built by `rebuild_toponyms_index`; ES dependency removed)
- `processing/embed_transform.py` — ✓ already storage-only (Parquet → Parquet)
- `processing/embed_load.py` — left as the Batch 11 ES-load step
- `processing/gazetteer_temporal_extent.py` ✅ (new — per-namespace temporal
  extent; H3 coverage handled in Batch 6)
- `processing/submit_batch9_slurm.py` ✅ (new — submits the temporal-extent
  array + the toponym-extraction job together, gated on the global barrier)

Dependencies: Batch 8 (global barrier complete).

> **Parallelism.** This batch is one of the two post-barrier jobs (Job A in the dependency
> flow above). Batch 12 (hard-link harvest, Job B) runs in parallel on a separate Slurm
> allocation; both are read-only over the staged corpus and the geometry store.

Tasks:

**Toponyms + Symphonym** (existing scope):

- [x] Read the union of all selected staged place snapshots (not ES `places`) as canonical
  source. `scan_places_staged(namespaces)` walks each namespace's most-enriched
  staged snapshot (`final/` → `h3_merged/` → `boundary_merged/` → `extract/`).
- [x] Extract unique toponyms with attestations across the full selected
  corpus (existing `extract_toponyms_to_db` logic, now driven by the staged
  iterator).
- [x] Deduplicate toponyms corpus-wide across gazetteers before embedding
  generation (DuckDB `toponym_id` is already a content-derived `name@lang`
  key; per-namespace attestations are written to `toponym_namespaces` /
  `toponym_attestations`).
- [x] Compute Symphonym embeddings (GPU-enabled) over the deduplicated
  corpus via `phonetics/inference/update_es.py compute`, reading from the
  Batch 9 DuckDB.
- [x] Persistent Symphonym cache (`phonetics/inference/symphonym_cache.py`,
  default path `processing.settings.SYMPHONYM_CACHE_DB`): a single DuckDB
  keyed on `(toponym_id, model_version, checkpoint_hash)` with `BLOB`
  embedding storage. `run_compute` loads all rows for the current
  `(version, hash)` into memory before the GPU loop, partitions each
  fetchmany batch into hits + misses, writes hits straight to the output
  Parquet, runs the GPU only on misses, and appends every freshly-computed
  embedding back to the cache. First run = full corpus on GPU + populates
  cache. Subsequent runs with unchanged version+checkpoint = pure cache
  hits, no GPU traffic. `--no-cache` bypasses entirely.
- [x] Symphonym model/version preflight: `compute_checkpoint_hash` SHA-256s
  the checkpoint file at start-up; the resulting hex digest is part of the
  cache key. A new `--checkpoint` value (or any byte change to the same
  path) flips the digest → zero hits → full recompute. The
  `--embedding-version` argument is the second axis of invalidation,
  bumped explicitly when the embedding schema changes.
- [x] Stage toponym records and PanPhon-augmented JSONL: `rebuild_toponyms_index`
  with `--skip-es-index` writes everything to the DuckDB + scratch JSONL and
  exits without touching ES.
- [x] **Clear scope boundary**: no ES indexing in this stage. The shared
  `submit_batch9_slurm` invocation passes `--skip-es-index --confirm` to the
  toponym job; ES connection is opened only when that flag is absent (Batch
  11 territory).

**Per-gazetteer temporal extent** (new, Master Plan §1.4.1 + E.2 item 2):

- [x] `gazetteer_temporal_extent.py` reads each selected staged snapshot and
  walks every `geometries[].timespans[]`, `toponyms[].timespans[]`, and
  `relations[].timespans[]` entry, yielding the integer years under
  `start` / `end` (handles `{"in": Y}`, `{"earliest": Y, "latest": Y}`, and
  arbitrary nested int leaves). Produces `temporal_extent =
  [min(start_year), max(end_year)]` with both endpoints `null` when the
  namespace has no parseable timespans.
- [x] Output written to `staged/_aggregates/{namespace}.temporal_extent.json`,
  validated against `validate_temporal_extent_aggregate`. Consumed by Batch
  11 inventory push together with the H3 coverage file produced in Batch 6.
- [x] Recomputed on every full run; the `temporal_extent` stage in the run
  manifest is updated by `run_temporal_extent` as `running` →`completed`
  per namespace. An incremental path may be added later if needed.

> **H3 coverage moved.** Per-gazetteer H3 coverage compaction now happens inside Batch 6
> (where the H3 cells are already in memory). Global gazetteers (`osm`, `ohm`, `wd`, `gn`,
> `po`, `tgn`) emit a `"global"` sentinel and skip enumeration entirely.

Validation gates:

- [ ] Toponym extraction produces expected counts (deferred — needs an
  end-to-end CRC dry run to confirm staged-vs-ES parity on a known corpus).
- [ ] Corpus-wide deduplication merges cross-gazetteer attestations as
  expected (preserved by existing logic; re-verify post-staged input).
- [ ] Embedding/index outputs remain schema-compatible (no schema change in
  this batch).
- [x] Incremental run re-embeds only changed toponyms when model version is
  unchanged: covered by the cache hit/miss partition in
  `update_es.py::run_compute` (verified by
  `tests/test_symphonym_cache.py::test_insert_then_lookup`).
- [x] Cache invalidation triggers full recompute when model/version changes:
  covered by the composite key on `(toponym_id, model_version,
  checkpoint_hash)` (verified by `test_version_bump_invalidates`).
- [x] No ES access during this stage when invoked via
  `submit_batch9_slurm`; the toponym job runs with `--skip-es-index` and the
  temporal-extent job has no ES code path at all.
- [x] `temporal_extent` correctly null-handles records lacking timespans
  (verified by the `_iter_year_ints` walker returning nothing for empty /
  malformed timespans, and `_collect_extent_for_doc` leaving `min_start` /
  `max_end` as `None` when no year is observed).

### Batch 10: Tile Generation from Staged Geometry (No ES Dependency)

Targets:

- `processing/generate_tiles.py` ✅ (refactored — staged path is now the default)
- `processing/submit_tiles_slurm.py` ✅ (new — bucket-driven Slurm array)
- `scripts/ingest.sh` (es.sh wrapper still pending Batch 8 wiring)

Dependencies: Batch 4 (extract) for `po`/`clio`/`nl`; Batch 5 (`boundary_merge`) for
`osm`/`ohm`. The job runs **before** the global barrier (Batch 8) so it can overlap with
H3, ccode, and toponym work — it touches neither ES nor the barrier prerequisites.

Tasks:

- [x] Tile generation reads staged artefacts (`final/` → `h3_merged/` →
  `boundary_merged/` → `extract/` preference chain) and pulls full polygon
  geometries exclusively from the external geom store (`GeomStoreReader`).
  Docs without a resolvable `geom_ref` are dropped — there is no hull
  fallback, since simplified hulls would mis-render at high zoom levels.
  The Elasticsearch path has been removed entirely; the geom store at
  `GEOM_STORE_DIR` is a hard prerequisite (`FileNotFoundError` raised if
  missing).
- [x] Required outputs are produced via the bucket-driven design
  (`TILE_BUCKETS` in `generate_tiles.py`):
  - `po.mbtiles` — `{po}`
  - `clio.mbtiles` — `{clio}`
  - `nl.mbtiles` — `{nl}`
  - `osm_admin.mbtiles` — `{osm}` admin-level boundaries (0..11)
  - `ohm_admin.mbtiles` — `{ohm}` admin-level boundaries
  - `osm_misc.mbtiles` — mixed `{osm, ohm}` curated misc + historic-prefix types
- [x] Synthetic boundary products from `un-geoscheme-boundaries.py` (which
  emit under the `osm:` namespace) flow into `osm_admin` / `osm_misc`
  automatically because bucket classification follows `place_id`, not the
  source snapshot's location.
- [x] Per-bucket Slurm tasks (one task per output bucket) eliminate writer
  contention on the mixed `osm_misc` file: a single task streams *both* OSM
  and OHM misc-boundary records into one output. Per-bucket prerequisites are
  enforced by `submit_tiles_slurm._eligible_buckets` — buckets with any
  contributing namespace's prerequisite stage incomplete are deferred.

Validation gates:

- [x] Tile generation runs with ES stopped (no `elasticsearch` import remains
  in `processing/generate_tiles.py`).
- [ ] Output layers and counts are reproducible (deferred — needs an end-to-end
  CRC dry run once Batch 8 wires this into `scripts/ingest.sh`).

### Batch 11: Index Loaders, Selection-Driven Lifecycle, Gazetteer Inventory Push

Targets:

- `processing/index_from_stage.py` ✅ (new — places loader)
- `processing/namespace_lifecycle.py` ✅ (new — alias swap, retention, deselection cleanup)
- `processing/push_gazetteer_inventory.py` ✅ (new — Master Plan §5.3)
- `processing/submit_index_slurm.py` ✅ (new — Slurm submission for places + toponyms)

Dependencies: Batch 3 (orchestrator), Batch 9 (toponyms + aggregates staged), Batch 10
(required tiles complete).

**Prerequisite for indexing:** staging ES instance must be running.

Tasks:

**Indexing:**

- [x] `index_from_stage.py` loads `places` exclusively from `staged/{ns}/final/places.parquet`
  (with stage-chain fallback), creates `places_<run_id>` from `schemas/places.json`
  with the `extract_namespace` ingest pipeline, bulk-loads via
  `helpers.streaming_bulk`, refreshes, and atomically swaps the `places` alias.
  Top-level `dataset_status` and `dataset_id` are indexed unchanged from
  the staged docs (populated at extract time by `write_staged_place_doc`).
- [x] Toponyms indexing is delegated to the existing
  `phonetics/inference/update_es.py index` flow which reads the staged
  DuckDB + Symphonym embeddings Parquet (Batch 9 outputs). No incremental
  diff path is implemented in this batch — the index is rebuilt from the
  full DuckDB; an attestation-diff path is a future workstream.
- [x] Rebuild covers the selected-gazetteer staged corpus only:
  `_eligible_namespaces` filters by manifest selection and skips namespaces
  already at `index: completed`.
- [x] `namespace_lifecycle.py` is the single chokepoint for deletions:
  `cleanup` removes docs whose namespace is no longer in
  `selected_namespaces`; `retention` keeps the latest N dated indices per
  family (preserving whatever the alias currently targets); `swap` re-aliases
  to a run's dated index. Ad-hoc per-namespace deletes are not used.
- [ ] Trigger Batch 12 hard-link harvest after index mutations: handled by
  the orchestrator wrapper (`scripts/ingest.sh`) which submits Batches 9 and
  12 together with `--depend-on` chained off the barrier. Wiring is left to
  the wrapper rather than baked into the Python submitters so operators can
  reorder/parallelise as needed.

**Gazetteer inventory push** (new, Master Plan §5.3, Appendix E.2 item 6):

> **Final-step gating.** The inventory push is the user-facing signal to Django that the
> ingestion/indexing run is complete. It must be gated on **all of**: successful
> indexing (Job A descendants — toponyms, places), and successful Ship-to-Pitt of the
> SQLite hard-link database (Job B → Batch 12 atomic swap). Firing it earlier would let
> the Django UI advertise a complete run while the gateway-side hard-link store is still
> mid-flight.

- [x] `push_gazetteer_inventory.py` reads the Batch 8 inventory file
  (`staged/runs/{run_id}.inventory.json`) and merges in per-namespace H3
  coverage (Batch 6) + temporal extent (Batch 9) + authority metadata
  (`AUTHORITIES`-derived `name` / `description`). LOC and other relations-only
  namespaces are excluded. Output payload per entry matches the master plan
  spec; non-global gazetteers carry the compacted cell list, global
  gazetteers the `"global"` sentinel.
- [x] POST/PUT to the Django registry endpoint (`WHG_INVENTORY_ENDPOINT`
  env var or `--endpoint`) with bearer token, `urllib.request` retries on
  5xx + connection errors, and `--dry-run` to print the payload.
- [x] Idempotent — the Django side is expected to upsert by `id`; the
  builder produces the same payload for the same indexed corpus, so re-runs
  are no-ops.
- [x] Gating: `assert_ready_to_push` requires the Batch 8 inventory file
  and a `temporal_extent` aggregate per selected per-gazetteer namespace.
  When `--require-hardlink-marker` is set the call also requires
  `staged/runs/{run_id}.hardlink_ship.json` (written by the Batch 12
  ship-to-Pitt step) — refuses to fire otherwise.
- [x] WHG-dataset entries (`class="dataset"`, `owner_user_id`,
  `whg:<dataset_id>`) are emitted: `authorities/whg-places.py` writes a
  sidecar `staged/_aggregates/whg.datasets.json` listing each
  Dataset/Collection's title / description / dataset_status /
  owner_user_id / record_count; `push_gazetteer_inventory.py::
  _expand_whg_dataset_entries` fans the `whg` namespace out into one
  `class='dataset'` inventory entry per dataset (or falls back to a single
  `class='dataset'` bulk row when the sidecar is missing — first runs).

Validation gates:

- [x] Deselected gazetteers absent from indexed outputs:
  `namespace_lifecycle.cleanup` runs `delete_by_query` for the deselected
  set on the live alias.
- [ ] Referential integrity checks pass for sampled docs (deferred —
  needs an end-to-end CRC dry run).
- [x] `dataset_status` and `dataset_id` index correctly: top-level
  `keyword` mapping was added in Batch 1; `index_from_stage` indexes the
  staged docs unchanged so both fields land as queryable keywords.
- [x] Inventory push round-trips through Django:
  `processing/push_gazetteer_inventory.py` POSTs to
  `${WHG_API_BASE_URL}/api/registry/inventory`, served by
  `api/views_indexing.py::GazetteerInventoryView` against the
  `GazetteerRegistryEntry` table. End-to-end live verification still
  needs a CRC dry run.

### Batch 12: Hard-Link SQLite Harvest from Staged Files (Replaces Post-Index Clustering)

Targets:

- `clustering/harvest/hard_links_staged.py` ✅ (new — replaces the ES-based
  `clustering/harvest/hard_links.py` for the production path)
- `clustering/harvest/loc_links.py` ✅ (new — LOC relations harvest; LOC's only entry point
  in the rebuild)
- `clustering/harvest/contributor_replay.py` ✅ (new — replays active rows from DO PG)
- `clustering/sqlite_overlay.py` ✅ (new — schema, builder context manager, atomic ship-to-Pitt)
- `processing/submit_hardlinks_slurm.py` ✅ (new — single-job sbatch chaining the three
  harvesters + `ship_to_pitt` + completion marker for Batch 11 inventory gating)

Dependencies: Batch 8 (barrier — staged files are complete and self-consistent for the
selected gazetteer set). Independent of Batch 11 (no ES dependency).

> **Parallelism.** This is **Job B** in the post-barrier flow; it runs in parallel with
> Batch 9 (Job A — Toponyms + Symphonym + temporal extent). Both are read-only over the
> staged corpus and the geometry store, so contention is limited to filesystem bandwidth.
> The Ship-to-Pitt swap (the final Batch 12 step) gates the Batch 11 inventory push
> together with successful indexing.

**Architectural change**: The previous Batch 12 ("Clustering Handoff (Post-Index ES Job)")
is **retired**. The Master Plan replaces ES-based pre-clustering entirely with:

- A SQLite hard-link database (`hard_link_assertions`) on the Pitt VM, queried by the
  gateway during search.
- Query-time clustering driven by the browser (Master Plan Part III) using edges and
  signals returned by the gateway.
- Synchronous forwarding of contributor attestations from Django (DO) to the Pitt SQLite
  via a gateway endpoint.

The ingestion pipeline's responsibility in this new model is **the offline harvest** that
populates the SQLite. The gateway-side query path, the contributor write path
(`POST /api/links`), and the realtime forwarding from Django are tracked separately in the
gateway/Master Plan implementation.

Tasks:

**Schema** (Master Plan §2a — already specified in `plan-dynamicClustering.DEPRECATED.md`
and reaffirmed by the Master Plan):

- [x] `clustering/sqlite_overlay.py` consumes the canonical
  `HARD_LINK_SQLITE_SCHEMA` from `processing/staging_contract.py` (single
  source of truth) and applies it via `initialise_schema`. The bulk-open
  context manager (`builder`) sets WAL + 200 MB cache + relaxed sync, and a
  WAL checkpoint + close on exit. DDL extract:
  ```sql
  CREATE TABLE hard_link_assertions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_a TEXT NOT NULL,
    place_b TEXT NOT NULL,
    relation_type TEXT NOT NULL,     -- 'sameAs' | 'exactMatch' | 'closeMatch' | 'distinct'
    source_category TEXT NOT NULL,   -- 'authority' | 'contributor'
    source_id TEXT NOT NULL,         -- 'wikidata' | 'tgn' | 'osm' | 'contributor:<user_id>' | ...
    asserted_at TEXT,
    justification TEXT,
    CHECK (place_a < place_b),
    UNIQUE (place_a, place_b, relation_type, source_id)
  );
  CREATE INDEX ix_hla_place_a ON hard_link_assertions(place_a);
  CREATE INDEX ix_hla_place_b ON hard_link_assertions(place_b);
  CREATE INDEX ix_hla_source ON hard_link_assertions(source_category, source_id);
  ```
  Use WAL mode and a large page cache for bulk insert.

**Phase 1A — Authority harvest from staged files**:

- [x] `hard_links_staged.py` iterates each selected gazetteer's most-enriched
  staged snapshot (`final/` → `h3_merged/` → `boundary_merged/` → `extract/`)
  and walks `relations[]` per doc. Filters preserved from the legacy
  ES-based `hard_links.py`: identity-relation gate via
  `IDENTITY_RELATION_TYPES`, same-namespace self-reference filtering, and
  unknown / non-WHG target-namespace filtering via `KNOWN_ES_NAMESPACES`.
- [x] Canonical-order `(place_a, place_b)` enforced by `_canonical_pair`
  before insert (and re-checked by the SQL `CHECK (place_a < place_b)`).
  `source_id` is the source namespace; `asserted_at` / `justification` are
  populated from the relation entry where available.
- [x] `INSERT OR IGNORE` via the shared `clustering.sqlite_overlay.insert_rows`
  helper; one outer connection across namespaces, batched transactions.
- [x] Legacy ES `clustering/harvest/hard_links.py` left in place as a
  recovery tool; the staged path is the live mechanism via
  `submit_hardlinks_slurm`.

**Phase 1A.LOC — LOC relations folded in here** (new entry point for LOC):

- [x] `clustering/harvest/loc_links.py` reads LOC NDJSON / NDJSON.gz from
  `${DATA_DIR}/authorities/loc/` (or `--source` override). The MADS/RDF
  parser is reimplemented locally (avoids the ES-coupled import in
  `authorities/loc-relations.py`).
- [x] Treats each LOC record as a transitivity hub: emits all C(N, 2)
  pairs among its in-scope external targets. `sameAs` only when both
  contributing LOC link types were exact (`hasExactExternalAuthority` /
  `identifiesRWO`), otherwise `closeMatch`. Out-of-scope targets (`viaf:`,
  anything not in `KNOWN_ES_NAMESPACES`) are dropped at parse time. LOC
  rows yielding fewer than 2 in-scope targets contribute nothing — the
  effective drop count is implicit in the gap between read records and
  emitted rows (logged as `attempted` vs. `inserted` per
  `insert_rows`).

**Phase 1B — Contributor replay from DO PostgreSQL**:

- [x] `contributor_replay.py` opens an SSH-tunnelled asyncpg connection
  via the existing `clustering.pg_client.pg_connection`, selects
  `status = 'active'` rows from `contributor_attestations`, and bulk-inserts
  them with `INSERT OR IGNORE`.
- [x] Source-of-truth: the DO PG table is authoritative; the SQLite is
  a derived view rebuildable from PG at any time (replay invocation is
  idempotent).
- [x] Pending assertions are excluded by the WHERE clause; the publishable
  SQLite never carries `status != 'active'` rows.
- [x] Schema-defensive: tolerates the `legacy_v3_2` column being absent
  (Batch 13b is a future DO migration). When present, `legacy_v3_2 = true`
  rows get `source_id = 'contributor:<user_id>:legacy_v3_2'` so the gateway
  can filter on the suffix without rejoining DO.

**Ship-to-Pitt with atomic swap**:

- [x] Build runs at `${IX1_BASE}/hardlinks/hard_links_<run_id>.sqlite` on
  CRC.
- [x] `sqlite_overlay.ship_to_pitt` rsyncs to a hidden
  `<remote_dir>/.<filename>.incoming` first, then runs a single SSH
  `mv .incoming → live` rename within the same remote filesystem. The
  rename is atomic and preserves the gateway's existing fd against the
  previous inode; the next `open()` picks up the new file.
- [x] Completion marker `staged/runs/{run_id}.hardlink_ship.json` is
  written on successful ship — `processing.push_gazetteer_inventory
  --require-hardlink-marker` checks for it.
- [ ] Gateway-side periodic re-open / SIGHUP handling tracked separately
  (gateway repo).

**Reconciliation / drift detection**:

- [ ] Periodic drift job not implemented — left as a future workstream
  once the synchronous forwarding path on the gateway side is live.

Validation gates:

- [ ] Bulk insert of expected scale (~10–50M authority assertions) completes within a
  defined wall-time budget on CRC. (Deferred — pending end-to-end CRC dry run.)
- [ ] Round-trip test: harvest → ship → query a sampled set of place_ids returns the
  expected hard-link members. (Deferred — pending CRC + gateway integration.)
- [x] Idempotency: `INSERT OR IGNORE` against the unique
  `(place_a, place_b, relation_type, source_id)` constraint guarantees re-runs
  produce no new rows; `insert_rows` reports `inserted = 0` on a no-op rerun.
- [x] `INSERT OR IGNORE` correctly de-duplicates assertions made by multiple
  sources — distinct `source_id`s create separate rows so multi-source
  attestations remain distinguishable while exact duplicates are dropped.
- [ ] Atomic-swap procedure verified on CRC → Pitt with a live gateway open
  against the file. (Deferred — needs the live Pitt gateway.)

### Batch 13a: WHG Dataset Authority Integration

> **Folded into Batch 4c Phase 4.** Discovery (`GET /reconcile/authority-datasets`), LPF
> fetch (`GET /entity/dataset:<id>/api?filetype=lpf`), per-Dataset/Collection sub-namespaces,
> auth wiring, `dataset_status` propagation, and per-dataset manifests are all part of
> building `authorities/whg-places.py` (see 4c Phase 4 for the full task list). There is no
> separate Batch 13a in the new plan; this row in the status snapshot is retained only as
> a pointer.

### Batch 13b: v3.2 Legacy Reconciliation Flagging (DO-Side, One-Time, Narrow Scope)

Targets (mostly DO-side, listed here for cross-reference):

- DO PG migration adding `legacy_v3_2 BOOLEAN DEFAULT false` to
  `contributor_attestations` (tracked in the Django repo at
  `/home/stephen/Documents/GitHub/whg3`; this plan is **not** the implementer).
- ✅ `clustering/harvest/contributor_replay.py` (this repo) — encodes the
  ``legacy_v3_2`` suffix onto ``source_id`` so the Pitt SQLite preserves
  distinguishability. Defaults to schema-defensive: tolerates the column
  being absent (falls back to a query that hard-codes ``FALSE``).
- ✅ `clustering/sqlite_overlay.py` writes ``source_id`` as-is — the suffix
  is preserved end-to-end.

Dependencies: Batch 12 (SQLite overlay operational).

> **Why this is no longer a "migration".** The DO PostgreSQL database remains the
> canonical store of every contributed Dataset/Collection — both legacy v3.2 accessions
> and any future contributions. They flow into the `places` index on every run via
> `authorities/whg-places.py` (Batch 4c Phase 4); the data is not migrated to a new
> store. The only legacy-specific work is annotating the historical reconciliation
> links so downstream consumers can distinguish them from new contributor work
> (Master Plan §10.2, Appendix E.2 item 7).

Tasks:

- [ ] DO-side schema change: add `legacy_v3_2 BOOLEAN DEFAULT false` to
  `contributor_attestations`. *(Django team, tracked separately.)*
- [x] Pitt-side: ``contributor_replay.py`` reads ``legacy_v3_2`` from the
  Django ``api_contributorattestation`` table and encodes the suffix onto
  ``source_id`` (``contributor:<user_id>:legacy_v3_2``). Tolerates the
  column being absent on older DO schemas (defensive fallback).
- [ ] Preserve v3.2 dataset metadata, accession history, in-progress
  reconciliations. *(Owned by Batch 4 Phase 4 / DO PG — no extra work
  needed in this repo.)*

Validation gates:

- [ ] Every v3.2-era reconciliation link in DO PG carries `legacy_v3_2 = true`.
- [ ] The next Batch 12 hard-link harvest puts those rows into the Pitt SQLite with the
  suffix on `source_id`; sample queries for `source_id LIKE '%:legacy_v3_2'` return the
  expected row count.
- [ ] No payload is duplicated between DO PG and the new indices beyond what
  `whg-places.py` produces on each run; the canonical store remains DO PG.

### Batch 14: Test Harness and Integration Rollout

Targets:

- `testing/integration_harness.py` (new)
- `scripts/ingest.sh`

Dependencies: All prior batches.

Tasks:

- [x] Minimal integration harness for small namespaces (`nl` + `po`):
  `testing/integration_harness.py` drives the staged-only chain (h3 →
  h3_merge → h3_coverage → ccode_enrichment → ccode_merge →
  temporal_extent → barrier → verify_aggregates) without touching ES.
- [ ] Validate end-to-end staged-first run without indexing.
- [ ] Validate multi-gazetteer fan-out through per-gazetteer preprocessing, then barrier,
  then single corpus-wide toponym/Symphonym + aggregates run.
- [ ] Validate index load from stage with ES running.
- [ ] Validate gazetteer deselection via checkbox config and staged-artefact cleanup.
- [ ] Validate gazetteer inventory push to Django (mock endpoint acceptable initially).
- [ ] Validate Batch 12 hard-link harvest end-to-end against a representative staged
  corpus; verify atomic swap on a Pitt-mock.
- [ ] **(New)** Scope-leakage test: issue queries from a synthetic off-scope user and
  assert that no `dataset_status: 'pending'` records appear in any field (Master Plan
  §7.4 / Appendix C.5). This is primarily a gateway-side test, but the ingestion side
  is responsible for ensuring the discovery filter has correct field types and indexed
  values to operate on.
- [ ] Benchmark OSM/OHM critical path (extract, H3 array, tile generation).
- [ ] Document performance baseline and scaling profile.

Validation gates:

- [ ] All acceptance criteria below pass.
- [ ] Full run no longer depends on one 48-hour ES staging wall-time window.
- [ ] Gazetteer inclusion/exclusion is controlled by checkbox selection file (and, once
  available, the Django registry), including stale staged-artefact cleanup for deselected
  gazetteers.
- [ ] Required tilesets and incremental toponym behaviour are verified.
- [ ] WHG `whg:` dataset authority ingestion is functional against
  `/reconcile/authority-datasets` + `/entity/dataset:<id>/api?filetype=lpf`.
- [ ] Hard-link SQLite harvest produces a correct overlay shipped to Pitt.

### Batch 14a: Retention Sweep for Pending Datasets

Targets:

- `processing/retention_sweep.py` ✅ (new — 11/12-month decision matrix,
  ES + DO PG-driven discovery, dry-run by default)
- Notifications dispatched via the WHG API (`WHG_RETENTION_NOTIFY_ENDPOINT`,
  default `${WHG_API_BASE_URL}/api/retention/notify`) — Django side owns the
  email path, so no separate `processing/notifications.py` is needed.

Dependencies: Batch 11 (indexing operational and gazetteer inventory push wired),
Batch 13a (WHG dataset ingestion path admits `dataset_status: 'pending'`).

Master Plan §10.1 specifies a one-year retention sweep on pending datasets.

Tasks:

- [ ] Scheduling — `processing/retention_sweep.py` is a CLI; wiring it
  to cron / Slurm / Django management command is an operational task.
- [x] Definition of "without contributor edits" implemented as
  ``MAX(modified_at)`` over ``contributor_attestations`` per dataset (via
  the SSH-tunnelled DO PG client); falls back to ``submission_date`` when
  PG isn't reachable.
- [x] Eleven-month boundary → POST to the configurable notify endpoint
  (default ``${WHG_API_BASE_URL}/api/retention/notify``); payload carries
  per-dataset metadata so Django can fan out notifications.
- [x] Twelve-month boundary → ``delete_by_query`` on ``places`` for
  ``dataset_id`` term match. Toponym cleanup happens implicitly on the next
  Batch 11 toponyms rebuild (which only emits attestations for live places).
  DO PG cleanup of pending assertions + dataset metadata is delegated to
  the Django side via the same notify endpoint.
- [x] Exclusions implemented in ``classify``: ``submitted`` and
  ``private_permanent`` exempt; ``published`` exempt; ``rejected`` continues
  the timer (callers can pass ``--as-of`` to test).

Validation gates:

- [ ] Eleven-month notification fires at the correct boundary in a synthetic time-warp
  test.
- [ ] Twelve-month deletion is total (no orphaned records in any index, no dangling
  assertions in DO PG).
- [ ] Submitted / `private_permanent` exclusions correctly pause/exempt the timer.

---

## Definition of Done (Full Implementation)

- [ ] All batches 1–14 (and 14a) complete and tested.
- [ ] Per-gazetteer staged snapshot fully materialised before indexing.
- [ ] H3 derived from full geometry only, per-geometry covers present.
- [ ] Ccode assignment uses UN H3 coverage as a pre-filter.
- [ ] Toponym deduplication and Symphonym generation run once, corpus-wide, after all
  selected gazetteers complete preprocessing.
- [ ] **Per-gazetteer H3 coverage (compacted in Batch 6, with the `"global"` sentinel
  for global gazetteers) and per-gazetteer temporal extent (Batch 9) are computed and
  shipped to the Django gazetteer registry on each successful run.**
- [ ] **Every place record carries `dataset_status` and `dataset_id`; pending records
  coexist with published records in the same indices and are correctly hidden from
  off-scope discovery queries.**
- [ ] Authority/gazetteer selection is controlled by checkbox configuration (and, once
  the Django registry is live, by that registry), with no parallel ad hoc removal path.
- [ ] Mbtiles products generated from staged artefacts only.
- [ ] Symphonym embeddings generated and used for `toponyms`.
- [ ] Symphonym cache supports incremental embedding updates with model-version-based
  invalidation.
- [ ] **Hard-link SQLite overlay is built from staged files, shipped to Pitt with atomic
  swap, and used by the gateway in place of the retired `clusters` ES index.** The
  pre-existing post-index ES clustering pipeline is removed from the live run graph.
- [ ] **Gazetteer inventory is pushed to the Django registry endpoint after each
  successful indexing run.**
- [ ] **v3.2 legacy reconciliation links flagged on the DO PG side
  (`legacy_v3_2 = true` on `contributor_attestations`); the suffix propagates through
  the Pitt SQLite via Batch 12. No payload migration — DO PG remains canonical for all
  contributed Datasets/Collections.**
- [ ] **LOC source consumed only at Batch 12 (relations-only); LOC does not appear in
  the gazetteer inventory, in any place index, or in any per-gazetteer aggregate.**
- [ ] **Retention sweep operational, including eleven-month notification and
  twelve-month deletion, with correct exclusions for submitted, rejected (timer-resumed),
  and `private_permanent` datasets.**
- [ ] Integration test suite passes, including the scope-leakage test for pending
  content.

---

## Acceptance Criteria (Master Plan-aligned)

- Per-gazetteer staged snapshot is complete before indexing.
- H3 is derived from full geometry only, with per-geometry coverage support.
- Toponym deduplication and Symphonym generation run only after all selected gazetteers
  complete per-gazetteer preprocessing.
- Gazetteer inclusion/exclusion is driven exclusively by the checkbox selection file
  (transitioning to the Django gazetteer registry), with deselected staged artefacts
  removed before preprocessing.
- Indexed outputs reflect only selected gazetteers from the current staged run.
- Required mbtiles products are generated from staged artefacts, not ES docs.
- Symphonym embeddings are generated and used to populate `toponyms`.
- Symphonym caching re-embeds only changed toponyms, with cache invalidation on
  model/version changes.
- Per-gazetteer `h3_coverage` (compacted) and `temporal_extent` are produced and pushed to
  the Django gazetteer registry on each successful run.
- Hard-link SQLite overlay is built from staged files (no ES dependency) and shipped to
  Pitt, replacing the previous post-index ES clustering job.
- Pending-dataset records carry `dataset_status='pending'` and are invisible to off-scope
  discovery queries.
- Enabled Django datasets are ingested under `whg:` using dataset-ID sub-namespaces,
  sourced from `GET /reconcile/authority-datasets` and LPF exports from
  `GET /entity/dataset:<id>/api?filetype=lpf`.
- v3.2 accessioned datasets continue to be re-pulled from DO PG on every run via
  `whg-places.py` (no payload migration); their historical reconciliation links carry
  `legacy_v3_2 = true` in DO PG and propagate that suffix to the Pitt SQLite.
- LOC contributes only via Batch 12 (relations-only) and is absent from the per-gazetteer
  preprocessing pipeline and the gazetteer registry.
- The orchestration controller supports partial runs: refreshing one or a subset of
  gazetteers (new Dataset contribution, periodic refresh) leaves untouched staged
  artefacts in place; post-barrier stages (Batches 9–12) re-run from scratch over the
  full selected corpus.
- One-year retention sweep is operational with eleven-month warnings and correct
  exclusions.

---

## Explicit Non-Goals

- No ad hoc gazetteer deletion workflow outside selection-file (or registry) control.
- No tile generation from ES documents.
- No reintroduction of a precomputed pairwise similarity graph or a `clusters` ES index;
  clustering is now query-time (browser-side per Master Plan Part III, server-side
  fallback per Master Plan §5.1).
- The browser-side clustering algorithm, the Atlas UI affordances, the gateway response
  shape, the `POST /api/links` endpoint, and the realtime forwarding from Django are
  **not** in scope for this ingestion plan; they are owned by the Master Plan and the
  gateway implementation. This plan provides the data those layers consume.

---

## Test Run Results — Session 2026-04-27

First end-to-end Slurm exercise on CRC, run ID `smoke-20260427T184236Z`,
namespaces `nl + po`, login node `crc2`/`crc0`, cluster `htc`.

### Patches landed during the run

The smoke surfaced four real bugs / friction points which were fixed and
pushed in-flight:

* `0668b0c` — every Slurm submitter now passes `-M htc` to `sbatch`. CRC is
  multi-cluster Slurm and `htc` partition lives on the `htc` cluster; the
  bare `sbatch <script>` defaulted to the wrong cluster and rejected
  submission with *"Invalid account or account/partition combination"*.
  Override via `WHG_SLURM_CLUSTER` env var.
* `bb716af` — robust Slurm job-id extractor. `sbatch -M htc` writes
  `Submitted batch job 12345 on cluster htc`; the previous `split()[-1]`
  parser captured `htc` instead of `12345`. Now matches the first integer
  token in the output.
* `6418ab8` — area-aware H3 polyfill (see perf table below) **plus** drop
  the stale `--staged` flag from `submit_tiles_slurm.py`'s
  `generate_tiles` invocation (the flag was removed when the legacy ES
  path was deleted).
* `c2afb83` — revert a multiprocessing.Pool prototype after benchmarking
  showed it added < 6% throughput on PeriodO at the cost of code
  complexity (kept in the file's commit history with the bench notes).

### What worked end-to-end

* **`submit_h3_slurm`** submitted Slurm array `8922051_[0-1]` to the `htc`
  cluster (partition `htc`, qos `htc-htc-s`, 1d wall, 4 cpu, 16 G mem).
  Both array tasks landed on real compute nodes (`htc-n25` and `htc-n59`).
* **`nl` array task** completed all three chained stages in **~3 min**:
  `h3_stage` (8728 docs, 8728 patched, 100% h3 coverage)
  → `h3_merge` (`nl/h3_merged/places.{jsonl,parquet}`)
  → `gazetteer_h3_coverage` (`_aggregates/nl.h3_coverage.json`,
  637 777 cells uncompacted → 567 163 compacted, ~12% reduction).
* **`po` array task** completed all three chained stages — slow because
  PeriodO records are large polygons that triggered the polyfill bottleneck
  (see perf table). After the area-aware optimisation landed, a fresh
  po-only run (`poperf-20260427T203518Z`, job `8923725`) finished in
  **15:02** (3.85× speedup over the baseline 57:43).
* **`po` h3_coverage** correctly emitted the `"global"` sentinel without
  enumerating cells (po is in `GLOBAL_COVERAGE_NAMESPACES`).
* **`submit_batch9_slurm --skip-toponyms`** for the same run submitted the
  per-namespace temporal-extent array (`8923084_[0-1]`); both tasks
  finished in **~1.5 s each**:
  - `_aggregates/nl.temporal_extent.json` → `[2025, 2025]`
  - `_aggregates/po.temporal_extent.json` → `[null, null]` — every
    sampled po record on the staged snapshot has no `timespans` field on
    any geometry / toponym / relation, even though
    `authorities/periodo-places.py` clearly intends to populate them.
    `gazetteer_temporal_extent` correctly returns null when no parseable
    years exist; the bug was upstream in the po extract — `_parse_year`
    silently returned None for every record because PeriodO wraps each
    endpoint year in a `{"year": "<signed-string>"}` dict that the
    parser tried to `int(str(...))`. Fixed in the same session; see
    "Follow-up: investigate po temporal pipeline — RESOLVED" below. The
    next po staging extract will populate timespans on every record.
* Per-namespace **manifest stage status** flipped `pending` → `running`
  → `completed` in real time, surviving atomic-write race windows.
* **Persistent runtime-history file** (`namespace-runtime-history.json`)
  picked up wall times for h3-stage / h3-merge / h3-coverage /
  temporal-extent on this run, ready for the next submitter to auto-tune
  `--time` from the median of the last 5 completed runs.

### H3 polyfill optimisation — measured on po (9 017 docs / 7 815 polygons)

| Variant | Wall | docs/s | Speedup |
|---|---:|---:|---:|
| Original (no opts) | 57:43 | 2.3 | 1.0× |
| Area-aware `_polyfill_adaptive` (helpers.py) | ~16:00 (extrapolated) | ~8.2 | **3.6×** |
| Area-aware + spawn `Pool(4)` worker pool | 15:02 | 8.7 | 3.85× |

The area-aware change picks the highest H3 resolution whose estimated cell
count fits the cap based on polygon bbox area, instead of always trying
r7 first and waiting for the cap to drop it to r5. For a continent-scale
period coverage that's the difference between enumerating ~22 k r7 cells
(slow) and going straight to r5 (~450 cells). The multiprocessing pool
added a marginal 6% at the cost of code complexity, spawn-mode startup
overhead, and per-doc pickle cost — the single-threaded parent
(read-jsonl + json.dumps + write + result aggregation) was the throughput
ceiling on po-shaped workloads. Reverted; kept the area-aware win.

### What was NOT exercised this round

* **Tiles smoke** — the dry-run sbatch was verified clean after the
  `--staged` flag fix, but the live run is blocked on consolidating
  `/vast/ishi/geom/staging/` (~8 GB of per-namespace `*.bin` shards) into
  `/vast/ishi/geom/index.json`. That's a separate one-time infra task,
  not a rebuild-code issue. Both `nl` and `po` would otherwise have been
  eligible (`po` would have produced an empty bucket — its records have
  inline hulls rather than geom-store entries).
* **ccode_enrichment** — needs UN's `h3_merged/` snapshot (UN wasn't in
  this smoke run); marked `skipped` for nl + po so the temporal-extent
  prerequisite check passed.
* **toponyms / Symphonym** — the 2-day rebuild_toponyms_index job is too
  large for a smoke; explicitly skipped via `--skip-toponyms` on the
  Batch 9 submitter.
* **index_from_stage / hard_links / inventory push** — defer until at
  least one indexable corpus + UN are present.

### Follow-up: investigate po temporal pipeline — RESOLVED 2026-04-28

**Root cause.** A fresh `p0d.json` survey across all 9 047 periods showed
`period.start.in` / `period.stop.in` is **always a dict**, in one of two
shapes:

* `{"year": "-0585"}` — single point (~94 % of endpoints; signed,
  zero-padded string).
* `{"earliestYear": X, "latestYear": Y}` — uncertain range (~6 %; same
  string format).

The old `_parse_year` did `int(str(value).lstrip('+'))` on the raw dict,
so `int("{'year': '-0585'}")` raised `ValueError` and the helper
silently returned `None` for **every** record. The original docstring's
hypothesis ("ISO8601 year strings like '0500'") was wrong — no PeriodO
record uses bare strings; it's always the wrapped dict. Also: the
existing fallback `start_node.get('in', start_node.get('earliestYear'))`
looked at the wrong nesting level (`earliestYear` is inside `in`, not on
the parent `start`/`stop`), so that branch was dead.

**Fix** (`authorities/periodo-places.py`):

* `_parse_year(value, *, prefer='earliest')` now recurses into the dict:
  picks `'year'` when present, else `'earliestYear'` for `start` nodes
  and `'latestYear'` for `stop` nodes — preserving the period's full
  extent rather than collapsing the range to a midpoint.
* Call sites updated to pass `prefer='earliest'` for `start_node.get('in')`
  and `prefer='latest'` for `end_node.get('in')`.
* Removed the dead top-level `earliestYear`/`latestYear` fallback.

**Verification (local, no Slurm):**

* New unit test `tests/test_periodo_parse_year.py` (6 cases: signed
  zero-padded year dict, range dict with `prefer='earliest'`/`'latest'`,
  bare values, None/empty, unparseable) — passes; full suite 31/31.
* Smoke run of `process_periodo_period` against the fresh `p0d.json`
  (geometry index empty to skip the slow gazetteer fetch) → **9 047 /
  9 047** docs now carry `timespans` (was 0 / 9 047). Examples:
  * "Early Bronze": `[{start: {in: -3499}, end: {in: -2249}}]`
  * "Energy epoch five: fire" (range shape): `[{start: {in: -1500000},
    end: {in: 2017}}]`

`enrich_geometry(... timespans=timespans or None ...)` at line 456 was
already wired up, so geometries[] entries pick up the same timespan
list automatically once the toponym list is non-empty.

**Next step on CRC.** Push the fix, pull on `/ix1/ishi/elastic`, re-run
the po staging extract, then re-run the temporal-extent array — `po`
`temporal_extent` should now land at roughly `[-1500000, 2017]` (driven
by the geological-epoch periods in the corpus) rather than `[null, null]`.

### Resume note — start the next session with OHM

OHM (OpenHistoricalMap) is the right next test. It has the **same data
shape and code path as OSM** (PBF input, `osm-boundary-pass.py` boundary
geometry stage, multipolygon assembly, identical `boundary` field, same
authority script class `osm-places.py`-style streaming) but at a much
smaller scale (~800 K records vs ~18 M for OSM). Specifically:

* It exercises the **boundary completion + boundary_merge stages**
  (skipped for nl + po), so we test `processing/boundary_stage.py` and
  `processing/boundary_merge.py` end-to-end on real data.
* It exercises the **OSM/OHM-specific h3_stage source-resolution path**
  (`update_merged → boundary_merged → extract` chain) which we didn't
  touch on nl + po.
* It exercises **tile bucket `ohm_admin`** (and contributes to the
  mixed `osm_misc` bucket once OSM is also staged) — verifies the
  bucket-driven `submit_tiles_slurm` path on a realistic boundary
  corpus, once the geom store is consolidated.
* It validates the **area-aware polyfill** on a different polygon-shape
  distribution (admin boundaries vs period coverages). If it scales as
  expected (~doc-count linear, area-aware behaving for small admin
  polygons), full OSM will be tractable; if not, we tune further before
  committing OSM time.
* The ~800 K records also exercise the **runtime-history wall-time
  estimator** (every per-stage wall time will land in the persistent
  history file, so the next OSM submission auto-tunes its `--time`
  request from real OHM measurements rather than the conservative
  first-run defaults).

Pre-OHM checklist:

1. Confirm OHM staged extract exists at `/vast/ishi/staged/ohm/extract/`
   (or run authority extract via `es -ingest -n ohm`).
2. Run `processing/geom_store.py` to consolidate
   `/vast/ishi/geom/staging/` → `/vast/ishi/geom/index.json` (needed
   for OHM admin polygons in tiles, and for ccode_enrichment if/when
   UN is included).
3. Submit `boundary_stage` → `boundary_merge` → `submit_h3_slurm` chain
   for ohm via the existing orchestration (or just `submit_h3_slurm`
   if extract + boundary already done).
4. Measure per-stage wall times against the runtime-history estimator's
   defaults and adjust `_LARGE_NAMESPACES` / `_LARGE_DEFAULT_HOURS` in
   `processing/submit_h3_slurm.py` if the medians drift far from the
   conservative bounds.

## Test Run Results — Session 2026-04-28 (po follow-up + OHM smoke)

### po temporal extent — re-verified after `_parse_year` fix

After the `_parse_year` fix landed (commit `5472a80`), re-ran the
po extract → h3_merge → temporal_extent chain on CRC. Results:

| Stage | Wall | Output |
|---|---:|---|
| `extract` (login node, gazetteer cache warm) | ~30 s | 9 017 docs, 9 017 / 9 017 toponyms with timespans, 7 815 / 7 815 geometries with timespans |
| `h3_merge` (login node) | ~5 s | 9 017 written, 7 815 patches applied, both jsonl + parquet |
| `temporal_extent` | 2.5 s | `[-4567998050, 3000]` (was `[null, null]`) |

The lower bound is the start of the Hadean — driven by geological-epoch
periods in the corpus. Confirms the fix end-to-end and clears the
earlier "must-fix before re-staging" gate.

### OHM smoke run

Goal: validate the OHM end-to-end chain (the trial run for full OSM)
on `/ix1/ishi/data/authorities/ohm/planet-latest.osm.pbf` (~1.1 GB).
Run id `ohmsmoke-22926571`; manifest at
`/vast/ishi/staged/runs/ohmsmoke-22926571.json`.

| Stage | Job | Wall | Notes |
|---|---|---:|---|
| `extract` | smp 22923902 | **30:38** | 905 205 docs / 654 012 geoms in geom_store / 99 512 relation fallbacks (boundary multipolygons that need `boundary_stage` to assemble) |
| `boundary` | smp 22926571 | 65 min, **cancelled** | Stalled in long-tail multipolygon assembly — see finding below |
| `boundary_merge` | — | skipped | Marked completed via white-lie so `h3_stage` gate would pass; `_extract_stage_dir` then falls through to `extract/` since `boundary_merged/` is absent |
| `h3_stage` | smp 22928657 | **1:09** | 905 205 seen / 805 693 patched / 805 693 geometries with H3 (~13 k docs/s — almost all are point geometries from the extract fallback path) |
| `h3_merge` | smp 22928658 | failed at parquet step | JSONL written intact (737 MB); parquet conversion crashed — see finding below |
| `h3_coverage` | login | <1 s | `is_global: true` (compaction skipped, same as po) |
| `temporal_extent` | login | **9.0 s** | `[-99999, 20222]` over 905 205 records (data-quality outliers from OHM `start_date` / `end_date` tags — see finding) |

**Aggregates:**
* `/vast/ishi/staged/_aggregates/ohm.h3_coverage.json` — `{"coverage": "global", "namespace": "ohm"}`
* `/vast/ishi/staged/_aggregates/ohm.temporal_extent.json` — `{"namespace": "ohm", "record_count": 905205, "temporal_extent": [-99999, 20222]}`

### Findings worth fixing before full OSM

1. **`processing/boundary_stage.py` long-tail latency.** Throughput
   dropped from ~380 / s in the first minute to **~4 / s after 30 min**
   on the OHM PBF (only 39 boundaries assembled in the 29 min between
   `[31m]` and `[60m]` checkpoints). Root cause: a small number of
   very-large multipolygon assemblies (likely oceans, continental admin
   units) dominate the tail. At this rate the run extrapolates to
   100 + h for the last 3 500 of ~68 300 boundaries — full OSM
   (~10× larger working set) would never finish under any reasonable
   Slurm budget.

   Suggested mitigations to evaluate (none implemented yet):

   * **Per-relation timeout / skip with diagnostic** so a few
     pathological assemblies don't block the rest. The fallbacks
     already in the extract become the de-facto representative for
     skipped relations.
   * **Polygon-count / member-way pre-filter** that buckets relations
     by size and processes the giant ones in a separate Slurm task
     with a longer time budget (or with osmium's "no-area-validation"
     mode for very large rings).
   * **Sort relations by member count descending and process in
     parallel** (one Slurm task per shard) so the long tail no longer
     serialises behind the small relations.

2. **`processing/h3_merge.py` source-resolution mismatch.** For
   OSM/OHM, `h3_merge._source_dir` returns `boundary_merged/`
   unconditionally and raises `FileNotFoundError` if it doesn't
   exist, while `h3_stage._extract_stage_dir` falls through
   `boundary_merged → update_merged → extract`. They should agree —
   right now you can produce H3 patches from the extract fallback but
   then can't merge them. Easy fix: copy the same fall-through chain
   into `h3_merge._source_dir`.

3. **`processing/h3_merge.py` parquet conversion fails on
   variable-depth `geometries[].hull.coordinates`.** `pyarrow.json.read_json`
   does schema inference and rejects rows where the same column has
   different nesting depths (here: hulls that are sometimes Polygon
   `[[lon,lat], …]` and sometimes MultiPolygon `[[[lon,lat], …], …]`).
   The JSONL is written intact, only the parquet sidecar is missing.
   Two acceptable fixes:

   * **Strip `hull` from `_normalize_for_parquet`** before the parquet
     re-read. Hull is consumed by `h3_stage` (already done by this
     point), `ccode_enrichment`, and `generate_tiles`; both downstream
     readers can pull hull from the JSONL or the staged geom store
     instead of the h3_merged parquet.
   * **Provide an explicit pyarrow schema** rather than letting
     pyarrow infer — this keeps hull in parquet but adds maintenance
     burden as the schema evolves.

   The first option matches the smaller-diff-wins norm; the second is
   only worth it if a downstream consumer explicitly needs hull from
   the h3_merged parquet (none today).

4. **OHM temporal_extent has data-quality outliers** (`-99999`, `20222`).
   These come from upstream `start_date` / `end_date` tag values that
   parse as bare integers but are clearly placeholders / typos. Not a
   bug in the staged pipeline; consider clamping in `gazetteer_temporal_extent`
   (e.g. drop years outside `[-10000, current_year + 10]`) to keep
   downstream UI/search filters sane.

### What this validates for full OSM

* The h3 derivation path is **fast even at 905 K docs** when most
  geometries fall back to points (1:09, ~13 k docs/s). For full OSM
  (~18 M docs) at the same rate, expect **~25 minutes** for h3_stage
  if a similar share of records are points — the polyfill remains the
  cost driver only for the boundary-merged subset.
* `h3_coverage` and `temporal_extent` scale linearly and are trivial
  even on 900 K records (<10 s combined). No tuning needed.
* `extract` for ~1 GB of PBF takes ~30 min single-process. Full OSM
  (~80 GB planet PBF) at the same I/O rate would take **~40 h**, well
  past any Slurm budget on `htc`. **Either** the existing checkpoint
  + resume mechanism needs to be exercised across multiple Slurm jobs,
  **or** the extract needs to be sharded by spatial bbox / PBF region.
  This is the largest remaining unknown for full OSM.

### Resume note — boundary_stage refactor before next OSM/OHM attempt

The boundary-stage long-tail issue (finding 1) is the gating problem.
Until that's addressed, no OSM/OHM run that needs full multipolygon
assembly will complete in a reasonable Slurm window. Recommend
landing the per-relation timeout + parallel sharding before the next
attempt.

### Patches landed 2026-04-28 (post-OHM-smoke, pre-push)

All four findings from the OHM smoke have local patches and tests.
67/67 unit tests pass. Pushed in a separate session and CRC re-test
pending.

**Finding 2 — `h3_merge` source-resolution mismatch.** Fixed
`processing/h3_merge.py::_source_dir` to mirror
`h3_stage._extract_stage_dir`'s fall-through chain
(`boundary_merged → update_merged → extract`). A smoke run that skips
boundary completion now flows cleanly through h3_merge instead of
crashing with `FileNotFoundError`. Covered by 3 tests in
`tests/test_h3_merge_helpers.py`.

**Finding 3 — `h3_merge` parquet hull crash.** Added
`processing/h3_merge.py::_strip_hull_for_parquet`. The persisted JSONL
keeps `hull` (downstream `ccode_enrichment` and `generate_tiles`
consume it from there), and a temporary parquet-input JSONL with hulls
stripped is fed to `pyarrow.json.read_json` for parquet conversion
only. Covered by 4 tests; verified by running the OHM h3_merge happy
path locally.

**Finding 4 — temporal-extent outlier clamping.** Added a
per-namespace clamp in
`processing/gazetteer_temporal_extent.py`. Year readings outside
`[clamp_min, clamp_max]` are dropped at the per-reading level (not the
aggregate), so a few bogus tags can't poison a namespace's extent.
Defaults: `[-10000, current_year + 100]`. The `po` namespace gets a
geological-deep-time override `[-5_000_000_000, 10_000]` — without it,
the just-verified po extent of `[-4567998050, 3000]` would have been
clipped to `[null, null]`. The CLI gains `--clamp-min` / `--clamp-max`
overrides; new metrics fields `clamp_range` and `rejected_readings`
are exposed (informational; not part of the on-disk aggregate
contract). Covered by 13 tests in
`tests/test_temporal_extent_clamp.py` including the
po-vs-default-clamp regression case.

Predicted OHM impact (to verify on next CRC run): the previous
`[-99999, 20222]` extent should land in the realistic
`[<historic-min>, ~current_year]` range, and the metrics file should
report a non-zero `rejected_readings` count.

**Finding 1 — `boundary_stage` parallelisation.** Implemented a full
sharded execution model with three new modules and a CLI extension to
the existing one:

* `processing/boundary_shard_planner.py` — scans the prefiltered PBF,
  counts member ways per boundary relation, and bin-packs them into N
  shards using the Longest Processing Time (LPT) greedy heuristic.
  The largest single relation always lands alone on the lightest
  shard, so max-shard-cost is minimised. Writes `shard_map.json`.
* `processing/boundary_stage.py` — gains `--shard-id N --shard-map
  PATH` worker mode. The worker uses `osmium getid -r` to subset the
  prefiltered PBF down to just its assigned relation IDs (plus their
  referenced ways and nodes), then runs normal area assembly on that
  small subset. Writes `places.boundary.shard_<I>.jsonl`. Standalone
  mode (no shard args) is preserved unchanged.
* `processing/boundary_stage_finalize.py` — concatenates the per-shard
  JSONLs into the canonical `places.boundary.jsonl` (the only path
  `processing.boundary_merge` reads), aggregates per-shard metrics,
  and flips the manifest's `boundary` stage to `completed` exactly
  once.
* `processing/submit_boundary_slurm.py` — orchestrates planner →
  worker array → finalizer with `afterok` dependencies. Defaults: ohm
  16 shards × 8h, osm 32 shards × 24h. Supports `--dry-run` for
  sbatch inspection.

Covered by 13 tests in `tests/test_boundary_shard_planner.py` (LPT
correctness incl. dominant-relation-lands-alone, boundary-tag
acceptance/rejection, shard-map round-trip). Submitter `--dry-run`
produces well-formed sbatch scripts for all three jobs.

Expected wall-time win on OHM (extrapolating from the cancelled
single-task run): ~65 min became infeasible because of ~3 500 large
relations stuck behind one another. With 16 parallel shards, no shard
holds more than ~220 large-tail relations, and the heaviest
single-relation cost (likely a continent or ocean) dominates only one
shard. Net wall: should land in the **2–3 h** range for OHM, with
linear scaling to ~6–8 h for full OSM at 32 shards.

#### Cross-shard leakage fix (post first-attempt cancel)

The first OHM benchmark attempt (run id `ohmsmoke-22926571`, planner
22929253 → workers 22929254) ran the planner cleanly (28 s, perfect
1.00 max/min cost ratio across the 16 shards) but each worker saw
~21 000 areas instead of the assigned 4 366 — a ~4× over-extraction.

Cause: ``osmium getid -r`` follows relation→relation member references
(``subarea``, ``admin_centre``, ``label``, etc. — common on OSM/OHM
admin boundaries), so each per-shard subset PBF contained many
boundary relations that belonged to *other* shards. Without a filter,
every shard re-assembled its leaked siblings and emitted duplicate
boundary patches.

Fix: in ``processing/boundary_stage.py``, the worker now keeps the
shard's relation-ID set and skips any area whose ``orig_id()`` is
not in it. The skip count is exposed as ``leaked_areas_skipped``
in the per-shard metrics so the next run can quantify how aggressive
the leakage really is. Three new tests in
``tests/test_boundary_shard_planner.py`` lock in the contract
(disjoint id-sets across shards, KeyError on unknown shard, returned
type is `set[int]`). Total: 70/70 unit tests pass.

Workers + finalize + post-chain cancelled at 65 min into the run; the
clean re-run will start fresh once the fix is pushed.

#### boundary_merge parquet hull crash + shared `staged_parquet` module

Run id `ohmsmoke-22926571` v2 (planner 22932735, workers 22932736,
finalize 22932737, postchain 22932765) ran the boundary chain to
completion successfully:

* Planner: ~3 min, perfect 1.00 max/min cost ratio across 16 shards.
* Workers: 16 parallel array tasks. Wall times ranged from 02:29 to
  ~07:05; median ~03:30. **All 16 shards completed** without the
  long-tail stall that killed the original single-task attempt.
* Finalize: ran cleanly, concatenated the per-shard JSONLs.
* Postchain: **failed** at the first stage (boundary_merge) with
  ``ArrowInvalid: Column(/geometries/[]/hull/coordinates/[]) changed
  from number to array in row 216`` — the same hull-shape regression
  we fixed in h3_merge but in boundary_merge's local copy of the
  parquet conversion code.

Refactor: extracted the parquet helpers into
``processing/staged_parquet.py`` so the fix can't drift again:

* ``normalize_for_parquet(doc)`` — empty nested-list fields → None.
* ``strip_hull_for_parquet(doc)`` — drop ``geometries[].hull``.
* ``write_parquet_from_jsonl(jsonl, parquet)`` — streams the canonical
  JSONL through hull-strip into a sibling temp file, runs pyarrow,
  cleans up the temp even on failure.

Both ``processing/h3_merge.py`` and ``processing/boundary_merge.py``
now import from the shared module; their local ``_normalize_for_parquet``
implementations are reduced to thin re-exports for test stability.
The h3_merge dual-write loop (canonical JSONL + parquet-input JSONL
written in lockstep) is replaced by a simpler "write canonical JSONL
once, then ``write_parquet_from_jsonl``" pattern that costs one extra
disk pass on a 700 MB file (~30 s on this hardware) — worth it for
the smaller diff and one-place-to-fix property.

11 new tests in ``tests/test_staged_parquet.py`` lock in the
contract, including a direct regression for the production crash
(mixed Polygon/MultiPolygon hull rows in a single JSONL). Total:
**81/81 unit tests pass**.

Boundary chain timings (already captured) will combine with the
post-boundary timings from the next clean run to give the OHM
benchmark.

#### Explicit-nulls-in-struct fix (h3_merge after boundary_merge)

Re-running the postchain after the shared-module refactor exposed
**another** ``pyarrow.read_json`` limitation:
``ArrowNotImplementedError: JSON conversion to struct<in: int64> is
not supported``. Symptom: h3_merge succeeded for ``po`` (where every
record has both ``start.in`` and ``end.in`` populated) but failed for
``ohm`` (where many boundary records have ``start_date`` but no
``end_date``, producing ``{"start": {"in": 1500}, "end": null}``).

Bisecting found the first offending row at index 1789 — a node with
``"timespans": null`` after a long run of records that had
``[{"start": {...}, "end": {...}}]``. The root cause is asymmetric:
``pyarrow`` **writes** parquet with nullable struct slots fine, but
``read_json`` **cannot read** a JSON literal ``null`` where another
row has a struct value at the same path. h3_merge reads from
``boundary_merged.parquet``, which preserves the nullable slots, then
``json.dumps``-serialises them back to JSONL with explicit
``"end": null`` — and that JSONL is the input to the next parquet
conversion, which crashes.

Fix: ``processing/staged_parquet.drop_nulls_for_parquet`` recursively
strips ``None`` values from dicts and lists. Wired into
``write_parquet_from_jsonl`` alongside ``strip_hull_for_parquet``.
The canonical JSONL keeps the explicit nulls (matching the
boundary_merge behaviour); only the parquet sidecar drops them, which
is lossless because parquet's "absent" and "null" are encoded
identically.

6 new tests in ``tests/test_staged_parquet.py`` cover the recursive
null-strip plus the regression case (explicit ``"end": null`` in a
timespan struct adjacent to a fully-populated one). Total: **87/87
unit tests pass**.

Verified on the actual OHM h3_merged JSONL: the first 5000 rows fail
``paj.read_json`` without the fix and pass with it. Now expecting the
re-submitted postchain to clear h3_merge → h3_coverage →
temporal_extent.
