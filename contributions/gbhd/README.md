# Great Britain Historical Database (Vision of Britain)

**Status (27 September 2026): converted and round-tripped as a PLATO conformance test. Not
ingested.** Like `pleiades/`, this is an assessment rather than an ingestion route.

## Sources

Both **open, no registration required, CC BY-SA 4.0**, downloaded 2026-09-27.

| | |
|---|---|
| SN 9033 | Digital Boundaries for Registration Counties of England and Wales, 1851-1911 |
| | doi:10.5255/UKDA-SN-9033-1 — `sn9033.zip`, 54,960,549 bytes |
| | sha256 `f38fa3b0f9588bc92857ff0b2243a20e7872de9354c28e744325ef17efff01d0` |
| SN 4559 | Census Data: Occupational Statistics, 1841-1991 |
| | doi:10.5255/UKDA-SN-4559-2 — `sn4559.zip` (TAB edition), 38,943,859 bytes |
| | sha256 `e0dcc58cbe10e8d1fe9cb8b5fc9e0b5e7bf85ca38e5acc60eb0d5bf0d8d5507c` |

Fetching: the catalogue is a single-page app and the download URL is built client-side, so the
files come from the study pages at `datacatalogue.ukdataservice.ac.uk/studies/study/{9033,4559}`
rather than from a stable link. A cookie notice can swallow the first click.

**Share-alike matters even though nothing is published.** A PLATO serialisation is an adaptation
and would inherit BY-SA. Nothing derived from these files should be published without that being
decided first.

**What is not available.** SN 3678, *Geographical Units and Changes 1888-1973*, is embargoed and
has no open successor. That is the `g_unit` backbone. Ten other original deposits are embargoed
too, but those are superseded rather than withheld: SN 3706 says in as many words that its data
"can be accessed openly under SN4559". The boundary studies substitute for units well enough for a
conformance test and are not the same thing.

## Coverage is by SHAPE, not by table

Every structural form in the deposit is exercised once, so that what the round trip proves is
proved for each form, rather than 27 times for three forms. 24 of the 27 tables carry a unit id;
the three that do not cannot be joined to a place at all.

| | |
|---|---|
| boundaries | SN 9033, all 7 years: one entity per `G_UNIT`, one geometry attestation per year |
| statistics, long | `occ_1851_ew`: one row per (county, occupation, sex), measure in `persons` |
| statistics, wide | `occ_1911_sm_m`: occupation categories as COLUMNS, with `total`, `retired` and `occupied` as sibling denominators |

## Converting

```
python3 vob2plato.py forward <dir with UKDA-9033-xml/ and UKDA-4559-tab/> vob-plato.json
python3 vob2plato.py inverse <roundtripped .json|.jsonl> vob-back/
python3 vob2plato.py compare <same dir> vob-back/
```

Needs `pyshp`. Forward → `plato_run.mjs convert ntriples` → `convert plato-jsonl` → inverse →
compare.

## Result

815 entities, 31,450 attestations, 1,920,798 triples. Schema-clean.
**0 differences across 357,450 non-empty source cells.**

Negative controls, each planted in the N-Triples and each caught: a name, a cell's text, and a
parsed number.

The numeric control matters. An earlier version of `inverse` read the source's own cell text and
ignored the parsed number beside it, so corrupting the number changed nothing and that region of
the check could not fail. `inverse` now requires the two to agree and reports it when they do not.

## What the conversion could not say

* **The dimensional address of a cell.** `occ_1851_ew` is 29,150 counts, each at (county, class,
  subclass, occupation, sex). A PropertyValue holds one property, one value and one unit, so the
  address scatters across sibling PropertyValues grouped only by a shared attestation `@id`, and
  nothing says which is the measure. It asserts the county *has a property* `class = 1`, which is
  false: that is a coordinate of a cell.
* **The universe, though it is right there in the row.** `occ_1911_sm_m` puts `total`, `retired`
  and `occupied` beside opaque occupational codes like `ix1coal`. PLATO holds every number and
  cannot say that `ix1coal` is to be read against `occupied`.
* **A table's own scope.** "Males in towns over 5,000 population" is in the documentation PDF, not
  the data. It is parked in `notes`, which is free text.

## Downgraded, after checking

`IM_AUTH`, the immediate authority on every boundary row, looked like a per-claim authority with no
home. In this extract it is constant (`GBHGIS`) and identical to the citation's source, so it is
redundant rather than unmodellable. The immediate-versus-ultimate distinction needs `ul_auth`,
which is in neither of these deposits nor in Vision of Ireland's exported views.
