# Plan — per-geometry tile channels for the gazetteer tilesets (place#166)

**Status, 9 October 2026:** implemented on `indexing` branch `feat/tile-channels` and `whg3`
branch `feat/tile-channels`, tested locally against a real `tippecanoe` 2.78.0, **not retiled,
not deployed**. The retile below needs the Technical Director's go on the decisions in §7.

Supersedes §3 and §8 items 6–8 of `plan-atlas-data-architecture.md` as the implementation record;
the diagnosis there (§1–§2) stands and is not repeated. Tracked as **place#166**.

---

## 1. What changes, in one paragraph

`generate_tiles.py` no longer decides a bucket's tiling mode by `polygon > point`. Every feature
routes itself by geometry type into one of four channels — **points**, **shapes**, **extent**,
**labels** — each tiled by its own `tippecanoe` pass with its own zoom range and flags, and all
`tile-join`ed into the ONE source-layer named after the bucket, told apart by a property exactly
as `coverage: 1` already was. A bucket emits every channel its data supports. The build also
writes a ledger (`<bucket>.channels.json`) of what it streamed and tiled, and a new verifier
(`processing/verify_tileset_channels.py`) decodes real tiles and holds the tileset to that ledger,
including the assertion that every registry `region_source` bucket actually publishes shapes.

## 2. The channel model

| channel | contents | zooms | tippecanoe | marker | required |
|---|---|---|---|---|---|
| **points** | Point / MultiPoint | 0–10 | `--cluster-distance 10 --cluster-maxzoom 8`, coalesce, `start:min/end:max` | *(Point, unmarked)* | yes |
| **shapes** | Polygon / MultiPolygon / LineString / MultiLineString | **8–10** when an extent was tiled, else 0–10 | `preserve_all` (no-drop) | *(geometry type)* | yes |
| **extent** | one dissolved footprint, `unary_union(polygons ∪ buffer(lines, 0.01°))` | 0–7 | plain, simplified 0.008° | `coverage: 1` | no |
| **labels** | one anchor per shape | **same minzoom as shapes** – 10 | `preserve_all`, never clustered | `label: 1` | no |

Routing is `_geometry_kind` → `_channel_for`; it is the only place the decision is made. A
`GeometryCollection` is classified by its most areal member. Anything unrecognised is dropped,
counted nowhere, and that is deliberate: the old test (`type in (Polygon, MultiPolygon,
GeometryCollection)` else point) counted every LineString as a point, which is how the 7 August
log reported `pl → pl: 18,250 features (poly=0 point=18,250)` for a bucket whose tiles carried 230
lines.

Rules, and where each lives:

- **No vote.** `_stream_bucket` writes `<bucket>.{points,shapes,labels,extent}.geojsonl`;
  `_channel_plan` turns what was streamed into tippecanoe passes; `_build_channels` runs them and
  joins. `wd` gets all four; so does `hgis`.
- **The z8 pin applies to shapes (and their labels) only, and only when an extent exists.** If
  the extent pass fails the shapes are re-planned from z0, so a footprint failure degrades to
  "boundaries everywhere" rather than the old "boundaries without footprint" — which, because
  the base pass was already pinned at z8, meant nothing at all below z8.
- **Points are always clustered**, in every bucket, and shapes are always `preserve_all`. A
  correction to the issue while here: its z9/z10 sample made `hgis` look polygon-dominant
  (41 polygons / 12 points), but the full 7 August stream was **892 polygons / 13,213 points**,
  so `hgis` was in fact tiled *point*-dominant — clustering on, and its 892 polygons given no
  extent and coalesce-dropped rather than preserved. The mirror failure the issue describes is
  real, just not for that bucket; `clio` (12,704 / 2,986) is the one whose points were pinned
  and unclustered. Either way the switch was per-bucket and is now per-feature.
