# Batch 4b Implementation Summary

**Date**: April 23, 2026  
**Focus**: Canary refactors of lightweight authority script extraction pattern

## What Was Accomplished

### 1. **Execution Plan Restructured** 
- Reorganized Batches 4–7 around a unified **Authority Preprocessing Pipeline** architecture
- Added pipeline diagram showing: extract → H3 → merge → ccode → merge → barrier
- Clarified that Batch 4 is the **primary batch**: refactor authority scripts to staged extractors
- Documented that Batches 5–7 are **post-processing stages** (merge + enrichment, not extraction)
- Key insight: all authority preprocessing is **ES-free**, per-namespace parallelizable

### 2. **Batch 4a: Staged Extraction Shim Infrastructure** (Previous commit)
- **`processing.helpers.write_staged_place_doc(namespace, doc)`** — appends place docs to staged JSONL without ES
- **`processing.helpers.is_staging_mode()`** — checks `WHG_STAGING_MODE` env var
- **`ingest_all_authorities.py` updates**:
  - Sets `WHG_STAGING_MODE=1` when staged mode detected (`--run-id` or `--resume-run`)
  - Skips ES checks (`check_elasticsearch()`, index counts, etc.) in staged mode
- This enables lightweight downstream refactors

### 3. **Batch 4b: Canary Authority Script Refactors** (This session)

#### **NativeLand (nl) — `authorities/nativeland-places.py`**
- Added staged extraction imports: `write_staged_place_doc`, `is_staging_mode`
- Removed H3 computation from `process_territory()`, `process_language()`, `process_treaty()` (H3 now belongs to Batch 6)
- Updated `index_nativeland_file()`:
  - Check `is_staging_mode()` at entry
  - Conditionally initialize ES client (skip if staged)
  - Call `write_staged_place_doc(namespace='nl', doc)` instead of `helpers.bulk(es, ...)`
  - Skip bulk operations in staged mode
  - Maintain backward compatibility: ES-direct path still works
- Skip checkpoint snapshot creation in staged mode

#### **Periodo (po) — `authorities/periodo-places.py`**
- Added staged extraction imports
- Removed H3 computation from `process_periodo_period()`
- Updated `index_periodo()`:
  - Check `is_staging_mode()` at entry
  - Conditionally initialize ES client
  - Call `write_staged_place_doc(namespace='po', doc)` instead of ES bulk
  - Maintain ES-direct fallback path
- Skip checkpoint snapshot in staged mode

**Pattern used in both**: 
```python
staged_mode = is_staging_mode()
if staged_mode:
    write_staged_place_doc(namespace='nl', doc=place_doc)
else:
    # ES-direct path (backward compatible)
    batch.append({'_index': places_index, '_id': ..., '_source': doc})
    if len(batch) >= BATCH_SIZE:
        helpers.bulk(es, batch, ...)
```

### 4. **Batch 4b Validation Test Script** (New)
- **`testing/batch-4b-validation.sh`**: end-to-end canary test
  - Activates conda env and staged run infrastructure
  - Extracts nl (NL) without ES
  - Extracts po (Periodo) without ES
  - Verifies JSONL outputs, row counts, manifest validity
  - Prints summary with next steps

## Test Procedure for Remote Validation

On CRC (`ssh crc0`):

```bash
cd /ix1/ishi/elastic
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
bash testing/batch-4b-validation.sh
```

Expected results:
- `nl/extract/places.jsonl` — ~4K documents
- `po/extract/places.jsonl` — ~5K documents
- No ES access required
- Run manifest JSON valid
- All tests pass (green checkmarks)

## Architecture Changes in Batch 4b

### **Before (ES-Direct)**
```python
# Authority script is a custom ES indexer
es = Elasticsearch(ES_HOST)
for record in source:
    place_doc = transform(record)
    batch.append({'_index': 'places', '_id': ..., '_source': place_doc})
    if len(batch) >= BATCH_SIZE:
        helpers.bulk(es, batch)
```

