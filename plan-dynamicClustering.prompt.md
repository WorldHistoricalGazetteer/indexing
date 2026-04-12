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

### 2d. Optionally precompute baseline clusters

Run connected components at a **high threshold** (e.g. 0.9) offline to identify near-certain identity groups. Store as a lightweight `baseline_cluster_id` on each neighbor doc. These provide instant grouping for obvious matches and a starting layer for client-side refinement (initial unions in the Union-Find).

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
- Each `SearchHit` gains: `h3` (string), `temporal_range` ([start, end] or null), `baseline_cluster_id` (str or null), `phon_emb` (base64-encoded 128-byte int8 Symphonym embedding for the best-matching toponym).
- Per-hit `aat_ids` and `aat_depths` are available for display (type-tree widget, tooltips) but are not used for client-side similarity — type similarity is precomputed in the edge signal breakdown.

### 4c. Suggest endpoint — no change

Typeahead remains lightweight and does not involve clustering.

### 4d. Reconcile endpoint (`POST /api/reconcile`)

Same adaptation as search: add optional neighbor expansion and edge emission. The existing `group_by_cluster` parameter (from CLUSTERS.md §2.3) is replaced by client-side grouping, but the server can still pre-group at a default threshold for non-JS consumers.

### 4e. Server-side fallback clustering

For API consumers that cannot do client-side clustering (e.g. OpenRefine, programmatic access), the server applies a default threshold (e.g. θ = 0.85) to the local subgraph and returns pre-grouped results. This reuses the same Union-Find logic, implemented in Python on the gateway. The `SearchRequest` model gains an optional `cluster_threshold: float | None` parameter; when set, the server clusters and returns grouped results.

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
  "temporal_range": [-500, 2026],
  "aat_ids": [300008347],
  "aat_depths": [6],
  "baseline_cluster_id": "c_abc123",
  "phon_emb": "<base64-encoded 128-byte int8 vector>",
  "names": [...],
  "ccodes": ["FR"],
  "types": [...],
  "geometries": [...]
}
```

The `phon_emb` field carries the Symphonym embedding for the place's best-matching toponym (the one that triggered the discovery hit). This is the same 128-d int8 vector stored in the `toponyms` index. Base64 encoding keeps the payload compact (128 bytes → 172 characters). The client uses this for phonetic re-scoring (§6g).

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

- ~500 results × ~470 bytes ≈ 235 KB (hits including base64 phon_emb at ~172 bytes each)
- ~2000 edges × ~120 bytes ≈ 240 KB (edges with signal breakdown)
- Total: ~475 KB before gzip, ~100–150 KB compressed — within budget.

For result sets > 500, cap the edges to top-scoring pairs and/or restrict clustering to the top N results.

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

This approach keeps all expensive similarity computation server-side (in the offline pipeline), while giving the client cheap, instant re-weighting with no server round-trip. The client never recomputes spatial distances, temporal overlaps, or AAT LCA depths — it only applies weight coefficients to precomputed normalised scores.

### 6b. Comparison pruning

Only compare pairs that have a precomputed edge. This avoids O(n²) explosion:

- The server already prunes to the local subgraph (edges between surviving results).
- Additional client-side blocking: same H3 cell, or shared authority link, or same baseline cluster.
- For ~500 results with ~2000 edges, clustering is O(n) — trivially fast.

### 6c. Union-Find with threshold

```
for each edge (a, b, signals) sorted by reweighted_S descending:
    S = Σ w_i · signals[i]    // reweight with current UI sliders
    if S >= θ:
        union(a, b)
