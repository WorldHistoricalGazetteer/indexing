# GLOBALISE (Huygens Institute): VOC-archive places

**Status (27 September 2026): assessed, not ingested.** SG: revisit once the project is properly
launched. GLOBALISE Places **v4 is due late 2026** and is to merge the Necessary Reunions
gazetteer and the GAVOC vol. I index into the main dataset, so the three converters here may
collapse into one.

## Sources

| | Where | Licence | Measured |
|---|---|---|---|
| GLOBALISE Places v3.0 | doi:10.34894/UFFFNO (DataverseNL, 2026-04-01), one xlsx | CC BY 4.0 | 2,372 places, 9,488 names, 2,337 types, 1,330 relations, 471 bibliography entries |
| Necessary Reunions (NeRu) gazetteer | `globalise-huygens/necessary-reunions`, `public/neru/*.csv` (commit 71e60a6) | **none stated for the data** (repo is EUPL, a software licence) | 393 places |
| GAVOC vol. I atlas index | same repo, `public/gavoc-atlas-index.csv` | **none stated**; a transcription of the index of a 2006 Asia Maior atlas, so rights need asking | 11,084 rows |

Fetching the v3 workbook: dataverse.nl is behind bot protection, and a plain `Wget/1.21.2`
User-Agent is let through on the API: `https://dataverse.nl/api/access/datafile/615664`.

Do not use as a source:

* **The newer Linked Art build** (`data.globalise.huygens.knaw.nl/hdl:20.500.14722/place:{ID}`, 3,251 ids).
  It states no licence, and its conversion merges each record's per-name sources into one list.
* **`globalise-refdata.huc.knaw.nl/api/places`**: 401 without a GLOBALISE login.
* **The NeRu AnnoRepo annotations** (20,442; only 113 link a label to a place). Cite them by
  URI from PLATO citations instead of converting them.

## Converters and result

```
python3 glob2plato.py  forward places_v3.xlsx out.json ; ... compare places_v3.xlsx <roundtripped>
python3 neru2plato.py  forward <neru csv dir> out.json ; inverse <roundtripped> <dir> ; compare <neru csv dir> <dir>
python3 gavoc2plato.py forward gavoc-atlas-index.csv out.json ; compare gavoc-atlas-index.csv <roundtripped>
```

Forward → `plato_run.mjs convert ntriples` → `convert plato-jsonl` → inverse → cell diff:
**0 differences** in 88,849 / 9,605 / 99,756 non-empty cells, and negative controls fail.

What the zero depends on:

* **Normalisation:** only surrounding whitespace is stripped.
* **Left out (spreadsheet artefacts):** blank rows, `TEST_*` formula errors, lookup display
  columns, and a `CHECKED?` that is FALSE on every row.
* **GLOBALISE Sheet 2 is derived:** it is an overview of sheets 3–6. Its alt labels and types
  are derivable from those sheets; its parent region is not, in 5 rows.

## Mapping decisions (provisional; several need the contributor)

* One attestation per source row. The attestation `@id` carries sheet and row, so the inverse
  knows where each fact came from, including attestations that have no facet.
* **Citations.** Packed cells such as "(Coolhaas 1979, 24, 183; van Goor 2004, 202)" become one
  citation per source, each with its own locator, whenever re-joining gives back the exact
  string (16,289 of 16,904 cells). The rest stay whole.
* **Remarks that carry their own source** become meta-attestations on the fact they qualify,
  typed with a project term: PLATO has no neutral "remark" meta type.
* **Record identifiers** (GLOB_…) go in a `dcterms:identifier` PropertyValue. PLATO has no
  slot for them.
* **A latitude with no longitude** (Generale Missiven; 5 + 4 places) becomes a PropertyValue,
  not a geometry.
* **certain/uncertain** becomes 1.0/0.5, with the word kept in `certaintyNote`. The numbers are
  invented; only the word is data.
* **NeRu PREF_LABEL** gets form status `Normalised` in NeRu and `Headword` in GLOBALISE; neither
  fits exactly.
* **Identity links** are all `closeMatch`, because the source states no strength. AMH, ESTA and
  `NEW_ADMIN_*` have no resolvable URIs, so placeholder namespaces are used.
* **NeRu CCODES** holds country *names* ("India"). PLATO's `ccodes` expects ISO alpha-2 codes,
  so map before using it.

The gaps above were reported to the PLATO repo on 27 September 2026 for fixing or filing.
