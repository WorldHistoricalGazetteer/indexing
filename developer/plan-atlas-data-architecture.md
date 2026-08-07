# Plan — Atlas data architecture: geometry channels, labels, the temporal model, and area selection

> **Status:** proposed. §8 step 1 done (whg3 `2182ebfec`); everything else not started.
> **Written:** 30 July 2026 (renamed from `plan-tileset-architecture.md` — it outgrew the name;
> older issue comments link to the previous path)
> **Supersedes the framing of:** `plan-tiling-fixes-159-160.md` (its findings stand; §7 says what changes)
> **Issues:** place#156 (Atlas Areas UI), place#159 (per-fragment labels), place#160 (missing tiles),
> place#140 (low-zoom coverage), place#133 (point density), place#131 (per-feature temporal props),
> **place#164 (temporal model — see `plan-temporal-model.md`)**, **place#165 (geom-store index)**,
> **place#166 (channel model)**
> **Runs AFTER `plan-temporal-model.md`**, which commissions the re-ingestion this plan's retile
> rides on — see §8's entry condition.
> **Repos:** `indexing` (tiling), `whg3` (rendering + interaction), `tileboss` (`style.json`)

---

## 0. What this changes

Today a bucket's rendering strategy is chosen by **one majority vote** — `polygon > point` —
which simultaneously decides four unrelated things. Every gazetteer whose geometry is mixed
gets at least one of them wrong.

This plan replaces that with **per-geometry-type channels**: each bucket emits the channels its
data actually needs, each tiled with its own flags, all joined into one source-layer and
distinguished by a property, exactly as the place#140 `coverage:1` footprint already is.

It then sets out what the two Atlas use cases require of those channels, and — for the
overlapping-polygon problem in OHM — proposes an order of attack in which the expensive
geometric work comes **last**, because measurement shows the cheap fixes solve most of it.

---

## 1. Evidence

### 1.1 Geometry mix per bucket (sampled at z9/z10 around each tileset's declared centre)

| bucket | Point | Line | Polygon | shape |
|---|---:|---:|---:|---|
| `gn` | 24,516 | — | — | point-only |
| `gb` | 25,893 | — | — | point-only |
| `tgn` | 7,907 | — | — | point-only |
| `chgis`, `iv`, `tm`, `dp`, `alc`, `dgsd`, `ofs`, `gn_capitals` | 19–3,414 | — | — | point-only |
| `po` | — | — | 7,080 | polygon-only |
| `kain_par` | — | — | 2,256 | polygon-only |
| `un`, `ukhc`, `og`, `vob_cty`, `vob_rc`, `vob_rd` | — | — | 4–225 | polygon-only |
| `clio` | — | 10 | 3,387 | polygon + trace lines |
| `nl` | — | 4 | 120 | polygon + trace lines |
| **`pl`** (Pleiades) | 546 | **230** | — | **point + line** |
| **`wd`** (Wikidata) | 10,323 | **51** | **80** | **all three** |
| **`hgis`** | 12 | 8 | 41 | **all three** |

### 1.2 What the current rule does with those

`generate_tiles.py:1557` — `if collect_cov and cov_geoms and polygon > point:`

| | polygon-dominant | point-dominant |
|---|---|---|
| coverage footprint (z0–7 mottle) | emitted | **none** |
| real features | pinned to z8+ | full zoom |
| point clustering / heatmap weights | **off** | on |
| tippecanoe | `preserve_all` | `coalesce-densest` |

Consequences, per bucket:

- **`wd`** — point-dominant, so its **80 polygons and 51 lines have no low-zoom extent at all**
  and draw as a sparse scatter of raw shapes over a heatmap at z0–7. This is precisely the
  "deceptively sparse" illusion place#140 fixed for polygon gazetteers, still live for the
  polygon *minority* of hybrids.
- **`pl`** — 230 LineStrings, no footprint, no extent. Lines get no low-zoom treatment anywhere
  in the pipeline: `_accumulate_coverage` accepts only Polygon/MultiPolygon/GeometryCollection.
- **`hgis`** — polygon-dominant, so clustering is **off** and its 12 points are pinned to z8+:
  invisible below z8, and absent from any heatmap. The mirror failure.
- **`clio` / `nl`** — their handful of lines are pinned to z8+ with the polygons and contribute
  nothing to the footprint.

### 1.3 Labels

