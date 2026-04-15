# CHGIS/TGAZ Authority Analysis

## Source

**Database:** `tgaz_dev` — Temporal Gazetteer of Administrative Entities (TGAZ), the successor to the China Historical GIS (CHGIS), a Harvard/Fudan University joint project. MySQL 8.2 dump dated 2025-03-29.

**SQLite built:** `authorities/chgis/tgaz.db` (108 MB) — 816,456 rows across 23 tables.

---

## Data Volume

| Table | Rows | WHG relevance |
|-------|------|---------------|
| `placename` | 82,117 | **Core** — each row is one place-time entity |
| `spelling` | 245,042 | **Core** — toponym forms in multiple scripts |
| `part_of` | 83,400 | **Relations** — temporal hierarchical containment |
| `present_loc` | 82,117 | **Spatial** — present-day location + country code |
| `prec_by` | 10,231 | **Relations** — temporal succession ("preceded by") |
| `ftype` | 898 | **Types** — feature type vocabulary with ADL classes |
| `snote` | 23,359 | Scholarly notes per placename |
| `v6_id` / `v5_id` | 82,858 / 77,769 | CHGIS version ID cross-references |
| `mv_pn_srch` | 82,117 | Materialised search view (redundant) |

---

## Alignment with WHG `places` Index

### ✅ Place Identity
- **81,292 distinct `sys_id`** values (e.g. `hvd_1`, `hvd_70685`)
- Namespace: `chgis:{sys_id}` → `chgis:hvd_70685`
- Three sub-corpora by `data_src`: **CHGIS** (77,769), **TBRC** (3,587 Tibetan places), **HGR** (761 Russian historical places)

### ✅ Temporal Coverage — Exceptional
- Every record has `beg_yr` and `end_yr` (100% coverage)
- Temporal range: **763 BCE – 9999** (9999 = "still existing")
- Uses a 10-level dating rule system (`drule`) tracking precision from "pan-Dynastic period" (rule 1) to "specific date" (rule 6)
- This is one of the richest temporal datasets in the WHG corpus — the `timespans[]` nested field will be fully populated

### ✅ Spatial Coverage — Excellent
- **82,117** records have coordinates (100%)
- 76,060 POINT, 6,004 POLYGON, 53 with no `obj_type`
- Coordinates are in decimal degrees (e.g. `116.39525, 39.10154`)
- Geographic focus: China (81,356), Russia (553), Ukraine (139), Belarus (34), plus Baltic/Central Asian edges
- `present_loc` provides modern location text and **ISO 2-letter country codes** → maps directly to `ccodes[]`

### ✅ Toponyms — Multilingual, Multi-Script
- **245,042 spelling records** across 5 writing systems:
  - n/a (romanised/English): 81,537
  - Simplified Chinese (zh): 81,221
  - Traditional Chinese (zh-Hant): 77,769
  - Tibetan (bo): 2,993
  - Cyrillic (ru): 1,522
- Transliteration systems: Pinyin (77,769), Tibetan Wylie (3,007), Russian ALA-LC (761)
- Average ~3 spellings per place → rich toponym attestation for the `toponyms` index
- Each spelling carries `script_id`, `trsys_id` (transliteration system), `exonym_lang`, `attested_by`

### ✅ Types — Rich Historical Vocabulary
- 898 feature types with Chinese (`name_vn`), pinyin (`name_tr`), English (`name_en`), and ADL class
- Top types by usage: county (县, 12,752), prefecture (州, 3,804), commandery (郡, 2,938), prefecture (府, 1,462)
- **ADL** = Alexandria Digital Library Feature Type Thesaurus — a standard geographic feature classification developed at UC Santa Barbara (now largely superseded by TGN and AAT). Each TGAZ feature type carries an `adl_class` label (e.g. "administrative areas", "populated places", "hydrographic features") — there are 107 distinct ADL classes across 898 types
- The ADL classes are **not explicitly mapped to AAT** in the data (the `ld_uri` column is empty for all 898 types). However, many ADL class labels have obvious AAT equivalents (e.g. "administrative areas" → AAT 300387176, "populated places" → AAT 300008347), so **a mapping could be constructed** as part of the WHG type system pipeline
- Period-specific types: "commandery" (Qin–Sui), "circuit" (Song), "banner" (Qing) — this temporal typing is unique among WHG authorities

### ✅ Relations — Temporal Hierarchy + Succession
- **`part_of`** (83,400 rows): Temporally-scoped parent-child containment with begin/end years
  → maps to `relations[{relation_type: "part_of", related_place_id, timespans}]`
- **`prec_by`** (10,231 rows): Temporal succession (place A was preceded by place B)
  → maps to `relations[{relation_type: "preceded_by", related_place_id}]`
- 62,487 places have at least one parent; 7,324 distinct parent entities

### ⚠️ Links — Empty in dump, but recoverable via Wikidata P4711
- The `link` and `wkt_definition` tables are empty in this dump
- No external cross-references within the TGAZ data itself

- **However:** Wikidata has property **P4711** ("CHGIS ID") on **5,674 items**.
  The P4711 values are plain numeric IDs that match `data_src_ref` in the
  `placename` table (and the numeric suffix of `hvd_` sys_ids). This gives us
  a direct CHGIS → Wikidata concordance for ~7% of the corpus.
  - Of those 5,674 Wikidata items, only 21 also carry GeoNames IDs (P1566) —
    so GeoNames cross-linking is sparse via this route.
  - The Wikidata links are more valuable anyway: our `wd:` authority already
    indexes ~11M Wikidata places, so 5,674 hard links from `chgis:` → `wd:`
    feed directly into the clustering pipeline.
  - `build_database.py --fetch-wikidata` queries the Wikidata SPARQL endpoint
    for all P4711 values and stores the Q-ID mappings in a `wikidata_links`
    table (also captures GeoNames IDs via P1566 when present).