- **Extent includes lines.** `_accumulate_coverage` buffers each line at `_LINE_BUFFER_DEG`
  (0.01°, about the footprint's own simplify tolerance). `pl` gets a mottle for the first time.
- **Per-feature dissolve.** `_dissolve_parts` unions a MultiPolygon's own parts when any two
  bounding boxes intersect (double loop up to 32 parts, `STRtree` above), so a place made of
  adjacent fragments renders without internal borders. Disjoint archipelagos are untouched and
  never pay for the union.
- **Line anchors** are the midpoint of the longest segment (`_line_anchor`), on the line.
  Polygon anchors are unchanged (`polylabel`, place#159). Points get no anchor.
- **Labels share the shapes' minzoom.** The plan table said 0–10; at z0 one tile would hold every
  anchor in the bucket (51k for `wd`), which is exactly the oversize-tile shape place#160 warned
  about, for anchors of shapes that do not draw below z8. This is a deliberate divergence (§7.2).
- **Extent cost control.** Each polygon is simplified at 0.008° *before* the union, and holes
  under 0.008°² (~1 km²) are dropped from the result. Inference, not measurement: the `un` task
  on 7 August ran 30 min for 247 polygons whose tippecanoe passes took 86 s, and the union of
  247 high-precision country outlines is the only other expensive step in that job, so the union
  of `wd`'s 51,095 polygons without pre-simplification is the retile's one unknown. §7.1.

Context-overlay buckets (`gn_capitals`) keep their own zoom range and clustering choice for the
points pass and emit no derived channels. The banded admin buckets (`osm`, `ohm`, `osm_misc`) are
untouched: they go through `_stream_bucket_banded` as before, and the `tileboss` style is not
regenerated by this change.

### 2.1 The ledger and the verifier

`_write_channel_ledger` writes `<bucket>.channels.json` beside the tileset after a successful
join: `counts {point, polygon, line}`, `label_anchors`, `extent`, and `channels {name: {minzoom,
maxzoom, cluster_points, preserve_all}}`. The build writes it; nothing else ever does.

`python -m processing.verify_tileset_channels <mbtiles>…` reads the ledger, checks
`vector_layers` is exactly `[<bucket>]`, then decodes every tile at z0–2 and a seeded sample of 64
per zoom above, classifies each feature (label / coverage / cluster / point / polygon / line) and
asserts, with denominators printed:

| ledger says | the tiles must show |
|---|---|
| shapes > 0 | `label` field; polygon/line features and `label:1` features at z ≥ shapes minzoom |
| extent tiled | `coverage` field; a coverage feature below the pin; **no shape below the pin**; no coverage at/above it |
| shapes = 0 | no `label`, no `coverage` — field or feature |
| points > 0 | point or cluster features somewhere, and below z8 when clustering was asked for |
| `--region-source <b>` | shapes > 0 in the ledger (else "declared a region source but streamed no polygons or lines") |

`--region-sources-url https://whgazetteer.org/api/sources/` reads the flags from the registry
once the whg3 branch ships (it adds `region_source` to that payload); until then pass
`--region-source` explicitly. A tileset with no ledger beside it **fails** unless
`--allow-missing-ledger`, because the old pipeline's tilesets have nothing to be held to.

The test suite (`tests/test_tile_channels.py`) proves the verifier can fail: the same real
build is refused when its ledger is edited to claim no shapes, and a build made the old way
(one clustered pass, no labels, shapes from z0) is refused for missing labels and for shapes
leaking below z8.

## 3. What whg3 must change, and how old and new tiles coexist

Nothing in the tile *contract* changes: one source-layer per bucket, the same name, the same
properties (`coverage`, `label`, `point_count`, `start`/`end`, names). `loadGazetteerStyle`'s
`_heat` / `_fill` / `_line` / `_circle` / `_coverage_*` layers already filter on exactly those
properties and need no edit. What a rebuilt tileset shows that today's does not:

| new in a rebuilt bucket | client effect | handled by |
|---|---|---|
| hybrid buckets (`wd`, `hgis`, `pl`, `clio`'s points) carry `coverage` | mottle at z0–7 **and** the heatmap; the "Coverage view — zoom in" pill shows for them | nothing — `_hasCoverage` reads the TileJSON field, as designed |
| points of polygon-majority buckets now exist below z8, clustered | they heat and cluster like any point gazetteer | nothing |
| lines carry a `label:1` anchor | one label per route | the `_label` filter below |
| multipart places arrive as one polygon | no internal borders | nothing |

**The one real client change — `heroMap._addGazetteerLabels`.** The gazetteer `_label` symbol
layer filtered `['!', ['has','point_count']]`: every unclustered feature, polygons included. A
polygon is cut at every tile edge and a symbol layer draws one label per fragment, and since
7 August the per-namespace tilesets have *also* carried one anchor per polygon — so `po`,
`clio`, `nl`, `un`, `kain_par`, `vob_*`, `ukhc` are drawing fragment labels **plus** the anchor
today. The plan's §3.2 `_label` layer (`filter: has label`) never shipped in whg3. The branch
fixes that, and does so tolerantly:

```js
hasLabelChannel = 'label' in vl.fields          // from the TileJSON, per source-layer
filter = hasLabelChannel
  ? ['all', ['!',['has','point_count']],
            ['any', ['has','label'],                                   // one anchor per shape
                    ['all', ['==',['geometry-type'],'Point'], ['!',['has','coverage']]]]]  // a point is its own anchor
  : ['!', ['has','point_count']]                 // legacy tileset: every unclustered feature
```

So during a rolling retile every bucket renders correctly whichever scheme it was built under:
the client reads the scheme off each tileset's own `vector_layers[].fields`, the same mechanism
`_hasCoverage` already uses. **Ship the whg3 branch before the first push** — it is an
improvement on today's tiles on its own, and no tileset needs it to avoid breaking.

Also in the whg3 branch: `/api/sources/` exposes `region_source` (already visible in the Atlas
page's `available_sources`), so the verifier can read the list instead of carrying a copy.

Not in this branch, deliberately: `_label` in `gazetteerInteraction.SHAPE_SUFFIXES` (label-click
selection, plan §5.2 / place#156) — the comment there says labels were excluded on purpose, and
that is a separate decision.

## 4. Re-tile runbook

Read `reference_crc_slurm_jobs` first (crc0 is a login node; `sbatch -M htc`; `df` on `/vast`
lies). Everything below is **proposed** and nothing has been submitted.

### 4.1 Preconditions

1. whg3 `feat/tile-channels` merged to `staging`, verified on dev, promoted to prod (§3).
2. `indexing` `feat/tile-channels` merged to `main` and pulled on `/vast/ishi/elastic` (the
   checkout Slurm tasks run from).
3. **Back up the live tilesets.** `/ix1/ishi/data/tiles/*.mbtiles` is NOT a copy of what is
   serving: measured 9 Oct, `clio.mbtiles` there is 6.3 MB dated 7 Aug 12:36 (the destroyed-store
   run that streamed `poly=0`), `po` is 2 MB, `wd`/`gn` are the 22 July pre-label builds. The
   verified 7 August builds lived in `/vast/ishi/tiles-verify`, released 3 September. **The only
   copy of each live per-namespace tileset is on the tileserver.** Before any push, from a CRC
   compute node (rsync is on compute nodes and the tileserver, not on pitt):

   ```bash
   mkdir -p /ix1/ishi/data/tiles-live-backup-$(date +%Y%m%d)
   rsync -a --info=stats1 -e "ssh -i $TILESERVER_SSH_KEY -o BatchMode=yes" \
     whgadmin@134.209.177.234:/srv/tileserver/tiles/{hgis,pl,ukhc,un,vob_rd,vob_rc,vob_cty,vob_lgd,kain_par,nl,po,clio,wd}.mbtiles \
     /ix1/ishi/data/tiles-live-backup-$(date +%Y%m%d)/
   ```
   That is ~7.8 GB against 2.1 TB free on `/ix1` (`shutil.disk_usage("/ix1/ishi")`, 9 Oct).
4. Disk on the tileserver, measured 9 Oct 2026 with `df -h /srv/tileserver/tiles` **on the
   tileserver** (not `df /vast` on pitt): **48 G, 41 G used, 7.4 G free, 85 %** (the plan doc's
   8.9 GB is from 7 August; it has lost 1.5 GB since). Per-namespace tilesets on it: `clio`
   2,507 MB, `wd` 1,660 MB, `gn` 1,537 MB, `po` 1,186 MB, `tgn` 441 MB, `gb` 132 MB, `nl` 113 MB,
   `un` 85 MB, the rest under 25 MB each. Pushes use `rsync --inplace` (same inode, no doubling),
   so the headroom constraint is the *growth* of the largest bucket, not its size — but a torn
   file during a push serves errors until the push completes, which is why pushes go one bucket
   at a time with a restart after each big one.

### 4.2 Build (no deploy), in waves

`submit_tiles_slurm` gains `--no-deploy`: the array builds to `--output-dir` and pushes nothing;
the restart job is not submitted. Use a fresh directory so the old `/ix1/ishi/data/tiles` is not
overwritten either:

```bash
cd /vast/ishi/elastic
OUT=/ix1/ishi/data/tiles-channels-$(date +%Y%m%d)
python -m processing.submit_tiles_slurm --run-id <RUN_ID> --no-deploy --output-dir $OUT \
    --only-bucket hgis --only-bucket pl --only-bucket ukhc        # wave 1
```

| wave | buckets | why this order | 7 Aug elapsed (task wall / tippecanoe) | expected now |
|---|---|---|---|---|
| 1 | `hgis`, `pl`, `ukhc` | the three measured failures: hybrid, line-bearing, tiny region source | 0:00:09 / 2 s; 0:00:16 / 7 s; 0:00:21 / 7 s | minutes; `pl` gains an extent for the first time |
| 2 | `un`, `vob_rd`, `vob_rc`, `vob_cty`, `vob_lgd`, `kain_par` | small polygon buckets; `un` is the pre-simplify timing probe | `un` **0:30:29** / 81 s; others < 1 min | `un` should drop well under 30 min if §2's inference is right — record it |
| 3 | `nl`, `po`, `clio` | the three `region_source` gazetteers (plus `osm*`, untouched) | 0:00:35 / 9 s; 0:04:43 / 105 s; 0:06:30 / 86 s | similar; `clio`'s 2,986 points now cluster |
| 4 | `wd` | the big hybrid; 51,095 polygons + 11.4 M points | 0:20:48 / 434 s (single pass) | **unknown** — union of 51k pre-simplified polygons; large tier (64 GB / 24 h) covers it |
| 5 | `whg-*` (48 datasets) | contributed data, some polygonal (`whg-1118` 100 MB) | seconds each | seconds each |
| — | `gn`, `tgn`, `gb`, `chgis`, `alc`, `iv`, `tm`, `ofs`, `og`, `dgsd`, `dp` | **points-only: the output is the same single clustered pass as today**; rebuild only if a ledger is wanted for the verifier, and do not push (3.5 GB of pushes for no content change) | `gn` 0:10:33 / 238 s, `tgn` 0:06:24 / 82 s | — |

Source for the 7 August figures: `/vast/ishi/elastic/logs/tiles-ns-10756209_*.out`, `tl-*.out`
and `sacct -M htc` for jobs 10756209 and 10756258–10756624. Total array wall for all 24
per-namespace buckets in parallel was 16 minutes; summed per-task elapsed ≈ 1 h 25 m.

**Headline cost:** about 2 h of compute across the five waves if `wd`'s union behaves, dominated
by `wd`; the human cost is the per-bucket verification and serial pushes, roughly a half day.

### 4.3 Verify, per bucket, before any push

```bash
grep -h "features (poly=\|label anchors\|extent footprint\|gate " $OUT/../logs/whg-tiles-<RUN_ID>-*.out
python -m processing.verify_tileset_channels $OUT/<b>.mbtiles [--region-source <b>]
python -m processing.verify_tileset_coverage --all $OUT/<b>.mbtiles     # expect "regional, skipped" for most
python developer/tile-qa/tsize.py $OUT/<b>.mbtiles /ix1/ishi/data/tiles-live-backup-<date>/<b>.mbtiles
```

Gates (all must hold):

1. The build log prints `poly=N line=N point=N` (new format — `developer/tile-qa/finalchk.py`'s
   regex matches the old `poly= point=` line only and will report `no log`; it is a verbatim
   forensic artefact and is left alone), `+ N label anchors` where shapes > 0, `+ dissolved extent
   footprint` where shapes > 0, and `gate <b>: … → PASS`.
2. `verify_tileset_channels` exits 0, and its per-zoom table shows a coverage feature below z8,
   shapes and labels at z8+, points below z8 where the ledger has points.
3. `tsize.py`: no tile over 500,000 bytes that the old tileset did not also have; **arguments are
   positional and its labels are hardcoded NEW/OLD — pass them in that order.**
4. For `region_source` buckets (`po`, `clio`, `nl` by the seed list; check the Django admin for
   any added since): `--region-source <b>` passes.

### 4.4 Push, one bucket at a time, restart, confirm

From a compute node (direct rsync, delta transfer):

```bash
python -m processing.generate_tiles --bucket <b> --output-dir $OUT --redeploy-only
python -m processing.update_tileserver_config --bucket <b> --execute   # verifies /data/<b>.json after restart
curl -s -H "Origin: https://whgazetteer.org" https://tiles.whgazetteer.org/data/<b>.json | jq '.vector_layers[0].fields | keys'
```

Push order within a wave: smallest first. After each of `po`, `clio`, `wd` check `df -h
/srv/tileserver/tiles` on the tileserver before the next; `tiler.service` holds deleted inodes
only across a rename, and `--inplace` does not rename, but the 7 August push reached 99 % and that
is not a margin to reason about from memory. The config rewrite is a no-op for existing buckets
(entry-level merge) and its restart is what releases anything held.

Then SG at the map, Places → Gazetteers → Explore: `hgis` heat at z5; `pl` mottle at z4 and a
label on a route at z9; `wd` mottle where it has polygons; `po` with one label per polygon, not
one per fragment; a multipart `clio` polity without internal borders.

### 4.5 Rollback

Per bucket, from the same compute node, the backup taken in §4.1:

```bash
rsync -a --inplace --info=stats1 -e "ssh -i $TILESERVER_SSH_KEY -o BatchMode=yes" \
  /ix1/ishi/data/tiles-live-backup-<date>/<b>.mbtiles whgadmin@134.209.177.234:/srv/tileserver/tiles/
python -m processing.update_tileserver_config --bucket <b> --execute     # restart + verify serving
```

The client needs no rollback: it reads the scheme off each tileset. The ledger beside the new
build stays on `/ix1` for the post-mortem. If `update_tileserver_config`'s own health check fails
after a restart it rolls the config back itself (`rollback_config`); the tiles are a separate
file and are restored by the rsync above.

## 5. Tests

`python -m unittest tests.test_tile_channels` — package-qualified, never `discover -s tests`
(`project_staging_test_debris`). 38 tests in three layers; 9 are skipped without `tippecanoe` +
`tile-join` on PATH and `mapbox_vector_tile` installed (`uv pip install mapbox-vector-tile`,
added to the local venv 9 Oct; it is pure Python and should go into the `whg` conda env on CRC
before the verifier runs there).

- routing, dissolve, extent, anchors — shapely only;
- `_channel_plan` / `_build_channels` with tippecanoe mocked: flags and zooms per channel, the
  extent-failure unpin, label failure survivable, points failure fatal, ledger contents;
- end-to-end `generate_tiles_from_staged` on a hybrid fixture (2 polygons incl. one two-part
  place, 1 line, 43 points incl. a dense knot) against the real tippecanoe 2.78.0: one layer,
  one ledger, coverage and clusters below z8 and no shapes, shapes + labels + points at z9, the
  two-part place arriving dissolved, the line's anchor on its longest segment; then the verifier
  passes the build and **refuses** an edited ledger, a legacy single-pass build, and a region
  source without shapes.

Plus `tests/test_generate_tiles_per_namespace.py` and `tests/test_label_anchors.py` updated to
the new contract (`_stream_bucket(bucket, reader, out_dir=…)` → `BucketStream`; lines now anchor).
`tests/test_restart_tileserver.py` fails on `main` before this branch and is unrelated.

## 6. Files

| repo / path | change |
|---|---|
| `indexing/processing/generate_tiles.py` | channel constants; `_geometry_kind`, `_channel_for`, `_dissolve_parts`, `_linear_parts`, `_line_anchor`, `_drop_small_holes`; `_accumulate_coverage` takes lines and pre-simplifies; `BucketStream`, `channel_paths`, `_stream_bucket` rewritten; `_channel_plan`, `_build_channels`, `_write_channel_ledger` replace `_with_labels` and the single-band branch of `generate_tiles_from_staged`; log line is `poly= line= point=` |
| `indexing/processing/verify_tileset_channels.py` | new |
| `indexing/processing/submit_tiles_slurm.py` | `--no-deploy` passthrough |
| `indexing/tests/test_tile_channels.py` | new |
| `indexing/tests/test_generate_tiles_per_namespace.py`, `test_label_anchors.py` | updated to the contract |
| `whg3/whg/webpack/js/heroMap.js` | `_addGazetteerLabels` scheme-aware filter |
| `whg3/api/views.py` | `/api/sources/` exposes `region_source` |

Not changed: `tilegen_bands.py`, `update_tileserver_config.py`, `push_gazetteer_inventory.py`,
the banded path, the `tileboss` style. `push_gazetteer_inventory.assert_tilesets_served` remains
the ordering gate for the registry push; the shapes assertion lives in the verifier because the
registry push runs from pitt, which has neither the tiles nor the ledgers.

## 7. Decisions for SG (none of these is taken)

1. **Pre-simplify before the union and drop sub-km² holes** (§2). Changes the fine detail of the
   existing `po`/`clio`/`nl`/`un`/`kain_par`/`vob_*`/`ukhc` footprints below what z7 can show;
   makes `wd`'s footprint buildable in bounded time. The alternative is to union `wd` raw and
   measure first. Reversible by two constants.
2. **Labels pinned with their shapes (z8+)** rather than the plan table's 0–10. The whg3
   `_label` layer has `minzoom: 8` so nothing is lost on screen; the saving is one anchor-per-shape
   tile at every low zoom.
3. **Which buckets to push.** Points-only buckets produce byte-equivalent content; pushing them
   costs 3.5 GB of transfer on a thin disk for nothing. Recommendation: build them for ledgers,
   push the 13 shape-bearing buckets plus `whg-*`.
4. **`region_source` on `/api/sources/`** — a public boolean the Atlas page already serialises.
5. **Timing.** `plan-atlas-data-architecture.md` §8 gated the last retile on place#164's
   re-ingestion. Whether any re-ingestion of `wd`/`pl`/`tgn` is pending now is not known from
   this session; if one is, build after it or pay for `wd` twice.
6. **Label-click selection** (`_label` in `SHAPE_SUFFIXES`) stays out until place#156 decides it.
