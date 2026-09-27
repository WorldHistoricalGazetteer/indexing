# Vision of Ireland

**Status (27 September 2026): converted and round-tripped as a PLATO conformance test. Not
ingested.**

Vision of Ireland is the teaching database designed and built by **Humphrey Southall**
(University of Portsmouth) as a small subset of the Vision of Britain system. He has seen the web
front end, asked that it not be deleted, and intends to use it in presentations.

## Source

The flattened views in `site/data/`, built by `build/01_export_views.sql` from the database
Southall's own scripts create. Nothing here re-models his data; the flattening is his repository's.

| | |
|---|---|
| Where | `github.com/docuracy/vision-of-ireland`, working copy at `~/Documents/GitHub/vision-of-ireland` |
| Version | commit `54d8b65`, 2026-09-23 |
| Licence | boundaries ODbL, re-sourced from OpenStreetMap; see the repository's *Licensing* section |

Measured: 2,925 units on five levels (COUNTRY 1, IRL_PROV 4, IRL_CNTY 45, IRL_BARONY 364,
IRL_PAR 2,511), 3,455 names, 6,380 relations, 255,760 statistical values for 1841 and 1851 in
twelve nCubes.

**`ul_auth` is not here.** `01_export_views.sql` selects `im_auth` only, so the
immediate-versus-ultimate authority distinction cannot be evidenced from the exported views, only
from Southall's database. Worth asking him directly what the two columns mean before anyone acts
on an inference about them.

## Converting

```
python3 voi2plato.py forward <vision-of-ireland checkout> voi-plato.json
python3 voi2plato.py inverse <roundtripped .json|.jsonl> voi-back.json
python3 voi2plato.py compare <vision-of-ireland checkout> voi-back.json
```

Forward → `plato_run.mjs convert ntriples` → `convert plato-jsonl` → inverse → compare.

## Result

2,925 entities, 268,519 attestations, 3,419,873 triples. Schema-clean.
**0 differences across 796,305 values.**

Negative controls, each planted in the N-Triples and each caught: a toponym, an nCube cell label,
and a numeric value.

The cell-label control matters. An earlier `inverse` read the cell back out of the property URI
this script mints, so corrupting the source's own words changed nothing. It now reads the cell from
`sourceLabel` and requires it to agree with the URI.

## Why this corpus is worth converting

The statistics. Every value knows three things PLATO has no structured place for, and unlike the
Vision of Britain tables they are here in words rather than in opaque codes:

* **the universe**, in the nCube label: *Families by Housing Class (Ireland)* counts families;
  *Persons aged 15+ by Sex and Irish 1841/51 Occupational Classification* counts persons aged 15
  and over. Get the universe wrong and the figure means something else.
* **the address**, in the cell: `First Class`, or `Female / Charity` where the cube has two
  dimensions. A coordinate, not a property of the place.
* **the denominator**, in a sibling nCube: *Total Population*, *Total Families* and *Total Houses*
  are recorded for the same unit and year as the cubes they are the universe of.

All three survive the round trip. None can be said. The universe survives only inside a property
URI this script invents, which is the finding.

`Area (acres)` is the one cube whose measure has a unit PLATO can hold, so it carries
`unit: http://qudt.org/vocab/unit/AC`.

## Downgraded, after checking

Two candidates that turned out to be misreadings, both caught by the round trip rather than by
reasoning:

* **Relation wording without a type.** The JSON Schema requires `relationType` on every relation,
  and VoI states relations only in words. But it has exactly one wording, "was a part of", 6,380
  times, which is `plato:ContainedIn`. Nothing was forced, so the relation carries PLATO's own
  starter type with the wording in `relationLabel`.
* **Name status.** VoI records Preferred 2,926, Alternate 495, Abbreviation 32, Official 2, and
  PLATO's FormStatusScheme has none of the last three. But `form_status` says "starter concepts"
  and the JSON Schema takes any URI, so the vocabulary is open at both layers and VoI's statuses
  are minted as project concepts. Collapsing them onto `plato:Attested`, which is what an earlier
  version did, asserted that an abbreviation and an official name are the same kind of form.

## Open questions

* One statistical value in the corpus is null. PLATO drops null from RDF, so emitting it would make
  the round trip lie; it is skipped, and excluded from the comparison's denominator.
* Whether the nCube label belongs in a structured slot, and whether measure and universe should be
  linked directly or through a grouping node.
