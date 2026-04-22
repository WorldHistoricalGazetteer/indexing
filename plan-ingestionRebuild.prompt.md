# Plan: WHG Authority File Ingestion Pipeline Rebuild

## Goal

Build a decoupled, restartable ingestion pipeline where each namespace is fully
materialized as staged artefacts before Elasticsearch indexing. This removes the
current ES wall-time bottleneck and enables reliable authority-set updates via
selection-driven runs without full corpus rebuilds.

## Objectives

- Use staged per-namespace place docs as canonical input for H3, tiles,
  toponyms/Symphonym, and indexing.
- Use Parquet as the primary staged format.
- Keep full geometries externalized in geometry blobs (JSON-indexed WKB store),
  with staged records carrying references and derived geometry fields.
- Keep JSONL as optional debug/export only (non-canonical).
- Compute H3 from full geometry only (no hull approximation).
- Expand OSM and OHM type extraction to include identified additional keys,
  with namespace-specific exceptions only where coverage is not meaningful.
- Use a root-level, checkbox-based authority selection file as the sole control
  for which authorities are included in a run (canonical file:
  `authority-selection.md`).
- Include production WHG Django datasets as first-class authorities under
  `whg:` with dataset-scoped sub-namespaces.

## WHG Django Datasets as Authorities

- In addition to existing authority sources, ingest eligible WHG Django datasets
  as separate authority units under namespace `whg:`.
- Use Django dataset integer IDs as sub-namespace discriminators, discovered via
  `GET /reconcile/authority-datasets`.
- Fetch each selected dataset LPF via
  `GET /entity/dataset:<id>/api?filetype=lpf` (gzip stream).
- Place IDs must remain globally unique and encode dataset provenance (for
  example `whg:{dataset_id}:{entity_id}` or equivalent canonical form).
- Dataset eligibility is controlled by Django `Dataset.authority`.
- Discovery and LPF fetch support authenticated access via token/session auth
  (token query parameter or bearer token).
- WHG authorities are configured remotely per dataset, but local inclusion is
  additionally gated by a group-level WHG checkbox in the local selection file.
- The local selection file should be bootstrapped with checked entries for all
  current authorities, including currently discovered WHG datasets.
- The ingestion pipeline must call the discovery endpoint during source refresh,
  then ingest each returned dataset independently with per-dataset manifests.

## Explicit Non-Goals

- No ad hoc authority deletion workflow outside selection-file control.
- No tile generation from ES documents.
- No pre-index clustering redesign in this plan.

## Target Architecture

### Canonical staged artefacts

- `staged/{namespace}/places/*.parquet`: canonical, enriched place records.
- `staged/{namespace}/geometry/*`: external geometry blobs and lookup index.
- `staged/{namespace}/h3/*.parquet`: derived H3 outputs keyed for efficient
  lookup during final snapshot materialization.
- `staged/{namespace}/manifest.json`: lineage, counts, checksums, stage status,
  and dependency metadata.
- Optional `staged/{namespace}/debug/*.jsonl` exports.

### Geometry representation

- Staged place records include `geometries` nested array, one entry per place geometry.
- Each geometry carries derived fields: `repr_point`, `hull`, `bounds`, `has_geom`,
  `h3_centroid`, `h3_cover`.
- Full geometries remain in external blobs to avoid Parquet inflation and to
  preserve efficient random access.

## Execution Model

- The pipeline is divided into three execution domains:
  1. **Global preflight** — determine the selected authority set, prepare shared
     prerequisites, and establish manifests/checkpoints.
  2. **Per-authority preprocessing** — run authority-local extraction and
     enrichment up to staged-place completion. This phase is intended to be
     parallelizable across authorities via separate Slurm jobs.
  3. **Global post-barrier processing** — run corpus-wide phases that require the
     union of all selected authorities, then index and cluster.
- Toponym deduplication and Symphonym embedding generation are **global corpus
  phases**, not per-authority phases, and must not start until all selected
  authorities have completed preprocessing through staged-place/H3/ccode output.
- Staging ES is not required for global preflight or per-authority preprocessing;
  it is first required at the indexing phase.

## Pipeline Stages (Global + Per-Authority + Global)

