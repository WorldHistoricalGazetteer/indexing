# Plan: Layered Co-Reference Recovery with Server-Side Dynamic Clustering and Optional Client-Side Reactivity

## Introduction

WHG v3.2 presents search results as a flat list of individual place records — one per authority per real-world place. A search for "Paris" returns separate entries from GeoNames, Wikidata, OSM, TGN, and Pleiades, forcing the user to mentally deduplicate dozens of records that all refer to the same city. The current v3.5 clustering pipeline partially addresses this by precomputing fixed equivalence clusters and storing static membership assignments in a `clusters` ES index, but this approach has fundamental limitations: a single scoring threshold is baked in, the grouping is binary rather than tunable, the similarity model is opaque, and the cost of maintaining a 47M-record materialised pairwise graph is high and brittle in the face of re-ingestion.

An earlier iteration of this plan attempted to address these limitations by retaining the offline pairwise graph but deferring the final clustering decision to query time, with a per-query subgraph extracted from a precomputed adjacency-list store and a UI threshold slider driving Union-Find on the client. That design was workable but carried substantial machinery: top-K neighbour docs per place, edge symmetrisation logic, baseline cluster precomputation, an enriched `place_graph` index, and a careful payload architecture shipping per-edge signal breakdowns to the browser.

This revision proposes a substantially simpler architecture, arrived at through a sequence of observations: that Symphonym's role at *discovery* time already collapses cross-script and orthographic variants into shared retrieval neighbourhoods, so co-reference recovery is largely a discovery problem rather than a post-discovery clustering problem; that the residual cases where co-referents fail to surface together (historical-name pairs, rebrandings, sparse cross-lingual records) can be addressed by a user-triggered second-phase toponym expansion that uses the toponyms index itself as the implicit similarity graph; that authority-asserted equivalence is qualitatively different from inferred similarity and deserves separate treatment; and that contributor-proposed clustering, fed back into the reconciliation store, lets the platform improve over time by the route of letting researchers say what they know rather than the route of inferring it from weak signals.

The result is a layered architecture in which each mechanism handles a specific slice of the co-reference problem with no overlapping responsibility:

- **Symphonym discovery** retrieves candidates including phonetic, transliteration, and orthographic variants of the query, ensuring that cross-script and cross-orthographic co-referents reach the result set together rather than separately.
- **Hard-link expansion** pulls in authority-asserted equivalents (sameAs, exactMatch, contributor reconciliation) for places already in the result set, recovering cases where co-referents share no toponym but are linked by explicit assertion.
- **Server-side dynamic clustering** groups the result set at query time using available per-hit signals (toponym similarity, spatial proximity, temporal overlap, AAT type similarity, link intersection), with the clustering threshold driven by user choice and the scoring cached for reactive slider control.
- **User-triggered Phase 2 toponym expansion** broadens the candidate pool when the user is dissatisfied with Phase 1 results or wishes to experiment, by issuing follow-up Symphonym searches for every toponym co-attested at any Phase 1 hit.
- **User-proposed clustering** captures the contested residue: where automatic mechanisms have not grouped two records that the researcher recognises as co-referent, the user can propose a link, which enters the contributor reconciliation store and becomes a hard link for future queries.

The materialised pairwise graph and the neighbour-doc machinery are removed entirely. The offline pipeline shrinks to hard-link harvesting (Phases 1A and 1B from the prior design) and Symphonym index maintenance, both of which are cheap and re-ingestion-tolerant. The architecture pre-figures the v4 graph-database migration rather than diverging from it, since query-time clustering with a thin hard-links overlay maps directly onto graph traversal in ArangoDB.

Simultaneously, this plan retains the geometry, H3, and AAT enrichment work from the prior design: full geometries move out of ES into the VAST filesystem, H3 indexing supports fast spatial blocking, and AAT type metadata enables type-similarity scoring. These changes stand on their own merits independently of how clustering is performed and are described in §10–§12 below.

---

## Summary of Changes

Replace the static `clusters` index and the precomputed similarity graph with a much smaller hard-link overlay and query-time scoring. Discovery and clustering both run at query time, with Symphonym handling cross-script and orthographic variation at the discovery stage and dynamic clustering operating over the resulting candidate set. A user-triggered Phase 2 broadens the candidate pool when needed; user-proposed clustering captures the contested residue. Geometry, H3, and AAT changes from the prior plan are retained.

---

## Core Architecture

```
Offline pipeline (batch)
  ├── Phase 1A: Authority hard links (relations with sameAs/closeMatch/exactMatch)
  ├── Phase 1B: Contributor reconciliation links (WHG Django PostgreSQL)
  └── Hard-links overlay maintained as a small, re-ingestion-tolerant view

Query time (server)
  ├── Step 1: Discovery (toponyms index — Symphonym KNN or BM25)
  ├── Step 2: Hard-link expansion (pull in authority-asserted equivalents)
  ├── Step 3: Filtering + aggregations (places index)
  ├── Step 4: Pair scoring within result set (H3-blocked, signal-decomposed)
  ├── Step 5: Server-side Union-Find at requested threshold
  └── Step 6: Return clustered results (and optionally edge list for reactive UI)

User-triggered Phase 2 (server, opt-in)
  ├── Collect toponym union of Phase 1 hits
  ├── Issue expanded discovery search across union
  ├── Re-cluster combined Phase 1 + Phase 2 set
  └── Return expanded clustered results

Client (browser)
  ├── Default: receive clustered results, display
  ├── Reactive option: receive scored edges, run Union-Find on slider change
  ├── User actions: trigger Phase 2, propose links, unpropose links
  └── Proposed links flow back to reconciliation store
```

---

## 1. Discovery: Symphonym at the Front

Discovery is the layer that does the most work for co-reference, and the architecture relies on it doing so. Symphonym's phonetic embedding space collapses transliteration and orthographic variants into tight retrieval neighbourhoods, which means that a query in any script or spelling tends to surface co-referent records carrying any of the equivalent forms.

