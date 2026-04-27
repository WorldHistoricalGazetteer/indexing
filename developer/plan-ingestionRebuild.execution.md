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
> `/reconcile/authority-datasets` and LPF endpoints, or the future gazetteer-registry
> endpoint contract is necessary. **Do not** assume the Django code matches what is
> described here without verifying against the clone.

- `Dataset.authority` controls dataset eligibility for authority ingestion.
- Discovery endpoint: `GET /reconcile/authority-datasets`
  (returns `result[{id,title,place_count}]`).
- LPF endpoint per dataset: `GET /entity/dataset:<id>/api?filetype=lpf`
  (streamed gzip response).
- Auth: token/session auth (token query param or bearer token).
- **(New, Master Plan §5.3, Appendix E.2 item 6)** Gazetteer registry endpoint
  (POST/PUT) accepting the inventory payload listed in Batch 11. Endpoint contract to
  be defined jointly with the Django team; the indexing pipeline is the producer.
- **(New, Master Plan §2d–2e of `plan-dynamicClustering.DEPRECATED.md`,
  reaffirmed in the Master Plan)** Pitt-side links endpoint:
  - `POST /api/links` — Django forwards each contributor attestation here for
    immediate insertion into the Pitt SQLite (idempotent via `INSERT OR IGNORE`).
  - `DELETE /api/links/<assertion_key>` — Django forwards revocations.
  These are gateway endpoints, so the indexing-side responsibility is to ship the
  SQLite and document the schema; gateway implementation is tracked separately.

---

## Runtime Prerequisites

- [ ] Configure WHG API base URL for discovery / LPF / gazetteer-registry calls.
- [ ] Provide token/session credentials for authenticated endpoint access.
- [ ] Add retry/backoff policy and timeout defaults for WHG API fetches.
- [ ] Add root-level checkbox authority selection file and parser/validation rules.
- [ ] Ensure `authority-selection.md` is bootstrapped with checked entries for all local
  authorities and currently discovered WHG datasets.
- [ ] (New) Pitt VM filesystem path for the SQLite hard-link database; atomic-swap
  procedure documented for the gateway lifespan handler.

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

**Phase 3 (Lower Priority, Update Scripts)** — pending; semantics differ from
Phase 1/2 (these *update* existing staged records rather than *emit* new ones, so
the staged-mode equivalent is a Parquet-merge step, not a `write_staged_place_doc`
loop). Treat as a separate workstream after Phase 4.

- [ ] `authorities/geonames-toponyms.py` (update; auxiliary toponym records).
- [ ] `authorities/wikidata-geoshapes.py` (update; enriches existing places).
- [ ] ~~`authorities/loc-relations.py`~~ — **deferred to Batch 12.** LOC contributes only
  owl:sameAs / skos:exactMatch links between existing places, not place records, so it has
  no role in the per-gazetteer extract → boundary → H3 → ccode pipeline. Its rows are
  consumed directly by the SQLite hard-link harvest in Batch 12.

**Phase 4 (WHG Datasets — DO PostgreSQL is canonical)** — pending; significant new
work because there is no existing `whg-places.py` to refactor. Requires verifying the
DO API contracts against the local Django clone at
`/home/stephen/Documents/GitHub/whg3` before implementation begins.

- [ ] `authorities/whg-places.py` (new). One script per run, but iterates over multiple
  Datasets/Collections. Responsibilities (formerly split off as a separate Batch 13a):
  - Discovery: `GET /reconcile/authority-datasets` against the DO Django API to enumerate
    enabled Datasets/Collections.
  - LPF fetch: `GET /entity/dataset:<id>/api?filetype=lpf` (streamed gzip) per dataset.
  - Auth: token-query and bearer-token modes wired through `processing/settings.py`.
  - Stable IDs: emit `place_id = whg:<dataset_id>:<entity_id>`, `dataset_id =
    whg:<dataset_id>` (each Dataset/Collection is its own numerical sub-namespace).
  - Per-dataset manifests under the `whg` namespace so individual datasets can be
    re-staged independently in partial-run mode.
  - `dataset_status`: `published` for accessioned/approved datasets, otherwise `pending`.
  - **v3.2 datasets are not special** — they flow through this same path because they
    live in DO PostgreSQL alongside any new contributions (see Batch 13b for the only
    legacy-specific work, which is DO-side flagging of historical reconciliation links).

