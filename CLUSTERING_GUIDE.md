# WHG Place Clustering — How It Works and How to Run It

**Last updated:** 26 March 2026  
**Status:** Operational — second full run (with HDBSCAN-based calibration) complete  
**Repository:** `WorldHistoricalGazetteer/indexing`

> **V4 note (ArangoDB migration).** This guide describes the current Elasticsearch-based system. In the planned V4 architecture (§4.2), ArangoDB replaces Elasticsearch as the primary data store and pairwise links are stored as graph edges. Under V4:
>
> - **Phases 1–3** (discovering pairwise links) and **calibration** (§8) carry over largely unchanged — the link-discovery logic is independent of the storage backend.
> - **Phase 4** (batch cluster computation) and the **`clusters` index** become redundant. Cluster membership is resolved on-the-fly by graph traversal at query time, which also enables per-query confidence thresholds (e.g. "show me the cluster at ≥ 0.6" vs "≥ 0.8").
> - **Staging/snapshot infrastructure** (§4.1) and the **operational commands** in §6 are specific to the CRC Slurm + ES environment and will not apply to V4.
> - **Membership documents** (§3) are eliminated entirely; the `cluster_state` index is replaced by ArangoDB metadata.
>
> Sections marked with these concerns will note their V4 status where relevant.

---

## 1. What Is Clustering?

The WHG Elasticsearch indices contain approximately **47 million distinct place records** drawn from multiple independent gazetteers: GeoNames (~12M), OpenStreetMap (~15M), Wikidata (~8M), the Getty Thesaurus of Geographic Names (~3M), GB1900 (~2.6M), Pleiades (~37K), and others. Each gazetteer calls the same real-world place by its own identifier — for example, Paris exists as `gn:2988507` in GeoNames, `wd:Q90` in Wikidata, `tgn:7008038` in TGN, and so on.

> **Note on index size reporting:** The ES `_cat/indices` API reports ~413M documents for the `places` index. This is the Lucene document count, which includes internal hidden documents for every nested object (each toponym variant, geometry, type, and relation stored within a place is a separate Lucene document). The actual number of distinct place records is ~47M.

**Clustering** is the process of figuring out which of these separate records actually refer to the same physical place and grouping them together. The result is a set of **clusters** — groups of place records that are believed to denote the same real-world location. Once clustered, a search for "Paris" can return a single grouped result representing the city, rather than a dozen confusing separate entries from different gazetteers.

### Why Not Just Match by Name?

Place names alone are hopelessly ambiguous. There are dozens of places called "Springfield", "Alexandria", "Santiago", or "San José" scattered across the globe. Matching by name alone would merge unrelated places into nonsensical mega-clusters. The clustering system therefore combines **multiple independent signals** — name similarity, geographic proximity, country codes, and place type — to decide which records genuinely refer to the same place.

---

## 2. The Four-Phase Pipeline

Clustering runs as a sequential pipeline of four phases. Each phase discovers pairs of place records that are likely the same entity, and the final phase assembles those pairs into clusters.

### Phase 1A: Authority Hard Links

**What it does:** Scans all ~47M place records for explicit cross-references already provided by the source gazetteers themselves. For example, Pleiades records often include a `sameAs` link to the corresponding GeoNames entry, and TGN records link to Wikidata.

**How it works:** The system scrolls through every place in the `places` index and examines its `relations` field. Any relation of type `sameAs`, `closeMatch`, or `exactMatch` that points to a place in a *different* gazetteer is harvested as a hard link with maximum confidence (score 1.0).

**What it produces:** Pairwise link documents — one for each pair of places asserted to be the same by an authority. In the 25 March run, 6.6M places had identity relations; after filtering out same-namespace and unknown-namespace links, this yielded **5,604,928** unique cross-namespace pairs. Phase 1A took approximately 8 minutes.

### Phase 1B: Contributor Reconciliation Links

**What it does:** Would harvest human-confirmed matches made by WHG users when they reconcile their contributed datasets against authorities (e.g., matching a user-uploaded "Constantinople" record to the Wikidata entry for Istanbul).