1. **Global preflight / source refresh**
   - Refresh authority source files by default when older than 30 days.
   - Read authority selection from root-level `authority-selection.md` and treat
     it as the authoritative inclusion list.
   - Determine the full selected authority set for the run and record it in a
     run-level manifest.
   - Record source version/timestamp in manifest.
   - Query `GET /reconcile/authority-datasets` and stage each returned dataset
     ID as a distinct `whg` authority input stream.
   - Refresh the WHG dataset subsection of `authority-selection.md` with newly
     discovered datasets checked by default.
   - Apply local WHG group include/exclude gate to discovered WHG datasets.
   - Detect deselected authorities with existing staged artefacts and remove
     those staged artefacts before preprocessing begins.
   - Source files remain cached even when an authority is deselected.

2. **Per-authority extract + normalize**
   - Parse authority source into normalized place docs.
   - Run geometry correction/validation and representative geometry derivation.
   - Write initial staged Parquet + geometry blobs.
   - This phase may run in parallel across authorities, with one Slurm job per
     authority (or per large authority shard) when storage layout is namespace-scoped.

3. **Per-authority patch-collapse materialization**
   - Apply any update-style transforms (e.g., geoshapes, relations, auxiliary
     toponym updates) to produce one final staged namespace snapshot.
   - Intermediate patch artefacts need not be retained beyond checkpoints.

4. **Per-authority H3 derivation (separate Slurm array job)**
   - Read staged records and external full geometries.
   - Compute `h3_centroid` and `h3_cover` from full geometry only.
   - For non-polygons use centroid-focused logic; for polygons compute cover.
   - For multi-geometry places, write `h3_centroid` and `h3_cover` into each
     corresponding `geometries[]` item.
   - Write derived H3 datasets back to staged storage (Parquet/lookup tables)
     and materialize into the final enriched staged snapshot.

5. **Per-authority ccode enrichment (patch stage, post-H3)**
   - Prerequisites: the `un` authority must already have completed H3 derivation.
   - Use UN polygon H3 coverage as efficient pre-filter for spatial containment.
   - For each place, check H3 coverage intersection with UN to narrow candidate ccodes.
   - Assign ccodes using:
     - point containment for point geometries,
     - polygon intersection / majority overlap for areal geometries,
     - all intersecting ISO codes when applicable.
   - Emit ccode patch records keyed by place_id for merge into enriched snapshot.
   - Non-`un` authorities may run this phase in parallel once the `un` H3 outputs
     are available.

6. **Global barrier: authority preprocessing complete**
   - Wait until every selected authority has completed stages 2–5.
   - Validate manifests/checkpoints so the corpus-wide phases run against a
     complete staged authority set.

7. **Global toponyms + Symphonym (staging and embedding, not indexing)**
   - Extract unique toponyms with attestations from the union of all selected
     staged authority snapshots.
   - Deduplicate toponyms corpus-wide across authorities.
   - Compute Symphonym embeddings (GPU-enabled) over the deduplicated corpus.
   - Use a persistent Symphonym cache so only changed toponyms are re-embedded.
   - Perform a Symphonym model/version check before run; clear/reset cache when
     model version changes.
   - Deprecated toponyms may remain in cache but are excluded from indexed output
     if absent from the current staged corpus.
   - Stage toponym records and embeddings; indexing deferred to stage 9.
   - **Important:** No ES indexing occurs in this stage; only staged outputs.

8. **Tiles generation**
   - Build from staged artefacts and external geometry store only.
   - Per-authority tile outputs may run after that authority completes stage 5;
     mixed-source outputs (notably OSM/OHM products) must wait for all relevant
     contributing authorities.
   - Required outputs:
     - `po.mbtiles`
     - `clio.mbtiles`
     - `nl.mbtiles`
     - `osm_admin.mbtiles`
     - `ohm_admin.mbtiles`
     - `osm_misc.mbtiles` (mixed OSM/OHM miscellaneous boundary types)
   - Keep synthetic boundary products folded into OSM tileset.

9. **Global indexing**
   - Index `places` from final enriched staged snapshot.
   - Index/update `toponyms` from staged toponym + Symphonym outputs.
   - **Prerequisite:** Staging ES instance must be running.

10. **Post-index clustering**
   - Run clustering after indexing as an ES-backed job (current architecture).
   - This plan defines handoff; clustering redesign is out of scope.

## Authority Selection Lifecycle

- Authority inclusion/exclusion is controlled only by the root-level checkbox
  selection file.
- At run start, the controller removes staged artefacts for deselected
  authorities so they are not pulled into subsequent global phases.