Each refactor follows the same pattern:

1. Replace `helpers.bulk(es, batch)` calls with `write_staged_place_doc(ns, doc)`.
2. Keep geometry-store writes as-is (already staged).
3. Remove ES client instantiation in staged mode.
4. Add `if os.environ.get("WHG_STAGING_MODE")` guard so the ES path remains for
   backward compatibility while we transition.
5. **(New)** Ensure `dataset_status` and `dataset_id` are populated on every emitted doc.

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
- [ ] Benchmark wall times for large namespaces (`osm`, `ohm`, `gn`) to tune Slurm QOS
  defaults.
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
- [ ] No hull-derived coverages (only full-geometry H3).
- [ ] H3 coverage file present for every non-global namespace; round-trips through
  `h3.uncompact_cells`.
- [ ] H3 coverage file for every global namespace contains the sentinel `"global"` and is
  not enumerated.

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

- [ ] Unit checks for point-in-polygon and overlap tie-break behaviour.
- [ ] Throughput benchmarks confirm pre-filtering efficiency.
- [ ] Ccode coverage statistics (places per country) match expectations.
- [ ] Ccode patches merge correctly without corrupting geometry indices.

### Batch 8: Global Barrier — All Selected Gazetteers Preprocessed

Targets:

- `processing/staging_orchestrator.py`
- `processing/ingest_all_authorities.py`

Dependencies: Batch 4–7 complete for every selected gazetteer.

Tasks:

- [ ] Manifest-based barrier check confirming all selected gazetteers completed
  extract/boundary/H3/ccode preprocessing.
- [ ] Fail fast if any selected gazetteer is missing, incomplete, or has stale prerequisite
  artefacts.
- [ ] Materialise the run-level gazetteer inventory used by subsequent global phases (this
  is the input to Batch 9 aggregates and Batch 11 inventory push).

Validation gates:

- [ ] Barrier refuses to start corpus-wide phases with partial gazetteer coverage.
- [ ] Barrier report lists all selected gazetteers and completion states.

### Batch 9: Global Toponyms + Symphonym + Per-Gazetteer Temporal Extent

Targets:

- `phonetics/extraction/rebuild_toponyms_index.py`
- `phonetics/inference/update_es.py`
- `processing/embed_extract.py`
- `processing/embed_transform.py`
- `processing/embed_load.py`
- `processing/gazetteer_temporal_extent.py` (new — temporal extent only; H3 coverage moved
  to Batch 6)

Dependencies: Batch 8 (global barrier complete).

> **Parallelism.** This batch is one of the two post-barrier jobs (Job A in the dependency
> flow above). Batch 12 (hard-link harvest, Job B) runs in parallel on a separate Slurm
> allocation; both are read-only over the staged corpus and the geometry store.

Tasks:

**Toponyms + Symphonym** (existing scope):

- [ ] Read the union of all selected staged place snapshots (not ES `places`) as canonical
  source.
- [ ] Extract unique toponyms with attestations across the full selected corpus.
- [ ] Deduplicate toponyms corpus-wide across gazetteers before embedding generation.
- [ ] Compute Symphonym embeddings (GPU-enabled) over the deduplicated corpus.
- [ ] Maintain persistent Symphonym cache; recompute embeddings only for changed toponyms.
  This is the primary cost amortisation in partial-run mode where most gazetteers are
  unchanged.
- [ ] Run Symphonym model/version preflight; invalidate cache when version changes.
- [ ] Stage toponym records and embeddings.
- [ ] **Clear scope boundary**: no ES indexing in this stage; outputs remain staged.

**Per-gazetteer temporal extent** (new, Master Plan §1.4.1 + E.2 item 2):

