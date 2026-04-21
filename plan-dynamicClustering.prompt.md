# Plan: Dynamic Query-Time Clustering with Precomputed Similarity Graph and Client-Side Threshold Control

## Introduction

WHG v3.2 presents search results as a flat list of individual place records — one per authority per real-world place. A search for "Paris" returns separate entries from GeoNames, Wikidata, OSM, TGN, and Pleiades, forcing the user to mentally deduplicate dozens of records that all refer to the same city. The current v3.5 clustering pipeline partially addresses this by pre-computing fixed equivalence clusters and storing static membership assignments in a `clusters` ES index, but this approach has fundamental limitations:

- **The threshold is baked in.** A single scoring cutoff determines what gets clustered. Users researching ancient Mediterranean geography need different grouping than users browsing modern administrative divisions — but there is no way to adjust sensitivity.
- **Clustering is all-or-nothing.** The current "Group linked records" toggle in the Data Sources panel is a binary switch. There is no middle ground between "show everything flat" and "apply the pre-computed clustering."
- **The similarity model is opaque.** Users cannot understand or influence what "similar enough to group" means. They cannot prioritise spatial proximity over name similarity, or vice versa.
- **The architecture does not scale to 47M records.** Pre-computing and storing cluster membership for every place is expensive and brittle — any re-ingestion invalidates the entire membership index.

This plan replaces the static clustering model with a **materialised similarity graph** that defers final grouping decisions to query time and ultimately to the user's browser. The core insight: do not ask "which places are the same?" offline and store the answer. Instead, ask "which places *might* be the same, and how similar are they across multiple dimensions?" offline, store the weighted evidence, and let the user decide what "same enough" means at search time via an interactive threshold slider and per-facet emphasis controls.

Simultaneously, this plan externalises full geometries from ES to the VAST filesystem, introduces H3 spatial indexing for fast blocking and containment prefiltering, and enriches place records with AAT type hierarchy metadata — all changes that reduce ES index size, improve query performance, and prepare the data model for the eventual v4 migration to a native graph database.

---

**Summary of changes:**

Replace the current static `clusters` index (which stores fixed membership assignments at a single threshold) with a **precomputed similarity graph** plus lightweight query-time and client-side clustering. The goal: when a user searches, results can be grouped on-the-fly according to a UI-controlled similarity slider (θ ∈ [0,1]), and the grouping updates interactively without re-querying the server. The existing 4-phase offline pipeline (hard links → exact toponyms → phonetic similarity → composite scoring + graph clustering) is retained for building the similarity graph, but the final clustering step becomes query-dependent rather than statically precomputed. Simultaneously, move full geometries out of ES into a **chunked WKB store on the VAST filesystem** (`/vast/ishi/`), replacing the `geom` field with a `has_geom` boolean and a stacked prefilter pipeline (H3 → bbox → hull → VAST read) for precise spatial operations.

---

## Core Architecture

```
Offline pipeline (batch)
  ├── Phase 1: Harvest hard links (authority sameAs, contributor reconciliation)
  ├── Phase 2: Exact toponym co-attestation
  ├── Phase 3: Phonetic similarity (Symphonym KNN)
  └── Phase 4: Composite scoring → sparse similarity graph (persisted)

Query time (server)
  ├── Step 1: Discovery (toponyms index — BM25 or Symphonym KNN)
  ├── Step 2: Filtering + aggregations (places index)
  ├── Step 3: Enrichment (toponyms + graph neighbors)
  └── Step 4: Return compact clustering-ready payload

Client (browser)
  ├── Receive per-result facet vectors + precomputed neighbor edges
  ├── Build local similarity subgraph (pruned)
  ├── Apply user-controlled threshold (slider)
  ├── Union-Find clustering on filtered subgraph
  └── Update display interactively (no round-trip)
```

---

## 1. Multi-Facet Similarity Representation

Each place carries separable, compact similarity signals rather than a single monolithic embedding. All are indexed at ingestion time (no new infrastructure cost since a full re-ingestion is planned).

### 1a. Toponym similarity

Handled by the `toponyms` index: Symphonym 128-d int8 embeddings + BM25 text fields. At query time the server computes toponym-match scores per place_id during discovery (Step 1). For client-side **phonetic re-scoring** — allowing the user to type a name variant and see how phonetically similar it is to each result's names — a compressed Symphonym embedding is included in the per-hit payload (see §5a). The `embed()` endpoint on the gateway produces the query-side embedding on demand.

### 1b. Spatial — H3 multi-resolution cells

Add H3 fields to the `places` schema for fast spatial blocking and coarse containment:

- `h3_centroid` (`keyword`) — H3 cell ID at a reference resolution (e.g. r7) for the representative point.
- `h3_cover` (`keyword`, multi-valued) — compacted H3 cell IDs covering the full geometry. For point geometries this equals the centroid cell. For polygons, use `h3.polyfill()` + `h3.compact()` to avoid explosion on continental geometries. Cap at a maximum resolution to prevent massive arrays.

Multi-resolution buckets (r5, r7, r9) can be added later but are not required initially — compacted covers already span multiple resolutions.

**Why H3 rather than only bbox/geo_shape:** H3 `terms` filters in ES are extremely fast (inverted index lookups), enable spatial blocking for client-side clustering (same cell = candidate pair), and provide resolution-adaptive containment. They form the first layer of a stacked prefilter pipeline: H3 → bbox (`bounds`) → hull → full geometry (VAST read). See §16 for the complete containment pipeline.

### 1c. Temporal

Already present: `toponyms[].timespans[].start.in` / `end.in` (nested integers on the `places` index). The gateway already filters on these. Temporal similarity between two places is computed during offline edge scoring (§2b) and baked into the composite edge weight — the client does not recompute it. For display purposes, the server returns a flattened temporal summary per result (see §5).

### 1d. Type similarity — AAT depth and ancestor encoding

Add to the `places` schema within `types[]` (nested):

- `aat_id` (`integer`) — the mapped AAT concept ID (from the type system mapping pipeline). Null if unmapped.
- `aat_depth` (`integer`) — depth in the AAT hierarchy (primary path).
- `aat_ancestors` (`integer`, multi-valued) — materialized ancestor set (all ancestors via any path, deduplicated). Stored as a flat array for efficient set intersection.

These are derivable from the `types` ES index (which already stores `depth`, `ancestors`, `path` per AAT concept). The `processing/aat_lookup.py` helper (from the consolidateBoundaries plan) computes these at ingestion time.

**Offline type similarity** (baked into edge scores) uses the Wu-Palmer formula:

```
S_type = 2 * depth(LCA) / (depth(a) + depth(b))
```

where LCA is the lowest common ancestor, found as the deepest node present in both `aat_ancestors` arrays. AAT is nominally a tree but has weak DAG-like cross-links in practice; storing the full ancestor **set** (not just the primary path) handles this correctly. The intersection is computed offline during edge scoring — the client receives the pre-normalised type similarity component in the edge signal breakdown and does not recompute LCA.

### 1e. Authority links (hard constraints)

Already present: `links[]` and `relations[]` on the `places` index carry explicit cross-authority identifiers (e.g. Wikidata Q-IDs, GeoNames IDs). The offline pipeline harvests these as hard links (score = 1.0). At query time, shared authority link IDs between two results set `S_links ≈ 1` — functioning as hard or near-hard clustering constraints.

---

## 2. Precompute a Sparse Similarity Graph (Offline)

The existing 4-phase clustering pipeline (`clustering/`) is adapted to produce a **sparse similarity graph** rather than final cluster assignments.

### 2a. Retain Phases 1–3 unchanged

- **Phase 1A**: Authority hard links (relations with sameAs/closeMatch/exactMatch).
- **Phase 1B**: Contributor reconciliation links (WHG Django PostgreSQL).
- **Phase 2**: Exact toponym co-attestation (shared toponym_id, spatially proximate).
- **Phase 3**: Phonetic similarity (Symphonym KNN, spatially proximate).

These phases generate candidate pairs with scored evidence signals — exactly the blocking step that reduces comparisons by orders of magnitude.

### 2b. Adapt Phase 4: Score but do not cluster

Currently Phase 4 computes composite scores and then runs connected components + DBSCAN spatial sub-clustering to produce static membership docs. Under the new design:

- **Keep** composite scoring (`scoring.py`): the weighted combination of signals (toponym exact, Symphonym similarity, spatial distance, type match, ccode overlap) produces a single `score` per pair.
- **Keep** pairwise link docs in the `place_graph` index (doc_type = `pairwise`).
- **Remove** static membership docs (doc_type = `membership`). These become query-time artifacts.
- **Add** a top-K neighbor list per place (see §2c).

### 2c. Persist per-place neighbor lists

After scoring all pairwise docs, build a per-place adjacency list of the top-K neighbors (K ≈ 20–100) with edge weights. Store these as a new document type in the `place_graph` index:

```json
{
  "doc_type": "neighbors",
  "place_id": "gn:2988507",
  "namespace": "gn",
  "neighbors": [
    {"place_id": "wd:Q90", "score": 0.95,
     "s": {"n": 0.98, "sp": 0.92, "t": 0.85, "ty": 1.0, "l": 1.0}},
    {"place_id": "osm:n12345", "score": 0.87,
     "s": {"n": 0.90, "sp": 0.95, "t": null, "ty": 0.78, "l": 0.0}},
    ...
  ],
  "algorithm_version": "graph_v1.0",
  "created_at": "..."
}
```

This is the materialized adjacency list store — a domain-aware weighted similarity graph. At query time, the server fetches neighbor docs for result-set places in a single `terms` query.

### 2c′. Edge symmetrisation (critical)

Top-K neighbor lists are inherently asymmetric: A may include B in its top-K, but B may not include A. This creates order-dependent clustering where Union-Find results change depending on which side's neighbor doc is read first.

**Required fix:** symmetrise edges during neighbor doc construction. For each pair (A, B), if A→B appears in A's top-K **or** B→A appears in B's top-K (or both), include the edge in **both** neighbor docs with `score = max(score_A→B, score_B→A)`. This ensures the graph is undirected and clustering is stable under threshold changes.

**Degree truncation.** Symmetrisation can create degree imbalance — hub places (e.g. a major city referenced by many records) may accumulate far more than K neighbors, while peripheral records have few. To keep the client payload predictable, truncate per-node degree to `K_max_client` (e.g. 50) after symmetrisation, retaining the highest-scoring edges.

**Note on stored asymmetry.** Truncation can make the stored neighbor docs asymmetric: if hub A has 200 edges and truncates to 50, it may drop its edge to B, while B (with only 5 edges) retains its edge to A. However, this does **not** affect query-time symmetry because the gateway's subgraph extraction step (§10b) collects edges from **all** fetched neighbor docs — if B's doc includes the B→A edge, it appears in the response regardless of whether A's doc was truncated. The only scenario where an edge is truly lost is when **both** endpoints independently truncate the edge to each other (both are high-degree hubs that rank the mutual edge below their respective top-50). This is rare in practice: if both places are hubs with 200+ edges, the edge between two hubs is typically high-scoring and survives truncation on both sides. The worst-case impact is a small number of lost edges between moderately-connected hubs — acceptable given the payoff in payload predictability.

### 2d. Optionally precompute baseline clusters

Run connected components at a **high threshold** (e.g. 0.9) offline to identify near-certain identity groups. Store as a lightweight `baseline_cluster_id` on each neighbor doc. These provide instant grouping for obvious matches and a starting layer for client-side refinement (initial unions in the Union-Find).

**Link-dominated construction.** Baseline clusters must be constructed using only **authority link signals** (`s.l`) and optionally very high toponym signals (`s.n ≥ 0.95`), not the full composite score with arbitrary weights. This is essential because the client can reweight facets arbitrarily — a user who sets `w_spatial` high and `w_name` low would find that baseline clusters (built with different weighting) contradict their semantic intent. By restricting baseline clusters to link-dominated evidence (the strongest and least subjective identity signal), they remain valid regardless of how the user tunes the emphasis sliders.

### 2e. Edge retention threshold

Only persist pairwise docs and neighbor entries where the composite score exceeds a floor (e.g. ε = 0.3). Edges below this threshold are unlikely to be useful even at the loosest user settings and would waste storage and query bandwidth.

---

## 3. Schema Changes

### 3a. `places` index (`schemas/places.json`)

Within the existing `geometries[]` nested object:

- **Remove** the `geom` field (`geo_shape`). Full geometries are no longer stored in ES (see §16).
- **Add** `has_geom` (`boolean`, default `false`) — when `true`, indicates that a full geometry for this geometry entry exists on the VAST filesystem. When `false`, `repr_point` is the only geometry supplied by the authority.
- **Retain** `repr_point` (`geo_point`), `hull` (`geo_shape`), `bounds` (`float` array), `timespans` (nested).

Add new top-level fields:

- `h3_centroid` (`keyword`) — H3 cell at reference resolution for the representative point.
- `h3_cover` (`keyword`, multi-valued) — compacted H3 coverage cells.

Within the existing `types[]` nested object, add:

- `aat_id` (`integer`) — mapped AAT concept ID.
- `aat_depth` (`integer`) — AAT hierarchy depth.
- `aat_ancestors` (`integer`, multi-valued) — materialized ancestor set (all ancestors, deduplicated).

### 3b. `place_graph` index (replaces `clusters`)

Rename the `clusters` index to `place_graph` to reflect its true role as a **materialized adjacency list store** (weighted graph edges), not a cluster assignment table. This prevents semantic confusion and eases v4 migration where these become native graph edges.

The `place_graph` index contains two document types:

**Pairwise docs** (retained from the existing pipeline):
- `doc_type`: `"pairwise"` — scored evidence for a pair of places.
- All existing pairwise fields unchanged (`place_id_a`, `place_id_b`, `score`, `signals`, etc.).

**Neighbor docs** (new):
- `doc_type`: `"neighbors"` (keyword)
- `place_id`: the place this adjacency list belongs to (keyword)
- `namespace`: extracted from place_id (keyword)
- `neighbors`: object array, each `{place_id, score, s}` where `s` is a signal breakdown `{n, sp, t, ty, l}` (normalised per-facet scores). Stored as a non-indexed (enabled: false) object to avoid nested overhead, since these are only fetched by place_id, never queried internally.
- `baseline_cluster_id`: optional (keyword) — precomputed high-threshold cluster.
- `baseline_cluster_size`: optional (integer).
- `algorithm_version`, `created_at`: as existing.

Remove the `membership` document type — static cluster assignments are replaced entirely by query-time dynamic clustering.

### 3c. No changes to `toponyms` index

Symphonym embeddings and attestation arrays remain as-is.

### 3d. No changes to `types` index

AAT depth, ancestors, and path are already stored there. The new `aat_*` fields on `places.types[]` are derived from this index at ingestion time.

---

## 4. Gateway Changes

### 4a. Search endpoint (`POST /api/search`)

Adapt the existing 3-step architecture to include a new Step 3c:

**Step 3c — Neighbor graph expansion.** For each surviving place_id, fetch its `neighbors` doc from the `place_graph` index (single `terms` query on `place_id` with `doc_type: "neighbors"`). Intersect each place's neighbor list with the result set to produce a **local similarity subgraph** — only edges between results that both survived filtering.

**Response payload changes** (see §5 for detail):

- Add per-result compact clustering signals (centroid, temporal summary, AAT info, baseline_cluster_id).
- Add an `edges` array: the local similarity subgraph edges `[{a, b, score}]`.
- Retain the existing flat `hits` list for backward compatibility.

### 4b. New response model: `ClusterableSearchResponse`

Extends `SearchResponse` with:

- `edges: list[Edge]` — pairwise similarity edges between result place_ids, each with composite score and per-facet signal breakdown (§5b).
- `query_emb: str` — base64-encoded Symphonym embedding of the query string (§5d).
- `toponym_stoplist: list[str]` — high-frequency generic toponym tokens for synthetic edge filtering (§6i Rule A). Maintained server-side, included in every response.
- `clustering_params: dict` — calibrated defaults for client-side clustering: `θ_bridge`, `θ_query`, `θ_synth`, `θ_synth_structural`, `τ_name`, `τ_link`, default facet weights `[w_n, w_sp, w_t, w_ty, w_l]`.
- Each `SearchHit` gains: `h3` (string), `h3_cover` (string[]), `temporal_range` ([start, end] or null), `baseline_cluster_id` (str or null), `query_match` (object with `name`, `score`, and `phon_emb` — the discovery-time match signal for query-conditioned clustering, §5a).
- Per-hit `aat_ids` and `aat_depths` are available for display (type-tree widget, tooltips) but are not used for client-side similarity — type similarity is precomputed in the edge signal breakdown.

### 4c. Suggest endpoint — no change

Typeahead remains lightweight and does not involve clustering.

### 4d. Reconcile endpoint (`POST /api/reconcile`)

Same adaptation as search: add optional neighbor expansion and edge emission. The existing `group_by_cluster` parameter (from CLUSTERS.md §2.3) is replaced by client-side grouping, but the server can still pre-group at a default threshold for non-JS consumers.

### 4e. Server-side fallback clustering

For API consumers that cannot do client-side clustering (e.g. OpenRefine, programmatic access), the server applies a default threshold (e.g. θ = 0.85) to the local subgraph and returns pre-grouped results. This reuses Union-Find logic implemented in Python on the gateway. The `SearchRequest` model gains an optional `cluster_threshold: float | None` parameter; when set, the server clusters and returns grouped results.

