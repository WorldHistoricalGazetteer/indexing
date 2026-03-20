# Clustering Reconnaissance Notes

**Date:** 2026-03-20  
**Author:** Coding Agent  
**Spec Reference:** CLUSTERS.md §8.2

---

## §8.2.1 — Namespaces in the `places` Index

| Namespace | Description         | Document Count |
|-----------|---------------------|----------------|
| `osm`     | OpenStreetMap       | 18,113,756     |
| `gn`      | GeoNames            | 13,378,039     |
| `wd`      | Wikidata            | 11,455,757     |
| `tgn`     | Getty TGN           |  2,972,412     |
| `gb`      | GB1900 / Ordnance   |  1,174,449     |
| `pl`      | Pleiades            |     34,085     |
| `iv`      | Index Villaris      |     24,000     |
| `nl`      | Native Land         |      4,343     |
| `dp`      | D-Place             |      2,599     |
| `un`      | UN Countries        |        257     |

**Total parent documents:** ~47,159,697  
**Note:** `_cat/indices` reports 413M `docs.count` for `places_20260317` because Lucene counts each nested object (toponyms, geometries, relations, links, etc.) as a separate document.

---

## §8.2.2 — Relation Types in the `relations` Field

| `relation_type`   | Count     | Identity Link? |
|-------------------|-----------|----------------|
| `sameAs`          | 6,572,194 | ✅ Yes         |
| `describedBy`     |   717,916 | ❌ No (external Wikipedia/LOC links) |
| `closeMatch`      |    71,707 | ✅ Yes         |
| `connectedTo`     |    26,748 | ❌ No (geographic connectivity, e.g. Pleiades roads) |
| `hasAuthority`    |     3,335 | ❌ No (authority reference) |
| `exactMatch`      |       522 | ✅ Yes         |
| `hasIdentifier`   |       471 | ❌ No          |

**Identity relation_types to harvest:** `sameAs`, `closeMatch`, `exactMatch`

---

## §8.2.3 — Namespaces with `relations` Populated

### All relation types:
| Namespace | Docs with relations |
|-----------|---------------------|
| `wd`      | 3,968,833           |
| `gn`      | 1,645,389           |
| `osm`     | 1,497,772           |
| `iv`      |    22,907           |
| `dp`      |     2,598           |
| `pl`      |    11,351           |
| `un`      |       236           |
| `tgn`     |         0           |
| `gb`      |         0           |
| `nl`      |         0           |

### sameAs only:
| Namespace | Docs with `sameAs` | Target Namespace |
|-----------|-------------------|------------------|
| `wd`      | 3,968,404         | → `gn:*` (GeoNames IDs) |
| `osm`     | 1,497,772         | → `wd:*` (Wikidata QIDs) |
| `gn`      | 1,102,633         | → `wd:*` (Wikidata QIDs) |
| `dp`      |     2,598         | → `glottolog:*`, `hraf:*` (non-WHG namespaces) |

### closeMatch only:
| Namespace | Docs with `closeMatch` | Notes |
|-----------|----------------------|-------|
| `iv`      | 22,907               | → `gb:*`, `osm:*`, `wd:*` (cross-namespace, useful!) |
| `wd`      |  2,391               | (needs sampling) |
| `gn`      |    902               | ⚠️ Self-references only (`closeMatch` → same `gn:*` ID). **Exclude.** |

### exactMatch only:
| Namespace | Count |
|-----------|-------|
| `gn`      |  522  |

---

## §8.2.4 — Sample Relations and `links` Documents

### GeoNames (`gn`) — sameAs
```json
{
  "place_id": "gn:1565033",
  "relations": [
    {"relation_type": "describedBy", "label": "External Link", "related_place_id": "https://en.wikipedia.org/wiki/..."},
    {"relation_type": "sameAs", "label": "Wikidata", "related_place_id": "wd:Q36399"},
    {"relation_type": "hasAuthority", "label": "LOC: ...", "related_place_id": "loc:authorities/names/n99027825"},
    {"relation_type": "closeMatch", "label": "LOC Authority", "related_place_id": "gn:1565033"}  // ⚠️ SELF-REFERENCE
  ]
}
```

**Key finding:** GeoNames `closeMatch` relations reference the same `gn:*` place_id — these are NOT cross-namespace and should be excluded by the same-namespace check.

