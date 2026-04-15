# DGSD Authority Analysis

## Source

**Database:** DGSD v1.1 — Digital Gazetteer of the Song Dynasty, created by Ruth Mostern and Elijah Meeks (UC Merced, 2010, revised 2022). MySQL dump derived primarily from Hope Wright's *An Alphabetical List of Geographical Names in Sung China* (1958).

**SQLite built:** `authorities/dgsd/dgsd.db` (2.1 MB) — 20,151 rows across 11 tables.

**Companion files:**
- `44108_dgsd11.zip` — MySQL SQL dump (3 MB compressed)
- `44108_DGSDshapefiles.zip` — Shapefiles for counties (1,938) and prefectures (652)
- `44108_TheDigitalGazetteerOfTheSongDynasty.pdf` — Documentation

---

## Data Volume

| Table | Rows | WHG relevance |
|-------|------|---------------|
| `entity` | 3,828 | **Core** — named administrative places |
| `historical_instance` | 4,849 | **Core** — temporal snapshots of each entity |
| `attribute` | 7,734 | **Attributes** — population, cantons, markets, mines |
| `point_location` | 3,630 | **Spatial** — coordinates (multiple sources per entity) |
| `feature_type` | 12 | **Types** — Song administrative hierarchy |
| `change_type` | 12 | **Metadata** — types of administrative change events |
| `attribute_type` | 45 | **Metadata** — attribute vocabulary |
| `rank_type` | 33 | **Metadata** — civil/military rank vocabulary |
| `source` | 8 | **Metadata** — coordinate/data provenance |
| `shp_counties` | 1,938 | Denormalised shapefile export |
| `shp_prefectures` | 652 | Denormalised shapefile export |

---

## Shapefiles vs SQL: Replication, Not Supplement

The shapefiles **replicate** the SQL data — they do not add new information:

- Both shapefiles contain **Point geometry** (confirmed from .shp headers) in WGS84 — no polygons
- All 2,590 shapefile records (1,938 counties + 652 prefectures) match SQL `historical_instance` IDs exactly
- Only 5 shapefile entities don't appear in `point_location` (trivial)
- The shapefiles are a pre-joined export of `historical_instance` + `point_location` + `attribute` with denormalised columns (population counts, canton counts, civil/military ranks)
- The SQL has **more entities** (2,006 with coordinates vs 1,763 in shapefiles) because the shapefiles exclude Rank 4 towns, markets, and stockades

**Verdict:** The shapefiles are convenient for GIS visualisation but add no data beyond the SQL. The SQL dump is the canonical source.

---

## Alignment with WHG `places` Index

### ✅ Place Identity
- **3,828 entities** with stable numeric IDs (e.g. `10010`, `14030`)
- Namespace: `dgsd:{entity_id}` → `dgsd:14030`
- Each entity represents a persistent place that may change names, ranks, and administrative parents over time

### ✅ Temporal Coverage — Exceptional (Song-specific)
- Temporal range: **957 – 1274 CE** (Song dynasty focus: 960–1276)
- 4,849 historical instances model administrative changes: establishments, abolitions, name changes, promotions, demotions, mergers, splits, transfers
- Average 1.3 instances per entity; most complex: Jiangyin (江陰) with 12 instances
- Change types: regained (597), transferred (308), name change (216), demoted (160), established (157), promoted (151)
- This granular administrative-change data is **unique among WHG authorities**

### ⚠️ Spatial Coverage — Moderate
- **2,006 entities** have coordinates (52% of 3,828)
- Coordinate precision varies by source:
  - CHGIS-sourced (852 entities): ~2 decimal places (e.g. 119.14, 33.50)
  - Hope Wright (325): integer degrees only (e.g. 113.0, 23.0)
  - DGSD/Tan (2,453 combined): mixed precision
