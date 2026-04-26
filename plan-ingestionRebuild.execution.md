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
   gazetteer at indexing time, supporting browser-side intersection tests with the Atlas Area
   filter (Master Plan §1.4.1, Appendix E.2 item 1). New stage in Batch 9.
3. **Per-gazetteer temporal extent.** A `[start_year, end_year]` summary per gazetteer is
   precomputed alongside the H3 coverage (Master Plan §1.4.1, Appendix E.2 item 2). New stage
   in Batch 9.
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
8. **v3.2 legacy migration path.** A one-time batch admits existing accessioned datasets to
   the new `places` index with `dataset_status: published`, mapping their reconciliation links
   to `contributor_attestations` rows with `legacy_v3_2: true` (Master Plan §10.2, Appendix
   E.2 item 7). New Batch 13b.
9. **Retention sweep for pending datasets.** A scheduled job deletes pending datasets that
   sit unmodified for one year, with an eleven-month notification (Master Plan §10.1,
   Appendix E.2 item 8). New Batch 14a.

The original "authority-selection.md checkbox file" remains the current control mechanism for
runs and is unchanged in the short term; once the Django gazetteer registry (item 7) is live,
authority selection moves to the API and the markdown file is retired.

### Status snapshot

| Batch | Scope | Status |
|-------|-------|--------|
| 1 | Schema / settings / staged layout | Largely done; **must add** `dataset_status` / `dataset_id`, gazetteer-aggregate fields |
| 2 | Type-mapping preflight (production `types` index) | Done |
| 3 | Orchestration + checkpointing | Done; needs Django-registry resolution path (later) |
| 4a | Staged extraction shim | Done (`helpers.py`) |
| 4b | Canary refactors (`nl`, `po`) | Done |
| 4c Phase 1 | `gn`, `wd`, `osm`, `ohm` refactor | Done |
| 4c Phase 2–4 | `tgn`, `pl`, `gb`, `iv`, `geonames-toponyms`, `wikidata-geoshapes`, `loc-relations`, `whg` | Pending |
| 4d | Boundary stage + consolidation | `boundary_stage.py` + `boundary_merge.py` done; `_consolidate_extracts()` pending |
| 5 | Patch-collapse merges | `boundary_merge.py` done; `h3_merge.py`, `ccode_merge.py` pending |
| 6 | H3 derivation (Slurm array) | Done (`h3_stage.py`, `submit_h3_slurm.py`); benchmarking pending |
| 7 | CCode enrichment | Pending (`ccode_enrichment.py`, `ccode_merge.py`) |
| 8 | Global barrier | Pending (manifest validator) |
| 9 | Toponyms + Symphonym **+ per-gazetteer aggregates** | Pending; aggregates are new |
| 10 | Tile generation (no ES) | Pending |
| 11 | Index loaders + **gazetteer inventory push** | Pending; inventory push is new |
| 12 | **SQLite hard-link harvest** (replaces post-index ES clustering) | New; pre-existing `clustering/harvest/hard_links.py` (ES-based) supplies the algorithm but must be re-targeted at staged files |
| 13a | WHG dataset authority integration (`whg:`) | Pending |
| 13b | **v3.2 legacy migration** | New; pending |
| 14 | Test harness + integration rollout | Pending |
| 14a | **Retention sweep** for pending datasets | New; pending |

---

## Execution Model

- **Global preflight**: establish the selected gazetteer set, shared config, manifests,
  credentials, and read-only caches. Validates production ES `types` index availability
  (Batch 2) and discovers WHG datasets (Batch 13a).
- **Per-gazetteer preprocessing fan-out**: each gazetteer runs its own staged extraction
  pipeline through place-level preprocessing (`extract → boundary → boundary_merge → H3 →
  ccode`). This is the main parallelisation domain and is intended to be runnable as
  separate Slurm jobs.
- **Global barrier** (Batch 8): corpus-wide phases must wait until every selected
  gazetteer reports preprocessing complete in its manifest.