```

Properties:
- As θ increases, clusters split (monotonic for fixed weights).
- Pre-sorting edges by current weights allows progressive application — slider updates do not require recomputation from scratch (only re-sort + re-apply, still O(E log E)).
- Union-Find is near-linear and runs in <10 ms for 500 nodes.

### 6d. Baseline cluster bootstrapping

Before applying the user threshold, initialize the Union-Find with baseline clusters (if present): for all results sharing a `baseline_cluster_id`, union them. This provides instant grouping for obvious matches (e.g. GeoNames + Wikidata for the same city) before the user even touches the slider.

### 6e. Cluster display

Each cluster gets:
- **Representative**: highest-scoring hit (or preferred-authority heuristic).
- **Aggregated metadata**: all names across members, all authorities, temporal span union, types union.
- **Expandable**: user can expand a cluster to see individual member records.

### 6f. Cluster-size damping

Union-Find can collapse aggressively at low θ, producing "mega-clusters" in dense urban regions (e.g. every "Paris" record in one group). Safeguard: during the union pass, refuse to merge two components if the resulting cluster would exceed a configurable maximum size (e.g. N = 50) **unless** the edge score exceeds a high-confidence threshold (e.g. 0.9) or the edge originates from a hard link (authority sameAs). This prevents runaway merging while still allowing genuinely co-referent large clusters to form.

### 6g. Client-side phonetic re-scoring

Each hit carries a `phon_emb` field: the Symphonym 128-d int8 embedding for the place's best-matching toponym. The client can use these to let the user type an alternative name variant and instantly see how phonetically close it is to every result — without a server round-trip.

**Flow:**

1. User types a variant in a "Compare name" input (e.g. "Parigi").
2. The client calls `GET /api/embed?name=Parigi` on the gateway, which returns the Symphonym int8 embedding for the query string (fast — single model inference, ~5 ms).
3. The client computes cosine similarity between the query embedding and each hit's `phon_emb` in JavaScript. Int8 dot product on 128 dimensions is trivially fast (~0.01 ms per pair).
4. Results are re-ranked or highlighted by phonetic proximity to the user's variant.

This enables cross-script and cross-transliteration name comparison directly in the browser — a researcher can type a name in Arabic script and see which Latin-script results are phonetically closest, or compare a medieval spelling variant against modern authority records.

The `phon_emb` vectors also support a secondary use: when two results lack a precomputed edge (because neither appeared in the other's top-K neighbors), the client can compute an ad-hoc phonetic similarity between them using their embeddings. This fills gaps in the precomputed graph for long-tail cases.

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
- Add `baseline_cluster_threshold: float = 0.9` to `ScoringConfig`.
- Add `weight_temporal: float = 0.10` (temporal interval overlap, computed during edge scoring using flattened timespans from the `places` index).
- Retain all existing thresholds — they still govern which pairs enter the graph.

### 9f. `clustering/scoring.py`

- Extend `composite_score()` to compute and return **per-facet normalised signal components** alongside the composite score: `{n, sp, t, ty, l}`.
- Add temporal similarity computation: interval overlap (Jaccard-like) between the flattened timespan unions of each place. Null when either place lacks timespans.
- These signal components are stored on each pairwise doc and propagated to neighbor docs for client-side facet-weight scaling.

### 9g. Weight calibration (`clustering/calibration.py`)

The existing `calibration.py` tunes scoring thresholds using positive/negative pair sampling (Phase 1A hard links as positive pairs, random cross-authority pairs as negatives). Extend it to also **derive optimal facet weights** for the combined similarity function:

1. Using the same positive/negative pair sets, compute per-facet signal components for each pair.
2. Fit a logistic regression (or similar lightweight model) to find the weight vector `[w_n, w_sp, w_t, w_ty, w_l]` that best separates positive pairs (true co-referents) from negatives.
3. Output the calibrated weights to `clustering/config.py` as the default facet weights.
4. These calibrated defaults become the initial weights both for the offline composite score and for the client-side default slider positions.

This is done during the pipeline build (Phase B), not deferred. The calibration data already exists (hard links provide ground truth); the only addition is fitting weights alongside thresholds. Running calibration before the first full graph build ensures that the similarity graph and the client-side defaults use empirically grounded weights from day one.

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

### 12a. Too many results (> 2000)

Client-side clustering degrades beyond ~2000 results due to edge volume. Mitigations:
- Cap clustering to top N results (e.g. 500); remaining results are ungrouped.
- Server-side fallback: apply default threshold and return pre-grouped results.

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
2. Adapt Phase 4 to produce neighbor docs instead of membership docs.
3. Run weight calibration (§9g) using hard-link ground truth to derive empirically grounded default facet weights.
4. Run a full graph build pass with calibrated weights.
5. Verify neighbor graph quality (spot-check known co-referent places).

### Phase C — Gateway integration

1. Add `build_neighbor_lookup()` helper.
2. Add neighbor expansion step (Step 3c) to the search endpoint.
3. Extend `SearchResponse` with edges, per-hit clustering signals, and `phon_emb`.
4. Add Symphonym embedding extraction in the enrichment step: for each hit, retrieve the best-matching toponym's int8 embedding and base64-encode it.
5. Add server-side fallback clustering (optional `cluster_threshold` parameter).
6. Verify response payloads are within size budget.

### Phase D — Client-side implementation

1. Remove the "Group linked records" toggle from the Data Sources panel.
2. Implement `clustering.js` module: Union-Find, edge reweighting, threshold application, `cosineSimilarity()`, `decodePhonEmb()`.
3. Implement threshold slider (§17b) with debounced re-clustering.
4. Implement facet emphasis controls (§17c), collapsed by default.
5. Implement phonetic comparison input (§17d) with `/api/embed` integration.
6. Bootstrap with baseline clusters (§6d).
7. Add cluster expansion/collapse UI (§17e).
8. Update result-facet filters to operate over clustered results (§17f).
9. Replace feature-class checkboxes with type facets (§17g).
10. Tune default weights and threshold using calibrated defaults from the server.

### Phase E — Cleanup and documentation

1. Remove `cluster_id` / `cluster_size` from the old `SearchHit` model.
2. Remove `build_cluster_lookup()` from `gateway/es_helpers.py`.
3. Remove `group_by_cluster` parameter from `ReconcileRequest` and all downstream code.
4. Update `CLAUDE.md`, `CLUSTERS.md`, `developer/search-system-architecture.md`, `README.md` (§18c).
5. Write OpenRefine migration guide: `group_by_cluster` → `cluster_threshold` (§18a).
6. Publish API changelog (§18b).

---

## 15. Dependencies

- **h3-py** (`h3`): Python H3 library for cell computation at ingestion time. Already available via pip; add to project dependencies.
- **h3-js**: Client-side H3 if needed for spatial blocking in-browser (optional — the server already provides H3 cell IDs).
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

---

## 17. Front-End UI Changes (Django Search Page)

The search page (`/search/`, template `search/templates/search/search.html`, JS in `whg/webpack/js/search.js`) requires significant changes to support dynamic clustering.

### 17a. Remove: "Group linked records" toggle

The current "Group linked records" checkbox in the **Data Sources** panel is a binary switch that triggers server-side static clustering via `group_by_cluster: true` on the reconcile request. This toggle is removed entirely — it is replaced by the continuous similarity slider (§17b) which provides strictly more functionality.

**Files affected:**
- `search/templates/search/search.html` — remove the checkbox element and its label from the Data Sources panel.
- `whg/webpack/js/search.js` — remove the `group_by_cluster` parameter from `gatherOptions()` and from the AJAX payload construction.
- `api/crc_client.py` (Django thin proxy) — stop forwarding `group_by_cluster` to the gateway.

### 17b. Add: Similarity threshold slider

A continuous slider (θ ∈ [0,1]) in the results panel controls clustering sensitivity. Position it prominently above the result list, with a label such as "Group similar places" and a tooltip explaining the behaviour.

| Slider position | Effect |
|----------------|--------|
| θ = 1.0 (rightmost) | No grouping — flat list identical to current behaviour |
| θ = 0.8 (default) | Conservative grouping — high-confidence co-referents only |
| θ = 0.5 | Moderate grouping — phonetically similar + spatially proximate |
| θ = 0.0 (leftmost) | Aggressive grouping — all connected results merged (subject to cluster-size damping) |

**Behaviour:**
- Moving the slider triggers client-side re-clustering (§6c) with no server round-trip.
- Debounce at ~100 ms to avoid flicker during drag.
- The result list re-renders with clustered/unclustered grouping.
- The map updates: clustered places share a marker group or are connected by visual links.
- Persist the slider position in `sessionStorage` so it survives page navigation.

### 17c. Add: Facet emphasis controls (optional, collapsible)

Below the threshold slider, an expandable "Similarity tuning" section exposes per-facet weight sliders:

| Slider | Default | Controls |
|--------|---------|----------|
| Name similarity | 0.30 | w_n — toponym match weight |
| Spatial proximity | 0.25 | w_sp — geographic distance weight |
| Temporal overlap | 0.10 | w_t — timespan overlap weight |
| Type match | 0.10 | w_ty — AAT type similarity weight |
| Authority links | 0.25 | w_l — shared cross-authority ID weight |

Weights are normalised to sum to 1.0 in real time. Moving any slider re-triggers the Union-Find pass with the new weight vector. A "Reset to defaults" button restores the calibrated defaults from the server response.

This section is collapsed by default for casual users and expanded for power users / researchers.

### 17d. Add: Phonetic comparison input

A small input field in the results panel labelled "Compare name variant" or similar. When the user types a name:

1. Debounce at 300 ms.
2. Call `GET /api/embed?name=<input>` to obtain the Symphonym embedding.
3. Compute cosine similarity against each result's `phon_emb`.
4. Display a phonetic proximity indicator (e.g. colour-coded badge or numeric score) next to each result.
5. Optionally re-sort results by phonetic proximity to the typed variant.

This is particularly valuable for researchers working with historical or non-Latin-script name variants.

### 17e. Add: Cluster expansion/collapse UI

When clustering is active (θ < 1.0), the result list displays **cluster cards** instead of individual place cards:

- **Collapsed state** (default): shows the representative place (highest-scoring or preferred-authority member), a count badge ("3 sources"), and the aggregated name list.
- **Expanded state**: clicking the cluster card expands it to show all member places as sub-cards, each with its own authority badge, names, and metadata.
- **Map interaction**: clicking a cluster card zooms to the bounding box of all member geometries. Expanded members are shown as individual markers; collapsed clusters show a single marker at the representative's centroid.

### 17f. Update: Result-facet filters (post-search)

The existing client-side facet filters (§2.7 in search-system-architecture.md — Place Types checkboxes, Countries checkboxes) continue to work as before, but now operate on the **clustered** result set:

- A cluster is visible if **any** of its members passes the facet filter.
- The facet counts reflect unique clusters, not individual places (when clustering is active).
- Toggling a facet filter does not re-trigger clustering — it only shows/hides clusters in the already-computed grouping.

### 17g. Update: Feature-class checkboxes → Type facets

The legacy feature-class checkboxes (`A`, `P`, `S`, etc.) in `#adv_checkboxes` are already marked for replacement (see search-system-architecture.md §2.2). This plan accelerates that: replace them with the server-side type aggregation facets returned in the search response. The type facets use AAT identifiers and hierarchical labels from the `types` index, not GeoNames feature classes.

