# Plan — tiling fixes place#159 (per-fragment labels) + place#160 (missing low-zoom tiles)

> **Status:** proposed, not started
> **Written:** 29 July 2026
> **Issues:** [place#159](https://github.com/WorldHistoricalGazetteer/place/issues/159),
> [place#160](https://github.com/WorldHistoricalGazetteer/place/issues/160)
> **Related:** place#140 (low-zoom coverage), place#131 (per-feature temporal props),
> place#156 (Atlas Areas UI — client half already fixed in whg3)
> **Scope:** one combined pass, one retile of `osm` / `ohm` / `osm_misc`.
>
> ⚠️ **Superseded in framing by `plan-atlas-data-architecture.md`** (30 July 2026; formerly
> `plan-tileset-architecture.md`). Every measurement below still stands — read §2 for the
> place#160 diagnosis — but see that plan's §7 for what changed: labels move into the same
> source-layer marked `label:1`, so §3.1f (multi-layer `tile-join`) is **withdrawn**.

---

## 0. Why one pass

Both fixes land in the same three places, and separating them makes each one worse:

| | #159 (labels) | #160 (missing tiles) |
|---|---|---|
| `generate_tileset()` flags — `generate_tiles.py:393-454` | new pass, new layer name | flag/budget change |
| `tile_join()` — `generate_tiles.py:1345-1403` | must stop filtering to one layer | new post-join assertion |
| `_stream_bucket_banded()` — `generate_tiles.py:1266-1342` | new label sink | band assignment is a *cause* |
| tileboss `style.json` (band spec **and** label layers) | repoint `source-layer` | band zoom coverage is a *cause* |

Sequencing hazards:

- Label points **add bytes** to tiles already measured at 499,106 / 500,000. Doing #159
  first makes #160 worse.
- Doing #160 first by dropping features risks the *labels* being the features dropped.
- `tilegen_bands.load_bands()` reads the band spec from the **same** `style.json` that
  holds the `osm-label-*` / `ohm-label-*` symbol layers. Two passes = two style deploys and
  a window where the band spec and the style disagree.
- The retile is the cost, not the code: `osm` 192 GB / 24 h, `ohm` 96 GB / 24 h,
  `osm_misc` 192 GB / 24 h (`submit_tiles_slurm.py:79-83`), then push → config rewrite →
  restart → serving verify.

---

## 1. Constraint carried forward from place#140 — do not undo the coverage fix

**This is the governing constraint on #160, and it contradicts the fix the issue proposes.**

place#140 fixed a real regression: polygon gazetteers rendered a *deceptively sparse*
scatter at low zoom, so a user could not tell that a gazetteer covered a region at all.
Two mechanisms came out of that work and both must survive:

1. **`--coalesce-densest-as-needed` (`generate_tiles.py:423`) coalesces rather than
   drops.** Merging features keeps the *ink on the map* — coverage remains legible even
   where individual features become unresolvable. `--drop-densest-as-needed` deletes
   features outright, which reintroduces exactly the sparse-coverage illusion #140 fixed.
   **#160's primary suggested fix is therefore rejected as the primary remedy.**
2. **The dissolved `coverage: 1` footprint** — `_accumulate_coverage`
   (`:1079-1098`) → `_coverage_feature` (`:1101-1129`) → separate z0–7 tippecanoe pass →
   `tile_join` into the same source-layer (`:1643-1671`), with the real boundaries pinned
   to z8+ via `_BOUNDARY_MINZOOM` and `preserve_all=True`. whg3 styles `coverage:1` as a
   mottled fill. Live corpus-wide since 23 July 2026.

**The footprint is not the answer for `osm` / `ohm` either.** Those are a *context
overlay*: real country and continental outlines at z2 are the entire point of the layer.
Dissolving them into one blob below z8 — which is what the single-band polygon path does —
would destroy the layer's purpose. The banded admin buckets get their low-zoom
legibility from **admin-level banding** instead (continental/country only at z0–4), which
is the same principle applied differently.

So the design rule for this plan:

> **No feature may be dropped to make a tile fit.** Reduce *bytes per feature* and raise
> the per-tile budget instead. Any change to `generate_tileset` flags must be scoped to
> the banded fixed buckets, never applied globally, so the #140 machinery on the
> single-band path is untouched.

---

## 2. Root causes to confirm before changing anything (step 0)

#160 attributes the missing tiles to the 500 KB ceiling. The code shows a **second,
independent cause** that produces the same symptom, and the remedies differ.

**Cause A — per-tile byte ceiling.** No `--maximum-tile-bytes` is set anywhere, so
tippecanoe's 500,000-byte default applies to every non-`preserve_all` pass, including all
five banded admin passes (`:1596-1599`). `tile-join` has no thinning strategy, so a
band-sum over budget can only skip the tile.

**Cause B — band zoom coverage.** In `scripts/build_whg_context_style.py:49-60` only two
of five admin bands reach below z5:

```
continental  z0-4   boundary in ["0","1"]
country      z0-8   boundary in ["2"]
state        z3-10  boundary in ["3","4"]
district     z5-10  boundary in ["5","6"]
local        z7-10  boundary in ["7","8","9","10","11"]
```

Any area whose OHM features are all level 3+ produces **no z0–z4 tile at all**, whatever
tippecanoe does. On top of that, `assign_band` returning `None` drops features silently
(`generate_tiles.py:1322-1325`, warning only at `:1333-1334`). OHM's historical polities
are heavily level 3+, which fits "OHM draws nothing at z0" at least as well as Cause A.

### Step 0 diagnostic (cheap, do first)

On CRC, against `/ix1/ishi/data/tiles/ohm.mbtiles` and the staged GeoJSONL:

1. Enumerate present tiles at z0–4 (`SELECT zoom_level, tile_column, tile_row,
   length(tile_data) FROM tiles WHERE zoom_level <= 4`) — remember mbtiles rows are **TMS**
   (y flipped) and that `tiles` may be a view over `map`/`images` after `tile-join`.
2. For each *missing* tile, count how many features in each band's GeoJSONL intersect it.
   - **Features present but tile absent → Cause A.** Confirm by reading
     `tile_size_desired` from the band mbtiles metadata.
   - **Zero features in the low-zoom bands → Cause B.**
3. Count features whose `boundary` matched no band (`assign_band` → `None`) per namespace.
4. Record the byte breakdown of a representative over-budget z2/z3 tile by attribute, so
   the pruning in §3.2 is sized from measurement rather than guessed.

Findings go in this document before any code changes.

### Step 0 findings — 29 July 2026

**Cause A confirmed. Cause B ruled out.** Evidence, from `/ix1/ishi/data/tiles/{ohm,osm}.mbtiles`:

Tile census (`SELECT zoom_level, COUNT(*), MAX(length(tile_data)) … WHERE zoom_level<=6`):

| zoom | ohm tiles | ohm max bytes | osm tiles | osm max bytes |
|---|---|---|---|---|
| 0 | **absent** | — | 1 | 96,072 |
| 1 | 2 / 4 | 219,469 | 4 / 4 | 65,420 |
| 2 | 13 / 16 | 337,938 | 16 / 16 | 60,838 |
| 3 | 55 / 64 | 460,135 | 62 / 64 | 231,380 |
| 4 | 196 / 256 | 438,793 | 232 / 256 | 112,837 |
| 5 | 614 | 499,106 | 472 | 491,875 |
| 6 | 2,027 | 497,027 | 1,516 | 300,185 |

No zero-byte rows anywhere — the missing tiles are absent rows, matching the reported
HTTP 204.

The `strategies` metadata (array indexed by zoom) is decisive. ohm z0:

```json
{"coalesced_as_needed":3314,"tiny_polygons":355,"tile_size_desired":3471465}
```

tippecanoe **built** the z0 tile, coalesced 3,314 features into it, and it still wanted
3,471,465 bytes — then dropped it at the 500,000 ceiling. A band gap (Cause B) would have
produced **no strategy entry at all** for that zoom. So the continental (z0–4) and country
(z0–8) bands did run; the band spec is correct and needs no change.

Confirmed the band spec independently: the live tileboss `style.json`
(`metadata.whg:tilegen.buckets`, branch `production`) carries all five admin bands for both
`osm` and `ohm`. **§3.2c is therefore dropped from the plan** — no band-coverage repair
needed. Making the `assign_band → None` drop loud is retained as a cheap hygiene item.

**Red herring, recorded so it isn't chased again:** `ohm.continental.geojsonl` and
`ohm.country.geojsonl` are absent from `/ix1/ishi/data/tiles` while `osm`'s equivalents are
present. This is *not* evidence of a missing band — `_stream_bucket_banded` truncate-creates
every band file (`:1298-1300`), so a zero-feature band leaves a 0-byte file rather than no
file, and the mbtiles metadata proves both bands were tiled. They were deleted after the
run (most likely manual `/ix1` reclamation).

**Why ohm and not osm.** osm records **no `coalesced_as_needed` and no `tile_size_desired`
at all below z7** — its low zoom never approaches the budget; it only starts coalescing at
z7 (471,373 features coalesced, 3.05 MB desired). ohm is 2.5–3.5 MB desired at every zoom
from z0. This is OHM stacking centuries of temporally-overlapping polities that are all
valid at low zoom, exactly as the issue proposed. It also means **`osm`'s low zoom is not at
risk** and the #160 remedy only has to rescue `ohm` — though `osm` z5 at 491,875 / 500,000
is close enough to warrant the same treatment prophylactically.

#### Step 0b — attribute pruning REFUTED; the tiles are geometry-bound (job 10687880)

**The §3.2a/b hypothesis is wrong, and with it the claim that #159 enables #160.** Measured,
not assumed.

Byte census over the 3,705 features in ohm's two low-zoom bands:

```
geometry:   3,349,455,047 bytes
ALL props:          1,391,774 bytes      ← 2,406× smaller
  largest single prop: name_local 144,517
```

Per-band source volume:

| band | features | GeoJSON bytes | per feature | osm equivalent |
|---|---|---|---|---|
| continental (boundary 1) | 487 | 2.02 GB | **4.15 MB** | 177 KB *total* |
| country (boundary 2) | 3,218 | 1.33 GB | 413 KB | 120 MB total (~250 feat) |

Attribute pruning therefore buys almost nothing. z0 `tile_size_desired`, continental band:

| variant | attributes kept | z0 desired | Δ |
|---|---|---|---|
| V0 | all | 2,212,284 | — |
| V1 | drop 12 `name_<lang>` + `aat` | 2,205,443 | −0.3 % |
| V2 | `boundary`/`place_id`/`start`/`end` | 2,186,487 | −1.2 % |
| V3 | V2 − `place_id` | 2,176,885 | **−1.6 %** |

**z0 was absent in all five variants** — but see the correction in §0c below: this is *not*
because the bands failed to produce a z0 tile. `tile_size_desired` is what tippecanoe wanted
*before* reduction, not what it stored; reading it as the stored size was an error.

**The bounded budget raise made things worse, not better.** V4 (`--maximum-tile-bytes
1000000`): z2 fell from 13 tiles to **12**, and z1 max bytes rose to 456,021. A larger
budget makes tippecanoe coalesce *less* (`coalesced_as_needed` 3,903 → 2,589), so tiles grow
and any that still exceed the higher limit are dropped anyway. Trading many small tiles for
fewer large ones is a net loss. **§3.2b is withdrawn.**

Consequences:

- **§3.2a (attribute pruning) is withdrawn as a remedy for #160.** Keep it only as tidiness
  once #159 moves names to the labels layer — it is worth ~1 % of tile bytes, not a fix.
- **#159 does not enable #160.** They are independent defects. The one-pass argument still
  holds on its other grounds (same three functions, same `style.json`, one 24 h retile), and
  the byte census makes #159 *cheaper* than feared: a point layer's attributes are noise next
  to 3.35 GB of coordinates.
- **The real defect is geometry volume at low zoom.** OHM's continental features average
  4.15 MB of coordinates each — full-detail coastlines on supranational polities — where a
  z0 tile spans the entire world at 256 px. `--simplification 10` is not reducing them
  enough, and `--detect-shared-borders` may be actively constraining how far simplification
  can go, since it forces shared edges to simplify identically.

#### Step 0c — CORRECTION: the proximate cause is `tile-join`, not tippecanoe

The control run (G0, exact production flags) reported the **stored** per-band z0 tile size
for the first time, and it changes the diagnosis again:

```
continental  z0 stored = 475,594 bytes   (desired 2,212,284 → coalesced to fit)
country      z0 stored = 425,743 bytes   (desired 1,901,954 → coalesced to fit)
                                sum = 901,337
JOINED z0                            = ABSENT
```

**Both bands successfully produce a z0 tile under the 500,000-byte ceiling.** Coalescing is
doing exactly its job — this is the place#140 machinery working as designed. The tile is
then destroyed at the merge: `tile-join` sums the two bands to ~901 KB, exceeds its own
500,000 limit, and **skips the tile entirely and silently**.

This is precisely mechanism (2) in the issue text, which deserved more weight than the byte
ceiling it was paired with:

> "`tile-join` has no thinning strategy at all — over the limit, it can only skip the tile.
> Each band may be comfortably under 500 KB on its own while their sum is not, which is
> exactly the shape of the failure."

It explains the whole observed pattern:

- **Band overlap × per-band budget saturation predicts the holes.** At low zoom every band
  crams the whole world into one tile, so each one saturates its own 500 KB budget; 2–3
  saturated bands then sum to 0.9–1.5 MB. Matching 0/1 tiles at z0, 2/4 at z1, 13/16 at z2,
  55/64 at z3.
- **High zoom is unaffected even with more bands.** Measured on prod ohm: z7 max 437,589 /
  z8 406,703 / z9 390,365 / z10 392,921, with **zero** tiles above 490 KB at any of them. Four
  bands overlap at z7, but each tile covers a small area so per-band tiles are content-sized
  rather than budget-bound, and the sum stays well under. (This corrects an earlier inference
  from the z5/z6 maxima that skipping was happening at all zooms — it is confined to z0–z6,
  and materially only z0–z3.)
- **Why `osm` is unaffected:** its per-band low-zoom tiles are ~60 KB, so even four bands sum
  well under the ceiling.
- **Why the flag sweeps in §0b all failed:** every variant was fixing the wrong stage. Each
  band was already under budget in every variant; the join dropped the result regardless.

Confirmed `tile-join` accepts **`-pk`** (its spelling of `--no-tile-size-limit`) in the
installed v2.78.0. Job 10688091 measures the two candidate remedies directly.

**Revised remedy shortlist:**

1. **`tile-join -pk`** — keeps the merged tile at its true size. The issue dismissed this as
   "multi-MB each … a poor trade", but the measured z0 merge is **901 KB, not multi-MB** — one
   tile for the entire world, fetched once. That is a materially different trade from the one
   the issue rejected, and it drops **nothing**, which is the strongest fit with §1.
   Needs a measured cap and a check on the largest merged tile corpus-wide, not a blanket
   removal.
2. **Make the sum fit** — reduce per-band low-zoom bytes so 2–4 bands stay under 500 KB
   (each band would need ≤ ~250 KB at z0). The geometry levers in §0d are the lever for this;
   it keeps browser payloads small but requires real geometry reduction.
3. **Reduce band overlap at low zoom** — e.g. let only `continental` own z0–2 by starting
   `country` at z3. Cheap, but the style renders country lines from z0 and country labels
   from z2, so it changes what the map shows; a product decision, not a pure fix.

(1) and (2) are complementary: apply geometry reduction to keep tiles small *and* raise the
join ceiling so a merge is never silently discarded again.

#### Step 0d — geometry levers (job 10688065)

Still worth the numbers, since remedy (2) depends on how far geometry can be reduced — and
because 4.15 MB of coordinates per continental feature is indefensible regardless of which
stage drops the tile.

Testing, on the same band files, against the target "z0 present at ≤ 500,000 bytes":

| variant | levers |
|---|---|
| G0 | production flags (control) |
| G1 / G2 | `--simplification 40` / `100` |
| G3 | drop `--detect-shared-borders` |
| G4 | `--tiny-polygon-size 8` (merge sub-pixel islands/rings) |
| G5 | `--drop-smallest-as-needed` instead of densest |
| G6 | `--simplification 40` + `--tiny-polygon-size 8` + `--drop-smallest-as-needed` + coalesce |
| G7 | G6 + attribute pruning |

Note on §1 for `--drop-smallest-as-needed`: dropping *sub-pixel* features at z0 is not the
same trade as `--drop-densest-as-needed`. It removes what is literally invisible at that
zoom while leaving the large polygons that carry coverage legibility — so it does not
reintroduce the place#140 sparse-coverage problem the way dropping the densest features
would. It is admissible where dropping the densest is not.

**Results (job 10688065 COMPLETED, 01:08:46).** z0 stored bytes per band, and whether a
size-driven reduction strategy had to fire at all:

| variant | continental z0 | size-strategy? | country z0 | size-strategy? | **sum** |
|---|---|---|---|---|---|
| G0 control | 475,594 | yes (desired 2,212,284) | 425,743 | yes (1,901,954) | 901,337 |
| G1 simp 40 | 491,113 | yes (2,085,785) | 414,087 | yes | 905,200 |
| G2 simp 100 | 494,532 | yes (2,071,265) | 412,472 | yes | 907,004 |
| G3 no shared-borders | 374,073 | yes (**1,232,134**) | 454,018 | yes (871,420) | 828,091 |
| G4 tiny-poly 8 | **361,006** | yes (1,539,278) | 424,095 | yes | 785,101 |
| G5 drop-smallest | 388,208 | yes (dropped 439) | 416,000 | yes | 804,208 |
| G6 combined | 467,028 | **no** | 481,191 | yes (607,321) | 948,219 |
| G7 combined + prune | 441,512 | **no** | **267,584** | **no** | **709,096** |

Three findings:

1. **Simplification alone is useless (G1/G2) — stored tiles are budget-bound, not
   content-bound.** `--coalesce-densest-as-needed` reduces only until the tile fits, then
   stops, so space freed by simplification is immediately spent retaining more features. The
   stored size lands just under 500,000 regardless. This is why raising simplification made
   tiles marginally *bigger*.
2. **`--detect-shared-borders` really was constraining simplification.** Dropping it (G3)
   nearly halves continental `tile_size_desired`, 2,212,284 → 1,232,134 (−44 %).
   `--tiny-polygon-size 8` (G4) is next best at −30 % and gives the smallest stored
   continental tile (361,006).
3. **No combination gets the z0 sum under 500,000.** The best case, G7 — aggressive geometry
   reduction *plus* full attribute pruning — still sums to **709,096**. G7 does get both bands
   out of budget-bound territory (no size-strategy fires), which is where further geometry
   reduction would finally start shrinking tiles rather than being absorbed.

**Therefore `tile-join -pk` is necessary, not optional.** Remedy (2) "make the sum fit" is
not achievable at z0 without degradation far beyond what §1 permits.

The pre-simplify-in-stream fallback is not needed: G3/G4 show tippecanoe can reduce geometry
adequately once `--detect-shared-borders` stops blocking it, and the residual problem is the
join ceiling rather than tile content.

---

## 3. Implementation

### 3.1 Label points as their own layer (#159)

**Superseded framing:** an earlier draft claimed moving names off the polygons was a
prerequisite for fixing #160. Step 0 refuted that — attributes are 1/2,406 of tile bytes, and
the actual defect is at the join, not in tile content (§0b/§0c). #159 and #160 are independent
defects fixed in one pass for operational reasons only (shared functions, shared `style.json`,
one 24 h retile). The byte census does make this fix reassuringly cheap: a point layer's
attributes are noise beside 3.35 GB of coordinates.

**a. Label-point geometry.** New helper beside `_coverage_feature`
(`generate_tiles.py:~1101`):

```python
def _label_point_feature(feature: dict) -> dict | None:
    """Pole-of-inaccessibility point for a polygon feature, for the
    <bucket>_labels layer (place#159). Returns None for non-polygons."""
```

- Import lazily, matching the existing style at `:1112`: `from shapely.ops import polylabel`
  (verified present on shapely 2.1.2; it is **not** exported as `shapely.polylabel`).
- `polylabel` takes a **single Polygon**. Flatten with the existing `_polygonal_parts`
  (`:1067-1076`) and take the **largest-area** member of a MultiPolygon.
- Fallback chain on degenerate rings: `polylabel(...)` → `geom.representative_point()` →
  `geom.centroid`. Never emit a label point outside the geometry.
- Do **not** reuse the staged `repr_point`: that is shapely `representative_point()`
  (`helpers.py:378-415`), guaranteed-inside but often near an edge — it does not satisfy
  the issue's requirement and would place labels badly on concave regions.
- Tolerance: start at `_COVERAGE_SIMPLIFY_DEG`-scale (~0.008°) and simplify large polygons
  before `polylabel`; it is iterative and unbounded cost on a 100k-vertex ring.

**b. Properties on the label point.** Copy the polygon's property dict, because the
style's label layers filter on `boundary` and the whg3 date filter reads `start`/`end`:

- required: `place_id`, `namespace`, `boundary`, `name`, `name_local`, `name_<lang>` ×12,
  `start`, `end`
- omit: `aat` (labels are not type-filtered), `population`, `fcode`

**c. Emission.** Two sinks, because the two streaming functions are separate:

- `_stream_bucket` (`:1230-1261`) — hook at the `is_poly` branch (`:1251-1256`), alongside
  the existing `_accumulate_coverage` call. Write to `<bucket>.labels.geojsonl`.
- `_stream_bucket_banded` (`:1310-1328`) — **the path `osm`/`ohm`/`osm_misc` take, and the
  one with the visible bug.** It currently has no second-stream channel at all (docstring
  `:1286-1289`); add a per-band labels handle and extend the return tuple.

**d. Label zoom ranges are NOT the polygon band ranges.** The style's label layers use
their own zoom windows (`build_whg_context_style.py:166-172`, `lbl_min`/`lbl_max`):

| level | polygon band | label layer |
|---|---|---|
| continental | z0–4 | **z0–5** |
| country | z0–8 | z2–8 |
| state | z3–10 | z3–10 |
| district | z5–10 | z5–10 |
| local | z7–10 | z7–10 |

Continental labels are asked for at z5 where the continental polygon band stops at z4. So
add optional `label_minzoom` / `label_maxzoom` to `Band` (`tilegen_bands.py`), defaulting
to the polygon `minzoom`/`maxzoom`, and populate them from the same `admin_levels` table
that builds the style layers — one source of truth, no drift.

**e. Tiling + join.** Per band: `generate_tileset(labels_geojsonl, labels_mbtiles,
f"{bucket}_labels", ..., minzoom=band.label_minzoom, maxzoom=band.label_maxzoom)`. Points
are tiny, so these passes are fast and will not approach any budget.

**f. `tile_join` blocker — must be fixed or every label is silently discarded.**
`generate_tiles.py:1383` passes `-l layer_name`, and tile-join's `-l` is a *filter*
("keep only this layer"). Change `layer_name: str | None` to accept a sequence, emit one
`-l` per layer, and keep a single `-n f'WHG {bucket}'` (the `-n` is load-bearing — see the
comment at `:1379-1382`). Also guard the single-input rename shortcut at `:1360-1367`: a
labels mbtiles must never be the sole input.

**g. Style side.** `build_whg_context_style.py:132-152` `label_layer()`:
`"source-layer": src` → `f"{src}_labels"`. The `source` stays `src` (same vector source,
different source-layer). Regenerate → commit & push from the sibling tileboss clone.

**h. Deferred: per-namespace polygon gazetteers.** #159 says it affects "every other
polygon gazetteer". Those tilesets are cheaper to retile (16–64 GB tiers) and their label
layers are defined in whg3's `loadGazetteerStyle`, not in `style.json` — and below z8 they
render the nameless `coverage:1` footprint anyway, so fragment-repeat only shows at z8+.
Handle as a phase 2 with a companion whg3 issue; do not hold this pass for it.

### 3.2 Missing low-zoom tiles (#160)

The cause is `tile-join` silently skipping merged tiles over 500,000 bytes (§0c). Three
changes, in dependency order.

**a. `tile_join` must not silently discard tiles — REQUIRED.** Two parts:

1. Pass **`-pk`** so an over-size merge is written rather than skipped. Measured cost on ohm:
   z0 becomes one 901,149-byte tile (with current flags) or ~709,096 (with (b) applied); z1–z2
   ~600 KB; z7–z10 unchanged, since they were never near the ceiling. This is the only change
   that restores z0 at all — no combination of content reduction gets the sum under 500,000
   (§0d finding 3).
2. **Fail the build on tile-join's own skip messages.** tile-join already prints
   `Tile z/x/y size is N, >500000. Skipping this tile.` on stdout, and
   `generate_tiles.tile_join` (`:1388`) passes it straight through to the Slurm log while
   returning success. Capture stdout, count those lines, and treat any as a bucket failure.
   This is the cheapest possible guard against the exact silent failure that shipped, and it
   stays useful after `-pk` (it would fire if the flag were ever dropped).

**b. Geometry reduction on the low-zoom bands — RECOMMENDED, to keep the merged payload
small.** Measured effect on continental `tile_size_desired` at z0:

| lever | desired | Δ |
|---|---|---|
| control | 2,212,284 | — |
| drop `--detect-shared-borders` | 1,232,134 | **−44 %** |
| `--tiny-polygon-size 8` | 1,539,278 | −30 % |
| `--simplification 40/100` | 2,085,785 / 2,071,265 | −6 % (useless alone) |

Apply per band, low zoom only, as a new `Band` field. Do **not** bother with
`--simplification`: because stored tiles are budget-bound, simplification gains are absorbed
by coalescing retaining more features (§0d finding 1).

Note the trade on `--detect-shared-borders`: it exists so that a border shared between two
polygons simplifies identically in both, avoiding visible slivers between neighbours. Dropping
it at z0–z2 risks hairline gaps between adjacent polities at exactly the zooms where a pixel
is ~150 km. Needs a visual check at step 8 before it goes in; if it looks bad, use
`--tiny-polygon-size 8` alone (−30 %) and accept a slightly larger merged tile.

**c. Attribute pruning — OPTIONAL tidiness, not a fix.** Worth ~1 % of tile bytes (§0b). Once
§3.1 moves names to the labels layer the polygon layer only needs `boundary`, `place_id`,
`start`, `end`; pruning to that is free and slightly reduces the merged size (country z0
425,743 → 267,584 when combined with (b)). Implement as `Band.include_properties`, default
full. Never presented as the #160 remedy.

**Not doing, with reasons:**

- **`--maximum-tile-bytes` raise on the bands** — measured *worse*: at 1,000,000 the z2 tile
  count fell 13 → 12 and z1 bytes rose to 456,021, because a bigger budget means less
  coalescing, so tiles grow and any still over the (now higher) join ceiling are dropped anyway.
- **Per-band budget *cuts*** (`500000 / N_bands`) — would make the sum fit without `-pk`, but
  forces much harder coalescing at exactly the zooms place#140 cares about. Held in reserve
  only if `-pk` payloads prove too large in practice; z7–z10 measurements say they will not.
- **Band-coverage repair** — the bands were never the problem (§0c). Making the
  `assign_band → None` drop loud (`:1333-1334`) is retained as cheap hygiene.
- **`--drop-densest-as-needed`** — rejected per §1, and now unnecessary.
- **`--drop-smallest-as-needed`** — admissible in principle (sub-pixel features only) but
  measured no better than the alternatives (G5: 388,208 vs G4's 361,006) and it does drop
  features. Not needed.
- **`--extend-zooms-if-still-dropping`** — tried and removed before; rationale inline at
  `generate_tiles.py:404-410` (ran past z10 into z12+ and blew the 24 h wall).
- **Pre-simplify geometry in the stream** — unnecessary; tippecanoe reduces adequately once
  `--detect-shared-borders` stops blocking it.

### 3.3 Post-build verification — new module

There is **no verification of mbtiles content anywhere in the repo**. Every existing gate
is transport-level: tippecanoe exit code (`:460-465`), rsync success (`:501-640`),
`curl /data/<bucket>.json` → 200 (`update_tileserver_config.py:167-184`), registry
preflight (`push_gazetteer_inventory.py:554-582`). That is exactly why "OHM draws nothing
at z0" shipped silently.

New `processing/verify_tileset.py` (stdlib `sqlite3`, no new deps):

- `missing_low_zoom_tiles(mbtiles, expected_tiles, maxzoom=4)` — expected set derived from
  the **input GeoJSONL feature bounding boxes**, not a land mask, so ocean/polar tiles are
  excluded exactly and no new data file is needed. Assert present **and**
  `length(tile_data) > 0` (the reported failure is HTTP 204 / 0 bytes).
- `assert_labels_layer(mbtiles, bucket)` — `<bucket>_labels` present in metadata
  `vector_layers`, and its feature count at maxzoom within tolerance of the source polygon
  count. Catches the `tile_join -l` regression directly.
- `assert_coverage_layer(mbtiles)` — for single-band polygon buckets, the `coverage`
  attribute still appears at z0–7. **Guards the place#140 fix against regression by this
  work.** (Verified 30 July 2026 that no such regression exists today: `clio`, `po`, `nl`
  and `kain_par` contain **zero** tiles above 500 KB on a full scan, with z9+ maxima of
  5–130 KB — so `tile-join`'s ceiling is not quietly capping preserved boundaries, and
  `-pk` will be a no-op for every bucket except `osm`/`ohm`/`osm_misc`.)
- `assert_no_skipped_tiles(tile_join_stdout)` — see §3.2a2. Cheapest of the three and the
  one that would actually have caught this: the failure announced itself on stdout and was
  discarded.

Wire in after `tile_join` succeeds — `:1605-1606` (band path) and `:1662-1674` (place#140
path) — feeding `bucket_failures` so the run manifest records the failure and
`push_gazetteer_inventory`'s gate sees it. Failing there is right: a tileset with holes
should not deploy.

---

## 4. Order of work

1. ~~**Step 0 diagnostic** (§2)~~ — **DONE 29–30 July 2026.** Cause identified and reproduced;
   findings and three superseded hypotheses recorded in §2. Jobs 10687880 / 10688065 / 10688091.
2. `processing/verify_tileset.py` + tests, wired to both join sites, **including the
   skip-message check (§3.2a2)**. Run against the **current** `ohm.mbtiles` to prove it
   reproduces the reported holes before any fix lands.
3. `tile_join`: `-pk` + stdout capture (§3.2a), multi-layer support + single-input guard
   (§3.1f). All unit-testable without a retile — this is where most of the #160 fix lives.
4. `Band.label_minzoom/label_maxzoom` + `Band.geometry_flags` + `Band.include_properties` in
   `tilegen_bands.py`, with `load_bands` back-compatible against the **live** `style.json`
   (absent keys → defaults).
5. `_label_point_feature` + both emission sinks + the labels tippecanoe passes (§3.1a–e).
6. Geometry levers on the low-zoom bands (§3.2b), scoped to the banded buckets only, with the
   sliver check at step 8 gating `--detect-shared-borders` removal.
7. Regenerate `style.json` via `scripts/build_whg_context_style.py` — new band metadata
   (label zooms, include lists) **and** repointed label `source-layer`. Commit & push from
   the sibling tileboss clone.
8. **Dry run:** `python -m processing.generate_tiles --bucket ohm --no-deploy` on a compute
   node. Inspect with the new verifier before anything is pushed.
9. Retile + deploy: `es -generate-tiles --run-id <ID>` with **`--only-bucket ohm,osm,osm_misc`**.
   The `--only-bucket` allow-list is mandatory — the event-log fallback otherwise re-queues
   every bucket (the 2026-05-05 incident that wiped a working tileset;
   see `submit_tiles_slurm.py:402-419`).
10. Trailing restart job runs `update_tileserver_config --execute`, which verifies serving
    and auto-rolls-back. Confirm `https://tiles.whgazetteer.org/data/ohm/3/4/2.pbf` is
    non-empty and that `osm.json` `vector_layers` lists `osm_labels`.
11. Visual check in Atlas at the reported cases: Nebraska z6.2 (one label), Italy z5 (one
    label), Europe z3 (no rectangular void).
12. Close #159 / #160; open the phase-2 issue for per-namespace polygon gazetteers (§3.1h)
    plus its whg3 companion.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Labels silently dropped at `tile_join` | `assert_labels_layer` in the verifier; step 3 before step 5 |
| `-pk` payloads too large in practice | Measured: z0 709 KB–901 KB (one world tile), z1–z2 ~600 KB, z7–z10 unchanged. Per-band budget cuts held in reserve (§3.2 "not doing") |
| Dropping `--detect-shared-borders` causes hairline slivers between neighbours at z0–z2 | Visual check at step 8 gates it; fall back to `--tiny-polygon-size 8` alone (−30 % vs −44 %) |
| Attribute pruning breaks a whg3 popup that reads `name_<lang>` from polygons | Optional change worth ~1 %; prune per band, low zoom only, and audit whg3 property reads first — or skip entirely |
| place#140 coverage regresses on single-band buckets | Flag changes scoped to banded buckets; `assert_coverage_layer` |
| `polylabel` cost on ~18M osm polygons | Simplify before `polylabel`; measure on `ohm` (smaller) first at step 8 |
| Style/band drift between repos | One regeneration (step 7) covering both; `load_bands` back-compatible |
| 24 h wall exceeded by the extra label passes | Point passes are cheap; if `osm` is tight, bump `_FIXED_RESOURCES` wall before step 9 |
