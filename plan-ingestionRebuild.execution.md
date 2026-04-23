# Execution Checklist: Ingestion Rebuild

This checklist maps `plan-ingestionRebuild.prompt.md` to concrete implementation
work in this repository, with external dependencies called out explicitly.

## Overview

This plan is divided into **PR-sized batches** with explicit dependencies. Phases are
ordered to enable early validation and staging of core infrastructure before
large-scale data operations (OSM, Wikidata, OHM).

## Execution Model

- **Global preflight**: establish the selected authority set, shared config,
  manifests, credentials, and read-only caches.
- **Per-authority preprocessing fan-out**: each authority runs its own staged
  extraction pipeline through place-level preprocessing (`extract → patch-collapse
  → H3 → ccode`). This is the main parallelization domain and is intended to be
  runnable as separate Slurm jobs.
- **Global barrier**: corpus-wide phases must wait until every selected
  authority reports preprocessing complete in its manifest.
- **Global post-barrier phases**: toponym deduplication, Symphonym generation,
  indexing, and clustering.

Toponym deduplication and Symphonym generation are **not authority-local** in
this design; they run once over the union of all selected authorities after the
preprocessing barrier.

Authority inclusion/exclusion is controlled by a root-level checkbox markdown
selection file (`authority-selection.md`). No separate ad hoc
authority-removal mechanism is used; deselection is handled by staged-artefact
cleanup during preflight while source files remain cached.

## Slurm Orchestration Sketch

The intended scheduler shape is a **fan-out / fan-in** workflow coordinated by a
single controller layer.

### Job classes

- **Controller job**
  - Computes the selected authority set for the run.
  - Writes the run manifest.
  - Submits downstream jobs with success-only dependencies.
  - Re-submits only missing/failed stages on resume.
- **Global preflight job**
  - Performs runtime setup, credential checks, type-mapping preflight, and WHG
    authority discovery.
  - Resolves selected authorities from checkbox configuration.
  - Removes stale staged artefacts for deselected authorities.
- **Per-authority preprocessing jobs**
  - One authority per job by default.
  - Large authorities may further split internally into arrays/shards for H3 or
    other heavy local work.
- **Barrier job**
  - Reads manifests for all selected authorities.
  - Refuses to release corpus-wide work until every required authority has
    completed stages 2–5.
- **Global corpus job**
  - Runs toponym deduplication and Symphonym once over the full staged corpus.
- **Tile jobs**
  - Per-authority jobs where outputs are authority-local.
  - Mixed-source jobs where outputs depend on multiple authorities.
- **ES-dependent jobs**
  - Start/check staging ES, index staged outputs, then run clustering.

### Recommended dependency flow

1. **Controller** submits **Global preflight**.
2. **Global preflight** submits an early **`un` preprocessing branch** through
   stages 2–5.
3. Once `un` H3 is complete, the controller submits **non-`un` authority
   preprocessing jobs** in parallel for stages 2–5.
4. Authorities that do not need `ccode` enrichment may proceed independently,
   but any authority requiring stage 5 waits on successful `un` H3 completion.
5. **Per-authority tile jobs** may start after that authority completes the
   relevant local preprocessing stages.
6. **Mixed-source tile jobs** wait until all contributing authorities complete.
7. When all selected authorities report stages 2–5 complete, submit the
   **Barrier job**.
8. On barrier success, submit one **Global Toponyms + Symphonym** job.
9. After corpus-wide toponym/Symphonym staging completes, start or verify the
   **staging ES instance**.
10. Submit the **Indexing** job.
11. On indexing success, submit the **Clustering** job.

### Resume / retry model

- Retries should occur at **stage boundaries**, not inside partially written
  artefacts.
- The controller should inspect manifests/checkpoints and re-submit only:
  - failed authority-local stages,
  - missing mixed-source tile jobs,
  - failed barrier/global jobs if their prerequisites are still valid.
- A failed corpus-wide toponym/Symphonym run must not trigger re-extraction of
  already-complete authorities; it should restart from the barrier output.
- ES-dependent retries should begin from staging ES verification onward and not
  force a rerun of completed pre-index staging work.

## External Contract (Implemented in Django)

- `Dataset.authority` controls dataset eligibility for authority ingestion.
- Discovery endpoint: `GET /reconcile/authority-datasets` (returns
  `result[{id,title,place_count}]`).
- LPF endpoint per dataset: `GET /entity/dataset:<id>/api?filetype=lpf`
  (streamed gzip response).
- Auth: token/session auth (token query param or bearer token).

