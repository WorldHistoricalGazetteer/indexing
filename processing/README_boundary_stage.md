# Boundary Stage (Staged Pipeline)

This document describes the staged (non-ES) boundary completion flow for `osm` and `ohm`.

## Why this stage exists

`osm`/`ohm` relation geometries often need a second pass (`with_areas`) to assemble full
multipolygon boundaries. In the staged pipeline, this must happen **before** H3 and ccode
post-processing and **without Elasticsearch**.

## Stages added

1. `boundary` (`processing.boundary_stage`)
   - Reads source PBF (`osm` or `ohm`)
   - Assembles relation geometry
   - Emits staged patch file:
     - `{STAGED_BASE_DIR}/{namespace}/boundary/places.boundary.jsonl`

2. `boundary_merge` (`processing.boundary_merge`)
   - Reads staged extract snapshot:
     - `{STAGED_BASE_DIR}/{namespace}/extract/places.parquet|places.jsonl`
   - Applies boundary patches to matching `place_id`
   - Writes merged snapshot:
     - `{STAGED_BASE_DIR}/{namespace}/boundary_merged/places.parquet`
     - `{STAGED_BASE_DIR}/{namespace}/boundary_merged/places.jsonl`

## Important behavior

- No Elasticsearch I/O in either stage.
- H3 fields are stripped from boundary patches; H3 is deferred to Batch 6.
- Merge is update-only by default (unmatched patches are counted, not inserted).
- Both stages write stage/runtime events and can update run manifest stage status
  when `--run-id` maps to an existing manifest.

## CLI usage

```bash
python3 -m processing.boundary_stage --run-id <RUN_ID> --namespace osm
python3 -m processing.boundary_merge --run-id <RUN_ID> --namespace osm
```

Override PBF path and manifest path when needed:

```bash
python3 -m processing.boundary_stage \
  --run-id <RUN_ID> \
  --namespace ohm \
  --pbf-file /path/to/ohm.osm.pbf \
  --manifest-path /vast/ishi/staged/runs/<RUN_ID>.json
```

## Minimal verification harness

```bash
python3 testing/test_boundary_merge_harness.py
```

This creates a synthetic staged namespace, applies a patch, runs boundary merge,
and checks merged output.