- [ ] `gazetteer_temporal_extent.py` reads all selected staged snapshots and produces,
  per gazetteer, `temporal_extent` = `[min(start_year), max(end_year)]` across all
  timespans on all records (`null` where the gazetteer has no temporal data).
- [ ] Output written to `staged/_aggregates/{namespace}.temporal_extent.json`; consumed
  by Batch 11 inventory push together with the H3 coverage file produced in Batch 6.
- [ ] Recomputed on every full run; an incremental path may be added later if needed.

> **H3 coverage moved.** Per-gazetteer H3 coverage compaction now happens inside Batch 6
> (where the H3 cells are already in memory). Global gazetteers (`osm`, `ohm`, `wd`, `gn`,
> `po`, `tgn`) emit a `"global"` sentinel and skip enumeration entirely.

Validation gates:

- [ ] Toponym extraction produces expected counts.
- [ ] Corpus-wide deduplication merges cross-gazetteer attestations as expected.
- [ ] Embedding/index outputs remain schema-compatible.
- [ ] Incremental run re-embeds only changed toponyms when model version is unchanged.
- [ ] Cache invalidation triggers full recompute when model/version changes.
- [ ] No ES access during this stage.
- [ ] `temporal_extent` correctly null-handles records lacking timespans.

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

- `processing/index_from_stage.py` (new)
- `processing/namespace_lifecycle.py` (new)
- `processing/push_gazetteer_inventory.py` (new — Master Plan §5.3)
- `processing/ingest_all_authorities.py`

Dependencies: Batch 3 (orchestrator), Batch 9 (toponyms + aggregates staged), Batch 10
(required tiles complete).

**Prerequisite for indexing:** staging ES instance must be running.

Tasks:

**Indexing:**

- [ ] Load `places` exclusively from final enriched staged snapshots, including
  `dataset_status` and `dataset_id` per record.
- [ ] Incrementally update `toponyms` based on staged attestation diffs.
- [ ] Rebuild index outputs from the selected-gazetteer staged corpus only.
- [ ] Do not use separate ad hoc gazetteer-removal operations outside selection-file
  control.
- [ ] Trigger Batch 12 hard-link harvest after index mutations (the harvest reads staged
  files, not ES, so it does not strictly depend on indexing — but tying it to the same
  signal keeps the user-facing system in sync).

**Gazetteer inventory push** (new, Master Plan §5.3, Appendix E.2 item 6):

> **Final-step gating.** The inventory push is the user-facing signal to Django that the
> ingestion/indexing run is complete. It must be gated on **all of**: successful
> indexing (Job A descendants — toponyms, places), and successful Ship-to-Pitt of the
> SQLite hard-link database (Job B → Batch 12 atomic swap). Firing it earlier would let
> the Django UI advertise a complete run while the gateway-side hard-link store is still
> mid-flight.

- [ ] `push_gazetteer_inventory.py` builds the inventory payload from the run-level
  gazetteer set (Batch 8) + per-gazetteer H3 coverage from Batch 6 + per-gazetteer
  temporal extent from Batch 9. Each entry:
  ```json
  {
    "id": "<namespace>",
    "name": "GeoNames",
    "description": "...",
    "namespace": "gn",
    "class": "authority",
    "owner_user_id": null,
    "record_count": 13000000,
    "status": "published",
    "h3_coverage": "global",
    "temporal_extent": [-2000, 2025]
  }
  ```
  Non-global gazetteers carry `h3_coverage: ["<compact h3 cell>", "..."]` instead of the
  `"global"` sentinel. WHG-dataset entries (one per Dataset/Collection in DO) carry
  `class: "dataset"`, the contributor's `owner_user_id`, and a numerical sub-namespace
  (`whg:<dataset_id>`); `status` reflects `draft` / `submitted` / `rejected` / `published`
  per the contribution workflow. **LOC** does not appear in this inventory at all
  (relations-only, no place records, see Batch 12).
- [ ] POST/PUT the payload to the Django gazetteer-registry endpoint (contract TBD with
  the Django team).
