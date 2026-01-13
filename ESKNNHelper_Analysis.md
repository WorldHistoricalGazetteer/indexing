# ESKNNHelper Class Analysis

## Purpose

`ESKNNHelper` is a helper class that interfaces with Elasticsearch for phonetic similarity operations using 192-dimensional PanPhon embeddings. It provides:

1. **Embedding retrieval** from ES (single and batch)
2. **Local clustering** using HDBSCAN for positive pair generation
3. **ES KNN search** for hard negative mining in Phase 3

## Key Features

### 1. LRU-Bounded Embedding Cache (MAX_CACHE_SIZE = 100,000)
- Manually-implemented LRU cache using a dict + insertion-order list
- Caches retrieved embeddings in memory
- ~77MB memory footprint for full cache
- Evicts oldest entries when full

### 2. Exponential Backoff Retry
- Wraps all ES operations with retry logic
- Prevents failures from transient ES overload

### 3. Failure Rate Tracking
- Monitors request success/failure ratio
- Aborts if failure rate exceeds threshold (prevents data quality issues)

### 4. Batched Operations
- `batch_get_embeddings()`: Uses ES `mget` for bulk retrieval
- `find_hard_negatives_batch()`: Uses ES `_msearch` for parallel KNN queries

---

## When ES KNN is Used vs Local Computation

| Operation | Method | Why |
|-----------|--------|-----|
| **Positive pair clustering** | Local HDBSCAN | Better clustering quality without arbitrary threshold; places have <50 toponyms so local computation is fast |
| **Hard negative mining** | ES KNN | Need to search entire corpus for similar-but-different toponyms; ES KNN is optimized for this |
| **Embedding retrieval** | ES get/mget | Embeddings are stored in ES |

---

## Core Method: `find_similar_in_place()`

This method clusters toponyms within a place using **HDBSCAN density-based clustering**. Unlike threshold-based approaches, HDBSCAN automatically determines the number of clusters based on local density structure of the PanPhon embeddings.

**Note:** This method fetches embeddings from ES but performs clustering locally in Python. This is more efficient than using ES KNN because:
- Places have ≤50 toponyms (capped)
- HDBSCAN on 50 points is trivial (~1ms)
- Eliminates arbitrary similarity threshold
- Reduces ES query load

### Algorithm

```
1. INPUT: place_id + list of toponym_ids belonging to that place

2. GET EMBEDDINGS: Fetch 192-dim PanPhon embeddings for each toponym from ES

3. EDGE CASES:
   - 0 toponyms: return []
   - 1 toponym: return [[tid]]
   - 2 toponyms: use simple cosine similarity check (HDBSCAN needs ≥3)

4. CLUSTER with HDBSCAN:
   - metric='cosine' (operates in phonetic similarity space)
   - min_cluster_size=2 (need at least 2 for a "real" cluster)
   - min_samples=1 (allow small tight clusters)
   - allow_single_cluster=True (critical: allows all points in one cluster)

5. OUTPUT: List of clusters, where each cluster is a list of toponym_ids
   - Noise points (label=-1) become singleton clusters
```

### Key Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `min_cluster_size` | 2 | Minimum points to form a cluster |
| `min_samples` | 2 | Provides denoising; prevents weak links from merging distinct clusters |
| `metric` | 'precomputed' | Uses precomputed cosine distance matrix for phonetic similarity |
| `allow_single_cluster` | True | Returns all points in one cluster if naturally similar |
| `cluster_selection_epsilon` | 0.2 | Merge clusters within cosine distance 0.2 (similarity ≥ 0.8). Creates larger, more meaningful clusters. |

---

## Example: Place with Three Distinct Phonetic Clusters

Let's consider a place like **Köln/Cologne** which might have these toponyms in different languages:

| Toponym ID | Name | Script | Language | Phonetic Group |
|------------|------|--------|----------|----------------|
| t1 | Köln | LATIN | de | German cluster |
| t2 | Keulen | LATIN | nl | German cluster |
| t3 | Colonia | LATIN | la | Latin cluster |
| t4 | Cologne | LATIN | fr | French cluster |
| t5 | Colònia | LATIN | ca | Latin cluster |
| t6 | Kolonia | LATIN | pl | Latin cluster |
| t7 | Köln | LATIN | en | German cluster |

