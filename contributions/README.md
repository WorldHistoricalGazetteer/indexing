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