### 17h. Update: Data Sources panel

The existing Data Sources panel lists the authority namespaces available for filtering (GeoNames, Wikidata, OSM, etc.). Changes:

- **Remove** the "Group linked records" toggle (§17a).
- **Retain** the namespace inclusion/exclusion checkboxes — these feed `namespaces` / `exclude_namespaces` on the search request and remain useful.
- **Add** a small indicator per namespace showing the count of results from that source in the current (possibly clustered) result set.

### 17i. JavaScript implementation

The client-side clustering logic (Union-Find, edge reweighting, threshold application) should be implemented as a self-contained ES module (e.g. `whg/webpack/js/clustering.js`) with no external dependencies:

- `class UnionFind` — standard disjoint-set with path compression and union by rank.
- `function clusterResults(hits, edges, theta, weights)` — returns a `Map<clusterId, ClusterGroup>`.
- `function reweightEdge(edge, weights)` — computes the weighted sum from signal components.
- `function cosineSimilarity(a, b)` — int8 dot product for phonetic re-scoring.
- `function decodePhonEmb(base64)` — decode base64-encoded int8 embedding to `Int8Array`.

This module is imported by `search.js` and called on every slider change. It should be pure (no DOM manipulation) — it returns data structures that the rendering layer consumes.

---

## 18. Documentation Changes

