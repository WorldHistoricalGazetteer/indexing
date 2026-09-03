# place#233 — OSM water extraction and tiling

Extract real water polygons **from the same planet PBF the admin boundaries
came from**, and tile them to z0–10, so coast and coastal border are the same
geometry rather than two approximations of it.

Run id: `water233-20260902T220703Z`
Working dir on CRC: `/ix1/ishi/water233-20260902T220703Z`

> Scope note: this campaign stops at the production of `water.mbtiles`.
> Deploying it and the `whg-context` style respecification in the second half
> of the issue are **out of scope** and gated on SG.

## Run order

| Script | Step | What it does |
|---|---|---|
| `00_pin_and_probe.sbatch` | 0a, 0b | Tooling probe, then **pins** the planet PBF by hardlink |
| `01_probe_gdal.sbatch` | 0a | Re-probe GDAL/tile-join after the first probe reported two false results |
| `02_probe_conda_net.sbatch` | 1 | Compute-node network + is `osmcoastline` packaged? (it is not) |
| `03_tags_filter.sbatch` | 0c | tags-filter the planet down to water |
| `04_build_osmcoastline.sbatch` | 1 | Build `osmcoastline` from source into an **isolated** env |
| `count_water.py` | controls 1, 2 | Count water objects split by type and closedness |
| `_guards.sh` | all | Volume capacity guards, sourced by every job |

Everything runs under Slurm (`sbatch -M htc --account=ishi`). Nothing runs on a
login node.

## Things that bit us, so they don't bite again

**The PBF is pinned by hardlink, not copied.** Both download paths in
`processing/fetch_authorities.py` write `<dest>.part` and finish with
`temp.replace(dest)` — an atomic **rename**, not an in-place rewrite. So a
hardlink retains the original inode across a refresh and costs zero bytes.
The issue originally said "overwritten in place", which argued for an 87.5 GB
copy; that wording is wrong and has been corrected.

The **edition** identifier is intrinsic and better than mtime or inode:
`osmium fileinfo` reports `osmosis_replication_timestamp=2026-07-20T00:00:00Z`.
mtime and inode are properties of the container; the replication timestamp is
a property of the contents and survives copying.

**⚠️ Guard the quota, not the pool.** `df` on this volume is quota-aware and
the two readings are near-indistinguishable — *both print `Mounted on /vast`*:

    df -h /vast        3.9P total, 3.3P avail    <- the whole VAST pool
    df -h /vast/ishi   1.0T total, 226G avail    <- our project quota

A guard pointed at `/vast` compares 3.3 PB against a 160 GB floor and can
**never fire**, while printing a healthy-looking free figure. The first
version of `_guards.sh` had exactly this bug. `guard_selftest` now runs the
guard against an impossible floor and **fails the job if it does not abort**,
so the guard is proven to discriminate at run time rather than argued for in
review. The same bug was found and fixed in
`processing/build_geom_index_sqlite.sbatch` (commit d506837).

**`osmium` and `ogr2ogr` cannot share one exported loader path.** osmium needs
`$CONDA_PREFIX/lib`; under it the system `ogr2ogr` dies with
`symbol lookup error: /lib64/libldap.so.2: undefined symbol: EVP_md2`.
Do **not** `export LD_LIBRARY_PATH` — prefix it per command instead.

**Do not request `boost-cpp`.** It is the deprecated package name and
conflicts with the `libboost` that current `libosmium` requires. Use
`libboost-devel`.

**Bulk work goes on `/ix1`, not `/vast`.** `/vast` is 1 TB shared with
production Elasticsearch and has hit flood-stage read-only in this project's
history; `/ix1` has ~1.8 TB free and no ES. Tippecanoe's temp (heavy random
I/O) goes to node-local `$SLURM_SCRATCH`, since `/ix1` is NFS with a
small-file pathology.

## Controls

Each exists because its failure mode is a plausible-looking pass. Report every
one as a **ratio against what was expected**, never as a bare count or a bare
OK.

1. **Filter kept what it should** — compare the filtered file's per-tag way and
   relation counts against the planet's. A tags-filter expression matching
   nothing yields a small, valid, useless PBF that every later step succeeds on.
2. **Multipolygon relations survive assembly** — on a small extract, count
   `natural=water` closed ways and multipolygon relations *separately*, then
   count polygons out. Simple ponds dominate numerically, so dropping every
   relation still looks healthy while losing exactly the big complex lakes.
   No total can see this; only the ways-vs-relations split can.
3. **Known-bad present before building** — confirm the coast/border mismatch is
   visibly there on the *current* map, so that a post-build "they look
   coincident" can distinguish a fixed map from an insensitive eye. This is
   evidence about the CURRENT map and must never be restated as if it were the
   post-build check.


## Result — built 3 September 2026

`water.mbtiles` **1.30 GB**, z0–10, 1,030,437 tiles, worst tile **454.4 KB**
(under tippecanoe's 500 KB ceiling, so no tile is at risk of being silently
skipped by `tile-join`).

| layer | source features | source bytes |
|---|---:|---:|
| ocean | 57,413 | 1.82 GiB |
| lakes | 22,432,127 | 18.20 GiB |
| rivers | 712,884 | 3.52 GiB |

Per-zoom totals: z7 93.9 MB · z8 192.3 MB · z9 344.9 MB · z10 531.4 MB.

All three layers were verified **present in decoded tiles**, not merely
declared in metadata, at five known locations — ocean at Neum and in the open
Pacific, rivers dominant in the Nile delta, lakes dominant in the Finnish
Lakeland, no ocean inland.

### Ocean: the close-distance trap

The first ocean build inherited `--close-distance=1` while running
`--srs=4326`. That option is in the **units of the output SRS**, so the
tolerance was 1 degree ≈ **111 km**, not the 1 metre the default intends in
osmcoastline's native EPSG:3857. It bridged **12.0 km and 15.6 km** of open
water, joining coastlines that are not connected.

Rebuilt with `-c 0.001` (~111 m), a value chosen for the scale of coastline
vertex spacing. Measured comparison:

| | c=1° (~111 km) | c=0.001° (~111 m) |
|---|---:|---:|
| polygons | 57,413 | 57,413 |
| total area | 354,241,468 km² | 354,241,475 km² |
| `added_line` | 3 | 0 |
| `not_closed` | 2 | 5 |

Area delta **+7 km² (+0.0000%)** — refusing the bridges cost no coverage.

⚠️ Polygon **count** is a poor comparator here: at `--max-points=1000` the
water polygons are split pieces, so the count measures geometric complexity,
not coverage. Compare **area**.

### The fallback ladder — measured, not assumed

The size gate passed with room, so no fallback was applied. If one is ever
needed, the rungs measured on this corpus:

1. **Minimum-area threshold.** 73.5% of inland features are under 1 hectare —
   0.65 px at z10, invisible — and cutting them removes 36.7% of source
   bytes. Cartographically correct rather than a degradation.
2. **Drop the rivers layer** — 3.52 GiB, 16.8% of inland bytes. But it removes
   a *visible category*, so it likely ranks below capping inland zoom. That
   ordering is SG's call.
3. ⚠️ **`waterway=riverbank` is dead** — 3 ways planet-wide. Dropping it saves
   nothing; river areas are `natural=water` + `water=river` now.

No area threshold gets past ~90% of bytes: the remainder is large lakes with
huge vertex counts, which must be kept. Simplification is the next axis, not
a lower threshold.