**Determinism requirement.** Server-side clustering must produce stable, reproducible results for the same query and threshold. To ensure this, the server-side path uses only **Rule 1** (standard edge thresholding) with **fixed calibrated weights** — no Rule 2 (query-bridge), no Phase 2 (synthetic edges). This eliminates the non-deterministic behaviours that are acceptable in interactive UI but unacceptable for programmatic reconciliation workflows. Edge iteration order does not affect the result because Union-Find is commutative.

---

## 5. Client-Side Clustering Payload

### 5a. Per-result compact payload

For each hit, the server returns (in addition to existing fields):

```json
{
  "place_id": "gn:2988507",
  "score": 87.2,
  "title": "Paris",
  "namespace": "gn",
  "repr_point": [2.3522, 48.8566],
  "h3": "871ea6d75ffffff",
  "h3_cover": ["871ea6d75ffffff", "851ea6d7fffffff"],
  "temporal_range": [-500, 2026],
  "aat_ids": [300008347],
  "aat_depths": [6],
  "baseline_cluster_id": "c_abc123",
  "query_match": {
    "name": "Paris",
    "score": 0.93,
    "phon_emb": "<base64-encoded 128-byte int8 vector>"
  },
  "names": [...],
  "ccodes": ["FR"],
  "types": [...],
  "geometries": [...]
}
```

The `query_match` object carries the discovery-time match signal: which toponym triggered the hit and how well it matched. `query_match.name` is the matched toponym string, `query_match.score` is the normalised discovery score (0–1), and `query_match.phon_emb` is the Symphonym 128-d int8 embedding of that toponym (base64-encoded, 128 bytes → 172 characters). The client uses `query_match.score` for query-conditioned clustering (§6h) and `query_match.phon_emb` for phonetic re-scoring (§6g).

### 5b. Edges array

Alongside the hits:

```json
{
  "edges": [
    {"a": "gn:2988507", "b": "wd:Q90", "score": 0.95,
     "s": {"n": 0.98, "sp": 0.92, "t": 0.85, "ty": 1.0, "l": 1.0}},
    {"a": "gn:2988507", "b": "osm:n12345", "score": 0.87,
     "s": {"n": 0.90, "sp": 0.95, "t": null, "ty": 0.78, "l": 0.0}},
    ...
  ]
}
```

Each edge carries the composite `score` plus a signal breakdown `s` with per-facet normalised similarities: `n` (toponym), `sp` (spatial), `t` (temporal, null if either place lacks timespans), `ty` (type/AAT), `l` (links). The client uses these for facet-weight scaling (§6a). Only edges between result-set members are included.

### 5c. Payload size budget

- ~500 results × ~500 bytes ≈ 250 KB (hits including query_match at ~200 bytes each)
- ~2000 edges × ~120 bytes ≈ 240 KB (edges with signal breakdown)
- query_emb: 172 bytes (negligible)
- Total: ~490 KB before gzip, ~110–160 KB compressed — within budget.

**Hard cap:** `max_edges = 4000`. The gateway enforces this limit on the `edges` array, selecting edges by highest composite score globally. Without a cap, worst-case scenarios (dense urban + high K + symmetrisation: 500 × 50 / 2 = 12,500 edges ≈ 1.5–2 MB) degrade both transfer and client parsing time. The cap keeps payload under ~750 KB pre-compression in all cases.

For result sets > 500, cap the edges to top-scoring pairs and/or restrict clustering to the top N results.

### 5d. Response-level query embedding

The response includes a top-level `query_emb` field: the Symphonym 128-d int8 embedding of the original query string (base64-encoded). For phonetic/fuzzy searches, this is the same embedding computed during discovery (zero additional cost). For exact/starts/in searches, the gateway generates it on demand via the Symphonym encoder (~5 ms).

This eliminates the need for the client to make a separate `GET /api/embed` call for the initial query string. The client uses `query_emb` for phonetic re-scoring (§6g) — comparing the query embedding against each hit's `query_match.phon_emb` to display query-relevance indicators. For the "Compare name variant" feature (where the user types a different name), the client still calls `/api/embed` for the new variant.

---

## 6. Client-Side Clustering Algorithm

### 6a. Edge scores and facet-weight scaling

Each edge arrives from the server with a precomputed composite score that already incorporates all facets (toponym, spatial, temporal, type, links) — see §2b. The client's primary operation is **thresholding**: keep or discard edges based on the user's slider position θ.

For richer control, the server decomposes the composite score into per-facet **signal components** on each edge:

```json
{"a": "gn:2988507", "b": "wd:Q90", "score": 0.95,
 "s": {"n": 0.98, "sp": 0.92, "t": 0.85, "ty": 1.0, "l": 1.0}}
```

where `s.n` = toponym, `s.sp` = spatial, `s.t` = temporal, `s.ty` = type, `s.l` = links — all pre-normalised to 0–1. The client can then reweight on the fly:

```
S = w_n·s.n + w_sp·s.sp + w_t·s.t + w_ty·s.ty + w_l·s.l
```

with UI-controlled emphasis sliders (e.g. "prioritise spatial proximity" or "prioritise name similarity"). This turns the system from simple threshold clustering into a **semantic lensing system** — the user can shift what "similar" means, not just how strict the cutoff is.

Default weights match the offline pipeline (`scoring.py`): w_n=0.30, w_sp=0.25, w_t=0.10, w_ty=0.10, w_l=0.25. The temporal and type facets are weighted lower because many records lack temporal data and type mappings are incomplete; links are weighted higher because authority assertions are the strongest identity signal.

**Null-facet handling.** When a signal component is `null` (e.g. `s.t = null` because one or both places lack timespans), the client **renormalises weights dynamically**: redistribute the null facet's weight proportionally among the non-null facets. For example, if `s.t = null` and the user's weights are `[0.30, 0.25, 0.10, 0.10, 0.25]`, the effective weights become `[0.30, 0.25, 0, 0.10, 0.25] / 0.90 = [0.333, 0.278, 0, 0.111, 0.278]`. This ensures records lacking temporal data are not penalised (treated as 0) or artificially boosted — they are simply scored on the available evidence. Both the server-side offline scoring (§9f) and the client-side reweighting must use the same renormalisation rule for consistency.

**Known tradeoff: missing data scores higher than noisy data.** Redistribution means that two places with perfect name/spatial/link match but *no* temporal data will score higher than two places with perfect name/spatial/link but *slightly mismatched* temporal data (because the latter incurs a small temporal penalty while the former redistributes the temporal weight to the already-perfect facets). This is an inherent property of proportional redistribution — records are implicitly rewarded for missing a facet rather than having a weak value in it. We accept this tradeoff because: (1) the alternative (treating null as 0) is strictly worse in this domain — ~40% of place records lack temporal data, and penalising them would systematically under-cluster the majority of the corpus; (2) the temporal weight is only 0.10 by default, so the maximum scoring advantage from missing temporal data is `0.10 × (1 - S_t)` ≈ at most 0.10 — small relative to the other facets; (3) as temporal coverage improves through ongoing authority enrichment, the issue diminishes naturally.

**Int8 cosine similarity.** Symphonym embeddings are unit vectors quantized to int8 range [-128, 127]. For pre-normalised int8 vectors, the dot product is proportional to cosine similarity (norms are approximately equal across vectors). The client computes `dot(a, b) / (norm(a) × norm(b))` using `Int8Array` arithmetic. Server-side and client-side similarity values are consistent because both use the same quantized vectors.

This approach keeps all expensive similarity computation server-side (in the offline pipeline), while giving the client cheap, instant re-weighting with no server round-trip. The client never recomputes spatial distances, temporal overlaps, or AAT LCA depths — it only applies weight coefficients to precomputed normalised scores.

### 6b. Comparison pruning

Only compare pairs that have a precomputed edge. This avoids O(n²) explosion:

- The server already prunes to the local subgraph (edges between surviving results).
- Additional client-side blocking: same H3 cell, or shared authority link, or same baseline cluster.
- For ~500 results with ~2000 edges, clustering is O(n) — trivially fast.

### 6c. Union-Find with threshold

