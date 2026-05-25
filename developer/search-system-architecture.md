# WHG Search System — Architecture Reference

> **Purpose:** This document describes how the WHG search page (`/search/`) works end-to-end — from the browser UI through JavaScript payload construction, the Django thin proxy, the CRC Gateway (FastAPI), Elasticsearch query execution across the `places`, `toponyms`, and `clusters` indices, and result rendering.  It covers both the legacy UI layer (still in use) and the v3.5+ backend architecture that replaced it.
>
> **Last updated:** 4 April 2026

---

## 1. Page load & context

| URL | Django view | Template |
|---|---|---|
| `GET /search/` | `SearchPageView` (TemplateView) | `search/templates/search/search.html` |
| `GET /search/<toponym>` | same, with toponym kwarg | same |

The view's `get_context_data()` provides:

| Context variable | Source | Purpose |
|---|---|---|
| `es_whg` | `settings.ES_WHG` (e.g. `whg3dev`) | Index name passed to JS |
| `adv_filters` | Hard-coded list of 7 feature-class tuples | Renders checkboxes |
| `dropdown_data` | `get_regions_countries()` | Populates the spatial-filter Select2 (regions + countries) |
| `has_areas` / `user_areas` | User's saved `Area` objects (types `ccodes`, `copied`, `drawn`) | Enables the "Custom" spatial filter option |
| `search_params` | `request.session['search_params']` | Not actively used by the template |
| `toponym` | URL kwarg (optional) | Pre-fills Schema.org metadata for SEO |

Template injects these into the global JS scope:

```html
<script>
  const dropdown_data = {{ dropdown_data|safe }};
  var eswhg = "{{ es_whg|escapejs }}";
  const has_areas = {{ has_areas|yesno:"true,false" }};
  const user_areas = {{ user_areas|safe }};
  const adv_filters = {{ adv_filters|safe }};
</script>
```

Scripts loaded (deferred, in order):
1. `whg_maplibre.bundle.js` — MapLibre GL wrapper
2. `search.bundle.js` (ES module) — main search logic

---

## 2. UI inputs & filters

### 2.1 Text input

- **Element:** `#search_input` — free-text place name.
- **Typeahead:** On each keystroke (debounced), `GET /search/suggestions/?q=…` returns up to 20 unique titles from ES using `SearchViewV3.build_search_query()` with just the `qstr` param.
- **Submit:** Pressing Enter or clicking `#initiate_search` calls `initiateSearch()`.

### 2.2 Feature-class checkboxes (legacy — to be replaced by type facets)

- **Container:** `#adv_checkboxes`
- **Values:** `A` (Administrative), `P` (Cities/towns), `S` (Sites/buildings), `R` (Roads/routes), `L` (Regions/landscape), `T` (Terrestrial landforms), `H` (Water bodies). All checked by default.
- **Behaviour:** Unchecking all returns zero results (empty `fclasses` string triggers early exit in the backend).

> **⚠ v3.5+ note:** The CRC `places` index does not use GeoNames `fclasses`.  Types are stored per-authority in `types[]` using `{identifier, label, sourceLabel}` (see §6).  The legacy feature-class checkboxes cannot filter on the new index directly.  The gateway's `POST /api/search` returns **server-side facets** (type aggregations and country aggregations) that should replace both the feature-class checkboxes and the client-side post-search facets (§2.7).  Until the UI is updated, `fclasses` is accepted but **ignored** by the gateway.

### 2.3 Temporal control (Dateline)

A custom slider widget (`whg/webpack/js/dateline.js`) rendered as a MapLibre GL control.

| Property | Default | Description |
|---|---|---|
| `fromValue` | 800 | Start of selected range |
| `toValue` | 1800 | End of selected range |
| `minValue` | -2000 | Slider minimum |
| `maxValue` | 2100 | Slider maximum |
| `open` | `false` | Whether temporal filtering is active |
| `includeUndated` | `true` | Whether to include records with no timespans |