### **After (Staged Extractor)**
```python
# Authority script is a lightweight data transformer
if is_staging_mode():
    for record in source:
        place_doc = transform(record)
        write_staged_place_doc('nl', place_doc)  # JSONL append, no ES
else:
    # Fallback ES-direct path for backward compatibility
    es = Elasticsearch(ES_HOST)
    for record in source:
        place_doc = transform(record)
        batch.append({'_index': 'places', '_id': ..., '_source': place_doc})
        if len(batch) >= BATCH_SIZE:
            helpers.bulk(es, batch)
```

**Key Benefits**:
- Scripts are simpler (no ES client lifecycle)
- Staging is parallelizable (no ES contention)
- Staged outputs are reusable (H3, ccodes, merges, indexing all read from JSONL/Parquet)
- ES is only needed for indexing (Batch 11), not preprocessing

## Current Implementation Status

| Batch | Component | Status | Notes |
|-------|-----------|--------|-------|
| 1–3 | Foundation, Type Mapping, Orchestration | ✅ Complete | |
| 4a | Extraction Shim Infrastructure | ✅ Complete | `write_staged_place_doc`, env var support |
| 4b | Canary Refactors (nl, po) | ✅ Complete | Both scripts refactored, tested locally |
| 4c | Full Authority Refactors | 🔲 Ready | Roadmap: gn, wd, osm, ohm, tgn, pl, etc. |
| 5 | Merge Stages (h3_merge, ccode_merge) | 🔲 To implement | Post-H3/ccode patches |
| 6 | H3 Slurm Array | ✅ Complete | `submit_h3_slurm.py`, runtime history |
| 7 | CCode Enrichment | 🔲 Ready | Batch 6 H3 pre-filter design done |
| 8–14 | Barrier, Toponym/Symphonym, Tiles, Index, Clustering, etc. | 🔲 Pending | |

## Next Steps

### **Immediate (Batch 4b Validation)**
1. Run test script on CRC to validate canary refactors work without ES
2. Examine staged JSONL documents for correctness
3. Verify run manifest reflects both nl and po as complete

### **Batch 4c: Full Authority Refactors (Phase 1)**
1. Refactor large core authorities in parallel:
   - **gn** (GeoNames, ~13M) — straightforward SQL iteration
   - **wd** (Wikidata, ~11M) — streaming JSON refactor
   - **osm** (OpenStreetMap, ~18M) — large; may need internal sharding
   - **ohm** (OpenHistoricalMap, ~800K)
2. Use same pattern as nl/po
3. Benchmark batched staged writes for performance

### **Batch 5: Merge Stages**
1. Implement `h3_merge.py` — merge H3 patches into staged snapshots
2. Implement `ccode_merge.py` — merge ccode patches into snapshots
3. Define idempotent merge semantics and validation

### **Batch 6 Integration**
1. After Batch 4c complete, run H3 Slurm array (already implemented)
2. Feed H3 patches to Batch 5 merge stage

## Documentation & References

- **`developer/batch-4-refactor-guide.md`**: detailed before/after pattern, roadmap, testing procedure
- **`plan-ingestionRebuild.execution.md`**: full execution plan with all batches
- **`testing/batch-4b-validation.sh`**: automated canary test script

## Key Files Modified/Created

### Modified:
- `authorities/nativeland-places.py` — refactored to staged extraction
- `authorities/periodo-places.py` — refactored to staged extraction
- `processing/helpers.py` — added `write_staged_place_doc()`, `is_staging_mode()`
- `processing/ingest_all_authorities.py` — added staged mode detection and infrastructure skipping
- `plan-ingestionRebuild.execution.md` — restructured Batches 4–7 architecture

### Created:
- `testing/batch-4b-validation.sh` — automated test script
- `developer/batch-4-refactor-guide.md` — detailed refactor pattern and roadmap

## Commits in This Session

1. `5a50962` — Execution plan: restructure Batches 4–7 as unified pipeline
2. `8c090fe` — Batch 4a: staged extraction shim framework
3. `47036d9` — Developer guide: Batch 4 authority script refactor pattern
4. `f9469aa` — Batch 4b: canary refactors (nl, po)
5. `8e0487e` — Batch 4b: validation test script

All pushed to remote as of `8e0487e`.

