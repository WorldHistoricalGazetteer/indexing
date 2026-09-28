# contributions/

Converters and assessments for datasets that come to WHG as **contributions**: a project's
own data, published under its name as a `whg:<dataset_id>` dataset. This folder is not for
**authorities**, i.e. reference gazetteers staged into their own namespace by a script in
`authorities/`.

The difference is the route:

| | `authorities/` | `contributions/` |
|---|---|---|
| Enters WHG | an `authorities/*-places.py` script stages it into its own namespace | the contributor (or we, on their behalf) submits a file through whg3; `authorities/whg-places.py` then stages every `authority=True` dataset into `whg` |
| Output of the code here | staged place docs | a **submission file**: PLATO JSON, and LPF for as long as whg3 accepts only LPF |
| Who owns the data | the upstream authority | the contributor, who sees and approves what we send |

So nothing in this folder writes to Elasticsearch or the geom store. Its output goes through
the same accessioning as any contributor's upload.

## One folder per contributor

```
contributions/<contributor>/
  README.md          sources (where, version, licence, how to fetch), mapping decisions, open questions
  <source>2plato.py  forward (source -> PLATO), and inverse + compare wherever reversibility is claimed
```

* Keep the source data **out** of git where it can be fetched. Record the DOI or URL, the version,
  and a checksum instead.
* A claim that a conversion is lossless needs a round trip that can fail: forward to PLATO, through
  RDF with `plato_run.mjs`, then back to the source's own cells and diff them.
* The negative controls must corrupt values the comparison actually **reads**. Corrupting one it
  ignores passes and looks like success. `controls.py` corrupts one value of every predicate in
  the graph and lists those whose corruption the check did not notice.
* A value carried twice (a coordinate as text and as numbers; a label and a sourceLabel) must be
  **required to agree** by the inverse. If the inverse reads only one of them, the other is never
  verified.
* LPF is lossy relative to PLATO (form status, per-value certainty, property values and
  meta-attestations are dropped). `plato_run.mjs <file> convert lpf <out>` lists what is lost.

`controls.py`: `python3 controls.py <graph.nt> <workdir> -- <check command with {nt} {out}>`.

`plato_run.mjs` drives the [PLATO tools](https://pelagios.org/plato-tools/) engine from Node:
`PLATO_TOOLS=<plato-tools checkout> node plato_run.mjs <in> check|convert <target> [<out>]`.

## Moved here from `authorities/` (27 September 2026)

`kgld/` and `joseon/`. `kgld/build_lpf.py` resolves its paths from its own file, and its outputs
were rebuilt at the new path and are byte-identical.

`authorities/hgis/` stays where it is. It looks like a contribution (its inputs are two
`whg_dataset_*.lpf` files), but `authorities/hgis-places.py` stages it as the `hgis` authority,
reading from `DATA_DIR/authorities/hgis/`.

## Statistical tables (PLATO issue #14)

`cube_encode.py` encodes Vision of Britain's `occ_1851_ew` and Vision of Ireland's twelve nCubes
under PLATO's statistics design, where a `plato:PropertyValue` is also a `qb:Observation`. It was
written to test that design and is kept because the release notes re-run it.

```
python3 cube_encode.py vob <gbhd dir>  vob-cube.json     # forward
python3 cube_encode.py voi <voi repo>  voi-cube.json
python3 cube_encode.py inverse <returned .json|.jsonl> back.json
python3 cube_encode.py compare-vob <gbhd dir> back.json  # 0 differences over 204,050 values
python3 cube_encode.py compare-voi <voi repo> back.json  # 0 differences over 1,023,036 values
```

Checking a cube export is `plato-tools datacube FILE…`, which streams the file and reports a
constraint over nothing as not tested rather than passed. `cube_ic.py` here runs the
specification's own SPARQL instead, which is worth having only as a cross-check on a sample or a
small corpus: on Vision of Britain it agrees with `plato-tools datacube` on all five constraints
and takes eleven minutes against four seconds.

Two traps `cube_ic.py` exists to document, both of which made a check report success over nothing:

* **Normalise before running the specification's queries.** The export writes a structure's
  components abbreviated (`_:c qb:dimension <p>`), and the spec's queries reach them only through
  `qb:componentProperty`, which section 10.3 adds. Run them as written on the export and IC-11,
  IC-12 and IC-14 find no components and hold vacuously. `--raw` skips normalisation to show this.
* **Declare what the export adds.** Until plato-tools 14c6047 the cube export wrote
  `sdmx-dimension:refArea` on every observation and declared it in no structure, so IC-12 read the
  same occupation in all 55 registration counties as 28,620 duplicate observations, and IC-11 could
  never have reported a missing area.

`controls.py` is the right way to check a round trip can fail: one corruption per predicate, every
one required to be caught. Six hand-picked controls over the cube round trip caught three; the
three that passed silently were fields the comparison never read (the parsed number beside the
printed cell, the dimension codes that *are* the cell, and the universe link). Reading them took
the values checked from 174,900 to 204,050 on Vision of Britain and from 511,518 to 1,023,036 on
Vision of Ireland. Report the values checked beside the differences: 0 differences over half the
data looks exactly like 0 differences over all of it.
