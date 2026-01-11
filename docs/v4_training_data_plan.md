# Symphonym v4 Training Data Pipeline

## Overview

This document outlines the v4 training data pipeline, designed to address the 90% triplet dropout problem identified in v3 and implement feature-aware, phonetically-grounded pair/triplet generation.

## Key Improvements over v3

1. **Early feature filtering**: Generate pairs only from toponyms with valid PanPhon embeddings
2. **Phonetic clustering**: Use PanPhon cosine similarity instead of string similarity thresholds
3. **Script+language stratification**: Balance by (script, language) pairs, not just script pairs
4. **Phonetic hard negatives**: Phase 3 negatives selected by PanPhon similarity, not orthographic prefix
5. **Unified bin-balancing**: Consistent sampling strategy across all phases
6. **Pre-computed training data**: All data generated before training begins (except Phase 2 Teacher embeddings)

## Pipeline Stages

### Stage 1: Extraction (ES places → SQLite → ES toponyms)

**Script**: `phonetics/extraction/rebuild_toponyms_index.py`

**Steps**:
1. Scan ES `places` index
2. Extract toponyms to SQLite with deduplication
3. For toponyms in training namespaces (gn, wd, tgn):
   - Compute IPA via Epitran (where supported)
   - Compute PanPhon features from IPA
   - Store as 24-dimensional articulatory feature vector
4. Second pass: populate attestations via ES search
5. Index to ES `toponyms` with schema including:
   - `panphon_embedding`: dense_vector(24) for clustering/similarity
   - `epitran_supported`: boolean for filtering
   - `prefix_2`, `prefix_3`: for fallback queries
6. Output coverage analysis by script+language

**Slurm Config**:
- Memory: 300G
- CPUs: 16
- Time: 48h
- Scratch: Copy SQLite to local scratch during processing

### Stage 2: Coverage Analysis

**Script**: `phonetics/extraction/analyse_coverage.py` (new)

**Queries** (ES aggregations):
```json
{
  "aggs": {
    "by_script_lang": {
      "composite": {
        "sources": [
          {"script": {"terms": {"field": "script"}}},
          {"lang": {"terms": {"field": "lang"}}}
        ]
      },
      "aggs": {
        "total": {"value_count": {"field": "toponym_id"}},
        "with_panphon": {
          "filter": {"exists": {"field": "panphon_embedding"}}
        }
      }
    }
  }
}
```

**Output**:
- `coverage_by_script_lang.json`: Counts and percentages
- `bin_sizes.json`: For stratification planning
- Statistics for `.tex` document

### Stage 3: Pair Generation (Feature-aware, Phonetic Clustering)

**Script**: `phonetics/extraction/generate_pairs_v4.py` (new)

**Algorithm**:
```python
for place_id in places_with_multiple_toponyms:
    # Get all toponyms with PanPhon embeddings
    toponyms = query_es(
        filter=[
            {"term": {"attestations": place_id}},
            {"exists": {"field": "panphon_embedding"}}
        ]
    )
    
    if len(toponyms) < 2:
        continue
    
    # Cluster by PanPhon cosine similarity
    embeddings = [t['panphon_embedding'] for t in toponyms]
    clusters = cluster_by_cosine_similarity(embeddings, threshold=0.7)
    
    # Generate pairs within each cluster
    for cluster in clusters:
        for t1, t2 in combinations(cluster, 2):
            pair = create_pair(t1, t2)
            script_lang_key = f"{pair.script1}:{pair.lang1}|{pair.script2}:{pair.lang2}"
            add_to_bin(script_lang_key, pair)
```

**Bin Balancing**:
```python
def balance_bins(bins, target_per_bin=100_000, min_bin_size=1_000, max_oversample=5):
    """
    Balance script+language bins for training.
    
    Strategy:
    - Bins < min_bin_size: Drop (log warning)
    - Bins < target_per_bin: Oversample up to max_oversample×
    - Bins > target_per_bin: Cap at target
    """
    balanced = {}
    for key, samples in bins.items():
        if len(samples) < min_bin_size:
            logger.warning(f"Dropping {key}: only {len(samples)} samples")
            continue
        
        if len(samples) < target_per_bin:
            # Oversample
            oversample_factor = min(max_oversample, target_per_bin / len(samples))
            balanced[key] = oversample(samples, int(len(samples) * oversample_factor))
        else:
            # Cap
            balanced[key] = random.sample(samples, target_per_bin)
    
    return balanced
```

