# Tile QA scripts — preserved from the 7 August 2026 verification pass

Four ad-hoc scripts written during the 7 August tile verification, recovered
from `/vast/ishi/tiles-verify` before that tree was released on 3 September
2026. **They existed nowhere else** — not in this repository under any name —
and survived only because the deleting session embedded their full source in
`/vast/ishi/tiles-verify-manifest-20260903.json` (23 KB) rather than deleting
them with the 9.2 GB of tilesets around them.

They are kept because each answers a question about a *built* tileset that the
pipeline's own status cannot answer, which is the standing lesson of this
campaign: **a tile job that reports success is not evidence it read any
geometry.**

| script | question it answers |
|---|---|
| `tsize.py` | Per-zoom tile counts, max and mean tile bytes, **and how many tiles exceed 500,000 bytes**. Takes two tileset paths and compares them. |
| `lblchk.py` | Which buckets carry a `label` field in their `vector_layers`, with tile counts and sizes. |
| `finalchk.py` | Joins the build logs' reported `poly=`/`point=` counts against what each tileset actually contains, and flags buckets whose polygons shipped **without** labels (or labels without polygons). |
| `arealchk.py` | Which namespaces hold `geom_class: "area"` geometries in ES, i.e. which buckets need a labelled rebuild. Queries the live index. |

## Why `tsize.py` earns its place on its own

On 3 September two sessions spent a full round trip arguing about whether
`water.mbtiles` had been built under pressure from tippecanoe's 500 KB tile
ceiling. One inferred pressure from a single tile at 493 KB; the other
"refuted" it with uncompressed byte counts against a cap that applies to
compressed size. **Both were wrong, and `tsize.py` reports exactly the figure
they were each guessing at** — `COUNT(*) ... WHERE LENGTH(tile_data) > 500000`,
over every tile, per zoom.

Had it been in the repository and run, that dispute would not have happened.
That is the argument for keeping ad-hoc forensic tools rather than rewriting
them each time.

⚠️ **The dispute was ultimately settled by neither measurement** but by reading
`metadata.generator_options`, which records the build's own invocation inside
the artefact. Prefer that where it exists; these scripts are for questions the
metadata cannot answer.

## Changes from the originals

Kept as close to verbatim as possible — they are forensic artefacts as well as
tools, and the **unmodified originals remain in the 3 September manifest**.

* `finalchk.py`, `lblchk.py`: the hardcoded `/vast/ishi/tiles-verify` directory
  is now a positional argument (or `WHG_TILE_QA_DIR`), defaulting to the
  original path. That tree no longer contains `.mbtiles`, so without this they
  scan an empty directory and report nothing — **a silent zero, from a
  predicate that can no longer match.**
* Nothing else. In particular the hardcoded assumptions below are left alone.

## Hardcoded assumptions — read before trusting output

* `arealchk.py` reads `/ix1/ishi/es/config/elastic.password` and queries
  `gazetteer.crcd.pitt.edu:9200` directly. Its namespace list is a fixed
  14-entry literal and **is not the full corpus of 27** — a namespace absent
  from that list produces no row, which looks identical to a namespace with no
  areal geometry.
* `finalchk.py` globs build logs by a **specific job id** (`tiles-ns-10756209_*`)
  plus `tl-*.out`. Against any other run it finds no logs and every bucket
  reports `no log` rather than failing.
* `finalchk.py` and `lblchk.py` skip buckets whose names contain `.base`,
  `.coverage` or `.labels`.
* `tsize.py` prints only z0–z8.

Each of these is a denominator the script does not state. **Check what a run
matched before believing what it reports** — the campaign's most repeated fault
is an absent input read as nothing to do.