In-scope work in this repo is to consume this contract during staging and
indexing orchestration.

## Runtime Prerequisites

- [ ] Configure WHG API base URL for discovery/LPF calls.
- [ ] Provide token/session credentials for authenticated endpoint access.
- [ ] Add retry/backoff policy and timeout defaults for WHG API fetches.
- [ ] Add root-level checkbox authority selection file and parser/validation rules.
- [ ] Ensure `authority-selection.md` is bootstrapped with checked entries for
  all local authorities and currently discovered WHG datasets.

## Parallelisation Model and Contention Points

- **Workable parallelism**: yes, authority ingestion is parallelizable up to but
  not including corpus-wide toponym deduplication/Symphonym.
- **Safe parallel region**: staged extraction, patch-collapse, H3 derivation,
  and post-H3 ccode enrichment, provided outputs are namespace-scoped.
- **Ordering constraint**: non-`un` ccode enrichment must wait until the `un`
  authority has completed H3 derivation.
- **Shared-resource contention to manage**:
  - staged/VAST filesystem bandwidth and metadata churn,
  - geometry-store consolidation/finalization steps,
  - remote API or source-download rate limits,
  - mixed-source tile products (especially OSM/OHM outputs),
  - CPU/RAM/GPU scheduling fairness across simultaneous Slurm jobs.
- **Not a blocker pre-index**: staging ES is not needed until indexing, so ES
  contention does not prevent per-authority preprocessing fan-out.

## Execution Batches

### Batch 1: Foundation (Schema, Settings, Manifest Contract)

Targets:
- `processing/settings.py`
- `processing/helpers.py`
- `processing/geom_store.py`
- `schemas/places.json` (if field additions)

Dependencies: None (foundational).

Tasks:
- [ ] Define staged filesystem layout (`staged/{namespace}/...`) in settings.
- [ ] Define manifest schema contract (`manifest.json`): fields for lineage, counts, checksums, stage status, H3 completion metrics, ccode coverage metrics, output artefact paths.
- [ ] Define geometry reference contract (lookup key, shard/index layout, per-geometry reference structure).
- [ ] Confirm `places` field compatibility for enriched staged output
  (`repr_point`, `hull`, `bounds`, `has_geom`, `h3_centroid`, `h3_cover`, `ccodes`).
  (Per-geometry: `repr_point`, `hull`, `bounds`, `has_geom`, `h3_centroid`, `h3_cover`; top-level: `ccodes`).
- [ ] Define ccode patch record schema and merge semantics.
- [ ] Define h3 fields as per-geometry nested: `geometries[].{h3_centroid: "cell_id", h3_cover: ["cell_id", ...]}` (H3 cell IDs as strings).

Validation gates:
- [ ] Schema review and acceptance.
- [ ] Geometry lookup resolves deterministically for sampled staged rows.
- [ ] Manifest can be serialized and loaded without data loss.

### Batch 2: Type Mapping Preflight (Production `types` Index)

Targets:
- `processing/aat_lookup.py`
- `processing/settings.py`

Dependencies: Batch 1 (settings).

Tasks:
- [ ] Add preflight check for production ES availability and `types` index access.
- [ ] Implement direct reverse-lookup mapping against production `types` index
  fields (e.g. `gn_fcodes`, `wd_qids`, `osm_tags`, `ohm_tags`).
- [ ] During ingestion, store both original source type and mapped AAT path
  string in staged place records.

Validation gates:
- [ ] Type preflight fails fast when production `types` index is unavailable.
- [ ] Sample mappings return expected AAT IDs/path values.

### Batch 3: Orchestration and Checkpointing

Targets:
- `scripts/ingest.sh`
- `scripts/es.sh`
- `processing/ingest_all_authorities.py`
- (new) `processing/staging_orchestrator.py`

Dependencies: Batch 1 (schema/settings), Batch 2 (type mapping preflight).

Tasks:
- [ ] Introduce stage-level checkpoints and resume semantics.
- [ ] Separate extract/stage from index operations in orchestration.
- [ ] Implement fan-out/fan-in orchestration: parallel authority preprocessing,
  then explicit global barrier before corpus-wide phases.
- [ ] Resolve run authority set from `authority-selection.md`.
- [ ] Add namespace-scoped run IDs and immutable manifests.
- [ ] Ensure idempotent reruns for a failed stage.

Validation gates:
- [ ] Kill/restart test resumes from the next incomplete stage.
- [ ] Re-run does not duplicate outputs or corrupt manifests.