A query for "Beijing" retrieves records carrying "北京", "Peking", "Pékin", "Pequim", and other phonetically near-equivalent forms, because all of them sit close to "Beijing" in the embedding space. A query for "Москва" retrieves records carrying "Moscow", "Moscou", "Moskau", "Mosca", for the same reason. This is precisely the cross-script case that an offline pairwise-similarity graph would otherwise have to identify and store; Symphonym makes it a property of retrieval rather than a property of stored similarity.

The cases Symphonym discovery does *not* handle by itself are those where the bridging name is not phonetically related to the query: "Lutèce" and "Paris" are not phonetic neighbours, and a query for either retrieves only records carrying that specific name or its phonetic neighbours. "Constantinople" and "Istanbul" are similarly disjoint. These cases require either hard-link expansion (§2) or user-triggered Phase 2 expansion (§5) to bring the co-referents into a shared result set.

### 1a. Discovery returns

The discovery step returns the top N hits ranked by Symphonym query relevance (or BM25 for exact-match queries), with N capped at a configurable limit (default 200; see §3 for the rationale on this cap). Each hit carries the per-hit metadata described in §4 below.

### 1b. No changes to the toponyms index

The toponyms index continues to store Symphonym int8 embeddings and BM25 text fields as in the current v3.5 design. The Symphonym extensions for the 20+ languages already covered remain in place.

---

## 2. Hard-Link Expansion

Hard links are authority-asserted equivalences: `sameAs`, `closeMatch`, `exactMatch` relations between place records, plus contributor reconciliation links from the WHG PostgreSQL store. They differ from inferred similarity in kind, not just in degree: they represent ground-truth identity claims made by gazetteer maintainers or contributing scholars, and they are citable, revertible, and version-tracked. The architecture treats them as first-class identity assertions rather than as one signal among many.

### 2a. Offline harvesting

The existing Phase 1A and Phase 1B logic from the prior offline pipeline is retained:

- **Phase 1A** harvests authority `sameAs`/`closeMatch`/`exactMatch` relations from the `places` index `relations[]` field.
- **Phase 1B** harvests contributor reconciliation decisions from the WHG Django PostgreSQL database, keyed on contributor place_id pairs.

The output is a flat hard-links table, indexed by place_id, with each entry listing the place_ids asserted to be equivalent (with provenance: which authority or contributor made the assertion, and at what confidence). This is small (orders of magnitude smaller than the prior pairwise-graph index) and cheap to rebuild from source on re-ingestion.

### 2b. Storage

Hard links are stored as a new lightweight `hard_links` document type in the ES `place_graph` index (renamed from `clusters` for semantic clarity, even though the index now holds only this overlay). Each doc:

```json
{
  "doc_type": "hard_links",
  "place_id": "wd:Q90",
  "namespace": "wd",
  "links": [
    {"place_id": "gn:2988507", "type": "sameAs", "source": "wikidata"},
    {"place_id": "tgn:7008038", "type": "exactMatch", "source": "wikidata"},
    {"place_id": "whg:contrib:abc123", "type": "sameAs", "source": "contributor:user42"}
  ],
  "created_at": "..."
}
```

No scoring, no signal breakdown, no symmetrisation logic beyond ensuring both endpoints have entries pointing at each other. The `pairwise` and `neighbors` document types from the prior design are removed.

### 2c. Query-time expansion

After the discovery step (§1), the gateway fetches the hard-link docs for all result-set place_ids in a single ES `terms` query. The union of linked place_ids, deduplicated against the existing result set, identifies places to pull in as expanded hits. The `places` documents for these are fetched (with the same metadata enrichment as discovery hits) and added to the response.

A cap on hard-link expansion size (e.g. result-set size + 50%) prevents pathological cases where a heavily-linked record (a famous city with sameAs assertions to dozens of authorities) inflates the result set unboundedly. Expansion proceeds in priority order: contributor links first (they represent explicit human judgement), then authority `sameAs`, then `exactMatch`/`closeMatch`. Truncation, if it occurs, drops the weakest authority assertions first.

### 2d. Provenance carried through to the response

Each expanded hit is flagged with its expansion provenance: `via_hard_link: {source: "wikidata", linked_from: "wd:Q90"}`. This lets the UI display the record with appropriate context (e.g. "added because Wikidata asserts this is the same place as a hit in your results") and lets the user revert a problematic link if the assertion turns out to be wrong.

---

## 3. Result-Set Sizing

A central design decision in this revision is to cap the result set more tightly than the prior plan envisaged. The rationale is straightforward: discovery is assumed to do its job well, meaning that the top-N hits represent the strongest matches for the query, and the marginal value of hits beyond rank N falls off rapidly in interactive search use cases. A tighter cap reduces clustering compute, payload, and visual clutter, while sacrificing little of substance for the dominant use case.

### 3a. Default cap

The default discovery cap is **200 hits**, plus up to 50% expansion via hard links, giving a maximum result-set size of ~300 records before clustering. This compares favourably to the prior plan's 500-hit budget while remaining well above the threshold at which research-meaningful results typically exhaust themselves.

### 3b. Reconciliation path

Programmatic reconciliation (OpenRefine, scripted ETL) sometimes needs deeper recall than interactive search. The reconciliation endpoint accepts an explicit `result_limit` parameter overriding the default, with a hard ceiling at 500 to bound server cost. Reconciliation requests at the higher cap pay correspondingly more in scoring time but operate on the same code path.

### 3c. User-triggered Phase 2

Phase 2 expansion (§5) can grow the result set further by pulling in records sharing toponyms with Phase 1 hits. The Phase 2 cap is set independently and defaults to doubling the Phase 1 size: a 200-hit Phase 1 result with Phase 2 triggered yields up to ~400 records before clustering (200 from Phase 1, plus up to ~100 from hard-link expansion, plus up to ~100 from Phase 2 expansion, with overall dedup).

---

## 4. Per-Hit Payload

