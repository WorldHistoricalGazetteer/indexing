# WHG Place Clusters: Entity Resolution via Elasticsearch

**Status:** Specification draft — March 2026  
**Context:** Interim measure before v4 graph model migration  
**Repository:** `WorldHistoricalGazetteer/indexing`  
**Author:** Stephen Gadd / WHG Technical Direction

---

## 1. Problem Statement

The WHG `places` and `toponyms` Elasticsearch indices contain the full corpus of GeoNames, Wikidata, OSM, TGN, Pleiades, and other authority records. Many of these records refer to the same real-world place but exist as separate documents because they originate from different authorities. Currently:

- Some authority records already carry explicit `sameAs` links in their `relations` field (e.g. Pleiades → GeoNames, TGN → Wikidata). These are **hard links** — asserted by the authority itself.
- Users performing reconciliation searches receive a flat list of matching places, with no indication of which results are likely to denote the same entity. This creates noise and cognitive burden.
- The `toponyms` index already encodes implicit co-attestation: when two places from different namespaces share the same toponym record (via the `attestations` keyword array), that is evidence of potential identity.
- Symphonym phonetic embeddings (`embedding` field, 128-dim byte vectors) in the `toponyms` index enable cross-script and cross-transliteration similarity detection.

The goal is to **pre-compute equivalence clusters** across authority records so that search results can be returned pre-grouped, dramatically reducing noise for users. This also lays groundwork for v4 migration, where pairwise links become `relates_to` attestation edges with `sameAs_candidate` semantics.

---

## 2. Architecture Overview

### 2.1 New Index: `clusters`

A dedicated Elasticsearch index storing two kinds of document:

1. **Pairwise link documents** — one per scored pair of places believed to be the same entity.
2. **Cluster membership documents** — one per place, recording which cluster it belongs to.

These are separate document types within the same index, distinguished by a `doc_type` field. This avoids a separate cluster-membership lookup index while keeping pairwise evidence queryable.

### 2.2 Relationship to Existing Indices

```
toponyms index                    places index
  ├─ attestations[] ────────────► place_id
  ├─ embedding (128-dim)          ├─ namespace
  ├─ name / name_romanized        ├─ geometries[].repr_point
  └─ lang / script                ├─ types[].identifier
                                  ├─ ccodes[]
        ┌─────────────────────────├─ relations[] ◄── existing hard links
        │                         └─ links[]
        ▼
  clusters index (NEW)
    ├─ pairwise link docs (scored evidence)
    └─ membership docs (cluster_id per place)
```

### 2.3 Relationship to the Gateway Reconciliation Endpoint

The existing `/gateway/reconcile.py` (FastAPI, `POST /api/reconcile`) implements a three-step search: toponym discovery → place filtering → toponym enrichment. It returns a flat ranked list. **This endpoint should be adapted, not scrapped**, because:

- Its discovery logic (Symphonym KNN + BM25 fallback) is sound and reusable.
- Its filtering logic (spatial, temporal, ccode, namespace) is well-tested.
- What it lacks is **post-retrieval grouping by cluster**.

The recommended adaptation:

1. **After Step 2 (filtering)**, look up the `cluster_id` for each surviving `place_id` from the `clusters` index.
2. **Group hits by `cluster_id`** in the response, with a designated "best representative" per cluster (highest-scoring hit).
3. Add an optional `group_by_cluster: bool = True` parameter to `ReconcileRequest`.
4. Extend `ReconcileResponse` with a `clusters` field containing grouped results alongside the existing flat `hits` for backward compatibility.

If clusters prove sufficient for all reconciliation use cases, the flat-list mode can eventually be deprecated. But for now, maintain both.

### 2.4 Relationship to the v4 Graph Model

In v4 (ArangoDB), pairwise links become:
- **Attestation nodes** with `certainty` scores and `certainty_note` recording the algorithm version.
- **Edges** of type `relates_to` referencing an Authority document where `authority_type: "relation_type"` and `label: "sameAs"` (or `"sameAs_candidate"` for soft links vs `"sameAs"` for hard links).

Cluster IDs become **derived views** — computed by graph traversal over `sameAs` / `sameAs_candidate` edges rather than stored as static assignments.

The `clusters` index is therefore a **transitional structure**. Design it so that migration means: iterate pairwise link docs → create attestation nodes + edges → discard cluster membership docs (they become traversal results).

---

## 3. `clusters` Index Schema

```json
{
  "settings": {
    "number_of_shards": 2,
    "number_of_replicas": 0,
    "refresh_interval": "30s",
    "index": {
      "codec": "best_compression"
    }
  },
  "mappings": {
    "properties": {
      "doc_type": {
        "type": "keyword",
        "doc_values": true
      },

      "_comment_pairwise": "Fields for doc_type=pairwise",

      "place_id_a": {
        "type": "keyword"
      },
      "place_id_b": {
        "type": "keyword"
      },
      "namespace_a": {
        "type": "keyword"
      },
      "namespace_b": {
        "type": "keyword"
      },
      "score": {
        "type": "float"
      },
      "link_class": {
        "type": "keyword",
        "doc_values": true
      },
      "link_method": {
        "type": "keyword",
        "doc_values": true
      },
      "signals": {
        "type": "object",
        "enabled": true,
        "properties": {
          "toponym_exact_count": { "type": "integer" },
          "toponym_symphonym_max": { "type": "float" },
          "spatial_distance_km": { "type": "float" },
          "type_match": { "type": "boolean" },
          "ccode_overlap_count": { "type": "integer" },
          "shared_link_ids": { "type": "keyword" }
        }
      },

      "_comment_membership": "Fields for doc_type=membership",

      "place_id": {
        "type": "keyword"
      },
      "namespace": {
        "type": "keyword"
      },
      "cluster_id": {
        "type": "keyword"
      },
      "cluster_size": {
        "type": "integer"
      },

      "_comment_shared": "Fields shared by both doc types",

      "algorithm_version": {
        "type": "keyword"
      },
      "created_at": {
        "type": "date"
      }
    }
  }
}
```