### Batch 4: Artefact Writers and Per-Authority Extraction

Targets:
- (new) `processing/stage_writers.py`
- `processing/ingest_all_authorities.py`

Dependencies: Batch 1 (schema/settings), Batch 2 (type mapping preflight), Batch 3 (orchestrator).

Tasks:
- [x] Implement Parquet + geometry blob writers for staged records (`stage_writers.py`).
- [x] Write initial namespace snapshots (small authorities first: `nl`, `po`, `clio`).
  > **⚠️ Current limitation**: `write_namespace_places_snapshot_parquet()` reads back from
  > Elasticsearch after the authority script indexes to ES. This means ES is still required during
  > the extract phase. **Remaining Batch 4 work** is to have authority scripts write staged
  > artefacts directly (without ES), so the staging phase is fully ES-independent.
  >
  > Required changes:
  > 1. Add a `WHG_STAGING_MODE=1` env var and a `write_staged_place_doc()` shim in
  >    `processing/helpers.py` that writes to `{STAGED_BASE_DIR}/{namespace}/extract/places.jsonl`
  >    instead of (or in addition to) calling `helpers.bulk()`.
  > 2. Patch each authority script to call the shim instead of direct `helpers.bulk()`.
  > 3. Change snapshot writer to read from staged files rather than ES scroll.
  > 4. Remove the `check_elasticsearch()` guard from `ingest_all_authorities.py` when
  >    `WHG_STAGING_MODE=1` is set (ES need not be running).
- [x] Validate row counts and key integrity.
- [x] Ensure outputs are namespace-scoped so concurrent authority jobs do not
  contend on staged artefact paths.
- [x] Resolve selected authority set from checkbox file and ingest only selected authorities.
- [x] Remove stale staged artefacts for deselected authorities before fan-out.

Validation gates:
- [ ] Staged snapshots for `nl` and `po` are complete **without ES running**. ← Pending ES-decoupling above.
- [ ] Geometry blob lookup works end-to-end for sampled records.

### Batch 5: Per-Authority Patch-Collapse and Update Transforms

Targets:
- (new) `processing/namespace_materialize.py`
- `processing/ingest_all_authorities.py`

Dependencies: Batch 4 (writers).

Tasks:
- [ ] Implement patch-collapse for update-style transforms (geoshapes, relations, auxiliary toponyms).
- [ ] Merge patches into one final staged namespace snapshot.
- [ ] Keep patch intermediates optional (checkpoint only).

Validation gates:
- [ ] Patch merge is idempotent and deterministic.
- [ ] Output row counts and key integrity pass.

### Batch 6: Per-Authority H3 Derivation (Slurm Array Job)

Targets:
- `processing/helpers.py`
- `processing/h3_stage.py`
- (new) `processing/submit_h3_slurm.py`
- `scripts/ingest.sh`

Dependencies: Batch 1 (settings), Batch 3 (orchestrator), Batch 4 (writers).

> **Status**: `h3_stage.py` is implemented (reads staged extract artefacts, writes per-geometry
> H3 patch JSONL without touching ES). `submit_h3_slurm.py` is new — reads pending namespaces
> from run manifest, estimates wall times from persistent runtime history, builds and submits a
> Slurm array job (one task per namespace) with per-namespace QOS selection.
>
> H3 is now **deferred by default** in `ingest_all_authorities.py` (`--inline-h3` required to
> opt in to inline computation, which is not recommended for large authorities).

Tasks:
- [x] Implement `h3_stage.py` — Slurm array worker reading staged records + full geometry.
- [x] Compute `h3_centroid` and `h3_cover` from full geometry only.
- [x] Write keyed derived artefacts (`{namespace}/h3/places.h3.jsonl`) for merge.
- [x] Implement `submit_h3_slurm.py` — auto-sizes wall time per namespace from history,
  selects QOS tier, emits sbatch array script and submits.
- [x] `--defer-h3` is now the default (`defer_h3=True`); `--inline-h3` opts back in.
- [x] H3 wall times recorded to persistent `namespace-runtime-history.json`.
- [ ] Benchmark array sizing/chunking for large namespaces (`osm`, `ohm`, `gn`).
- [ ] Integrate H3 merge step into the snapshot after H3 array completes
  (`processing/h3_merge.py` — merge H3 patches into final staged snapshot).

Validation gates:
- [ ] No hull-derived coverages in outputs.
- [ ] Array sizing/chunking benchmark documented.
- [x] H3 fields correctly materialise into enriched snapshot within geometry objects.
- [x] Multi-geometry places have h3_centroid and h3_cover in each geometry.