Each hit returned from the gateway carries the following metadata, regardless of whether clustering is performed server-side, client-side, or in a hybrid arrangement:

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
  "ccodes": ["FR"],
  "names": [
    {"toponym": "Paris", "lang": "fr", "timespans": [...]},
    {"toponym": "Parigi", "lang": "it", "timespans": [...]},
    {"toponym": "Lutèce", "lang": "fr", "timespans": [...]}
  ],
  "types": [
    {"identifier": "PPLC", "label": "capital", "aat_id": 300008347, "aat_depth": 6, "aat_ancestors": [300387554, 300387552, 300236157]}
  ],
  "links": ["wd:Q90", "tgn:7008038"],
  "geometries": [...],
  "discovery_match": {
    "name": "Paris",
    "score": 0.93
  },
  "via_hard_link": null
}
```

Notes:

- `names[]` carries the full toponym set, capped at the top-12 toponyms per hit, prioritised by attestation count and temporal-span breadth. The toponym set supports server-side cross-product scoring during the pair-scoring pass (§6a).
- `discovery_match` records which toponym caused the hit and the discovery score, allowing the UI to highlight what matched without ambiguity.
- `via_hard_link` is non-null on hits added by hard-link expansion (§2c), carrying the link source and the originating in-result place_id.
- `aat_ancestors` enables Wu-Palmer type similarity computation server-side during pair scoring; the values are also returned to the client to support facet reweighting under the reactive option (§7b).
- `h3` and `h3_cover` enable spatial blocking for the server-side pair-scoring pass.
- No embedding data is shipped to the client by default. The optional embedding-shipping configuration described in §7c is available for clients that prefer fully reactive client-side scoring.

The total payload per hit is approximately 1 KB compressed, giving a 200-hit response of around 200 KB before the optional edge list is added.

---

## 5. User-Triggered Phase 2 Toponym Expansion

Phase 2 broadens the candidate pool by issuing follow-up Symphonym searches for every toponym attested at any Phase 1 hit, then re-clustering the combined set. It is **not** automatic: it runs only on explicit user request, either as a result-set-wide expansion or as a per-cluster "find more like this" action. The rationale for making it user-triggered rather than automatic is laid out in the conversation that produced this plan: as an automatic step it had to justify its latency cost on every query, but as a deliberate user action it pays its cost only when the user wants the broader recovery, removing the need for predictive heuristics.

### 5a. Mechanism

When the user triggers Phase 2:

1. The gateway collects the toponym union across the relevant scope (whole result set, or single cluster, or single hit), capped at top-K toponyms per place (default 12) and deduplicated globally.
2. The gateway issues a batched Symphonym search across the toponym union, retrieving the top-M hits per toponym (default M = 20), deduplicated against the existing result set.
3. The combined Phase 1 + Phase 2 result set is re-scored and re-clustered (§6).
4. The expanded clustered result is returned, with Phase 2 members visually distinguished from Phase 1 members in the UI.

### 5b. Two granularities

**Result-set-wide expansion.** Triggered by an "expand search" affordance in the search results header. Operates on the toponym union of all Phase 1 hits. Appropriate for cases where the user suspects their query terminology is too narrow ("I searched for Lutèce but I want everything that goes with this set of records").

**Per-cluster expansion.** Triggered by a "find more like this" affordance on a specific cluster or hit. Operates on the toponym set of that cluster only. Appropriate for focused investigation ("I have the Wikidata Paris record, what else is co-referent with it specifically?"). Runs faster than the result-set-wide version because the toponym union is smaller.

### 5c. Scope and bounding

To prevent pathological expansion:

- The toponym union is capped at the top-K toponyms per place (default 12), prioritised by attestation count and temporal breadth.
- The expansion search returns at most M hits per toponym (default 20), with a global cap on Phase 2 additions (default equal to the Phase 1 size).
- A small stoplist of high-frequency generic tokens ("Central", "Station", "Market", "Church", "School", "Main", "New", "San", "Saint", "North", "South", "East", "West") is excluded from the toponym union to prevent expansion via uninformative names. The stoplist is empirically derived from toponym frequency in the toponyms index (see §17f).

### 5d. Phase 2 hits in the response

Phase 2 hits carry a `via_phase_2: {triggered_from: "wd:Q90", matched_toponym: "Lutèce"}` flag, parallel to `via_hard_link`. The UI renders them with a distinguishing visual treatment (subtle border, icon, or section heading) so the user can see at a glance which records are direct query matches and which arrived via expansion. This preserves epistemic transparency: the user always knows why a record is in the result set.

### 5e. Latency

Phase 2 adds approximately 100–200 ms to the query-response cycle: the toponym-union search runs as a single batched ES query across the deduplicated toponym set (typically 1500–2500 distinct toponyms for a 200-hit Phase 1), and re-clustering is fast. Because Phase 2 is user-triggered, the user expects and tolerates this additional latency; the request is naturally accompanied by a "expanding..." indicator. Pipelining (issuing the toponym-union search while the UI is still presenting the user's choice) can shave this further but is not essential.

### 5f. Diagnostic logging

Phase 2 triggers are logged in aggregate with their queries, providing a free signal about where Phase 1 (Symphonym + hard links) is under-recovering co-references. Repeated triggers in a particular subdomain (medieval Latin toponyms, say) indicate areas where Symphonym coverage or hard-link density would benefit from improvement. The logs feed into a review dashboard for the WHG team and inform decisions about authority enrichment priorities.

---

## 6. Server-Side Dynamic Clustering

Clustering runs at query time on the server, over the result set assembled by discovery (§1) plus hard-link expansion (§2) plus optionally Phase 2 expansion (§5). The threshold is set by request parameter; the user controls it via slider in the UI.

### 6a. Pair scoring

For each pair of places in the result set that passes spatial blocking (shared H3 cell at r7, or intersecting `h3_cover` at r5), the gateway computes a composite similarity score as a weighted sum of per-facet signals:

| Signal | Description | Computation |
|--------|-------------|-------------|
| `s.n` | Toponym similarity | Maximum Symphonym int8 cosine over the cross-product of toponym embeddings between the two places. The toponym embeddings are already in the toponyms index from discovery; the gateway loads them for the result-set toponyms in a single batched fetch and scores in-process. |
| `s.sp` | Spatial similarity | Function of `repr_point` haversine distance, with `h3_cover` overlap as bonus signal for places with full geometries |
| `s.t` | Temporal similarity | Interval overlap between `temporal_range` ranges, normalised |
| `s.ty` | Type similarity | Wu-Palmer over `aat_ancestors` arrays |
| `s.l` | Link similarity | 1.0 if the pair appears in the hard-links overlay; otherwise 0 |

The composite score is `S = w_n·s.n + w_sp·s.sp + w_t·s.t + w_ty·s.ty + w_l·s.l`, with default weights `w_n=0.30, w_sp=0.25, w_t=0.10, w_ty=0.10, w_l=0.25`. Null facets (e.g. missing temporal data) are handled by proportional weight redistribution: the null facet's weight is redistributed among the non-null facets, preserving the `[0, 1]` range of the composite.

The pair-scoring pass is bounded by H3 blocking: for a 200-hit result set with H3 r7 blocking, candidate pairs typically number in the low hundreds rather than the 19,900 of an unblocked O(n²) pass. Total scoring time is a few tens of milliseconds.

### 6b. Toponym scoring via Symphonym embeddings

Symphonym int8 embeddings are already produced and stored in the toponyms index for discovery purposes. The pair-scoring pass reuses them: for each candidate place pair, the gateway loads the embeddings of all toponyms attested at both places (a batched fetch keyed by toponym IDs) and computes the maximum cosine similarity across the cross-product. For 200 hits with up to 12 toponyms per place, the toponym pool is bounded at ~2400 vectors, the loads are coalesced into a single ES fetch, and the cosine arithmetic is cheap (int8 dot products on 128-dimensional vectors, dominated by load latency rather than computation).

The advantage of this approach over surface-form string similarity is that Symphonym handles cross-script and cross-orthographic variation natively. A Cyrillic "Москва" and a Latin "Moscow" are near-equivalent in Symphonym's phonetic embedding space, despite sharing zero characters, so the toponym signal correctly fires on cross-script pairs without any transliteration step. The same applies to historical-spelling variants, diacritic differences, and transliteration differences across authorities.

### 6c. Union-Find at threshold

After scoring, Union-Find runs over all pairs with composite score `S >= θ`, where θ is the request-supplied threshold (default 0.85). Hard links are unioned unconditionally as a bootstrapping pass before threshold-based unions, ensuring that `s.l = 1.0` pairs are always merged regardless of θ. The clustering is order-independent because Union-Find is commutative.

### 6d. Cluster-size limiting

To prevent mega-clusters in dense urban regions, clusters with more than `N_max` members (default 50) are split as a post-processing step: the threshold is tightened iteratively within the cluster until it fragments into sub-clusters all `<= N_max`, or until θ reaches 0.95 (at which point the cluster is accepted as genuinely co-referent). Hard-link edges are never cut during splitting.

### 6e. Cluster representation

Each cluster is returned with:

- `cluster_id`: synthetic identifier for this query-clustering pass (not persistent across queries).
- `members[]`: the place_ids in the cluster.
- `representative`: the place_id of the highest-scoring or preferred-authority member, used for compact cluster display.
- `aggregated`: union of names, types, ccodes, temporal range, authorities across members.

### 6f. Reconciliation determinism

The reconciliation endpoint uses the same code path as interactive search, with default weights and a default threshold. Determinism is automatic because the scoring is deterministic, Union-Find is order-independent, and the splitting post-process operates on a fixed input. No separate "fallback clustering" code path is needed; the primary clustering is the reconciliation clustering.

### 6g. Server-side pair-score caching

The most expensive part of server-side clustering is the pair-scoring pass (§6a). To support reactive slider-driven reclustering without repeatedly re-scoring, the gateway caches the scored pair list keyed on `(query, filters, result_set_hash)`. Cache hits skip directly to Union-Find at the new threshold, which runs in milliseconds. Cache eviction is LRU with a modest memory budget (a few hundred MB). Threshold changes that reuse a cached result complete in roughly one network round-trip with negligible server work, making slider control responsive even without client-side reclustering.

---

## 7. Client-Side Reactive Reclustering: Three Options

The slider-driven reactivity that motivated the prior plan's heavy client-side architecture remains a desirable property, but it can be achieved in three different ways with different cost-benefit profiles. The architecture supports any of them; the choice can be made independently for different deployment scenarios (interactive web UI vs. reconciliation API vs. embedded widgets) or revisited as usage data emerges.

### 7a. Option A — Server round-trip per slider change (debounced)

The simplest approach: every slider movement triggers a debounced gateway request with the new threshold. With server-side pair-score caching (§6g), the server-side work is negligible (Union-Find only) and the round-trip is dominated by network latency.

**Cost profile:** ~150–300 ms per slider rest position (debounce + round-trip + Union-Find + render). UI shows a "computing..." indicator during the gap.

**Pros:** Simplest implementation, no client-side scoring logic, no payload inflation beyond the clustered result. Determinism is automatic. Works for non-JS clients (the threshold is just a request parameter). Suitable for the reconciliation API.

**Cons:** Slider drag is not smooth; only rest positions update. May feel sluggish on high-latency networks (mobile, geographically distant clients).

**Implementation:** Add a `cluster_threshold` parameter to the search request. The client debounces slider changes (default 200 ms) and re-issues the search. The gateway hits the pair-score cache and returns the re-clustered result.

### 7b. Option B — Ship scored edges, run Union-Find client-side

The middle option: the server computes scored pairs server-side as in Option A, but ships the scored-pair list to the client alongside the clustered result. Subsequent threshold changes run Union-Find in the browser over the cached pairs, with no round-trip.

**Edge payload structure** (extending §4's per-hit payload with a top-level `edges` array):

```json
{
  "edges": [
    {"a": "gn:2988507", "b": "wd:Q90", "score": 0.95,
     "s": {"n": 0.98, "sp": 0.92, "t": 0.85, "ty": 1.0, "l": 1.0}},
    {"a": "gn:2988507", "b": "osm:n12345", "score": 0.87,
     "s": {"n": 0.90, "sp": 0.95, "t": null, "ty": 0.78, "l": 0.0}}
  ]
}
```

Each edge carries the composite `score` plus the per-facet signal breakdown `s`. The client re-runs Union-Find at the new threshold instantly; with the signal breakdown, the client can also offer **facet reweighting** ("prioritise spatial proximity") by recomputing the composite score with user-supplied weights before applying the threshold.

**Cost profile:** Edge payload of ~200–250 KB compressed for a 200-hit result set with H3-blocked pairs. Client-side Union-Find on threshold change: <10 ms. Total slider response: instantaneous.

**Pros:** Truly reactive slider; no round-trips during interaction. Facet reweighting available without server work. Determinism is preserved because both server and client use the same scoring (the server scored once; the client only re-thresholds).

**Cons:** Modest payload increase. The client must implement Union-Find and the threshold-and-reweight logic, but these are small (~100 lines of JS). Not suitable for non-JS clients (which fall back to Option A).

**Implementation:** The gateway returns `edges[]` alongside the clustered result. The client renders the server-clustered result on initial load (so the initial display is correct without waiting for client-side computation), then runs its own Union-Find on subsequent slider changes.

### 7c. Option C — Ship embeddings, full client-side scoring (advanced)

The most reactive option: the server ships per-hit toponym embeddings to the client, and the client computes pair scores from scratch as well as Union-Find. This decouples the client from the server's scoring choices entirely, allowing arbitrary reweighting (including changing what "toponym similarity" means by, say, switching from string similarity to phonetic-embedding cosine).

**Per-hit embedding bundle:** Each hit's `names[]` entries are augmented with `phon_emb` (Symphonym 128-d int8 embedding, base64-encoded, ~185 bytes per toponym). With a cap of top-12 toponyms per place, this adds approximately 2.2 KB per hit, or ~440 KB compressed across a 200-hit result set.

**Cost profile:** Larger payload (total ~600–700 KB compressed for a 200-hit result with embeddings). Client-side scoring: a few thousand toponym-pair cosine computations, ~10–50 ms in JS. Total slider + reweight response: still well under 100 ms.

**Pros:** Maximum client-side flexibility. Enables on-the-fly switching between scoring methods. Facet reweighting can include reweighting *within* the toponym component (e.g. weighting phonetic similarity vs string similarity). Useful for power-user research interfaces and for embedded widgets that may want to experiment with scoring without server changes.

**Cons:** Larger payload. Duplicates scoring logic between server and client (must stay synchronised). Determinism becomes harder to guarantee (floating-point cosine across browsers may produce minor variation). Not suitable for the reconciliation API or non-JS clients.

**Implementation:** A request flag `include_embeddings: true` triggers the embedding-shipping path. Without this flag, hits do not carry embeddings and the client falls back to Option B or Option A.

### 7d. Recommended default

**Option A is the recommended default for the interactive search UI**, with debounced slider control and server-side pair-score caching providing acceptable reactivity at minimal complexity. Option B is offered as a configurable upgrade for clients that want smoother slider control or facet reweighting; the server work is identical and the only difference is whether the edges are shipped to the client or consumed internally. Option C is available for power-user contexts but is not part of the default interactive experience.

This phased approach lets the implementation prioritise getting Option A working reliably, then add Option B as a refinement once the core architecture is solid, then evaluate Option C based on actual user demand.

### 7e. Determinism guarantees by option

| Option | Determinism for reconciliation | Determinism for interactive UI |
|--------|-------------------------------|-------------------------------|
| A | Strong (server is single source of truth) | Strong |
| B | Strong (server-shipped edges are deterministic; client only re-thresholds) | Strong |
| C | Approximate (cross-browser floating-point variation) | Acceptable |

Reconciliation requests always use Option A's path regardless of the interactive-UI choice, ensuring deterministic and reproducible reconciliation output.

---

## 8. User-Proposed Clustering

The platform exposes "propose a link" and "propose unlinking" as first-class user affordances within the search and cluster-display UI. The mechanism captures the contested residue that automatic mechanisms cannot adjudicate: cases where a researcher recognises co-reference (or non-co-reference) that the system has not detected (or has incorrectly detected).

### 8a. UI affordances

**Propose a link.** When the user is viewing a cluster or a flat list of results, they can select two records and assert that they refer to the same place. A small dialog captures an optional justification (citation, note, contextual evidence) and submits the assertion.

**Propose unlinking.** When viewing a cluster, the user can flag a member as not belonging — asserting that the system has incorrectly clustered this record with the others. The same justification dialog applies.

Both affordances are equally prominent in the UI: the platform does not treat one direction (asserting identity) as more privileged than the other (asserting non-identity). This is deliberate, because in historical place research, distinguishing co-located but distinct places (a chapel within a parish, a market site within a town) is as important as identifying co-referents.

### 8b. Storage

User proposals enter the WHG Django PostgreSQL contributor reconciliation store, the same store that Phase 1B already harvests. New proposals join the existing data model:

- Asserted-same: `{place_a, place_b, type: "sameAs", contributor, justification, timestamp}`.
- Asserted-distinct: `{place_a, place_b, type: "distinct", contributor, justification, timestamp}`.

The asserted-distinct relation is new: the prior schema only carried positive identity assertions. Adding negative assertions lets the platform record "these are not the same place" in a form that subsequent clustering can respect.

### 8c. Application in clustering

Both relations participate in §6's clustering:

- Asserted-same entries flow through Phase 1B harvesting into the hard-links overlay (§2). They become unconditional unions in §6c.
- Asserted-distinct entries become **hard splits**: pairs that may not be unioned regardless of edge score. The Union-Find pass is augmented with a hard-split check before each union: if `(a, b)` is asserted-distinct, the union is skipped. If a transitive chain (`a~b`, `b~c`) would put an asserted-distinct pair in the same cluster, the cluster is split during the post-processing step (§6d), with the splitting algorithm preferring to honour the asserted-distinct constraint at the cost of weaker edges within the would-be component.

### 8d. Provenance and review

User proposals are visible to other users with appropriate provenance: cluster-display tooltips can show "linked by user X (justification: ...)" or "split by user Y" so subsequent researchers can evaluate the assertion. A review queue in the WHG admin interface lets the team curate user proposals, accepting them as canonical hard links, demoting them to advisory, or removing them in cases of error.

### 8e. Composability with the layered architecture

User-proposed clustering composes cleanly with the other layers:

- A proposed-same assertion that survives review becomes a hard link, and Symphonym discovery + hard-link expansion (§1, §2) automatically apply it on future queries without re-clustering.
- A proposed-distinct assertion is recorded once and persistently honoured, preventing the system from repeatedly making the same clustering error.
- Phase 2 expansion (§5) respects user assertions: if a Phase 2 candidate is asserted-distinct from any Phase 1 hit, it is still pulled in (the user may want to inspect it) but not unioned in clustering.

The result is that the platform learns from researcher input over time, with the offline pipeline shrinking even further in conceptual scope: it just harvests what users (and authorities) have asserted, without inferring anything more. Inference is the responsibility of the query-time scoring pass, which is transparent and reproducible because it operates on shipped or shippable signals rather than precomputed scores.

---

## 9. Schema Changes

### 9a. `places` index (`schemas/places.json`)

Within the existing `geometries[]` nested object:

- **Remove** the `geom` field (`geo_shape`). Full geometries are no longer stored in ES (see §10).
- **Add** `has_geom` (`boolean`, default `false`) — when `true`, indicates that a full geometry for this entry exists on the VAST filesystem.
- **Retain** `repr_point` (`geo_point`), `hull` (`geo_shape`), `bounds` (`float` array), `timespans` (nested).

Add new top-level fields:

- `h3_centroid` (`keyword`) — H3 cell at reference resolution for the representative point.
- `h3_cover` (`keyword`, multi-valued) — compacted H3 coverage cells.

Within the existing `types[]` nested object, add:

- `aat_id` (`integer`) — mapped AAT concept ID.
- `aat_depth` (`integer`) — AAT hierarchy depth.
- `aat_ancestors` (`integer`, multi-valued) — materialized ancestor set.

No changes to the `names[]` (or `toponyms[]`) nested object beyond what already exists in v3.5: the surface forms, language tags, and timespans are sufficient. Symphonym embeddings are already maintained in the separate `toponyms` index and are loaded from there during the server-side pair-scoring pass (§6b).

### 9b. `place_graph` index (replaces `clusters`)

Renamed from `clusters` to `place_graph` for semantic clarity. The index contains a single document type:

**Hard-link docs:**

- `doc_type`: `"hard_links"` (keyword)
- `place_id`: the place this overlay belongs to (keyword)
- `namespace`: extracted from place_id (keyword)
- `links`: object array, each `{place_id, type, source}` — symmetrised at ingestion so each pair appears in both endpoints' docs.
- `created_at`: timestamp.

The `pairwise`, `neighbors`, and `membership` document types from prior designs are removed.

### 9c. `toponyms` index — unchanged

Symphonym embeddings and attestation arrays remain as in the current v3.5 design.

### 9d. `types` index — unchanged

AAT depth, ancestors, and path are already stored. The new `aat_*` fields on `places.types[]` are derived from this index at ingestion time (§11).

### 9e. WHG PostgreSQL — schema extension

The contributor reconciliation table gains a `relation_type` column with values `sameAs` (existing semantics) or `distinct` (new, per §8b). Existing rows default to `sameAs` on migration.

---

## 10. External Geometry Store (VAST)

Full geometries move out of ES into a chunked WKB store on the VAST filesystem (`/vast/ishi/`), replacing the `geom` field with a `has_geom` boolean and a stacked prefilter pipeline (H3 → bbox → hull → VAST read) for precise spatial operations. This section is largely unchanged from the prior plan and is reproduced here for completeness.

### 10a. Storage layout

```
/vast/ishi/geometries/
  ├── index.json                 # geom_key → {file, offset, length}
  ├── geom_shard_0001.bin
  ├── geom_shard_0002.bin
  └── ...
