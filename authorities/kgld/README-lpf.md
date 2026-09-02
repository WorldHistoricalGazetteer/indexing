# KGLD → LPF: what was built, and the decisions inside it

Generated 2 September 2026 by `build_lpf.py` from the published Zenodo package
(`kyrgyzstan_lakes_dataset_v1.0.0.zip`, SHA-256
`524cb43f5910d0be249c0c2929e82d933ff95f9ea225db40924b2dc97273251e`) and its Zenodo API
record (`zenodo_record_22178862.json`).

**Both outputs are derived. Do not hand-edit them — re-run the builder.**

```
python3 authorities/kgld/build_lpf.py --validate
```

| File | What it is |
|---|---|
| `kgld_v1.0.0.lpf.json` | Canonical LPF FeatureCollection, **80 features** (71 lakes + 9 supraglacial), 145 KB, with `indexing` + `citation` provenance blocks. **The artefact to send.** |
| `kgld_v1.0.0_myd.csv` | Flat one-row-per-record form, **80 rows**, 19 columns. Fallback only — it carries no names and no types. |

Both carry the same data.

✅ **Send the LPF. It round-trips fully as of whg3 `ab0441c49` (production, 2 Sep),
re-verified on the current build of this file — including `links[]`, which were read but
silently dropped on export until [place#228](https://github.com/WorldHistoricalGazetteer/place/issues/228)
(147 in → 0 out; now 147 → 147, with `certainty` and `citations` intact).**

```
40 name variants · 71 rows typed · 79 type assignments · 8 rows with BOTH
   lakes (bodies of water) + intermittent lakes
5 dates from `when` · 8 coordinates · citation: Ethan Hamilton, the Zenodo DOI, description
`observation_years` → role `other`, 49 rows, values intact, claimed by no hint
validation → missing when (66) · missing geometry (63)
```

The dual typing survives import, so the intermittent-lake distinction is preserved
end-to-end, and the imported type labels read as the AAT terms rather than KGLD's — the
`label` fix is visible in the running tool.

This flipped twice in one day and the history is worth keeping, because the middle state is
what a reader would otherwise assume still holds. When the files were built, MyD's
`fromJSON()` returned each feature's `properties` and nothing else, so an LPF import
discarded names, types, `when`, links, descriptions and both provenance blocks —
[place#224](https://github.com/WorldHistoricalGazetteer/place/issues/224). For a few hours
the CSV was the only usable vehicle. #224 is now fixed and verified by importing *this
file* through the production tool:

| | before #224 | now |
|---|---|---|
| Russian toponyms | gone | **40 kept**, in `alt_names` |
| AAT types | gone | **71 rows typed** |
| feature-level `when` | gone | **5 dates read** |
| creator / DOI | gone | **Ethan Hamilton** / the Zenodo DOI |
| citation title | the filename | the real title |
| coordinates | 0 of 71 | **8 of 71** |

The import summary now says what it kept — *"kept 40 name variants from `names`; assigned
place types to 71 rows from `types`; read 5 dates from `when`"* — and validation's date
bullet reads **66 of 71**, exactly our 71 minus the 5 the file dates itself.

**The CSV is now a fallback, not the route.** It carries no names and no types, so the LPF
is strictly better.

### Dataset-level provenance: both metadata blocks, from the Zenodo record

The FeatureCollection carries the two blocks MyD itself writes at the top of every
exported or contributed LPF, here populated from `zenodo_record_22178862.json` (the REST
API response, kept in this folder) rather than from a browser form:

* **`indexing`** — schema.org `Dataset`. **This is the one WHG's ingest actually reads**
  (`validation.views.extract_dataset_metadata`): `creator` → `Dataset.creator`, `name` →
  title, `description` → description, `url` → webpage, `citation` → `Dataset.citation`.
* **`citation`** — CSL-JSON, the format `lpf_v2.0.jsonld` natively `$ref`s. Not consumed
  on ingest yet; carried so it can be.

Both sit **ahead of** `features` so the server's streaming `ijson` reader finds them
without walking the array. Simulating the ingest read against our file gives:

```
creator     Ethan Hamilton
title       Kyrgyzstan Lakes Dataset: Morphometry, Geography, Hydrology, …
webpage     https://doi.org/10.5281/zenodo.22178862
citation    Hamilton, Ethan (2026). Kyrgyzstan Lakes Dataset … (Version 1.0.0)
            [Data set]. Zenodo. https://doi.org/10.5281/zenodo.22178862   (204 chars, cap 2044)
```

✅ **`description` now round-trips too** — [place#227](https://github.com/WorldHistoricalGazetteer/place/issues/227).
When this file was first built, `extract_dataset_metadata` read a `description` that
`schemaOrgDataset` never emitted, so every MyD-contributed dataset landed with a blank
description on its public page. A description field was added; then re-importing *this*
file showed MyD reporting "no description" from a file that carries one — the citation
seeding predated the new field and nothing connected them, so MyD wrote `description` on
export and dropped it coming back. Both halves are fixed, and MyD's contract harness now
checks the direction it was missing: every `CITE_FIELDS` entry must be recoverable by
`lpfCitation`, proven by reverting the fix.

⚠️ **`--validate` must register the CSL schema locally or it cannot run at all.**
`lpf_v2.0.jsonld` `$ref`s csl-citation.json by absolute `$id`, and
`https://whgazetteer.org/schema/csl-citation.json` returns **403** — the copy is published
at `/static/`, not `/schema/`. With a top-level `citation` present, a naive validator
raises `Unresolvable` rather than returning errors. The builder registers the local file
under its `$id`, exactly as `recon-validate.js` does in the browser. Server-side the
question never arises: `validation/tasks.py` validates *feature batches*, so it never
dereferences a top-level ref.

The citation block is control-tested, not merely passed:

| Control | Result |
|---|---|
| `citation` missing `schema` | **FAIL** ✅ |
| `itemData` missing `type` | **FAIL** ✅ |
| bogus key in `itemData` (`additionalProperties: false`) | **FAIL** ✅ |
| author without `family` (WHG's CSL requires it) | **FAIL** ✅ |

---

```
features   : 71  (2 excluded)
  located  :  8   ← the gap the trial exists to close
  dated    :  5   ← feature-level `when`, evidence-backed only
  described: 62
  toponyms : 111
schema     : PASS against whg3 validation/static/lpf_v2.0.jsonld (Draft7)
```

---

## 🛑 The file no longer validates — 66 of 71 fail, and that is correct

**Since [place#221](https://github.com/WorldHistoricalGazetteer/place/issues/221) shipped
(2 Sep), `build_lpf.py --validate` reports 66 errors and should.** The builder says so in
words rather than leaving a future reader to guess:

```
schema: 66 error(s) — EXPECTED, and exactly the 66 undated features (place#221).
        Not a regression: these lakes carry no `when` because no source dates them.
```

Until #221, the schema's temporal requirement was **vacuously satisfiable** — a feature
merely *lacking* `relations` passed that branch for free — so the same file returned PASS.
The table below is the evidence that was used to argue for #221, and row **E is now simply
the behaviour**:

| # | Case | Result |
|---|---|---|
| A | The file exactly as delivered | **PASS** |
| B | Control — `names` emptied on one feature | **FAIL** ✅ the check discriminates |
| B2 | Control — `types` removed from one feature | **FAIL** ✅ |
| C | One **undated** lake given a container relation | **FAIL** |
| D | The same feature, plus a **geometry-level `when`** | **PASS** |
| E | **All 66** undated lakes given container relations | **FAIL — 66 errors** |

The controls matter: without B and B2 a PASS would prove only that the validator was
running, not that it could tell good from bad
(see [[feedback_measure_must_discriminate]] in the working memory).

**What changed, and why it is an improvement.** Before #221, those 66 passed *by accident*
and would have failed the moment the contributor reconciled the `oblast` column — the
natural thing to do, and what the column is there for. Now they fail immediately,
uniformly, and for a reason that can be explained in a sentence. A harder first message,
but an honest one, and `whg3-17` confirmed the new behaviour on the production build:

```
validate KGLD from production   →  FAIL, 66 of 71 (the undated ones)
control: same file, all dated    →  PASS
```

The route out is unchanged and is row D: a **geometry-level `when`**, via the
`Geometry captured (date)` role from
[place#220](https://github.com/WorldHistoricalGazetteer/place/issues/220). The contributor
supplies it as he places each lake, so the same action that fixes the geometry gate fixes
the temporal one.

⚠️ **This must be in the covering letter, not discovered.** He will import his own data and
be told that 66 of 71 records are invalid. That needs to arrive as an explanation rather
than as a verdict on his dataset.

---

## Decisions inside the conversion

### 80 features: 71 of the 73 lakes, plus the 9 supraglacial features

`KGLD-L0011` (legacy ambiguous Merzbacher record) and `KGLD-L0069` (unresolved Kulun FAO
inventory row) are excluded. Both are `record_status = disputed` provenance placeholders
whose *titles read as place names*, which is exactly why indexing them would be wrong.
Ethan is asked to confirm this in the covering letter; if he objects, the exclusion is one
constant in `build_lpf.py` (`EXCLUDE`).

The 9 `KGLD-F####` study-local supraglacial features were never candidates — the source's
own `identity_status` declines to promote them to geographic entities.

### No `relations`, despite having the data

`admin1`/`admin2` are oblast and district *names*, and LPF's `relationTo` must be a URL or
a namespace term. Emitting `"relationTo": "Issyk-Kul"` would be invalid, and inventing an
identifier for it would be worse. They stay as properties and as the `oblast`/`district`
columns so the contributor can reconcile them in MyD — which is what a container column is
for, and which is also what triggers case C above.

### `when` only where the evidence supports it — 5 features

65 of 73 lakes are undated physical geography. The short-lived glacial lakes are not: they
formed and drained on record, so a lifespan is a real claim about the place. Those five
(`L0027` Zyndan Western, `L0070` Kashkasuu, `L0071` Jeruy, `L0072` Karateke, `L0073`
Toguz-Bulak) get a feature-level `when` bounded by their observation and event dates,
marked `certainty: less-certain` and labelled *"documented formation/drainage episode"*.

Publication year is never substituted for observation year — that is KGLD's methodology §5
and it is the right rule.

**Everything else is left undated on purpose.** Emitting a date for a permanent lake would
be the precise misrepresentation this entire exercise has been avoiding.

### Types: prefilled, because AAT offers essentially one choice

Every feature carries `aat:300008680` *lakes (bodies of water)*. Eight also carry
`aat:300387086` *intermittent lakes*.

This is not a shrug — it is what the vocabulary actually contains. Measured against the
**live** `types` index (`types_20260404_150351`) on 2 Sep 2026, the entire lake branch is:

| under `aat:300008680` | elsewhere |
|---|---|
| `300008682` oxbow lakes | `300263360` artificial lakes |
| `300132303` tarns | `300266556` salt lakes |
| `300266561` crater lakes | `300387087` dry lakes |
| `300387021` underground lakes | `300132301` lacustrine bodies of water *(the parent)* |
| `300387086` intermittent lakes | |

**AAT has no concept for a glacial lake of any kind.** Nothing corresponds to KGLD's
`moraine_dammed_glacial`, `riegel`, `ice_dammed`, `landslide_dammed`,
`proglacial_glacier_contact` or `intramorainic`. So for almost every record there is
exactly one applicable concept, there is nothing for a contributor to choose between, and
leaving the field for him to fill would have been busywork with one right answer.

The one honest refinement is applied automatically: **8 lakes** whose KGLD classification
says outright that they are not permanent — `permanence: non_permanent | cyclic_seasonal`,
`lake_behavior: short_lived | seasonally_recurrent`,
`hydrologic_variability: strongly_variable_intermittent` — also get
`aat:300387086` *intermittent lakes*. That is the source's own assertion, not our inference.

**The 9 supraglacial features carry the same pair, and it is the best AAT can do.**
Checked against the live types index on 2 Sep, with scope notes:

| concept | scope note | verdict |
|---|---|---|
| `300008680` lakes | *"bodies of fresh or salt water **surrounded by land**"* | these are surrounded by **ice** |
| `300132301` lacustrine bodies of water | *"**depressions in the earth** filled with water"* | same problem, one level up |
| `300008688` ponds (water) | *"relatively small bodies of water, **usually** surrounded on all sides by land"* | looser, but we have no areas for these 9, so a size judgement would be invention |
| `300008835` glaciers | *"very large bodies of **ice**…"* | the ice, not the water on it |
| `300008832` glacial landforms | *"landforms resulting from glaciers"* | a lake is not a landform |
| **`300387086` intermittent lakes** | *"lakes that appear at intervals, generally with **predictable cycles**"* | ✅ **exactly right** |

**There is no supraglacial-lake concept in AAT, and its lake definition positively excludes
ice-bound water.** That is a genuine vocabulary gap, not a lookup failure. `300387086` is
the concept that fits on its own terms; `300008680` is retained so these records sit in the
same hierarchy as the other 71; and the precise term rides in
`sourceLabels: [{"label": "supraglacial_lake"}]`, which is what `sourceLabels` is for.

Worth passing to whoever next works the type system: it is a real hole in the mapped
vocabulary, and glacial-lake genesis (moraine-dammed, riegel, ice-dammed, proglacial) is
the same hole one level down.

Two more are deliberately **not** assigned, though a case exists for each:

- `300132303` **tarns** — a tarn is specifically a cirque lake, and KGLD's categories
  (moraine-dammed, riegel, ice-dammed) are not the same thing.
- `300266556` **salt lakes** — Issyk-Kul is brackish, but KGLD does not classify it as
  saline and we do not add facts the source withholds.

Both are good questions to put to the contributor inside MyD, where he can see the
hierarchy. They are the only two genuinely open type questions in the dataset.

⚠️ **Correction, 2 Sep:** an earlier build emitted `{"identifier": "aat:300008680",
"label": "tectonic lake"}` — KGLD's term against AAT's id, which **mislabels the
concept**. `label` is now always the AAT term for `identifier`, and KGLD's richer
vocabulary lives entirely in `sourceLabels[]` where it belongs. ANALYSIS.md's earlier
caveat that the AAT ids were "read out of `typesystem/data/*.json`, not confirmed against
the live types index" is now closed: the earlier query returned 0 because the field is
`term`, not `prefLabel`.

### Geometry: 8 points, each cited, each carrying the source's own tolerance

Coordinates carry a `citations[]` naming the registered source that supports them,
`certainty` (`less-certain` at ≥ 1 km, `certain` below), and — since
[place#229](https://github.com/WorldHistoricalGazetteer/place/issues/229) —
an **`approximation`** carrying KGLD's own `coordinate_precision_m` as a
`geo:hasSpatialAccuracy` tolerance. LPF wants kilometres and KGLD records metres, so the
source's own figure divides straight in, unbucketed:

```
Issyk-Kul 5 km · Son-Kul 3 · Chatyr-Kul 3 · Kel-Suu 2
Sary-Chelek 0.5 · Kulun 0.5 · Ala-Kul 0.1 · Zyndan Western 0.1
```

KGLD methodology §12 requires that coordinate precision be retained; before #229 it
survived only as the coarse `certainty` above — and `certainty` could not express it
anyway, since 0.1 km and 0.5 km both land on `certain`, losing a fivefold difference. The
two now say different things: `certainty` how sure, `approximation` how close.
(⚠️ MyD dropped all eight tolerances on round-trip until
[place#231](https://github.com/WorldHistoricalGazetteer/place/issues/231) — a coordinate
whose precision has been silently dropped does not look damaged, it looks exact.) This mirrors what MyD writes for cloned and
drawn geometry, so the file and the tool speak the same language.

The other 63 features have `geometry: null`, which the schema explicitly permits
(`geometry.oneOf` includes `{"type": "null"}`).

### Morphometry as prose, in `descriptions[]` — 62 features

Area, elevation, maximum depth and volume from the audited `reference_public` table, each
with its source named, plus a conflict note where KGLD flags one. This is a summary with
attribution, **not a pretence of structured data** — the places schema has no numeric
attribute bag, and the evidence layer (166 measurements, comparability groups, the conflict
register, the audit statuses) stays at Zenodo where it is properly modelled. See ANALYSIS.md,
"Fields with no home in the `places` schema".

### `observation_years` — a lookup, not a claim

`properties.observation_years` (and a CSV column of the same name) lists the distinct years
in which KGLD observed each lake, drawn from `measurements.observation_year` /
`observation_start` and `observations.observation_date`. Event years are excluded: for the
5 lakes that have them they are already the feature-level `when`, and a drainage date is
not a candidate capture date.

**It makes no claim about the place.** Its only purpose is the `Geometry captured (date)`
the contributor is asked for when he places a lake — so he reads a candidate year off his
own data instead of inventing one.

It is deliberately **not** promoted to a feature-level `when`; see ANALYSIS.md, "Why only 5
features carry a `when`". The short version: these are attestations, and WHG's ingest
collapses any timespan to `[min, max]` where `minmax` drives the map's temporal filter, so
"measured 1911–2009" would make Petrov Lake vanish from today's map.

⚠️ **58 of 80 features carry a year, but 38 carry only 2025 or 2026** — the year the lake
was listed in the MCHS catalogue, which is not an observation of its shape. **The field is
genuinely useful for 20 records**: 11 lakes (of which only three offer a real series —
Petrov 1911–2009, Adygine 2 2005–2017, Adygine 3 2007–2017) and all 9 supraglacial
features, whose dates are day-precise. The catalogue years are kept rather than filtered,
because seeing "my only dated observation here is the catalogue entry" is useful in
itself — but the contributor has to be told, or he will read `2026` as a capture date.

### `@id`

`https://doi.org/10.5281/zenodo.22178862#KGLD-L0001` — resolvable, unambiguous, and it
does not squat a `kgld:` namespace prefix that WHG has not registered. The bare KGLD id is
also kept in `properties.kgld_id` and as the CSV's first column, so the join back to the
source package is one field either way.

---

## Known limits

- **111 toponyms, not 114.** Three name rows duplicate a canonical form already emitted;
  they are deduplicated per lake on the exact string.
- **No Kyrgyz names**, because the source has none. No conversion can fix that; it is a
  question for the contributor.
- **`is_preferred` is unusable** — it is `TRUE` on both the English and the Russian name
  for all 41 two-name lakes. `title` is taken from `canonical_name` in the entity table
  instead, and every name row becomes a toponym.
- **The AAT refinements are coarse** (above).
- **LPF import fidelity: measured, and bad** — place#224, above. The CSV is the working
  path until it is fixed.
- ✅ **The CSV run is done (dev, 2 Sep) — see "What the CSV run measured" below.** The
  paragraph that follows describes what was expected; it held.
- **The CSV dodges both `place#225` faults by construction.** MyD's coords-role hint is unanchored and claims any header containing
  "coord", so the LPF's `coordinate_method` / `coordinate_precision_m` properties were
  guessed as coordinate columns and — because `rowCoordValue` prefers a coords column
  exclusively — **suppressed every coordinate in the dataset** (`0 of 71` parsed; setting
  them to "other" gave `8 of 71`). The CSV has no `coordinate_*` header at all. Separately,
  MyD's country hint matches `ccode` and not LPF's own `ccodes`, and stringifies array
  values to `'["KG"]'` which `isCcode` rejects; the CSV uses a singular `ccode` column
  holding the plain string `KG`. Both confirmed safe by the CSV run below.
- ⚠️ **`aat_type` is not parsed as an identifier** — but it *is* the grouping key that makes
  typing two clicks rather than seventy-one, and the per-row modal is the route to use, not
  Scope → What. Measured; see "The prefilled AAT id does not set the type" below.

---

## What the CSV run measured (dev, 2 Sep)

| Check | Result |
|---|---|
| Coordinates parse | ✅ **8 of 71** — no `coordinate_*` header, so place#225(a) does not bite |
| `ccode` = `KG` detected as country role | ✅ and it **fixes the containment entirely** |
| `aat_type` read as a place type | 🛑 **No** |

The `ccode` contrast against the LPF is the clearest evidence for place#225(b):

| | LPF (`ccodes` → `'["KG"]'`) | CSV (`ccode` → `KG`) |
|---|---|---|
| `Naryn` resolves to | Naryn, **Kazakhstan** | Naryn, **Kyrgyzstan** ✓ |
| needing review | **14** | **0** |
| auto-confirmed | 56 | **70** |

### 🛑 The prefilled AAT id does not set the type

`buildLPF` reads types only from `project.rowTypes` (written **solely** by the per-row AAT
modal), from Scope → What, or from an enriched match. Setting the `aat_type` column to the
`type` role leaves `rowTypes` empty and validation still reports *71 of 71 places have no
place type*. **The column's contents are never parsed as identifiers.**

So the prefill supplies no identifiers to the exporter, by either route — dropped outright
from the LPF (place#224), never parsed in the CSV.

### ✅ …but the column is what makes typing two clicks instead of seventy-one

Measured on dev after the above. A `type`-role column drives the per-row modal's **"apply
to all rows where this column = X"** grouping, keyed on the **exact cell value**:

```
click a type cell  →  "Also apply to all N rows where this column = aat:300008680"
tick + Apply       →  project.rowTypes = N entries
```

Our column holds **two** distinct values, so it is two actions, not one (measured at 71
features; at 80 the split is 63 / 17, the 9 supraglacial features being intermittent too):

```
63 rows   aat:300008680
17 rows   aat:300008680;aat:300387086
```

**And that is the point, not a shortfall.** A single dataset-wide **Scope → What** pick
applies one set of types to all 71 and would **flatten the intermittent-lake
distinction** — the one piece of the typing that carries real information from the source.
The per-row route preserves it: pick *lacustrine bodies of water* once for the 63, then the
pair once for the 8.

⚠️ **So the earlier instruction in this file — "set the type through Scope → What" — was
wrong for this dataset and has been replaced.** Scope → What is simpler and lossy here.

✅ **On the LPF path this is now moot.** Since #224, types come straight off the file's
`types[]` into `project.rowTypes` — all 71 rows typed on import, intermittent distinction
intact, nothing to set by hand. The apply-to-all grouping remains if he wants to *regroup*,
but he no longer has to use it. The same goes for the citation: it survives import, so he
does not retype title, year, DOI or authorship, and `admin1` was already detected as a
container.

The two instructions below apply only to the **CSV fallback**:

1. **Set `aat_type`'s role to *Feature type* by hand** — not auto-detected, and the "apply
   to all" option only exists once the column has the type role.
2. **Ignore the Step 2 nudge**, which keeps reading *"No place-type column detected"* after
   the role is set. Cosmetic, but it says the opposite of the truth at the moment he most
   needs reassurance.

### Two column hints that do not fire

- ✅ **`oblast` is now recognised as a container** (fixed with #224). It was not: the hint
  covered `county|region|province|state|district` with no Central Asian terms (`oblast`,
  `viloyat`, `aimag`, `velayat`), and it is the column that matters — 70 of 71 rows carry
  it. On the LPF path it arrives as `admin1`, which was always detected.
- **`aat_type` is not guessed as the type role** either.

Both are on place#224. They are the mirror image of place#225(a): there the coords hint was
**unanchored and claimed too much**; here the hints are **anchored and claim too little**.
Same family, opposite failure, and nothing tests either — which is the argument for a guard
rather than a third patch.