- Geographic extent: 101°–123°E, 18°–40°N (Song China)
- All coordinates are points — no polygons
- **1,822 entities lack coordinates** (mostly Rank 4 garrison markets/towns from Hope Wright's text without georeferencing)

### ✅ Toponyms — Bilingual (Pinyin + Traditional Chinese)
- **3,828 Pinyin romanisations** (100% of entities) — proper modern Pinyin, not Wade-Giles
- **3,763 traditional Chinese names** (98%)
- Historical instances provide **additional name variants** (4,849 names, many different from the entity name due to name changes over the Song period)
- Pinyin names are single-word transcriptions without tone marks (e.g. "Chengdu", "Anfeng", "Jinghubei")

### ✅ Types — Song Administrative Hierarchy
- 12 feature types with Chinese, Pinyin, and English labels, ranked by administrative status:
  - **Rank 1:** circuit (路 lù) — 26 instances
  - **Rank 2:** fu prefecture (府), zhou prefecture (州), jun prefecture (軍), jian prefecture (監) — 653 instances
  - **Rank 3:** county (县 xiàn), jun county (軍) — 1,936 instances
  - **Rank 4:** garrison market (鎮 zhèn), market (塲 chǎng), walled town (城 chéng), stockade (寨 zhài), industrial center (監 jiān) — 2,234 instances
- Civil and military rank vocabularies (33 rank types) provide additional granularity

### ✅ Relations — Temporal Administrative Hierarchy
- `parent_name` references on 3,567 entities (93%) → parent entity IDs, fully resolvable within DGSD
- `historical_instance.prefecture` and `historical_instance.circuit` fields provide **temporally-specific containment** — which prefecture and circuit an entity belonged to at a given time
- This models Song administrative reorganisation (frequent transfers between circuits and prefectures) at a granularity no other gazetteer provides

### ✅ Attributes — Unique Economic/Demographic Data
- 7,734 attributes on 1,813 entities, including:
  - Administrative cantons (鄉 xiāng): 1,953 records
  - Civil/military rank: 1,838 records
  - Population data (戶 hù households, 口 kǒu persons, 丁 dīng adult males): 2,148 records
  - Industrial infrastructure: iron markets (14), salt markets (29), silver markets (83), tin markets (24), tea markets (7), various mines and foundries
- Song-era population figures are valuable historical demographic data

### ⚠️ Links — No external cross-references
- No external IDs within the DGSD data
- However, DGSD sources 852 coordinates from CHGIS — establishing an implicit connection

---

## Comparison with CHGIS/TGAZ

### Overlap

| Metric | DGSD | TGAZ (Song-era) | Overlap |
|--------|------|-----------------|---------|
| Entities | 3,828 | 5,673 | — |
| Distinct Pinyin names | 2,640 | 2,356 | **7** |
| Distinct Chinese names | 3,029 | 6,027 | **22** (all circuit names) |

The **extremely low name overlap** is noteworthy: only 22 Chinese names match, and these are all circuit-level (路) names. This means:

1. **TGAZ and DGSD model different granularities.** TGAZ tracks ~5,700 Song-era placename records which are temporal slices of administrative entities. DGSD has 3,828 persistent entities with 4,849 historical instances — it models the *changes* rather than the *snapshots*.

2. **The two datasets are highly complementary.** DGSD provides Rank 4 entities (garrison markets, markets, stockades, industrial centres — 2,234 instances) that TGAZ largely lacks. TGAZ provides broader temporal coverage (763 BCE – present) and polygonal geometries that DGSD lacks.

3. **Name overlap is low because TGAZ uses the CHGIS naming convention** (full Pinyin with tone-neutral transcription like "Dingxing") while **DGSD uses Hope Wright's transcription** (also Pinyin but sometimes with abbreviations/elisions). Chinese character comparisons are more reliable but the overlap is still low — DGSD uses exclusively traditional characters while TGAZ has both simplified and traditional.

### Complementary strengths

| Feature | DGSD | TGAZ/CHGIS |
|---------|------|------------|
| Temporal focus | Song only (960–1276) | All Chinese history (763 BCE – present) |
| Entity model | Persistent entities + change instances | Place-time snapshots |
| Rank 4 places | ✅ 2,234 towns/markets/stockades | ❌ Few |
| Administrative changes | ✅ Explicit change types | ⚠️ Modelled via succession (prec_by) |
| Population data | ✅ Song-era households + persons | ❌ Not included |
| Industrial sites | ✅ Markets, mines, foundries | ❌ Not included |
| Coordinate precision | ⚠️ 52% coverage, low precision | ✅ 100% coverage, good precision |
| Polygon geometries | ❌ Points only | ✅ 6,000+ polygons |
| Multi-script names | ❌ Pinyin + Traditional Chinese only | ✅ 5 scripts |
| External links | ❌ None | ⚠️ Via Wikidata P4711 |

---

## Suggested `place_id` Scheme

```
dgsd:{entity_id}    e.g.  dgsd:14030
```

The entity ID is the stable numeric key used throughout DGSD and in the shapefiles. It resolves to a persistent place that may have multiple historical instances.

---

## Ingestion Considerations

### Should DGSD be ingested as a standalone authority?

**Recommendation: Yes, but as a supplement to CHGIS, not independently.**

Reasons in favour:
1. **2,234 Rank 4 entities** (garrison markets, commodity markets, stockades, industrial centres) provide Song sub-county data not available in any other WHG authority
2. **Administrative change events** are unique metadata that WHG's temporal relations model can capture
3. **Song-era population and economic data** (households, population, industrial infrastructure) is valuable historical attribute data

Concerns:
1. Only 2,006 entities have coordinates (52%), and many are low-precision (integer degrees from Hope Wright)
2. The dataset is small (3,828 entities) — modest impact on index size
3. No external cross-references for clustering

### Ingestion approach

If ingested, DGSD should:
- Be ingested as namespace `dgsd:` with entity IDs
- Use `historical_instance` records to populate `timespans[]` (begin_date/end_date per instance)
- Map feature types to the AAT hierarchy (circuit → administrative division, prefecture → province, county → county, etc.)
- Use the highest-priority coordinates from `point_location` (priority=1 preferred)
- Include historical instance names as additional toponyms (Pinyin + Traditional Chinese per instance)
- Populate `relations[]` with parent/child and temporal hierarchy from `historical_instance.prefecture` and `historical_instance.circuit`
- Link to CHGIS entities where DGSD uses CHGIS-sourced coordinates (852 entities) — potential hard links

---

## Estimated WHG Impact

| Index | New documents | Notes |
|-------|--------------|-------|
| `places` | ~3.8K | Small but fills a gap in Song sub-county data |
| `toponyms` | ~8.7K | ~2 forms per entity (Pinyin + Chinese) + historical variants |
| `clusters` | Moderate | Name overlap with CHGIS is low; spatial/temporal matching more promising |

---

## Summary Verdict

**DGSD is a useful complement to CHGIS/TGAZ**, particularly for:
- Song-dynasty administrative geography below the county level
- Administrative change events (a temporal model no other source provides)
- Song-era economic infrastructure (markets, mines, foundries)
- Historical population data

It is **not a high priority** for ingestion compared to the main CHGIS/TGAZ dataset (82K records) or the major gazetteers (GeoNames 13M, Wikidata 11M), but it fills a specific niche that no other WHG authority covers. If ingested, it should be treated as a small specialist authority similar to Pleiades (37K records) or Index Villaris (24K records), but at ~3.8K records it is smaller than either.

The coordinate precision issue (many integer-degree locations) means that spatial clustering will be unreliable for Hope Wright-sourced points. The CHGIS-sourced coordinates (852 entities) are higher quality and provide the best candidates for cross-linking.