### 3.1 Field Semantics

#### Pairwise documents (`doc_type: "pairwise"`)

| Field | Description |
|-------|-------------|
| `place_id_a`, `place_id_b` | Namespaced place IDs (e.g. `gn:745044`, `wd:Q90`). Canonically ordered so that `place_id_a < place_id_b` lexicographically, ensuring each pair is stored once. |
| `namespace_a`, `namespace_b` | Extracted from the place IDs for fast aggregation (e.g. `gn`, `wd`, `tgn`, `pl`, `osm`). |
| `score` | Composite score, 0.0–1.0. |
| `link_class` | One of: `authority_sameAs` (from `relations` field in source data), `contributor_sameAs` (from WHG user reconciliation), `algorithmic_soft` (computed by this module). |
| `link_method` | More specific provenance. For `authority_sameAs`: the source namespace (e.g. `pleiades_relation`, `tgn_relation`). For `algorithmic_soft`: the algorithm version string (e.g. `cluster_v1.0`). For `contributor_sameAs`: `whg_reconciliation`. |
| `signals` | Breakdown of evidence used in scoring. Only populated for `algorithmic_soft` links. |

#### Membership documents (`doc_type: "membership"`)

| Field | Description |
|-------|-------------|
| `place_id` | Single place ID. |
| `namespace` | Extracted namespace. |
| `cluster_id` | Opaque string identifier for the cluster. Format: `c_{hash}` where hash is derived from the sorted set of member place_ids, ensuring deterministic regeneration. |
| `cluster_size` | Number of places in this cluster. Denormalised for fast filtering (e.g. exclude singletons). |

---

## 4. Link Classes and Their Sources

### 4.1 `authority_sameAs` — Existing Hard Links

These come from the `relations` field in the `places` index where `relation_type` indicates identity (typically `sameAs`, `closeMatch`, or equivalent). They also come from the `links` field, which stores cross-references like `{"type": "closeMatch", "identifier": "gn:12345"}`.

**IMPORTANT — Reconnaissance task for the coding agent (see §8):** The exact prevalence, format, and naming conventions of these relations vary by namespace and must be audited before implementation. The coding agent should SSH to the VM and run exploratory queries.

### 4.2 `contributor_sameAs` — WHG User Reconciliation Links

These are `sameAs` links created when WHG users reconcile their contributed datasets against authorities (typically GeoNames and Wikidata). They are stored in the Django/PostgreSQL database on the DigitalOcean VM running the main WHG application, not in the ES indices. The indexing VM has SSH access to that DO VM (`ssh do` or equivalent — the coding agent should check `~/.ssh/config`), so the clustering module can query the PostgreSQL database directly via an SSH tunnel or by running `psql` commands remotely.

**This class should be harvested as part of Phase 1 (§5.2)**, alongside authority hard links. Contributor reconciliation links are human-confirmed assertions and carry the same evidential weight as authority `sameAs` relations — they are hard links, not algorithmic suggestions. The only difference is their provenance (a WHG user rather than an upstream authority).

**IMPORTANT — Reconnaissance task for the coding agent (see §8.2.7):** The coding agent must SSH to the DO VM, identify the relevant Django model table(s) storing reconciliation decisions, and determine the schema — particularly how the target authority place is identified (Wikidata QID, GeoNames ID, etc.) and how that maps to the namespaced `place_id` format used in the ES `places` index (e.g. `wd:Q90`, `gn:745044`). The reconciliation links likely reference the authority's native identifier rather than the WHG namespaced form, so a mapping step will be needed.

### 4.3 `algorithmic_soft` — Computed by This Module

These are the novel soft links produced by the entity resolution algorithm described in §5.

---

## 5. Entity Resolution Algorithm

### 5.1 Design Principles

- **Toponyms are not places.** The same toponym (e.g. "Springfield", "San José", "Alexandria") may be attested by hundreds of unrelated places across the globe. Toponym co-attestation is necessary but not sufficient evidence for identity. Spatial proximity is the primary disambiguator.
- **Namespace-crossing is mandatory.** We are only interested in links between places in *different* namespaces. Two GeoNames records for the same place would be a data quality issue in GeoNames, not a WHG clustering concern.
- **Incremental operation.** The initial run processes the full corpus. Subsequent runs focus on documents added or modified since the last run, tracked via per-source high-water marks (see §6.2): `indexed_at` on the `toponyms` index, `indexed_at` on the `places` index (to be added — see open question §12.5), and a timestamp column on the WHG PostgreSQL reconciliation table. Phase 4 (clustering) always recomputes from all pairwise docs for safety.
- **Deterministic and reproducible.** Given the same index state and algorithm version, the same clusters should be produced. Use canonical pair ordering and deterministic cluster ID generation.

### 5.2 Four-Phase Pipeline

#### Phase 1: Harvest Existing Hard Links

This phase has two parts — both produce pairwise link documents with `score: 1.0`.

**Part A — Authority `sameAs` relations (from ES `places` index)**

Scan the `places` index for all documents where `relations` or `links` contain cross-namespace identity assertions. Emit these as pairwise link documents with `link_class: "authority_sameAs"`.