### Batch 7: Per-Authority CCode Enrichment (Post-H3, Using UN Coverage Pre-Filter)

Targets:
- (new) `processing/ccode_enrichment.py`
- `processing/ingest_all_authorities.py`

Dependencies: Batch 6 (target authority H3 complete), Batch 6 for `un` authority complete.

**Important:** UN authority must be pre-ingested with H3 coverage computed before
ccode enrichment can use its coverage as pre-filter. This requirement may be satisfied
by a prior full ingestion run or by explicitly ordering UN early in a new full run.

Tasks:
- [ ] Load UN staged records with H3 coverage into memory.
- [ ] For each place geometry's `h3_cover`, use intersection with UN geometry coverage to pre-filter candidate ccodes.
- [ ] Implement point containment for point geometries.
- [ ] Implement polygon intersection / majority overlap for areal geometries.
- [ ] Emit ccode patch records keyed by place_id.

Validation gates:
- [ ] Unit checks for point-in-polygon and overlap tie-break behavior.
- [ ] Throughput benchmark confirms efficient pre-filtering.
- [ ] Ccode coverage statistics match expectations.

### Batch 8: Global Barrier — All Selected Authorities Preprocessed

Targets:
- `processing/staging_orchestrator.py`
- `processing/ingest_all_authorities.py`

Dependencies: Batch 4–7 complete for every selected authority.

Tasks:
- [ ] Implement manifest-based barrier check confirming all selected authorities
  completed extract/patch/H3/ccode preprocessing.
- [ ] Fail fast if any selected authority is missing, incomplete, or has stale
  prerequisite artefacts.
- [ ] Materialize the run-level authority inventory used by subsequent global phases.

Validation gates:
- [ ] Barrier refuses to start corpus-wide phases with partial authority coverage.
- [ ] Barrier report lists all selected authorities and completion states.

### Batch 9: Global Toponyms + Symphonym (GPU-Enabled, Staged Only)

Targets:
- `phonetics/extraction/rebuild_toponyms_index.py`
- `phonetics/inference/update_es.py`
- `processing/embed_extract.py`
- `processing/embed_transform.py`
- `processing/embed_load.py`

Dependencies: Batch 8 (global barrier complete).

Tasks:
- [ ] Read the union of all selected staged place snapshots (not ES `places`) as
  canonical source.
- [ ] Extract unique toponyms with attestations across the full selected corpus.
- [ ] Deduplicate toponyms corpus-wide across authorities before embedding generation.
- [ ] Compute Symphonym embeddings (GPU-enabled) over the deduplicated corpus.
- [ ] Maintain persistent Symphonym cache and compute embeddings only for changed toponyms.
- [ ] Run Symphonym model/version preflight; invalidate cache when version changes.
- [ ] Stage toponym records and embeddings.
- [ ] **Clear scope boundary:** No ES indexing in this stage; outputs remain staged.

Validation gates:
- [ ] Toponym extraction produces expected counts.
- [ ] Corpus-wide deduplication merges cross-authority attestations as expected.
- [ ] Embedding/index outputs remain schema-compatible.
- [ ] Incremental run re-embeds only changed toponyms when model version is unchanged.
- [ ] Cache invalidation triggers full recompute when model/version changes.
- [ ] No ES access during this stage.

### Batch 10: Tiles Generation from Staged Geometry (No ES Dependency)

Targets:
- `processing/generate_tiles.py`
- `scripts/ingest.sh`

Dependencies: Batch 4/5/6 complete for relevant authorities; mixed-source outputs
require all contributing authorities complete.

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
- [ ] Separate per-authority tile jobs from mixed-source tile jobs where that
  reduces scheduling contention.

Validation gates:
- [ ] Tile generation runs with ES stopped.
- [ ] Output layers and counts are reproducible.

### Batch 11: Index Loaders and Selection-Driven Lifecycle

Targets:
- (new) `processing/index_from_stage.py`
- (new) `processing/namespace_lifecycle.py`
- `processing/ingest_all_authorities.py`

Dependencies: Batch 3 (orchestrator), Batch 9 (global toponyms staged), Batch 10 (required tiles complete).

**Prerequisite for indexing:** Staging ES instance must be running.

Tasks:
- [ ] Load `places` exclusively from final enriched staged snapshots.
- [ ] Incrementally update `toponyms` based on staged attestation diffs.
- [ ] Rebuild index outputs from the selected-authority staged corpus only.
- [ ] Do not use separate ad hoc authority-removal operations outside selection-file control.
- [ ] Trigger clustering follow-up after index mutations.

