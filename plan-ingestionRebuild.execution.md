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

### Authority Preprocessing Pipeline (Batches 4–7)

**Unified Architecture**: Authorities follow a standardised extraction → enrichment → merge pipeline:

```
┌─────────────────────────────────────────────────────────────────────┐
│ EXTRACT (Batch 4)                                                   │
│ Authority script extracts, writes to {namespace}/extract/places.json│
│ No ES access. Standardised output. Can run in parallel.             │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (per-namespace staged snapshot)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ H3 DERIVATION (Batch 6, Slurm Array)                               │
│ For each {namespace}/h3 task: read snapshot + full geom,           │
│ compute h3_centroid/h3_cover per geometry, write patches.          │
│ Can run in parallel (one Slurm task per namespace).                │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (H3 patches)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ H3 MERGE (Batch 5)                                                  │
│ Read extract snapshot + H3 patches, merge fields into docs.        │
│ Produce {namespace}/h3-merged/places.parquet.                      │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (H3-enriched snapshot)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ CCODE ENRICHMENT (Batch 7)                                          │
│ For each {namespace} ≠ un: read H3-enriched snapshot + UN H3       │
│ geometries, containment-test, emit ccode patches.                  │
│ Can run in parallel (un must complete H3 first).                   │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (ccode patches)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ CCODE MERGE (Batch 5)                                               │
│ Read H3-enriched snapshot + ccode patches, merge ccodes into docs. │
│ Produce {namespace}/final/places.parquet (fully enriched).         │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (fully enriched per-namespace staged snapshot)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ BARRIER (Batch 8)                                                   │
│ Wait for all selected authorities to finish the pipeline above.    │
│ Verify manifest completeness before proceeding to corpus-wide      │
│ phases (Batch 9: Toponyms+Symphonym, Batch 10: Tiles, Batch 11:  │
│ Indexing).                                                          │
└─────────────────────────────────────────────────────────────────────┘
```

**Key Properties**:
- **No ES during Batches 4–7**: All processing on staged files.
- **Per-namespace parallelisation**: Each namespace is independent until the barrier.
- **Standardised outputs**: Each batch produces pre-defined artefacts (JSONL/Parquet snapshots, patches).
- **Idempotent, resumable**: Failed stages can re-run; earlier stages' outputs are reused.

---



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

### Batch 4: Authority Script Refactor to Staged Extraction Pattern

Targets:
- `processing/helpers.py` (add staged extraction shim)
- `authorities/*.py` (all authority scripts)
- `processing/ingest_all_authorities.py`

Dependencies: Batch 1 (schema/settings), Batch 2 (type mapping preflight), Batch 3 (orchestrator).

**Architecture Change:** Authority scripts are refactored from custom ES indexers to **lightweight
standardised extractors** that write place documents to staged JSONL/Parquet files. All per-authority
preprocessing (H3 derivation, ccode enrichment, etc.) becomes generic post-processing that runs
uniformly after extraction.

**Rationale:** This dramatically simplifies authority scripts (each becomes a focused data transformer),
enables true parallelisation (no inter-authority contention), and makes the pipeline maintainable
(generic stages can be reasoned about independently).

Tasks:

#### 4a. Extraction Shim and Staged Writer Framework

- [ ] Add `write_staged_place_doc(namespace: str, doc: dict)` to `processing/helpers.py`:
  - Accepts standardised place documents (same schema as ES `places` index, minus IDs).
  - Writes to `{STAGED_BASE_DIR}/{namespace}/extract/places.jsonl` (append mode).
  - Periodically batches to Parquet for efficiency (or writes both JSONL + Parquet).
  - **Does NOT touch Elasticsearch**.
- [ ] Add registry/factory to `processing/settings.py` mapping authority namespace → extraction script path.
- [ ] Modify `ingest_all_authorities.py`:
  - When `run_manifest_path` is set (which indicates staged mode), set `WHG_STAGING_MODE=1` before calling authority scripts.
  - Remove `check_elasticsearch()` guard when staged mode is enabled.
  - Authority scripts check `WHG_STAGING_MODE` env var to decide whether to call `write_staged_place_doc()` or `helpers.bulk(es, …)`.

#### 4b. Authority Script Canaries (Batch 4 Proof-of-Concept)

Refactor three small authorities to the staged extraction pattern:

- [x] **`authorities/nativeland-places.py`** (nl)
  - Extract territory/language/treaty geometry + metadata.
  - Call `write_staged_place_doc(namespace="nl", doc)` instead of `helpers.bulk()`.
  - Geometry-store writes remain (already staged, separate).
  - ~1 min expected runtime.

- [x] **`authorities/periodo-places.py`** (po)
  - Extract period geometries from Periodo API.
  - Call `write_staged_place_doc(namespace="po", doc)` instead of `helpers.bulk()`.
  - ~1-5 min expected runtime (API dependent).

- [ ] **`authorities/cliopatria-places.py`** (clio, if exists; else skip to `pl`)
  - Extract Cliopatria points/geometries.
  - Call `write_staged_place_doc(namespace="clio", doc)` instead of `helpers.bulk()`.

Validation gates:
- [ ] Each canary script runs without ES, writes `{namespace}/extract/places.jsonl`.
- [ ] Staged output is valid JSONL, row count matches expected (or is logged).
- [ ] No ES indexing calls occur in staged mode.

#### 4c. Remaining Authority Scripts (Phased Refactor)

**Phase 1 (High Priority):**
- [x] `authorities/geonames-places.py` (gn) — large, but straightforward SQL iteration.
- [x] `authorities/wikidata-places.py` (wd) — large, but straightforward JSON streaming.
- [x] `authorities/osm-places.py` (osm) — very large, heavy lifting; plan for parallelisation.
- [x] `authorities/ohm-places.py` (ohm) — large, similar to osm.

**Phase 2 (Medium Priority):**
- [ ] `authorities/tgn-places.py` (tgn), `authorities/pleiades-places.py` (pl).
- [ ] `authorities/gb1900-places.py` (gb), `authorities/indexvillaris-places.py` (iv).

**Phase 3 (Lower Priority, Update Scripts):**
- [ ] `authorities/geonames-toponyms.py` (update; produces auxiliary toponym records).
- [ ] `authorities/wikidata-geoshapes.py` (update; enriches existing places, not new extraction).
- [ ] `authorities/loc-relations.py` (relations-only, no place extraction).

**Phase 4 (WHG Datasets):**
- [ ] `authorities/whg-places.py` (new) — once Django endpoints are stable.

Each refactor follows the same pattern:
1. Replace `helpers.bulk(es, batch)` calls with `write_staged_place_doc(ns, doc)`.
2. Keep geometry-store writes as-is (already staged).
3. Remove ES client instantiation / connection logic (not needed in staged mode).
4. Add `if os.environ.get("WHG_STAGING_MODE")` guard so old ES-direct mode still works for backward compatibility.

#### 4d. Staged Snapshot Consolidation

- [ ] Implement `_consolidate_extracts()` in `processing/stage_writers.py`:
  - After all authority scripts complete (per Batch 3 manifest), merge fragmented JSONL writes into consolidated Parquet per namespace.
  - This is a lightweight IO-only step (no reprocessing).
  - Happens automatically after the orchestrator marks a namespace's extract stage complete.

Validation gates:
- [ ] Staged snapshots for all selected authorities are complete and valid Parquet.
- [ ] Row counts and key uniqueness verified per namespace.
- [ ] No ES access required during extraction or consolidation.

### Batch 5: Per-Authority Patch-Collapse and Update Transforms

Targets:
- (new) `processing/h3_merge.py` (merge H3 patches into staged snapshot)
- (new) `processing/ccode_merge.py` (merge ccode patches into staged snapshot)
- `processing/namespace_materialize.py` (already exists; finalize manifests)
- `processing/ingest_all_authorities.py`

Dependencies: Batch 4 (extract finished), Batch 6 (H3 patches available for merge), Batch 7 (ccodes patches available).

Tasks:
- [ ] Implement `h3_merge.py`:
  - Reads staged extract snapshot + H3 patch JSONL for a namespace.
  - Merges H3 fields into each place document's geometries.
  - Writes final snapshot with H3 enrichment.
- [ ] Implement `ccode_merge.py`:
  - Reads staged snapshot (post-H3) + ccode patch JSONL for a namespace.
  - Merges ccodes into each place document.
  - Writes final snapshot with ccodes enrichment.
- [ ] Define patch merge semantics (e.g., ccode_merge treats patch entries as authoritative; overwrites existing ccodes).
- [ ] Idempotency: merging the same patches twice produces identical output.
- [ ] Ensure patch files reference the correct geometry indices.