```
For each place P in the places index:
    For each relation R in P.relations where R.relation_type in {sameAs, closeMatch, exactMatch}:
        If R.related_place_id has a different namespace than P.place_id:
            Emit pairwise(P.place_id, R.related_place_id, link_class="authority_sameAs",
                          link_method="{P.namespace}_relation")
    For each link L in P.links where L.type in {closeMatch, exactMatch}:
        If L.identifier has a different namespace than P.place_id:
            Emit pairwise(P.place_id, L.identifier, link_class="authority_sameAs",
                          link_method="{P.namespace}_link")
```

**Part B — Contributor reconciliation links (from WHG PostgreSQL database)**

Query the Django/PostgreSQL database on the DO VM for user-confirmed reconciliation matches. These are human-verified identity assertions and carry the same weight as authority hard links.

```
Connect to WHG PostgreSQL via SSH tunnel to DO VM.
For each reconciliation decision D where D.status = "confirmed" (or equivalent):
    Map D.source_place to its namespaced place_id in the ES index.
    Map D.target_authority_id to its namespaced place_id (e.g. wd:Q90, gn:745044).
    If source and target are in different namespaces:
        Emit pairwise(source_place_id, target_place_id, link_class="contributor_sameAs",
                      link_method="whg_reconciliation")
```

The mapping from Django model identifiers to ES namespaced `place_id` format must be determined during reconnaissance (§8.2.7). The contributor's own place record will typically be in a contributed namespace, and the target will be in an authority namespace — so the cross-namespace condition should always hold, but verify.

Deduplication across Parts A and B: if the same pair appears as both an authority relation and a contributor reconciliation, emit only one pairwise doc. Prefer `authority_sameAs` as the `link_class` since it is the more authoritative provenance; record both methods in `link_method` as a comma-separated value (e.g. `pleiades_relation,whg_reconciliation`).

#### Phase 2: Exact Toponym Co-Attestation

Scan the `toponyms` index for records where the `attestations` array contains place_ids from more than one namespace.

```
For each toponym T in the toponyms index:
    Group T.attestations by namespace.
    If more than one namespace is represented:
        For each cross-namespace pair (pid_a, pid_b):
            Fetch repr_point for both from the places index.
            Compute spatial_distance_km.
            If spatial_distance_km < THRESHOLD_EXACT_KM (suggest: 50km):
                Emit candidate pair with signals.toponym_exact_count += 1
```

**Optimisation:** Rather than scanning all ~67M toponyms, use an aggregation query to find toponyms whose `attestations` span multiple namespaces. An ES `terms` aggregation on `attestations` with a `min_doc_count` won't work directly (it aggregates over documents, not array values). Instead, use a scripted approach or, more practically, scan with `_source: ["attestations"]` and filter in Python. Given the index is sorted and compressed, a scroll scan is feasible.

**Further optimisation:** Many common toponyms ("Church", "Main Street", etc.) will produce an enormous number of cross-namespace pairs, almost all of which are false positives because they are geographically dispersed. Pre-filter by requiring `ccodes` overlap between the two places before computing spatial distance. This is a cheap keyword lookup.

#### Phase 3: Symphonym Phonetic Similarity

For places not yet linked by Phases 1–2, use KNN search on the `embedding` field.

```
For each newly-added place P (or all places on initial run):
    Collect P's toponym embeddings from the toponyms index.
    For each embedding E:
        KNN search for k nearest neighbours (suggest k=20, similarity >= 0.85).
        For each neighbour N:
            If N's attesting place is in a different namespace than P:
                Fetch repr_points and ccodes for both.
                If ccodes overlap AND spatial_distance_km < THRESHOLD_PHONETIC_KM (suggest: 25km):
                    Emit candidate pair with signals.toponym_symphonym_max = cosine_score
```

**Critical note on KNN at scale:** Running a KNN query for each of ~67M toponym embeddings is not feasible in a single pass. The initial run should be structured as:

1. Build Phase 1 + Phase 2 clusters first.
2. For places that remain un-clustered (no hard links and no exact toponym co-attestation), run Phase 3.
3. On subsequent incremental runs, Phase 3 only processes newly-added places.

#### Phase 4: Composite Scoring and Clustering

For each candidate pair, compute a composite score:

```python
def composite_score(signals: dict) -> float:
    """
    Weighted combination of evidence signals.
    All component scores are normalised to 0.0–1.0 before weighting.
    """
    weights = {
        'toponym_exact':    0.30,   # Exact name match count (log-scaled, capped)
        'symphonym':        0.25,   # Best phonetic embedding similarity
        'spatial':          0.25,   # Inverse spatial distance (sigmoid-scaled)
        'type_match':       0.10,   # Boolean: do place types overlap?
        'ccode_overlap':    0.10,   # Fraction of ccodes in common
    }
    
    s = 0.0
    s += weights['toponym_exact'] * min(1.0, math.log1p(signals['toponym_exact_count']) / math.log1p(5))
    s += weights['symphonym'] * signals.get('toponym_symphonym_max', 0.0)
    s += weights['spatial'] * (1.0 / (1.0 + signals['spatial_distance_km'] / 10.0))
    s += weights['type_match'] * (1.0 if signals.get('type_match') else 0.0)
    s += weights['ccode_overlap'] * min(1.0, signals.get('ccode_overlap_count', 0) / 2.0)
    return round(s, 4)
```

Then compute transitive closure with safeguards:

```
Build undirected graph G where:
    - Nodes are place_ids
    - Edges are pairwise links with score >= CLUSTER_THRESHOLD (suggest: 0.4)
    
For each connected component C in G:
    If diameter(C) in spatial terms > MAX_CLUSTER_DIAMETER_KM (suggest: 100km):
        Sub-cluster C using spatial DBSCAN or by removing lowest-scoring edges
        until connected components are geographically coherent.
    Assign cluster_id to each member of C.
```