---

## Suggested `place_id` Scheme

```
chgis:{sys_id}    e.g.  chgis:hvd_70685
```

The `sys_id` is the stable identifier used across CHGIS versions. The `v5_id`/`v6_id` tables confirm which IDs existed in each version.

---

## Estimated WHG Impact

| Index | New documents | Notes |
|-------|--------------|-------|
| `places` | ~82K | Every record has temporal, spatial, and type metadata |
| `toponyms` | ~134K distinct forms | Multi-script (CJK + Cyrillic + romanised) — excellent for Symphonym |
| `clusters` | High potential | Administrative entities cluster well with GeoNames ADM records and Wikidata Q-items for Chinese places |

---

## Romanisation, Symphonym, and Chinese

### The question

> "Does Symphonym know how to Romanize Chinese — that would be a very useful feature."

### Short answer

**The data already has romanised forms — and Symphonym doesn't romanise anything, but it doesn't need to.**

### 1. The data already includes romanisations

Yes, **Pinyin is romanised Chinese.** It's the standard Latin-script transliteration system for Mandarin Chinese (ISO 7098). The TGAZ database already has comprehensive romanisation coverage:

| Sub-corpus | Records | Romanisation | System | Coverage |
|------------|---------|-------------|--------|----------|
| **CHGIS** (Chinese places) | 77,769 | ✅ Pinyin | `trsys=py` | **100%** — every record |
| **TBRC** (Tibetan places) | 3,587 | ✅ Wylie transliteration | `trsys=tib_wylie` | **84%** (3,007 of 3,587) |
| **HGR** (Russian places) | 761 | ✅ ALA-LC romanisation | `trsys=ru_lc` | **100%** — every record |

**99.3% of all 82,117 placenames already have a romanised form.** Only 580 TBRC records (Tibetan Buddhist monasteries with only simplified Chinese names and no Tibetan script or Wylie) lack romanisation.

Sample data showing the three forms per CHGIS place:

| Traditional Chinese | Simplified Chinese | Pinyin |
|--------------------|--------------------|--------|
| 定興縣 | 定兴县 | Dingxing |
| 深澤縣 | 深泽县 | Shenze |
| 曲陽縣 | 曲阳县 | Quyang |

### 2. What Symphonym actually does (and why it's even better)

Symphonym is not a romanisation tool — it's a **phonetic similarity model**. It takes a toponym in *any* script and produces a 128-dimensional embedding vector that captures how the name *sounds*, enabling cross-script matching. The distinction matters:

- **Romanisation** converts 北京 → "Běijīng" (a specific transliteration standard)
- **Symphonym** converts 北京 → `[23, -8, 47, ...]` (a phonetic fingerprint)

The phonetic fingerprint lets Symphonym match "Beijing", "Peking", "Pékin", "بكين", and "Пекин" as phonetically similar — something romanisation alone cannot do.

### 3. How Symphonym already handles Chinese

The WHG toponym pipeline already has **CharsiuG2P** (a neural grapheme-to-phoneme model) specifically for Chinese characters. When CHGIS records enter the pipeline:

1. The **Pinyin forms** go through Epitran (Latin → IPA) — straightforward
2. The **Chinese character forms** (simplified + traditional) go through CharsiuG2P (CJK → IPA) — handles Mandarin, Cantonese, Gan, and Wu pronunciations
3. Both IPA representations feed into Symphonym for embedding generation

So Symphonym already "understands" Chinese — not by romanising it, but by converting characters directly to phonetic representations via a neural G2P model, which is more powerful than romanisation because it captures pronunciation rather than orthographic convention.

### 4. The 580 missing romanisations

The 580 TBRC records without romanisation have only simplified Chinese names for Tibetan monasteries (e.g. 拉扎寺, 天觉林寺). These are Chinese phonetic approximations of Tibetan names.

**Pinyin could be generated trivially** using the `pypinyin` library:
```python
from pypinyin import pinyin, Style
pinyin('拉扎寺', style=Style.NORMAL)  # → [['lā'], ['zhā'], ['sì']]
```

However, this is low priority because:
- Symphonym's CharsiuG2P will process these characters directly during embedding generation
- The generated Pinyin would be a romanisation of a *Chinese approximation* of a Tibetan name — not very useful for matching
- It's only 580 records (0.7% of the corpus)

**Verdict:** The romanisation data is already there. No generation step is needed before ingestion.

---

## Summary Verdict

**CHGIS/TGAZ is an excellent candidate for WHG ingestion.** It offers:

1. **Complete temporal coverage** — 100% of records have begin/end years, with precision metadata
2. **Complete spatial coverage** — 100% have coordinates, plus POLYGON geometries for 6K+ regions
3. **Rich multilingual toponyms** — 3 spellings per place on average across 5 scripts
4. **Temporal-hierarchical relations** — rare among gazetteers; directly models administrative reorganisation
5. **Historical feature types** — period-specific Chinese administrative terminology with English translations
6. **~82K records** — a meaningful addition (comparable to Pleiades at 37K, larger than several existing authorities)

The main gap is external cross-links, which could be addressed later with concordance tables or spatial/temporal matching during clustering.