- **When closed** (`open: false`): temporal params are excluded from the search; `temporal: false`, `start: ''`, `end: ''`.
- **When open** (`open: true`): `temporal: true`, `start` and `end` are integer years from the slider.
- **`onChange`:** calls `initiateSearch()` (throttled to 300ms) on every slider drag.

### 2.4 Period filter (PeriodO / chrononym)

- **Element:** `#chrononym_input` — typeahead for period names.
- **Suggestion source:** `GET /suggest/entity?limit=60&type=period&mode=nosort&prefix=…`  
  This hits `api/reconcile.py :: SuggestEntityView`, which queries the `Chrononym` Django model using trigram similarity. Returns `{ result: [{ id, name, description }, …] }`.
- **On selection:**
  1. JS fetches `GET /entity/<period_id>/api` → `api/views_entity.py :: EntityFeatureView` → returns an LPF GeoJSON Feature with `when.timespans` and `geometry`.
  2. `deriveOuterBounds(period)` extracts the min start / max end years from `period.when.timespans[].start.in` and `period.when.timespans[].end.in`.
  3. `dateline.reconfigure(outerStart, outerEnd, outerStart, outerEnd, true)` — sets the slider range to the period bounds **and opens the temporal control** (`open = true`).
  4. `draw.deleteAll()` then `draw.add(period.geometry)` — adds the period's spatial geometry (typically multi-polygon covering the region where the period applies) to the MapLibre Draw layer.
  5. Both changes (temporal open + draw geometry) feed into the next `gatherOptions()` → `initiateSearch()` call automatically.