---

## 6. Python Module Structure

The module lives at `indexing/clustering/` within the existing `indexing` repository.

```
indexing/
├── clustering/
│   ├── __init__.py
│   ├── config.py              # ES connection, PG connection (SSH tunnel), index names, thresholds, weights
│   ├── schemas.py             # Pydantic models for pairwise docs, membership docs
│   ├── es_client.py           # Async ES client wrapper (reuse gateway patterns)
│   ├── pg_client.py           # PostgreSQL client via SSH tunnel to DO VM
│   │
│   ├── harvest/
│   │   ├── __init__.py
│   │   ├── hard_links.py      # Phase 1A: extract authority_sameAs from ES relations/links
│   │   ├── contributor_links.py  # Phase 1B: extract contributor_sameAs from WHG PostgreSQL
│   │   ├── exact_coattest.py  # Phase 2: exact toponym co-attestation
│   │   └── phonetic.py        # Phase 3: Symphonym KNN similarity
│   │
│   ├── scoring.py             # Composite score calculation
│   ├── clustering.py          # Transitive closure, DBSCAN sub-clustering, cluster ID generation
│   ├── indexer.py             # Bulk-index pairwise + membership docs into clusters index
│   │
│   ├── runner.py              # CLI entry point: full run vs incremental
│   └── state.py               # High-water mark persistence (last run timestamp, algorithm version)
│
├── gateway/
│   ├── reconcile.py           # EXISTING — to be extended with cluster grouping
│   ├── ...
```

### 6.1 Key Design Decisions

**Async throughout.** Use `elasticsearch[async]` (the `AsyncElasticsearch` client). The gateway already uses `httpx.AsyncClient`; the clustering module should use the official ES async client for scroll/scan operations, which handles connection pooling and retry better.

**Pluggable scoring.** The `scoring.py` module should accept a `ScoringConfig` dataclass with all weights and thresholds, loaded from `config.py` (which in turn reads from environment variables or a YAML file). This makes it trivial to retune without code changes.

**Batch processing.** Phases 2 and 3 should process places in batches (suggest 1000 at a time), accumulating candidate pairs in memory, then bulk-indexing when the batch is complete. Use ES `bulk` API with `_op_type: "index"` and deterministic `_id` values (e.g. `pw_{place_id_a}_{place_id_b}` for pairwise, `mb_{place_id}` for membership) so that reruns are idempotent.

**Logging.** Use structured logging (Python `logging` with JSON formatter) so that run statistics (pairs found per phase, clusters formed, time elapsed) are machine-parseable.

### 6.2 State Management and Incremental Runs

The `clustering/state.py` module persists run state between executions. This is the mechanism that makes incremental runs possible. State should be stored as a small JSON document in a dedicated ES index (e.g. `cluster_state`, single document) or as a local JSON file on the indexing VM — the former is preferable because it survives VM reimaging and is visible to monitoring.

#### State Document Structure

```json
{
  "last_run_timestamp": "2026-03-20T14:30:00Z",
  "last_run_mode": "full",
  "algorithm_version": "cluster_v1.0",
  "high_water_marks": {
    "places_indexed_at": "2026-03-20T12:00:00Z",
    "toponyms_indexed_at": "2026-03-20T12:00:00Z",
    "contributor_links_modified_at": "2026-03-20T10:00:00Z"
  },
  "run_statistics": {
    "phase_1a_pairs": 482310,
    "phase_1b_pairs": 15422,
    "phase_2_pairs": 1203844,
    "phase_3_pairs": 87213,
    "clusters_formed": 892341,
    "singletons_excluded": 14203112,
    "duration_seconds": 9840
  }
}
```

#### High-Water Marks

Each data source has its own high-water mark, tracked independently:

**ES `places` index — `places_indexed_at`**

The `places` index does not currently have an `indexed_at` field (check the schema in §3 of the places index — it is absent). Two options:

1. **Add an `indexed_at` date field to the `places` index mapping** and ensure the ingestion pipeline populates it on every document write. This is the clean solution and should be proposed as a minor schema change. The coding agent should check whether the ingestion scripts in `indexing/authorities/` and `indexing/processing/` already track ingestion timestamps in any form.

2. **Use the ES `_seq_no` / `_primary_term` mechanism** to detect changed documents. This is fragile and not recommended for cross-shard tracking.

3. **Maintain an external record of which `place_id`s have been processed.** Store a set of processed place_ids (or a Bloom filter for space efficiency) alongside the state document. On incremental run, scroll the `places` index and skip already-processed IDs. This is wasteful at scale but simple.

**Recommendation:** Option 1 — add `indexed_at` to the `places` schema. The `toponyms` index already has `indexed_at`; the `places` index should match. The coding agent should add this field to `schemas/places.json` and update the ingestion pipeline accordingly. Until that field is populated across the corpus, the first incremental run after a full run can fall back to comparing the set of `place_id`s in the `clusters` index (membership docs) against the set of `place_id`s in the `places` index.

**ES `toponyms` index — `toponyms_indexed_at`**

The `toponyms` index already has an `indexed_at` date field. On incremental runs, Phase 2 (exact co-attestation) queries for toponyms where `indexed_at > high_water_marks.toponyms_indexed_at`. Any newly-indexed toponym that bridges two namespaces may create new candidate pairs. The coding agent should also check whether toponym re-indexing (e.g. when a place is re-ingested with updated names) updates the `indexed_at` timestamp on affected toponym documents — if not, some updates may be missed.

**WHG PostgreSQL — `contributor_links_modified_at`**

