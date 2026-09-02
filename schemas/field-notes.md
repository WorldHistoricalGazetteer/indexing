# Schema field notes

Human- and machine-readable annotations for the ES index schemas in this folder.
ES mappings cannot carry inline comments (ES 9.x rejects unknown parameters such
as `_comment`), so field-level documentation lives here instead.

## Format (machine-readable convention)

- Each schema file gets an `## <filename>` section.
- Non-obvious fields are listed in a table with columns **Field**, **Type**,
  **Purpose**.
- **Field** is a dot-path from the mapping root. `[]` marks a `nested` array
  (e.g. `geometries[].source`); nested-within-nested uses further `[]`
  (e.g. `geometries[].timespans[].start.in`).
- Only fields whose purpose is **not obvious** from name + type are documented.
  Self-explanatory fields (`title`, `population`, `elevation`, `ccodes`, …) are
  intentionally omitted.
- A trailing `## <filename> — open questions` block records fields whose meaning
  is not yet confirmed; these are questions, not documentation.

A parser can split on `## ` headings and read the pipe-delimited tables.

---

## places.json

| Field | Type | Purpose |
|-------|------|---------|
| `place_id` | keyword | Namespaced identity `{namespace}:{source_id}` (e.g. `gn:2988507`, `wd:Q90`, `osm:n12345`). WHG-contributed records use a three-part form `whg:{dataset_id}:{entity_id}`. |
| `namespace` | keyword | The authority prefix alone (the part of `place_id` before the first `:`): `gn`, `osm`, `wd`, `tgn`, `ohm`, `whg`, … Used for per-authority filtering and aggregation. |
| `dataset_status` | keyword | Record visibility lifecycle: `published` (public) or `pending` (a contributor's un-published dataset). Pending records live in the same index as public ones; a discovery-time **scope filter** is what hides them from off-scope users — they are never a separate index. |
| `dataset_id` | keyword | The owning gazetteer/dataset id (e.g. `whg:1234`). Doubles as the **scope token**: a contributor's pending `dataset_id`s are the set admitted alongside `dataset_status:published` for that user. |
| `geometries[].source` | keyword | Namespace the geometry was sourced/derived from: the record's own namespace when inherent in the source (e.g. `og`); `ofs` for a hull WHG computed from another namespace's component points; `wd` for a geometry pulled from a linked Wikidata record. Absent on legacy docs = source-inherent. |
| `geometries[].approximation` | keyword | Nature of the geometry vs the true extent: `exact` (or absent) for a faithful source geometry; `convex_hull` \| `concave_hull` \| `bbox` \| `centroid` for a WHG-computed approximation. |
| `geometries[].repr_point` | geo_point | A representative point **guaranteed to fall within** the geometry. This guarantee is exploited by the spatial-containment engine (a container's polygon test can use `repr_point ∈ geom`). |
| `geometries[].h3_centroid` | keyword | The H3 cell id of the representative/centroid point, computed from the real geom-store polygon (not the ES-stored approximation). |
| `geometries[].h3_cover` | keyword | Compacted, multi-resolution set of H3 cell ids covering the full geometry. Used for spatial bucketing / coverage-intersection tests. For point-only geometries this is effectively the single centroid cell. |
| `geometries[].has_geom` | boolean | **Storage-state flag: is the full geometry retrievable from the `/vast` geom store right now** (keyed `{place_id}_{geometry_index}`)? This is the original intent — "does the point undersell the geometry; do I need to look past `repr_point`?" — NOT a shape flag. Always written; set `true` by `enrich_geometry` only when a non-point geom is actually written to VAST (points are never stored → `false`). **Distinct from `geom_class`** (which records *shape*): they diverge, and the divergence is diagnostic — see below. Do NOT use `has_geom` as a "polygon/container" test (a LineString is `has_geom:true` but not areal); use `geom_class == "area"` for that. |
| `geometries[].geom_class` | keyword | **Shape discriminator ∈ {`point`, `line`, `area`}** — the coarse geometry class, computed once at ingest from the actual geometry (`osm_way_area_geometry.geom_class_of`). Multi- variants collapse to their base (`MultiPolygon`→`area`, **`MultiPoint`→`point`**); a `GeometryCollection` is resolved by its members (any polygon→`area`, else any line→`line`, else `point`) so no consumer re-opens the geometry. This is what "is it areal / can it serve as a `contained_in` scope region" should key on. Complementary to `has_geom`: `geom_class` is *shape* (a property of the source), `has_geom` is *retrievability* (a property of storage). Their divergence is an **auditable defect predicate, INCOMPLETE IN BOTH DIRECTIONS** — `geom_class ∈ {area,line} AND NOT has_geom` means the geometry's shape is known but its full form is missing from the store (exactly the place#145 dangling-`has_geom` condition). ⚠️ **It is blind to two classes, both measured in this corpus (2 Sep 2026):** (i) **`MultiPoint`→`point`** — a multi-part point feature whose store entry was never written reads `geom_class:point, has_geom:false`, **indistinguishable from an ordinary point**, and a never-written geometry leaves nothing in the index saying it should have existed (690 coordinates in `whg`); (ii) **the inverse — `geom_class = point` carrying an AREAL `h3_cover`**, which no predicate currently tests and which was **248 of 248 defective** in the `whg` census. **Two predicates are needed, not one, and the pair is still not complete.** Populated on `osm`/`ohm` way docs by the way-geometry pass; being backfilled corpus-wide + added to `enrich_geometry` for all future ingests. |
| `geometries[].geom_ref` | keyword | The geom-store key `{place_id}_{geometry_index}` to fetch the full geometry with (present iff `has_geom`). Consumers may also *construct* this key from `place_id` + `geometry_index`, so it is a convenience, not the sole path. **Live-index caveat:** on `places_postbarrier-20260502…` this field was only ever *dynamically* mapped (as `text`+`.keyword`, because it predates the schema); the schema now maps it `keyword`, which future rebuilds pick up. |
| `geometries[].geometry_index` | integer | Index of this geometry within the place's `geometries[]` array; the second half of the geom-store key. Almost always `0` (most places have a single geometry). Also only dynamic-mapped on the current live index (as `long`); schema now maps it `integer`. |
| ~~`geometries[].hull`~~ | *(removed)* | **REMOVED 2026-07-11.** Was a derived **convex hull** computed by `enrich_geometry` for every geometry — an *ingestion intermediate* used to compute `h3_cover` and for ccode containment; never read at query time. It leaked into ES only via the JSONL index path (incremental adds `ofs`/`og`/`hgis`/`whg` + the `wd` geoshapes patch). Now removed from the schema, stripped on **both** index paths (`staged_parquet.strip_hull`), and scrubbed from all **120,457** live docs via `_update_by_query`. The live index *mapping* keeps an empty `hull` field (ES mappings are append-only) until the next full rebuild. Kept here as a tombstone since old data/mappings may still reference it. |
| `geometries[].bounds` | float | Bounding-box coordinates (float array) for the geometry. (Array order to be confirmed — see open questions.) |
| `types[].label` | text | **Not** a human-facing label — it names the *source vocabulary* of the type: `osm`, `wikidata`, `pleiades`, or a GeoNames feature class letter (`P`, `A`, …). |
| `types[].sourceLabel` | keyword | The composed original source token, e.g. `place=city` (OSM), `P.PPL` (GeoNames), `Q515` (Wikidata). |
| `types[].aat_ids` | long | Getty AAT concept ids mapped from the native type (cross-vocabulary harmonisation). |
| `types[].aat_paths` | keyword | Materialised AAT ancestor paths for each mapped concept (enables hierarchical/consanguinity type filtering). |
| `links[].type` | keyword | The link/match relation: `seeAlso` (external reference, dominant), `closeMatch` / `exactMatch` (identity assertions), `primaryTopicOf`, etc. Note: `links` are external references for display/provenance — the co-reference *graph* used for clustering lives in the hard-link overlay, not here. |
| `links[].identifier` | keyword | The link target — an external URI or authority id (e.g. a Wikipedia URL, `wd:Q90`, a Getty/VIAF id). |
| `boundary` | keyword | Present (non-empty) marks the record as a boundary of a given kind; the value is the boundary type (e.g. `polity` (Cliopatria), `period` (PeriodO), `historic-county` (UKHC), `administrative` (OSM)). Feeds the spatial "Space/Area" filters. |
| `indexed_at` | date | Timestamp of the last index write to this doc. Set by in-place update patches (e.g. the links / attestation patches) so re-runs are traceable. |

### places.json — open questions

- `geometries[].bounds` — confirm the array order (`[minLon, minLat, maxLon,
  maxLat]`?).

---

## toponyms.json

_TBD — non-obvious fields (e.g. `attestations`, the Symphonym embedding field,
`namespaces`) to be documented._

## clusters.json

_TBD — note: this index (the static `cluster_v1.0` HDBSCAN output) is slated for
retirement under the dynamic-clustering re-architecture; document only if it
outlives that work._

## types.json

_TBD — AAT hierarchy fields (materialised paths, fclasses, cross-vocabulary
mappings) to be documented._

## places_pipeline.json / toponyms_pipeline.json / toponyms-panphon.json

_TBD._
