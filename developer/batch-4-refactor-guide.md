# Batch 4: Authority Script Refactor Guide

## Overview

Authority scripts are being refactored from **custom ES indexers** to **lightweight staged extractors**.

- **Old pattern**: authority script iterates through source data, builds place docs, calls `helpers.bulk(es, batch)` to index directly to ES.
- **New pattern**: authority script iterates through source data, builds place docs, calls `write_staged_place_doc(namespace, doc)` to append to a staged JSONL file.

**Advantages**:
- Authority scripts have no ES dependency (staged mode runs without ES)
- Scripts become simpler (focus on data transformation, not ES client lifecycle)
- Processing is parallelisable (no inter-authority contention on ES)
- Staged outputs are standardised and reusable downstream

## Refactor Pattern

### Infrastructure (Batch 4a, already complete)

- `processing.helpers.write_staged_place_doc(namespace: str, doc: dict)` — appends to `{STAGED_BASE_DIR}/{namespace}/extract/places.jsonl`
- `processing.helpers.is_staging_mode()` — checks `WHG_STAGING_MODE` env var
- `ingest_all_authorities.py` sets `WHG_STAGING_MODE=1` when `--run-id` or `--resume-run` is passed

### Canary 1: `authorities/nativeland-places.py` (nl)

**Source pattern** (existing, ES-direct):

```python
# Existing code around line 215-240 in index_nativeland_file():
places_batch = []
for place_doc in iterate_places(...):
    places_batch.append({'_index': places_index, '_id': place_doc['place_id'], '_source': place_doc})
    if len(places_batch) >= batch_size:
        success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
        print(f"  Indexed {success} / {success + failed}")
        places_batch = []

# Final batch
if places_batch:
    success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
```

**Refactored pattern** (staged extraction):

```python
from processing.helpers import write_staged_place_doc, is_staging_mode

# In index_nativeland_file():
staged_mode = is_staging_mode()
count = 0

for place_doc in iterate_places(...):
    if staged_mode:
        # Write to staged file (no ES)
        write_staged_place_doc(namespace="nl", doc=place_doc)
    else:
        # Fallback to ES-direct for backward compatibility
        places_batch.append({'_index': places_index, '_id': place_doc['place_id'], '_source': place_doc})
        if len(places_batch) >= batch_size:
            success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)
            print(f"  Indexed {success} / {success + failed}")
            places_batch = []
    count += 1

# Final batch (ES mode only)
if not staged_mode and places_batch:
    success, failed = helpers.bulk(es, places_batch, raise_on_error=False, stats_only=True)

print(f"  Total: {count} places processed")
```

**Key changes**:
1. Import `write_staged_place_doc` and `is_staging_mode` from `processing.helpers`
2. Check `is_staging_mode()` at entry
3. If staged: call `write_staged_place_doc(namespace, doc)` for each place
4. Else: use existing ES-direct logic (backward compatible)
5. Remove ES client instantiation from staged branches (or skip if `staged_mode`)

### Canary 2: `authorities/periodo-places.py` (po)

**Pattern**: Similar to NL. Locate all `helpers.bulk()` calls and wrap with `if staged_mode` checks.

Refactor steps:
1. Add imports at top level
2. Check `is_staging_mode()` inside entry point (e.g., `index_periodo()`)
3. Wrap each place doc emission in staged-vs-ES logic
4. Keep ES-direct path for fallback

---

## Full Refactor Roadmap (Batch 4c)

### Phase 1: Large Core Authorities (concurrent refactors)
- ✅ **nl** (Native Land) — 4K records, ~1 min
- ✅ **po** (Periodo) — ~5K records, ~5 min
- [ ] **gn** (GeoNames) — ~13M records; should use batched staged writes
- [ ] **wd** (Wikidata) — ~11M records; streaming refactor
- [ ] **osm** (OpenStreetMap) — ~18M records; may benefit from shard-level parallelisation
- [ ] **ohm** (OpenHistoricalMap) — ~800K records

### Phase 2: Medium Authorities
- [ ] TGN (~3M)
- [ ] Pleiades (~37K)
- [ ] GB1900 (~1.2M)
- [ ] Index Villaris (~24K)

### Phase 3: Auxiliary / Update Scripts
- [ ] `geonames-toponyms.py` (auxiliary toponym records)
- [ ] `wikidata-geoshapes.py` (enrichment, not extraction)
- [ ] `loc-relations.py` (relations-only)

### Phase 4: WHG Datasets
- [ ] `whg-places.py` (new extraction from Django endpoints)

---

## Testing Canaries (Batch 4b)

Once NL and PO are refactored:

```bash
# Test staged extraction (no ES running)
export STAGED_BASE_DIR=/vast/ishi/staged
run_id=$(python -c "from processing.staging_orchestrator import generate_run_id; print(generate_run_id())")

# Run with --run-id (triggers WHG_STAGING_MODE=1)
python -m processing.ingest_all_authorities \
    --run-id $run_id \
    --namespaces nl,po \
    --write-stage-snapshots

# Verify staged output
ls -lh /vast/ishi/staged/{nl,po}/extract/places.jsonl
```

Expected results:
- `nl/extract/places.jsonl` — ~4K lines, ~500 KB
- `po/extract/places.jsonl` — ~5K lines, ~500 KB
- No ES access required
- Staged snapshot manifest in `staged/runs/{run_id}.json`

---

## Merge and Pipeline

After extraction (Batches 4b, Batch 4c), the pipeline continues:

1. **Batch 4d**: Consolidate fragmented JSONL writes → Parquet per namespace
2. **Batch 6**: H3 derivation (Slurm array) → produces `{namespace}/h3/places.h3.jsonl` patches
3. **Batch 5**: H3 merge → enriches snapshot with h3_centroid / h3_cover
4. **Batch 7**: CCode enrichment (Slurm) → produces `{namespace}/ccode/places.ccode.jsonl` patches
5. **Batch 5**: CCode merge → enriches snapshot with ccodes
6. **Batch 8**: Barrier confirms all authorities complete
7. **Batch 9**: Global toponym/Symphonym (GPU Slurm)
8. **Batch 10**: Tile generation
9. **Batch 11**: Index load from staged snapshots
10. **Batch 12**: Clustering

All of Batches 5–12 read from staged files, **no direct ES access during staging**.