- **Global post-barrier phases**: toponym deduplication + Symphonym embedding (Batch 9),
  per-gazetteer H3-coverage and temporal-extent aggregation (Batch 9), tile generation
  (Batch 10), indexing (Batch 11), gazetteer inventory push (Batch 11), hard-link SQLite
  harvest + ship-to-Pitt (Batch 12).

Toponym deduplication and Symphonym generation are **not gazetteer-local** in this design;
they run once over the union of all selected gazetteers after the preprocessing barrier.

Authority inclusion/exclusion is currently controlled by a root-level checkbox markdown file
(`authority-selection.md`). Deselection triggers staged-artefact cleanup at preflight while
source files remain cached. Once the Django gazetteer registry is live (Master Plan §5.3),
selection moves into Django and the markdown file is retired.

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
   local preprocessing stages.
5. **Mixed-source tile jobs** wait until all contributing gazetteers complete.
6. When all selected gazetteers report stages 2–5 complete, submit the **Barrier job**.
7. On barrier success, submit one **Global Toponyms + Symphonym + Aggregates** job.
8. After corpus-wide toponym/Symphonym + aggregate staging completes, start or verify the
   **staging ES instance**.
9. Submit the **Indexing** job; on success, submit the **Gazetteer inventory push** job.
10. In parallel with indexing, submit the **Hard-link harvest** job (no ES dependency); on
    success, submit the **Ship-to-Pitt** swap.

### Resume / retry model

- Retries occur at **stage boundaries**, not inside partially written artefacts.
- The controller inspects manifests/checkpoints and re-submits only failed stages.
- A failed corpus-wide toponym/Symphonym run must not trigger re-extraction of already-
  complete gazetteers; it restarts from the barrier output.
- ES-dependent retries begin from staging ES verification onward.
- Hard-link harvest is independent and can be retried without re-running indexing.

---

## External Contracts (Implemented in Django)

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
- [ ] **(New, Master Plan E.2 item 3)** Add top-level `dataset_status` (`published` |
  `pending`) and `dataset_id` (string) fields to the staged record contract and to
  `schemas/places.json`. Default at extract time: `dataset_status='published'`,
  `dataset_id='<namespace>'` for ordinary authorities; `dataset_id='whg:<dataset_id>'` for
  WHG datasets (Batch 13a).
- [ ] **(New, Master Plan §1.4.1 + E.2 items 1–2)** Define the per-gazetteer aggregate
  contract that Batch 9 will produce: `gazetteer_aggregates/{namespace}.json` containing
  `{namespace, record_count, h3_coverage_compacted: [str], temporal_extent: [int|null,
  int|null]}`.
- [ ] **(New)** Document the SQLite hard-link database schema (`hard_link_assertions`)
  in `processing/staging_contract.py` for Batch 12 consumers.

Validation gates:

- [x] Schema review and acceptance.
- [x] Geometry lookup resolves deterministically for sampled staged rows.
- [x] Manifest can be serialised and loaded without data loss.
- [ ] `places.json` ingest test admits `dataset_status` and `dataset_id` and they
  round-trip through the indexing path.
- [ ] Aggregate file schema validates against a sample for `nl` and `po`.

### Batch 2: Type Mapping Preflight (Production `types` Index)

Targets:

- `processing/aat_lookup.py`
- `processing/settings.py`

Dependencies: Batch 1.

Status: complete.

Tasks:

- [x] Add preflight check for production ES availability and `types` index access.
- [x] Implement direct reverse-lookup mapping against production `types` index fields
  (e.g. `gn_fcodes`, `wd_qids`, `osm_tags`, `ohm_tags`).
- [x] During ingestion, store both original source type and mapped AAT path string in
  staged place records.

Validation gates:

- [x] Type preflight fails fast when production `types` index is unavailable.
- [x] Sample mappings return expected AAT IDs/path values.

### Batch 3: Orchestration and Checkpointing

Targets:

- `scripts/ingest.sh`
- `scripts/es.sh`
- `processing/ingest_all_authorities.py`
- `processing/staging_orchestrator.py`

Dependencies: Batch 1, Batch 2.

Status: complete; fan-out / fan-in dependency chain wired through `ingest.sh` for
boundary → boundary_merge → H3.

Tasks:

- [x] Introduce stage-level checkpoints and resume semantics.
- [x] Separate extract/stage from index operations in orchestration.
- [x] Implement fan-out/fan-in orchestration: parallel authority preprocessing, then
  explicit global barrier before corpus-wide phases.
- [x] Resolve run authority set from `authority-selection.md`.
- [x] Add namespace-scoped run IDs and immutable manifests.
- [x] Ensure idempotent reruns for a failed stage.
- [ ] **(New, deferred to Batch 11 enabling work)** Add a fallback selection-resolution
  path that consults the future Django gazetteer registry once it is available; keep the
  markdown file as the immediate source of truth.

Validation gates:

- [x] Kill/restart test resumes from the next incomplete stage.
- [x] Re-run does not duplicate outputs or corrupt manifests.

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
- [ ] **(New)** Extend `write_staged_place_doc` to require `dataset_status` and
  `dataset_id` on every doc (default `published` / `<namespace>`); reject docs missing
  either field.

#### 4b. Authority Script Canaries

- [x] `authorities/nativeland-places.py` (nl) — staged-mode + ES backward compatibility.
- [x] `authorities/periodo-places.py` (po) — same.
- [ ] `authorities/cliopatria-places.py` (clio, if it exists; else skip to `pl`).

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

**Phase 2 (Medium Priority)** — pending:

- [ ] `authorities/tgn-places.py` (tgn).
- [ ] `authorities/pleiades-places.py` (pl).
- [ ] `authorities/gb1900-places.py` (gb).
- [ ] `authorities/indexvillaris-places.py` (iv).

**Phase 3 (Lower Priority, Update Scripts)** — pending:

- [ ] `authorities/geonames-toponyms.py` (update; auxiliary toponym records).
- [ ] `authorities/wikidata-geoshapes.py` (update; enriches existing places).
- [ ] `authorities/loc-relations.py` (relations-only).

**Phase 4 (WHG Datasets)** — pending; depends on Batch 13a:

- [ ] `authorities/whg-places.py` (new).

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
- [ ] `_consolidate_extracts()` in `processing/stage_writers.py` — merge fragmented JSONL
  writes into consolidated Parquet per namespace. Lightweight IO-only step that runs after
  the orchestrator marks a namespace's extract stage complete.

Validation gates:

- [ ] Staged snapshots for all selected gazetteers are complete and valid Parquet.
- [ ] Row counts and key uniqueness verified per namespace.
- [ ] No ES access during extraction or consolidation.
- [ ] `dataset_status` and `dataset_id` present and correctly set on every record.

### Batch 5: Per-Gazetteer Patch-Collapse and Update Transforms

Targets:

- `processing/boundary_merge.py` ✅
- `processing/h3_merge.py` (new)
- `processing/ccode_merge.py` (new)
- `processing/namespace_materialize.py` (existing; finalise manifests)
- `processing/ingest_all_authorities.py`

Dependencies: Batch 4 (extract finished), Batch 6 (H3 patches), Batch 7 (ccode patches).

Tasks:

- [x] `boundary_merge.py` — reads staged extract snapshot + boundary patch JSONL,
  merges completed boundary geometry into place docs before H3, writes
  `{namespace}/boundary_merged/places.parquet|jsonl`.
- [ ] `h3_merge.py` — reads staged extract snapshot + H3 patch JSONL, merges H3 fields
  into each place document's geometries, writes the H3-enriched snapshot.
- [ ] `ccode_merge.py` — reads staged snapshot (post-H3) + ccode patch JSONL, merges
  ccodes into each place document, writes the final snapshot.
- [ ] Define patch merge semantics (e.g., ccode_merge treats patch entries as
  authoritative; overwrites existing ccodes).
