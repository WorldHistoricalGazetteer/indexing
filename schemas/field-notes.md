# Schema field notes

Human- and machine-readable annotations for the ES index schemas in this folder.
ES mappings cannot carry inline comments (ES 9.x rejects unknown parameters such
as `_comment`), so field-level documentation lives here instead.

## Format (machine-readable convention)

- Each schema file gets an `## <filename>` section.
- Non-obvious fields are listed in a table with columns **Field**, **Type**,
  **Purpose** — or **Field**, **Purpose** where the schema file is the
  authority on types and repeating them here would only invite drift
  (`types.json`).
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
| `geometries[].bounds` | float | Bounding-box coordinates as **`[minLon, minLat, maxLon, maxLat]`** — confirmed 3 Sep 2026 from `enrich_geometry` (`helpers.py:1374`), which fills it from a Shapely `.bounds`, i.e. `(minx, miny, maxx, maxy)`. So `bounds[2] - bounds[0]` is the longitude span; it reads 360.0 for `un:rus`, correct for a country crossing the antimeridian. ⚠️ A naive span from this is **not** a validity test — six `un` countries legitimately exceed 180° (`ata`, `rus`, `fji`, `kir`, `nzl`, `usa`). |
| `types[].label` | text | **Not** a human-facing label — it names the *source vocabulary* of the type: `osm`, `wikidata`, `pleiades`, or a GeoNames feature class letter (`P`, `A`, …). |
| `types[].sourceLabel` | keyword | The composed original source token, e.g. `place=city` (OSM), `P.PPL` (GeoNames), `Q515` (Wikidata). |
| `types[].aat_ids` | long | Getty AAT concept ids mapped from the native type (cross-vocabulary harmonisation). |
| `types[].aat_paths` | keyword | Materialised AAT ancestor paths for each mapped concept (enables hierarchical/consanguinity type filtering). |
| `links[].type` | keyword | The link/match relation: `seeAlso` (external reference, dominant), `closeMatch` / `exactMatch` (identity assertions), `primaryTopicOf`, etc. Note: `links` are external references for display/provenance — the co-reference *graph* used for clustering lives in the hard-link overlay, not here. |
| `links[].identifier` | keyword | The link target — an external URI or authority id (e.g. a Wikipedia URL, `wd:Q90`, a Getty/VIAF id). |
| `boundary` | keyword | Present (non-empty) marks the record as a boundary of a given kind; the value is the boundary type (e.g. `polity` (Cliopatria), `period` (PeriodO), `historic-county` (UKHC), `administrative` (OSM)). Feeds the spatial "Space/Area" filters. |
| `geometries[].boundary_source` | keyword | Which source supplied this boundary. **Read in production** by `processing/ccode_enrichment.py:208`, which splits `un` records into a primary tier and a `bnda` fallback tier — records with no `boundary_source` (a pre-place#173 extract) all count as primary, reproducing the old single-tier behaviour. **Declared 3 Sep 2026**; it had been accepted only because `dynamic` defaults to true. ⚠️ This field is the standing argument against applying `dynamic: strict` before a full declaration pass: strict mapping would have **rejected a field production depends on**. |
| `kaza`, `kaza_1848`, `liva_1848`, `admin_unit`, `wikidata_qid` | keyword | Ottoman administrative fields carried by `og`/`ofs` records and **read by `processing/interlink_ottgaz.py`**, which resolves each `ofs` place's free-text `kaza_1848`/`liva_1848` to the matching `og` admin unit (kaza-within-sancak) to build `within` relations. Declared 3 Sep 2026 — legitimate per-source extras whose defect was the omission, not the write. |
| `timespans` | nested | **The place's own lifespan** — the LPF place-level `when`, as distinct from the three nested timespans (`toponyms[]`, `geometries[]`, `relations[]`) which qualify a *name*, a *geometry* or an *edge* respectively. Written deliberately by exactly two authorities: `chgis` (81,292 docs) and `dgsd` (1,216), measured 3 Sep 2026. **Declared 3 Sep 2026 (SG) rather than migrated away**, after a proposal to move it into a nested path was found unexecutable: all 202 documents whose *only* temporal data was here carry **zero geometries**, so `geometries[].timespans` cannot receive them; and moving a place lifespan onto `toponyms[].timespans` would silently restate "this place existed 965–1170" as "this **name** was in use 965–1170" — a different claim. There is no other correct home for a place-level lifespan, and this is not an accident to be cleaned up. ⚠️ **Live-index caveat:** on `places_h3ccode-20260805t120000z` this was only ever *dynamically* mapped — as an `object` (not `nested`) carrying only `start.in`/`end.in`, since that is all the data showed — so `earliest`/`latest` are absent and nested queries against the root path fail there. The schema now matches its three siblings exactly; the live mapping converges at the next full rebuild. |
| `indexed_at` | date | Timestamp of the last index write to this doc. Set by in-place update patches (e.g. the links / attestation patches) so re-runs are traceable. |

### places.json — open questions

- ✅ **RESOLVED 3 Sep 2026 — `geometries[].bounds` is `[minLon, minLat, maxLon,
  maxLat]`.** Read from the code rather than inferred: `enrich_geometry`
  (`helpers.py:1374` and the envelope fallback at `:1386`) builds it as
  `[b[0], b[1], b[2], b[3]]` from a Shapely `.bounds`, whose contract is
  `(minx, miny, maxx, maxy)`. Corroborated in use — `bounds[2] - bounds[0]`
  gives a longitude span of 360.0 for `un:rus`, which is correct for a
  country crossing the antimeridian. This had been open long enough to be
  worth answering; it took one grep.

### places.json — REBUILD CHECKPOINTS

⚠️ **These expire at the next full rebuild and must be RE-CHECKED there, not
assumed to have converged.** They describe the gap between what the schema
declares and what the current live index actually maps — a gap that closes only
when a rebuild recreates the mapping.

- **root `timespans` is mapped as an `object`, not `nested`.** On
  `places_h3ccode-20260805t120000z` it was only ever *dynamically* mapped, from
  the data alone, so it carries `start.in` / `end.in` and **no
  `earliest` / `latest`**. The schema now declares it `nested` with the full
  shape, identical to its three siblings. **Until a rebuild: nested queries
  against the root path fail there**, and `earliest`/`latest` cannot be queried
  even where a document carries them. After the rebuild, verify the live mapping
  reports `"type": "nested"` before deleting this checkpoint.
- **The undeclared root fields removed in 4.17 still exist in the live index.**
  Their writers are fixed, so nothing can recreate them, but `source` (2,991,143
  docs), `description` (2,057) and root `h3_centroid` / `h3_cover` (1,310,192
  each) remain present until a rebuild drops them. A census run before the
  rebuild will still find them; that is expected and is not a regression.

---

## toponyms.json

One document per **distinct name form**, not per place — the index is
deduplicated across the whole corpus, which is why a single row can carry
thousands of attestations. It is the **discovery** index: every `/api/search`
and `/api/reconcile` request hits this first, collects `place_id`s, and only
then touches `places`.

| Field | Type | Purpose |
|-------|------|---------|
| `toponym_id` | keyword | Identity in LST form `{name}@{lang}` (`London@en`, `Лондон@ru`). ⚠️ **The lang tag here is NOT the one in `places`**: the vocabulary normalises to a base language and puts the rest in `lang_variant`, while `places.toponyms[]` keeps the full tag. Normalise before any cross-index comparison. |
| `name` | text | The name as written, analysed by `toponym_analyzer`. Sub-fields carry the work: `name.keyword` (exact term), `name.raw` (case-insensitive exact — the **lexical exact** discovery pass), `name.prefix` (edge-ngram, typeahead + `starts` mode). |
| `name_romanized` | text | Latin-script transliteration, so a Latin query can reach a non-Latin name by spelling as well as by sound. |
| `lang` / `lang_variant` | keyword | Base language, and whatever the source tag carried beyond it (script, region, variant subtags). Split deliberately — see the `toponym_id` warning. |
| `script` | keyword | Writing system of `name`. |
| `namespaces` | keyword | **Every** authority in which this name form occurs. Pushed into discovery as a `terms` filter when a request scopes namespaces, so the top-`size` window is drawn only from the requested ones — without it a namespace-scoped search draws its window from the whole corpus and then filters, and a rare name loses to common ones it should have outranked. |
| `primary_namespace` | keyword | The first authority to contribute this name. Maintained by `index_namespace.py:269-279`: set when absent, and **re-derived from `namespaces`** when the namespace that owned it is removed, so it never dangles at a namespace no longer present. |
| `attestations` | keyword | ⭐ **The join to `places` — a flat array of `place_id`s.** Discovery reads *this field alone* (`build_toponym_query` puts it in `_source`; `collect_place_ids` walks it) and accumulates `{place_id: best_score}`. **It is NOT a nested array and must not be modelled as one**: it is a flat `keyword` list, so a `terms` filter on it is an ordinary inverted-index lookup, which is what makes the reverse direction (`build_toponym_lookup`, place_ids → full name inventory) cheap. §4.18's verdict turned entirely on this. An incremental single-namespace add **appends** to it and never rewrites the document. |
| `embedding` | dense_vector | The **Symphonym** phonetic vector: 128-d, `element_type: byte` (int8), `similarity: cosine`, indexed for KNN. `fuzzy`/`phonetic` discovery queries this field by name (`gateway/symphonym.py:197`). ⚠️ **Do not threshold the raw cosine to infer match quality** — measured 2026-08-20, genuine cross-script matches (`Marsails → مارساليس`, 0.9878) sit *inside* the junk band, so no cutoff separates them. Confidence is derived from the scoring tiers instead. ⚠️ And KNN alone does **not** reliably retrieve a name spelled exactly as asked: "Newton with Scales" is indexed with 3 attestations and never entered the 200-candidate pool (place#197), which is why the lexical passes exist alongside it. |
| `embedding_version` | integer | Which Symphonym model produced `embedding`. Lets a partial re-embedding be identified without recomputing, and stops vectors from two model generations being compared as if commensurable. |
| `indexed_at` | date | Last write to this document. |

**Operational note:** `dense_vector` merges on this index are the known driver of
production ES heap exhaustion — 72.7 M documents each carrying a 128-d vector.
An OOM or 429 storm here is usually HNSW merge pressure, not query load.

## clusters.json

🛑 **Not documented, deliberately — the index is legacy and its field semantics
are not worth preserving.**

The earlier note here said to document it *only if it outlived the
dynamic-clustering re-architecture*. It did not. `CLUSTERS_INDEX` is gone from
`gateway/config.py`; the membership join (`cluster_id` / `cluster_size`) was
removed from `search.py` and `reconcile.py` on 2026-07-12 after confirming no
consumer read those fields; co-reference now comes from the hard-link overlay
plus client-side Union-Find at a user-chosen θ.

Writing this section up would document a corpse, and create a maintained-looking
account of an index nothing queries. If the static index is ever revived, write
the section *then*, against whatever it means at that point.

## types.json

**The AAT concept hierarchy.** ⚠️ **Division of responsibility, so neither
account drifts: the `type-system` skill documents the PIPELINE** — vocabulary
building, the AAT mapping stages (static / Wikidata P1014 / SPARQL), the ES sync
commands and `scripts/types.sh`. **This section documents only what the FIELDS
mean.** Go to the skill for how they are produced; do not restate it here.

| Field | Purpose |
|-------|---------|
| `aat_id` / `parent_id` | Getty AAT concept id, and its parent — the edges of the hierarchy. |
| `term` / `term_full` | The preferred label, and its disambiguated form (AAT terms collide; `term_full` is what distinguishes them). |
| `labels` / `notes` | Multilingual labels and scope notes. |
| `note` | The English scope note, kept separately for display. |
| `path` | **Materialised ancestor path.** Enables hierarchical type filtering with a prefix match instead of a recursive walk — "everything under *settlements*" is one query. |
| `ancestors` / `depth` | The path decomposed into an id array, plus distance from the root. `ancestors` answers "is X under Y" as a `terms` test; `depth` supports specificity ranking. |
| `fclasses` | GeoNames feature *classes* (`P`, `A`, `H`, …) this concept corresponds to — the bridge that lets a GeoNames-derived type join the AAT tree. |
| `is_place_type` | Whether the concept is usable as a place type at all. AAT contains a great deal that is not (materials, periods, styles); this is the gate that keeps them out of type facets. |
| `gn_fcodes`, `wd_qids`, `osm_tags`, `ohm_tags`, `pleiades_types` | Cross-vocabulary mappings — the native source tokens that map to this concept, one field per authority vocabulary. This is what makes a single type facet span gazetteers that share no vocabulary. |
| `mapping_conf` | Confidence in the mapping, since the three mapping stages do not agree in reliability: a curated static mapping and a SPARQL label match are not equally trustworthy, and this records which produced the row. |
| `indexed_at` | Last write to this document. |

## places_pipeline.json / toponyms_pipeline.json / toponyms-panphon.json

ES **ingest pipelines**, not index mappings — attached as `default_pipeline` and
run server-side on every write.

| File | Role |
|------|------|
| `places_pipeline.json` | Defines `extract_namespace`, which derives the `namespace` field from the `place_id` prefix so no writer has to send it twice. ⚠️ **A snapshot restore does not carry ingest pipelines.** If it is missing, writes to `places` fail with a 400 and the index silently stops accepting documents — re-`PUT` it from this file. This has bitten a restore before. |
| `toponyms_pipeline.json` | The equivalent for the toponyms index. |
| `toponyms-panphon.json` | Index template/settings for the PanPhon stage of the toponym rebuild — the 192-d articulatory-feature representation, distinct from the 128-d Symphonym `embedding` that ends up in `toponyms.json`. Two different vectors from two different stages; don't conflate them. |