### 18a. OpenRefine / Reconciliation API documentation

The WHG reconciliation service is used by OpenRefine users who cannot perform client-side clustering. The `POST /api/reconcile` endpoint must document the new `cluster_threshold` parameter:

**New parameter: `cluster_threshold`** (`float | null`, default `null`)

When set to a value between 0.0 and 1.0, the server performs Union-Find clustering on the result subgraph and returns grouped results. When `null` (default), results are returned as a flat list (backward-compatible with existing OpenRefine workflows).

Example request body:
```json
{
  "query": "Paris",
  "mode": "fuzzy",
  "cluster_threshold": 0.85
}
```

Example grouped response (additional to the flat `hits` list):
```json
{
  "clusters": [
    {
      "cluster_id": "c_abc123",
      "representative": { "place_id": "gn:2988507", "title": "Paris", ... },
      "members": [
        { "place_id": "gn:2988507", ... },
        { "place_id": "wd:Q90", ... },
        { "place_id": "osm:n12345", ... }
      ],
      "score": 0.95
    },
    ...
  ],
  "hits": [ ... ]
}
```

The flat `hits` list is always present for backward compatibility. The `clusters` list is populated only when `cluster_threshold` is set.

**Removed parameter: `group_by_cluster`** (`bool`)

The previous boolean toggle is removed. Users should migrate to `cluster_threshold` which provides the same functionality (use `cluster_threshold: 0.85` as equivalent to the old `group_by_cluster: true`) with the additional ability to control sensitivity.

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
| OpenRefine integration guide (if separate) | Document `cluster_threshold` parameter with examples. Provide migration guidance from `group_by_cluster: true` to `cluster_threshold: 0.85`. |