### Wikidata (`wd`) — sameAs
```json
{
  "place_id": "wd:Q24515153",
  "relations": [
    {"relation_type": "sameAs", "label": "GeoNames", "related_place_id": "gn:10075555"}
  ]
}
```

**Key finding:** Wikidata sameAs → GeoNames. `related_place_id` is already namespaced.

### OpenStreetMap (`osm`) — sameAs
```json
{
  "place_id": "osm:w183215811",
  "relations": [
    {"relation_type": "sameAs", "label": "Wikidata", "related_place_id": "wd:Q22995056"}
  ]
}
```

**Key finding:** OSM sameAs → Wikidata. Namespaced format.

### Index Villaris (`iv`) — closeMatch
```json
{
  "place_id": "iv:IV:IV1680-001-02",
  "relations": [
    {"relation_type": "closeMatch", "label": "GB Match", "related_place_id": "gb:58803d4c2c66dc67e2067b3c"},
    {"relation_type": "closeMatch", "label": "OSM Match", "related_place_id": "osm:425307559"},
    {"relation_type": "closeMatch", "label": "WD Match", "related_place_id": "wd:Q3137539"}
  ]
}
```

**Key finding:** IV has rich cross-namespace closeMatch links to GB, OSM, WD.

### D-Place (`dp`) — sameAs
```json
{
  "place_id": "dp:Ce4",
  "relations": [
    {"relation_type": "sameAs", "label": "Glottolog", "related_place_id": "glottolog:labo1236"},
    {"relation_type": "sameAs", "label": "HRAF: Basques (EX08)", "related_place_id": "hraf:EX08"}
  ]
}
```

**Key finding:** D-Place links to `glottolog:*` and `hraf:*` — these are NOT WHG namespaces. The target place_ids won't resolve in the ES index. **D-Place sameAs relations should be skipped** (or logged as warnings) since the targets don't exist in the `places` index.

### `links` field
**Not populated in ANY namespace.** All cross-references are in the `relations` field. The `links` field in the schema is defined but unused. Phase 1A only needs to process `relations`.

---

## §8.2.5 — Toponym Cross-Namespace Prevalence

- **Total toponyms:** 66,924,548
- **Multi-namespace toponyms** (namespaces field has >1 value): **7,269,777** (10.9%)
- The `toponyms` index has a `namespaces` keyword array field pre-computed, making cross-namespace queries efficient (no need for scripted scanning).

### Sample cross-namespace toponym:
```json
{
  "_id": "Wedemark@frp",
  "name": "Wedemark",
  "namespaces": ["gn", "wd"],
  "attestations": ["gn:3213273", "wd:Q505948"],
  "indexed_at": "2026-02-22T05:19:20.228109+00:00"
}
```

**Optimisation:** Instead of the scroll-based Python script suggested in the spec, we can use the `namespaces` keyword field with a script query: `doc["namespaces"].size() > 1`. Even better, we can use a `terms` filter for specific namespace pairs.

---

## §8.2.6 — Index Sizes

| Index               | Doc Count   | Store Size |
|---------------------|-------------|------------|
| `toponyms_20260317` |  66,924,548 | 41.8 GB    |
| `places_20260317`   | 413,144,365 | 44.3 GB    |

**Note:** The `places` doc count includes nested documents. Actual parent document count is ~47.2M.

---

## §8.2.7 — WHG PostgreSQL Reconciliation Schema

### Connection Details
- **SSH host alias:** `whg` (IP: 144.126.204.70, user: whgadmin)
- **Database:** `whgv2`
- **DB user:** `whgadmin` (owner) / `postgres` (superuser)
- **No `do` alias** — the spec's reference to `ssh do` should be `ssh whg`.

### Key Tables

#### `hits` — Reconciliation task results
| Column         | Type           | Description |
|----------------|----------------|-------------|
| `id`           | integer (PK)   | Auto-increment |
| `authrecord_id`| varchar(255)   | Target authority record ID (e.g. `Q14719784` for Wikidata, `13163303` for WHG) |
| `authority`    | varchar(12)    | Authority name: `wd` or `whg` |
| `place_id`     | integer (FK→places) | Source place (Django PK, not ES place_id) |
| `dataset_id`   | integer (FK→datasets) | Source dataset |
| `src_id`       | varchar(2044)  | Source place's `src_id` within its dataset |
| `reviewed`     | boolean        | Whether user has reviewed this hit |
| `matched`      | boolean        | Whether user confirmed the match |
| `score`        | double precision | Reconciliation confidence score |
| `task_id`      | varchar(50)    | Celery task UUID |