The reconciliation table in the WHG Django database is live. On each incremental run, Phase 1B should query for reconciliation decisions created or modified since `high_water_marks.contributor_links_modified_at`. This requires a timestamp column on the reconciliation table — the coding agent must verify during reconnaissance (§8.2.7) whether such a column exists. Common Django patterns include `auto_now_add=True` on a `created` field and/or `auto_now=True` on a `modified` field.

If no suitable timestamp column exists, Phase 1B must re-harvest all confirmed reconciliation links on every run and rely on the deterministic `_id` generation (§6.1, Batch processing) to make re-indexing idempotent. This is slightly wasteful but acceptable given the expected row count (tens of thousands, not millions).

#### Incremental Run Logic

```
On --incremental:

1. Load state document.
2. Phase 1A (authority hard links):
     - Scroll places index where indexed_at > high_water_marks.places_indexed_at
       (or fall back to set-difference if indexed_at is not yet populated).
     - For each new/updated place, extract relations and links as in the full run.
     - Also: check whether any NEW relations have appeared on EXISTING places
       (this can happen if an authority re-ingestion adds relations that weren't
       present before). This is only detectable if indexed_at is updated on
       re-ingestion. Flag this as a known limitation if indexed_at reflects
       only initial ingestion.

3. Phase 1B (contributor reconciliation links):
     - Query PostgreSQL for decisions where modified_at > high_water_marks.contributor_links_modified_at.
     - Emit pairwise docs for new/changed decisions.
     - Handle revocations: if a previously-confirmed decision has been changed to
       rejected, delete the corresponding pairwise doc from the clusters index.

4. Phase 2 (exact co-attestation):
     - Query toponyms index where indexed_at > high_water_marks.toponyms_indexed_at.
     - For new toponyms with cross-namespace attestations, generate candidate pairs
       as in the full run.

5. Phase 3 (phonetic similarity):
     - Only process places that are new (from Phase 1A/1B) AND not yet clustered
       by Phases 1–2.
     - Run KNN queries for their toponym embeddings against the full toponyms index.

6. Phase 4 (re-cluster):
     - Load all existing pairwise docs from the clusters index.
     - Add new pairwise docs from this run.
     - Recompute connected components and cluster assignments.
     - Bulk-update membership docs for any clusters that changed.
     - NOTE: recomputing all clusters from all pairwise docs is the safest approach.
       Attempting to incrementally update only affected clusters risks inconsistency
       from transitive-closure edge cases. Given that Phase 4 is an in-memory graph
       operation completing in seconds, full recomputation is cheap.

7. Update state document with new high-water marks and run statistics.
```

#### Cluster Invalidation on Revocation

When a contributor revokes a reconciliation decision (changes status from confirmed to rejected), Phase 1B must not only skip that pair but actively **delete the pairwise doc** from the `clusters` index. Phase 4 will then recompute clusters, which may cause a previously-merged cluster to split. The runner should log cluster splits as notable events.

---

## 7. Query-Time Integration

### 7.1 Cluster Lookup After Reconciliation Search

After the existing gateway reconciliation returns a set of candidate `place_id`s, look up their cluster memberships:

```python
# In gateway/reconcile.py, after Step 2 (filtering):

cluster_body = {
    "size": len(surviving_pids),
    "query": {
        "bool": {
            "filter": [
                {"term": {"doc_type": "membership"}},
                {"terms": {"place_id": surviving_pids}}
            ]
        }
    },
    "_source": ["place_id", "cluster_id", "cluster_size"]
}
```

### 7.2 Grouping Response

Extend `ReconcileResponse` with an optional grouped view:

```python
class ClusterGroup(BaseModel):
    cluster_id: str
    cluster_size: int
    representative: CandidateHit        # highest-scoring member
    members: list[CandidateHit] = []    # remaining members

class ReconcileResponse(BaseModel):
    hits: list[CandidateHit] = []       # flat list (existing, always populated)
    clusters: list[ClusterGroup] = []   # grouped view (populated when group_by_cluster=True)
    max_score: float = 0
    total: int = 0
```

### 7.3 Standalone Cluster API Endpoint

Add a new endpoint to the gateway for direct cluster lookup:

```
GET /api/cluster/{place_id}     → returns the full cluster for a given place
GET /api/cluster/{cluster_id}   → returns all members of a cluster
```

---

## 8. Coding Agent Instructions

### 8.1 Prerequisites

- SSH access: `ssh pitt` (connects to the VM running Elasticsearch).
- The ES instance is accessible at `localhost:9200` on that VM (or via the `ES_BACKEND` env var in the gateway config).
- ES credentials: check `indexing/.env` or `gateway/config.py` for the `ELASTIC_PASSWORD` mechanism.
- Python 3.10+ with `elasticsearch[async]`, `httpx`, `pydantic`, `numpy`.

### 8.2 Step 0: Reconnaissance — Audit Existing Relations

**This step must be completed before writing any clustering code.** SSH to the VM and run the following queries to understand the shape of existing hard links. Record the results in a file `clustering/RECON_NOTES.md`.

#### 8.2.1 What namespaces exist?

```bash
curl -s -u elastic:$ELASTIC_PASSWORD localhost:9200/places/_search -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "namespaces": {
      "terms": { "field": "namespace", "size": 50 }
    }
  }
}'
```

Record the namespace list and document counts.

#### 8.2.2 What relation_types exist in the `relations` field?

```bash
curl -s -u elastic:$ELASTIC_PASSWORD localhost:9200/places/_search -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "rel_types": {
      "nested": { "path": "relations" },
      "aggs": {
        "types": {
          "terms": { "field": "relations.relation_type", "size": 100 }
        }
      }
    }
  }
}'
```

#### 8.2.3 Which namespaces have `relations` populated?

