# KGLD Authority Analysis — Kyrgyzstan Lakes Dataset

**Everything below was written and measured on 2 September 2026.** The document grew
through that day as findings landed, so a few passages record a position that was later
corrected — those are marked ⚠️ or ✅ in place rather than deleted, because the middle
state is what a reader would otherwise assume still holds. **This block is the current
state; trust it over anything further down that disagrees.**

## ⏱️ Where this stands

| | |
|---|---|
| **Verdict** | Worth taking. Small, exceptionally well-curated, and ~70 of its 71 lakes are absent from WHG. |
| **Route** | Ethan runs it through **Map your Data** himself; Contribute posts an LPF to `/datasets/validate/` and it lands as a `whg:` `authority=True` dataset. Not a new authority namespace — see "Recommended route". |
| **Deliverables** | ✅ Built and re-tested through the production tool. See the table below. |
| **whg3 blockers** | ✅ **All closed** — place#220–#231, production `04019e8e5`. |
| **🛑 Blocking** | 1. The re-ingestion campaign ([`plan-completion-2026-08-31.md`](../../developer/plan-completion-2026-08-31.md)) — SG's standing ruling, corpus correctness first.<br>2. **Ethan is not a registered WHG user**; he needs an account, then `can_access_beta`. |
| **Next action** | Neither blocker is an agent's to move. When both clear: send `questions-for-ethan.txt` (read its header first — it carries send-gate notes and one instruction to reorder a paragraph). |

## 📁 What is in this folder

| File | |
|---|---|
| `ANALYSIS.md` | this document — the assessment |
| `README-lpf.md` | **the conversion's decisions and limits. Read before touching `build_lpf.py`.** |
| `questions-for-ethan.txt` | draft covering letter, unwrapped paragraphs, **DO NOT SEND** header |
| `build_lpf.py` | builds both deliverables from the zip. `--validate` expects **66 errors** and says so |
| `kgld_v1.0.0.lpf.json` | **80 features** · sha `ed9deda2bf09ada6` · the artefact to send |
| `kgld_v1.0.0_myd.csv` | 80 rows · sha `85ef661292e5bf42` · fallback; carries no names or types |
| `kyrgyzstan_lakes_dataset_v1.0.0.zip` | the Zenodo package, sha `524cb43f5910d0be…` |
| `zenodo_record_22178862.json` | the REST record, for the citation blocks |

## 🔢 The file at a glance

```
80 features = 71 lakes + 9 supraglacial      129 toponyms (40 Cyrillic)
 8 located, each with a tolerance in km      80 typed, 17 dual-typed
14 dated                                      2 closeMatch + 3 Ramsar links
70 described                                 indexing + citation provenance blocks
```

⚠️ **`--validate` reports 66 errors and that is correct** — the 66 undated lakes, since
place#221 closed a vacuous schema branch. Not a regression; the route out is a capture date
on each geometry, which Ethan supplies as he places them. The builder prints this in words.

## ✍️ Three things a future session should not re-derive

1. **The dataset's own numbers are right; my first readings of them were not.** Where a
   count in this document has been corrected, the correction is marked. Notably:
   external identifiers are **not** absent (2 pre-existing reconciliations), the useful
   `observation_years` are **11 lakes not 45**, and the 9 supraglacial features are **in**.
2. **Do not "fix" the empty `minmax` on any boundary-shaped dataset** that arrives with
   only geometry-level dates. See place#222 — it is by design.
3. **Do not assert `parent_glacier`.** Two GeoNames records are both titled plainly
   "Inylchek Glacier", separated by latitude alone. The letter asks Ethan which.

---

## Source

**Dataset:** Kyrgyzstan Lakes Dataset (KGLD) v1.0.0 — *Morphometry, Geography, Hydrology,
Glacial-Lake Dynamics and Source Provenance*

| | |
|---|---|
| Creator | Ethan Hamilton (no institutional affiliation stated) |
| DOI | [`10.5281/zenodo.22178862`](https://doi.org/10.5281/zenodo.22178862) (concept DOI `10.5281/zenodo.22178861`) |
| Published | 2026-08-30 |
| Licence | CC BY 4.0 on KGLD-original material; third-party rights explicitly carved out |
| Documentation | https://kyrgyzstanplanner.com/lakes-of-kyrgyzstan/ |
| Package | single 249 KB zip, 39 files (16 CSV + 1 GeoJSON + Frictionless descriptor + docs + xlsx) |
| Local copy | `authorities/kgld/kyrgyzstan_lakes_dataset_v1.0.0.zip` |
| SHA-256 (zip) | `524cb43f5910d0be249c0c2929e82d933ff95f9ea225db40924b2dc97273251e` |

Downloaded direct from the Zenodo API on 2 Sep 2026; the record has 1 file and 0 prior
downloads. Ethan has confirmed by email that the Zenodo release **is** the complete
dataset, not a trimmed subset — so there is no richer private version to request. There
*may* still be unpublished working material; see the questions file.

---

## Data volume

| Resource | Rows | WHG relevance |
|---|---|---|
| `data/kyrgyzstan_lakes.csv` | **73** | **Core** — the entity registry |
| `data/kyrgyzstan_lake_names.csv` | 114 | **Core** — toponyms |
| `data/kyrgyzstan_lakes.geojson` | **8** | **Spatial** — representative points, WGS84 |
| `data/kyrgyzstan_lake_measurements.csv` | 166 | Morphometry (65 lakes) |
| `data/kyrgyzstan_lake_classifications.csv` | 199 | **Types** + hazard/catalogue attributes |
| `data/kyrgyzstan_lake_reference_public.csv` | 63 | Audited flat view (the "safe" subset) |
| `data/kyrgyzstan_lake_reference.csv` | 73 | Full flat view incl. blank rows |
| `data/kyrgyzstan_glacial_lake_events.csv` | 11 | **Temporal** — GLOF / drainage events |
| `data/kyrgyzstan_lake_observations.csv` | 26 | **Temporal** — dated states (6 lakes) |
| `data/kyrgyzstan_lake_study_features.csv` | 9 | Study-local supraglacial features — *not places* |
| `metadata/kyrgyzstan_lake_sources.csv` | 37 | **Provenance** — source registry |
| `metadata/kyrgyzstan_lake_rights_matrix.csv` | 37 | Per-source reuse class |
| `curation/kyrgyzstan_lake_conflict_register.csv` | 16 | Explicit unresolved conflicts |
| `curation/kyrgyzstan_lake_reference_audit.csv` | 129 | Public-release audit per measurement |
| `data/kyrgyzstan_lake_reference_decisions.csv` | 19 | Logged curation decisions |
| `metadata/kyrgyzstan_lake_data_dictionary.csv` | 284 | Field-level documentation |

Everything is UTF-8 CSV **with a BOM** — read with `encoding="utf-8-sig"` or the first
column name comes through as `﻿lake_id`.

---

## Alignment with the WHG `places` index

### ✅ Place identity — stable, well-modelled

- 73 entities with permanent, never-recycled ids `KGLD-L0001` … `KGLD-L0073`.
- The methodology explicitly separates *entity* from *measurement* from *classification*,
  and states that ids survive renaming. This is exactly the identity model WHG wants and
  is better articulated than most contributed datasets.
- All 73 are `country_iso3 = KGZ` → `ccodes: ["KG"]` with no ambiguity.

### 🛑 Spatial coverage — **8 of 73 (11%)**. The blocker, and the point of the trial.

Only these entities carry a coordinate:

| lake_id | name | lat | lon | precision | method |
|---|---|---|---|---|---|
| KGLD-L0001 | Issyk-Kul | 42.416667 | 77.250000 | 5000 m | Ramsar site approximate centre |
| KGLD-L0002 | Son-Kul | 41.833056 | 75.116667 | 3000 m | Ramsar site approximate centre |
| KGLD-L0003 | Chatyr-Kul | 40.616667 | 75.300000 | 3000 m | Ramsar site approximate centre |
| KGLD-L0004 | Sary-Chelek | 41.890000 | 71.960000 | 500 m | published study-site coords |
| KGLD-L0005 | Kel-Suu | 40.643056 | 76.395000 | 2000 m | GNS gazetteer representative point |
| KGLD-L0006 | Kulun | 40.542511 | 74.313168 | 500 m | GeoNames representative point |
| KGLD-L0007 | Ala-Kul | 42.317167 | 78.534664 | 100 m | published study-site coords |
| KGLD-L0027 | Zyndan Western Lake | 41.943889 | 77.022222 | 100 m | published study-area coordinate |

**65 entities have no coordinate of any kind, and no polygon exists for any entity.**
`kyrgyzstan_lakes_gis.md` states the omission is deliberate: shoreline geometry for
glacial and seasonal lakes is observation-specific, and redistributing third-party
polygons risks licence incompatibility. Methodology §12 further requires that any
coordinate be source-traceable, which is why the 65 are blank rather than guessed.

That is defensible curation and a real problem for us. Consequences, stated plainly:

- 65 records would enter `places` with `geometries: []` — no map presence, no
  `repr_point`, no `bounds`, no `h3_cover`, and therefore **invisible to every spatial
  query** (`contained_in`, `bounds`, `containment=fuzzy|exact`) and absent from tiles.
- Neither `has_geom` nor `geom_class` applies. The standing defect predicate
  `geom_class ∈ {area,line} AND NOT has_geom` will not fire on them, so they will not
  register as an incomplete ingestion — they will just be quietly unfindable on the map.
  Anyone auditing later needs to know this was a **property of the source**, not a
  pipeline fault. Record it in the registry note.
- The gateway's "borrow a `sameAs` co-referent's polygon" path (`spatial.resolve_region`,
  `source="linked-polygon"`) cannot rescue them, because establishing that hard link
  needs either a coordinate or an external identifier — and they have neither.

