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

Forward → `plato_run.mjs convert ntriples` → `convert plato-jsonl` → inverse → compare, against
PLATO 9d2c36e+ and plato-tools 0007a24+: **0 differences** in 88,849 / 9,605 / 99,756 non-empty
cells, and **0 disagreements** between the two carriers of any value carried twice (a
coordinate's text and its numbers, a type's label and sourceLabel, a relation's wording and its
type, a place's label and its preferred name) or asserted by the conversion itself (dataset title
and licence, namespace, remark type and target, the atlas citation).

**Controls** (`../controls.py`): corrupting one value of every predicate in the graph makes the
check fail for 29 of 30 (GLOBALISE: 28 of 29; GAVOC: 14 of 15) predicates. The one that does not
is `rdf:rest`: pointing a list's terminator at an unknown IRI leaves plato-tools' reading of the
list unchanged, so no comparison downstream can see it.

What the zero depends on:

* **Normalisations:** surrounding whitespace is stripped. Numbers are equal to 15 significant
  digits, the precision any decimal → double → decimal round trip keeps. (JSON-LD writes
  xsd:double to 16 significant digits, which turns the workbook's `106.82041100000001` into
  `106.820411`.) GLOBALISE `ccodes` are compared as lists, because the workbook separates codes
  with `,`, `, ` or `|`. GAVOC treats an empty cell and `-` alike (7 cells).
* **Left out (spreadsheet artefacts):** blank rows, `TEST_*` formula errors, lookup display
  columns, and a `CHECKED?` that is FALSE on every row.
* **GLOBALISE Sheet 2 is derived:** it is an overview of sheets 3–6. Its alt labels and types
  are derivable from those sheets; its parent region is not, in 5 rows.

## Mapping decisions (provisional; several need the contributor)

* One attestation per source row. The attestation `@id` carries sheet and row, so the inverse
  knows where each fact came from, including attestations that have no facet.
* **Record identifiers** (GLOB_…, NR_…) → `entityIdentifier`, with `namespace` `globalise` / `neru`.
* **Citations.** Packed cells such as "(Coolhaas 1979, 24, 183; van Goor 2004, 202)" become one
  citation per source, each with its own locator, whenever re-joining gives back the exact
  string (16,289 of 16,904 cells). The rest stay whole.
* **Remarks that carry their own source** → a meta-attestation on the fact it qualifies,
  `plato:Annotates`, with the remark in its notes.
* **Coordinates:** the source's text goes in the WKT (GLOBALISE, NeRu) or in the geometry's
  `sourceLabel` (GAVOC), and the numbers in `reprPoint`/`geojson`. The 42 GAVOC strings that
  cannot be parsed (`??`, `03-47S/13038E`) become a geometry with only its `sourceLabel`.
* **A latitude with no longitude** (Generale Missiven; 5 + 4 places) → a PropertyValue with
  `wgs84_pos#lat`, not a geometry.
* **certain/uncertain** → `certaintyLevel` `plato:Certain` / `plato:Uncertain`. No number is
  invented.
* **PREF labels** → `plato:Preferred`, the contributing project's display form.
* **GAVOC present names** are printed in the cited index, so they are `Attested`. The `/present`
  `@id` is what tells them from the name on the map.
* **Identity links** → `identityType: unspecified`, because the source states no strength. AMH,
  ESTA and `NEW_ADMIN_*` have no resolvable URIs, so placeholder namespaces are used.
* **Country codes:** NeRu CCODES holds country *names* ("India"), mapped to `ccodes` `IN`.
  GLOBALISE codes are alpha-2 already.
* **Still a workaround:** NeRu's single `annotated` workflow flag (1 cell) is a PropertyValue,
  which makes it read as a claim about the place.

The PLATO gaps this assessment found were resolved in PLATO 9d2c36e (27 September 2026); the
converters above use the new terms.