There is **no symbol layer for gazetteer features anywhere** in `loadGazetteerStyle`
(`whg_maplibre.js:178-305`). Explore mode draws unlabelled circles and polygons. Only the
baked-in `osm`/`ohm` context layers have labels, and those are the ones repeating per tile
fragment (place#159).

### 1.4 The temporal props are baked into the tiles and never used by the map

place#131 put per-feature `start`/`end` on every feature. Nothing in `whg3` filters a **map
layer** on them — `grep` for `['get','start']` across `whg/webpack/js` returns nothing. The
Date Range control filters search results and the gazetteer list only.

Measured on live OHM tiles:

| tile | features | all carry start/end | alive in 1500 | alive in 1800 | alive in 1900 |
|---|---:|---:|---:|---:|---:|
| Berlin z6 | 591 | 591 | 44 | **73** | 143 |
| Berlin z8 | 166 | 163 | 2 | **5** | 22 |
| Paris z6 | 214 | 214 | 12 | **2** | 1 |

591 features carry **403 distinct `(start,end)` spans**. OHM's overlap is overwhelmingly
*temporal versioning of the same polity*, not spatial competition — and an instant-in-time
filter collapses it by 88 % at Berlin and 99 % at Paris.

### 1.5 …but the date stamps are not yet a usable convention

Sampled at z9 across every published tileset:

| stamp | tilesets | effect of a historical range filter |
|---|---|---|
| `(2025, 2025)` — "contemporary", 100 % of features | **`osm`, `osm_misc`, `tgn`, `nl`** | **excluded entirely** |
| `(2025, 9999)` — current and ongoing | `un` | excluded by any range ending before 2025 |
| no `start`/`end` at all — 0 % dated | **`gn`** | excluded in *range* mode; passes only in *+undated* mode |
| genuine historical spans | `ohm`, `po`, `clio`, `gb` (1888–1914), `kain_par`, `iv`, `pl`, `wd` (82 %) | filters correctly |

So switching the map layers onto the temporal filter today would **blank OpenStreetMap, the
Getty TGN and Native Land the moment a user drags the Date Range into the past**, and blank
GeoNames in plain range mode. `(2025, 2025)` is a snapshot stamp being read as a lifespan.

*(The `un` row reports the **tile** value. ES holds `{start: {latest: 2025}}` with no end; the
`9999` is `TILE_OPEN_END_YEAR` substituted for an absent endpoint. The table describes tile
sentinels, not stored semantics — a distinction the temporal model depends on.)*

This does not sink §5.1 — it moves it. **An earlier draft of this section prescribed the wrong
fix** (make contemporary sources open-ended, `end = 9999`). §1.6 below, and
`plan-temporal-model.md` in full, supersede it: `(2025, 2025)` is not a provenance artefact to be
neutralised, it is a real claim about the place that we are recording in the wrong field.

---

## 1.6 The date stamps encode the wrong claim — split out to its own plan

`schemas/places.json` gives every temporal endpoint `in` / `earliest` / `latest`, and ingestion
uses `in` almost exclusively. So a source that records places *as they were* at a moment — OSM's
2025 dump, Index Villaris in 1680 — has its **attestation** stored as a **lifespan**:

| encoding | claim |
|---|---|
| `start.in = Y`, `end.in = Y` | the place existed **only** in year Y |
| `start.latest = Y`, `end.earliest = Y` | attested alive at Y — started no later, ended no earlier |

That is why §1.5's stamps are unusable, and encoding them correctly dissolves the problem with no
convention hack: an OSM boundary is then not *definitely* alive in 1500 but **is** *possibly*
alive, because `start.earliest` is absent and therefore unbounded.

**The analysis, the per-source encoding table, the Wikidata precision mapping, the calendar-model
finding and the dump-refresh sequencing now live in `plan-temporal-model.md`** (place#164). It
outgrew this document — it is a different programme, in ingestion and query semantics rather than
tiling, and needs its own owner.

**What this plan needs from it:** only §5.1. The temporal filter on map layers is the largest
measured win here (it collapses OHM's overlap by 88–99 %, §1.4) and it **must not ship first**, or
it blanks `osm` / `osm_misc` / `tgn` / `nl`. Two further consequences are recorded there and
matter to scheduling here:

- the client half becomes **two filter modes** — *definitely* vs *possibly* alive — rather than one
  range test plus an "+Undated" escape hatch;
- refreshing the `osm` / `ohm` dumps forces a retile of exactly the three buckets §8 covers, so
  running the tiling fix standalone first means paying that 24 h retile twice.

---

## 2. Diagnosis

One switch is doing four jobs:

1. does this bucket get a low-zoom extent representation?
2. are real features hidden below the crossover?
3. are points clustered (and therefore heatmap-weighted)?
4. `preserve_all` or `coalesce-densest`?

These are properties of a **geometry type**, not of a bucket. Points want clustering and a
heatmap; polygons want a dissolved footprint and no-drop preservation; lines want a footprint
too. A bucket holding all three needs all three answers, not one.

---

## 3. The channel model (**place#166**)

Each bucket emits up to five channels. Each is its own `tippecanoe` pass with its own flags and
zoom range; all are `tile-join`ed into **one source-layer** and distinguished by a property —
the pattern `coverage:1` already establishes and which the renderer already understands.

| channel | contents | zooms | tippecanoe | marker |
|---|---|---|---|---|
| **points** | Point features | 0–10 | `--cluster-*`, `--accumulate-attribute start:min/end:max` | *(Point, unmarked)* |
| **shapes** | Polygon / LineString features | 8–10 | `preserve_all` (no-drop) | *(geometry type)* |
| **extent** | one dissolved footprint | 0–7 | plain, simplified | `coverage: 1` |
| **labels** | one anchor point per shape | 0–10 | plain, **never clustered** | `label: 1` |
| **bands** | *(fixed admin buckets only)* per-`admin_level` partitions of *shapes* | per band | as now | `boundary` |

Rules:

- **A bucket emits every channel its data supports** — no majority vote. `wd` gets points *and*
  shapes *and* extent *and* labels. `pl` gets points, shapes, extent (from its lines), labels.
- **`extent` = `unary_union(polygons ∪ buffer(lines, ~0.01°))`.** Buffering lines at roughly the
  footprint's own simplify tolerance (`_COVERAGE_SIMPLIFY_DEG` ≈ 0.008° ≈ 900 m) makes a line
  gazetteer legible at z0–7 as the same mottle, satisfying "lines to be treated similarly to
  areas". Points contribute nothing — the heatmap is their extent.
- **The z8 pin applies to `shapes` only.** Points keep full-zoom clustering in every bucket, so
  `hgis`'s twelve points stop vanishing.
- **Per-feature dissolve.** Before writing a shape, `unary_union` its own parts, so a place made
  of several adjacent polygons renders without internal borders (use case 1). Guard with a cheap
  bbox-intersection precheck so the ~18 M single-part OSM features skip the call entirely.

### 3.1 Why one source-layer, not one per channel

`loadGazetteerStyle` builds a **full layer stack — heatmap, fill, line, circle, coverage — for
every entry in `vector_layers`** (`whg_maplibre.js:216`). Publishing `wd_labels` as a separate
vector layer would therefore give the label points **their own heatmap**: a spurious density
field made entirely of label anchors. For `po` (7,080 polygons, zero points) that would
manufacture a heatmap out of nothing.

Keeping labels in the same source-layer, marked `label: 1`, avoids this, and brings three
further benefits:

- **The `tile-join -l` problem disappears.** `plan-tiling-fixes-159-160.md` §3.1f has to convert
  `layer_name` to a sequence because tile-join's `-l` is a *keep-only* filter that would
  silently discard a second layer. With one source-layer there is nothing to change and one
  fewer way to lose every label without noticing.
- **Feature-state is shared.** The label point carries the **same `id`** as its shape
  (`encode_feature_id(namespace, source_id)`), so a single `setFeatureState` highlights the
  polygon when the label is hovered, and vice versa, with no bookkeeping.
- It matches how `coverage:1` already works, so there is one convention rather than two.

**Cost: one property.** `label: 1` on shape-anchor points only.

### 3.2 Renderer changes this implies (`whg3`)

```js
_heat    filter: Point AND !has coverage AND !has label     // ← new clause: labels never heat
_circle  filter: Point AND !has label
_fill    filter: Polygon AND !has coverage
_line    filter: (Polygon|LineString) AND !has coverage
_label   filter: has label                                   // ← new symbol layer
```

and in the `tileboss` style, `osm-label-*` / `ohm-label-*` gain `['has','label']` while the
`*-line-*` layers gain `['!', ['has','label']]`.

The `!has label` clause on `_heat` is the requirement "representative points … must be
distinguished from actual point data so that they do not contribute to heatmaps in hybrid
point/line/area gazetteers", enforced at the only place it can be enforced.

### 3.3 Label anchor geometry

- **Polygons** — `shapely.ops.polylabel` (pole of inaccessibility) on the largest-area member of
  a MultiPolygon; fall back to `representative_point()` then `centroid`. Simplify large rings
  first: polylabel is iterative and unbounded on a 100 k-vertex ring. Do **not** reuse the staged
  `repr_point` — that is `representative_point()`, guaranteed inside but often hard against an
  edge, which is exactly wrong for concave regions.
- **Lines** — midpoint of the longest constituent segment.
- **Points** — **no label feature.** A point *is* its own anchor; the symbol layer renders from
  the points channel at z8+, where the circles appear and clustering has stopped. Emitting label
  points for point features would double `gn`'s ~12 M features for nothing.

---

## 4. Use case 1 — Gazetteer Explore mode

> *Each place labelled once at its representative point; area shown as a polygon without
> internal borders if composed of several fragments; lines treated like areas.*

| requirement | delivered by |
|---|---|
| labelled once | §3 `labels` channel + `_label` symbol layer; MapLibre collision keeps it to one |
| at its representative point | §3.3 pole-of-inaccessibility |
| no internal borders | §3 per-feature `unary_union`; place#156's client-side fragment union stops being needed once §5.3 lands |
| lines like areas | §3 `extent` includes buffered lines |
| labels don't pollute the heatmap | §3.2 `!has label` |

`symbol-sort-key` should rank by feature area (polygons) or length (lines) so that at low zoom
the large places win collisions and the map degrades gracefully rather than arbitrarily.

`gazetteerInteraction.SHAPE_SUFFIXES` gains `_label`. The existing comment — *"labels are
excluded because labels-without-shapes feel ambiguous"* — is superseded: once there is exactly
one label per place, a label is the **least** ambiguous target on the map. See §5.2.

---

## 5. Use case 2 — Area selection

> *Applies to tilesets with good, authoritative polygon coverage: Wikidata, UN, OSM, OHM and
> others.*

Eligibility is a registry flag, not a hard-coded list. `region_source` already exists and drives
the panel (`search/views.py:266-292`); it needs a companion assertion that the bucket actually
publishes a `shapes` channel, checked by the verifier (§7) rather than assumed.

From §1.1 the eligible set is `osm`, `ohm`, `un`, `po`, `clio`, `nl`, `kain_par`, `ukhc`, `og`,
`vob_*`, and `wd` **partially** (80 polygons in the sampled area — real, but not "good coverage";
it should be offered with that caveat rather than silently behaving like `un`).

### 5.1 Date first — the largest win, once the stamps mean something

§1.4: the props are already in the tiles and the map ignores them. Wiring the existing Date Range
control into the boundary and gazetteer layer filters collapses OHM's stack by 88–99 %. But
§1.5 shows it cannot simply be switched on: four tilesets are point-stamped `(2025, 2025)` and
would vanish.

Three changes, **in this order**:

1. **Fix the stamp convention at staging** (indexing). Contemporary sources get an open-ended or
   absent span, never `(2025, 2025)`. Applies to `osm`, `osm_misc`, `tgn`, `nl`; `un` is already
   right. Cheap to emit, but it rides on the next retile of each source.
2. **Apply the temporal filter to map layers**, composed with the existing `boundary` filter and
   matching `temporalHitPasses` (`atlas.js:264`) exactly — undated features pass only in
   *+undated* mode:
   `['all', ['has','start'], ['<=', ['get','start'], to], ['>=', ['get','end'], from]]`
   The `coverage:1` footprint layers are **excluded** from the clause: an extent is not a place
   and should not blink out with the date.
3. **Add an instant ("as at year Y") mode.** A *range* of 800–1800 selects almost every OHM
   polity and disambiguates nothing; a single year is what makes a historical boundary layer
   legible. The sliders already permit `from == to`, so this is discoverability rather than new
   capability — a lock toggle, defaulting on for `ohm` area selection.

Together these convert "a great many overlapping polygons at the same zoom" into a normal map.
**Open product question for (2):** should picking 1500 hide OSM's contemporary boundaries
altogether? That is what the filter *means*, but it will read as breakage unless the panel says
so — the Regions status line should distinguish "no boundaries at this level" from "this source
has nothing in this period".

### 5.2 Select by label, not by fill

With a label per place (§4), the click target problem dissolves without touching geometry:

- MapLibre's symbol collision already guarantees **at most one label per screen position**, so a
  label click is unambiguous by construction. No `e.features[0]` lottery.
- Hovering a label highlights its polygon via the shared feature id (§3.1).
- It keeps working however deeply the polygons overlap, and however many share a level.
- For the residual case — a click that genuinely could mean several places — offer a small
  disambiguation list rather than pretending one wins. Competing historical claims *should*
  overlap; the UI should say so.

Keep fill clicks as a fallback, but make the label the primary affordance.

### 5.3 Authoritative geometry, served — retire the fragment stitching

place#156 currently reconstructs a selected region by collecting its **tile fragments** and
unioning them client-side. That works, but it is a workaround with two real limits: only
fragments in *loaded* tiles are found, so a region larger than the viewport is silently
truncated; and the geometry is whatever simplification the current zoom happens to carry.

The authoritative geometry already exists in the **geom store** that the tiling pipeline reads
(`GeomStoreReader`). Exposing it removes the whole class of problem:

```
GET /api/geometry/<place_id>  →  GeoJSON geometry
```

whg3 then fetches the true polygon on selection and uses it for both the overlay and the spatial
constraint. This also makes §5.4 possible (ancestors need real geometry, not tile fragments) and
finishes the job place#156 started.

#### Verified 30 July 2026 — reachable, but `index.json` is the blocker

| check | result |
|---|---|
| Same host? | **Yes.** The gateway runs on `gazetteer.crcd.pitt.edu`; the store is at `/vast/ishi/geom` on that machine — 78 shards, ~20 GB. |
| Readable by the gateway user? | **Yes.** `gazetteer` has primary group `ishi`; `/vast/ishi` is `drwxrws--- root ishi`, `/vast/ishi/geom` is `drwxrwsr-x`, shards are `rw-rw-r--` group `ishi`. |
| Already wired? | **Yes.** `gateway/spatial.py:76-90` has a lazy `get_geom_reader()` built on `processing.geom_store.GeomStoreReader`, used by `search.py:529` and `reconcile.py:654` for `containment_mode='exact'`. |
| Ever actually loaded in production? | **No.** Zero `geom-store` lines in `/vast/ishi/elastic/logs/gateway.log`, and the gateway process RSS is **0.63 GB**. Exact containment has been silently degrading to `repr_point` — consistent with the known `resolve_region` behaviour. |

**The blocker is `GeomStoreReader.__init__`, which does `json.load()` on a 1.02 GB
`index.json`.** Measured on the host by parsing a 20 MB prefix and extrapolating: 228,116
sample entries cost 105,995,204 bytes as a Python dict — **465 bytes/entry × 11.5 M entries ≈
5.4 GB RSS**, plus a cold parse of a gigabyte of JSON on first request. The host has 36 GB
available, so it is survivable but not something to put in a request path by accident, and it
explains why the feature has never switched itself on.

**So the endpoint is cheap only if the index stops being a Python dict.** That single change
unblocks **two** things: `/api/geometry/<place_id>`, and the exact-containment path that has been
dark since it was written. Do it before the endpoint, not after.

#### 5.3.1 Replace `index.json` with SQLite — ✅ **DONE, LIVE IN PROD 30 July 2026** (**place#165**)

Shipped in commits `e98a1af` (store + backfill + tests) and `361e5ba` (gateway threading).
Predictions vs. what actually happened on the live 11,545,093-entry store:

| | `index.json` before | SQLite — predicted | SQLite — **measured** |
|---|---|---|---|
| gateway RSS | **5.4 GB** | ~0 | **+3.8 MB** (642.9 → 646.8 MB across the first exact request) |
| on-disk size | 1.02 GB | 0.39 GB | **0.375 GB** |
| build time | — | ~31 s | **~2 min** (`ijson` streaming is the bottleneck, not the inserts) |

**Verified live:** `geom-store: opened /vast/ishi/geom/index.sqlite (11545093 entries, sqlite
backend)` now appears in `logs/gateway.log` — the first time the store has ever loaded in
production. The backfill's `verify` pass confirmed row-count agreement and **byte-identical WKB
across 10,000 sampled keys** resolved through both paths. End-to-end, `containment=exact` and
`containment=fuzzy` now return **different** result sets (38 vs 35 for a `Saint*`-within-France
query, all 38 `ccodes:[FR]`) — which is itself the proof the exact path is live, since with
`reader=None` `prepared` stayed unset and `hit_matches` fell straight through to the fuzzy test.

Three problems were mitigated beyond the spec below; see place#165 for the full write-up:
`__contains__`/`__len__` also read the index (and `__len__` would full-scan a `WITHOUT ROWID`
table, so the row count is cached in a `meta` row); shard reads moved to `os.pread` because
`seek()`-then-`read()` on a shared handle returns another thread's bytes; and connections are
keyed per-process as well as per-thread, since a fork-inherited connection is unusable and
`h3_stage` is a second consumer. Turning the feature on also meant the Shapely refine had to
move off the event loop (`apply_containment_async`), which in turn required a lock on the
cached `ResolvedRegion`.

*Original estimate, kept for the record — benchmarked on the gateway host against the live store
(684,076 real entries parsed from a 60 MB prefix of `index.json`, extrapolated to the full
11.5 M):*

| | `index.json` today | SQLite |
|---|---|---|
| gateway RSS | **5.4 GB** | **~0** (pages read on demand) |
| cold start | parse 1.02 GB of JSON | open a file |
| on-disk size | 1.02 GB | **0.39 GB** (34 bytes/row) |
| lookup | O(1) after the 5.4 GB | **9.7 µs/key** warm |
| build time | — | **~31 s** for 11.5 M rows (374 k rows/s) |

Schema — `WITHOUT ROWID` so the key *is* the B-tree, and the shard filename reduces to its
number (`geom_shard_0001.bin` → `1`):

```sql
CREATE TABLE geom(k TEXT PRIMARY KEY, shard INT, off INT, len INT) WITHOUT ROWID;
```

Three changes in `processing/geom_store.py`:

1. **`GeomStoreReader.__init__`** — prefer `index.sqlite` when present, fall back to `index.json`
   otherwise, so nothing breaks before the backfill runs and old stores keep working. Open with
   `check_same_thread=False` and `PRAGMA query_only=1`; give each thread its own connection
   (the gateway is async, and a shared connection would serialise).
2. **`_read_wkb`** — one `SELECT shard, off, len FROM geom WHERE k=?` in place of the dict
   lookup. The existing `lru_cache` and open-handle pool are unchanged and still absorb hot keys.
3. **`consolidate_geom_store`** (`:343`) — write `index.sqlite` alongside `index.json`, same
   `.tmp` + atomic-rename discipline it already uses.

Plus a **one-off backfill** for the store that exists now, which must not re-run consolidation:
stream `index.json` with a incremental parser (or `ijson`) → `executemany` in batches →
atomic rename. ~31 s of insert on top of however long the streaming parse takes; the peak memory
is one batch, not the whole index.

Verification before it goes near the gateway: sample ~10 k keys, resolve each through both the
old dict path and the new SQLite path, and assert byte-identical WKB.

### 5.4 The containment hierarchy — what it is, what it buys, and why it goes last

The proposal — slice overlapping polygons into non-overlapping fragments, link each fragment to
the label(s) of the originals, and reassemble the full extent from labels — is, stated precisely,
a **containment forest whose leaves partition the covered area**. England, Scotland and Wales are
leaves; "Great Britain" is their parent, reconstructed by unioning them; each is itself
subdivided, so the forest is deep.

**What it buys, beyond selection:**

- **The union machinery is already three-quarters built, and this unifies it.** The place#140
  coverage footprint is `unary_union` of *every* polygon in a bucket — i.e. **the union of the
  forest's roots**. The per-feature dissolve of §3 is the same operation at a leaf. Ancestor
  reconstruction is the same operation at every node in between. One dissolve engine, three
  products: the low-zoom mottle, a clean feature, and a reassembled ancestor. That is the overlap
  the brief identified, and it is real.
- **Legible fills.** Stacked translucent polygons darken with overlap depth; a partition renders
  evenly, and overlap depth becomes something you can *style deliberately* rather than suffer.
- **It is a first pass at `within` edges** for the v4 graph model / PLATO containment relations —
  derived geometrically, to be corrected by attestation rather than trusted outright.

**Why it is not the next thing to build:**

- **Most of the reported problem is temporal, and §5.1 removes it** for a fraction of the cost.
  403 distinct spans across 591 features at Berlin z6: these are versions, not rivals.
- **Containment overlap is not a defect.** A state inside a country is information. Any slicing
  scheme must preserve it, which is exactly why the proposal needs the hierarchy — but it means
  the expensive part (a true planar arrangement) is only needed for *sibling partial* overlaps,
  which are rare once containment is factored out.
- **Scale.** A global arrangement over ~18 M OSM polygons is not a single pass. Containment
  testing (STRtree + `contains`) is tractable; genuine slicing of partial overlaps should be
  scoped to the pairs that need it.
- **Fragment→label links cost tile bytes** — an array-valued attribute encoded as a delimited
  string, in a pipeline already fighting a 500 KB ceiling (place#160).

**Staged, if it is pursued:** `un` (~200 polygons) → `ukhc` / `vob_*` (dozens–hundreds) → `ohm`
at a single date → `osm`. Each stage is independently useful, and the first two are small enough
to prove the model in days rather than months.

---

## 6. What every bucket ends up with

| bucket class | points | shapes | extent | labels |
|---|---|---|---|---|
| point-only (`gn`, `gb`, `tgn`, …) | heatmap → circles | — | — | from points, z8+ |
| polygon-only (`po`, `kain_par`, `un`, …) | — | z8+ | mottle z0–7 | ✓ |
| line-bearing (`pl`) | heatmap → circles | z8+ | **mottle from buffered lines** | ✓ |
| hybrid (`wd`, `hgis`) | heatmap → circles | z8+ | mottle from polygons ∪ lines | ✓ |
| banded admin (`osm`, `ohm`, `osm_misc`) | — | per-band | — *(banding is its low-zoom strategy)* | ✓ per band |

The banded buckets keep admin-level banding rather than a footprint: they are a *context
overlay* where real country outlines at z2 are the point of the layer, and dissolving them into
one blob would destroy it (`plan-tiling-fixes-159-160.md` §1).

---

## 7. Effect on the in-flight place#159/#160 plan

Everything measured there stands. Three changes of shape:

| §  | change |
|---|---|
| §3.1 | Labels move into the **same** source-layer marked `label:1`, not a `<bucket>_labels` layer (§3.1 above). |
| §3.1f | **Withdrawn.** No multi-layer `tile-join`, so no `-l` sequence and no single-input guard. |
| §3.1h | The per-namespace deferral becomes §3 of *this* plan rather than an afterthought — and gains a blocker the old framing missed: a separate labels vector-layer would have been given its own heatmap by `loadGazetteerStyle`. |
| §3.2a | Unchanged and still required: `tile-join -pk`, plus failing the build on tile-join's skip messages. |
| §3.3 | Verifier gains two assertions: `label:1` features present in proportion to shape features; every `region_source` bucket publishes a `shapes` channel. |

The 24 h retile of `osm`/`ohm`/`osm_misc` is unchanged in cost and now carries the label channel
in its final form rather than one that would need revisiting for the gazetteers.

---

## 8. Order of work

**No retile required (whg3 only):**

1. `_heat` gains `!has label`; `_circle` gains `!has label` (§3.2). Harmless before labels exist,
   and prevents the contamination the moment they do. **DONE — `whg3` commit on `staging`,
   30 July 2026.**

**Blocked on a data convention, not on effort (§1.5):**

2. Temporal filter on map layers (§5.1). The largest measured win, but it must not ship before
   the `(2025, 2025)` stamps are fixed, or it blanks `osm` / `osm_misc` / `tgn` / `nl`. The
   client-side half is a few lines; the encoding fix is the work — **now tracked as place#164**
   (`plan-temporal-model.md`), which also changes what the client half looks like: two filter modes (*definitely* vs
   *possibly* alive) rather than one range test plus an "+Undated" escape hatch.
3. Instant-in-time lock toggle (§5.1.3) — follows (2); useless before it.

**⚠️ ENTRY CONDITION FOR THE RETILE — read before scheduling one.**

`plan-temporal-model.md` (place#164) commissions a **re-ingestion** of `wd` / `osm` / `ohm` / `gn` /
`pl` / `tgn` from refreshed dumps. Re-ingesting `osm`/`ohm` forces a retile of exactly the three
buckets below, so **that plan runs first and hands over here at the point re-ingestion completes.**
Retiling before then means paying the 24 h-per-bucket cost twice *and* publishing the wrong
temporal encoding for another cycle.

The retile is gated on **four** things, all of which change tile content:

| gate | issue | why it gates |
|---|---|---|
| temporal encoding fixed **and** re-ingested | place#164 | `start`/`end` values in every feature |
| `tile-join -pk` + skip-message failure + verifier | place#160 | tiles currently discarded at the join |
| labels channel (`label:1`) | place#159 | new features in the tileset |
| containment-hierarchy **test decided** (§9.4) | place#166-adjacent | if adopted, fragment→label links add an array-valued attribute |

#### ✅ ALL FOUR GATES CLEARED — 7 August 2026

| gate | state |
|---|---|
| temporal encoding fixed **and** re-ingested | ✅ live as `places_h3ccode-20260805t120000z` (place#164 closed) |
| `tile-join -pk` + skip-message failure + verifier | ✅ place#160 — `ohm` rebuilt clean, 0 skips, land-coverage assertion passes |
| labels channel (`label:1`) | ✅ place#159 — 12,713 anchors / 12,713 place_ids / 0 duplicates on `ohm`; all 27 tilesets rebuilt |
| containment-hierarchy test decided | ✅ **DEFERRED by SG, 7 August 2026** — see below |

**The containment-hierarchy decision: deferred, not rejected.**

§5.4's own argument carried it. Most of the reported problem is *temporal* — 403 distinct spans
across 591 features at Berlin z6 are versions, not rivals — and §5.1's date filter removes it for
a fraction of the cost. Containment overlap is information rather than a defect, so the expensive
part (a true planar arrangement) is only needed for sibling *partial* overlaps, which are rare
once containment is factored out. Against that, it was the only thing standing between a finished
pipeline and a publishable map.

What it would still buy is unchanged and worth revisiting later: one dissolve engine serving the
low-zoom mottle, per-feature dissolve and ancestor reconstruction; evenly-rendering fills with
overlap depth as a deliberate style choice; and a geometric first pass at `within` edges for the
v4 graph model. If pursued, take §5.4's staged route — `un` (~200 polygons) → `ukhc` / `vob_*` →
`ohm` at a single date → `osm` — where the first two stages prove the model in days.

**The retile is therefore unblocked.** The tilesets are already built and verified, parked at
`/vast/ishi/tiles-verify/` (27 buckets; `osm` 966 MB, `ohm` 702 MB, `wd` 1.84 GB). What remains is
publication only:

1. deploy the tilesets to the tileserver, **and**
2. regenerate + `git push origin production` the `tileboss` style in the same operation.

Neither alone: tiles-without-style makes place#159 worse (labels drawn from polygons *and*
anchors), style-without-tiles blanks every boundary label. Confirm afterwards with
`grep -c '"has", *"label"' tileserver/styles/whg-context/style.json`.

**⚠️ AND ONE THING THAT IS NOT A TILE CHANGE, BUT MUST SHIP WITH THE RETILE.**

The `tileboss` style must be regenerated and pushed **in the same operation** as the retile —
not before, not after. `scripts/build_whg_context_style.py` writes into the sibling `tileboss`
clone; the change is committed *here* (the label layers gain `["has","label"]`) but publishing it
is a separate `git push origin production` in **that** repo, which the tileserver picks up via
`git pull` on `/srv`.

Order matters in both directions, and neither failure is subtle:

| sequence | result |
|---|---|
| tiles deployed, style not yet | label layers draw from the polygons **and** the anchors — Nebraska gets its five fragment labels *plus* a sixth. #159 gets **worse**. |
| style deployed, tiles not yet | `["has","label"]` matches nothing — **every boundary label disappears**. |
| both together | one label per boundary, at its pole of inaccessibility. |

State as of 7 August 2026: the tiler emits anchors (place#159, committed), the generator emits
the filter (committed), and the **published `tileboss/tileserver/styles/whg-context/style.json`
still has the old unfiltered label layers** — deliberately, since deploying it alone would blank
the labels. Regenerate and push it as part of the retile, and check
`grep -c '"has", *"label"' tileserver/styles/whg-context/style.json` is non-zero afterwards.

Everything *except the retile itself* parallelises freely — the `tile_join` change and the verifier
are unit-testable today against the band GeoJSONL kept on `/ix1` (§8.1), and the dump downloads
(148 GB `wd`, 92 GB `osm`) are the real long pole and should start first, since they block nothing
and are blocked by nothing.

**Retile of `osm` / `ohm` / `osm_misc` (the in-flight plan, amended by §7):**

3. `tile-join -pk` + skip-message failure + `verify_tileset.py` (place#160).
4. Labels channel for the banded buckets; style label layers gain `['has','label']`.
5. `_label` symbol layer + `_label` in `SHAPE_SUFFIXES`; label-click selection (§5.2).

**Retile of the per-namespace gazetteers (cheaper — 16–64 GB tiers):**

6. Channel model in `_stream_bucket`: split the majority vote into per-type channels; extent from
   polygons ∪ buffered lines; per-feature dissolve (§3).
7. Labels channel for gazetteers.
8. Registry: assert `region_source` buckets publish shapes; surface partial-coverage sources
   (`wd`) honestly.

**Separately scoped:**

9. **`index.sqlite` for the geom store (§5.3.1).** Reachability is verified, so this is no longer
   blocked on a decision — it is ~1 day of contained work in `processing/geom_store.py` and it is
   worth doing *whether or not* the endpoint follows, because it also switches exact containment
   back on. Independent of every retile.
10. `/api/geometry/<place_id>` (§5.3) — trivial once (9) lands; pointless before it.
11. Containment hierarchy, staged from `un` upward (§5.4).

### 8.1 Scratch artefacts left on CRC — keep until the retile lands, then delete

`/ix1/ishi/data/tiles/_step0/` holds the working files from the place#160 diagnostic
(29–30 July 2026, jobs 10687880 / 10688065 / 10688091):

| artefact | what it is |
|---|---|
| `ohm.{continental,country,state,district,local}.geojsonl` | ohm's five band streams, ~5.9 GB apparent (2.7 GB on disk) |
| `step0.sbatch`, `step0b.sbatch`, `step0d.sbatch` + `*-<jobid>.log` | the measurement record behind §7 and `plan-tiling-fixes-159-160.md` §2 |
| `g_*.log`, `v_*.log`, `d_*.log` | per-variant tippecanoe / tile-join output, including the `Skipping this tile` lines that identified the cause |

**Keep them until §8 items 3–5 have landed and been verified.** Regenerating the band files
means re-running the streaming pass over ohm's staged parquet plus geom-store reads — the bulk
of a 1 h 17 m job at 48 GB on `htc`. While they exist, the `tile-join -pk` change, the
skip-message check and the labels channel can all be exercised against real ohm band data in
minutes rather than hours:

```bash
tippecanoe --output /tmp/x.mbtiles --force --layer ohm --minimum-zoom 0 --maximum-zoom 2 \
  --simplification 10 --detect-shared-borders --coalesce-densest-as-needed \
  --no-tile-compression --read-parallel /ix1/ishi/data/tiles/_step0/ohm.continental.geojsonl
```

**Then delete the directory.** They are a subdirectory, so they are outside the
`/ix1/ishi/data/tiles/*.mbtiles` glob and cannot be picked up by the pipeline or deployed —
but they are 2.7 GB of undocumented scratch on a shared volume, and the numbers they support
are already written down here and in `plan-tiling-fixes-159-160.md` §2. Nothing needs them
after the retile.

Note the band `.geojsonl` files produced by a *normal* run are written to
`/ix1/ishi/data/tiles/` itself and are **not** cleaned up either — only the per-band `.mbtiles`
intermediates are (`generate_tiles.tile_join`, `:1397-1402`). `osm*.geojsonl` and
`osm_admin*.geojsonl` totalling tens of GB are sitting there from previous runs, including
`osm_admin.*` from the May 2025 pre-rename era. Worth a sweep at the same time, though that is
a separate cleanup from this plan's scratch.

---

## 9. Ownership, and the decisions this plan does not make

This spans three repos, so it cannot be handed to one team wholesale.

| repo | items | notes |
|---|---|---|
| **`indexing`** | §3 channels, §3.3 label anchors, §3.2a `tile-join -pk` + skip-message failure, §3.3 verifier, §5.3.1 geom-store index | the bulk of it |
| **`indexing`** (separate track) | temporal encoding — **place#164**, `plan-temporal-model.md`. Ingestion, ES mapping, reader, query semantics; the tiling side is trivial. The ingest fix is needed *regardless of backfill*, so future re-runs stay correct. | own issue, own owner; gates §5.1 only |
| **`whg3`** | §3.2 layer filters *(done)*, §4 `_label` symbol layer, §5.2 label-click selection, §5.1.2 client half of the temporal filter, Regions status-line wording | needs its own owner; §4/§5.2 must land in the **same release** as the label channel or labels ship invisible |
| **`tileboss`** | `style.json` regeneration — band metadata **and** `['has','label']` on the label layers | generated by `scripts/build_whg_context_style.py` here, pushed from the sibling clone |

**Startable today, no dependencies, no retile:**

- §5.3.1 `index.sqlite` — measured, self-contained, and switches exact containment back on.
- §3.2a `tile-join -pk` + failing on skip messages, and the §3.3 verifier — unit-testable before
  any retile, and where most of the place#160 fix lives.

**Four decisions that are not mine to make, and which the plan deliberately leaves open:**

1. ~~**The date-stamp convention for contemporary sources** (§1.5). Open-ended `end = 9999`, or no
   span at all?~~ **Superseded by `plan-temporal-model.md` / place#164.** The question was wrongly posed: `(2025, 2025)`
   is not a provenance artefact needing a convention to neutralise it, it is an *attestation*
   being recorded in the wrong sub-field, and both candidate answers were workarounds (`end = 9999`
   over-claims "still alive today"). The remaining decision is narrower and belongs to place#164:
   the **per-source** encoding table — specifically the two classed `?`, `vob_*` (census snapshots:
   lifespan or attestation?) and `wd` (per-statement). Still true that the next retile of
   `osm` / `tgn` / `nl` is the cheap moment to apply it, and that shipping that retile without it
   locks the wrong encoding in for another cycle.
2. ~~**Should a historical date range hide contemporary sources?**~~ **DECIDED (SG, 30 July 2026):
   yes — it hides them.** That is what the filter means, and the alternative was worse: the
   "+Contemporary" / include-21st-century toggle is **abandoned**, because a year heuristic
   misclassifies genuine 21st-century history (measured: 5,405 OHM records have `start >= 2000` and
   `end <= 2026`, comparable to the 8,288 alive in 1500). Correct attestation encoding
   (`plan-temporal-model.md`) makes the toggle unnecessary anyway: an OSM boundary is *possibly*
   alive in 1500 because its `start.earliest` is unbounded. **UI consequence that must ship with
   it:** the Regions status line has to distinguish "no boundaries at this level" from "this source
   has nothing in this period", or it reads as breakage.

3. ~~**`/api/geometry/<place_id>` — gateway endpoint or sidecar?**~~ **DECIDED (SG, 30 July 2026):
   the gateway.** Co-location is verified (§5.3), so the sidecar buys nothing. Gated on the
   geom-store index replacement — **place#165** — which must land first; the endpoint is cheap
   after it and expensive before it.

4. ~~**Whether to pursue the containment hierarchy at all**~~ **DECIDED (SG, 30 July 2026): yes —
   but prove it on a sensibly-sized polygon gazetteer first, explicitly NOT `osm`.** Start at `un`
   (~200 polygons) per the staging in §5.4; `ukhc` (92) is an even cheaper second opinion. **The
   corpus retile waits on the outcome of that test**, because if the hierarchy is adopted the
   fragment→label links add an array-valued tile attribute — i.e. it changes tile *content*, and a
   retile that predates the decision would have to be redone. See §8's entry condition.

**One thing to be sceptical of:** §5.4 is the part I am least confident about. The measurements
say most of OHM's overlap is temporal and dissolves under a date filter, so the hierarchy may buy
much less than it appears to — but that judgement rests on two sampled tiles (Berlin, Paris), and
a wider sample could change it. Measure more before committing to the expensive version.

## 10. Risks and open questions

| | |
|---|---|
| `polylabel` cost across ~18 M OSM polygons | Simplify first; measure on `ohm` (smaller); it is a per-feature cost on a pass that already reads every feature |
| Per-feature `unary_union` cost | bbox-intersection precheck skips single-part and disjoint-part features, which is nearly all of them |
| Line buffering distorts extent for very long sparse routes (`pl`) | Buffer at the footprint's own simplify tolerance so it cannot claim more area than the footprint can represent; visual check |
| Adding channels grows merged tiles, and place#160 shows the join ceiling is real | Labels are points (cheap); extent is one simplified feature; `-pk` must land first — hence the ordering in §8 |
| Instant-in-time mode changes what users see by default | Make it opt-in for gazetteers, default only for `ohm` area selection where the range mode is demonstrably useless |
| Temporal filter blanks contemporary sources | **Measured, real** (§1.5). Ordering in §8 gates it behind the stamp fix; do not reorder |
| Geom store endpoint costs 5.4 GB of gateway RSS | **Verified** (§5.3). Replace `index.json` with an on-disk index first; the endpoint is cheap after that and expensive before it |
| Hierarchy work expands without bound | Gate each stage on the previous one being useful on its own; `un` first |