There **is** precedent for staging coordinate-less entities: `authorities/dgsd/places.py`
does exactly this ("entities without coordinates are also staged for their toponym and
temporal value"), and 48% of DGSD is coordinate-less. So this is a known-acceptable
shape, not a novel one. It is a matter of degree: DGSD is 52% located, KGLD is 11%.

### ⚠️ Toponyms — 114 source rows, and **no Kyrgyz** (the file ships 129, incl. 9 coined)

| language | script | count | name_type |
|---|---|---|---|
| `en` | Latn | 35 | `canonical` |
| `en` | Latn | 37 | `canonical_transliteration` |
| `en` | Latn | 1 | `source_literal` |
| `ru` | Cyrl | 39 | `official_catalog` |
| `ru` | Cyrl | 2 | `reference_name` |

- Every lake has exactly one `en` Latin form; 41 also have a Russian Cyrillic form.
- **There are zero `ky` (Kyrgyz) names.** For a Kyrgyz national dataset in a gazetteer
  that values endonyms, that is the most conspicuous gap after the coordinates. The
  `notes` on the seed names say "local-language forms to be curated separately".
- 103 of 114 names carry a `source_id`; 95 are `review_status = verified`.
- **Data defect:** `is_preferred` is `TRUE` on *both* the English and the Russian name
  for all 41 two-name lakes — 41 lakes with two preferred names. The field is also
  inconsistently cased (`TRUE` ×102, `True` ×11, `FALSE` ×1). Whatever we do, do not
  drive a `title` choice off `is_preferred` without a rule; use `canonical_name` from
  `kyrgyzstan_lakes.csv` for `title` and treat all name rows as toponyms.
- Toponym ids would be `{name}@{lang}` per the house LST convention:
  `Issyk-Kul@en`, `Алакёль@ru`. The Cyrillic official-catalogue forms are the most
  valuable part of this dataset for the `toponyms` index (see novelty, below).

### ✅ Types — mappable, and genuinely interesting

`classifications.csv` holds 199 rows over 14 `property` values:

| property | rows | use |
|---|---|---|
| `monitoring_status` | 41 | attribute |
| `government_catalog_id` | 39 | **near-identifier** (MCHS catalogue `I-17`, `N-20`, …) |
| `mchs_lake_type` | 39 | **type** (`moraine_glacial`, `riegel`, …) |
| `hazard_category` | 39 | attribute (Roman numerals I–IV) |
| `origin` | 10 | **type** (`tectonic`, `moraine_dammed_glacial`, `landslide_dammed`, …) |
| `glacier_relation` | 7 | type qualifier |
| `lake_behavior`, `drainage_mechanism`, `outburst_susceptibility`, `permanence`, `hydrologic_variability`, `lake_system_role`, `entity_scope` | 22 | attributes |
| `ramsar_site_id` | 3 | **external identifier** → `links` |

Suggested `types[]` entries follow the house `{identifier, label, sourceLabel}` shape with
`label: "kgld"`. Every entity gets a baseline lake type; `origin` / `mchs_lake_type`
refine it. AAT ids already present in `typesystem/data/`:

| KGLD value | AAT | term |
|---|---|---|
| *(baseline, all entities)* | `300008680` | lakes (bodies of water) |
| `moraine_glacial`, `moraine_dammed_glacial`, `proglacial_glacier_contact` | `300008680` + `300008706` | (no distinct "glacial lakes" concept is in our vocab files — see below) |
| small high-mountain lakes | `300008706` | *lake* token used by `gb1900_os_abbrev.json` |
| — | `300266556` / `300263360` / `300387086` | salt / artificial / intermittent lakes |
| — | *"tarns"* | present in AAT term text; id not captured in our vocab files |

✅ **Resolved 2 Sep.** The earlier `prefLabel` query returned 0 because the field is
`term`, not `prefLabel` (`schemas/types.json`). Queried properly against the live
`types_20260404_150351`, the **entire** AAT lake branch is: `300008682` oxbow lakes,
`300132303` tarns, `300266561` crater lakes, `300387021` underground lakes, `300387086`
intermittent lakes — all under `300008680` — plus `300263360` artificial lakes,
`300266556` salt lakes, `300387087` dry lakes, and `300132301` lacustrine bodies of water
as the parent.

⚠️ **AAT's lake definition also positively excludes these 9.** `300008680` is *"bodies of
fresh or salt water **surrounded by land**"*; a supraglacial lake is surrounded by ice.
`300132301` (*"depressions in the earth"*), `300008688` ponds (*"usually … land"*),
`300008835` glaciers (the ice itself) and `300008832` glacial landforms (a lake is not a
landform) all miss too. **`300387086` intermittent lakes is the only concept whose scope
note actually fits** — *"lakes that appear at intervals, generally with predictable
cycles"*. It is assigned with `300008680` as the hierarchy baseline and
`supraglacial_lake` in `sourceLabels`. A real vocabulary gap, verified against the live
types index rather than inferred, and worth passing to the type-system work.

**AAT has no glacial-lake concept at all**, so nothing corresponds to KGLD's
`moraine_dammed_glacial`, `riegel`, `ice_dammed`, `landslide_dammed`,
`proglacial_glacier_contact` or `intramorainic`. There is one applicable concept for
almost every record, which is why the deliverable **prefills** the type rather than
leaving it as contributor work — see `README-lpf.md`, "Types: prefilled". The only two
open questions are `tarns` and `salt lakes`, both left for him deliberately.

### ✅ Temporal — small but high quality, and unusual

- 11 documented glacial-lake drainage / GLOF events, 1 with a fatality count (Zyndan
  Western, 24 July 2008, 3 deaths). Dates are exact day or a bounded range, with an
  explicit `date_precision` field — the range case maps cleanly onto the schema's
  `{earliest, latest}` timespan form rather than `{in}`.
- 26 dated observations across **5** lakes (2006–2019) plus 9 rows on study-local features, each with `presence_status`
  (`present` / `absent` / `residual`) and area. For the short-lived glacial lakes this is
  a genuine *existence* timespan, not a measurement footnote — L0070/L0071/L0072/L0073
  are lakes that formed and drained within a season.
- **Caveat:** 6 of the 11 events have an empty `lake_id` — they attach to
  `feature_ids` (the study-local supraglacial features), not to registry entities. Do
  not join events to lakes on `lake_id` without counting the misses.

This is a good fit for place#164's temporal model (attestations as lifespans), and it is
the one thing here that no other WHG authority provides for Central Asia.

#### Why only 5 features carry a `when` — the full dating audit

The LPF dates 5 features. That is not because the source dates only 5: **45 of 71 lakes
carry at least one dated observation.** The rest is deliberately unused, and the reason is
worth recording because it looks like an omission.

| Evidence | Lakes | What it dates |
|---|---|---|
| Drainage/outburst **events** + observation series | **5** | the lake's **existence** — these formed and drained on record |
| Historical measurement **series** | ~5 | Petrov 1911–2009 (13 rows, 7 years); Adygine 2 2005–2017; Adygine 3 2007–2017; single years for Issyk-Kul 1990, Son-Kul 2010, Sary-Chelek 2013 |
| **MCHS catalogue rows** dated 2025/2026 | ~30 | that the lake was *listed in a catalogue* contemporaneous with KGLD itself |
| undated | 26 | — |

Only the first group dates *the place*. The rest are **attestations** — someone measured or
listed the lake in year Y — and a lake measured in 1911 and 2009 existed before and after
both.

**Emitting those as a feature-level `when` would actively damage the records**, for the
same reason place#222 existed. `validation/create_dataset.py: reduce_timespan_to_years`
collapses any timespan to `[min year, max year]`, and `minmax` drives the map's temporal
filter (`utils/mapdata.py`). So:

- "catalogued 2026" → `minmax [2026, 2026]` → the lake vanishes from every window but 2026;
- "measured 1911–2009" → `[1911, 2009]` → **Petrov Lake disappears from today's map**, though it is still there.

There is no LPF idiom for *"extant, attested from X"* that survives that collapse: an open
`end` still yields `max` = the last start year. So the choice is a wrong date or no date,
and no date is right.

✅ **The evidence is not wasted, but it is worth less than it first looks.** It is now
surfaced per lake as `properties.observation_years` (and a CSV column of the same name) —
a lookup, not a claim, so the contributor reads a candidate capture date off his own data
instead of inventing one. `geometry.when` is where a dated observation legitimately belongs,
and place#222 guarantees it never reaches `minmax`.

⚠️ **But count it honestly: 58 of 80 features carry a year, and 38 of those carry *only*
2025 or 2026** — the year the lake was listed in the MCHS catalogue, which is not an
observation of its shape. **Just 11 lakes offer a year that is genuinely useful as a
capture date:**

```
L0001 Issyk-Kul     1990          L0027 Zyndan Western   2008
L0002 Son-Kul       2010          L0070 Kashkasuu        2006
L0004 Sary-Chelek   2013          L0071 Jeruy            2013
L0010 Petrov Lake   1911,1947,1957,1980,1995,2006,2009
L0024 Adygine 2     2005,2008,2015,2017                L0072 Karateke   2014
L0025 Adygine 3     2007,2012,2017                     L0073 Toguz-Bulak 2019
```

So the field helps with **20 of the 72 records he has to place** — 11 lakes and all 9
supraglacial features, whose dates are day-precise — not the 45 I first briefed. The 2025/2026 rows
are kept rather than filtered because hiding them would be worse — he can see at a glance
that his only dated observation for that lake is a catalogue entry — but the walkthrough
must say so, or he will read `2026` as a capture date and it is not one.

⚠️ **Not prefilled, on purpose.** The 8 existing coordinates come from *coordinate* sources
(Ramsar 2025, GeoNames, published studies), whose dates are publication or access dates,
not survey dates. KGLD methodology §5 forbids substituting publication year for observation
year, and prefilling `geometry_captured` from them would break his own rule in his own
file.

### ⚠️ Links / external concordance — **two, and I first reported none**

There is no `wikidata_id`, `geonames_id`, `osm_id`, or any other concordance column
anywhere in the 16 CSVs. I grepped every header and every cell.

⚠️ **But my first reading of that was wrong, and SG caught it.** I wrote that Wikidata
Q13642455 and GeoNames appear "only as citations for a coordinate … which is a different
thing". It is not a different thing. **Taking a gazetteer record's coordinate as your
lake's own is an identity assertion** — you cannot do it without having judged that the
record denotes the same lake. So two lakes here are *already reconciled*, and reading them
as bibliography would have shipped them looking unreconciled:

| lake | link | basis |
|---|---|---|
| `L0005` Kel-Suu | `wd:Q13642455` | source `S0019` is titled *"Köl-Suu (Wikidata Q13642455…)"* and supplies the coordinate **and** the elevation |
| `L0006` Kulun | `gn:8403583` | source `S0021` cites a GeoNames *search page* but adopts the point `40.542511,74.313168`; verified 2 Sep against the live index — `gn:8403583` "Ozero Kulun" is the only GeoNames record within 200 m |

Both are now emitted as `closeMatch`, `certainty: less-certain`, each with a citation
stating the basis — less-certain because the inference is **ours**: he never wrote
`sameAs`. The covering letter asks him to confirm or correct both, and whether he holds
others resolved the same way.

The three `ramsar_site_id` classifications also become resolvable
`https://rsis.ramsar.org/ris/<id>` links — but as **`seeAlso`, never `closeMatch`**, because
his own source note says a Ramsar site boundary may take in terrestrial protected area
beyond the water. A Ramsar site is not the lake.

The only usable `links[]` material is:
- 3 `ramsar_site_id` classifications;
- 39 `government_catalog_id` values (MCHS catalogue `I-##` / `N-##`) — a real identifier
  in a real official catalogue, but one with no URI scheme and no other WHG holder, so it
  buys nothing for clustering today;
- the dataset DOI and the KyrgyzstanPlanner page, as `seeAlso`.

**Consequence for clustering:** the hard-link overlay gets **two** edges, not none — Co-reference with
`gn`/`wd`/`osm` would have to be discovered by name + coordinate — and 65 of 73 have no
coordinate. See the reconciliation test below.

---

## Novelty against the live corpus (measured 2 Sep 2026)

### What WHG already holds for Kyrgyzstan

Lake-typed places with `ccodes: KG` in the live `places` index:

| namespace | count |
|---|---|
| `osm` | 263 |
| `gn` | 142 |
| `wd` | 55 |
| `ohm` | 1 |
| **total** | **461** |

*(query: `ccodes:KG` AND nested `types.identifier ∈ {LK, LKI, LKN, LKS, lake, water, reservoir, Q23397, Q131681, …}`)*

So WHG already carries roughly **six times** KGLD's entity count for Kyrgyz lakes, much
of it with OSM polygons. KGLD is not filling an empty hole; it is offering *curation and
provenance* over a space we already cover geometrically.

### But the name strings are almost entirely new

Of the 116 distinct name strings in KGLD (73 canonical + all name rows), **only 15 appear
anywhere in the 72.7 M-doc `toponyms` index**, matching 76 toponym docs in total:

```
issyk-kul  kulun  chatyr-kul  кулун  тюз  sary-chelek  ala-kul  туюктор
чакыркорум  son-kul  малый кулун  мерцбахера  kulun lake  merzbacher lake  кель-тор
```

101 of 116 are unattested. Two causes, and they pull in opposite directions:

1. **Genuine novelty.** The 39 Russian official-catalogue forms
   (`Арасан-4 верхнее`, `Кызылтор верх.`, `Шаркыратма-3.Тюп.`, …) come from MCHS hazard
   catalogue tables that nobody has digitised into an open gazetteer. These are real,
   sourced, otherwise-unavailable toponyms — the strongest single argument for taking
   this dataset.
2. **Transliteration drift.** WHG holds these lakes under different romanisations
   (`Ysyk-Köl` for Issyk-Kul, `Song Köl` for Son-Kul). A zero here does not always mean a
   new place; sometimes it means a new *spelling* of a place we hold, which is still
   worth having but is a toponym contribution, not a place contribution.

### Reconciliation is feasible for the 8, impossible for the 65

Nested `geo_distance` within 5 km of each KGLD coordinate, restricted to `ccodes:KG`:

| KGLD | WHG places within 5 km | plausible counterpart |
|---|---|---|
| Issyk-Kul | 4 | `wd:Q42191`, `tgn:7012189` (*Ysyk-Köl*) |
| Son-Kul | 3 | `gn:1527354` (*Song Köl*), `wd:Q2298945` |
| Chatyr-Kul | 7 | `gn:8065805` (reserve), `wd:Q55835242` |
| Sary-Chelek | 22 | dense cluster, needs name disambiguation |
| Kel-Suu | 18 | dense cluster |
| Kulun | 5 | `wd:Q4246465`, `gn:8403583`, `osm:w574360411` |
| Ala-Kul | 39 | dense (trekking area POIs) |
| Zyndan Western | 8 | dense |

All 8 have credible counterparts; several are unambiguous enough to assert `sameAs` by
hand. **For the other 65 there is no route at all** — no coordinate, no identifier, and
name matching against Cyrillic OSM labels is not reliable enough to auto-assert.

✅ **Settled 2 Sep by running the real thing.** MyD's reconciliation over all 71 names on
production returned **1 matched, 70 no match**. So the 461 Kyrgyz lake records WHG already
holds are, with a single exception, *different lakes* — the overlap I was estimating from
name strings is very nearly zero. This is the strongest argument for the contribution in
the whole assessment, and it means duplication is not the risk I had weighted it as.

---

## Sources and rights

37 registered sources, and they are real scholarly and official literature — peer-reviewed
glaciology (Janský et al. 2009; Falatkova et al. 2019; Erokhin et al. 2018), Soviet-era
Russian limnology (1975, 1977, 1984), the 1987 *Atlas Kyrgyzskoy Respubliki*, FAO fisheries
inventories, Ramsar information sheets, and the 2025/2026 MCHS regional outburst-hazard
technical reports. Each carries `source_quality_code` Q1–Q5, a locator, and an access date.
This is a materially better-evidenced dataset than its travel-site provenance suggests.

Rights are handled conservatively and explicitly (`kyrgyzstan_lake_rights_matrix.csv`):

| reuse class | sources |
|---|---|
| `open_attribution` | 10 |
| `copyright_citation_only` | 9 |
| `terms_unclear` | 8 |
| `bibliographic_only` | 4 |
| `restricted_nc_sa` | 2 |
| other | 4 |

KGLD's own contribution — schema, ids, curation decisions, documentation, database
organisation — is CC BY 4.0. Individual factual claims are stored, not source tables.

Per SG's standing instruction, **do not gate ingestion on licence**
(`feedback_defer_licensing`); attribution is handled separately. Recorded here so the
registry entry can be filled in correctly later:

```python
'license_spdx': 'CC-BY-4.0',
'license_url':  'https://creativecommons.org/licenses/by/4.0/',
'rights_holder': 'Ethan Hamilton',
'source_url':   'https://doi.org/10.5281/zenodo.22178862',
'citation_text': 'Hamilton, E. (2026). Kyrgyzstan Lakes Dataset v1.0.0. Zenodo. '
                 'https://doi.org/10.5281/zenodo.22178862',
'redistributable': True,   # KGLD-original material only
```

⚠️ The CC BY 4.0 covers *KGLD-original* material. Whether the derived facts we would
index (elevation, area) are separately encumbered is a question for Ethan, not for us to
assume either way — see the questions file. Note also that `license_spdx` values are
silently dropped by the registry if its License table doesn't know them; run
`python -m processing.verify_licences` after any push.

---

## Recommended route: **contributed dataset under `whg:`, not a new authority namespace**

> ⤵ **Read with the Map-your-Data section below**, added 2 Sep after SG's proposal. It does not
> replace this recommendation — MyD's own output is an LPF posted to `/datasets/validate/`, so it
> reaches the same destination — it changes *who* fills the gaps identified here.

This is the main recommendation and it differs from what was offered by email.

Palak offered to ingest KGLD "as a custom job". Read literally that means a new authority
namespace with its own script, `AUTHORITIES` entry, tile bucket and registry row. **For 73
records that is the wrong instrument.** Compare the cost of each route:

| | new authority `kgld:` | contributed dataset under `whg:` |
|---|---|---|
| Authority script | ~300 lines, maintained forever | none |
| `processing/settings.py` entry | yes | no |
| Stage chain | extract → h3 → ccode → final | n/a (points only) |
| Tile bucket + `_PER_NAMESPACE_BUCKETS` | yes, for 8 points | no |
| `update_tileserver_config` run | yes | no |
| Registry push | yes | automatic |
| Namespace count | +1 permanently (currently 24) | unchanged |
| Refresh on v1.1 | full re-run | contributor republishes |
| Records | 71 | 71 |

`whg:` already holds **228,918 records across 48 datasets** measured live on 2 Sep.
Adding a 49th of 71 records is what that route exists for, and — per
`whg_id_map_join` / CLAUDE.md — a contributor publishing an `authority=True` public
dataset moves it with **no pipeline run at all**. That is a decisive operational
advantage while the campaign is still being unwound.

**Recommend:** convert KGLD to LPF ourselves (honouring Palak's promise that Ethan does no
reformatting), load it as a WHG contributed dataset with `authority=True`, and give it
`place_id`s of the form `whg:<dataset_id>:L0001`.

**Escalate to a real `kgld:` authority namespace only if** (a) the coordinate gap is
closed to a substantial majority of the 73, **and** (b) Ethan commits to versioned
maintenance so there is something to re-ingest. Neither is true today.

### If it does become an authority namespace

For the record, so a future session doesn't have to re-derive it:

- **Namespace:** `kgld` (matches the dataset's own acronym and its id prefix).
- **`place_id`:** `kgld:L0001` — i.e. the source id `KGLD-L0001` with the redundant
  prefix stripped. Reverse map is `"KGLD-" + suffix`, deterministic in both directions.
  (Precedent: `dgsd:14030`, `alc:…` use the bare source key.)
- **Shape:** point-only incremental add. Per `point_only_incremental_add`, points skip
  `geom_store` / `h3_stage` / `ccode_merge`; use `index_namespace --source-stage extract`.
  **But** re-read Fault 12 in `developer/postmortem-ingestion-faults.md` first —
  `ccode_merge` is the only writer of `final/`, so if any part of the stage chain is
  entered at all it must come out through `ccode_merge` (with `--allow-missing-patch`) or
  the indexer silently serves the previous run's `final/`.
- **Symphonym:** new toponyms need an embedding backfill or they are invisible to
  `fuzzy`/`phonetic` discovery. The Cyrillic forms are the whole point of taking this
  dataset, so this step is not optional.
- **Tiles:** 8 points. Not worth a bucket.

---

## Preferred route (SG, 2 Sep 2026): run it through **Map your Data** as the trial dataset

SG's proposal, and it supersedes the plain "we convert it for him" reading of the route
above without contradicting it — MyD's own output *is* an LPF submitted to
`/datasets/validate/`, which lands as a `whg:` contributed dataset. Same destination,
better path. Verified against `whg3` HEAD `81d75f44c` and confirmed by the MyD session
(`whg3-17`).

### Why this dataset is a good trial

It stresses **three of the five Contribute gates**, which is the opposite of a soft test.
`runValidation` (`reconciliation.js` ~3309) counts five per-feature requirements and
`updateContributeGate` disables the button unless every count is zero:

| Gate | KGLD's state |
|---|---|
| `title` | ✅ `canonical_name` |
| `names` | ✅ 114 rows, incl. 41 Cyrillic |
| **`geometry`** | 🛑 **65 of 73 missing — the gate blocks submission until he supplies them** |
| **`types`** | 🛑 no AAT at all (satisfiable by `properties.fclasses` as an alternative) |
| **`when`** | 🛑 essentially undated — only 6 lakes have observations, 5 have events |

The geometry gate is the point: **the dataset's worst weakness becomes the trial's main
exercise**, and Ethan answers by *doing* what a letter would only have asked him. 73 rows
is small enough to finish by hand; the 8 already-located lakes are a built-in control
group for match quality; and the Latin/Cyrillic name pairs exercise the Symphonym and
derived-form paths that place#197–#205 rebuilt.

### What MyD can actually do — verified, not assumed

- **Geometry drawing:** the review card's location picker offers **Point / Line /
  Polygon**, click-to-add-vertices, *Finish shape*, *Clear all*, and multi-part capture
  (press the same shape button again → `MultiPolygon` etc.).
  `reconciliation.js:6922-6945` → `recon-map.js:180-194`.
- **Clone from match:** pulls the matched WHG place's *full* geometry from
  `/entity/<id>/api`. Ethan would not have to trace Issyk-Kul — he could adopt the
  existing OSM polygon. This matters given 461 Kyrgyz lake records are already in the
  corpus, many with OSM boundaries.
- **AAT editing:** `createAatPicker` is a searchable + browsable Getty hierarchy
  multi-select, available per-column and dataset-wide (Scope → What), falling back to the
  types the matched record carries. (⚠️ For KGLD the dataset-wide pick is the *wrong* one —
  it would flatten the intermittent-lake distinction. See the trial-run section below.)
  🛑 **Scope → What was inert, and not only on dev** —
  [place#223](https://github.com/WorldHistoricalGazetteer/place/issues/223), found 2 Sep.
  The shared AAT widget silently discards any selection beneath a facet row: the italic
  "by biome" / "by form" groupings carry no checkbox, so ancestor propagation stops and the
  selection reads back empty. Search results only *navigate* to a node rather than
  selecting it, so **there is no workaround in the UI** — and place type is a Contribute-gate
  requirement. Lake types sit under exactly these facet groupings, so this is not a corner
  case for KGLD; it is the path he would take. `whg3-17` got past it only by writing
  `scope.types` directly into IndexedDB. ✅ **FIXED on dev** (not prod): facet rows
  (`li.tt-node.tt-guide`) carry no checkbox and all three walks in the widget treated them
  as ordinary children, so ancestor propagation stopped at the first facet level. They are
  now transparent to every walk. Measured — ticking a lake type went from
  `checked 6, indeterminate 0 → "No place types selected"` to
  `checked 2, indeterminate 3 → chip shown`, persisting through Apply as
  `scope.types.selected = [{id:"aat:300132301", text:"lacustrine bodies of water"}]`.
- **Submission:** one-click Contribute builds the LPF in-browser and POSTs to
  `/datasets/validate/`, gated by an Ajv pass against the same schema the server uses.
  ⚠️ If the validator fails to load the gate **degrades to non-blocking** and lets the
  server be the gate — so a green Contribute button is not by itself proof the client
  validated anything.
- **Access:** `main/views.py:497` — `@login_required`, then `can_access_beta` or 404. An
  authenticated non-beta user gets a 404; an *anonymous* visitor gets the login redirect
  first. Ethan needs an account flagged for beta.

### 🛑 The `when` gate is the real design question, and there is an honest answer

MyD offers exactly three routes to `when`: a per-row date column, a dataset-wide Scope →
When range, or a Scope PeriodO period. **For 73 lakes, all three are an invention** — a
lake is not a historical event and has no date.

But the LPF schema is broader than MyD's UI. Its temporal requirement is an `anyOf` that
accepts a `when` at feature level **or on the geometry**, or on a type, relation or name.
So there is a truthful formulation:

> The lake is undated. The **polygon** is not — it was acquired from a specific image on
> a specific date.

A `when` on the geometry says precisely that, it validates (tested by `whg3-17` with the
browser's own Ajv config), and it is **exactly what Ethan's own GIS note demands** of any
polygon product: acquisition date, sensor, method, mapping threshold, licence. The
dataset's stated standard and the schema's honest answer are the same answer.

✅ **BUILT — [place#220](https://github.com/WorldHistoricalGazetteer/place/issues/220),
`7fbcfa838`, on `staging`/dev (not prod).** MyD wrote only `feat.when` when this analysis
was first drafted. It now offers two routes to a geometry-level `when`, mirroring how
place dates already work:

* a per-row column with the new **"Geometry captured (date)"** role, parsed by the same
  date parser;
* a dataset-wide capture range in **Scope → When**.

Three design points from `whg3-17` worth keeping, because each one is a trap avoided:

* It is **deliberately not** the existing Scope → When range. That one constrains *which
  gazetteer records may match*, and "this polygon was traced from a 2019 image" is not a
  claim about which records should match. The new fields never reach the reconciliation
  query.
* For the same reason the new role hint is tested **before** the place-date hint — a
  column called `acquisition_date` must not be silently claimed as a date for the *place*,
  "which is precisely how an undated dataset ends up asserting dates it does not have".
* It rides **only on the contributor's own geometry**. A shape cloned from a match, or
  auto-enriched from one, keeps the gazetteer's provenance instead: a capture date says
  nothing about a shape someone else made. So Ethan owes a capture date only for polygons
  he supplies or draws, not for OSM boundaries he adopts.

The Contribute gate accepts a geometry-level `when` (otherwise emitting one would have
unblocked nothing), and the "no date" help text names the new route. Verified against the
repo's schema with the browser's own Ajv config: **an undated place with a dated polygon
*and* a container relation — the exact case that fails the server's schema today — is
valid.** So the route for the 73 lakes is acquisition dates on the geometry, and every
row passes, container relations and all.

✅ **PROVEN END-TO-END ON DEV, 2 Sep.** `whg3-17` built a KGLD-shaped fixture — 5 undated
lakes, polygons, `acquisition_date`, a container column so each row carries `relations` —
and ran it through MyD. Roles auto-detected, 5/5 capture dates parsed, and with **Scope →
When left completely empty** the validation pane read *"Ready to contribute. All 5 places
pass WHG's Linked Places validation."* Contributed with full citation metadata → dataset
1778, 5 places, geometry + AAT type + container relation + a TGN `closeMatch`, citation
carried through. `PlaceGeom.jsonb` preserves `geometry.when` verbatim.

So the producer side works, and SG's question — *can data submitted this way reach the
fully-ingested stage* — is answered **yes**.

### 🛑 …but the consumer side then throws the distinction away (place#222)

The run surfaced a defect that matters more here than the original provenance bug.
`validation/create_dataset.py: parse_dates()` harvests `when` from *every* location LPF
permits — `['geometry','when']` included — and flattens them all into a single
`Place.minmax`. Measured on the ingested record:

```
"geoms":  [{ "when": {"timespans":[{"start":{"in":"2019-08-14"}, …}]} }]
"minmax": [2019, 2019]
```

**Issyk-Kul becomes a place that existed in 2019.** `minmax` drives the map, the timeline
and the dataset's advertised temporal extent. The LPF file stays honest; the WHG record
does not. The exact distinction #220 exists to preserve is discarded one step after it
arrives.

For Ethan this is worse than the bug we started from. He would supply acquisition dates
*because his methodology demands them*, and WHG would use them to assert that his undated
lakes are dated — the precise failure the invitation is written to avoid.
**Shipping #220 without #222 would let him contribute and then misrepresent him**, so
#222 joins the preconditions rather than sitting alongside them.

✅ **FIXED on dev** (not prod). Same fixture, same acquisition date:

```
before:  "minmax": [2019, 2019]
after:   "minmax": [null, null]
         "geoms": [{ "when": {"timespans":[{"start":{"in":"2019-08-14"}, …}]} }]   ← still there
```

`parse_dates()` no longer harvests `when` from the geometry, so a shape's date cannot
become the place's date; the geometry keeps its own `when` verbatim in `PlaceGeom.jsonb`,
which is where a claim about a geometry belongs. Names, types and relations still
contribute, since each attests the *place* at a time — which is what `minmax` is for.

⚠️ **One consequence that cuts the other way, worth knowing before this is generalised:** a
dataset whose *only* temporal information is geometry-level — historical boundary layers,
say — now ingests with **no place-level `minmax`** rather than an inferred one. `whg3-17`
argues that is both the honest reading and the safer failure, since the old behaviour could
place a place on the timeline at a date nobody asserted. Recorded because several of our own
boundary namespaces (`vob_*`, `kain_par`, `ukhc`) are exactly that shape, so if any of them
ever arrives through this route the `minmax` will be empty by design, not by fault.

A full re-run after both fixes, from the fixture with Scope → When still empty: roles
auto-detected → "Ready to contribute" → contributed → ingested with type
`lacustrine bodies of water`, geometry `when` preserved, `minmax [null, null]`. Test
datasets 1778/1779/1780 all deleted.

Two upstream findings from `whg3-17` that bear on this, recorded so nobody re-derives them:

- The LPF schema's temporal requirement is **vacuously satisfiable**: each `anyOf` branch
  is shaped `{properties: {relations: {…}}}` with no `required`, so a feature that simply
  *lacks* `relations` satisfies that branch for free. A feature with no `when` anywhere
  validates — unless it happens to carry `relations`, in which case it correctly fails.
  That is an upstream LPF schema defect, not a WHG one.
- It bites unevenly here: MyD emits `relations` for any row with a resolved container
  column, so **an undated lake with a container fails the server's schema while an undated
  lake without one passes by accident.**

  ⚠️ **But that asymmetry is reachable by only one of the two routes into WHG**, and the
  distinction matters (corrected 2 Sep by `whg3-17`; my first write-up had it as a general
  property of submission). Both routes validate against the same file —
  `whg/settings.py:803` points `LPF_SCHEMA_PATH` at `validation/static/lpf_v2.0.jsonld`
  and `validation/views.py:352` is its only consumer — so the difference is entirely in
  what happens *before* the server sees anything:

  * **Contribute** is client-gated first. `updateContributeGate` disables the button
    whenever any feature is missing `when`, container column or not. All 73 undated lakes
    are blocked **uniformly** and nothing reaches the server; the accidental pass is
    unreachable.
  * **Export → manual upload at `/datasets/`** skips that gate entirely (manual Export
    stays available by design when Contribute is blocked). **This is the only route where
    the vacuous branch is live**, and therefore the only one where the
    with-container/without-container asymmetry bites.

  For *this* contributor that is a plausible route rather than a hypothetical one — he is
  exactly the sort to export, inspect the JSON and upload it himself. If he does, he gets
  a partial pass that looks like validation endorsing some of his lakes and rejecting the
  rest for no reason he can see. **That is a worse first contact than a clean uniform
  block**, and it is a further argument for the geometry-level `when` rather than a
  separate concern.

### ✅ Geometry provenance — defect found statically, fixed, live

I found on 2 Sep that MyD discarded geometry provenance on export, and `whg3-17` confirmed
and fixed it. ✅ **LIVE ON PRODUCTION — `80bfc5d9b`**, verified in the deployed prod bundle.

⚠️ The hash is **`80bfc5d9b`**, not the `8f3ca2c5a` this document first recorded: `main` is
built by selective cherry-pick, not by merging `staging` (which carries ~80 other commits,
GRACE among them — `main` has already had to revert three of those once). Check against the
`main` hash; the `staging` one will not appear in a prod build.

The bug: `onReviewGeom` recorded `{source: 'drawn'|'match', geometry}` faithfully, but the
record assembly at `reconciliation.js` ~2626 (`const geom = ov ? ov.geometry : wktGeom;`)
kept only `.geometry`, so `buildLPF` never received the provenance — and the clone branch
had not recorded *which* record it cloned from, so there was nothing to cite anyway. The
result was an asymmetry: **MyD choosing the match's location for you produced a cited,
`less-certain` geometry (place#184); the user deliberately clicking "Clone from match"
produced a bare one, indistinguishable from a surveyed coordinate.**

The exported shapes after the fix:

| origin | shape |
|---|---|
| cloned from match | `certainty: "less-certain"` + `citations: [{label: "WHG reconciliation match <id>", "@id": <id>}]` — identical to auto-enrichment |
| drawn on the map | `citations: [{label: "Drawn by the contributor (WHG Map your Data)"}]`, **no `certainty`** |
| the dataset's own WKT column | unchanged, bare — it is the contributor's own data |

`whg3-17`'s reasoning, worth keeping because it is the kind of thing that gets re-argued:
the clone gets *identical* treatment to auto-enrichment because for a reader of the file
the claim is the same either way — the shape is the gazetteer's, not the contributor's;
who clicked is a UI fact, not a provenance fact. Drawn geometry gets no `certainty`
because LPF's `certainty` is confidence in *the assertion*, which we cannot know for a
shape someone drew, and `certain` would positively claim they surveyed it. Stating the
method in a citation is the fix; a certainty value is not. `approximation`
(`crm:P189_approximates` / `geo:hasSpatialAccuracy`) was considered and rejected —
`additionalProperties: false`, no free text, and no tolerance figure to put in it.

Both shapes validate, and they reach the WHG record rather than stopping at the download:
`validation/create_dataset.py:338` stores `feat['geometry']` verbatim into
`PlaceGeom.jsonb`.

**This mattered specifically because of this dataset.** Ethan's methodology §12 requires
every coordinate to be source-traceable with method and precision. Had he drawn or cloned
65 geometries against the unfixed build, the WHG record would have silently broken the
standard that makes his dataset worth having — and he would have been the first person to
find the bug, which is a bad way to find it.

### Deliverables built 2 Sep — the LPF and the MyD spreadsheet

`build_lpf.py` converts the Zenodo package into two artefacts in this folder. Both are
derived; re-run the builder rather than editing them. Full rationale in
[`README-lpf.md`](README-lpf.md).

| File | Rows / features | sha256 (first 16) |
|---|---|---|
| `kgld_v1.0.0.lpf.json` | 80 features — 71 lakes + 9 supraglacial, 8 located (each with a `geo:hasSpatialAccuracy` tolerance), 14 dated, 129 toponyms, 2 `closeMatch` + 3 Ramsar links, plus `indexing` + `citation` blocks | `ed9deda2bf09ada6` |
| `kgld_v1.0.0_myd.csv` | 80 rows, 19 columns incl. `geometry_captured`, `observation_years`, `close_match`, `ramsar_ris` | `85ef661292e5bf42` |

Validated against whg3's own `validation/static/lpf_v2.0.jsonld` (Draft7): **66 errors,
and that is correct** — see below. It returned PASS until
[place#221](https://github.com/WorldHistoricalGazetteer/place/issues/221) shipped on 2 Sep
and closed the vacuous temporal branch.

Dataset provenance rides in **both** blocks MyD itself writes — `indexing` (schema.org
`Dataset`, the one `validation.views.extract_dataset_metadata` actually reads) and
`citation` (CSL-JSON, what the LPF schema `$ref`s) — populated from the Zenodo REST record
kept alongside as `zenodo_record_22178862.json`, and placed ahead of `features` so the
server's streaming reader stops early. Four controls confirm the citation block is really
being checked (missing `schema`, missing `type`, a bogus key against
`additionalProperties: false`, and an author without `family`, which WHG's CSL requires —
all four FAIL as they should).

🛑 **That PASS was vacuous for 66 of the 71, and the file was built to demonstrate it.**
The evidence below argued for place#221; since #221 shipped, **row E is simply the
behaviour** and the file fails until those 66 are dated:

| # | Case | Result |
|---|---|---|
| A | as delivered | **PASS** |
| B / B2 | controls — `names` emptied / `types` removed | **FAIL** ✅ the check discriminates |
| C | one **undated** lake + a container relation | **FAIL** |
| D | the same + a **geometry-level `when`** | **PASS** |
| E | **all 66** undated lakes + container relations | **FAIL, 66 errors** |

✅ **Resolved: place#221 shipped and the schema now enforces what it documented.** Before,
those 66 passed by accident and would have failed the moment the `oblast` column was
reconciled; now they fail immediately, uniformly, and explicably. Confirmed on the
production build — `validate KGLD → FAIL, 66 of 71`; `control: same file, all dated →
PASS`. `build_lpf.py --validate` prints this as EXPECTED rather than as an error count, so
a future session does not read it as a regression.

The route out is row D — a geometry-level capture date (place#220) — which the contributor
supplies as he places each lake, so **one action closes both the geometry gate and the
temporal one**. ⚠️ It must be in the covering letter: he will import his own data and be
told 66 of 71 records are invalid, and that needs to arrive as an explanation, not a
verdict on his dataset.

⚠️ **Two files, because MyD's importer is column-oriented** and has no column shape for
LPF's `names[]` and `types[]` arrays. An LPF import may silently lose the Russian
toponyms — the most valuable thing in the dataset. **Which format MyD handles better is
untested and is the first thing the trial should measure.**

---

### End-to-end trial run on production, 2 Sep — measured results

`whg3-17` ran the real file through MyD on production. Nothing was contributed or
published: the type, date and geometry gates all held, so Contribute never became
reachable and no dataset was created.

**Four things confirmed as predicted:**

| Prediction | Measured |
|---|---|
| Geometry gate blocks the unlocated | **`63 of 71 have no location`** — exactly the 63 |
| C→D: a capture date repairs the temporal gate | `71 of 71 no date` → **`63 of 71`** after Scope → When → Geometry captured = 2019. The 8 rows with a shape gained a valid `geometry.when`; the other 63 have no shape to attach one to |
| AAT cannot do better than `aat:300008680` | Searching the live picker: `glacial` → only *glacial landforms*; `moraine` → only *moraines*; `riegel`, `landslide`, `dammed` → **nothing**. Prefilling was right |
| The lakes are genuinely new to WHG | see below |

**🎯 Reconciling the 71 lake names against the live gazetteer returned `1 matched, 70 no
match`.** That is the strongest argument for taking this dataset that has come out of the
whole assessment, and it materially sharpens the earlier name-string test (15 of 116
strings existed *somewhere* in the toponyms index; only one is actually the same lake).
The 461 Kyrgyz lake records WHG already holds are, with one exception, **different lakes**.

**Three new faults, all surfaced by this file:**

- ✅ **[place#224](https://github.com/WorldHistoricalGazetteer/place/issues/224) — the LPF
  import kept only `properties`.** Names, types, `when`, links, descriptions and both
  provenance blocks were discarded, so for a few hours the CSV was the only usable vehicle.
  **Fixed and verified on production the same day** by importing this file: 40 Russian
  toponyms kept, 71 rows typed, 5 dates read, creator and DOI preserved. **Send the LPF;
  the CSV is now a fallback that carries neither names nor types.**
- 🛑 **[place#225](https://github.com/WorldHistoricalGazetteer/place/issues/225) — two
  column-hint faults.** The coords-role hint is unanchored, so `coordinate_method` and
  `coordinate_precision_m` were claimed as coordinate columns and **suppressed every
  coordinate in the dataset** (`0 of 71`; setting them to "other" restored `8 of 71`).
  KGLD's own methodology fields silenced its own coordinates. Separately, MyD's country
  hint matches `ccode` but **not LPF's `ccodes`**, and array values stringify to `'["KG"]'`
  which `isCcode` rejects — with the measured consequence that **`Naryn` oblast reconciles
  to Naryn, *Kazakhstan* at score 100**, with the correct Kyrgyz Naryn at #2 among five
  Russian and Kazakh homonyms. For a dataset that is 100% one country, that is a
  containment error waiting to happen.
- ⚠️ **[place#226](https://github.com/WorldHistoricalGazetteer/place/issues/226) — the
  `/schema/` prefix is served only under `DEBUG`.** Both `lpf_v2.0.jsonld` and
  `csl-citation.json` are unreachable in production; a comment in `whg/urls.py` says
  staging and prod need an nginx alias that was never added. Nothing breaks today, but we
  hand contributors an exported file and tell them it validates against a schema they
  cannot fetch.

✅ **The CSV run is done too** (dev, same day). It dodges both #225 faults, and the
containment contrast is the clearest evidence for filing #225(b) at all:

| | LPF (`ccodes` → `'["KG"]'`) | CSV (`ccode` → `KG`) |
|---|---|---|
| `Naryn` resolves to | Naryn, **Kazakhstan** | Naryn, **Kyrgyzstan** ✓ |
| containers needing review | **14** | **0** |
| auto-confirmed | 56 | **70** |

Coordinates parse at 8 of 71, as they should.

⚠️ **The prefilled AAT id does not set the type directly** — `buildLPF` reads types only
from `project.rowTypes` (written solely by the per-row AAT modal), from Scope → What, or
from an enriched match, and a `type`-role column's contents are never parsed as
identifiers.

✅ **But the column is what makes typing two clicks instead of seventy-one.** It drives the
modal's "apply to all rows where this column = X" grouping, keyed on exact cell value, and
our column holds two values — `aat:300008680` on 63 rows, `aat:300008680;aat:300387086` on
17 (8 lakes + the 9 supraglacial features). **Two picks, and the intermittent-lake
distinction survives.** A single dataset-wide
Scope → What pick would apply one set to all 71 and flatten it, so the simpler route is the
lossy one here. He must set the column's role to *Feature type* by hand first — it is not
auto-detected — or the affordance never appears. Full detail in `README-lpf.md`.

*(An earlier version of this document said to use Scope → What. That was wrong for this
dataset and is corrected above.)*

⚠️ **`oblast` is not auto-detected as a container** — MyD's hint covers
`county|region|province|state|district` with no Central Asian terms — and it is the column
that matters, on 70 of 71 rows. It must be mapped by hand, which belongs in the
walkthrough. (`district` *is* detected, and is real data on 14 rows.) This is the mirror
image of #225(a): there an **unanchored** hint claimed too much; here **anchored** hints
claim too little. Same family, opposite failure, no test on either — which is why
`whg3-17` is putting a guard to SG rather than filing a third patch.

**The LPF is deliberately *not* being changed to work around #225.** `properties.ccodes`
is correct LPF; emitting a duplicate singular `ccode` to satisfy a faulty hint would
corrupt the canonical artefact to paper over a bug that is already filed. Fix the reader.

---

### Preconditions before inviting him

*Status 2 Sep 2026, end of day: **all four whg3 items are on production**. Two remain.*

1. ⏳ **The indexing campaign is complete** (SG's standing ruling). The long pole.
2. ✅ Geometry provenance — `80bfc5d9b`, live.
3. ✅ **[place#220](https://github.com/WorldHistoricalGazetteer/place/issues/220)** —
   geometry-level `when`. Live: the prod bundle carries the `Geometry captured (date)`
   role, the `geometry.when` panel and the Scope capture-range fields. *Left open
   deliberately* — the code path is proven, but whether it fits how real sources describe
   their imagery is the contributor's judgement, not a code path.
4. ✅ **[place#222](https://github.com/WorldHistoricalGazetteer/place/issues/222)** —
   verified inside the **running prod Celery worker**, not the web container:
   `geometry-only when → ([], None)`, with the control `feature-level when →
   ([[1850,1900]], [1850,1900])` proving ordinary dates are untouched. Closed.
5. ✅ **[place#223](https://github.com/WorldHistoricalGazetteer/place/issues/223)** —
   verified on production. Closed.

   🛑 **This was never a dev-only bug.** `whg3-17` first reported it as confined to dev and
   corrected itself after checking the file rather than the issue: `typeTreeWidget.js` was
   already on `main`, byte-identical to the buggy copy, and shared by `reconciliation.js`,
   `wb-record-fields.js`, `wb-aat-modal.js` and `atlas.js` — **four production surfaces**.
   So Scope → What has been silently inert on production for most of the vocabulary, with
   place type a Contribute-gate requirement and no workaround. Anyone who tried to type
   their places that way and gave up had no way to tell why. Worth knowing beyond this
   dataset: it is not a KGLD-specific finding, and KGLD is only what made someone look.
6. ✅ **[place#224](https://github.com/WorldHistoricalGazetteer/place/issues/224)** — LPF
   import now reads names, types, dates, geometry and both provenance blocks. Verified by
   importing *this file* on production. Closed.
7. ✅ **[place#225](https://github.com/WorldHistoricalGazetteer/place/issues/225)** — the
   column-hint faults. Closed.
8. ✅ **[place#226](https://github.com/WorldHistoricalGazetteer/place/issues/226)** — both
   schemas now resolve at their declared `$id`; the cause was `/schema/` being served only
   under `DEBUG`, with the nginx alias written in a code comment and never added. Closed.
9. ✅ **[place#221](https://github.com/WorldHistoricalGazetteer/place/issues/221)** — the
   vacuous temporal branch. Closed; consequences above.
10. ⏳ **He is not a registered WHG user at all** (SG, 2 Sep), so the beta flag is a second
    step: the letter must ask him to register first.

11. ✅ **[place#227](https://github.com/WorldHistoricalGazetteer/place/issues/227)** — the
    dataset `description` had a reader and no writer, so every MyD-contributed dataset
    landed with a blank description on its public page. Raised from this work; both the
    missing field and a second fault it exposed (MyD writing `description` on export and
    dropping it on import) are fixed, and MyD's contract harness gained the missing
    direction — every `CITE_FIELDS` entry must be recoverable by `lpfCitation`. Closed.

12. ✅ **[place#228](https://github.com/WorldHistoricalGazetteer/place/issues/228)** — the
    importer read `links[]` but the exporter **dropped every one**: 147 in, 0 out, both
    `closeMatch` assertions included. #224's last unswept corner, found by asking rather
    than assuming. Fixed: 147 in → 147 out (145 `seeAlso`, 2 `closeMatch`), and they return
    *whole* — `certainty` and `citations` intact, `rsis.ramsar.org` intact. Closed.

⚠️ **One deliberate non-behaviour worth knowing.** An imported `closeMatch` does **not**
pre-confirm a row. MyD's matches are keyed to reconciliation runs against specific columns,
and fabricating a match from an imported link would misrepresent where the judgement came
from — ours, in the file, not the reconciler's. Correct, but it leaves a real hazard: a
contributor can accept a *different* match and export two conflicting `closeMatch` links
for one place. `whg3-17` has recorded that as an open design question rather than guessing
at a rule. **Consequence for us: the letter must tell Ethan that if he agrees with our two
links he should accept them in the tool as well, not merely leave them in the file.**

13. ✅ **[place#230](https://github.com/WorldHistoricalGazetteer/place/issues/230)** — a
    **coined name came back looking attested.** The importer flattened `names[]` to a
    column and the exporter rebuilt from that column, so the toponyms round-tripped and
    everything said *about* them did not: `Hamilton 1` returned with no `citations`, no
    coinage statement, indistinguishable from a name people use. Found by asking whether
    the coinage citations survived. The whole point of the naming decision was that a
    gazetteer may coin a name but must never let a coined one pass as attested — and the
    round trip was quietly undoing exactly that. Fixed; the study labels keep their
    figure-numbering citations too. Closed.
14. ✅ **[place#229](https://github.com/WorldHistoricalGazetteer/place/issues/229)** — a
    contributor can now declare `certainty` and `approximation`
    (`geo:hasSpatialAccuracy`, tolerance in km) on geometry they create. `whg3-17`'s
    original reasoning for leaving drawn geometry uncertainty-free was that MyD cannot know
    how sure the contributor is — true, and the wrong conclusion, because *he* can know and
    had no way to say it. **Directly serves this dataset:** KGLD methodology §12 requires
    coordinate precision be retained, and the 72 shapes Ethan places can now each carry it.

✅ **The conflicting-`closeMatch` hazard is resolved, and narrower than feared.** Simulated
by hand-search acceptance: **both links survive, neither is dropped**, and they are legible
as different kinds of claim — the tool's carries `whg_match_score` and no citation, ours
carries its citation and no score. Better still, **the reconciler offers no candidate at all
for Kel-Suu or Kulun**, so Ethan cannot accidentally accept a competitor; he would have to
hand-search. The letter's instruction to accept our two in the tool still stands.

15. ✅ **[place#231](https://github.com/WorldHistoricalGazetteer/place/issues/231)** —
    **MyD discarded the precision figures while keeping the coordinates they qualify.** All
    eight `geo:hasSpatialAccuracy` tolerances and their `certainty` and `citations` were
    dropped on round-trip; the lon/lat survived. Found within an hour of adding them.
    Fixed — all eight return exactly (5, 3, 3, 0.5, 2, 0.5, 0.1, 0.1). Closed.

    ⚠️ **The most quietly damaging of the three**, and worth understanding rather than just
    recording: a coordinate whose stated precision has been dropped **does not look
    damaged — it looks exact.** Issyk-Kul's centre stated to ±5 km and Issyk-Kul's centre
    stated to nothing are different claims, and the second is the one that gets used badly
    downstream. KGLD methodology §12 requires the figure be retained; MyD was failing that
    requirement silently while reporting a clean import.

    ⚠️ **Correctly scoped on the fix:** the restored annotation holds only while the shape
    is still the file's own. A shape Ethan later draws or clones is a *different* shape, and
    our assessment of the old one does not describe it.

### 🔁 One pattern, three files — worth carrying past this dataset

`links[]` (#228), `names[]` (#230) and `geometry` (#231) failed **identically**: MyD
flattens each structure to a column or a pair of columns, so **the value round-trips and
everything qualifying it does not.**

| structure | value that survived | annotation that was lost |
|---|---|---|
| `links[]` | — (all 147 dropped) | `certainty`, `citations` |
| `names[]` | the toponym | the coinage citation — *a coined name came back looking attested* |
| `geometry` | lon/lat | `certainty`, `approximation` tolerance, `citations` |

Each was found by *this file* carrying something the previous rebuild had not, and each
time the fix was applied to the one structure rather than to the pattern. `whg3-17`'s own
summary is the useful statement of it: **three fields need two checks each, because the
value and its annotation travel by different routes.** Its contract harness now has seven
parts.

The generalisable form, for any converter in this repo too: **a round-trip that preserves
values may still be dropping everything that qualifies them, and the result looks correct —
often *more* correct, because an unqualified number reads as an exact one.** Test the
annotation separately from the value it hangs on. See
[[roundtrip_preserves_values_drops_qualifiers]] in the working memory.

**Production is `04019e8e5`. Every whg3 item is closed — the send gate is now the indexing
campaign plus registration, and nothing else.**

The rebuilt file was re-verified through the production tool: 40 name variants, 71 rows
typed with 79 assignments and the 8 dual-typed rows intact, 5 dates, 8 coordinates, the
citation carrying creator, DOI and description, and `observation_years` landing safely on
role `other` where no hint claims it — now covered by a role-hint test fixture rather than
by luck. Validation reports `missing when (66)` and `missing geometry (63)`, cleanly.

Production is `b4a51481c`.

⚠️ **If a dev-side trial run of KGLD is ever proposed, know this first:** whg3's dev and
prod share the **same Elasticsearch** (`ES_PUB=pub`, `ES_WHG=whg`, same CRC gateway), and
Postgres is separate — so everything up to *and including* ingestion is contained on dev,
and **a dev trial can be taken as far as ingested and no further.**

Dev's connection to that shared index is **read-only**, though: the dev container announces
`ES_CONN is READ-ONLY (context 'dev-whgazetteer-org' points at production index 'whg')` on
startup, so a dev publish would *fail* rather than pollute production. (Corrected 2 Sep by
`whg3-17`, who first reported it the other way; an earlier version of this paragraph said a
dev publish would write to the live index. It would not — the risk is smaller than first
recorded.)

🪤 And a deployment trap if anyone verifies one of these fixes themselves: `deploy dev
restart` restarts only the web service, but **ingestion runs in Celery**, so a worker holds
the old module in memory and a corrected `parse_dates()` still reports the old `minmax`.
Use `deploy dev restart --celery`. `whg3-17` nearly recorded a working fix as broken.

Related but **not** a precondition:
[place#221](https://github.com/WorldHistoricalGazetteer/place/issues/221) — the
vacuously-satisfiable `anyOf` in the LPF temporal requirement, filed by `whg3-17` from the
second finding above. Tightening it makes undated features fail *uniformly* rather than
erratically, which is right, but it also removes the accidental escape hatch — so **#220
must land first**. That ordering is recorded in the issue.

---

## What to exclude

| Records | Why |
|---|---|
| `KGLD-L0011` *Merzbacher Lake (legacy ambiguous record)* | `record_status = disputed`; explicitly a provenance placeholder superseded by L0028/L0029 |
| `KGLD-L0069` *Kulun lake — FAO inventory record (unresolved)* | `record_status = disputed`; its own note says it "is not mapped to Main Kulun"; the title is a bibliographic description, not a toponym |
| the 9 `KGLD-F####` study features | ⚠️ **REOPENED — see below.** The original reason was deference to `identity_status`, which is not a gazetteer argument |

→ **80 places** (71 lakes + 9 supraglacial features), of which **8 located** and **14 dated**.

### ✅ Resolved: the 9 supraglacial features ARE places, and are named

SG's challenge, 2 Sep, and it lands: *these are precisely the kind of temporally-scoped
entities a historical gazetteer ought to record.* My stated reason for excluding them —
that KGLD's `identity_status` says they are "not to be promoted to permanent geographic
entities" — is a **physical-geography objection, not a gazetteer one.** "Not permanent" is
a reason to include a place in a *historical* gazetteer, not to leave it out. Deferring to
the source's framing there was the wrong move.

**We already accept the class.** Five short-lived lakes are in the file with lifespans
(Kashkasuu, Jeruy, Karateke, Toguz-Bulak, Zyndan Western) — they formed and drained on
record, and four of the five have no coordinates either. So neither impermanence nor
missing geometry distinguishes the 9.

**What the 9 actually carry**, on the Southern Inylchek Glacier, from one 2022 study
(`S0037`):

- **6 dated drainage events at day precision**, 2017-07-24 → 2018-07-18, including two
  *simultaneous* multi-lake drainages — `E0009` across 5 features, `E0010` across 7 — which
  is the scientifically distinctive part: a shared englacial conduit network emptying
  several lakes at once.
- **9 dated observations**, including recurrence: `F0001` drains 24 Jul 2017, recharges the
  25th, drains again 5 Aug, recharges the 9th, then drains twice more in 2018. It is a
  *recurrent* feature, not a one-off.
- A named parent glacier, and stable KGLD feature ids.

**The one real obstacle is naming, not permanence.** `"Southern Inylchek supraglacial
Lake 1"` is a figure label — `source_locator` reads *"Figures 2–11 / study-local lake
numbering"*. Nobody calls it that; another paper would number them differently. That is
what `study_local_feature` actually means, and it is a genuine gazetteer objection where
impermanence was not: a gazetteer indexes *named* places, and this is a within-paper
referent.

**The cost of excluding them is larger than I recorded.** It is not 9 records — it is
**9 records plus 6 dated events plus 9 observations**, i.e. all six of the events that
carry no `lake_id` and go nowhere else. Those are the only day-precision multi-entity
events in the dataset.

✅ **RESOLVED — SG, 2 Sep: include them, and give them names.** *"A name is what anyone
calls something."* The naming objection is answered by naming: unnamed, they are
unsearchable and unmatchable; named, they are ordinary gazetteer records with unusually
good temporal evidence.

They are named **`Hamilton <n>`** after the dataset's compiler, keyed to KGLD's own
study-local numbering so the link back to the source figure survives (hence the gaps —
1, 3, 4, 5, 7, 8, 9, 14, 15 are the study lakes that have events).

⚠️ **The coinage is ours and every name says so**, in an LPF `citations` entry on the name
itself:

> *Name coined by the World Historical Gazetteer for this contribution, after Ethan
> Hamilton, compiler of the Kyrgyzstan Lakes Dataset; the numeral is KGLD's own study-local
> lake number. Not a local, historical or otherwise attested toponym.*

A gazetteer may coin a name; it may never pass one off as attested usage. The source's own
label (`"Southern Inylchek supraglacial Lake 1"`) is kept as a second name, cited to Sakurai
et al. 2022 and its figure numbering.

Each feature carries a lifespan from its observed drainage and recharge dates
(`Hamilton 1`: 2017-07-24 → 2018-07-18, four events, two of them simultaneous with other
lakes), the intermittent type — these fill and empty by definition — and a description
naming the shared englacial conduit network. No geometry: the source publishes none.

⚠️ **`parent_glacier` stays a property, not a `relations` entry.** The live index holds two
GeoNames records both titled plainly *"Inylchek Glacier"* — `gn:1526997` at 42.158 N and
`gn:1527406` at 42.245 N — with nothing but latitude to separate North from South.
Asserting one would be a containment we cannot support; Ethan knows the region and can
reconcile it in MyD exactly as he does `oblast`. **The letter asks him which it is.**

The file is now **80 features — 71 lakes + 9 supraglacial — 14 dated, 129 toponyms.** The
66 undated lakes are unchanged: the 9 new records all carry a `when` and validate.

Indexing either disputed record would inject a non-place into the corpus under a title
that reads as a place name — precisely the kind of thing that is invisible until someone
searches for it. Exclude both; the evidence stays in the source package.

---

## Fields with no home in the `places` schema

Worth stating clearly, because the email promised Ethan we would "preserve the more
unusual fields, the ones LPF has no obvious place for":

| KGLD field | WHG destination |
|---|---|
| `elevation_m` (50 of 63 public rows) | ✅ top-level `elevation` (integer) — direct fit |
| `canonical_name`, all name rows | ✅ `title`, `toponyms[]` |
| `origin`, `mchs_lake_type` | ✅ `types[]` |
| lat/lon (8) | ✅ `geometries[].repr_point` via `enrich_geometry` |
| events, observations | ✅ `timespans[]` / per-toponym timespans |
| `ramsar_site_id`, DOI, project page | ✅ `links[]` |
| **`area_km2`, `max_depth_m`, `volume_km3`, `mean_depth_m`, `shoreline_km`, `catchment_km2`, `salinity_g_kg`** | 🛑 **nowhere** — the schema has no numeric attribute bag |
| **166 measurement rows with per-value source, method, comparability group, confidence** | 🛑 **nowhere** |
| **16-row conflict register** | 🛑 **nowhere** |
| `source_quality_code`, `audit_status`, `public_reference_eligible` | 🛑 **nowhere** |

The only available carrier is `descriptions[]` (`{value: text, lang: keyword}`), i.e.
prose. We could compose one description per lake summarising area / depth / volume with
its source — searchable text, but not queryable data, and it discards the comparability
and conflict apparatus that is the dataset's distinguishing feature.

**This is the honest limit and SG should see it before anyone replies to Ethan:** WHG can
faithfully carry KGLD's *identity, names, types, temporality and provenance links*, and
cannot carry its *measurement evidence layer* as structured data. The morphometry is
exactly the part Ethan already agreed "falls outside your main focus", so this is
consistent with the correspondence — but "we can very often find a way to capture those
on our side" is a stronger promise than the schema currently supports, and it would be
better to say so now than after conversion.

---

## Estimated WHG impact

| Index | New documents | Notes |
|---|---|---|
| `places` | **80** | 71 lakes + 9 supraglacial features; 8 located as shipped, 63 to be placed by the contributor |
| `toponyms` | 129 attestations, ~115 genuinely new | the 40 Russian official-catalogue forms are the real prize — **reconciliation returned 1 match and 70 misses**, so these lakes are almost entirely new to WHG |
| hard-link overlay | 2 `closeMatch` as shipped, more after his reconciliation | KGLD had already reconciled two lakes to `wd:` and `gn:` without recording it as such |
| tiles | 8 points as shipped, up to 71 after | not worth a bucket either way |

---

## Summary verdict

**Worth taking; small; built and ready; blocked only on the campaign and his registration.**

KGLD is unusually well-made for an unaffiliated single-author dataset. The entity /
measurement separation, the refusal to average conflicting values, the public-release
audit, the per-source rights matrix and the Frictionless descriptor with 110 passing
checks are all better practice than several datasets already in the corpus. The source
registry is real scholarship.

Its value to WHG is concentrated in three things: **~100 Russian official-catalogue lake
names that exist in no other open gazetteer**, **11 dated GLOF events with a fatality
record**, and **a clean provenance chain back to MCHS hazard catalogues and Soviet-era
limnology**.

Against that, three limits, in order of severity — **and the first two are now work rather
than objections**, because SG's Map-your-Data proposal puts them in the hands of the person
who can settle them:

1. **89% of the entities have no location.** For a *gazetteer* that is close to
   disqualifying on its own terms. MyD's Contribute gate will not let him submit until
   every record has one, so the dataset's worst weakness becomes the trial's main exercise.
2. **Almost no external identifiers.** Two were already there, unrecorded as such (see
   "Links / external concordance"); the reconciliation step supplies the rest. Note that
   duplication against the 461 Kyrgyz lake records WHG holds is **not** the risk I first
   weighted it as — running the real reconciler returned **1 match, 70 misses**.
3. **No Kyrgyz-language names**, in a Kyrgyz dataset. This one no tool can fix: nothing
   extracts a toponym nobody has recorded. It stays a plain question in the letter.

**Sequence:** finish the campaign → ask Ethan to register, then flag his account for beta
→ send the invitation (`questions-for-ethan.txt`, read its header first) → he reconciles,
places and dates his own 80 records in MyD → Contribute posts the LPF to
`/datasets/validate/` → it lands as a `whg:` `authority=True` dataset.

What changes from the plain "we convert it for him" route is that **he** supplies the
coordinates, the capture dates and the match judgements — the three things we could not
supply for him, and the three the tool was rebuilt this week to let him supply well.

---

## Verification notes

Everything numeric above was measured, not inferred. Reproduce with:

- **Package contents** — `curl -sSL https://zenodo.org/api/records/22178862/files/kyrgyzstan_lakes_dataset_v1.0.0.zip/content`, or unzip the local copy; verify against the SHA-256 recorded at the top.
- **Row counts / coordinate coverage / name languages** — read the CSVs with `encoding="utf-8-sig"`.
- **461 KG lake places, by namespace** — `places/_search`, `term ccodes:KG` + nested `types.identifier` terms filter, `terms` agg on the top-level `namespace` field.
- **15 of 116 names present** — `toponyms/_search`, `terms` on `name.raw` (lower-cased), `terms` agg on the same field.
- **5 km reconciliation** — `_msearch` over the 8 coordinates, `geo_distance` **wrapped in a `nested` query on `geometries`**. `geometries` is a nested field: an unwrapped `geo_distance` on `geometries.repr_point` returns **0 hits for every lake with no error**, which is exactly the silent-zero trap that `filters_must_report_denominator` warns about. It caught me once here; check the denominator.

  This is not specific to lakes. It was carried into `developer/postmortem-ingestion-faults.md` (commit `5661568`) after being reproduced against the live index — the unwrapped query returns 0, the identical query wrapped in `nested` returns 1,149 — and it is logged there as the third instance of one cause: `geometries` is nested and does not announce it, which has also produced a root-level `geom_class` read returning `None` for all 4,363 `nl` records, and a docstring that called `h3_cover` top-level for four months. The gateway itself never issues a `geo_distance` (`grep -rn "geo_distance" gateway/ processing/` is empty), so the trap is latent — it lies in wait for ad-hoc queries and new spatial code, which is exactly what a future session picking up this analysis would be writing. **Read that post-mortem entry before writing any spatial query against `places`.**

- **AAT lake concepts** — `types/_search` on the live `types_20260404_150351`, matching `term` (**not** `prefLabel`, which is why an earlier query here returned 0 and was wrongly recorded as unverifiable) plus `parent_id: 300008680` / `300132301`. 45 hits; the lake branch is the five concepts listed above. ✅ **This caveat is now closed** — the ids are confirmed against the index, not inferred from `typesystem/data/*.json`.
- **LPF conversion** — `authorities/kgld/build_lpf.py --validate`, against whg3's own `validation/static/lpf_v2.0.jsonld` with `csl-citation.json` registered under its `$id` (the published `/schema/` URL 403s). Controls in `README-lpf.md`; a bare PASS proves nothing without them.

One thing I could **not** verify, and which a future session must not take on trust:

- **The `whg` dataset-loading path** is documented from CLAUDE.md and memory, not exercised end-to-end from this side. `whg3-17` has taken a fixture as far as ingestion on both dev and prod, so the route is real; what is untested is *this* file going through it. That is the run now requested.