### Step-by-Step Execution

#### Step 1: Get Embeddings from ES

```python
embeddings = {
    't1': [0.12, -0.05, ...],  # "Köln" phonetics
    't2': [0.11, -0.04, ...],  # "Keulen" (similar to Köln)
    't3': [0.82, 0.31, ...],   # "Colonia" phonetics
    't4': [0.65, 0.45, ...],   # "Cologne" phonetics  
    't5': [0.80, 0.33, ...],   # "Colònia" (similar to Colonia)
    't6': [0.79, 0.29, ...],   # "Kolonia" (similar to Colonia)
    't7': [0.13, -0.06, ...],  # "Köln" (same as t1)
}
```

#### Step 2: HDBSCAN Clustering

HDBSCAN analyzes the density structure of these 7 points in 192-dimensional space:
- t1, t2, t7 form a dense region (German phonetics)
- t3, t5, t6 form another dense region (Latin phonetics)
- t4 is in a sparse area (French phonetics, isolated)

```python
clusterer = hdbscan.HDBSCAN(
    min_cluster_size=2,
    min_samples=1,
    metric='cosine',
    allow_single_cluster=True
)
labels = clusterer.fit_predict(vectors)
# labels = [0, 0, 1, -1, 1, 1, 0]
#           t1 t2 t3 t4  t5 t6 t7
#           German Latin noise Latin German
```

#### Step 3: Group by Label

```python
clusters_dict = {
    0: ['t1', 't2', 't7'],  # German cluster
    1: ['t3', 't5', 't6'],  # Latin cluster
}
noise_points = ['t4']  # "Cologne" is noise (low density)
```

#### Step 4: Output Clusters

```python
clusters = [
    ['t1', 't2', 't7'],  # German cluster: Köln, Keulen, Köln
    ['t3', 't5', 't6'],  # Latin cluster: Colonia, Colònia, Kolonia
    ['t4'],              # Singleton: Cologne (noise point)
]
```

---

## Positive Pairs Generated

From these 3 clusters, the training data generator creates positive pairs:

**German cluster (3 toponyms → 3 pairs):**
- (t1, t2): Köln ↔ Keulen
- (t1, t7): Köln ↔ Köln
- (t2, t7): Keulen ↔ Köln

**Latin cluster (3 toponyms → 3 pairs):**
- (t3, t5): Colonia ↔ Colònia
- (t3, t6): Colonia ↔ Kolonia
- (t5, t6): Colònia ↔ Kolonia

**Singleton cluster (1 toponym → 0 pairs):**
- No pairs (need at least 2 to form a pair)

**Total: 6 positive pairs from this place**

---

## Key Design Decisions

1. **HDBSCAN vs Threshold**: Density-based clustering finds natural phonetic groupings without arbitrary similarity cutoffs

2. **`allow_single_cluster=True`**: Handles the common case where all toponyms in a place are phonetically similar (e.g., minor spelling variants)

3. **Noise as Singletons**: Isolated toponyms (like "Cologne" which doesn't match German or Latin phonetics) become their own cluster rather than being forced into an inappropriate group

4. **Cosine Metric**: HDBSCAN operates directly on cosine similarity in embedding space, matching how PanPhon embeddings are designed to be compared

5. **Edge Case Handling**: 
   - n=0: empty result
   - n=1: single-element cluster
   - n=2: falls back to simple cosine threshold (HDBSCAN needs ≥3 points)
   - n≥3: full HDBSCAN clustering

---

## Stochastic Oversampling

When bins are under-represented and need oversampling, the system implements **stochastic oversampling** rather than simple duplication:

### Phase 1 (Random Negatives)
- Each copy of an oversampled (anchor, positive) pair gets a **different negative**
- Achieved by including `triplet_idx` in the RNG seed
- Prevents the model from memorising identical triplets

### Phase 3 (Hard Negatives)
- Each copy queries ES KNN for the same candidates (same anchor embedding)
- But selects **randomly** from valid candidates using `sample_idx`
- Provides variety even when ES returns the same candidate list

**Benefits:**
- Same class balance as simple oversampling
- Better generalisation (no identical triplets)
- Preserves reproducibility (deterministic seeds)