Validation gates:
- [ ] Patch merge is idempotent and deterministic.
- [ ] Output row counts and key integrity pass.
- [ ] Enriched snapshots have h3_centroid/h3_cover + ccodes as expected.

### Batch 6: Per-Authority H3 Derivation (Slurm Array Job)

Targets:
- `processing/helpers.py`
- `processing/h3_stage.py`
- (new) `processing/submit_h3_slurm.py`
- `processing/h3_merge.py` (called from Batch 5, but H3 stage produces outputs)

Dependencies: Batch 4 (extract complete), Batch 3 (orchestrator).

> **Status**: `h3_stage.py` is implemented. `submit_h3_slurm.py` is new. H3 is deferred by default
> (`--inline-h3` to opt in during extraction, not recommended).
>
> **Architecture**: After extract stage, per-namespace H3 derivation runs as aseparate Slurm array job
> (one task per namespace). Each task reads the staged extract snapshot + full geometries, computes
> `h3_centroid` and `h3_cover` for each geometry,
> and writes H3 patches (`{namespace}/h3/places.h3.jsonl`). Batch 5 merges these patches into
> the final staged snapshot.

Tasks:
- [x] Implement `h3_stage.py` — reads staged extract artefacts + full geometry, writes H3 patch JSONL.
- [x] Compute `h3_centroid` and `h3_cover` from full geometry only (never from hull or repr_point).
- [x] Implement `submit_h3_slurm.py` — reads pending namespaces from manifest, estimates wall times
  from persistent runtime history, submits Slurm array job with per-namespace QOS selection.
- [x] H3 wall times recorded to `namespace-runtime-history.json` for future job size estimation.
- [x] Per-namespace H3 stage status tracked in run manifest.
- [ ] Benchmark wall times for large namespaces (`osm`, `ohm`, `gn`) to tune Slurm QOS defaults.

Validation gates:
- [x] H3 patches write without ES access.
- [x] H3 fields (h3_centroid, h3_cover) correctly materialise within geometry objects.
- [x] Multi-geometry places have h3 data in each geometry.
- [ ] No hull-derived coverages (only full-geometry H3).

### Batch 7: Per-Authority CCode Enrichment (Post-H3, Using UN Coverage Pre-Filter)

Targets:
- (new) `processing/ccode_enrichment.py`
- (new) `processing/ccode_merge.py` (called from Batch 5, but ccode stage produces outputs)
- `processing/ingest_all_authorities.py`

Dependencies: Batch 6 (H3 complete for all authorities, especially `un`).

**Important:** UN authority must have completed extract + H3 stages before other authorities can
use its H3 coverage for ccode pre-filtering. This can be satisfied by a prior run (staged artefacts
persistent) or by explicitly ordering UN early in a new run.

> **Architecture**: After H3 stage, per-namespace ccode enrichment runs (can be per-namespace Slurm job
> or batched). Each namespace's ccode enrichment reads:
> - The namespace's H3-enriched snapshot (output of Batch 5 h3_merge, or interim snapshot pre-merge).
> - UN H3 coverage (if namespace ≠ un; for ccode pre-filtering).
> - UN place geometries (for containment testing).
>
> For each place geometry's H3 cover, test containment against UN geometries, emit ccodes patch.
> Batch 5 merges ccode patches into the final snapshot.

Tasks:
- [ ] Implement `ccode_enrichment.py`:
  - Load UN staged records with H3 coverage into memory.
  - For each namespace ≠ un, iterate through H3-enriched staged snapshot.
  - For each place geometry's `h3_cover`, use H3 intersection with UN coverage to pre-filter candidate ccodes.
  - Implement point-in-polygon (centroid test) for point geometries.
  - Implement polygon intersection / majority-overlap for areal geometries.
  - Emit ccode patch records (`{namespace}/ccode/places.ccode.jsonl`).
- [ ] Implement `ccode_merge.py`:
  - Reads staged snapshot + ccode patches, merges ccodes into place docs.
- [ ] Add per-namespace ccode stage status to run manifest.

Validation gates:
- [ ] Unit checks for point-in-polygon and overlap tie-break behavior.
- [ ] Throughput benchmarks confirm pre-filtering efficiency.
- [ ] Ccode coverage statistics (places per country) match expectations.
- [ ] Ccode patches merge correctly without corrupting geom indices.

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