- No separate ad hoc authority-removal mechanism is used in this pipeline.
- Authority source files remain cached regardless of selection state.
- Indexing and clustering operate only on the selected-authority staged corpus.

## Type Mapping Dependency

- Add a global preflight step that verifies production ES availability for the
  `types` index (`schemas/types.json` schema).
- During ingestion, map encountered source place types to AAT via direct
  lookups in production ES `types` index fields (e.g. `gn_fcodes`, `wd_qids`,
  `osm_tags`, `ohm_tags`).
- Store both original source type and mapped AAT path string in staged place
  documents.
- Type-mapping availability checks are early preflight gates.

## Orchestration and Resilience Requirements

- Slurm orchestration must support stage-level resume and idempotent reruns.
- Per-authority preprocessing should support fan-out scheduling (separate Slurm
  jobs per authority) followed by an explicit global barrier before corpus-wide
  toponym/Symphonym work.
- Every stage writes checkpoint state and updates namespace manifest.
- Manifests include: source versions, row/document counts, geometry stats,
  H3 completion metrics, ccode coverage metrics, and output artefact paths.
- Pipeline design must avoid dependence on a single 48-hour staging ES window.

## Parallelisation Constraints and Resource Contention

- Parallel per-authority preprocessing is workable provided staged artefacts are
  written to namespace-scoped paths and patch outputs are namespaced.
- Shared read-only resources are low-risk: production `types` index lookups,
  source files, and `un` H3 outputs once written.
- The main contention risks are:
  - VAST / staged filesystem bandwidth and metadata pressure,
  - external API rate limits for WHG/remote source fetches,
  - geometry-store consolidation if multiple jobs try to finalize shared shards,
  - mixed-source tile products that depend on multiple authorities,
  - the `un` dependency for post-H3 ccode enrichment.
- Pre-index stages do not require staging ES, so ES contention is not a blocker
  for per-authority preprocessing parallelism.

## Performance Focus

- Primary optimization target: OSM/OHM throughput.
- Use Slurm arrays for heavy geometry/H3 workloads.
- Keep Parquet partitioning and geometry lookup layout tuned for fast merge and
  index preparation.
- Use GPU where it materially improves throughput (at minimum Symphonym).

## Acceptance Criteria

- Per-namespace staged snapshot is complete before indexing.
- H3 is derived from full geometry only, with per-geometry coverage support.
- Toponym deduplication and Symphonym generation run only after all selected
  authorities complete per-authority preprocessing.
- Authority inclusion/exclusion is driven exclusively by the checkbox selection
  file, with deselected staged artefacts removed before preprocessing.
- Indexed outputs reflect only selected authorities from the current staged run.
- Required mbtiles products are generated from staged artefacts, not ES docs.
- Symphonym embeddings are generated and used to populate `toponyms`.
- Symphonym caching re-embeds only changed toponyms, with cache invalidation on
  model/version changes.
- Clustering runs successfully as a post-indexing ES job.
- Enabled Django datasets are ingested under `whg:` using dataset-ID
  sub-namespaces, sourced from `GET /reconcile/authority-datasets` and LPF
  exports from `GET /entity/dataset:<id>/api?filetype=lpf`.

## Coding-Agent Handoff Tasks

1. Define staged schemas and manifest contracts (Parquet + geometry + H3).
2. Implement stage orchestration with checkpoints/resume semantics.
3. Implement namespace extract/normalize and patch-collapse materialization.
4. Implement Slurm array H3 stage and final snapshot materialization.
5. Implement post-H3 ccode enrichment using UN H3 coverage as pre-filter.
6. Implement selection-driven toponym/index updates based on the selected
   authority corpus.
7. Refactor tiles generation to consume staged geometry artefacts only.
8. Integrate Symphonym/toponym staged flow into incremental indexing.
9. Integrate production ES `types` index lookups into early preflight and
   ingestion-time AAT mapping.
10. Add validation/reporting gates tied to manifest and acceptance criteria.
11. Integrate WHG dataset discovery and LPF ingestion flow using the published
    Django contract:
    - discovery endpoint: `GET /reconcile/authority-datasets`,
    - LPF export endpoint: `GET /entity/dataset:<id>/api?filetype=lpf`,
    - per-dataset `whg` staged snapshots and manifests.
12. Implement root-level checkbox authority selection ingestion controls,
    including stale staged-artefact cleanup for deselected authorities.
13. Add Symphonym incremental-cache + model-version invalidation workflow.