- [ ] Idempotency: merging the same patches twice produces identical output.
- [ ] Patch files reference correct geometry indices.

Validation gates:

- [ ] Patch merge is idempotent and deterministic.
- [ ] Output row counts and key integrity pass.
- [ ] Enriched snapshots have `h3_centroid` / `h3_cover` per geometry and `ccodes` at
  document level as expected.

### Batch 6: Per-Gazetteer H3 Derivation (Slurm Array Job)

Targets:

- `processing/helpers.py`
- `processing/h3_stage.py` ✅
- `processing/submit_h3_slurm.py` ✅
- `processing/h3_merge.py` (called from Batch 5)

Dependencies: Batch 4 (extract complete), Batch 3 (orchestrator).

> **Status**: `h3_stage.py` and `submit_h3_slurm.py` are implemented. H3 is deferred by
> default (`--inline-h3` to opt in during extraction; not recommended).

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
- [ ] **(New, supports Batch 9 aggregate computation)** Emit a per-namespace
  `h3_cell_set` checkpoint (the union of all `h3_centroid` + `h3_cover` cells observed
  during the H3 stage) at coarse resolution (e.g. r5) for fast downstream compaction in
  Batch 9. Avoids re-reading the full snapshot just to compute the gazetteer-level
  coverage set.

Validation gates:

- [x] H3 patches write without ES access.
- [x] H3 fields correctly materialise within geometry objects.
- [x] Multi-geometry places have h3 data in each geometry.
- [ ] No hull-derived coverages (only full-geometry H3).

### Batch 7: Per-Gazetteer CCode Enrichment (Post-H3, Using UN Coverage Pre-Filter)

Targets:

- `processing/ccode_enrichment.py` (new)
- `processing/ccode_merge.py` (new; called from Batch 5)
- `processing/ingest_all_authorities.py`

Dependencies: Batch 6 (H3 complete for all gazetteers, especially `un`).

> **Architecture**: per-namespace ccode enrichment reads the namespace's H3-enriched
> snapshot, the UN H3 coverage (if namespace ≠ un), and the UN place geometries. For each
> place geometry's H3 cover, test containment against UN geometries; emit a ccode patch.
> Batch 5 merges the patch into the final snapshot.

Tasks:

- [ ] `ccode_enrichment.py`:
  - Load UN staged records with H3 coverage into memory.
  - For each namespace ≠ un, iterate through H3-enriched staged snapshot.
  - Use H3 intersection with UN coverage to pre-filter candidate ccodes per geometry.
  - Implement point-in-polygon for points; polygon intersection / majority-overlap for
    areas.
  - Emit ccode patch records (`{namespace}/ccode/places.ccode.jsonl`).
- [ ] `ccode_merge.py` reads staged snapshot + ccode patches, merges ccodes into docs.
- [ ] Per-namespace ccode stage status in run manifest.

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

### Batch 9: Global Toponyms + Symphonym + Per-Gazetteer Aggregates

Targets:

- `phonetics/extraction/rebuild_toponyms_index.py`
- `phonetics/inference/update_es.py`
- `processing/embed_extract.py`
- `processing/embed_transform.py`
- `processing/embed_load.py`
- `processing/gazetteer_aggregates.py` (new — H3 coverage + temporal extent)

Dependencies: Batch 8 (global barrier complete).

Tasks:

**Toponyms + Symphonym** (existing scope):

- [ ] Read the union of all selected staged place snapshots (not ES `places`) as canonical
  source.
- [ ] Extract unique toponyms with attestations across the full selected corpus.
- [ ] Deduplicate toponyms corpus-wide across gazetteers before embedding generation.
- [ ] Compute Symphonym embeddings (GPU-enabled) over the deduplicated corpus.
- [ ] Maintain persistent Symphonym cache; recompute embeddings only for changed toponyms.
- [ ] Run Symphonym model/version preflight; invalidate cache when version changes.
- [ ] Stage toponym records and embeddings.
- [ ] **Clear scope boundary**: no ES indexing in this stage; outputs remain staged.

