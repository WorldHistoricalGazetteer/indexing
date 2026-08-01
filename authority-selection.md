# Authority Selection

This file is the canonical run-selection control for ingestion.

- `[x]` include authority in current run
- `[ ]` exclude authority in current run
- Excluding an authority removes its staged artefacts at run start.
- Excluding an authority does not delete cached source files.
- Do not use any separate ad hoc authority-removal mechanism.

## Core Authorities (local)

> **Every namespace in `INGESTION_ORDER` must appear here.** Omission is not the
> same as `[ ]`: an absent entry parses as deselected, and deselection *deletes*
> that namespace's staged artefacts at run start. This file drifted to 18 of 27
> between May and July 2026, so a from-scratch run would have silently dropped
> nine authorities and deleted their staged trees. `tests/test_authority_selection.py`
> now fails if the two lists diverge.

- [x] `osm` - OpenStreetMap places
- [x] `ohm` - OpenHistoricalMap places
- [x] `gn` - GeoNames places + toponym updates
- [x] `wd` - Wikidata places + geoshape updates
- [x] `tgn` - Getty TGN
- [x] `pl` - Pleiades
- [x] `un` - UN countries (ISO country boundaries; the ccode authority)
- [x] `dp` - D-PLACE
- [x] `nl` - Native Land
- [x] `ukhc` - UK Historic Counties
- [x] `gb` - GB1900
- [x] `iv` - Index Villaris
- [x] `chgis` - CHGIS/TGAZ
- [x] `dgsd` - DGSD
- [x] `tm` - Trismegistos
- [x] `po` - PeriodO
- [x] `clio` - Cliopatria
- [x] `ofs` - Ottoman NFS Gazetteer
- [x] `og` - Ottoman Gazetteer (ottgaz; reads the `ofs` staged extract for hulls)
- [x] `hgis` - HGIS de las Indias (lugares + territorios)
- [x] `alc` - Alcedo / TopUrbi
- [x] `kain_par` - Kain & Oliver ancient parishes (pre-1850)
- [x] `vob_rd` - GBHGIS registration districts / poor-law unions
- [x] `vob_rc` - GBHGIS registration counties
- [x] `vob_cty` - GBHGIS administrative counties
- [x] `vob_lgd` - GBHGIS local-government districts
- [x] `loc` - Library of Congress relations update (relations only; Batch 12)

## WHG Authorities (group + discovered datasets)

- [x] `whg` - enable WHG dataset ingestion as a group

WHG dataset eligibility is still controlled remotely by Django `Dataset.authority`.
If `whg` is unchecked above, discovered WHG datasets are ignored for the run.

Discovered WHG datasets (last bootstrap):

- [x] `whg:892` - Geographic Names of Antarctica
- [x] `whg:1052` - Indigenous Place Names of the Yukon
- [x] `whg:1076` - Indigenous Place Names in Florida
- [x] `whg:1361` - Theophanes Bulgaria Places
- [x] `whg:1481` - Eritrea Settlements
- [x] `whg:1485` - Congo Settlements
- [x] `whg:1486` - Gabon Settlements

Notes:
- This dataset subsection can be refreshed from
  `GET /reconcile/authority-datasets`.
- New discovered datasets should be appended as checked by default on refresh,
  then edited manually if needed.