Validation gates:
- [ ] Deselected authorities are absent from staged corpus and therefore absent from indexed outputs.
- [ ] Referential integrity checks pass for sampled docs.

### Batch 12: Clustering Handoff (Post-Index ES Job)

Targets:
- `scripts/cluster.sh`
- `clustering/runner.py`
- `clustering/state.py`

Dependencies: Batch 11 (indexing complete).

Tasks:
- [ ] Keep clustering post-indexing and ES-backed.
- [ ] Define ingestion-to-clustering handoff contract.
- [ ] Ensure namespace mutation triggers documented clustering policy.

Validation gates:
- [ ] Clustering job launches from staged-index completion signal.
- [ ] Cluster stats remain queryable after namespace updates.

### Batch 13: WHG Dataset Authority Integration (`whg:`)

Targets:
- `processing/fetch_authorities.py`
- `processing/settings.py`
- (new) `authorities/whg-places.py`

Dependencies: Batch 1 (settings), Batch 3 (orchestrator), Batch 4 (writers), Runtime Prerequisites complete.

Tasks:
- [ ] Integrate discovery call to `GET /reconcile/authority-datasets`.
- [ ] Parse `result[{id,title,place_count}]` and register each dataset ID for staging.
- [ ] Refresh WHG dataset entries in `authority-selection.md` (append new
  discovered datasets as checked by default).
- [ ] For each dataset ID, fetch LPF stream from
  `GET /entity/dataset:<id>/api?filetype=lpf`.
- [ ] Treat each dataset as separate authority unit under `whg` namespace.
- [ ] Emit per-dataset manifests and staged artefacts.
- [ ] Ensure stable ID mapping to canonical `whg:{dataset_id}:{entity_id}`.
- [ ] Implement auth wiring for both token query and bearer-token modes.
- [ ] Respect local WHG group checkbox gate when deciding whether discovered
  WHG datasets are included in the current run.

Validation gates:
- [ ] Discovery endpoint integration test validates response parsing and empty-result handling.
- [ ] LPF fetch integration test validates streaming gzip handling.
- [ ] Multiple dataset IDs ingest independently and can be replaced/removed independently.
- [ ] WHG dataset subsection in `authority-selection.md` is refreshable and
  remains user-editable for run inclusion/exclusion.

### Batch 14: Test Harness and Integration Rollout

Targets:
- (new) `testing/integration_harness.py`
- `scripts/ingest.sh`

Dependencies: All prior batches.

Tasks:
- [ ] Build minimal integration harness for small namespaces (`nl` + `po`).
- [ ] Validate end-to-end staged-first run without indexing.
- [ ] Validate multi-authority fan-out through per-authority preprocessing, then
  barrier, then single corpus-wide toponym/Symphonym run.
- [ ] Validate index load from stage with ES running.
- [ ] Validate authority deselection via checkbox config and staged-artefact cleanup.
- [ ] Benchmark OSM/OHM critical path (extract, H3 array, tile generation).
- [ ] Document performance baseline and scaling profile.

Validation gates:
- [ ] All acceptance criteria in `plan-ingestionRebuild.prompt.md` pass.
- [ ] Full run no longer depends on one 48-hour ES staging wall-time window.
- [ ] Authority inclusion/exclusion is controlled by checkbox selection file,
  including stale staged-artefact cleanup for deselected authorities.
- [ ] Required tilesets and incremental toponym behavior are verified.
- [ ] WHG `whg:` dataset authority ingestion is functional against
  `/reconcile/authority-datasets` + `/entity/dataset:<id>/api?filetype=lpf`.

## Definition of Done (Full Implementation)

- [ ] All batches 1–14 complete and tested.
- [ ] Per-namespace staged snapshot fully materialized before indexing.
- [ ] H3 derived from full geometry only, per-geometry covers present.
- [ ] Ccode assignment uses UN H3 coverage as pre-filter.
- [ ] Toponym deduplication and Symphonym generation run once, corpus-wide,
  after all selected authorities complete preprocessing.
- [ ] Authority selection is controlled only by checkbox configuration with no
  parallel ad hoc removal path.
- [ ] Mbtiles products generated from staged artefacts only.
- [ ] Symphonym embeddings generated and used for `toponyms`.
- [ ] Symphonym cache supports incremental embedding updates with model-version
  based invalidation.
- [ ] Clustering runs post-indexing as documented.
- [ ] Integration test suite passes.