**Assessment:** The period filter IS properly wired — it feeds both temporal range and spatial bounds into the standard search pipeline. However, it is worth noting that the PeriodO geometry ends up in the `bounds` field (the Draw layer's GeometryCollection), not in a separate field. The period's temporal bounds become the slider range.

### 2.5 Spatial filter

Three-part UI in `#spatial_selector`:

| Element | Role |
|---|---|
| `#categorySelector` | Dropdown: `None`, `Country`, `Custom` (user areas, if any) |
| `#entrySelector` | Select2 multi-select, populated dynamically based on category |
| `#clearButton` | Resets spatial filter |

Data flow depending on category:

| Category | `#entrySelector` populated with | Payload fields set |
|---|---|---|
| None | — | `countries: []`, `regions: []`, `userareas: []` |
| Country | Countries from `dropdown_data` (id = ISO2 code, text = name) | `countries: [selected codes]` |
| Custom | User's saved Area objects | `userareas: [area IDs]` |

When countries or regions are selected, their geometries are also drawn on the map via the `countryCache` GeoJSON system.

> **Note:** The `regions` option is commented out in the template (`<!-- <option value="regions">Region</option> -->`). When it was active, selecting a region would expand to its constituent country codes. The `regions` param is gathered in JS but **never processed** by the backend — only `countries` (the derived ccodes) are used in the ES query.

### 2.6 Drawing control

MapLibre GL Draw allows freehand polygon drawing on the map. Drawn geometries are included in the `bounds` GeometryCollection and used as `geo_shape` intersection filters.

### 2.7 Result-facet filters (post-search, client-side)

After results are returned, two accordion sections appear:

- **Place Types** (`#type_checkboxes`) — dynamically built from result types.
- **Countries** (`#country_checkboxes`) — dynamically built from result ccodes.

These filter the already-returned results **client-side only** — no new ES query is made. Toggling checkboxes shows/hides result cards and updates the map source.

---

## 3. Search request

### 3.1 Payload construction (`gatherOptions()`)

```javascript
// whg/webpack/js/search.js, lines 1097–1127
function gatherOptions() {
    return {
        qstr:      $('#search_input').val(),
        idx:       eswhg,                                      // e.g. "whg3dev"
        fclasses:  checkedFclasses.join(','),                   // e.g. "A,P,S,R,L,T,H"
        temporal:  window.dateline.open,                        // boolean
        start:     window.dateline.open ? dateline.fromValue : '',
        end:       window.dateline.open ? dateline.toValue : '',
        undated:   window.dateline.open ? dateline.includeUndated : true,
        bounds:    { type: 'GeometryCollection',
                     geometries: draw.getAll().features.map(f => f.geometry) },
        regions:   [...],    // region IDs (if any)
        countries: [...],    // ISO2 country codes (if any)
        userareas: [...],    // Area model IDs (if any)
        spatial:   $('#categorySelector').val(),                 // "none" | "countries" | "userareas"
    };
}
```

### 3.2 AJAX call (`initiateSearch()`)

```javascript
$.ajax({
    type: 'POST',
    url: '/search/index/',
    data: JSON.stringify(options),
    contentType: 'application/json',
    headers: { 'X-CSRFToken': csrfToken },
    success: function(data) { renderResults(data); }
});
```

### 3.3 Example payload

```json
{
    "qstr": "coventry",
    "idx": "whg3dev",
    "fclasses": "A,P,S,R,L,T,H",
    "temporal": false,
    "start": "",
    "end": "",
    "undated": true,
    "bounds": {
        "type": "GeometryCollection",
        "geometries": []
    },
    "regions": [],
    "countries": [],
    "userareas": [],
    "spatial": null
}
```

---

## 4. Backend processing (v3.5+ Gateway architecture)

The v3.5+ architecture splits backend responsibilities between a **thin Django proxy** (on DigitalOcean) and the **CRC Gateway** (FastAPI on the Pitt CRC ES instance).  All query building, execution, and result normalisation happen in the gateway.

```
Browser ──POST /search/index/──► Django (DO) ──POST /api/search──► CRC Gateway (Pitt)
                                                                        │
                                                                  ES 9.x (localhost:9201)
                                                                  ├── places   (~47M)
                                                                  ├── toponyms (~67M)
                                                                  └── clusters (~20M)
```

### 4.1 Django thin proxy

The Django view becomes a minimal proxy that forwards the browser payload to the CRC Gateway and returns the response:

```python
# Sketch — see api/crc_client.py for the actual implementation
class SearchProxyView(View):
    def post(self, request):
        payload = json.loads(request.body)
        resp = requests.post(settings.CRC_GATEWAY_URL + '/api/search', json=payload)
        return JsonResponse(resp.json(), safe=False)
```

**CRC Gateway access** is configured in `whg/local_settings.py`:

```python
CRC_GATEWAY_URL = 'https://index.whgazetteer.org'
CRC_GATEWAY_API_KEY = ''
CRC_GATEWAY_TIMEOUT = 10
```

Access is gated by the **UI beta version** — the user's session version must be ≥ 3.5.  If any condition fails, the system falls back to the legacy ES indices on DigitalOcean.  This allows development and testing without affecting production users on the stable version.

### 4.2 Gateway request model (`SearchRequest`)

The gateway endpoint `POST /api/search` accepts:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string \| null | null | Search text. **Optional** — omit for a pure-spatial query (must then supply `contained_in` or `bounds`). |
| `mode` | string | `"fuzzy"` | `exact` \| `starts` \| `in` \| `fuzzy` \| `phonetic` |
| `ccodes` | string[] \| null | null | ISO-3166 country codes |
| `bounds` | GeoJSON dict \| null | null | Spatial filter geometry. Routed through the containment engine (§4.4a). |
| `contained_in` | string[] \| null | null | Place_ids whose geometries define a containment region (§4.4a). |
| `containment` | string | `"fuzzy"` | `fuzzy` (H3 cell-based) \| `exact` (Shapely geometry). |
| `relation` | string | `"intersects"` | `intersects` (any overlap) \| `within` (candidate fully inside region). |
| `start_year` | int \| null | null | Temporal filter start |
| `end_year` | int \| null | null | Temporal filter end |
| `undated` | bool | false | Include places with no timespans |
| `size` | int | 100 | Max results (1–500) |
| `exclude_namespaces` | string[] | `["gb"]` | Namespace prefixes to exclude |
| `geom` | string | `"full"` | `"full"` (complete geometries + repr_point) or `"repr_point"` (centroids only) |

`POST /api/reconcile` accepts the same `contained_in` / `containment` / `relation` fields
(its `query` was already optional).

### 4.2a Spatial-containment filter (`contained_in` / `bounds`) — `gateway/spatial.py`

A general spatial-containment capability used by the Atlas UI and the WHG Reconciliation API.
Callers pass **place_ids** (`contained_in`) and/or raw GeoJSON (`bounds`); the gateway resolves
them to a **containment region** and filters results to places that `intersects` / `within` it.
**No Elasticsearch reindex** — it uses only already-indexed fields.

Two-pass engine (mirrors `processing/ccode_enrichment.py`):

1. **Step 0 — resolve region.** `resolve_region(place_ids)` fetches the region places' own
   `geom` from `_source` and Shapely-unions them; `region_from_geojson(bounds)` does the same
   for raw geometry. Builds a compacted, multi-resolution **H3 cover** (adaptive polyfill,
   capped) + a prepared Shapely geometry. Resolved regions are cached (the Atlas pattern reuses
   the same region repeatedly). A region place with no geometry → **HTTP 422**. (The
   gazetteer-level `h3_coverage="global"` sentinel is irrelevant here — per-place geometry
   exists for all gazetteers, so osm/ohm/wd/gn/po/tgn polygons are valid regions.)
2. **Step 1 — candidate gather (ES).** Text+spatial → toponym discovery as usual; pure-spatial
   → skip discovery and gather via `build_places_filter(region=…)`: `repr_point` ∈ region bbox
   **OR** `h3_cover` ∩ region cells (the latter gives recall for large polygons that overlap the
   region away from their representative point).
3. **Step 2.5 — refine** (`apply_containment`): **fuzzy** = H3 cell membership (cheap, tolerant
   to the coarsest region-cover resolution); **exact** = Shapely `prepared.intersects/contains`.
   Key optimisation: `repr_point` is computed via `representative_point()` so it is **guaranteed
   within the place geometry** — hence `repr_point ∈ region ⟹ intersects` with no Shapely, so
   the exact refine only parses full geometry for the smaller `h3_cover`-gathered subset.

**Caveat:** type/country **facets** are computed by ES on the pre-refine candidate set, so they
slightly over-count relative to the refined hits (documented; recompute-in-Python is a possible
follow-up). The response `total` for region queries reports the post-refine survivor count.

### 4.3 Step 1 — Discovery (toponym search)

The gateway searches the **`toponyms`** index to find candidate place_ids.

**For `fuzzy` / `phonetic` modes:** Symphonym KNN search using 128-d byte embeddings.  The phonetic embedding space naturally ranks exact-string matches highest (cosine ≈ 1.0), so a separate BM25 text search is redundant.

```python
# gateway/es_helpers.py — build_phonetic_knn()
knn_body = symphonym.build_knn_query(name=query, lang="und", k=200)
knn_body["knn"]["similarity"] = 0.7
knn_body["_source"] = ["name", "lang", "attestations"]
```

**For `exact` / `starts` / `in` modes:** BM25 text search on `name` sub-fields:

| Mode | Query type | Fields |
|------|-----------|--------|
| `exact` | `term` | `name.keyword` |
| `starts` | `prefix` + `match` | `name.keyword`, `name.prefix` (edge_ngram) |
| `in` | `wildcard` | `name.raw` (lowercased keyword) |
| `fuzzy` | `multi_match` + `term` boost | `name^3`, `name_romanized^2`, `name.prefix`, `name.raw^5` |

Each toponym document carries an `attestations` list — the place_ids of every place that uses that name form.  The gateway accumulates a scored set of unique candidate place_ids (best toponym-match score per place), excluding any namespaces in `exclude_namespaces`.

### 4.4 Step 2 — Filtering + Aggregations (place lookup)

The gateway fetches candidate places from the **`places`** index using a `terms` filter on `place_id` (inverted-index lookup, fast even for thousands of IDs), with optional filters:

**Country codes:**
```json
{ "terms": { "ccodes": ["GB", "FR"] } }
```

**Spatial (geometry intersection):**
```json
{
  "nested": {
    "path": "geometries",
    "query": {
      "geo_shape": {
        "geometries.geom": {
          "shape": { /* GeoJSON from bounds */ },
          "relation": "intersects"
        }
      }
    }
  }
}
```

**Temporal (toponym timespans):**
```json
{
  "nested": {
    "path": "toponyms",
    "query": {
      "nested": {
        "path": "toponyms.timespans",
        "query": {
          "bool": {
            "must": [
              { "range": { "toponyms.timespans.end.in": { "gte": start_year } } },
              { "range": { "toponyms.timespans.start.in": { "lte": end_year } } }
            ]
          }
        }
      }
    }
  }
}
```

**Namespace exclusion:**
```json
{ "bool": { "must_not": [{ "terms": { "namespace": ["gb"] } }] } }
```

**Aggregations** are included in this step for server-side facets:

```json
{
  "aggs": {
    "type_facets": {
      "nested": { "path": "types" },
      "aggs": {
        "by_identifier": {
          "terms": { "field": "types.identifier", "size": 50 },
          "aggs": {
            "label": { "terms": { "field": "types.sourceLabel", "size": 1 } }
          }
        }
      }
    },
    "country_facets": {
      "terms": { "field": "ccodes", "size": 50 }
    }
  }
}
```

### 4.5 Step 3 — Enrichment

Two parallel enrichment lookups for surviving place_ids:

**3a. Toponym enrichment** — Query the `toponyms` index with `{"terms": {"attestations": surviving_pids}}` to retrieve the full name inventory (label + lang) for each place, regardless of which toponym triggered the original match.

**3b. Cluster membership** — Query the `clusters` index for `doc_type: "membership"` docs matching the surviving place_ids.  Each hit provides `cluster_id` and `cluster_size` for prominence ranking.

### 4.6 Step 4 — Response formatting

Toponym-match scores (from Step 1) are normalised to 0–100.  Results are sorted by score descending, with cluster_size as a tiebreaker.  This replaces the legacy `linkcount` ranking.

**Score → cluster-size ranking** replaces the legacy `linkcount` (count of children under a parent document in the union index).  In the v3.5+ system, related place records are grouped into clusters rather than merged into parent documents.  `cluster_size` serves a similar prominence signal.

### 4.7 Suggest endpoint (`GET /api/suggest`)

A separate lightweight endpoint for typeahead, querying only the `toponyms` index:

```
GET /api/suggest?q=Coven&size=10
```

Uses `name.prefix` (edge_ngram) with an exact-match boost on `name.raw`.  Returns deduplicated name strings (no place lookups, no filters), sorted by score.  This replaces the legacy `GET /search/suggestions/` path.

---

## 5. Response & rendering

### 5.1 Response shape (`SearchResponse`)

```json
{
  "hits": [
    {
      "place_id": "gn:2652546",
      "title": "Coventry",
      "names": [
        {"label": "Coventry", "lang": "en"},
        {"label": "Coventrie", "lang": null},
        {"label": "كوفنتري", "lang": "ar"}
      ],
      "ccodes": ["GB"],
      "types": [
        {"identifier": "PPL", "label": "P", "sourceLabel": "P.PPL"},
        {"identifier": "Q515", "label": "wikidata", "sourceLabel": "Q515"}
      ],
      "repr_point": [-1.51, 52.41],
      "geometries": [
        {"type": "Point", "coordinates": [-1.51, 52.41]}
      ],
      "score": 95.3,
      "namespace": "gn",
      "cluster_id": "c_a1b2c3",
      "cluster_size": 4
    }
  ],
  "total": 42,
  "max_score": 95.3,
  "facets": {
    "types": [
      {"identifier": "PPL", "label": "P.PPL", "count": 15},
      {"identifier": "city", "label": "place=city", "count": 8}
    ],
    "countries": [
      {"code": "GB", "count": 12},
      {"code": "US", "count": 6}
    ]
  }
}
```

Key differences from the legacy response:

| Legacy field | v3.5+ replacement | Notes |
|---|---|---|
| `whg_id` | — | No longer exists; places are identified by `place_id` |
| `pid` (integer) | `place_id` (namespaced string) | e.g. `gn:2652546`, `wd:Q6346`, `osm:n12345` |
| `children` | — | Replaced by cluster membership |
| `linkcount` | `cluster_size` | Prominence signal from `clusters` index |
| `variants` | `names[]` | Full name inventory with language tags |
| `fclasses` | `types[]` | Per-authority type objects with `identifier`/`label`/`sourceLabel` |
| — | `facets` | **New:** server-side aggregations for type and country facets |
| — | `namespace` | **New:** source authority (e.g. `gn`, `wd`, `osm`) |

### 5.2 Client-side rendering

The browser receives the `SearchResponse` and:

1. Converts `hits` into a GeoJSON FeatureCollection for MapLibre display (using `repr_point` or full `geometries`).
2. Renders result cards showing: title, names, types, country codes, score, cluster_size, and a "Place Details" link.
3. Uses `facets.types` and `facets.countries` for **server-driven faceted filtering** — replacing the legacy client-side-only type/country checkboxes.
4. Results are sorted by score (toponym-match quality) with cluster_size as tiebreaker, replacing the legacy `linkcount` sort.

---

## 6. ES index structure (v3.5+)

Schemas are in `schemas/` in this repository.  Index names may use dated aliases (e.g. `places_20260317`); the gateway config resolves them via wildcards (`places_*`, `toponyms_*`).

### 6.1 `places` index (~47M docs)

All place records from all authorities.  Schema: `schemas/places.json`.

| Field | Type | Notes |
|---|---|---|
| `place_id` | keyword | Namespaced ID: `gn:2652546`, `wd:Q6346`, `osm:n12345` |
| `namespace` | keyword | Source authority: `gn`, `wd`, `osm`, `tgn`, `pl`, `gb`, etc. |
| `title` | text (+keyword) | Primary place name |
| `toponyms[]` | nested | `{toponym_id, label (text+keyword), timespans[{start.in, end.in}]}` |
| `geometries[]` | nested | `{geom (geo_shape), repr_point (geo_point), timespans[]}` |
| `types[]` | nested | `{identifier (keyword), label (text), sourceLabel (keyword)}` |
| `ccodes[]` | keyword | ISO 3166-1 alpha-2 country codes |
| `population` | long | |
| `elevation` | integer | |
| `relations[]` | nested | `{relation_type, related_place_id, label, timespans[]}` |
| `links[]` | nested | `{type, identifier}` |
| `descriptions[]` | nested | `{value (text), lang}` |
| `depictions[]` | nested | `{@id, title, license}` |
| `indexed_at` | date | |

**Type representation:** Each authority uses its native vocabulary in the `types[]` nested objects:

| Authority | Example `identifier` | `label` | `sourceLabel` |
|-----------|---------------------|---------|---------------|
| GeoNames | `PPL` | `P` | `P.PPL` |
| OSM | `city` | `osm` | `place=city` |
| Wikidata | `Q515` | `wikidata` | `Q515` |
| Pleiades | `settlement` | `pleiades` | `settlement` |
| TGN | `place` | `tgn` | `getty-tgn` |

### 6.2 `toponyms` index (~67M docs)

Deduplicated name records with phonetic embeddings.  Schema: `schemas/toponyms.json`.

| Field | Type | Notes |
|---|---|---|
| `toponym_id` | keyword | Unique ID |
| `name` | text (toponym_analyzer) | Standard analysis + asciifolding; sub-fields: `.keyword`, `.raw` (normalised), `.prefix` (edge_ngram 2–20) |
| `name_romanized` | text (toponym_analyzer) | Romanised form; sub-fields: `.keyword`, `.prefix` |
| `lang` | keyword | ISO 639 language code |
| `lang_variant` | keyword | |
| `script` | keyword | Script code (e.g. `Latn`, `Cyrl`) |
| `namespaces` | keyword | All authority namespaces attesting this toponym |
| `primary_namespace` | keyword | |
| `attestations` | keyword | **List of place_ids** that use this name form — the join key to the `places` index |
| `ipa` | keyword (not indexed) | IPA transcription |
| `panphon_embedding` | dense_vector (192-d, cosine) | Panphon feature vector |
| `embedding` | dense_vector (128-d, byte, cosine) | **Symphonym** phonetic embedding for KNN search |
| `embedding_version` | integer | |
| `indexed_at` | date | |

The `attestations` field is the critical link: each toponym document lists all place_ids whose records attest that name form.  This enables the discovery → filtering pipeline (§4).

### 6.3 `clusters` index (~20M docs)

Pairwise link documents and cluster membership documents.  Schema: `schemas/clusters.json`.  Two `doc_type` values share the same index:

**Link documents** (`doc_type: "link"`):

| Field | Type | Notes |
|---|---|---|
| `place_id_a`, `place_id_b` | keyword | The two linked place_ids |
| `namespace_a`, `namespace_b` | keyword | Their source authorities |
| `score` | float | Composite link score |
| `link_class` | keyword | e.g. `hard`, `toponym`, `phonetic` |
| `link_method` | keyword | Discovery method |
| `signals` | object | `{toponym_exact_count, toponym_symphonym_max, spatial_distance_km, type_match, ccode_overlap_count, shared_link_ids}` |

**Membership documents** (`doc_type: "membership"`):

| Field | Type | Notes |
|---|---|---|
| `place_id` | keyword | Member place_id |
| `namespace` | keyword | Source authority |
| `cluster_id` | keyword | Cluster identifier |
| `cluster_size` | integer | Total members in this cluster |

Both doc types carry `algorithm_version` (keyword) and `created_at` (date).

> **Legacy comparison:** The `clusters` index replaces the parent-children model (`whg_id` + `children[]`) used in the legacy `whg` union index.  `cluster_size` is the v3.5+ analogue of `linkcount`.

---

## 7. Summary of data flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    BROWSER (search.js)                           │
│                                                                 │
│  #search_input ─┐                                               │
│  type facets ────┤                                               │
│  dateline slider ┤  gatherOptions()  ─→  JSON payload           │
│  #chrononym_input┤  (query, mode, ccodes, bounds,               │
│  #categorySelector┤  start_year, end_year, ...)                 │
│  MapLibre Draw ──┘                                               │
│                                                                 │
│         │  POST /search/index/                                   │
│         ▼                                                       │
├─────────────────────────────────────────────────────────────────┤
│               DJANGO thin proxy (DigitalOcean)                   │
│                                                                 │
│  json.loads(body) → forward to CRC Gateway                      │
│                                                                 │
│         │  POST /api/search                                      │
│         ▼                                                       │
├─────────────────────────────────────────────────────────────────┤
│              CRC GATEWAY (FastAPI, Pitt CRC)                     │
│                                                                 │
│  Step 1: Discovery                                               │
│    └─ toponyms index → KNN or BM25 → candidate place_ids        │
│                                                                 │
│  Step 2: Filtering + Aggregations                                │
│    └─ places index → terms(place_id) + spatial/temporal/ccode    │
│       + aggs for type_facets, country_facets                     │
│                                                                 │
│  Step 3: Enrichment                                              │
│    ├─ toponyms index → full name inventory for surviving places  │
│    └─ clusters index → cluster_id + cluster_size                 │
│                                                                 │
│  Step 4: Format → SearchResponse                                 │
│    └─ rank by toponym score, tiebreak by cluster_size            │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│              ELASTICSEARCH 9.x (localhost:9201)                   │
│                                                                 │
│  places    (~47M docs) — place records + nested types/geoms      │
│  toponyms  (~67M docs) — names + Symphonym embeddings            │
│  clusters  (~20M docs) — pairwise links + membership             │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                    BROWSER (rendering)                            │
│                                                                 │
│  renderResults() → HTML cards + map update                       │
│  Server-side facets → type/country filter UI                     │
│  localStorage('last_search') → persist for reload                │
└─────────────────────────────────────────────────────────────────┘
```

---

## 8. Remaining concerns & planned work

### 8.1 Architecture (implemented)

The CRC Gateway architecture described in §4 is live.  The gateway accepts connections **only from the DigitalOcean server running Django** (the only port open through the CRC firewall is 9200).  Django acts as a thin proxy.

Access is gated by the **UI beta version** (version ≥ 3.5 in the user's session).  This allows development and testing without affecting production users on the stable version.

### 8.2 Pre-resolved data: user areas & PeriodO

Two filter types require Django DB lookups — but both are **already resolved client-side** before the search call:

| Filter | How it's pre-resolved | What arrives in the payload |
|---|---|---|
| **User areas** | `user_areas` (GeoJSON features) injected at page load; geometries added to Draw layer when selected. | `bounds` already contains the actual geometry — the Area model ID is redundant. |
| **PeriodO periods** | On chrononym selection, JS fetches `/entity/<id>/api`, extracts temporal bounds → dateline slider, spatial geometry → Draw layer. | `start_year`/`end_year` and `bounds` already contain the resolved data. |

The only Django-dependent calls are the **pre-search** lookups (page context for user areas, `/suggest/entity` for chrononym typeahead, `/entity/<id>/api` for period details).  These remain on Django but happen *before* the search, not during it.

### 8.3 Remaining issues

1. **No pagination:** `size` defaults to 100 per request.  No search-after or scroll mechanism is implemented.

2. **`undated` handling:** The search endpoint accepts an `undated` flag but the temporal filter query in `build_places_filter()` does not yet wrap the range query in a `should` clause that also matches documents with no timespans.  This needs implementing for parity with the legacy system.

3. **Type vocabulary harmonisation:** Types are stored in native vocabularies per authority.  The planned AAT mapping system (`type_mappings` index) is not yet built.  Server-side type facets return raw `identifier` values (e.g. `PPL`, `Q515`, `city`) which are not user-friendly until AAT labels are available.

4. **Feature-class UI migration:** The legacy `fclasses` checkboxes (A, P, S, R, L, T, H) need replacing with a faceted type filter driven by the gateway's server-side aggregations.

5. **PeriodO geometry mixing:** Period geometry is mixed with user-drawn geometry in the `bounds` field — no way to distinguish them on the backend.

### 8.4 Responsibility split (as implemented)

| Responsibility | Owner | Notes |
|---|---|---|
| Page context (user areas, dropdown data) | **Django** | `SearchPageView` unchanged |
| Chrononym typeahead | **Django** | `/suggest/entity` unchanged |
| Period entity fetch | **Django** | `/entity/<id>/api` unchanged |
| Search payload construction | **Browser** | `gatherOptions()` |
| Search payload → ES query building | **CRC Gateway** | `gateway/search.py` |
| ES query execution | **CRC Gateway** → ES | Three-step pipeline |
| Result normalisation | **CRC Gateway** | `SearchResponse` model |
| Faceted aggregations | **CRC Gateway** | Type + country aggs |
| Thin proxy (browser ↔ gateway) | **Django** | Minimal pass-through |
| Result rendering | **Browser** | `renderResults()` |

### 8.5 Reconcile endpoint

The reconciliation endpoint (`POST /api/reconcile`) uses the same three-step architecture as search but with different response models (`CandidateHit`, `ClusterGroup`) and optional cluster grouping.  See `gateway/reconcile.py`.