```

Each shard is a binary file of concatenated WKB-encoded geometries. The index maps a geometry key (deterministic ID derived from `place_id` + authority namespace + geometry index) to a `(file, offset, length)` tuple.

### 10b. Ingestion-time write path

For each geometry in a place's `geometries[]` array:

1. If the geometry is point-only, skip the VAST write; `has_geom` remains `false`.
2. Otherwise serialise to WKB and append to a per-authority staging file, recording `{geom_key, h3_centroid, offset, length}` in a staging index.
3. Set `has_geom: true` on the ES geometry entry.
4. Do not include the full geometry in the ES document.

After all authorities are ingested, run a consolidation step that sorts staging entries by `h3_centroid` (coarse resolution, e.g. r3), writes spatially-grouped shard files (`geom_shard_NNNN.bin`, splitting at a configurable shard size such as 256 MB), writes the final `index.json`, and deletes temporary staging files.

### 10c. Query-time containment pipeline

When precise containment is needed (e.g. "is this place inside boundary X?"):

1. **H3 prefilter.** ES `terms` query on `h3_cover` eliminates ~90% of candidates.
2. **Bbox rejection.** Check `bounds` array against the query bbox in-memory.
3. **Hull containment.** Point-in-polygon on `hull` (stored in ES as `geo_shape`).
4. **Full geometry.** For surviving candidates, load the geometry from VAST via the index, deserialise WKB, and perform exact containment with Shapely.

### 10d. LRU cache

An in-memory LRU cache (a few hundred MB) on the gateway process holds recently-loaded geometries. Hot regions (frequently-queried administrative boundaries) become effectively in-memory after first access.

### 10e. Gateway geometry serving

The `POST /api/search` and `POST /api/places` endpoints support three geometry modes:

- **`geom: "repr_point"`** (default): return only `repr_point` from ES.
- **`geom: "hull"`**: return `hull` from ES.
- **`geom: "full"`**: load full geometries from VAST for surviving hits where `has_geom: true`.

### 10f. Tileset generation

The standalone tileset generator reads boundary geometries from the VAST store rather than from ES `_source`, making it independent of the ES index entirely.

---

## 11. AAT Type Enrichment at Ingestion

(Largely unchanged from the prior plan.) The `processing/aat_lookup.py` module loads AAT mappings; for each place's `types[]` entry, it queries the `types` ES index for the corresponding `aat_id` to retrieve `depth` and `ancestors`, and stores `aat_id`, `aat_depth`, `aat_ancestors` alongside the existing identifier and label.

Unmapped types (common for Wikidata and OSM) have null `aat_*` fields. The clustering scorer treats unmapped types as neutral: the type signal contributes 0 with redistributed weight, the same null-handling rule that applies to other facets.

The `apply_aat_mappings_to_index()` function supports bulk-updating `types[]` entries on existing place docs when new AAT mappings are added via the Django mapping UI, avoiding re-ingestion.

---

## 12. H3 Implementation Details

(Largely unchanged from the prior plan.)

### 12a. Computing H3 at ingestion

In `processing/helpers.py` `enrich_geometry()`:

- **Point geometry:** `h3.latlng_to_cell(lat, lon, resolution=7)` for centroid; cover = [centroid].
- **Polygon/MultiPolygon:** centroid cell as above; `h3.polygon_to_cells(polygon, resolution)` + `h3.compact_cells(cells)` for cover. Adapt resolution: start at r7, drop to r5 if polyfill yields >10,000 cells, drop to r3 if still too many.
- **LineString:** buffer to small polygon, then polyfill.

### 12b. Resolution strategy

| Resolution | Hex edge ~km | Use case |
|------------|-------------|----------|
| r3 | ~69 km | Continental blocking |
| r5 | ~8 km | Regional blocking |
| r7 | ~1.2 km | Typical place clustering |
| r9 | ~0.17 km | Dense urban disambiguation |

Default `h3_centroid` uses r7. The `h3_cover` field contains compacted cells naturally spanning multiple resolutions.

### 12c. Query-time usage

- **Spatial blocking in search.** When a `bounds` GeoJSON filter is provided, compute H3 cover of the bounds and add a `terms` filter on `h3_cover` as a fast prefilter before any `geo_shape` intersects query.
- **Resolution adaptation.** The query-side cover uses a coarsest-appropriate resolution by bbox extent (>2000 km → r3, 200–2000 km → r5, 20–200 km → r7, <20 km → r9).
- **Clustering blocking.** Server-side pair-scoring (§6a) uses H3 to limit pair enumeration to spatially proximate candidates only.

---

## 13. API Endpoint Changes

### 13a. `POST /api/search` (and `POST /api/reconcile`)

**New parameters:**

- `cluster_threshold: float` (default 0.85) — clustering threshold θ.
- `facet_weights: {n, sp, t, ty, l}` (optional) — override default weights for the composite score.
- `phase_2: boolean | object` (default false) — trigger Phase 2 expansion. When an object, scope to a specific cluster: `{scope: "cluster", cluster_id: "..."}` or `{scope: "all"}`.
- `include_edges: boolean` (default false) — return `edges[]` for client-side reactive reclustering (Option B, §7b).
- `include_embeddings: boolean` (default false) — return Symphonym embeddings per toponym (Option C, §7c). Mutually compatible with `include_edges`.
- `result_limit: int` (default 200, max 500) — discovery cap.

**Response additions:**

- `clusters[]`: list of cluster objects per §6e.
- `edges[]` (when requested): scored pair list per §7b.
- `phase_2_metadata` (when triggered): scope, toponym union size, expansion count.
- Each hit gains `via_hard_link` and `via_phase_2` provenance flags (§4, §5d).

**Removed parameters:**

- `group_by_cluster` (replaced by `cluster_threshold`).

### 13b. `POST /api/places`

Geometry mode parameter (`geom`) per §10e. No clustering parameters (this endpoint serves single records).

### 13c. `POST /api/links` (new)

For user-proposed clustering (§8). Accepts:

```json
{
  "place_a": "...",
  "place_b": "...",
  "type": "sameAs" | "distinct",
  "justification": "..." (optional)
}
```

Authenticated. Records the proposal in the contributor reconciliation store. Future queries automatically respect the assertion via §2 (sameAs) or §6c augmented with hard-split logic (distinct).

### 13d. `GET /api/embed`

Unchanged from the prior plan: returns the Symphonym int8 embedding for an arbitrary string. Used by the client for "compare name variant" workflows when embeddings are not shipped per-hit.

---

## 14. Front-End UI Changes

A separate document (`plan-dynamicClusteringUI.prompt.md` in `whg3`) covers UI implementation in detail. Key UI elements implied by this plan:

- **Threshold slider** in the search results panel, controlling `cluster_threshold`. Debounced (Option A) or reactive (Option B) per the deployment choice.
- **Facet weight sliders** (optional) for emphasising name vs spatial vs temporal vs type vs link similarity. Available with Option B or C.
- **"Expand search" button** in the results header, triggering Phase 2 result-set-wide expansion (§5b).
- **"Find more like this" affordance** on individual clusters and hits, triggering Phase 2 per-cluster expansion (§5b).
- **"Propose link" / "Propose split" affordances** in cluster detail views (§8a).
- **Provenance indicators** on hits: distinguishing direct query matches from hard-link expansions (§2d) and Phase 2 expansions (§5d).
- **User-proposal review queue** in the WHG admin interface (§8d).

---

## 15. Documentation Changes

### 15a. API changelog

Document the following changes in the gateway API changelog:

1. `POST /api/reconcile`: parameter `group_by_cluster` removed; replaced by `cluster_threshold: float`. New parameter `result_limit: int`.
2. `POST /api/search`: new parameters `cluster_threshold`, `facet_weights`, `phase_2`, `include_edges`, `include_embeddings`, `result_limit`. Response gains `clusters[]`, optional `edges[]`, `phase_2_metadata`, per-hit `via_hard_link` and `via_phase_2`.
3. `POST /api/places`: `geom` parameter modes `"repr_point"` (default), `"hull"`, `"full"`. Geometries served from VAST when `"full"` is requested.
4. `POST /api/links`: new endpoint for user-proposed clustering.
5. ES index `clusters` renamed to `place_graph`. Index now contains only `hard_links` document type; `pairwise`, `neighbors`, and `membership` types are removed.

### 15b. Internal documentation updates

| Document | Changes |
|----------|---------|
| `CLAUDE.md` | Update Gateway Architecture: add discovery-stage Symphonym role, hard-links overlay, server-side dynamic clustering, Phase 2 expansion, user proposals. Remove references to precomputed pairwise graph and neighbour docs. Update index table. |
| `CLUSTERS.md` | Major rewrite. Replace static-membership and adjacency-list-store descriptions with the layered architecture: discovery, hard-link expansion, server-side dynamic clustering, Phase 2, user proposals. Document the three reactivity options (§7). |
| `developer/search-system-architecture.md` | Update to describe query-time clustering at the gateway, hard-link overlay, Phase 2 expansion. Remove references to neighbour expansion, pairwise scoring storage, baseline cluster precomputation. |
| `README.md` | Update architecture diagram and index table. |
| `gateway/reconcile.py` docstring | Document `cluster_threshold`, `result_limit`, and the shared code path with interactive search. |

OpenRefine integration guide updates are in `plan-dynamicClusteringDocumentation.prompt.md`.

---

## 16. Migration and Retirement

### 16a. Retired infrastructure

The following components from the prior offline pipeline and v3.5 design are retired:

- `clustering/scoring.py` composite-score persistence (scoring logic remains, but now runs at query time rather than offline).
- `clustering/clusters.py` connected-components and DBSCAN sub-clustering for offline membership generation.
- ES `clusters` index `pairwise`, `neighbors`, `membership` document types.
- The neighbour-doc symmetrisation and degree-truncation logic.
- The baseline-cluster precomputation step.
- The client-side synthetic-edge passes (Phase 2a/2b in the prior plan): no longer needed because the discovery + hard-link + Phase 2 + user-proposal layers cover the same recovery cases more transparently.

### 16b. Retained infrastructure

The following components are retained and continue under the new architecture:

- Symphonym index, training pipeline, and language-extension work.
- `clustering/phase1_hard_links.py` (Phase 1A and 1B harvesting).
- AAT type enrichment pipeline (`processing/aat_lookup.py`, `apply_aat_mappings_to_index()`).
- H3 indexing in `processing/helpers.py`.
- VAST geometry store and gateway containment pipeline.
- Contributor reconciliation store in WHG Django PostgreSQL (extended per §9e).

### 16c. Migration sequence

1. Schema changes (§9): deploy new fields on `places` and rename `clusters` to `place_graph`. Run hard-link harvesting against the new schema.
2. AAT enrichment backfill (§11): populate `types[].aat_*` fields.
3. H3 backfill (§12): populate `h3_centroid` and `h3_cover`.
4. VAST geometry migration (§10): write existing geometries to VAST, set `has_geom`, remove `geom` from ES.
5. Gateway: deploy new clustering endpoint logic with Option A reactivity. Confirm reconciliation determinism on representative test queries.
6. Front-end: deploy threshold slider, Phase 2 affordance, user-proposal UI.
7. Optional later: Option B (edge shipping) for smoother slider control.
8. Optional much later: Option C (embedding shipping) if power-user demand emerges.

---

## 17. Further Considerations

### 17a. Empirical validation of the discovery-completeness assumption

The architecture rests substantially on the claim that Symphonym discovery brings co-referent records into the result set together for the dominant query patterns. This is plausible but should be measured: take a sample of queries from production logs, run them under the new architecture, and count how often expected co-referents appear in Phase 1 vs require Phase 2 vs require hard-link expansion vs are missed entirely. The measurement informs whether the result-set cap (§3a) and the Phase 2 trigger threshold need adjustment, and identifies subdomains where hard-link density is insufficient.

### 17b. Phase 2 default scoping

The user-triggered model (§5) leaves the choice of result-set-wide vs per-cluster expansion to the user. Usage data may indicate that one scope is the dominant choice and could become a default with the other available as an "expand differently" option. Worth deferring until usage data exists.

### 17c. Asserted-distinct splitting algorithm

The hard-split logic in §8c is sketched but not specified in detail. If a chain `a~b~c` would put an asserted-distinct pair `(a, c)` in the same cluster, the algorithm must decide which edge to cut. A reasonable heuristic: cut the weakest edge in the chain that, when removed, separates the asserted-distinct endpoints. Multiple cuts may be needed if multiple asserted-distinct constraints apply. Worth specifying once enough asserted-distinct relations exist to test against, which will not be initially.

### 17d. Phase 2 proximity to v4

Phase 2's toponym-expansion mechanism is structurally similar to a graph traversal: starting from Phase 1 hits, follow toponym-attestation edges to reach co-referent records. In v4, this becomes an explicit graph traversal in ArangoDB rather than a batched ES search, with potentially better performance characteristics and richer query expressiveness. The v3.5 implementation is deliberately a simpler version that does not pre-figure graph-DB query language but produces equivalent results, easing the eventual transition.

### 17e. Performance budget for the H3 pair-scoring pass

§6a estimates "a few tens of milliseconds" for pair scoring on a 200-hit result set. This is plausible but unverified. A microbenchmark before production deployment, using realistic toponym counts and AAT ancestor depths, would confirm the estimate and surface any unexpected costs (e.g. AAT ancestor-set intersections at very deep nodes, or string-similarity degradation on long toponyms). The Symphonym discovery latency dominates total query time even in the worst case for pair scoring, so there is some headroom, but it is worth measuring.

### 17f. Stoplist maintenance

The Phase 2 stoplist (§5c) needs occasional review as the toponyms index grows. A periodic batch job (quarterly, perhaps) recomputes top-frequency tokens and proposes additions to the stoplist for review. The stoplist is small (a few dozen entries) and its contents are noncontroversial; the maintenance burden is light.