```
// Phase 1 — precomputed edges
for each edge (a, b, signals):
    S = reweight(signals, weights)    // with null-facet renormalisation (§6a)

    // Rule 1 — standard: edge exceeds user threshold
    if S >= θ:
        union(a, b)

    // Rule 2 — query bridge: relax threshold for query-relevant pairs (§6h)
    elif S >= θ_bridge
         AND min(query_score[a], query_score[b]) >= θ_query
         AND (signals.n >= τ_name OR signals.l >= τ_link):   // name/link guard
        union(a, b)

// Phase 2a — synthetic phonetic edges for edgeless pairs (§6i)
θ_synth_eff = max(θ_synth, θ)    // never below calibrated floor or user threshold
for each spatial bucket (results sharing h3 centroid OR h3_cover intersection at r5):
    for each pair (a, b) in bucket where find(a) ≠ find(b) AND no precomputed edge:
        if NOT both_high_frequency(name[a], name[b]):  // stoplist guard (§6i)
            if types_overlap(a, b):   // at least one shared type (§6i)
                sim = cosine(phon_emb[a], phon_emb[b])
                if sim >= θ_synth_eff:
                    union(a, b)

// Phase 2b — structural synthetic edges (§6i)
for each spatial bucket (same as 2a):
    for each pair (a, b) in bucket where find(a) ≠ find(b) AND no precomputed edge:
        if (ccode_overlap(a, b) OR shared_namespace(a, b) OR shared_baseline(a, b)):
            union at θ_synth_structural (≈ 0.7)

// Phase 3 — post-processing: split oversized clusters (§6f)
for each component C where |C| > N_max:
    split C by tightening threshold within the component
```

Properties:
- Edge iteration order does not affect the result — Union-Find is applied over all qualifying edges in a single pass (no sorting required). Complexity: O(E·α(n)) ≈ O(E).
- Rule 2 has a **name/link guard** (`signals.n >= τ_name OR signals.l >= τ_link`, where `τ_name ≈ 0.5`, `τ_link ≈ 0.8`) that prevents the bridge from firing on weak edges between places matching generic query terms (e.g. "San", "New", "Central"). Without this guard, two places both matching a common query fragment would merge on a sub-threshold edge with no substantive name or link alignment.
- Rule 2 and Phases 2a/2b can cause **non-monotonic behaviour**: lowering θ may merge clusters that were separate at higher θ due to the bridge and synthetic thresholds. In practice this is rare (bridge fires on <5% of edges, synthetic passes on edgeless pairs only). Tying `θ_synth_eff = max(θ_synth, θ)` limits the effect: at high θ, synthetic edges require even higher phonetic similarity, preserving monotonic feel in the common case.
- Union-Find is near-linear and runs in <10 ms for 500 nodes.
- The query-bridge rule (Rule 2) ensures query-relevant pairs cluster even when their precomputed toponym signal `s.n` is low — see §6h for the full rationale.
- The phonetic synthetic-edge pass (Phase 2a) closes the "missing edge" gap for pairs that share spatial proximity, phonetic similarity, and type overlap but were never candidates in the offline pipeline — see §6i.
- The structural synthetic-edge pass (Phase 2b) catches same-place records across authorities where phonetics fail (cross-lingual exonyms, sparse names) but structural signals confirm co-reference — see §6i.
- Oversized clusters are split as a post-processing step (Phase 3), not blocked during union — see §6f.

### 6d. Baseline cluster bootstrapping

Before applying the user threshold, initialize the Union-Find with baseline clusters (if present): for all results sharing a `baseline_cluster_id`, union them. This provides instant grouping for obvious matches (e.g. GeoNames + Wikidata for the same city) before the user even touches the slider.

**θ = 1.0 bypass.** When the user sets θ = 1.0 ("no grouping — flat list"), baseline bootstrapping is **skipped entirely**. The Union-Find starts with every result in its own singleton component, and since no edge can have a reweighted score ≥ 1.0, no unions occur. This guarantees a truly flat result list identical to unclustered behaviour. At any θ < 1.0, baseline bootstrapping runs normally.

**Safety:** baseline clusters are **link-dominated** (§2d): constructed using only authority link signals (`s.l`) and very high toponym signals (`s.n ≥ 0.95`), not the full composite score. This ensures they remain valid regardless of how the user tunes facet weights. Bootstrapping cannot merge two *different* baseline clusters — it only unions results within the same cluster ID. Subsequent edges from Phase 1 may *expand* a baseline cluster by merging additional results into it, but only if those edges pass the user's threshold θ. Two separate baseline clusters can only end up in the same component if a chain of θ-passing edges connects them — which is correct behaviour, not a conflict.

### 6e. Cluster display

Each cluster gets:
- **Representative**: highest-scoring hit (or preferred-authority heuristic).
- **Aggregated metadata**: all names across members, all authorities, temporal span union, types union.
- **Expandable**: user can expand a cluster to see individual member records.

### 6f. Cluster-size limiting (post-processing)

Union-Find can produce "mega-clusters" at low θ in dense urban regions (e.g. every "Paris" record in one group). Rather than blocking merges during the union pass (which introduces order-dependent results and breaks transitivity), oversized clusters are **split as a post-processing step** after the Union-Find completes:

1. For each connected component with more than `N_max` members (e.g. 50):
2. Extract the subgraph of edges within the component.
3. Tighten the threshold iteratively: raise θ within this component until it fragments into sub-clusters all ≤ N_max, or until θ reaches 0.95 (at which point accept the large cluster as genuinely co-referent).
4. Hard-link edges (authority sameAs, `s.l ≈ 1.0`) are never cut during splitting — they act as unbreakable bonds within the component.

This preserves transitivity: if A~B and B~C both pass the user's threshold, they are always in the same component. Splitting only tightens the threshold *within* oversized components, producing deterministic and order-independent results.

### 6g. Client-side phonetic re-scoring

Each hit carries a `query_match.phon_emb` field: the Symphonym 128-d int8 embedding for the place's best-matching toponym. The response also includes `query_emb` — the embedding of the original query string (§5d). The client can use these to let the user type an alternative name variant and instantly see how phonetically close it is to every result — without a server round-trip.

**Flow:**

1. User types a variant in a "Compare name" input (e.g. "Parigi").
2. The client calls `GET /api/embed?name=Parigi` on the gateway, which returns the Symphonym int8 embedding for the new variant (fast — single model inference, ~5 ms). For the *initial* query, `query_emb` from the response is used directly (no extra call needed).
3. The client computes cosine similarity between the variant embedding and each hit's `query_match.phon_emb` in JavaScript. Int8 dot product on 128 dimensions is trivially fast (~0.01 ms per pair).
4. Results are re-ranked or highlighted by phonetic proximity to the user's variant.

This enables cross-script and cross-transliteration name comparison directly in the browser — a researcher can type a name in Arabic script and see which Latin-script results are phonetically closest, or compare a medieval spelling variant against modern authority records.

The `query_match.phon_emb` vectors also serve a structural role in clustering: they enable **synthetic phonetic edges** for result pairs that lack a precomputed edge — see §6i.

### 6h. Query-conditioned clustering

