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
3. `tsize.py`: no tile over 500,000 bytes that the old tileset did not also have, **except named,
   measured exceptions recorded here** (SG, 10 Oct 2026: `wd` `8/133/85` and `8/134/84`, §8.3 —
   the shapes pass is no-drop and the old pass coalesce-dropped ~70 % of wd's z8 fragments). An
   exception is a tile that is served whole; **never drop a feature to make a tile fit**. A new
   oversize tile is a stop, not a note, until it is measured and named. Follow-up (not now):
   simplification pressure in dense tiles. **Arguments are positional and its labels are
   hardcoded NEW/OLD — pass them in that order.**
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

**Large buckets: push under a temporary name, then rename (SG, 10 Oct 2026).** Measured on the
10 Oct swap: `rsync --inplace` rewrites the live file, so the bucket's tiles are torn for the WHOLE
transfer, not just the restart: 8–27 s for the small buckets, but `po` 169 s, `clio` 309 s and `wd`
**2,387 s (≈ 40 min, 2.17 GB)**. For any bucket over ~200 MB, copy to a sibling name in the same
directory and swap it in with a rename, which is atomic on one filesystem:

```bash
rsync -a --info=stats1 -e "ssh -i $TILESERVER_SSH_KEY -o BatchMode=yes" \
    $OUT/<b>.mbtiles whgadmin@<tileserver>:/srv/tileserver/tiles/<b>.mbtiles.new
ssh ... 'cd /srv/tileserver/tiles && mv <b>.mbtiles <b>.mbtiles.prev && mv <b>.mbtiles.new <b>.mbtiles'
python -m processing.update_tileserver_config --bucket <b> --execute   # restart releases the old inode
ssh ... 'rm /srv/tileserver/tiles/<b>.mbtiles.prev'                   # only after the check passes
```

Disk: the new file and the old one coexist until the restart (and `tiler.service` holds the old
inode until then), so free space must exceed the new file's size: check `df -h` first; `wd` needs
~2.2 GB against 6.9 GB free after the 10 Oct swap. `<b>.mbtiles.prev` is also the fastest rollback
(rename it back and restart). Not yet wired into `generate_tiles --redeploy-only` or `tilech-swap.sbatch`;
do that before the points-only pass (`gn`, `tgn`), whose files are the largest.

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

**SG, 9 Oct 2026:** recommendations accepted on all five (place#166 comment). whg3 `d3fe7f386`
promoted as whg3 main `550300a4a`; prod `/api/sources/` serves `region_source` (28 entries; true
for clio, nl, po, osm, ohm, osm_misc — measured 10 Oct).

## 8. Execution record — 10 Oct 2026 (build and verification only; nothing pushed)

Run-id **`tilech-20261010T065558Z`** (manifest copied from `h3ccode-20260805T120000Z` with the
`tiles` stage reset to pending, so the August run's manifest is untouched). Code: this branch at
`123df38`, cloned to **`/vast/ishi/elastic-tile-channels`** — a second checkout, so the gateway's
`/vast/ishi/elastic` (`main`) was neither pulled nor restarted; `.env`/`.env.local` copied in.
Output: **`/ix1/ishi/data/tiles-channels-20261010/`** (58 tilesets + 58 ledgers; the kept
`*.geojsonl` intermediates are what the drop-rate experiment reads). **The tileserver was not
written to, reloaded or reconfigured.**

### 8.1 Backup of the live tilesets — verified, not inferred

`/ix1/ishi/data/tiles-live-backup-20261010/` (Slurm 11868727, 7 min). Seeded from
`/ix1/ishi/data/tiles-20260902-retile/`, then `rsync -a --checksum` *from the tileserver*:
**0 of 13 per-namespace files transferred** (5,874,216,960 bytes, every checksum identical), so the
2 Sep build on `/ix1` IS the live set byte for byte — §4.1's "the only copy is on the tileserver"
was true of `/ix1/ishi/data/tiles/`, not of the 2 Sep directory. Of the 47 live `whg-*` files one
(`whg-ne-basic`) was absent from the 2 Sep directory and was fetched (78.7 MB). `SHA256SUMS` (60
lines) sits beside them. Tileserver disk at the time: 48 G, 41 G used, **7.4 G free, 85 %**.

### 8.2 Build

| array | buckets | elapsed | gates |
|---|---|---|---|
| 11868755 | `hgis` `pl` `ukhc` | 1:14 / 1:17 / 0:51 | 3/3 PASS |
| 11868857 (57 tasks) | `un` `vob_*` `kain_par` `nl` `po` `clio` + 48 `whg-*` | `clio` 25:03, `po` 19:45, `un` 18:42, rest < 5 min | 55/55 PASS; `whg-1642`, `whg-1644` produced nothing (no geometry, as the code expects) |
| 11868858 | `wd` (64 GB tier) | **1:32:27** (stream to 03:38, union ~1 min, passes to 03:54, `tile-join` 03:54–04:30) | PASS — 51,094 polygons / 6,346 lines / 11,401,945 points, 57,440 anchors, 1,734.4 MB (live 1,740.6) |

Stream counts that corrected the issue's samples: `hgis` 892 polygons / 13,213 points (23 of the
892 are GeometryCollections carrying lines or points, which tippecanoe splits — hence 29 line
features at z8 against a ledger of 0 lines; harmless); `pl` **4,843 lines** / 13,407 points (the
issue's 230 was a z9/z10 sample); `po` 7,815 polygons; `clio` 15,690; `un` 247. §7.1's timing
probe: `un` 30:29 → **18:42** — pre-simplification helped, less than hoped; `wd`'s union took about
a minute by file mtimes (`wd.shapes.geojsonl` 03:38, `wd.extent.geojsonl` 03:39), the cost is the
`tile-join` of a 1.5 GB points mbtiles. New sizes: `po` 1,242.1 MB (live 1,242.8), `clio` 2,627.5
(2,628.8), `hgis` 18.7 (20.7), `un` 17.9 (88.3), `nl` 116.7 (118.3) — the swap needs no headroom.

### 8.3 Verification

* `verify_tileset_channels` exit 0 for `hgis`, `pl`, `ukhc`: coverage at z0–7, no shape below z8,
  shapes + `label:1` at z8–10, points below z8 where streamed. `verify_tileset_coverage --all`
  "fails" `hgis` with 20 missing land tiles (Jakarta, Cairo…) because its bounds span −127°..121°
  and the checker treats it as global; the z0–z4 tile sets are identical to the live build (1/3/4/
  6/14 tiles), so the holes are inherited and not holes. `tsize.py`: no tile over 500,000 bytes in
  any new tileset checked (max 43,649, `pl` z10).
* `developer/tile-qa/chancmp.py` (new), full decode to z8 and a 300-tile sample above: at **z10
  the sampled counts are identical** (`hgis` 494 = 494 points, 2,022 ≈ 2,021 polygons; `pl` 370 =
  370, 405 = 405 lines; `ukhc` 533 = 533) — nothing is lost at max zoom. `ukhc` is identical at
  every zoom bar the low-zoom anchors (51 = 51 labels at z8).
* Headless Atlas (Playwright, bundled Chromium, scratch harness `atlas_newtiles.py`; LIVE run as
  control, `--prove-it-fails` run first): the prod client at `/atlas/?gazetteer=<ns>` with the NEW
  tileset route-intercepted renders, at z9, **exactly what it renders with LIVE** (`hgis` fill 86 /
  line 86 / circle 219 / label 85; `pl` line 126 / circle 176; `po` fill 6,729; `clio` fill 7,888 /
  line 8,218), picks up the new `coverage` field (mottle + the "zoom in" pill at z5 for `hgis` and
  `pl`, which LIVE lacks), and labels `pl` by anchor (63 placed vs 75 fragment labels).

* Waves 2/3/5 (Slurm 11871795, 49 min; 11871796, 1:38): **every tileset's channels match its
  ledger** (`un` + `vob_*` + `kain_par` + `nl` + 45 `whg-*`; `po` with `--region-source`, `clio`),
  `un` and one `whg-*` found by the scan fallback (after 25 of 29,105 and 2,239 of 4,894 z8 tiles).
  `po` and `clio` are identical to LIVE in the sampled z8/z9 tiles (po polygons 9,771 = 9,771,
  labels 10 = 10; clio 12,211 = 12,211, lines 114 = 114, labels 20 = 20); `po` is the one bucket
  whose tile count differs by one (825,111 vs 825,112). `verify_tileset_coverage` flags `nl`,
  `whg-12`, `whg-1165`, `whg-1360`, `whg-1381` as hgis above: z0–z4 tile counts identical to LIVE
  in every case — inherited by the checker's global heuristic, not holes.
* `wd` (11871891, 1:33): channels match the ledger (clusters and points at every zoom below z8,
  coverage at z0–7, shapes + labels at z8+), all land tiles present, tile count 787,671 vs
  787,663, z10 sample identical (573 = 573 points, 249 vs 246 polygons). **Two z8 tiles exceed
  500,000 bytes** — gate 3 of §4.3 as written: `8/133/85` (Ruhr, 922,964 bytes stored, 525,497
  gzipped; LIVE 164,992; 1,754 polygons + 1,161 multipolygons + 448 anchors + 550 clusters) and
  `8/134/84` (Weser hills, 541,873 / 306,664; LIVE 134,658). The cause is the design: `wd`'s
  shapes are now no-drop where the old point-dominant pass coalesce-dropped about 70 % of its z8
  polygon fragments (chancmp: 872 vs 263 in 100 sampled tiles), and the German nature-reserve
  polygons are the densest place on earth for `wd`. No tile was dropped (`-pk` on the join). SG
  decides: accept two large tiles, or raise bytes-per-feature pressure on the shapes pass (the
  rule stands — no feature may be dropped to fit).

### 8.4 Two findings that need a rebuild (fixed on the branch in `c1cb2ee`, NOT rebuilt)

1. **The "no-drop" labels pass was never no-drop.** tippecanoe's point drop rate (`-r`, 2.5 per
   zoom below the base zoom) is untouched by `--no-feature-limit` / `--no-tile-size-limit`, and
   anchors are Points. Full decode of `hgis`: 892 anchors → **399 distinct at z8, 701 at z9, 892 at
   z10 — identical in the live build**. `po` at z8: 1,484 of 7,815. `preserve_all` now adds
   `--drop-rate 1` (tested: 200 anchors, all 200 at z8).
2. **Low-zoom heat is thinner on the hybrids, by construction.** The same rate removes points from
   the clustered pass *uncounted*. The old single pass got its `point_count` mass by accident —
   the polygons' bulk forced `coalesced_as_needed=871` at z0 — so with points in their own pass
   `hgis` z0 holds 2 of 13,213 points and the Atlas heat weight in a z5 view fell 343 → 80 (`pl`
   1,238 → 100). Measured remedy (`developer/sbatch-templates/tilech-exp-droprate.sbatch`,
   11869180): `--drop-rate 1` on the points pass keeps every point as a cluster from z0 (24
   clusters standing for all 13,213; z5 1,805 + 633), keeps all points at z9 (not ~40 %), max tile
   42,628 bytes, wall unchanged at 13k points; **`wd` at 11.4 M points and `gn`/`tgn` unmeasured**,
   and it changes every point bucket, so §7.3's "points-only buckets need no push" would no longer
   hold. Opt-in: `WHG_POINTS_DROP_RATE=1`. **Decision for SG before the rebuild**, so `wd` is built
   once more, not twice.
3. (Verifier) a 64-tile sample cannot see one anchor per country-sized polygon (`po`: labels in
   304 of 39,300 z8 tiles; `un` likewise) — it now scans the zoom before failing.

### 8.5 Swap and rollback (NOT run; needs SG's go)

From a CRC compute node (`srun -M htc --partition=htc --qos=htc-htc-s --mem=4G --time=2:00:00
--pty bash`, then `source …/conda.sh && conda activate whg && cd /vast/ishi/elastic-tile-channels`;
`TILESERVER_SSH_KEY` is set by `.env`), **one bucket at a time, smallest first**:

```bash
OUT=/ix1/ishi/data/tiles-channels-<rebuild date>
python -m processing.generate_tiles --bucket <b> --output-dir $OUT --redeploy-only   # rsync --inplace, direct
python -m processing.update_tileserver_config --bucket <b> --execute                # config merge (no-op for an existing bucket) + restart both services + /data/<b>.json check, self-rollback on failure
curl -s -H "Origin: https://whgazetteer.org" https://tiles.whgazetteer.org/data/<b>.json | jq '.vector_layers[0].fields | keys'
```

After `po`, `clio`, `wd`: `ssh tileserver 'df -h /srv/tileserver/tiles'`. Rollback per bucket:

```bash
rsync -a --inplace --info=stats1 -e "ssh -i $TILESERVER_SSH_KEY -o BatchMode=yes" \
  /ix1/ishi/data/tiles-live-backup-20261010/<b>.mbtiles whgadmin@134.209.177.234:/srv/tileserver/tiles/
python -m processing.update_tileserver_config --bucket <b> --execute
```

Downtime (inference — the 2 Sep logs record only `total size` per push, not a rate): with
`--inplace` a bucket serves torn tiles for the length of its own transfer (seconds for all but
`po`/`clio`/`wd`, which at a few tens of MB/s is 1–3 minutes each), and each `--execute` restarts
both services (the 2 Sep restart job ran 28 s end to end). Measure the first small push and
scale from it rather than from this paragraph.

### 8.6 SG rulings, 10 Oct 2026 (relayed), and the rebuild

1. Low-zoom heat: `--drop-rate 1` on the clustered points pass **ON for this rebuild**
   (`submit_tiles_slurm --points-drop-rate 1`, exported into the job script). `wd` to be MEASURED
   with it (z0–z7 tile sizes, cluster counts, build time): any tile > 500 KB at z0–z7, or a build
   time more than double 1:32:27, stops `wd`'s swap (the others may proceed). **Points-only
   buckets (`gn`, `tgn`, …) are not rebuilt or pushed in this swap** — separate pass after `wd`'s
   numbers.
2. `wd`'s two oversize z8 tiles **accepted** (gate 3 amended above).
3. Then rebuild the 13 + `whg-*` under a new run-id with `c1cb2ee`, re-verify everything, swap
   smallest first with the first small push timed, `/data/<b>.json` after each, `df -h` after
   `po`/`clio`/`wd`, immediate per-bucket rollback on a failed check, then
   `scripts/atlas_smoke.py https://whgazetteer.org` (76/76).

### 8.7 Open

* The rebuild, its measurements and the swap log: §9 when done.

## 9. Rebuild record — 10 Oct 2026, run `tilech-20261010T102801Z` (SG's rulings applied; swap NOT run)

Code `c0550fe` (this branch); `--no-deploy --points-drop-rate 1` (exported in every job script);
output **`/ix1/ishi/data/tiles-channels-20261010b/`** (58 tilesets + ledgers). `gn`, `tgn` and
the other points-only buckets were not built (separate pass after `wd`'s numbers, per SG).

### 9.1 Build

| array | buckets | elapsed | gates |
|---|---|---|---|
| 11882787 (60 tasks) | 12 shape buckets + 48 `whg-*` | `clio` 30:49, `po` 24:32, `un` 22:21, rest < 5 min | 58/58 PASS (`whg-1642`/`-1644` empty) |
| 11882788 | `wd` with `--drop-rate 1` on the points pass | **1:23:48** (first build 1:32:27 — inside SG's 2× limit) | PASS; **2,169.7 MB** (first build 1,734.4; live 1,740.6: +429 MB against 7.4 G free) |

### 9.2 Verification (all of §8.3's checks, repeated on the rebuild)

* Ledger match: **every tileset** (Slurm 11886202, 56 tilesets; 11886201 `po` + `clio` with
  `--region-source`; `wd`: §9.3). No channel-verifier failure anywhere. No tile over 500,000 bytes
  in any of the 57 non-`wd` tilesets. `verify_tileset_coverage` "missing land tiles" on the same
  regional set as before plus `whg-892` (63 points, no polygons — the checker's land test cannot
  apply) and `whg-1760`: z0–z4 tile counts identical to LIVE in every case, so inherited.
* The two fixes are visible in the tiles: anchors no longer thinned (`hgis` z8 sampled labels 87
  vs 43; `ukhc` 98 vs 51; `po` 57 vs 10; `clio` 68 vs 20) and `hgis` z0 holds 24 clusters standing
  for all 13,213 points (was 2 bare points; live 890 of 13,213). `po`/`clio` polygons identical
  to LIVE in every sampled z8/z9 tile; `pl` lines identical; `ukhc` identical.
* Headless Atlas (prove-it-fails first; LIVE control; NEW route-intercepted): at z9 the shapes
  render exactly as LIVE (`hgis` fill 86, `pl` lines 126, `ukhc` 14, `po` 6,729, `clio` 7,888 /
  8,218, `wd` 2,263 vs 2,235) with the circles no longer rate-dropped (`hgis` 545 vs 219, `pl`
  437 vs 176, `wd` 5,717 vs 3,489); at z5 the coverage mottle + pill appear for every hybrid and
  the heat mass is restored (`hgis` 2,433 vs 343 LIVE, `pl` 4,323 vs 1,238, `wd` 192,912 vs
  18,523 over the Ruhr).

### 9.3 `wd` with `--drop-rate 1` — SG's measurement (filled in from Slurm job below)

Slurm 11892282 (35 min): channels match the ledger, all land tiles present, tile count 787,671
(live 787,663), z10 sample identical (573 = 573 points).

| | first build (default rate) | **rebuild, `--drop-rate 1`** | live (2 Sep) |
|---|---|---|---|
| build time | 1:32:27 | **1:23:48** | — |
| mbtiles | 1,734.4 MB | **2,169.7 MB** | 1,740.6 MB |
| z0 tile | 22,480 B; 97 clusters standing for 1,196 points | **101,015 B; 604 clusters standing for 11,401,515 of 11,401,945** | 80,149 B; 161 clusters, 58,987 |
| max tile z0–z7 | 90,208 B | **115,248 B (z2)** — z4–z7 maxima 108–111 KB, all *smaller* than live (118–163 KB) | 162,687 B |
| z8 sample (100 tiles): points + clusters | 1,212 + 1,332 | **1,047 + 3,395** | 1,203 + 1,381 |
| z9 sample: points | 2,360 | **4,446** (nothing rate-dropped) | 2,354 |
| tiles > 500,000 B | 2 (z8) | **8** | 0 |

**SG's two conditions are met** (no z0–z7 tile over 500 KB; build time down, not up). **But the
un-dropped points at z8/z9 push six MORE tiles past 500 KB** than the two SG accepted — all in
the Rhine/Ruhr: z8 `133/85` **1,050,596** (accepted; was 922,964), `134/84` 629,789 (accepted),
and new `132/85` 559,776, `134/88` 537,664, `134/85` 529,534, `133/84` 516,067; z9 `266/171`
529,470, `266/170` 507,074. All served whole (no tile dropped). Under gate 3 as amended that is a
stop for `wd` until SG names them too — or asks for the dense-tile simplification follow-up
first. The other 12 buckets and `whg-*` are unaffected.

### 9.4 The swap was blocked

With every check above green, the swap job (`developer/sbatch-templates/tilech-swap.sbatch`:
one Slurm job, buckets smallest first, per bucket push → `update_tileserver_config --execute` →
independent TileJSON field check → automatic rollback from the verified backup and STOP on any
failure; a 0.25 s poller on the tileserver measuring each bucket's downtime; `whg-*` chained
`afterok` with one restart) was submitted and **the permission classifier refused the submission
as a production deploy**. Nothing reached the tileserver. The job script is written, on
`/vast/ishi/staged/runs/tilech-adhoc/swap.sbatch`, and needs a human to submit it:

```bash
# on crc0, as stg135
cd /vast/ishi/staged/runs/tilech-adhoc
NEW=/ix1/ishi/data/tiles-channels-20261010b
ORDER=$(for b in hgis pl ukhc un vob_rd vob_rc vob_cty vob_lgd kain_par nl po clio; do echo "$(stat -c %s $NEW/$b.mbtiles) $b"; done | sort -n | awk '{print $2}' | tr "\n" " ")
WHG=$(ls $NEW/whg-*.mbtiles | xargs -n1 basename | sed 's/\.mbtiles//' | tr "\n" " ")
J1=$(sbatch -M htc --parsable swap.sbatch "$ORDER" | cut -d";" -f1)
J2=$(sbatch -M htc --parsable --dependency=afterok:$J1 swap.sbatch "$WHG" whg | cut -d";" -f1)
# wd separately, once §9.3 is green:  sbatch -M htc swap.sbatch "wd"
# then, locally:  /usr/bin/python3 ~/Documents/GitHub/whg3/scripts/atlas_smoke.py https://whgazetteer.org   (76/76)
```

### 9.5 Open

* `wd`: SG to accept (or not) the six additional oversize z8/z9 tiles in §9.3 before its swap.
* The swap itself, the harness run, and landing `feat/tile-channels` on `origin/main`
  (fast-forward by cherry-pick) — all after a human submits §9.4.

## 10. Points-only pass — 10 Oct 2026, run `tilech-20261010T151451Z` (built and verified; NOT pushed)

Lane I. Code `origin/main` **`6199c7b`** (every build-side fix, `c1cb2ee` and later) in the separate clone
`/vast/ishi/elastic-tile-channels` (the gateway's `/vast/ishi/elastic` untouched); manifest copied from
`tilech-20261010T102801Z` with every `tiles` stage reset to pending; `submit_tiles_slurm --no-deploy
--points-drop-rate 1 --only-bucket …`; output **`/ix1/ishi/data/tiles-channels-20261010c/`**. Buckets: the
eleven of §4.2's points-only row — `gn` `tgn` `gb` `chgis` `alc` `iv` `tm` `ofs` `og` `dgsd` `dp`
(`gn_capitals` is a context overlay and was left alone). **The tileserver was read (`df`, `ls`, one
`rsync --checksum` *from* it) and never written, reloaded or reconfigured.**

### 10.1 The push tooling (Task 1) — branch `feat/tiles-atomic-push`, `dde1b78`, not on `origin/main`

§4.4's rule is now code. `generate_tiles --redeploy-only --push-mode auto|inplace|rename`
(`ATOMIC_PUSH_MIN_BYTES` = 200 MiB decides `auto`; `ATOMIC_PUSH_FREE_MARGIN_BYTES` = 1 GiB).
`atomic_swap_tileset`: `df -B1` **on the tileserver** and refuse unless free ≥ new size + margin (the old inode
is held by `tiler.service` until the restart, so both files must fit); `rsync --inplace` to `<b>.mbtiles.new`
(the live file is untouched for the whole transfer); `mv <b>.mbtiles <b>.mbtiles.prev && mv <b>.mbtiles.new
<b>.mbtiles` in one shell, then `stat` the live size against the local file; `update_tileserver_config
--execute` (restart + `/data/<b>.json` 200) plus an independent TileJSON `vector_layers == [<b>]` check;
`rm .prev` only after the check passes; on a failed check `.prev` is renamed back, the restart repeated and
the rejected file discarded. `tilech-swap.sbatch` chooses the scheme per bucket with `push_mode_for`, keeps
the 0.25 s poller, re-checks the TileJSON itself, and after a tool failure proves by sha256 whether the tool's
own rollback restored the live file before rsyncing the `/ix1` backup; `NEW`/`BAK`/`REPO`/`PUSH_MODE` are
`--export` parameters. `tests/test_tiles_atomic_push.py`: 15 tests with ssh/rsync faked (exact host
sequence, rollback path, disk refusal with the numbers printed, unmeasurable `df` = refusal, no key =
refusal, mode selection, CLI); 7 mutations (reversed `mv` order, disk check removed, wrong cleanup target,
no rollback, threshold off by one, size check removed, push to the live name) each turn the suite red.
Smoke from the CRC `whg` env against the real files: `gn`/`tgn` → `rename`, `gb`/`chgis` → `inplace`,
`df` over ssh 7,384,477,696 B. `developer/tile-qa/oversize.py` names every tile over a limit at every zoom;
`tilech-verify-points.sbatch` and `tilech-live-backup-points.sbatch` are the jobs below.

### 10.2 Backup, disk, build

* **Backup** (Slurm 11893267, 7 min): the eleven live files added to `/ix1/ishi/data/tiles-live-backup-20261010/`
  — seeded from the 2 Sep `/ix1` build, then `rsync --checksum` *from the tileserver*: **0 bytes transferred
  for 10 of 11**; `tgn`'s seed (446.9 MB, 2 Sep) was NOT live — live is the 21 Sep build (462,290,944 B,
  job 11412177) and was fetched. `SHA256SUMS` now 71 lines, `sha256sum -c` clean.
* **Disk, measured on the tileserver before anything**: 7,387,144,192 B free (6.9 G of 48 G, 86 %).
* **Build**: 11893269 (8 small, 7–29 s each), 11893270 (`gb` 4:15, `tgn` 10:12 — tippecanoe 188 s),
  11893271 (`gn`, 64 GB tier: **31:24**, tippecanoe **722 s**, MaxRSS 9.99 GB; 2 Sep: 52:48 / 1,061 s /
  9.72 GB). 11/11 gates PASS. `gn` streams 13,454,817 points, `tgn` 2,972,719, `gb` 1,174,449.

| bucket | live B | NEW B | growth | fits under §10.1's rule (NEW + 1 GiB ≤ 7,387,144,192)? |
|---|---:|---:|---:|---|
| `gn` | 1,611,468,800 | **2,167,218,176** | +34.5 % | **yes**: needs 3,240,960,000, spare 4,146,184,192 |
| `tgn` | 462,290,944 | **682,446,848** | +47.6 % | **yes**: needs 1,756,188,672, spare 5,630,955,520 |
| `gb` | 138,145,792 | 147,017,728 | +6.4 % | inplace (< 200 MiB) |
| `chgis` | 19,750,912 | 32,395,264 | +64 % | inplace |
| `tm` | 4,894,720 | 9,019,392 | +84 % | inplace |
| `alc` | 4,235,264 | 7,020,544 | +66 % | inplace |
| `iv` | 3,919,872 | 5,894,144 | +50 % | inplace |
| `ofs` | 2,764,800 | 4,681,728 | +69 % | inplace |
| `dp` | 2,428,928 | 3,317,760 | +37 % | inplace |
| `dgsd` | 675,840 | 1,126,400 | +67 % | inplace |
| `og` | 172,032 | 253,952 | +48 % | inplace |

After the whole swap the volume holds **+834 MB** net (2,250,747,904 → 3,060,794,368 B for the eleven), so
free space goes from 6.9 G to ≈ 6.1 G. If `tgn` is swapped first its +220 MB is already gone when `gn`'s `df`
runs: 7,387,144,192 − 220,155,904 = 7,166,988,288 ≥ 3,240,960,000, still fits.

### 10.3 Verification (Slurm 11893328 nine small, 11893391 `tgn`, 11893431 `gn`; §9's checks)

* **Ledger match: 11/11** (`verify_tileset_channels` exit 0; every ledger `polygon 0 / line 0 / point N`,
  one clustered points channel z0–10). **No tile over 500,000 B in any of the eleven** (`oversize.py` at
  every zoom: `gn` 0 of 260,605 tiles, max z9 390,162 B vs live 403,258; `tgn` 0 of 153,324, max z9 403,006
  vs 207,439; `gb` max z9 388,959 vs 388,986; the rest ≤ 107 KB). z0–z7 maxima: `gn` 57–90 KB (live
  14–74), `tgn` 43–81 KB (live 7–55).
* **Nothing lost at max zoom**: the z10 sample is identical point for point in all eleven (`gn` 17,346 =
  17,346; `tgn` 7,279; `gb` 231,410; `chgis` 3,901; `alc` 1,006; `iv` 13,709; `ofs` 5,766; `tm` 827;
  `dp` 243; `dgsd` 319; `og` 271), and z9 is no longer rate-dropped (`gn` 36,666 vs 17,382 in 200 tiles,
  `tgn` 13,030 vs 6,525, `gb` 345,520 vs 256,462).
* **Low-zoom mass restored** (z0 is one tile, so the figure is exact): `gn` 513 clusters + 21 points stand for
  **13,454,513 of 13,454,817** (live: 1,411); `tgn` 347 + 38 for 2,972,716 of 2,972,719 (live 312); `gb`
  4 clusters for 1,174,449 of 1,174,449 (live 123); `chgis` 81,292 of 81,292 (live 9); `alc` 17,997 of
  17,997 (live 2); the other six likewise complete. The 304 / 3 points short at z0 for `gn` / `tgn` are
  presumably outside the z0 tile's ±85.05° (inference, not measured). At z1–z7 `reprN` exceeds the input
  because clusters in tile buffers are decoded in two tiles (inference from the shape: ≈ 1.05–1.08× for
  `gn`); z0 has no neighbour and matches.
* **Tile counts per zoom identical NEW vs OLD in all eleven**, so `verify_tileset_coverage`'s "missing land
  tiles" on `alc` (23) and `chgis` (3) are inherited exactly as `hgis`/`nl` were in §8.3 (`alc` z0–z4
  1/3/6/12/30 in both, `chgis` 1/4/7/13/26 in both) — the checker's global heuristic on regional data.
* **Headless Atlas** (Playwright, bundled Chromium; `--prove-it-fails` first: every check failed on a page
  with no map; LIVE control; NEW route-intercepted into the prod client; `gn` measured at its view from a
  subset mbtiles of that view's tiles after the one-ssh-call-per-tile route twice ran out of idle budget):
  every NEW tileset boots with 0 page errors; at z9 circles are drawn where LIVE draws them and more, nothing
  rate-dropped (`gn` 10,808 vs 9,753, `tgn` 8,563 vs 4,349, `gb` 10,451 vs 9,900, `iv` 1,456 vs 583,
  `alc` 680 vs 273, `dp` 12 vs 6, `tm` 327 vs 131, `ofs` 825 vs 330, `og` 15 vs 9, `dgsd` 36 vs 16);
  at z5 the heat weight in view is `gn` 115,478 vs 6,213, `tgn` 32,183 vs 3,148, `gb` 22,147 vs 2,202,
  `tm` 2,697 vs 88, `alc` 1,384 vs 51, `dp` 540 vs 17. `chgis` renders nothing at its auto-chosen view in
  LIVE and NEW alike: that view is the centre of its largest z9 tile at **lng −0.35°, lat 0.35°** — the
  biggest `chgis` tile is at null island, which looks like a bad-coordinate population and is a data
  question for `chgis`, not a tiles one (observation, not investigated).

### 10.4 Swap job (written, NOT submitted; needs SG's go) and rollback

The job is `developer/sbatch-templates/tilech-swap.sbatch` on branch `feat/tiles-atomic-push`, checked out
at `/vast/ishi/elastic-tile-channels` (`dde1b78`). Two submissions: the nine small buckets pushed in place
with ONE restart at the end (the second positional argument, `whg`, means exactly that — nine restarts of
both services for nine files of 0.25–147 MB would be nine global blinks for nothing), then `tgn` and `gn`
by rename, chained `afterok`:

```bash
# on crc0, as stg135 — nothing below runs until SG says so
cd /vast/ishi/elastic-tile-channels && git log --oneline -1          # dde1b78 feat/tiles-atomic-push
NEW=/ix1/ishi/data/tiles-channels-20261010c
BAK=/ix1/ishi/data/tiles-live-backup-20261010
J1=$(sbatch -M htc --parsable --export=ALL,NEW=$NEW,BAK=$BAK developer/sbatch-templates/tilech-swap.sbatch "og dgsd dp ofs iv alc tm chgis gb" whg | cut -d";" -f1)
J2=$(sbatch -M htc --parsable --dependency=afterok:$J1 --export=ALL,NEW=$NEW,BAK=$BAK developer/sbatch-templates/tilech-swap.sbatch "tgn gn" | cut -d";" -f1)
# watch:  sacct -M htc -X -o JobID,State,Elapsed -j $J1,$J2 ; logs/tilech-swap-<id>.out carries per-bucket
#         "monitor: samples=N bad=M (x0.25 s = unavailability)", the sha256 of what is live, any LEFTOVER
#         <b>.mbtiles.{new,prev,rejected} on the host, and df after each bucket
# then, locally:  /usr/bin/python3 ~/Documents/GitHub/whg3/scripts/atlas_smoke.py https://whgazetteer.org   (76/76)
```

Rollback, per bucket, from the verified backup (the job already does this itself on any failed check; these
are for a later decision). For `gn`/`tgn` use the rename scheme so the rollback is not itself a 40-minute
tear; for the small ones `--inplace` is seconds:

```bash
cd /vast/ishi/elastic-tile-channels; set -a; . ./.env.local; set +a        # TILESERVER_SSH_KEY
BAK=/ix1/ishi/data/tiles-live-backup-20261010
python -m processing.generate_tiles --bucket gn  --output-dir $BAK --redeploy-only --push-mode rename   # push + rename + restart + check; .prev removed
python -m processing.generate_tiles --bucket tgn --output-dir $BAK --redeploy-only --push-mode rename
python -m processing.generate_tiles --bucket <small> --output-dir $BAK --redeploy-only --push-mode inplace && \
    python -m processing.update_tileserver_config --bucket <small> --execute
ssh -i $TILESERVER_SSH_KEY whgadmin@134.209.177.234 'sha256sum /srv/tileserver/tiles/<b>.mbtiles'; grep " <b>.mbtiles$" $BAK/SHA256SUMS
```

**Unavailability under the rename scheme (inference, not yet measured):** the live file is untouched during
the transfer, so a bucket is unavailable only for the restart; on 10 Oct the buckets whose push was ~1 s
(`vob_*`, `ukhc`, `kain_par`) measured 8–27 s end to end, so expect ~10–30 s each for `gn` and `tgn` against
`wd`'s 2,387 s under `--inplace`. The nine in-place pushes tear each small file for its own transfer (≤ 147 MB;
`gb`-sized was 19–25 s on 10 Oct for `hgis`/`pl`) and share one restart. Every restart is global: all tilesets
blink for it. The poller in the job will replace this paragraph with measurements.

### 10.5 Open

* SG's go for §10.4 (two `sbatch` lines), then the harness, then landing `feat/tiles-atomic-push` on
  `origin/main` (it carries nothing but the push tooling, the two job templates, `oversize.py` and this §10).
* `chgis`'s null-island tile (§10.3), for the `chgis` ingest, not for tiles.