**Output**:
- `pairs/` directory with Parquet files
- `pair_stats.json` with bin sizes and balancing decisions

### Stage 4: Phase 1 Triplet Generation

**Script**: `phonetics/extraction/generate_triplets_v4.py` (new)

**Algorithm**:
- For each pair (anchor, positive):
  - Sample random negative from toponyms with PanPhon (via ES random_score)
  - Ensure negative not in same attestation set
- All triplets guaranteed to have valid PanPhon features

**Output**:
- `triplets/phase1/` with Parquet files including PanPhon features

### Stage 5: Phase 3 Hard Negative Generation

**Script**: Same as Stage 4, different mode

**Algorithm**:
```python
for anchor, positive in pairs:
    # Find phonetically similar but geographically distinct names
    hard_negatives = query_es(
        filter=[
            {"term": {"script": anchor.script}},
            {"term": {"lang": anchor.lang}},
            {"exists": {"field": "panphon_embedding"}}
        ],
        must_not=[
            {"terms": {"attestations": anchor.attestations}}
        ],
        script_score={
            "source": "cosineSimilarity(params.vec, 'panphon_embedding') + 1.0",
            "params": {"vec": anchor.panphon_embedding}
        },
        min_score=1.5  # cosine > 0.5
    )
    
    # Sample from top-k similar
    negative = random.choice(hard_negatives[:10])
    yield (anchor, positive, negative)
```

**Output**:
- `triplets/phase3/` with Parquet files

### Stage 6: Phase 2 Data Preparation

**Script**: `phonetics/extraction/prepare_phase2_data.py` (new)

**Algorithm**:
- Sample balanced subset from full toponyms corpus
- Stratify by script+language
- Include all toponyms with valid PanPhon (for Teacher input)
- Include all toponyms with valid char encoding (for Student input)

**Note**: Teacher embeddings added AFTER Phase 1 training completes.

### Stage 7: Training

**Script**: `phonetics/training/train.py` (modified)

**Changes**:
- Phase 1: Load pre-generated triplets with embedded PanPhon features (no dropout!)
- Phase 2: Load pre-generated samples, add Teacher embeddings on-the-fly
- Phase 3: Load pre-generated triplets with character encodings

## ES Schema Changes

### Extended `toponyms` Index Schema

```json
{
  "mappings": {
    "properties": {
      "toponym_id": {"type": "keyword"},
      "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
      "name_romanized": {"type": "keyword"},
      "lang": {"type": "keyword"},
      "script": {"type": "keyword"},
      "attestations": {"type": "keyword"},
      "namespaces": {"type": "keyword"},
      
      "epitran_supported": {"type": "boolean"},
      "epitran_code": {"type": "keyword"},
      "ipa": {"type": "keyword", "index": false},
      "panphon_embedding": {
        "type": "dense_vector",
        "dims": 24,
        "index": true,
        "similarity": "cosine"
      },
      
      "prefix_2": {"type": "keyword"},
      "prefix_3": {"type": "keyword"},
      
      "embedding": {
        "type": "dense_vector",
        "dims": 128,
        "index": true,
        "similarity": "cosine"
      },
      "embedding_version": {"type": "keyword"}
    }
  }
}
```

## Slurm Job Structure

```bash
# Stage 1: Extraction + ES indexing
es -rebuild-toponyms 4

# Stage 2-5: Pair/triplet generation (single job)
es -generate-training-data 4

# Stage 6: Training
es -train-model 4

# Stage 7: Embeddings + final index
es -update-embeddings 4
```

## Expected Outcomes

1. **No triplet dropout**: All Phase 1 triplets have valid PanPhon features
2. **Balanced training**: Script+language bins equally represented
3. **Better hard negatives**: Phonetically similar, not just orthographically similar
4. **Principled pair generation**: Clustering instead of arbitrary thresholds
5. **~10× more Phase 1 training data**: From 467K to ~4-5M triplets

## Migration Notes

- v3 SQLite schema compatible with v4 (additive changes only)
- ES `toponyms` schema requires reindexing for `panphon_embedding` field
- Training code requires updates to load new Parquet structure

