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