**Current status:** **Skipped.** These links live in the WHG Django/PostgreSQL database on the DigitalOcean VM. However, contributor place records are namespaced as `whg:place:NNNNN` and have not yet been indexed into the Elasticsearch `places` index. The system automatically detects this (by checking for any `whg:` namespace documents in the places index) and skips Phase 1B when there are none. Once WHG places are indexed, this phase will activate automatically.

### Phase 2: Exact Toponym Co-Attestation

**What it does:** Looks for pairs of places from different gazetteers that share the exact same name spelling in the `toponyms` index.

**How it works:** The `toponyms` index (67M records) stores every name variant for every place. Each toponym document lists which places "attest" it — i.e., which place records use that particular name. When a toponym like "München" is attested by both a GeoNames place and a Wikidata place, that is evidence they might be the same city.

The system scrolls toponyms that span multiple gazetteer namespaces, generates cross-namespace pairs, and then **filters** them:

1. **Country-code overlap** — both places must share at least one country code (e.g., both tagged with "DE" for Germany). Pairs with no country overlap are discarded. This cheaply eliminates most false positives from common names.
2. **Spatial distance** — the two places must be within **50 km** of each other (using the haversine formula on their representative points). This is not the main discriminatory filter — the name and country-code match have already done the heavy lifting. The 50 km is a sanity ceiling that accommodates known coordinate imprecision: modern gazetteers (GeoNames, OSM) agree within 1–5 km, but historical gazetteers (TGN, Pleiades) often have 10–30 km uncertainty, and administrative-centre coordinates can differ from geographic centroids by a similar margin. "Springfield, Illinois" and "Springfield, Massachusetts" are 1,400 km apart and are correctly rejected.
3. **Overflow cap** — toponyms attested by more than **500** cross-namespace pairs are skipped entirely to avoid combinatorial explosion on ultra-common names like "Church" or "San José".

**Scale:** In the 25 March run, Phase 2 scanned 7.3M multi-namespace toponyms (47K skipped by overflow cap), producing 43M raw candidate pairs. Filtering removed 10.4M for no country-code overlap and 20M for spatial distance > 50 km, leaving **12,564,241** surviving pairs. Scanning took approximately 2 hours; filtering and indexing took a further hour.

### Calibration Step (between Phase 2 and Phase 3)

Before Phase 3 runs, the pipeline automatically calibrates its thresholds and scoring weights using the authority hard links from Phase 1A as ground truth. This adjusts the Phase 3 cosine similarity and spatial distance thresholds, the Phase 4 composite score weights, and the cluster score threshold to fit the empirical distribution of positive and negative pairs. The calibration process is described in detail in §8.1. It can be skipped with `--no-calibrate`, in which case the hand-picked defaults in `ScoringConfig` are used.

### Phase 3: Phonetic Similarity (Symphonym)

**What it does:** Finds places with *similar-sounding* names, even across different scripts and transliteration conventions, using the Symphonym phonetic embedding model.

**How it works:** Every toponym in the `toponyms` index has a 128-dimensional embedding vector produced by the Symphonym model. These embeddings capture how a name *sounds* rather than how it is spelled, so "München" and "Munich" and "Мюнхен" all cluster together in embedding space.

The system:

1. Scrolls all 67M toponyms that have embeddings.
2. Skips toponyms whose attesting places are already linked by Phases 1–2 (no point re-discovering known pairs).
3. Skips single-namespace toponyms (they can never produce cross-namespace pairs).
4. For qualifying toponyms, fires **KNN (k-nearest-neighbours) queries** in batches of 50 via Elasticsearch's `_msearch` API, asking: "what are the k closest embeddings to this one, above a minimum cosine similarity?" (By default k=20 and minimum similarity=0.85, but these are adjusted by automatic calibration — see §8.1.)
5. For each KNN hit, forms cross-namespace pairs and filters them by:
   - **Country-code overlap** (same rule as Phase 2)
   - **Spatial distance** within a calibrated limit (default 25 km — tighter than Phase 2's 50 km, because phonetic similarity is weaker evidence than exact name match, so tighter geographic corroboration is required)

In densely settled areas (UK, Netherlands, Japan), phonetically similar but distinct place names can exist within a small radius. The calibration step (§8.1) tunes these thresholds empirically using authority hard links as ground truth, rather than relying solely on hand-picked defaults.

**Scale:** In the 21 March run (before calibration), this phase scanned all 67M toponyms, issued ~1.1M KNN queries, produced ~948M raw candidate pairs, and filtered them down to ~7.6M surviving pairs in approximately 3 hours. The 25 March calibrated run used a lower cosine threshold (0.79 vs 0.85), which admits more KNN hits, but a much tighter spatial limit (5 km vs 25 km), which rejects more of them. It issued a similar number of KNN queries (~1.1M) and produced a similar volume of raw candidates (~948M), but the tight 5 km spatial filter cut the surviving pairs to **3,564,082** — less than half the pre-calibration count. Phase 3 took approximately 2.5 hours.

### Phase 4: Composite Scoring and Clustering

**What it does:** Takes all the pairwise links from Phases 1–3, scores each pair, and assembles them into clusters.

**Scoring:** Each algorithmic pair (from Phases 2 and 3) receives a composite score between 0.0 and 1.0, computed as a weighted sum of five evidence signals. The default weights (which may be adjusted by calibration — see §8.1) are:

| Signal | Default weight | How it's measured |
|--------|--------|-------------------|
| Exact toponym co-attestation count | **30%** | Number of shared exact name spellings (log-scaled, capped at 5) |
| Phonetic embedding similarity | **25%** | Best cosine similarity from Symphonym KNN |
| Spatial proximity | **25%** | Inverse distance with a sigmoid curve (half-strength at 10 km) |
| Place-type match | **10%** | Binary: do the two places share any type classification? |
| Country-code overlap | **10%** | Number of shared country codes (capped at 2) |

Hard links from Phase 1 always have score 1.0 and are not re-scored.

**Graph construction:** All pairs with a composite score **≥ 0.40** (the cluster threshold) are treated as edges in an undirected graph, where nodes are place records. Connected components of this graph form the initial clusters.

**Spatial coherence check:** Some clusters may connect geographically distant places through chains of similar names. For each cluster, the system computes the maximum pairwise distance between members. If this exceeds **100 km**, the cluster is broken apart using DBSCAN spatial sub-clustering (with a radius of 50 km, i.e., half the max diameter). Members without coordinates are assigned to the largest sub-cluster.

**Cluster IDs:** Each cluster receives a deterministic ID (`c_` followed by a 16-character hex hash of its sorted member list). This means re-running clustering with the same inputs produces the same cluster IDs.

---

## 3. What Gets Stored

All results go into a dedicated Elasticsearch index called `clusters`, containing two types of document:

- **Pairwise link documents** — one per pair, recording which places are linked, the score, the evidence breakdown, and the provenance (which phase discovered it). Phases 1A and 2 alone produce ~18M pairs; the total depends on Phase 3 results.
- **Membership documents** — one per place that belongs to a cluster, recording the cluster ID and cluster size.

A separate single-document index, `cluster_state`, stores the high-water marks and statistics from the last run, enabling incremental updates.

### Run Results (25 March 2026)

This is the first run using HDBSCAN-based calibration (§8.1).

| Metric | Value |
|--------|-------|
| Phase 1A pairs (authority hard links) | 5,604,928 |
| Phase 1B pairs (contributor links) | 0 (skipped — no WHG places indexed) |
| Phase 2 pairs (exact co-attestation) | 12,564,241 |
| Phase 3 pairs (phonetic similarity) | 3,564,082 |
| **Total pairwise docs** | **20,347,077** |
| Pairs above score threshold (0.38) | 16,809,486 |
| Graph nodes / edges | 20,580,538 / 16,809,486 |
| Connected components | 7,309,234 |
| **Clusters formed** (after spatial coherence) | **7,309,689** |
| Membership docs indexed | 20,571,872 |
| **Total runtime** | **7 h 9 min** |

**Calibration results (first empirical run):**

| Parameter | Default | Calibrated |
|-----------|---------|------------|
| `knn_min_similarity` | 0.85 | **0.79** |
| `threshold_phonetic_km` | 25.0 km | **5.0 km** |
| `cluster_score_threshold` | 0.40 | **0.38** |
| `weight_toponym_exact` | 0.30 | **0.00** |
| `weight_symphonym` | 0.25 | **0.38** |
| `weight_spatial` | 0.25 | **0.42** |
| `weight_type_match` | 0.10 | **0.02** |
| `weight_ccode_overlap` | 0.10 | **0.18** |

Several results are notable:

- **Spatial distance limit dropped from 25 km to 5 km.** The 95th percentile of positive-pair distances in the authority hard links is only 5 km. The original 25 km default was far too generous — most genuine same-place pairs from different gazetteers have representative points within a few kilometres of each other.
- **Exact toponym count weight went to zero.** The logistic regression found that sharing an exact name spelling adds no discriminative power on top of the other signals. This is unsurprising: exact name matches already produce cosine similarity ≈ 1.0 in embedding space, so the phonetic similarity signal already captures this information.
- **Phonetic similarity (38%) and spatial proximity (42%) dominate**, together accounting for 80% of the composite score. Country-code overlap (18%) provides the remaining useful signal. Place-type match (2%) is nearly inert — place-type vocabularies are too inconsistent across gazetteers to be reliable.
- **Cosine threshold relaxed from 0.85 to 0.79.** The HDBSCAN phonetic-group matching exposes endonymic variation at lower cosine similarities that the default missed.

Calibration ran in approximately 5 minutes (20,000 positive + 20,000 negative pairs; 19,557 and 17,963 with valid signals respectively).

---

## 4. Deployment

### 4.1 Interim Environment (Current — Elasticsearch)

The clustering pipeline runs against the existing Elasticsearch infrastructure at the University of Pittsburgh's Center for Research Computing (CRC).

**Architecture:**

```
Production ES (VM)                    Staging ES (Slurm node)
  gazetteer.crcd.pitt.edu:9200         htc-nXX:9201
  ├── places (~47M places, 45 GB)      ├── places (snapshot copy)
  ├── toponyms (~67M docs, 42 GB)      ├── toponyms (snapshot copy)
  ├── clusters ← (finalized)           ├── clusters (computed here)
  └── cluster_state                    └── cluster_state
```

**Why staging?** The CRC firewall blocks direct connections from Slurm compute nodes to the production Elasticsearch VM. The solution is to:

1. **Snapshot** the production `places` and `toponyms` indices to a shared filesystem.
2. **Restore** them into a temporary staging ES instance running on a Slurm node.
3. **Run clustering** against the staging ES (which is on the same network as the compute node).
4. **Snapshot** the output `clusters` and `cluster_state` indices.
5. **Restore** them into production ES and swap aliases atomically (zero downtime).

This is orchestrated by the `es.sh` script:

```bash
# Start a staging ES instance on Slurm
es -staging-start

# Run clustering (snapshots prod → staging, submits Slurm job)
es -cluster --full --slurm

# After the job completes, push results to production
es -cluster-finalize TIMESTAMP
```

**Slurm resource allocation:**

| Resource | Allocation |
|----------|-----------|
| Memory | 500 GB |
| Wall time | 3 days |
| CPUs | 16 |
| Partition | htc (high-throughput computing) |

In practice, the full run completed in about 7 hours, well within the 3-day limit.

**Incremental runs:** After the initial full run, subsequent runs can use `--incremental` mode, which only processes documents added or modified since the last run. This should complete in minutes for typical daily updates. Phase 4 (cluster recomputation) always runs in full because it is an in-memory graph operation that completes in seconds.

**Resume after crash:** If a run is interrupted (e.g., Slurm wall-time exceeded, node failure), it can be resumed with `--resume`:

```bash
es -cluster --resume --slurm
```

This skips phases that already completed (checkpointed to the `cluster_state` index) and picks up from where it left off.

### 4.2 V4 Environment (Future — ArangoDB Graph Model)

In the planned V4 architecture, Elasticsearch is replaced by ArangoDB as the primary data store. The clustering results will migrate as follows:

- **Pairwise link documents** become **Attestation nodes** with edges:
  - `subject_of` → Place A
  - `relates_to` → Place B
  - `typed_by` → an Authority document with label `sameAs` (for hard links) or `sameAs_candidate` (for algorithmic soft links)
  - `sourced_by` → the algorithm, source authority, or contributing dataset
  - `certainty` = the composite score
  - `certainty_note` = the algorithm version string

- **Membership documents are discarded entirely.** Cluster membership becomes a graph traversal query — "find all places reachable from this one via `sameAs` / `sameAs_candidate` edges." This is more flexible than pre-computed clusters because it allows different confidence thresholds at query time.

- **Gateway reconciliation grouping** shifts from ES `terms` lookups to AQL graph traversal queries.

The migration is a one-time batch operation: iterate all pairwise docs → create attestation nodes and edges → delete the `clusters` index.

---

## 5. Thresholds and How to Adjust Them

All thresholds are defined in `clustering/config.py` in the `ScoringConfig` dataclass. They can be overridden via environment variables or by editing the config directly.

### 5.1 Phase 2: Exact Toponym Co-Attestation

| Parameter | Default | Config field | Effect |
|-----------|---------|-------------|--------|
| Maximum spatial distance | **50 km** | `threshold_exact_km` | Pairs of places sharing an exact name spelling but further apart than this are discarded. Increase for regions with imprecise historical coordinates; decrease to be more conservative. |
| Max attestations per toponym | **500** | `max_attestations_per_toponym` | Toponyms attested by more cross-namespace pairs than this are skipped entirely (e.g., "Church" might produce millions of false pairs). Increase if you suspect valid pairs are being lost in high-frequency toponyms; decrease for faster processing. |

**Why 50 km?** At this point in the pipeline, exact name match + country-code overlap have already done the heavy filtering. The 50 km is a sanity ceiling, not a discriminatory threshold. It accommodates 10–30 km coordinate scatter in TGN, Pleiades, and other historical gazetteers, plus differences between administrative-centre coordinates and geographic centroids in modern gazetteers. Lowering to 30 km risks losing valid pairs with imprecise historical coordinates; raising to 100 km starts admitting within-country homonyms.

### 5.2 Phase 3: Phonetic Similarity

| Parameter | Default | Config field | Effect |
|-----------|---------|-------------|--------|
| KNN neighbours (k) | **20** | `knn_k` | How many nearest embedding neighbours to retrieve per query. Higher values find more pairs but increase processing time and false positives. |
| Minimum cosine similarity | **0.85** | `knn_min_similarity` | Embeddings less similar than this are not returned by the KNN query. Lower values cast a wider net but produce more noise. The gateway reconciliation endpoint uses 0.7; clustering uses 0.85 for higher confidence. |
| Maximum spatial distance | **25 km** | `threshold_phonetic_km` | Tighter than Phase 2's 50 km because phonetic similarity alone is weaker evidence. Increase if you are missing valid pairs in areas with inaccurate coordinates. |
| KNN concurrency | **10** | `knn_concurrency` | Number of concurrent KNN query batches. Higher values speed up processing but increase ES load. |
| _msearch batch size | **50** | `MSEARCH_BATCH_SIZE` (in phonetic.py) | How many KNN queries to pack into a single `_msearch` HTTP request. |

**Why 85% cosine / 25 km?** These are the starting defaults but are **automatically calibrated** before each Phase 3 run. The calibration step (§8.1) uses authority hard links as ground truth to fit optimal thresholds. If calibration is skipped (`--no-calibrate`), these defaults apply. In densely settled areas (UK, Netherlands, Japan), many phonetically similar but distinct place names exist within 25 km.

### 5.3 Phase 4: Scoring and Clustering

| Parameter | Default | Config field | Effect |
|-----------|---------|-------------|--------|
| **Cluster score threshold** | **0.40** | `cluster_score_threshold` | The minimum composite score for a pair to be included as an edge in the cluster graph. **This is the single most impactful threshold.** Lowering it (e.g., to 0.30) will merge more places into clusters but risks false merges. Raising it (e.g., to 0.50) produces fewer, higher-confidence clusters but may miss valid links. |
| Max cluster diameter | **100 km** | `max_cluster_diameter_km` | Clusters spanning more than this distance are split using DBSCAN spatial sub-clustering. Increase for large administrative regions or historical territories; decrease for tighter geographic coherence. |

### 5.4 Composite Score Weights

| Signal | Default weight | Config field |
|--------|---------------|-------------|
| Exact toponym count | **0.30** | `weight_toponym_exact` |
| Phonetic similarity | **0.25** | `weight_symphonym` |
| Spatial proximity | **0.25** | `weight_spatial` |
| Place-type match | **0.10** | `weight_type_match` |
| Country-code overlap | **0.10** | `weight_ccode_overlap` |

These must sum to 1.0. To emphasise spatial evidence over name matching, you might set `weight_spatial=0.35` and `weight_toponym_exact=0.20`. To prioritise phonetic similarity, increase `weight_symphonym`.

The spatial signal uses a **sigmoid curve** centred at 10 km: places 0 km apart score 1.0, places 10 km apart score 0.5, and places 100 km apart score ~0.09. This means spatial proximity contributes strongly within a city-scale radius but diminishes rapidly at regional distances.

### 5.5 Processing Controls

| Parameter | Default | Config field / env var | Effect |
|-----------|---------|----------------------|--------|
| Batch size | 1000 | `CLUSTER_BATCH_SIZE` | Documents per processing batch |
| Bulk chunk size | 5000 | `CLUSTER_ES_BULK_CHUNK` | Documents per ES bulk request |
| Scroll size | 2000 | `CLUSTER_SCROLL_SIZE` | Documents per ES scroll page |
| Bulk throttle | 0.5s | `CLUSTER_BULK_THROTTLE` | Sleep between bulk index flushes (reduces ES merge pressure) |
| Terms query max | 2000 | `CLUSTER_TERMS_MAX` | Max place IDs per ES terms query (ES has a 65536 hard limit) |

### 5.6 How to Re-Cluster with Different Thresholds

1. **Edit `clustering/config.py`** — change the desired values in the `ScoringConfig` dataclass, or set environment variables before running.

2. **Run a full re-cluster:**
   ```bash
   es -cluster --full --slurm
   ```
   This re-runs all four phases from scratch. Phase 4 will re-score all existing pairwise docs with the new weights/thresholds and recompute clusters.

3. **If you only changed Phase 4 thresholds** (score weights, cluster threshold, max diameter): you can save time by letting Phases 1–3 use their cached results via `--resume`, provided a previous run completed them:
   ```bash
   es -cluster --full --resume --slurm
   ```
   Note: Phase 4 always re-runs regardless of `--resume`, so new scoring parameters will take effect.

4. **Finalize** when the job completes:
   ```bash
   es -cluster-finalize TIMESTAMP
   ```

5. **Compare results** using `es -cluster --stats` to see cluster counts and statistics.

---

## 6. Operational Commands Reference

| Command | What it does |
|---------|-------------|
| `es -cluster --full --slurm` | Full run via Slurm staging ES (production workflow) |
| `es -cluster --full --no-calibrate --slurm` | Full run skipping automated calibration (use default thresholds) |
| `es -cluster --resume --slurm` | Resume a crashed/interrupted full run |
| `es -cluster --incremental` | Incremental run (on VM, against localhost ES) |
| `es -cluster --full --dry-run` | Compute but don't index (for testing) |
| `es -cluster --stats` | Show current state and statistics (quick, no nohup) |
| `es -cluster-finalize TIMESTAMP` | Push Slurm results to production ES |
| `es -staging-start` | Start a staging ES instance on Slurm |
| `es -staging-health` | Check staging ES health |

**Monitoring a running Slurm job:**

```bash
squeue -j JOBID                               # check job status
tail -f /ix1/ishi/es/logs/cluster_JOBID.out    # stdout (progress summaries)
tail -f /ix1/ishi/es/logs/cluster_JOBID.err    # stderr (tqdm progress bars, warnings)
```

---

## 7. Known Limitations and Future Work

1. **Phase 1B (contributor links) is inactive** until WHG contributor places are indexed into ES with `whg:place:NNNNN` identifiers.

2. **No negative evidence.** The system does not currently record "these two places are definitely NOT the same." Rejected reconciliation matches in the WHG database could serve as negative evidence in future.

3. **Phase 3 thresholds are automatically calibrated.** The phonetic similarity cosine threshold (default 0.85) and spatial distance limit (default 25 km) are fitted from authority hard links before each Phase 3 run (`clustering/calibration.py`). Composite score weights and the cluster score threshold are also calibrated. Use `--no-calibrate` to skip calibration and use the manual defaults. Calibration quality depends on the coverage and geographic distribution of authority hard links; volunteer pair review (§8.2) provides additional validation.

4. **Cluster IDs are not stable across runs.** When a new place bridges two existing clusters, the merged cluster gets a new ID. No external system should depend on cluster ID stability.

5. **The `places` index lacks a fully backfilled `indexed_at` field** (unlike `toponyms`). This means incremental runs for Phase 1A fall back to re-scanning all places rather than filtering by timestamp. Adding `indexed_at` to the places schema is recommended.

6. **Large-scale KNN is expensive.** Phase 3 processed ~67M toponyms and issued ~1.1M KNN queries. The `_msearch` batching (50 per HTTP call) makes this tractable, but it remains the slowest phase. Future optimisation could pre-filter toponyms more aggressively or use approximate nearest-neighbour indices.

---

## 8. Threshold Calibration

The Phase 3 phonetic thresholds and the Phase 4 composite score weights are **automatically calibrated** before each Phase 3 run using authority hard links as ground truth (`clustering/calibration.py`). A secondary volunteer review module provides validation and covers edge cases the hard links miss.

### 8.1 Automated Calibration from Authority Hard Links

Phase 1A harvests millions of authority hard-linked pairs — pairs that gazetteers themselves assert are the same place (e.g., Pleiades → GeoNames, TGN → Wikidata). These are a large, high-confidence labeled dataset that can be used to calibrate the phonetic thresholds without any human effort.

**Implementation:** `clustering/calibration.py` — called automatically between Phase 2 and Phase 3 (skip with `--no-calibrate`).

**Method:**

1. **Compute signals for known positives.** For each authority hard-linked pair (A, B):
   - Look up all toponyms attesting A and all toponyms attesting B in the `toponyms` index.
   - **Cluster each place's toponym embeddings into phonetic groups** using HDBSCAN on cosine distances (the same density-based clustering used during Symphonym training data curation, with `min_cluster_size=2`, `cluster_selection_epsilon=0.2`). For example, Köln's toponyms split into a Germanic group (Köln, Keulen, Cologne) and a Latin group (Colonia, Kolonia, Colònia).
   - **Match phonetic groups across the two places** by centroid cosine similarity (greedy 1-to-1, threshold ≥ 0.65). Only groups representing the same phonetic form are compared.
   - For each matched group pair, compute the **best cross-pair cosine similarity** between the two places' embeddings within that group. Emit one calibration observation per matched group.
   - Compute the spatial distance, ccode overlap, and type match (shared across all observations for the pair).

   The phonetic-group approach is important because many places have names in multiple, phonetically unrelated languages — **exonyms** such as "Germany" vs "Deutschland", or "Japan" vs "日本". A naïve comparison of all toponym cross-pairs would include these exonymic mismatches (which have low cosine similarity despite being the same place), polluting the positive distribution and pulling the fitted threshold down. By clustering each place's toponyms into phonetic groups first, exonymic pairs are naturally separated into different groups whose centroids do not match across the two places, so they never enter the calibration distribution.

   At the same time, the per-group approach captures **endonymic variation**: if two places share a Latin-derived phonetic group (e.g. "Colonia" / "Kolonia") alongside a higher-scoring Germanic group ("Köln" / "Keulen"), the Latin group's lower cosine similarity is emitted as a separate genuine positive observation rather than being hidden behind the Germanic group's higher score. This gives the calibration a realistic picture of the full range of within-group similarities.

   If no phonetic groups match across the two places (a pure exonym pair), a single observation using the global best cosine is emitted so these hard positives remain visible without dominating the distribution.

2. **Sample negatives.** Draw a comparable number of random cross-namespace pairs that have no authority hard link between them. Compute the same signals (with the same HDBSCAN group-matching). These are overwhelmingly true negatives (randomly paired places from different gazetteers are almost never the same place).

3. **Fit thresholds.** With both distributions in hand:
   - **Cosine threshold:** ROC analysis on cosine similarity, selecting the Youden-optimal point (clamped to 0.5–0.95).
   - **Spatial threshold:** Set to the 95th percentile of positive pair distances (clamped to 5–200 km).
   - **Composite weights:** Logistic regression on all five normalised signal components; absolute coefficients are normalised to sum to 1.0.
   - **Cluster score threshold:** Sweep from 0.15 to 0.80 in 0.01 steps, selecting the point that maximises F1.

4. **Re-cluster and compare.** Run `es -cluster --full --slurm` with the new parameters and compare cluster counts, precision (spot-checked), and recall against authority hard links.

**Configuration:** The sample size (default 20,000) and negative-to-positive ratio (default 1.0) can be set in `ScoringConfig.calibration_sample_size` and `ScoringConfig.calibration_neg_ratio`. Calibration requires at least 500 authority hard-link pairs with valid embeddings; if fewer are available it is skipped with a warning.

**Limitations:** Authority hard links are biased toward well-documented places with good coordinates and toward European/Mediterranean gazetteers (Pleiades, TGN). They may underrepresent the kinds of difficult pairs the system encounters in practice — phonetically similar names in densely settled regions with imprecise historical coordinates. The HDBSCAN phonetic-group matching mitigates the exonym/endonym problem (see Method above), but calibration quality still depends on the coverage and geographic distribution of authority hard links. Volunteer review (§8.2) compensates for remaining bias.

### 8.2 Volunteer Pair Review

A simple review module — not a gamified platform, but a low-friction tool that contributors can use whenever they wish — provides validation of the automated calibration and collects judgments on pairs in the ambiguous zone that hard links don't cover.

**How it works:**

1. **Sampling:** The system draws pairs from the `clusters` index, stratified by composite score — concentrating on the **uncertain zone** (scores 0.30–0.50) where threshold changes have the most impact, but also including clear positives (>0.70) and clear negatives (<0.20) to anchor responses.

2. **Presentation:** Each pair is shown as two place cards side by side on a map: names, coordinates, country, source gazetteer, type, and distance. The volunteer clicks **Same place**, **Different places**, or **Unsure**.

3. **Redundancy:** Each pair is shown to **2–3 independent volunteers** to mitigate individual bias. A pair's label is determined by majority agreement; pairs with no majority go back into the review pool or are flagged for expert adjudication.

4. **Gold standard pairs:** Phase 1A authority hard links serve as a built-in gold set of positive examples. A volunteer whose judgments consistently contradict the gold set can have their responses down-weighted.

### 8.3 Integration with Reconciliation

When a WHG user performs dataset reconciliation and encounters clustered results, the interface can offer an optional **"Do you agree these are the same place?"** prompt for displayed cluster members. This produces labeled data as a by-product of an activity the user is already performing, at zero additional effort.

### 8.4 Iterative Refinement

Calibration is iterative: after each round (automated or volunteer-based), the new "most uncertain" pairs can be identified and targeted for the next round of review, converging on near-optimal parameters over a few cycles.