**Row count:** 890,856  
**Confirmed matches** (`reviewed=true AND matched=true`): **13,814**

**Authority breakdown of confirmed matches:**
- `whg`: 10,243 (matches against other WHG contributed places)
- `wd`: 3,571 (matches against Wikidata)

**⚠️ No timestamp columns on `hits` table.** No `created`, `modified`, or `reviewed_at` field. Incremental harvesting of new confirmed hits is NOT possible via timestamp. Must re-scan all confirmed hits and rely on idempotent indexing.

#### `place_link` — Links created from confirmed reconciliation
| Column       | Type           | Description |
|--------------|----------------|-------------|
| `id`         | integer (PK)   | Auto-increment |
| `place_id`   | integer (FK→places) | Source place (Django PK) |
| `jsonb`      | jsonb          | Link data: `{"type": "closeMatch"/"exactMatch", "identifier": "wd:Q123"}` |
| `task_id`    | varchar(100)   | Celery task UUID |
| `src_id`     | varchar(100)   | Source place's `src_id` |
| `created`    | timestamp with time zone | ✅ **Has timestamp** (but may be NULL for older records) |

**Row count:** 2,248,515

**Link types in `jsonb->>'type'`:**
| Type         | Count     |
|--------------|-----------|
| `closeMatch` | 2,238,343 |
| `exactMatch` |     9,587 |
| `related`    |       515 |
| (null)       |        70 |

**Identifier namespaces in `jsonb->>'identifier'`:**
| Namespace Prefix | Count     | Notes |
|------------------|-----------|-------|
| `tgn`            | 1,818,917 | Getty TGN — maps to `tgn:*` in ES |
| `wd`             |   162,600 | Wikidata — maps to `wd:*` in ES |
| `gn`             |   108,385 | GeoNames — maps to `gn:*` in ES |
| `https`          |    38,194 | Full Wikidata URLs: `https://www.wikidata.org/wiki/Q*` |
| `viaf`           |    33,184 | VIAF — NOT in ES places index |
| `loc`            |    21,152 | Library of Congress — NOT in ES |
| `gnd`            |    16,632 | German National Library — NOT in ES |
| `wp`             |    12,433 | Wikipedia — NOT in ES |
| `bnf`            |    10,321 | Bibliothèque nationale de France — NOT in ES |
| `http`           |    10,003 | Full URLs (various) |
| `dbp`            |     8,289 | DBpedia — NOT in ES |
| `pl`             |     5,922 | Pleiades — maps to `pl:*` in ES |
| `gov`            |       931 | NOT in ES |
| `wwf`            |       814 | NOT in ES |
| `whg`            |       481 | WHG internal — may need mapping |
| Others           |       257 | Various |

### Mapping Strategy: `place_link.jsonb->>'identifier'` → ES `place_id`

Only identifiers with prefixes matching WHG ES namespaces are useful:
- `tgn:*`, `wd:*`, `gn:*`, `pl:*`, `osm:*`, `gb:*` → direct mapping (already namespaced)
- `https://www.wikidata.org/wiki/Q*` → extract QID, map to `wd:Q*`
- `viaf:*`, `loc:*`, `gnd:*`, `wp:*`, `bnf:*`, `dbp:*`, `gov:*`, `wwf:*` → **skip** (targets not in ES index)

### Mapping Strategy: `place_link.place_id` (Django PK) → ES `place_id`

The `place_link.place_id` is a Django model integer PK referencing the `places` table. To get the ES-format `place_id`:

1. Join to `places` table: `places.id = place_link.place_id`
2. The `places` table has `dataset` (varchar FK to `datasets.label`) and `src_id`
3. For **authority datasets** whose label matches a known ES namespace (or maps via `_DATASET_NS_MAP`), the ES `place_id` = `{namespace}:{src_id}` (e.g. `gn:745044`)
4. For **contributed datasets** (user uploads that don't match any authority namespace), the ES `place_id` = `whg:place:{django_pk}` (e.g. `whg:place:169687`)

**Note:** The `whg:place:{pk}` format uses the Django `places.id` primary key — NOT `src_id` — because contributed datasets have user-defined `src_id` values that are not globally unique. The Django PK is the stable identifier.

**Current status (March 2026):** Contributed WHG places have NOT yet been indexed to the ES `places` or `toponyms` indices. Phase 1B will therefore emit pairwise docs referencing `whg:place:*` IDs that don't yet resolve in ES. This is harmless: the clustering graph simply won't include these nodes until the corresponding ES documents exist. Once WHG places are indexed, Phase 1B pairs will automatically participate in clustering on the next incremental or full run.

**⚠️ IMPORTANT REALISATION:** Contributor reconciliation links connect **contributed places** (which will be `whg:place:*` in ES) to **authority places** (which are already in ES as `wd:*`, `gn:*`, `tgn:*`, etc.). The cross-namespace condition always holds since `whg` ≠ any authority namespace.

#### `places_closematch` — Pairwise close matches
| Column        | Type | Description |
|---------------|------|-------------|
| `place_a_id`  | FK→places | First place |
| `place_b_id`  | FK→places | Second place |
| `created_at`  | timestamp | ✅ Has timestamp |
| `updated_at`  | timestamp | ✅ Has timestamp |
| `basis`       | varchar(200) | Reason/source |
| `created_by_id` | FK→auth_user | |

**Row count:** 0 (empty table)

---

## §8.2.8 — `indexed_at` on the `places` Index

- **Present in mapping:** ✅ Yes (type: `date`)
- **Populated:** 44,187,285 of ~47,159,697 documents (93.7%)
- **Missing for:** ~3M documents (likely older ingestions before `indexed_at` was added)
- **Format:** ISO 8601 with nanosecond precision: `2025-12-26T18:43:41.364228983Z`
- **No need to add to schema** — already present. The `schemas/places.json` file doesn't include it, so it was added dynamically to the mapping. Should be added to the schema file for consistency.

---

## Summary of Actionable Findings

### Phase 1A (Authority Hard Links from ES)
1. **Identity relation_types:** `sameAs`, `closeMatch`, `exactMatch`
2. **All `related_place_id` values are already namespaced** (e.g. `wd:Q90`, `gn:745044`)
3. **`links` field is unused** — only process `relations`
4. **Cross-namespace filter is essential:** GeoNames has `closeMatch` self-references; D-Place links to non-WHG namespaces (`glottolog:*`, `hraf:*`)
5. **Expected yield:** ~6.6M `sameAs` + ~72K `closeMatch` + ~500 `exactMatch` = ~6.7M candidate pairs (before dedup and cross-namespace filtering)
6. **After cross-namespace filtering with valid ES namespaces only:** The main productive pairs are:
   - `wd` → `gn` sameAs (~4M bidirectional)
   - `gn` → `wd` sameAs (~1.1M)
   - `osm` → `wd` sameAs (~1.5M)
   - `iv` → `gb`/`osm`/`wd` closeMatch (~23K)
   - After deduplication (pairs from both sides): expect **~5-6M unique cross-namespace pairs**

### Phase 1B (Contributor Reconciliation from PostgreSQL)
1. **Primary table:** `place_link` (2.2M rows, has `created` timestamp)
2. **Source mapping:** Authority datasets → `{namespace}:{src_id}`; contributed datasets → `whg:place:{django_pk}`
3. **Target mapping:** Only records where `jsonb->>'identifier'` resolves to an ES namespace (incl. `whg`)
4. **Current status:** Contributed places (`whg:place:*`) are not yet indexed to ES. Phase 1B will emit pairwise docs referencing them, but they won't participate in clusters until ES documents exist. This is by design — no special handling needed.
5. **Connection:** SSH to `whg` (144.126.204.70), then `sudo -u postgres psql -d whgv2`

### Phase 2 (Toponym Co-Attestation)
1. **7.3M multi-namespace toponyms** — significant yield expected
2. **`namespaces` keyword field** enables efficient ES queries without scripting
3. **Must apply ccodes overlap + spatial distance filtering** to manage combinatorial explosion

### Schema Updates
1. **Add `indexed_at` to `schemas/places.json`** for consistency (already exists in live mapping)
2. **Create `schemas/clusters.json`** per §3 of the spec

