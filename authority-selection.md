# Authority Selection

This file is the canonical run-selection control for ingestion.

- `[x]` include authority in current run
- `[ ]` exclude authority in current run
- Excluding an authority removes its staged artefacts at run start.
- Excluding an authority does not delete cached source files.
- Do not use any separate ad hoc authority-removal mechanism.

## Core Authorities (local)

- [x] `osm` - OpenStreetMap places
- [x] `ohm` - OpenHistoricalMap places
- [x] `gn` - GeoNames places + toponym updates
- [x] `wd` - Wikidata places + geoshape updates
- [x] `tgn` - Getty TGN
- [x] `pl` - Pleiades
- [x] `un` - UN countries
- [x] `dp` - D-PLACE
- [x] `nl` - Native Land
- [x] `gb` - GB1900
- [x] `iv` - Index Villaris
- [x] `chgis` - CHGIS/TGAZ
- [x] `dgsd` - DGSD
- [x] `tm` - Trismegistos
- [x] `po` - PeriodO
- [x] `clio` - Cliopatria
- [x] `loc` - Library of Congress relations update

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