```bash
# For each major namespace (gn, wd, tgn, pl, osm, etc.):
curl -s -u elastic:$ELASTIC_PASSWORD localhost:9200/places/_search -H 'Content-Type: application/json' -d '{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {"term": {"namespace": "pl"}},
        {"nested": {"path": "relations", "query": {"exists": {"field": "relations.relation_type"}}}}
      ]
    }
  }
}'
```

Repeat for each namespace. Note which namespaces contain relations and how many documents.

#### 8.2.4 Sample `relations` and `links` documents

```bash
# Fetch a few examples from Pleiades:
curl -s -u elastic:$ELASTIC_PASSWORD localhost:9200/places/_search -H 'Content-Type: application/json' -d '{
  "size": 5,
  "query": {
    "bool": {
      "filter": [
        {"term": {"namespace": "pl"}},
        {"nested": {"path": "relations", "query": {"exists": {"field": "relations.related_place_id"}}}}
      ]
    }
  },
  "_source": ["place_id", "namespace", "relations", "links"]
}'
```

Repeat for TGN, Wikidata, and any other namespaces that have relations. Examine:
- The format of `related_place_id` — is it namespaced (e.g. `gn:12345`) or bare?
- The values of `relation_type` — what strings indicate identity?
- The format of `links[].identifier` — same question.
- Whether `links[].type` uses `closeMatch`, `exactMatch`, `sameAs`, or other vocabulary.

#### 8.2.5 Toponym attestation cross-namespace prevalence

```bash
# How many toponyms have attestations spanning multiple namespaces?
# This requires a script — ES cannot natively aggregate on "distinct prefixes in an array".
# Write a short Python scroll script:
```

```python
"""
recon_cross_namespace.py — Run on the ES VM.
Scrolls the toponyms index and counts how many toponym docs
have attestations from >1 namespace.
"""
from elasticsearch import Elasticsearch
import os

es = Elasticsearch(
    "http://localhost:9200",
    basic_auth=("elastic", os.environ["ELASTIC_PASSWORD"]),
)

cross_ns_count = 0
total = 0
scroll_size = 5000

resp = es.search(
    index="toponyms",
    body={"query": {"match_all": {}}, "_source": ["attestations"]},
    scroll="5m",
    size=scroll_size,
)

while True:
    hits = resp["hits"]["hits"]
    if not hits:
        break
    for hit in hits:
        total += 1
        atts = hit["_source"].get("attestations", [])
        namespaces = set(a.split(":")[0] for a in atts if ":" in a)
        if len(namespaces) > 1:
            cross_ns_count += 1
    resp = es.scroll(scroll_id=resp["_scroll_id"], scroll="5m")

print(f"Total toponyms: {total}")
print(f"Cross-namespace toponyms: {cross_ns_count}")
print(f"Ratio: {cross_ns_count/total:.4f}")
```

#### 8.2.6 Index size and document counts

```bash
curl -s -u elastic:$ELASTIC_PASSWORD localhost:9200/_cat/indices/places,toponyms?v&h=index,docs.count,store.size
```

Record these for capacity planning.

#### 8.2.7 WHG PostgreSQL reconciliation schema (on DO VM)

SSH from the indexing VM to the DigitalOcean VM where the WHG Django application runs. Check `~/.ssh/config` on the indexing VM for the correct host alias (likely `do` or similar).

```bash
# From the indexing VM:
ssh do

# Once on the DO VM, identify the Django database:
sudo -u postgres psql -l    # list databases, find the WHG one (likely 'whg' or 'whgv3')

# Connect and explore the reconciliation-related tables:
sudo -u postgres psql -d whg   # adjust database name as needed

# List all tables — look for tables related to places, links, or reconciliation:
\dt

# The reconciliation links are likely in a table associated with the Place model,
# possibly called main_placelink, places_placelink, or similar.
# Also check for a table storing reconciliation task hits/results.
# Common Django model names to look for: PlaceLink, Hit, PlaceMatch, etc.

# Once you identify the table, inspect its schema:
\d main_placelink          # or whatever the table is called

# Key questions to answer and record in RECON_NOTES.md:
# 1. What columns store the source place and target authority place?
# 2. Is the target stored as a full URI (e.g. https://www.geonames.org/745044),
#    a bare ID (745044), or a namespaced ID (gn:745044)?
# 3. How is the authority namespace identified — by a separate column, by URI prefix,
#    or by the task that generated the match?
# 4. What status values exist — is there a confirmed/rejected/pending distinction?
# 5. What is the approximate row count?

# Example exploratory queries:
SELECT count(*) FROM main_placelink;
SELECT DISTINCT link_type FROM main_placelink LIMIT 20;
SELECT * FROM main_placelink LIMIT 10;

# Also check for a reconciliation task/hit table:
\d main_hit                # or similar
SELECT count(*) FROM main_hit WHERE reviewed = true;
SELECT * FROM main_hit LIMIT 5;
```

Record the full table schema, sample rows, and row counts in `clustering/RECON_NOTES.md`. Note specifically:
- The exact column names for source place ID, target authority identifier, and link type.
- How to map the target identifier to the namespaced `place_id` format in the ES index. For example, if the target is stored as a GeoNames URI `https://www.geonames.org/745044`, the module must parse this to `gn:745044`. If it is a Wikidata URI `https://www.wikidata.org/wiki/Q90`, map to `wd:Q90`. Build a mapping function that handles all authority URI patterns encountered.
- Whether the source place has an identifier that corresponds to an ES `place_id`, or whether it is a Django model primary key that requires a further lookup.
- The SSH connection parameters needed for `pg_client.py` to establish a tunnel programmatically (host, port, database name, user, password — the password may be in a Django settings file or environment variable on the DO VM).
- **Whether the reconciliation table has timestamp columns** (`created`, `modified`, `reviewed_at`, or similar). This is critical for incremental runs (§6.2). If no such column exists, Phase 1B must re-harvest all confirmed links on every run and rely on idempotent indexing.