**Per-gazetteer aggregates** (new, Master Plan §1.4.1 + E.2 items 1–2):

- [ ] `gazetteer_aggregates.py` reads all selected staged snapshots (or the per-namespace
  H3 cell-set checkpoints from Batch 6) and produces, per gazetteer:
  - `record_count`,
  - `h3_coverage_compacted` — union of all `h3_centroid` and `h3_cover` cells, compacted
    via `h3.compact_cells` to the smallest representation (typically dominated by r5/r6
    parents, ~hundreds to a few thousand cells per large gazetteer),
  - `temporal_extent` — `[min(start_year), max(end_year)]` across all timespans on all
    records (null where the gazetteer has no temporal data).
- [ ] Outputs written to `staged/_aggregates/{namespace}.json`; consumed by Batch 11
  inventory push.
- [ ] Recomputed on every full run; an incremental path may be added later if needed.

Validation gates:

- [ ] Toponym extraction produces expected counts.
- [ ] Corpus-wide deduplication merges cross-gazetteer attestations as expected.
- [ ] Embedding/index outputs remain schema-compatible.
- [ ] Incremental run re-embeds only changed toponyms when model version is unchanged.
- [ ] Cache invalidation triggers full recompute when model/version changes.
- [ ] No ES access during this stage.
- [ ] Aggregate H3 sets are valid compacted H3 representations and round-trip through
  `h3.uncompact_cells`.
- [ ] `temporal_extent` correctly null-handles records lacking timespans.

### Batch 10: Tile Generation from Staged Geometry (No ES Dependency)

Targets:

- `processing/generate_tiles.py`
- `scripts/ingest.sh`

Dependencies: Batch 4/5/6 complete for relevant gazetteers; mixed-source outputs require
all contributing gazetteers complete.

Tasks:

- [ ] Refactor tile generation to read staged artefacts + geometry store only.
- [ ] Produce required outputs:
  - `po.mbtiles`
  - `clio.mbtiles`
  - `nl.mbtiles`
  - `osm_admin.mbtiles`
  - `ohm_admin.mbtiles`
  - `osm_misc.mbtiles` (mixed OSM/OHM types)
- [ ] Keep synthetic boundary products folded into OSM tileset.
- [ ] Separate per-gazetteer tile jobs from mixed-source tile jobs where that reduces
  scheduling contention.

Validation gates:

- [ ] Tile generation runs with ES stopped.
- [ ] Output layers and counts are reproducible.

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