- [ ] Idempotent: re-running the push for the same indexed corpus produces no change in
  Django; updates use upsert semantics.
- [ ] Runs **after** every successful indexing job **and** Batch 12 Ship-to-Pitt swap.

Validation gates:

- [ ] Deselected gazetteers absent from staged corpus and from indexed outputs.
- [ ] Referential integrity checks pass for sampled docs.
- [ ] `dataset_status` and `dataset_id` index correctly and are queryable as keyword
  filters.
- [ ] Inventory push round-trips through Django and the resulting `/suggest` response
  contains every selected gazetteer with correct counts and aggregates.

### Batch 12: Hard-Link SQLite Harvest from Staged Files (Replaces Post-Index Clustering)

Targets:

- `clustering/harvest/hard_links_staged.py` (new — replaces the ES-based
  `clustering/harvest/hard_links.py` for the production path; the ES variant may be
  retained as a one-time backfill helper but is no longer the live mechanism)
- `clustering/harvest/loc_links.py` (new — LOC relations harvest; LOC's only entry point
  in the rebuild)
- `clustering/harvest/contributor_replay.py` (new — replays active rows from DO PG)
- `clustering/sqlite_overlay.py` (new — schema, builders, atomic ship-to-Pitt)
- `processing/ingest_all_authorities.py` (orchestration hook)

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

- [ ] `clustering/sqlite_overlay.py` defines:
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

- [ ] `hard_links_staged.py` iterates over each selected gazetteer's
  `staged/{namespace}/final/places.parquet` (or equivalent canonical snapshot).
- [ ] For each record, walk `relations[]` and emit one row per entry whose `type` is
  `sameAs`, `exactMatch`, or `closeMatch` (and `distinct` once that schema is settled).
- [ ] Canonical-order `(place_a, place_b)` so `place_a < place_b`; populate `source_id`
  with the source namespace (e.g. `'wikidata'`, `'tgn'`); populate `asserted_at` from
  the source where available.
- [ ] Use `INSERT OR IGNORE` for idempotency; bulk insert via prepared statements + a
  single transaction per gazetteer.
- [ ] **Re-target the existing implementation**: `clustering/harvest/hard_links.py`
  currently scans the ES `places` index. Port its filtering rules
  (`IDENTITY_RELATION_TYPES`, same-namespace self-reference filtering, non-WHG
  namespace filtering) to the staged-file path. Retire the ES path from the live run
  graph; keep it available as a recovery tool until the staged path is verified at
  scale.

**Phase 1A.LOC — LOC relations folded in here** (new entry point for LOC):

- [ ] `clustering/harvest/loc_links.py` (new) reads the LOC relations source — fetched
  by `processing/fetch_authorities.py` to `${DATA_DIR}/loc/...` — and emits rows
  matching the same `hard_link_assertions` shape as Phase 1A. `source_category =
  'authority'`, `source_id = 'loc'`. LOC has no place records of its own, so it never
  participates in extract / boundary / H3 / ccode / toponyms / inventory; this is its
  sole consumption point.
- [ ] LOC-derived rows refer to existing `place_id`s in the staged corpus; rows whose
  endpoints do not resolve to any indexed place should be dropped at harvest time
  with a logged count (these typically reflect upstream drift between LOC and its
  referent gazetteers).

**Phase 1B — Contributor replay from DO PostgreSQL**:

- [ ] `contributor_replay.py` connects to DO PG, selects all rows from
  `contributor_attestations` with `status = 'active'`, and inserts them into the SQLite.
- [ ] Source-of-truth note: the DO PG table is authoritative for contributor assertions;
  the SQLite is a derived view that can be rebuilt at any time.
- [ ] Pending assertions (`status = 'pending'`, Master Plan §7.4 / §9.3) are **not**
  included in the publishable SQLite — they are visible to in-scope users via a
  Django-side scope-filtering merge at request time, not by being in the public hard-link
  store.

**Ship-to-Pitt with atomic swap**:

- [ ] Build the SQLite at a temporary path on the build host (CRC).
- [ ] `sqlite_overlay.py` defines the swap procedure: rsync to a Pitt staging path, then
  `mv` over the live file. The gateway's open file descriptors remain valid; the next
  `open()` uses the new file.
- [ ] Document the gateway-side requirement to reopen the connection on a SIGHUP or via
  a periodic re-open (gateway-side change tracked separately).

**Reconciliation / drift detection**:

- [ ] A periodic job (initially manual) compares DO PG `status = 'active'` rows against
  the live Pitt SQLite contributor rows and reports any drift. Drift in normal operation
  should be zero or near-zero; non-trivial drift indicates a problem in the synchronous
  forwarding path implemented by the gateway.

Validation gates:

- [ ] Bulk insert of expected scale (~10–50M authority assertions) completes within a
  defined wall-time budget on CRC.
- [ ] Round-trip test: harvest → ship → query a sampled set of place_ids returns the
  expected hard-link members.
- [ ] Idempotency: re-running the harvest over the same staged files produces the same
  database (byte-equivalent or structurally equivalent — relax to "queries return
  identical results").
- [ ] `INSERT OR IGNORE` correctly de-duplicates assertions made by multiple sources.
- [ ] Atomic-swap procedure verified on CRC → Pitt with a live gateway open against the
  file.

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
- `clustering/sqlite_overlay.py` (this repo) — accept and propagate the suffix on
  `source_id` so the Pitt SQLite preserves distinguishability.

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
  `contributor_attestations`. Backfill `true` for every row whose original creation
  predates the v4 contribution workflow rollout. (Owned by the Django team; this plan
  records the dependency.)
- [ ] Pitt-side: the Batch 12 hard-link harvest's contributor-replay path
  (`contributor_replay.py`) reads `legacy_v3_2` from DO PG and encodes it onto the
  SQLite `source_id` (e.g. `contributor:<user_id>:legacy_v3_2`) so the gateway can
  filter on it without joining back to DO.
- [ ] Preserve v3.2 dataset metadata (description, citation, license, contributor
  identity) — already preserved by `whg-places.py` since it reads from DO PG, no extra
  work.
- [ ] Preserve v3.2 accession history as historical metadata in DO PG, **not** mapped
  onto the new submission/review state machine.
- [ ] In-progress v3.2 reconciliations: their parent datasets carry
  `dataset_status='pending'` if the work was not finalised under v3.2; assertions
  carry `status='pending'` in DO PG.

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

- [ ] Build minimal integration harness for small namespaces (`nl` + `po`).
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

- `processing/retention_sweep.py` (new)
- `processing/notifications.py` (new — or wire into existing Django notification path)

Dependencies: Batch 11 (indexing operational and gazetteer inventory push wired),
Batch 13a (WHG dataset ingestion path admits `dataset_status: 'pending'`).

Master Plan §10.1 specifies a one-year retention sweep on pending datasets.

Tasks:

- [ ] Schedule a periodic job (cron / Slurm scheduled job / Django management command —
  whichever fits the operational pattern).
- [ ] Definition of "without contributor edits": no new assertions, no revoked or revised
  assertions, no record modifications, no submission attempt, no withdrawal. Passive
  viewing does not extend the retention window.
- [ ] At the **eleven-month mark** for any pending dataset, send a notification to the
  contributor (in-platform + opt-in email) warning of impending deletion and offering
  resume/export options.
- [ ] At the **twelve-month mark**, execute total deletion:
  - Remove records from the `places` and `toponyms` indices.
  - Remove pending assertions from DO PG `contributor_attestations`.
  - Remove dataset metadata.
  - Remove the dataset from the contributor's dataset management panel.
  - Send confirmation notification.
- [ ] Exclusions:
  - Datasets in `submitted` state pause the timer for the duration of editorial review.
  - Rejected datasets resume the timer from the rejection date.
  - Datasets flagged `retention: private_permanent` (Master Plan §10.3) are not subject
    to the sweep.
  - Published datasets are never subject to retention.

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