#### 8.2.8 Check for `indexed_at` on the `places` index

The `toponyms` index has an `indexed_at` date field; the `places` index may not. Verify:

```bash
# Check the places mapping for indexed_at:
curl -s -u elastic:$ELASTIC_PASSWORD localhost:9200/places/_mapping | python3 -m json.tool | grep -A2 indexed_at

# If present, check how many documents have it populated:
curl -s -u elastic:$ELASTIC_PASSWORD localhost:9200/places/_search -H 'Content-Type: application/json' -d '{
  "size": 0,
  "query": { "exists": { "field": "indexed_at" } }
}'

# Also check a sample to see the date format:
curl -s -u elastic:$ELASTIC_PASSWORD localhost:9200/places/_search -H 'Content-Type: application/json' -d '{
  "size": 3,
  "query": { "exists": { "field": "indexed_at" } },
  "_source": ["place_id", "indexed_at"]
}'
```

Record the results. If `indexed_at` is absent or unpopulated, note this in `RECON_NOTES.md` — the coding agent should then add `"indexed_at": { "type": "date" }` to `schemas/places.json` and update the ingestion scripts to populate it. See §6.2 and open question §12.5 for the implications and fallback strategy.

### 8.3 Step 1: Create the `clusters` Index

Use the schema from §3. Create the index on the ES instance:

```bash
curl -s -u elastic:$ELASTIC_PASSWORD -X PUT localhost:9200/clusters -H 'Content-Type: application/json' -d @schemas/clusters.json
```

Add `schemas/clusters.json` to the repository alongside the existing `places.json` and `toponyms.json`.

### 8.4 Step 2: Implement Phase 1 (Hard Link Harvest)

**Phase 1A — Authority hard links (`clustering/harvest/hard_links.py`)**

Based on the ES reconnaissance results from §8.2.2–8.2.4, implement the authority sameAs harvester. This must handle:

- The specific `relation_type` values that indicate identity (discovered in §8.2.2–8.2.4).
- The specific format of `related_place_id` and `links[].identifier` (namespaced or bare).
- If identifiers are bare (e.g. just `12345` rather than `gn:12345`), the code must infer the target namespace from context or the link type.

**Phase 1B — Contributor reconciliation links (`clustering/harvest/contributor_links.py`)**

Based on the PostgreSQL reconnaissance results from §8.2.7, implement the contributor sameAs harvester. This must:

- Establish an SSH tunnel to the DO VM and connect to the WHG PostgreSQL database (encapsulate this in `clustering/pg_client.py`).
- Query the reconciliation table(s) for confirmed matches.
- Map source and target identifiers to the namespaced `place_id` format used in the ES `places` index. Build a robust URI/ID → namespace:id mapping function that handles GeoNames URIs, Wikidata URIs, and any other patterns discovered in §8.2.7.
- Handle cases where the source place (a contributed record) may not yet exist in the ES `places` index — log these as warnings and skip rather than failing.

**Deduplication across 1A and 1B:** After both parts run, deduplicate the accumulated pairwise docs. If the same pair (A, B) appears from both sources, emit one doc with `link_class: "authority_sameAs"` and `link_method` recording both provenances (e.g. `pleiades_relation,whg_reconciliation`).

### 8.5 Step 3: Implement Phase 2 (Exact Co-Attestation)

Implement `clustering/harvest/exact_coattest.py`. Key considerations:

- The `attestations` array in the `toponyms` index contains namespaced place IDs (e.g. `gn:745044`).
- Very common toponyms will produce a combinatorial explosion of pairs. **Limit to toponyms where the cross-namespace pair count is manageable** (e.g. skip any toponym attested by >500 places).
- Require `ccodes` overlap as a pre-filter before computing spatial distance.
- Fetch `repr_point` from the `places` index in bulk (multi-get or terms query) rather than one-at-a-time.

### 8.6 Step 4: Implement Phase 3 (Phonetic Similarity)

Implement `clustering/harvest/phonetic.py`. This is the most expensive phase. Key considerations:

- Only process places not already clustered by Phases 1–2.
- Use Elasticsearch's `knn` search on the `embedding` field (128-dim, byte, cosine similarity).
- The Symphonym model is already deployed; the gateway uses it via `gateway/symphonym.py`. Reuse or import that module for embedding generation if needed for query-side embedding, or rely on ES-side stored embeddings.
- Use a tighter similarity threshold (0.85+) than the gateway reconciliation (0.7) because we need higher confidence for pre-computed clusters.

### 8.7 Step 5: Scoring and Clustering

Implement `clustering/scoring.py` and `clustering/clustering.py`. Use `networkx` (or `scipy.sparse` + `connected_components`) for the graph operations — these are well-tested and handle the scale.

The spatial coherence check (§5.2, Phase 4) is critical: without it, common names like "Santiago" would merge dozens of unrelated cities into a single cluster. Use the `repr_point` from the `places` index for each cluster member, compute the pairwise maximum distance, and split if it exceeds the threshold.

### 8.8 Step 6: Adapt the Gateway

Modify `gateway/reconcile.py` as described in §7. This is a minimal change:

1. Add `group_by_cluster` to `ReconcileRequest`.
2. After Step 2, do a terms lookup on the `clusters` index.
3. Group the results.
4. Return both flat and grouped views.

### 8.9 Step 7: Runner and Scheduling

Implement `clustering/runner.py` with CLI arguments:

```bash
# Full initial run
python -m clustering.runner --full

# Incremental (since last run)
python -m clustering.runner --incremental

# Dry run (compute and log but don't index)
python -m clustering.runner --full --dry-run

# Statistics only (report index state)
python -m clustering.runner --stats
```

The runner should be schedulable via cron or systemd timer on the indexing VM.

---

## 9. Performance Considerations

### 9.1 Scale

Approximate corpus size (verify with §8.2.6):
- `places` index: likely 15–25M documents.
- `toponyms` index: likely 60–80M documents.

### 9.2 Phase Costs

- **Phase 1 (hard links):** Part A: one scroll of the `places` index filtering on nested `relations` existence — fast, minutes. Part B: one query to the WHG PostgreSQL database over SSH tunnel — fast, seconds to low minutes depending on row count. Combined: well under 30 minutes.
- **Phase 2 (exact co-attestation):** Full scroll of `toponyms` index. The bottleneck is the Python-side filtering + bulk place lookups for spatial distance. Expect 1–4 hours for initial run.
- **Phase 3 (phonetic similarity):** Depends on how many un-clustered places remain. Each KNN query takes ~10–50ms. If 5M places need KNN queries with ~3 toponyms each, that is ~15M queries × 30ms = ~125 hours serial. **Must be parallelised** using async batching (multiple concurrent KNN queries). With 50 concurrent queries, ~2.5 hours.
- **Phase 4 (clustering):** In-memory graph operation. Even with millions of nodes, `scipy.sparse.csgraph.connected_components` handles this in seconds.

### 9.3 Incremental Runs

See §6.2 for full incremental run logic. In summary:

- **Phases 1A, 1B, 2:** Only process documents newer than the relevant high-water mark (`places_indexed_at`, `contributor_links_modified_at`, `toponyms_indexed_at`). Expected volume: hundreds to low thousands of new documents per source per day. These phases complete in minutes on incremental runs.
- **Phase 3:** Only runs KNN queries for places that are new AND not yet clustered by earlier phases. Typically a small fraction of the corpus. Minutes, not hours.
- **Phase 4:** Recomputes all clusters from all pairwise docs (full recomputation is cheap — seconds in-memory). This is the safest approach; incremental cluster updates are error-prone due to transitive-closure edge cases.
- **Total incremental run time:** Expect under 15 minutes for a typical daily run.

---

## 10. Testing Strategy

### 10.1 Known-Good Pairs

Select 20–30 places that are known to be the same entity across multiple authorities (e.g. London, Baghdad, Constantinople/Istanbul, Tokyo, Tenochtitlan/Mexico City). Verify that the algorithm produces high-confidence pairwise links and correct clusters for these.

### 10.2 Known-Bad Pairs

Select places with confusable names in different locations (e.g. Springfield IL vs Springfield MA vs Springfield UK, Alexandria Egypt vs Alexandria VA). Verify that these are correctly separated into distinct clusters.

### 10.3 Edge Cases

- Places with no geometry (missing `repr_point`). The spatial signal should be nullified, not produce an error.
- Places with no toponyms in the `toponyms` index (should this happen? verify).
- Extremely large clusters (e.g. if "London" appears in 50 authorities, is the cluster correct and spatially coherent?).
- Single-namespace places (should be singletons, not clustered).

---

## 11. Migration Path to v4

When the v4 graph model is implemented:

1. **Pairwise link docs** → Create one `Attestation` node per pairwise doc, with `certainty` = `score`, `certainty_note` = `"algorithm: {algorithm_version}"`. Create edges: `subject_of` from the attestation to `Thing` A, `relates_to` from the attestation to `Thing` B, `typed_by` to an `Authority` document with `label: "sameAs_candidate"` (for `algorithmic_soft`) or `label: "sameAs"` (for `authority_sameAs` and `contributor_sameAs`), and `sourced_by` to an `Authority` document representing the algorithm, source authority, or contributing dataset respectively.
2. **Membership docs** → Discard. Cluster membership becomes a graph traversal query.
3. **Gateway grouping logic** → Replace ES `terms` lookup with AQL graph traversal.

This migration is a one-time batch operation. The pairwise docs contain all the information needed; the membership docs are purely derived.

---

## 12. Open Questions

1. **Weight tuning** — The composite score weights in §5.2 are initial suggestions. Should there be a ground-truth evaluation set for systematic tuning?
2. **Cluster stability** — When a new place is added and bridges two existing clusters, should the merged cluster get a new `cluster_id` or inherit one? New IDs are simpler and safer; no external system should depend on cluster ID stability.
3. **Negative evidence** — Should the system record "these two places are definitely NOT the same" (e.g. after a user rejects a match)? This would be a `notSameAs` link class. Useful for preventing re-suggestion, but adds complexity. Note that rejected reconciliation matches in the WHG PostgreSQL database could serve as an initial source of negative evidence. See also §6.2 on cluster invalidation when contributor decisions are revoked.
4. **Threshold sensitivity** — The `THRESHOLD_EXACT_KM` (50km) and `THRESHOLD_PHONETIC_KM` (25km) values are conservative starting points. Some places (e.g. cities with imprecise historical coordinates) may need larger thresholds. Should thresholds be type-dependent (e.g. larger for regions, smaller for buildings)?
5. **`indexed_at` on the `places` index** — The `places` schema currently lacks an `indexed_at` field (unlike `toponyms` which has one). Adding it is recommended (§6.2) and requires updating `schemas/places.json` and the ingestion pipeline. Until it is backfilled across the full corpus, the first incremental run must fall back to set-difference logic. Is there appetite to run a one-off script to populate `indexed_at` retroactively on all existing place documents (e.g. using the document's `_seq_no` ordering or the ingestion log)?