Precomputed edges encode **query-independent** similarity (`place ↔ place`). But effective search-result clustering requires **query-conditioned** grouping (`query → place → place`). Consider: a user searches for "Big Apple". One result (Wikidata's New York City) matches via that alias. Other NYC records from GeoNames, OSM, etc. may also appear in the result set — matched via "New York" through different discovery paths or neighbor expansion — but the precomputed edge toponym signal `s.n` between "Big Apple" and "New York" is low because the names are phonetically unrelated. Standard threshold clustering could fail to group these co-referent results.

**Solution:** add a **query-bridge rule** to the Union-Find. The server returns a `query_match.score` per hit (the toponym-level discovery score, §5a) and a `query_emb` at the response level (§5d). The client uses these to relax the edge threshold for query-relevant pairs:

```
for each edge (a, b, signals):
    S = Σ w_i · signals[i]

    // Rule 1 — standard: edge exceeds user threshold
    if S >= θ:
        union(a, b)

    // Rule 2 — query bridge: relax threshold when both endpoints
    // strongly match the query and have substantive name or link signal
    elif S >= θ_bridge
         AND min(query_score[a], query_score[b]) >= θ_query
         AND (signals.n >= τ_name OR signals.l >= τ_link):
        union(a, b)
```

Where:
- `θ` is the user's main similarity threshold (slider).
- `θ_bridge = θ × 0.6` (or a configurable floor, e.g. 0.3) — minimum edge quality for bridging.
- `θ_query` — minimum query-match score (e.g. 0.7).
- `τ_name ≈ 0.5` — minimum toponym signal for name-based bridge qualification.
- `τ_link ≈ 0.8` — minimum link signal for link-based bridge qualification.

**Why the name/link guard:** without it, two places both matching a generic query term ("San", "New", "Central") could merge on a weak edge that happens to exceed `θ_bridge`. The guard ensures the bridge only fires when there is *some* substantive name alignment (`s.n >= τ_name`) *or* a strong authority signal (`s.l >= τ_link`). This prevents the query-bridge from becoming a semantic shortcut that merges unrelated places sharing a common name fragment.

**Why `min()` not `max()`:** both endpoints must strongly match the query for the bridge to fire. Using `max()` would let a single strong match pull in weakly-related neighbors indiscriminately. Using `min()` ensures both places are relevant to what the user searched for.

**Why a precomputed edge is required:** merging two places purely because both match the query (without any precomputed edge) is dangerous — "London" would merge London-UK with London-Ohio. The bridge rule only relaxes the *threshold* on existing edges; it does not create edges from nothing.

**Relationship to alias-aware scoring (§9f).** The primary defence against the alias problem is built into the offline pipeline: the toponym facet signal `s.n` on each edge is computed as the maximum Symphonym cosine similarity across ALL cross-name pairs of the two places, not just the single toponym that triggered blocking. This means the "Big Apple" ↔ "New York" case produces a high `s.n` (via the shared "New York" alias) even though the names that matched the user's query are phonetically unrelated. The query-bridge rule here is a **safety net** for residual gaps — edge cases where the max-pairwise toponym score is still coincidentally low but both places are clearly query-relevant. With alias-aware scoring in the graph, the bridge rule fires rarely; without it, the bridge rule would be the primary mechanism, which is less robust.

### 6i. Synthetic edges (edgeless pairs)

The precomputed graph, however recall-heavy the offline pipeline, will inevitably miss some co-referent pairs — rare aliases, missing language variants, or simply places that fell outside the blocking thresholds. Two complementary synthetic passes close this gap at query time.

#### Synthetic Rule A — Phonetic (§6c Phase 2a)

The `query_match.phon_emb` vectors in the payload enable **phonetic synthetic edges** between result pairs that have no precomputed edge.

After the main Union-Find pass (§6c Phase 1), the client runs Phase 2a over spatial buckets:

1. Group results by spatial proximity: same `h3` centroid (r7 ≈ 1.2 km) **or** `h3_cover` intersection at coarse resolution (r5 ≈ 8 km). Centroid equality catches point-vs-point matches; cover intersection catches cases where the same place has different centroids across authorities (e.g. Paris polygon vs Paris point, linear features, boundary geometries). No extra storage is required — `h3_cover` already exists on each place doc.
2. Within each bucket, for every pair (a, b) not already in the same component and with no precomputed edge:
   - **Stoplist guard:** skip if both `query_match.name` values are in a **high-frequency toponym stoplist** (e.g. "Central", "Station", "Market", "Church", "School", "Main", "Park", "New", "San", "Saint"). Without this guard, high phonetic similarity + same H3 + same type (e.g. "building") produces catastrophic merging in OSM-heavy urban regions. The stoplist is maintained server-side and included in the response metadata; it should contain the ~50–100 most common generic place-name tokens across all authorities.
   - **Type constraint:** at least one type must overlap (shared `aat_id`, or both lacking type data). This prevents merging "Central Station" with "Central Park" in the same H3 cell. If both places have typed records, require at least one shared AAT ancestor; if either is untyped, allow the comparison (untyped records are common and should not be excluded).
   - Compute `cosine(phon_emb[a], phon_emb[b])`.
3. If the similarity exceeds `θ_synth_eff = max(θ_synth, θ)`, union them.

#### Synthetic Rule B — Structural (§6c Phase 2b)

A second synthetic pass catches same-place records across authorities where phonetics fail entirely — cross-lingual exonyms with weak phonetic alignment, sparse single-attestation records, or type-misaligned authorities (settlement vs admin unit). This pass uses **shared structural identifiers** rather than phonetic similarity:

Within the same spatial buckets (h3 centroid or cover intersection):

```
for each pair (a, b) in bucket where find(a) ≠ find(b) AND no precomputed edge:
    if (ccode_overlap(a, b) OR shared_namespace(a, b) OR shared_baseline(a, b)):
        union at θ_synth_structural (≈ 0.7)
```

Rationale:
- **Country code overlap** catches same-place records from different authorities that share a country (cheap, high precision when combined with spatial proximity).
- **Shared authority namespace** catches records that somehow bypassed blocking but originate from the same source (rare but diagnostic).
- **Shared `baseline_cluster_id`** propagates high-confidence offline groupings to pairs that lost their connecting edges during result-set pruning.

This is very cheap (set intersections, no embedding computation), high precision (spatial + structural confirmation), and catches the dominant failure mode — edge incompleteness for cross-lingual or sparse records that phonetics alone cannot bridge.

#### Cost analysis

**Phonetic pass:** H3 bucketing reduces comparisons from O(n²) to O(Σ |bucket|²). Typical result sets have most buckets containing 1–5 results; dense urban areas might have ~20. With 500 results across ~200 buckets, total comparisons are a few hundred — each a ~0.01 ms int8 dot product plus a cheap type-set intersection. Total cost: <1 ms.

**Structural pass:** same bucket structure, but comparisons are set intersections (ccodes, namespace string equality, baseline_cluster_id equality) — even cheaper than the phonetic pass.

#### Why spatial gating is essential

Without spatial gating, high phonetic similarity would merge phonetically similar but geographically distant places (e.g. "Springfield" in Illinois vs "Springfield" in Massachusetts). The H3 requirement ensures synthetic edges only form between spatially co-located results.

**Why centroid-only is insufficient:** large polygons can have different centroids across authorities (e.g. one authority centroids Paris at the city hall, another at the geographic center of the commune polygon). Using `h3_cover` intersection at r5 catches these cases without requiring exact centroid alignment.

---

## 7. H3 Implementation Details

### 7a. Computing H3 at ingestion

In `processing/helpers.py` `enrich_geometry()`, add H3 computation:

- **Point geometry**: `h3.latlng_to_cell(lat, lon, resolution=7)` for centroid; cover = [centroid].
- **Polygon/MultiPolygon**: centroid cell as above; `h3.polygon_to_cells(polygon, resolution)` + `h3.compact_cells(cells)` for cover. Choose resolution adaptively: start at r7, if polyfill yields > 10,000 cells, drop to r5; if still > 10,000, drop to r3. Compact after polyfill to minimize array size.
- **LineString**: buffer to small polygon, then polyfill; or sample points along the line.

### 7b. H3 resolution strategy

| Resolution | Hex edge ~km | Use case |
|------------|-------------|----------|
| r3 | ~69 km | Continental blocking |
| r5 | ~8 km | Regional blocking |
| r7 | ~1.2 km | Typical place clustering |
| r9 | ~0.17 km | Dense urban disambiguation |

For the `h3_centroid` field, use r7 as the default. The `h3_cover` field contains compacted cells which naturally span multiple resolutions.

### 7c. Query-time H3 usage

- **Spatial blocking in search**: when a `bounds` GeoJSON filter is provided, compute H3 cover of the bounds and add a `terms` filter on `h3_cover` as a fast prefilter before the more expensive `geo_shape` intersects query.
- **Resolution adaptation**: the H3 cover of the query bounds must use a resolution appropriate to the query scale. A continent-level bbox covered at r7 would produce millions of cells. The gateway should choose the cover resolution dynamically: compute the query bbox area, select the coarsest resolution where cell area < bbox area / 100 (ensuring manageable cell counts), and compact the result. Alternatively, use a simple area-based lookup table:

  | Query bbox approximate extent | Cover resolution |
  |-------------------------------|-----------------|
  | > 2,000 km | r3 |
  | 200–2,000 km | r5 |
  | 20–200 km | r7 |
  | < 20 km | r9 |

  The `h3_cover` field on place documents already contains compacted cells spanning multiple resolutions, so a coarse query-side cover still matches fine-resolution place covers via H3's hierarchical containment.

- **Client-side**: two results sharing an `h3` value (centroid at r7) are spatially proximate — useful as a clustering signal.

---

## 8. AAT Type Enrichment at Ingestion

### 8a. Lookup pipeline

The `processing/aat_lookup.py` module (from the consolidateBoundaries plan) already provides `load_aat_mappings()`. Extend it to also return AAT depth and ancestors for each mapping:

1. For each place's `types[]` entry, look up the AAT mapping (e.g. GeoNames `PPL` → AAT `300008347`).
2. Query the `types` ES index for that `aat_id` to get `depth` and `ancestors`.
3. Store `aat_id`, `aat_depth`, and `aat_ancestors` alongside the existing `identifier`, `label`, `sourceLabel` in the place's `types[]` nested entry.

### 8b. Handling unmapped types

Many types (especially Wikidata and OSM) do not yet have AAT mappings. These entries have null `aat_id`/`aat_depth`/`aat_ancestors`. The client-side similarity function treats unmapped types as neutral (S_type contributes 0, effectively reducing the weight pool for remaining facets).

### 8c. Updating AAT fields when mappings change

The `apply_aat_mappings_to_index()` function (consolidateBoundaries plan, step 2) already supports bulk-updating `types[]` entries on existing place docs. Extend it to also write `aat_id`, `aat_depth`, `aat_ancestors`. This avoids re-ingestion when the Django mapping UI adds new AAT mappings.

---

## 9. Offline Pipeline Adaptations (`clustering/`)

### 9a. `clustering/schemas.py`

- Add `NeighborDoc` Pydantic model (doc_type = `"neighbors"`, place_id, namespace, neighbors list, baseline_cluster_id, baseline_cluster_size, algorithm_version, created_at).
- Retain `PairwiseDoc` and `Signals` unchanged.
- Remove `MembershipDoc`.

### 9b. `clustering/clustering.py`

- Rename `compute_clusters()` → `compute_neighbor_graph()`.
- After scoring, for each place build its top-K neighbor list from all pairwise docs where it appears.
- **Symmetrise edges**: for each pair (A, B), if either A→B or B→A appears in a top-K list, include the edge in both neighbor docs with `score = max(A→B, B→A)`. This may cause some neighbor docs to exceed K entries; that is acceptable.
- Optionally run high-threshold connected components to assign `baseline_cluster_id`.
- Return `list[NeighborDoc]` instead of `list[MembershipDoc]`.

### 9c. `clustering/indexer.py`

- Add `index_neighbor_docs()` function.
- Retain `index_pairwise_docs()`.
- Remove `index_membership_docs()` and `delete_stale_memberships()`.

### 9d. `clustering/runner.py`

- Phase 4 calls `compute_neighbor_graph()` and `index_neighbor_docs()`.
- Incremental runs update neighbor docs for affected place_ids.
- `show_stats()` reports neighbor doc count instead of membership count.

### 9e. `clustering/config.py`

- Add `neighbor_top_k: int = 50` to `ScoringConfig`.
- Add `k_max_client: int = 50` to `ScoringConfig` — per-node degree cap after symmetrisation (§2c′).
- Add `max_edges: int = 4000` to `ScoringConfig` — hard cap on edges in the response payload (§5c).
- Add `baseline_cluster_threshold: float = 0.9` to `ScoringConfig`.
- Add `weight_temporal: float = 0.10` (temporal interval overlap, computed during edge scoring using flattened timespans from the `places` index).
- Add `theta_synth_structural: float = 0.7` — threshold for structural synthetic edges (§6i Rule B).
- Add `tau_name: float = 0.5` — minimum toponym signal for query-bridge name guard (§6h).
- Add `tau_link: float = 0.8` — minimum link signal for query-bridge link guard (§6h).
- Add `toponym_stoplist: list[str]` — high-frequency generic toponym tokens for synthetic edge guard (§6i Rule A). Derived empirically from the toponyms index.
- Retain all existing thresholds — they still govern which pairs enter the graph.

### 9f. `clustering/scoring.py`

- Extend `composite_score()` to compute and return **per-facet normalised signal components** alongside the composite score: `{n, sp, t, ty, l}`.
- Add temporal similarity computation: interval overlap (Jaccard-like) between the flattened timespan unions of each place. Null when either place lacks timespans.

  **Temporal similarity definition.** For each place, flatten all `timespans[].start.in` / `end.in` values into a union interval set (merge overlapping intervals). Then:

  ```
  S_t = overlap_duration / union_duration
  ```

  where `overlap_duration` is the total length of the intersection of the two union intervals, and `union_duration` is the total length of their union. Specific cases:
  - If either place has no temporal data → `S_t = null` (handled by null-facet renormalisation).
  - If both have open-ended ranges (no `end.in`) → treat as extending to the present year → `S_t` reflects overlap from start to now.
  - If both are unbounded on both ends → `S_t = 1.0` (infinite overlap).
  - Multiple intervals per place are merged before comparison (union of all intervals).

- **Null-facet renormalisation**: when computing the composite score and a signal is null (e.g. temporal overlap when either place lacks timespans), redistribute that facet's weight proportionally among the non-null facets. This must match the client-side renormalisation rule (§6a) exactly, so the `score` field on each edge is consistent with what the client computes from the `s` breakdown under default weights.
- **Score invariance guarantee.** The composite `score` on each edge MUST equal the weighted sum of signal components after null renormalisation under the default (calibrated) weights. Formally: `score == Σ (w_i / Σ_nonnull w_j) × s_i` for all non-null `s_i`. This invariant ensures that (a) the client can reconstruct the server's composite score exactly from the `s` breakdown under default weights, (b) server-side fallback clustering and client-side clustering produce identical results at the same threshold with default weights, and (c) debugging is tractable — any discrepancy between server and client scores indicates a bug, not a design ambiguity. Test this invariant as part of the offline pipeline validation (§9g).
- These signal components are stored on each pairwise doc and propagated to neighbor docs for client-side facet-weight scaling.

**Alias-aware toponym scoring.** The toponym facet signal `s.n` must capture the best name match across all aliases, not just the single toponym pair that generated the candidate during blocking (Phases 2–3). For each candidate pair (A, B), compute:

```
s.n = max { cosine(emb(t_a), emb(t_b))  ∀ t_a ∈ names(A), t_b ∈ names(B) }
```

where `emb()` is the Symphonym 128-d int8 embedding (already stored on the `toponyms` index). If A has 5 names and B has 3 names, this is 15 int8 dot products — trivially cheap offline (~0.2 µs per pair).

This handles the "Big Apple" problem at its root: place A = {"Big Apple", "New York"} and place B = {"New York", "NYC"} produce `s.n ≈ 1.0` from the "New York"↔"New York" pair, regardless of which alias matched the user's query. Without this, `s.n` would reflect only the single toponym that triggered blocking — which might be the phonetically-unrelated alias, yielding a deceptively low signal.

**Implementation:** during Phase 4 scoring, for each candidate pair, fetch the Symphonym embeddings for all attestation toponyms of both places (available from the `toponyms` index or cached in DuckDB during the pipeline run). Compute all pairwise cosine similarities and take the max. With ~5 names per place on average and ~20M candidate pairs, total cost is ~20M × 25 × 0.2 µs ≈ 100 seconds — negligible relative to the hours-long pipeline.

### 9g. Weight calibration (`clustering/calibration.py`)

The existing `calibration.py` tunes scoring thresholds using positive/negative pair sampling (Phase 1A hard links as positive pairs, random cross-authority pairs as negatives). Extend it to also **derive optimal facet weights** for the combined similarity function:

1. Using the same positive/negative pair sets, compute per-facet signal components for each pair.
2. Fit a logistic regression (or similar lightweight model) to find the weight vector `[w_n, w_sp, w_t, w_ty, w_l]` that best separates positive pairs (true co-referents) from negatives.
3. Output the calibrated weights to `clustering/config.py` as the default facet weights.
4. These calibrated defaults become the initial weights both for the offline composite score and for the client-side default slider positions.
5. Also derive the client-side clustering parameters `θ_bridge`, `θ_query`, `θ_synth`, `θ_synth_structural`, `τ_name`, and `τ_link` by evaluating precision/recall on the same pair sets under the query-bridge (§6h), phonetic synthetic-edge (§6i Rule A), and structural synthetic-edge (§6i Rule B) rules. Output these alongside the facet weights — the server includes them in the response so the client uses empirically grounded defaults rather than hard-coded constants.
6. Validate the **score invariance** (§9f) on the calibration pair set: for every edge, verify that the stored composite `score` equals the weighted sum of signal components under default weights after null renormalisation. Flag any discrepancies as pipeline bugs.

This is done during the pipeline build (Phase B), not deferred. The calibration data already exists (hard links provide ground truth); the only addition is fitting weights and thresholds alongside the existing calibration. Running calibration before the first full graph build ensures that the similarity graph and the client-side defaults use empirically grounded parameters from day one.

---

## 10. Gateway Neighbor Expansion (`gateway/es_helpers.py`)

### 10a. New helper: `build_neighbor_lookup()`

```python
def build_neighbor_lookup(place_ids: list[str]) -> dict:
    """Fetch neighbor docs for a set of place_ids from the place_graph index."""
    return {
        "size": len(place_ids),
        "query": {
            "bool": {
                "filter": [
                    {"term": {"doc_type": "neighbors"}},
                    {"terms": {"place_id": place_ids}},
                ]
            }
        },
        "_source": ["place_id", "neighbors", "baseline_cluster_id", "baseline_cluster_size"],
    }
```

### 10b. Subgraph extraction

In the search endpoint, after fetching neighbor docs:

1. Build a set of all result place_ids.
2. For each neighbor doc, filter its `neighbors[]` array to only those place_ids in the result set.
3. Collect all surviving edges into the `edges` response array.
4. Collect `baseline_cluster_id` per result.

This produces the local similarity subgraph with no extra ES round-trips.

---

## 11. Performance Characteristics

| Phase | Latency | Notes |
|-------|---------|-------|
| Offline graph build | Hours (Slurm) | Same as current clustering pipeline |
| Server: discovery + filtering | ~200 ms | Unchanged from current search |
| Server: neighbor lookup | ~50 ms | Single `terms` query, ~500 docs |
| Server: subgraph extraction | ~5 ms | In-memory filtering |
| Client: Union-Find clustering | <10 ms | ~500 nodes, ~2000 edges |
| Client: slider re-clustering | <5 ms | Re-apply threshold, no recompute |

Total perceived latency: **~300 ms server + instant client interaction**.

---

## 12. Failure Modes and Mitigations

### 12a. No server-side pagination for clusterable results

Client-side clustering requires the full result set and its edge subgraph in a single payload — traditional server-side pagination (page 1 = results 1–20, page 2 = results 21–40) is fundamentally incompatible because co-referent places split across pages could never be clustered together.

The existing gateway architecture already avoids this: `SearchRequest.size` (max 500) returns all results in one response with no `page`/`offset` parameter. The clustering design preserves this: the entire clustering window (up to 500 results + up to 4000 edges) is delivered in a single payload. Any "pagination" is purely **client-side display pagination** — the browser holds all results and edges in memory, clusters them, and uses virtual scrolling or page controls to render subsets of the already-clustered list.

For queries producing more than 500 matches, the gateway returns the top 500 by discovery score. Matches beyond 500 are not clusterable but are summarised in the response metadata (total hit count, facet aggregations). If a user needs to explore beyond the clustering window, they should refine the query (add filters, narrow spatial bounds) rather than paginate. This is consistent with the existing search UX — the current gateway already caps at 500.

If future requirements demand clustering over larger result sets, the server-side fallback path (§4e) can cluster on the gateway and return pre-grouped results for any number of hits, since it has access to the full graph. But the client-side interactive clustering path is bounded at ~500 results by design.

### 12b. Dense urban datasets (OSM-heavy)

OSM contributes many spatially proximate records with similar names (e.g. "Pharmacy" × 50 in a city). Mitigations:
- The offline pipeline's spatial proximity thresholds (`threshold_exact_km`, `threshold_phonetic_km`) already limit candidate pairs.
- Type similarity separates "pharmacy" from "city" even when spatially co-located.

### 12c. Weak type/temporal data → over-merging

Many records (especially GeoNames) lack temporal data and have generic types. Mitigations:
- Baseline clusters at θ = 0.9 only merge near-certain matches.
- The default UI slider position should start high (e.g. 0.8), encouraging conservative grouping.
- Hard links (authority sameAs) bypass the threshold entirely.

### 12d. Stale neighbor graphs

After authority re-ingestion, the neighbor graph must be rebuilt. Mitigations:
- The existing `es -cluster --full` workflow triggers a complete rebuild.
- Incremental runs (`es -cluster --incremental`) update affected neighbors.

### 12e. Sparse graph fragmentation

If the offline pipeline prunes edges aggressively (high ε floor, low K), the result graph may be disconnected even for true co-referents. This is the **dominant recall failure mode** — clustering becomes recall-limited by graph construction, not by θ. Lowering θ cannot fix missing edges. Mitigations:
- Recall-heavy Phases 2–3 (exact co-attestation + phonetic KNN) generate candidates generously; the ε floor (§2e) is set conservatively low (0.3).
- Alias-aware toponym scoring (§9f) produces strong edges for places sharing any name variant.
- Phonetic synthetic edges (§6i Rule A) catch local gaps where spatial proximity + phonetic similarity indicate co-reference.
- **Structural synthetic edges (§6i Rule B) catch co-referent records where phonetics fail entirely** — cross-lingual exonyms, sparse-name records, temporal divergence, type-misaligned authorities. Country code overlap + spatial proximity is a cheap, high-precision signal that requires no embedding computation.
- The query-bridge rule (§6h) catches residual cases where both places strongly match the query.
- If edge density proves too low in practice, increase `neighbor_top_k` (§9e) or lower ε.

---

## 13. Relationship to Existing Architecture

### 13a. Backward compatibility

The v3.2 Django front-end queries legacy ES indices directly. The gateway must continue to proxy these queries until the Django app is updated to use the new `/api/search` response format. Beyond that, no backward compatibility with the old static `membership` document type is maintained — the `MembershipDoc` schema and all associated indexing/lookup code are removed outright.

### 13b. Relationship to consolidateBoundaries plan

Complementary, with one supersession:
- H3 fields (§3a) are new additions alongside the `hull` and `bounds` fields from that plan.
- AAT enrichment (§8) extends the AAT lookup helper from that plan.
- The `geom` field in `geometries[]` (retained in that plan) is **replaced** here by `has_geom` + external VAST geometry store (§16). The consolidateBoundaries plan's `enrich_geometry()` helper must be updated accordingly.
- Both plans share the assumption of a full re-ingestion.

### 13c. Relationship to v4 graph model

This design is a **materialised, weighted place similarity graph with query-time projection and client-side subgraph clustering**. It maps directly to a native graph model:

- `place_graph` pairwise docs → graph edges with scored evidence.
- `place_graph` neighbor docs → materialised adjacency lists (a performance cache over the edge set).
- `places` docs → graph nodes with facet properties.
- In v4 (ArangoDB or similar), pairwise docs become `relates_to` attestation edges, neighbor docs become traversal results from AQL queries, and client-side clustering transfers unchanged — it operates on a projected subgraph regardless of the server's storage engine.
- A formal **graph schema (node/edge model + scoring invariants)** should be defined before v4 migration begins, making the transition mechanical rather than interpretive.

---

## 14. Migration Path

### Phase A — Schema augmentation and geometry externalization (during re-ingestion)

1. Remove `geom` (`geo_shape`) from `geometries[]` in `schemas/places.json`; add `has_geom` (`boolean`).
2. Add `h3_centroid`, `h3_cover` to `places` schema.
3. Add `aat_id`, `aat_depth`, `aat_ancestors` to `places.types[]`.
4. Implement the VAST geometry store: pack file writer, index builder, LRU cache reader (§16).
5. Update `enrich_geometry()` to write full geometry to VAST and set `has_geom` on the ES doc.
6. Compute all new fields during authority re-ingestion.
7. Update gateway geometry-serving logic to read from VAST when `geom: "full"` is requested.

### Phase B — Offline pipeline adaptation and calibration

1. Add `NeighborDoc` schema and `neighbors` doc type to `place_graph` index (renamed from `clusters`).
2. Implement alias-aware toponym scoring in `composite_score()`: max pairwise Symphonym cosine similarity across all names (§9f).
3. Adapt Phase 4 to produce neighbor docs instead of membership docs.
4. Run weight calibration (§9g) using hard-link ground truth to derive empirically grounded default facet weights.
5. Run a full graph build pass with calibrated weights.
6. Verify neighbor graph quality (spot-check known co-referent places, including alias-heavy cases like "Big Apple" / "New York").

### Phase C — Gateway integration

1. Add `build_neighbor_lookup()` helper.
2. Add neighbor expansion step (Step 3c) to the search endpoint.
3. Extend `SearchResponse` with edges, per-hit clustering signals, `query_match`, and response-level `query_emb`.
4. Add Symphonym embedding extraction in the enrichment step: for each hit, build the `query_match` object from the discovery-time toponym match (name, score, and base64-encoded int8 embedding). Compute `query_emb` from the original query string.
5. Add server-side fallback clustering (optional `cluster_threshold` parameter), including the query-bridge rule (§6h).
6. Verify response payloads are within size budget.

### Phase D — Client-side implementation

> **See `plan-dynamicClusteringUI.prompt.md`** (§5, Phase D) — client-side clustering JS, UI changes, and Django thin-proxy changes are managed in the `whg3` project. [Stored locally on the `atlas` branch at /home/stephen/Documents/GitHub/whg3/plan-dynamicClusteringUI.prompt.md]

### Phase E — Cleanup and documentation

1. Remove `cluster_id` / `cluster_size` from the old `SearchHit` model.
2. Remove `build_cluster_lookup()` from `gateway/es_helpers.py`.
3. Remove `group_by_cluster` parameter from `ReconcileRequest` and all downstream code.
4. Update `CLAUDE.md`, `CLUSTERS.md`, `developer/search-system-architecture.md`, `README.md` (§18c).
5. OpenRefine migration guide and API changelog — see `plan-dynamicClusteringDocumentation.prompt.md`.
6. Publish API changelog (§18b).

---

## 15. Dependencies

- **h3-py** (`h3`): Python H3 library for cell computation at ingestion time. Already available via pip; add to project dependencies.
- **No new database software**: no PostGIS, no Redis, no FAISS. ES remains the search/index layer. The VAST filesystem provides the geometry store. The chunked WKB format (§16) requires only Shapely (already a project dependency) for serialization/deserialization — no GDAL or fiona.

---

## 16. External Geometry Store (VAST Filesystem)

### 16a. Motivation

Full GeoJSON geometries (`geo_shape`) are the largest single contributor to ES document size — continental polygons, complex administrative boundaries, and multipolygon relations can be tens of KB each. Storing them in ES inflates shard sizes, slows retrieval, and wastes heap on fields rarely needed during search. Moving full geometries to the VAST filesystem (`/vast/ishi/`) eliminates this overhead while keeping them accessible for precise containment checks when needed.

### 16b. Storage format

Use a **chunked-binary archive** with a JSON index, **spatially sharded** for query-time locality:

```
/vast/ishi/geom/
  ├── index.json              # { geom_key: { file, offset, length } }
  ├── geom_shard_0001.bin     # packed WKB geometries, spatially grouped
  ├── geom_shard_0002.bin
  └── ...
```

The index maps geometry key → `{file, offset, length}`, enabling O(1) lookups. WKB serialization/deserialization uses Shapely (already a project dependency) — no GDAL, fiona, or other external binary libraries required.

**Spatial sharding** (not authority-based): geometries are assigned to shard files based on their H3 centroid cell at a coarse resolution (e.g. r3 or r4). This means geometries from different authorities but in the same geographic region share a shard file. The benefit is critical at query time: a typical search result set spans multiple authorities (GeoNames + Wikidata + OSM for the same region), so authority-based packing would require random reads across many files per request. Spatial packing aligns file I/O with the query's geographic locality — a search for places in France reads predominantly from the France-region shards, regardless of which authority produced each record.

During ingestion, a two-pass strategy handles this: first pass writes geometries to temporary per-authority files (matching the sequential authority ingestion order); a post-ingestion consolidation step re-packs them into spatially-sharded files sorted by H3 centroid and writes the final `index.json`.

This format is chosen over FlatGeobuf because the primary access pattern is batch ID lookup (not spatial query), the write pattern benefits from incremental append during ingestion, and the multi-geometry-per-place model maps naturally to composite index keys. A FlatGeobuf export can be generated as a derivative artifact if direct GIS tool interoperability is needed.

### 16c. Ingestion pipeline

In `processing/helpers.py` `enrich_geometry()`, change the output structure:

1. Compute `repr_point`, `hull`, `bounds`, `h3_centroid`, `h3_cover` as before — these go into the ES document.
2. Serialize the full GeoJSON geometry to WKB and write it to a temporary per-authority staging file, recording `{geom_key, h3_centroid, offset, length}` in a staging index.
3. Set `has_geom: true` on the ES geometry entry.
4. Do **not** include the full geometry in the ES document.

For point-only geometries (no polygon/line supplied by the authority):

1. `repr_point` is the only spatial field in ES.
2. `has_geom: false` — no external geometry exists.
3. `hull` and `bounds` are omitted or trivially derived from the point.

After all authorities are ingested, run a **consolidation step** that:

1. Reads all staging files and sorts entries by `h3_centroid` (coarse resolution, e.g. r3).
2. Writes spatially-grouped shard files (`geom_shard_NNNN.bin`), splitting at a configurable shard size (e.g. 256 MB).
3. Writes the final `index.json` mapping each `geom_key` → `{file, offset, length}`.
4. Deletes temporary staging files.

### 16d. Query-time containment pipeline

When the gateway needs to perform precise containment checks (e.g. "is this place inside boundary X?"):

1. **H3 prefilter** — ES `terms` query on `h3_cover` eliminates ~90% of candidates.
2. **Bbox rejection** — check `bounds` array against the query bbox in-memory (trivial).
3. **Hull containment** — point-in-polygon on `hull` (stored in ES as `geo_shape`), rejecting false positives from H3 (important for thin/concave geometries).
4. **Full geometry** — for surviving candidates only, load the geometry from VAST via the index, deserialize WKB, and perform exact containment with Shapely.

Because the candidate set after steps 1–3 is typically small (tens of records), the VAST reads are fast and bounded.

### 16e. LRU cache

Add an in-memory LRU cache (a few hundred MB) for recently-loaded geometries on the gateway process. Hot regions (e.g. European administrative boundaries frequently used in spatial filters) become effectively in-memory after the first access.

### 16f. Gateway geometry serving

The existing `POST /api/search` and `POST /api/places` endpoints return `geometries[]` on each hit. Under the new design:

- **Default** (`geom: "repr_point"`): return only `repr_point` from ES. No VAST access. Fast.
- **Full** (`geom: "full"`): after the ES query, batch-load full geometries from VAST for all surviving place_ids where `has_geom: true`. Include them in the response as GeoJSON. This replaces the previous pattern of reading `geom` from the ES `_source`.
- **Hull** (`geom: "hull"`): return `hull` from ES without VAST access. Useful for lightweight map display of approximate shapes.

### 16g. Relationship to tileset generation

The standalone tileset generator (`processing/generate_tiles.py` from the consolidateBoundaries plan) reads boundary geometries to produce `.mbtiles`. Under this design, it reads from the VAST geometry store rather than from ES `_source`, making it independent of the ES index entirely.

### 16h. Attestation-level geometry

Different authorities may assert different geometries for the same real-world place, potentially at different times. The `places.geometries[]` array already supports multiple entries (one per authority), each carrying its own `timespans`. The external geometry store preserves this: each geometry entry in `geometries[]` has its own `has_geom` flag, and the VAST index is keyed by `{place_id}_{geometry_index}` (or equivalently, a deterministic ID derived from place_id + authority namespace). In v4, when geometry attaches to attestation nodes rather than abstract places, the VAST store migrates unchanged — only the index keys change.

---

## Further Considerations

1. **Formal graph schema.** Before v4 migration, define a formal node/edge model with scoring invariants (e.g. "edge weights are symmetric", "hard links are never dropped below threshold", "signal components sum to composite score under default weights"). This document would make the ES → graph DB migration mechanical.

2. **Minimum spanning forest per component (optional improvement).** The current approach (thresholded connected components) has a known weakness: chaining (A~B, B~C ⇒ A~C even when A~C is weak). The post-processing split (§6f) partially mitigates this. A stronger alternative: after building each component, keep only the strongest edges forming a minimum spanning tree, then prune weak bridges. This removes spurious chaining without heavy computation. Not required for v1, but worth evaluating if chaining proves problematic in practice.

3. **High-frequency toponym stoplist.** The synthetic phonetic edge pass (§6i Rule A) requires a stoplist of generic place-name tokens to prevent catastrophic merging. This list should be derived empirically from the `toponyms` index (e.g. top 100 tokens by attestation count across all authorities). Initial candidates: "Central", "Station", "Market", "Church", "School", "Main", "Park", "New", "San", "Saint", "North", "South", "East", "West", "Old", "National", "Grand", "Royal", "Great". The stoplist is maintained as a server-side configuration and included in the search response metadata so the client can apply it during synthetic edge construction.

---

## 17. Front-End UI Changes

> **Moved to `plan-dynamicClusteringUI.prompt.md`** — the front-end UI, client-side clustering JS, and Django thin-proxy changes are managed in the `whg3` project. That document includes all necessary context (response payload format, client-side algorithm, UI specifications).

---

## 18. Documentation Changes

### 18a. OpenRefine / Reconciliation API documentation

> **Moved to `plan-dynamicClusteringUI.prompt.md`** (§4a) — the Django-side API code (`api/crc_client.py`, reconciliation endpoint) is managed in the `whg3` project. The OpenRefine integration guide is in `plan-dynamicClusteringDocumentation.prompt.md` (§1).

### 18b. API changelog

Document the following breaking changes in the gateway API changelog:

1. `POST /api/reconcile`: parameter `group_by_cluster` removed; replaced by `cluster_threshold: float | null`.
2. `POST /api/search`: response now includes `edges` array and per-hit `phon_emb`, `h3`, `temporal_range`, `baseline_cluster_id` fields. New optional parameter `cluster_threshold: float | null`.
3. `POST /api/places`: response `geometries[]` no longer includes `geom` (full geometry). Request `geom: "full"` to have the server load full geometries from VAST. New geometry modes: `"repr_point"` (default), `"hull"`, `"full"`.
4. ES index `clusters` renamed to `place_graph`. Internal change — not directly exposed to API consumers, but affects any tooling that queries the index directly.

### 18c. Internal documentation updates

| Document | Changes |
|----------|---------|
| `CLAUDE.md` | Update Gateway Architecture section: add `place_graph` index, `phon_emb` in hit payload, `cluster_threshold` parameter, VAST geometry store. Remove references to `clusters` index `membership` doc type. Update index table (add `place_graph`, remove `clusters`). |
| `CLUSTERS.md` | Major rewrite or replacement — the document currently specifies static membership clustering. Replace with a description of the similarity graph architecture, neighbor docs, and query-time projection model. |
| `developer/search-system-architecture.md` | Update §4 to describe the new Step 3c (neighbor expansion) and the clusterable response format. Update §2.7 to reflect facet filtering over clustered results. Remove references to `group_by_cluster`. |
| `README.md` | Update the architecture diagram and index table. |
| `gateway/reconcile.py` docstring | Update to describe `cluster_threshold` replacing `group_by_cluster`. |

OpenRefine integration guide documentation is in `plan-dynamicClusteringDocumentation.prompt.md` (§1).