- [ ] `push_gazetteer_inventory.py` builds the inventory payload from the run-level
  gazetteer set (Batch 8) + per-gazetteer aggregates (Batch 9). Each entry:
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
    "h3_coverage": ["<compact h3 cell>", "..."],
    "temporal_extent": [-2000, 2025]
  }
  ```
  WHG-dataset entries (Batch 13a) carry `class: "dataset"` and the contributor's
  `owner_user_id`; `status` reflects `draft` / `submitted` / `rejected` / `published`
  per the contribution workflow.
- [ ] POST/PUT the payload to the Django gazetteer-registry endpoint (contract TBD with
  the Django team).
- [ ] Idempotent: re-running the push for the same indexed corpus produces no change in
  Django; updates use upsert semantics.
- [ ] Runs after every successful indexing job and after dataset publish/withdraw events
  affect the registry.

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
- `clustering/harvest/contributor_replay.py` (new — replays active rows from DO PG)
- `clustering/sqlite_overlay.py` (new — schema, builders, atomic ship-to-Pitt)
- `processing/ingest_all_authorities.py` (orchestration hook)

Dependencies: Batch 8 (barrier — staged files are complete and self-consistent for the
selected gazetteer set). Independent of Batch 11 (no ES dependency).

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

### Batch 13a: WHG Dataset Authority Integration (`whg:`)

Targets:

- `processing/fetch_authorities.py`
- `processing/settings.py`
- `authorities/whg-places.py` (new)

Dependencies: Batch 1 (settings), Batch 3 (orchestrator), Batch 4 (writers), Runtime
Prerequisites complete.

Tasks:

- [ ] Integrate discovery call to `GET /reconcile/authority-datasets`.
- [ ] Parse `result[{id, title, place_count}]` and register each dataset ID for staging.
- [ ] Refresh WHG dataset entries in `authority-selection.md` (append newly discovered
  datasets as checked by default), pending the gazetteer-registry transition (Batch 11).
- [ ] For each dataset ID, fetch the LPF stream from
  `GET /entity/dataset:<id>/api?filetype=lpf`.
- [ ] Treat each dataset as a separate authority unit under the `whg` namespace.
- [ ] Emit per-dataset manifests and staged artefacts.
- [ ] Stable ID mapping to canonical `whg:{dataset_id}:{entity_id}`.
- [ ] Implement auth wiring for both token-query and bearer-token modes.
- [ ] Respect the local WHG group checkbox gate when deciding whether discovered WHG
  datasets are included in the current run.
- [ ] **(New, Master Plan §7.2)** Each emitted record carries
  `dataset_status='published'` if the dataset is published in Django, otherwise
  `dataset_status='pending'`; `dataset_id='whg:<dataset_id>'` in both cases.

Validation gates:

- [ ] Discovery endpoint integration test validates response parsing and empty-result
  handling.
- [ ] LPF fetch integration test validates streaming gzip handling.
- [ ] Multiple dataset IDs ingest independently and can be replaced/removed independently.
- [ ] Pending-status datasets index with `dataset_status='pending'` and are correctly
  hidden from off-scope discovery queries (smoke-test against the gateway's scope filter
  once the gateway-side change lands).

### Batch 13b: v3.2 Legacy Migration (One-Time)

Targets:

- `processing/legacy_migration_v3_2.py` (new)
- `clustering/sqlite_overlay.py` (extended for `legacy_v3_2` flag)

Dependencies: Batch 11 (indexing pipeline operational), Batch 12 (SQLite overlay
operational).

Master Plan §10.2 requires preserving v3.2's accumulated accessioned datasets and their
reconciliation links through the architectural transition.

Tasks:

- [ ] One-time batch admits all currently-published v3.2 datasets to the new `places`
  index with `dataset_status: 'published'`, `dataset_id: 'whg:<dataset_id>'`.
- [ ] Map existing v3.2 reconciliation links to `contributor_attestations` rows
  (DO PG side) with:
  - `source_category = 'contributor'`,
  - `source_id = 'contributor:<original_contributor_id>'`,
  - `status = 'active'`,
  - **`legacy_v3_2 = true`** (new boolean column on the DO PG table; Pitt SQLite
    `source_id` carries the same suffix for downstream filterability).
- [ ] Preserve dataset metadata (description, citation, license, contributor identity).
- [ ] Preserve accession history as historical metadata, **not** mapped onto the new
  submission/review state machine.
- [ ] In-progress v3.2 reconciliations migrate as pending datasets with their existing
  assertions in `status = 'pending'`.
- [ ] Diagnostic output identifies edge cases requiring manual attention.

Validation gates:

- [ ] All published v3.2 datasets present in the new `places` index after migration.
- [ ] Migrated assertions survive the next Batch 12 hard-link harvest and appear in the
  Pitt SQLite with `legacy_v3_2` distinguishability.
- [ ] Manual-attention edge-case list is bounded and addressable by the WHG team.

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
- [ ] **Per-gazetteer H3 coverage and temporal extent are computed and shipped to the
  Django gazetteer registry on each successful run.**
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
- [ ] **v3.2 legacy migration completed (one-time); all migrated assertions carry
  `legacy_v3_2 = true`.**
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
- v3.2 accessioned datasets and their reconciliation links are migrated, with
  `legacy_v3_2 = true` distinguishing them from new contributor work.
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
