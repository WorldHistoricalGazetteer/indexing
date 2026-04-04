# OHM (OpenHistoricalMap) Tag Inventory for WHG Place Type Mapping

> **Purpose:** Comprehensive catalogue of all OHM primary feature tag
> keys and values relevant to a historical gazetteer, with AAT mapping
> assessments, temporal coverage statistics, and implementation priority.
>
> **Status:** Reference document.  Feeds into `type-mapping-plan.md`
> and guides the future `authorities/ohm-places.py` ingestion script.
>
> **Last updated:** 4 April 2026

---

## 1. What is OpenHistoricalMap?

**OpenHistoricalMap (OHM)** is a community-built, time-aware geographic
database using the OpenStreetMap data model and stack.  Where OSM
captures *current* features, OHM focuses on **historical features with
temporal extent** — places, boundaries, buildings, and infrastructure
that existed in the past but may no longer exist (or whose names, types,
or boundaries have changed over time).

- **Website:** <https://www.openhistoricalmap.org>
- **API:** OHM runs its own Overpass instance, OSM API clone, and
  (sometimes) TagInfo instance.
- **License:** ODbL (same as OSM).
- **Data model:** Identical to OSM — nodes, ways, relations with free-
  form `key=value` tags.  The key difference is the systematic use of
  `start_date` and `end_date` tags for temporal extent.

### 1.1 Why OHM for WHG?

OHM is uniquely valuable for a *historical* gazetteer because:

1. **Temporal coverage:** Features systematically carry `start_date`
   and `end_date` tags, mapping directly to WHG's `timespans` fields.
2. **Historical depth:** Contains castles, forts, ancient roads, defunct
   railways, historical boundaries, abolished parishes, disappeared
   towns — features that no longer exist in OSM.
3. **Community-curated names:** Carries historical name forms
   (`name:date`, alternate historical names).
4. **Complementary to OSM:** While OSM provides the modern ~18M place
   baseline, OHM adds the diachronic dimension that WHG scholars need.
5. **Moderate size:** ~1.7M named features — large enough to be
   valuable, small enough to process as a single authority.

### 1.2 Key Differences from OSM

| Aspect | OSM | OHM |
|--------|-----|-----|
| Temporal scope | Present only | Past + present |
| `start_date`/`end_date` | Rare (~2%) | Systematic (~50%+ on curated features) |
| Total named features | ~260M | ~1.7M |
| Focus | Infrastructure & navigation | Historical geography |
| Feature types | Same tag vocabulary | Same, plus `historic=*` is much more prominent |

---

## 2. Data Source and Methodology

All tag value inventories and feature counts in this document are
sourced from the **OHM Overpass API**
(`https://overpass-api.openhistoricalmap.org/api/interpreter`),
queried on 4 April 2026.  Raw JSON data is archived at
`developer/ohm-taginfo-data.json`.

**Methodology:** OHM's TagInfo API blocks programmatic access, so
counts were obtained via Overpass `out count` queries, and value
distributions via `out tags` sampling (up to 5,000 features per key).
The fetch script is at `scripts/fetch_ohm_taginfo.py`.

**Global totals (4 April 2026):**

| Element type | Named features |
|-------------|---------------:|
| Nodes | 245,378 |
| Ways | 1,308,559 |
| Relations | 119,431 |
| **Total** | **1,673,368** |

---

## 3. Tier Structure

| Tier | Description | Est. named features | Implementation |
|------|-------------|--------------------:|---------------|
| **Tier 1** | Core place-like features: settlements, boundaries, historic sites, natural features, waterways | ~825K | First pass |
| **Tier 2** | High-priority infrastructure & amenities with historical depth: amenities, tourism, leisure, man-made, military, landuse, railways | ~192K | Second pass |
| **Tier 3** | Moderate-priority built environment: buildings, shops, offices, bridges, tunnels, aeroway, power, healthcare | ~158K | Selective |
| **Tier 4** | Low-priority: geological (tiny count) | ~12 | Deferred |

**Tier assignment criteria** (same as the OSM inventory):
- Would a scholar or general user look this up in a gazetteer?
- Does the feature type have historical depth?
- Does AAT have a plausible mapping concept?
- How many named features carry this tag?

**Key difference from OSM:** OHM's temporal coverage is a major
advantage — features with `start_date`/`end_date` map directly to
WHG `timespans`, making OHM records significantly richer than typical
OSM imports.

---

## 4. Tier 1: Core Place Features

### 4.1 `place`

**131,257 named features**, 13.7% with temporal tags, 15 distinct values.

The core settlement hierarchy — same tag vocabulary as OSM but focused
on historical places.  Low temporal tag rate suggests many are modern
settlements with implicit "still exists" semantics.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `village` | 3,784 | 75.7 | villages | 300008372 |
| `town` | 542 | 10.8 | towns | 300008375 |
| `locality` | 340 | 6.8 | inhabited places | 300008347 |
| `city` | 175 | 3.5 | cities | 300008389 |
| `hamlet` | 82 | 1.6 | hamlets | 300008197 |
| `island` | 23 | 0.5 | islands (landforms) | 300008791 |
| `suburb` | 20 | 0.4 | suburbs | 300000745 |
| `islet` | 16 | 0.3 | islets | 300008792 |
| `neighbourhood` | 8 | 0.2 | neighborhoods | 300000745 |
| `isolated_dwelling` | 2 | 0.0 | dwellings | 300005433 |
| `farm` | 1 | 0.0 | farms (inhabited places) | 300000206 |

**AAT coverage:** ~98% — excellent. Same mappings as OSM `place=*`.

### 4.2 `historic`

**5,998 named features**, 69.4% with temporal tags, 83 distinct values.

The most distinctively "OHM" key — rich in features that no longer
exist.  Nearly 70% carry temporal metadata, the highest rate among
non-infrastructure keys.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `boundary_stone` | 1,163 | 23.3 | boundary markers | 300006973 |
| `castle` | 717 | 14.3 | castles (fortifications) | 300006891 |
| `memorial` | 605 | 12.1 | memorials | 300006812 |
| `city_gate` | 585 | 11.7 | city gates | 300002779 |
| `monument` | 279 | 5.6 | monuments | 300006958 |
| `archaeological_site` | 184 | 3.7 | archaeological sites | 300000810 |
| `fort` | 174 | 3.5 | forts | 300006909 |
| `manor` | 163 | 3.3 | manor houses | 300005961 |
| `mine` | 155 | 3.1 | mines (extractive complexes) | 300000383 |
| `crash_site` | 115 | 2.3 | — | — |
| `ruins` | 97 | 1.9 | ruins | 300008057 |
| `building` | 87 | 1.7 | buildings (structures) | 300004792 |
| `ship` | 53 | 1.1 | shipwrecks | 300266038 |
| `roman_road` | 48 | 1.0 | Roman roads | 300008300 |
| `church` | 44 | 0.9 | churches (buildings) | 300007466 |
| `trail` | 40 | 0.8 | trails | — |
| `tomb` | 32 | 0.6 | tombs | 300005926 |
| `heritage` | 32 | 0.6 | — | — |
| `bunker` | 29 | 0.6 | bunkers | 300006926 |
| `mill` | 21 | 0.4 | mills (buildings) | 300006252 |

**AAT coverage:** ~80% — very good for the high-frequency values.
Values like `crash_site`, `heritage`, `yes` lack clear AAT mappings.

### 4.3 `boundary`

**87,802 named features**, 12.0% with temporal tags, 16 distinct values.

Predominantly administrative boundaries — extremely valuable for WHG's
administrative hierarchy.  The `admin_level` tag provides granularity.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `administrative` | 3,311 | 66.2 | administrative divisions | 300000745 |
| `marker` | 889 | 17.8 | boundary markers | 300006973 |
| `religious_administration` | 347 | 6.9 | dioceses / parishes | 300000694 |
| `national_park` | 236 | 4.7 | national parks | 300008069 |
| `civil_parish` | 118 | 2.4 | parishes | 300000778 |
| `protected_area` | 26 | 0.5 | reserves (protected sites) | — |
| `political` | 10 | 0.2 | political divisions | 300000745 |
| `census` | 10 | 0.2 | census districts | — |
| `landuse` | 7 | 0.1 | — | — |
| `country_border` | 5 | 0.1 | international boundaries | — |
| `historic_county` | 2 | 0.0 | counties | 300000771 |

**AAT coverage:** ~75%. `administrative` subdivides further by
`admin_level` (1=country, 2=state, 4=county, etc.) which can refine
AAT mappings.

**Note:** Historical boundaries are a flagship OHM feature — e.g.
the Holy Roman Empire, pre-colonial African kingdoms, historical US
county boundaries.  The 12% temporal rate likely understates coverage
since many boundaries carry dates via the relation members rather than
the relation itself.

### 4.4 `natural`

**53,272 named features**, 9.0% with temporal tags, 40 distinct values.

Natural features — mostly modern-era but valuable for geographic
anchoring.  Same vocabulary as OSM.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `peak` | 2,167 | 43.3 | peaks (landforms) | 300008816 |
| `coastline` | 889 | 17.8 | coastlines | — |
| `cape` | 476 | 9.5 | capes | 300008850 |
| `spring` | 322 | 6.4 | springs | 300008697 |
| `water` | 308 | 6.2 | bodies of water | 300008680 |
| `saddle` | 165 | 3.3 | cols / saddles | — |
| `tree` | 152 | 3.0 | — | — |
| `bay` | 142 | 2.8 | bays | 300132316 |
| `cave_entrance` | 80 | 1.6 | caves | 300008746 |
| `beach` | 71 | 1.4 | beaches | — |
| `waterfall` | 40 | 0.8 | waterfalls | 300008736 |
| `cliff` | 39 | 0.8 | cliffs | 300008749 |
| `peninsula` | 17 | 0.3 | peninsulas | 300008804 |
| `hill` | 15 | 0.3 | hills | — |
| `volcano` | 14 | 0.3 | volcanoes | 300132325 |
| `rock` | 14 | 0.3 | — | — |
| `strait` | 9 | 0.2 | straits | 300266559 |
| `reef` | 9 | 0.2 | reefs | 300008808 |

**AAT coverage:** ~70% for named features.

### 4.5 `waterway`

**546,757 named features**, 1.1% with temporal tags, 34 distinct values.

The largest single key in OHM by named feature count — dominated by
river geometry.  Very low temporal coverage (rivers don't change much).

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `river` | 3,105 | 62.1 | rivers | 300008707 |
| `lock_gate` | 627 | 12.5 | lock gates | — |
| `waterfall` | 537 | 10.7 | waterfalls | 300008736 |
| `weir` | 434 | 8.7 | weirs | 300006164 |
| `dam` | 96 | 1.9 | dams | 300006072 |
| `rapids` | 52 | 1.0 | rapids | — |
| `lock` | 34 | 0.7 | locks (hydraulic structures) | — |
| `dock` | 18 | 0.4 | docks | 300120665 |
| `riverbank` | 17 | 0.3 | — | — |
| `stream` | 16 | 0.3 | streams | 300008699 |
| `canal` | 8 | 0.2 | canals (waterways) | 300006075 |

**AAT coverage:** ~65% for high-frequency values.

---

## 5. Tier 2: High-Priority Infrastructure & Amenities

### 5.1 `amenity`

**55,607 named features**, 78.7% with temporal tags, 87 distinct values.

Rich in historically significant places — post offices, ferry
terminals, places of worship, schools.  Very high temporal coverage.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `post_office` | 2,122 | 42.4 | post offices | 300007015 |
| `ferry_terminal` | 710 | 14.2 | ferry terminals | — |
| `place_of_worship` | 467 | 9.3 | religious buildings | 300007391 |
| `school` | 329 | 6.6 | schools (buildings) | 300007141 |
| `restaurant` | 181 | 3.6 | restaurants | 300005182 |
| `theatre` | 131 | 2.6 | theaters (buildings) | 300007117 |
| `pub` | 119 | 2.4 | public houses | 300005214 |
| `bank` | 85 | 1.7 | banks (buildings) | 300005226 |
| `hospital` | 76 | 1.5 | hospitals | 300007145 |
| `cinema` | 69 | 1.4 | movie theaters | — |
| `fuel` | 48 | 1.0 | gas stations | — |
| `cafe` | 47 | 0.9 | cafés | 300005181 |
| `townhall` | 46 | 0.9 | town halls | 300007017 |
| `library` | 39 | 0.8 | libraries (buildings) | 300007145 |
| `nightclub` | 35 | 0.7 | nightclubs | — |
| `pharmacy` | 31 | 0.6 | pharmacies | 300005204 |
| `courthouse` | 29 | 0.6 | courthouses | 300007028 |
| `police` | 28 | 0.6 | police stations | 300007051 |
| `fire_station` | 24 | 0.5 | fire stations | 300007056 |

**AAT coverage:** ~70% — good for historically significant values
(place_of_worship, school, hospital, theatre).  Modern amenities
(fuel, nightclub) are less relevant for a historical gazetteer.

**Suggested allowlist for WHG:** `place_of_worship`, `school`,
`hospital`, `theatre`, `library`, `courthouse`, `townhall`,
`fire_station`, `police`, `post_office`, `bank`, `ferry_terminal`,
`marketplace`, `prison`.

### 5.2 `tourism`

**8,847 named features**, 92.3% with temporal tags, 24 distinct values.

Hotels, museums, and attractions — extremely high temporal coverage
(92%!), suggesting strong editorial curation.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `hotel` | 1,618 | 32.4 | hotels | 300007012 |
| `guest_house` | 892 | 17.8 | guesthouses | 300005402 |
| `camp_site` | 803 | 16.1 | campsites | — |
| `museum` | 574 | 11.5 | museums (buildings) | 300005768 |
| `artwork` | 457 | 9.1 | public art | — |
| `attraction` | 338 | 6.8 | tourist attractions | — |
| `information` | 89 | 1.8 | — | — |
| `gallery` | 77 | 1.5 | galleries (buildings) | 300005768 |
| `viewpoint` | 46 | 0.9 | — | — |

**AAT coverage:** ~60%. `museum` and `gallery` have strong AAT
mappings.  Modern tourism concepts (camp_site, viewpoint) are weaker.

### 5.3 `leisure`

**6,909 named features**, 79.9% with temporal tags, 47 distinct values.

Parks, gardens, sports venues — good historical depth for parks and
nature reserves.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `park` | 2,706 | 54.1 | parks (grounds) | 300008069 |
| `nature_reserve` | 483 | 9.7 | nature reserves | — |
| `garden` | 352 | 7.0 | gardens (grounds) | 300008090 |
| `sports_centre` | 211 | 4.2 | sports facilities | — |
| `stadium` | 205 | 4.1 | stadiums | 300007205 |
| `pitch` | 185 | 3.7 | — | — |
| `slipway` | 123 | 2.5 | slipways | — |
| `marina` | 82 | 1.6 | marinas | 300120670 |
| `golf_course` | 68 | 1.4 | golf courses | — |
| `swimming_pool` | 57 | 1.1 | swimming pools | — |
| `playground` | 57 | 1.1 | — | — |

**AAT coverage:** ~40%. Parks and gardens map well.  Modern sports
facilities are weakly mapped.

### 5.4 `man_made`

**5,524 named features**, 87.5% with temporal tags, 81 distinct values.

Works, bridges, towers, mines — rich in historically significant
structures.  Very high temporal coverage.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `works` | 1,486 | 29.7 | factories | 300006232 |
| `tower` | 823 | 16.5 | towers (buildings) | 300004847 |
| `bridge` | 814 | 16.3 | bridges | 300007836 |
| `mineshaft` | 309 | 6.2 | mine shafts | — |
| `pier` | 250 | 5.0 | piers | 300120675 |
| `windmill` | 172 | 3.4 | windmills | 300006268 |
| `watermill` | 152 | 3.0 | water mills | 300006267 |
| `adit` | 84 | 1.7 | adits | — |
| `lighthouse` | 82 | 1.6 | lighthouses | 300007741 |
| `geoglyph` | 71 | 1.4 | geoglyphs | — |
| `pipeline` | 53 | 1.1 | — | — |
| `water_well` | 42 | 0.8 | wells (water sources) | 300006194 |
| `water_tower` | 36 | 0.7 | water towers | 300007103 |
| `dyke` | 33 | 0.7 | dikes (structures) | 300006175 |

**AAT coverage:** ~65% — good for major structures.

### 5.5 `military`

**995 named features**, 96.5% with temporal tags, 30 distinct values.

Very high temporal coverage (97%) — almost every military feature is
temporally scoped.  Extremely relevant for historical gazetteer.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `tower` | 202 | 20.3 | watchtowers | 300004847 |
| `barracks` | 150 | 15.1 | barracks | 300006898 |
| `base` | 147 | 14.8 | military bases | — |
| `bunker` | 102 | 10.3 | bunkers | 300006926 |
| `trench` | 101 | 10.2 | trenches | 300006940 |
| `airfield` | 100 | 10.1 | airfields | 300007036 |
| `office` | 33 | 3.3 | — | — |
| `training_area` | 14 | 1.4 | training grounds | — |
| `range` | 12 | 1.2 | — | — |
| `fort` | 10 | 1.0 | forts | 300006909 |
| `fortification` | 7 | 0.7 | fortifications | 300006888 |
| `bastion` | 6 | 0.6 | bastions | 300004690 |

**AAT coverage:** ~70%. Fortification-related terms map well to AAT.

### 5.6 `landuse`

**10,376 named features**, 79.5% with temporal tags, 56 distinct values.

Land use classifications with good temporal depth — useful for
historical land use patterns.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `residential` | 979 | 19.6 | residential areas | — |
| `highway` | 761 | 15.2 | highways | 300008270 |
| `military` | 641 | 12.8 | military installations | — |
| `cemetery` | 442 | 8.8 | cemeteries | 300005781 |
| `industrial` | 347 | 6.9 | industrial areas | — |
| `forest` | 288 | 5.8 | forests | 300008838 |
| `meadow` | 239 | 4.8 | meadows | — |
| `farmland` | 174 | 3.5 | farmland | — |
| `construction` | 147 | 2.9 | — | — |
| `reservoir` | 109 | 2.2 | reservoirs | 300006191 |
| `commercial` | 94 | 1.9 | commercial areas | — |
| `quarry` | 65 | 1.3 | quarries | 300000445 |
| `allotments` | 58 | 1.2 | — | — |
| `recreation_ground` | 57 | 1.1 | recreation grounds | — |
| `orchard` | 54 | 1.1 | orchards | — |
| `religious` | 46 | 0.9 | religious precincts | — |
| `railway` | 40 | 0.8 | railway yards | — |

**AAT coverage:** ~45%. `cemetery`, `quarry`, `forest`, and `reservoir`
have clear AAT counterparts.

### 5.7 `railway`

**103,881 named features**, 90.8% with temporal tags, 17 distinct values.

Railway stations and stops — the second largest key by volume.
Extremely high temporal coverage (91%), reflecting OHM's strong
railway-history community.  Historically very important: defunct
stations are a major class of "disappeared places."

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `tram_stop` | 2,328 | 46.6 | streetcar stops | — |
| `station` | 1,889 | 37.8 | railroad stations | 300007783 |
| `halt` | 318 | 6.4 | railway halts | — |
| `stop` | 252 | 5.0 | — | — |
| `site` | 109 | 2.2 | — | — |
| `service_station` | 63 | 1.3 | — | — |
| `junction` | 16 | 0.3 | junctions | — |

**AAT coverage:** ~40%. `station` maps to AAT 300007783 (railroad
stations).  Tram stops and halts are less well covered.

**Note:** Railway features represent a disproportionately large share
of OHM data.  A WHG import might focus on `station` + `halt` only,
which would still yield ~45K well-dated named features.

---

## 6. Tier 3: Built Environment & Infrastructure

### 6.1 `building`

**53,366 named features**, 93.1% with temporal tags, 68 distinct values.

Named buildings with dates — the generic `yes` value dominates but
specific values like `church`, `commercial`, `university` are
gazetteer-relevant.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `yes` | 3,311 | 66.2 | buildings (structures) | 300004792 |
| `residential` | 392 | 7.8 | houses | 300005433 |
| `church` | 239 | 4.8 | churches (buildings) | 300007466 |
| `commercial` | 219 | 4.4 | commercial buildings | 300004790 |
| `house` | 148 | 3.0 | houses | 300005433 |
| `public` | 114 | 2.3 | public buildings | — |
| `university` | 74 | 1.5 | university buildings | 300007143 |
| `school` | 52 | 1.0 | schools (buildings) | 300007141 |
| `warehouse` | 36 | 0.7 | warehouses | 300005230 |
| `industrial` | 33 | 0.7 | industrial buildings | — |
| `chapel` | 25 | 0.5 | chapels | 300004590 |
| `hospital` | 18 | 0.4 | hospitals | 300007145 |
| `train_station` | 17 | 0.3 | railroad stations | 300007783 |
| `cathedral` | 16 | 0.3 | cathedrals | 300007501 |

**AAT coverage:** ~55% for specific building types.

**Suggested allowlist:** `church`, `cathedral`, `chapel`, `mosque`,
`synagogue`, `university`, `school`, `hospital`, `warehouse`,
`train_station`, `castle`, `palace`, `temple`, `monastery`, `prison`.

### 6.2 `shop`

**8,396 named features**, 97.8% with temporal tags, 180 distinct values.

Extremely high temporal coverage but many values are modern retail
categories of limited gazetteer relevance.  Historical shops (general
stores, blacksmiths, etc.) could be interesting.

**Top values:** `convenience` (13%), `clothes` (10%),
`general` (8%), `supermarket` (6%), `hairdresser` (5%),
`butcher` (4%), `bakery` (3%).

**AAT coverage:** ~20%.  Low priority for WHG type mapping.

### 6.3 `office`

**1,843 named features**, 96.6% with temporal tags, 63 distinct values.

Government offices, company headquarters — moderate gazetteer
relevance.

**Top values:** `government` (32%), `company` (12%),
`association` (7%), `insurance` (5%).

**AAT coverage:** ~25% (`government` → government buildings).

### 6.4 `bridge`

**68,215 named features**, 92.8% with temporal tags, 10 distinct values.

Named bridges — large volume, very high temporal coverage.  Bridges
are significant landmarks, especially in historical contexts.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `yes` | 4,776 | 95.5 | bridges | 300007836 |
| `viaduct` | 94 | 1.9 | viaducts | 300007838 |
| `aqueduct` | 52 | 1.0 | aqueducts | 300006165 |
| `boardwalk` | 26 | 0.5 | — | — |
| `movable` | 20 | 0.4 | — | — |
| `covered` | 14 | 0.3 | covered bridges | — |
| `trestle` | 10 | 0.2 | trestle bridges | — |

**AAT coverage:** ~95% (almost all are `yes` → bridges).

### 6.5 `tunnel`

**24,113 named features**, 1.5% with temporal tags, 6 distinct values.

Named tunnels — significant volume but very low temporal coverage.

| Value | Sample count | % | AAT concept | AAT ID |
|-------|------------:|--:|-------------|--------|
| `yes` | 4,913 | 98.3 | tunnels | 300007899 |
| `building_passage` | 58 | 1.2 | — | — |

**AAT coverage:** ~98% (almost all `yes` → tunnels).

### 6.6 `aeroway`

**841 named features**, 97.7% with temporal tags, 12 distinct values.

Small but very well-dated — historical airfields and airports.

**Top values:** `aerodrome` (82%), `helipad` (5%), `terminal` (4%).

**AAT concept:** aerodromes → airfields (300007036).

### 6.7 `power`

**808 named features**, 81.3% with temporal tags, 11 distinct values.

Power stations and substations — small volume.

**Top values:** `plant` (62%), `substation` (23%), `line` (6%).

**AAT concept:** `plant` → power plants; limited AAT coverage.

### 6.8 `healthcare`

**1,006 named features**, 95.5% with temporal tags, 21 distinct values.

Hospitals, clinics, doctors — overlaps significantly with
`amenity=hospital`.

**Top values:** `hospital` (35%), `doctor` (18%), `clinic` (13%).

---

## 7. Temporal Coverage Summary

One of OHM's most distinctive qualities for WHG is systematic temporal
tagging.  Summary across sampled features:

| Key | Named features | % with start_date | % with end_date | % with either |
|-----|---------------:|------------------:|----------------:|--------------:|
| `military` | 995 | 96% | 67% | 97% |
| `shop` | 8,396 | — | — | 98% |
| `aeroway` | 841 | — | — | 98% |
| `office` | 1,843 | — | — | 97% |
| `healthcare` | 1,006 | — | — | 96% |
| `building` | 53,366 | 92% | 36% | 93% |
| `bridge` | 68,215 | — | — | 93% |
| `tourism` | 8,847 | 92% | 72% | 92% |
| `railway` | 103,881 | 88% | 58% | 91% |
| `man_made` | 5,524 | 86% | 51% | 88% |
| `landuse` | 10,376 | 78% | 38% | 80% |
| `leisure` | 6,909 | 79% | 19% | 80% |
| `amenity` | 55,607 | 78% | 25% | 79% |
| `historic` | 5,998 | 68% | 29% | 69% |
| `place` | 131,257 | 13% | 6% | 14% |
| `boundary` | 87,802 | 12% | 7% | 12% |
| `natural` | 53,272 | 8% | 5% | 9% |
| `waterway` | 546,757 | 1% | 0% | 1% |
| `tunnel` | 24,113 | — | — | 2% |

**Observation:** Built-environment features (military, buildings,
railways, amenities, tourism) have 80–98% temporal coverage.  Natural
features and waterways have very low temporal coverage.  Settlements
and boundaries are in between.

**For WHG ingestion:** Features with `start_date`/`end_date` should
map to `timespans[]` in the places index schema.  OHM uses ISO 8601
extended dates (e.g. `1850`, `1939-09-01`, `-0500` for 500 BCE),
which will need a date parser.

---

## 8. Comparison with OSM

| Aspect | OSM (current) | OHM | Notes |
|--------|-------------:|----:|-------|
| Total named features | ~260M | ~1.7M | OHM is ~0.65% of OSM by volume |
| `place` | 7.7M | 131K | OHM has historical settlements not in OSM |
| `historic` | 1.3M | 6K | OHM's `historic` features are fewer but better-dated |
| `boundary` | 1.2M | 88K | OHM specialises in historical boundaries |
| `railway` | 250K named | 104K | OHM is 42% of OSM railway volume — strong focus |
| `waterway` | 4.5M | 547K | OHM imports many river geometries |
| Temporal tags | ~2% | ~50% | OHM's defining feature |
| Overlap | — | Moderate | Many OHM features also exist in OSM (but with different temporal metadata) |

**Key takeaway:** OHM adds ~1.7M features with rich temporal metadata.
The primary value for WHG is not raw volume but **temporal depth** —
timespans, historical names, and features that no longer exist.

---

## 9. Proposed AAT Type Mapping Strategy

OHM uses the same tag vocabulary as OSM, so the **same AAT mapping
tables** developed for OSM (§2.3 of `type-mapping-plan.md`) apply
directly.  The per-key AAT mappings documented in
`osm-types-inventory.md` are reusable without modification.

**Additional considerations for OHM:**

1. **`historic=*` expansion:** OHM's `historic` key is richer than
   OSM's, with values like `roman_road`, `crash_site`, `ship`,
   `trail`.  Some of these need OHM-specific AAT mappings.

2. **`boundary=administrative` + `admin_level`:** OHM's historical
   boundaries are a major feature.  The `admin_level` tag should
   refine the AAT mapping:
   - `admin_level=2` → nations (300128207)
   - `admin_level=4` → states/provinces (300000776)
   - `admin_level=6` → counties (300000771)
   - `admin_level=8` → municipalities (300265612)
   - `admin_level=10` → parishes (300000778)

3. **`boundary=religious_administration`:** Maps to dioceses
   (300000694) or parishes (300000778) depending on `admin_level`.

4. **Temporal type assertions:** When a feature has `start_date` and
   `end_date`, the type assertion itself should carry the timespan.
   This enables queries like "show me all castles that existed in
   1500."

---

## 10. Proposed WHG Namespace and Ingestion Plan

### 10.1 Namespace

**Proposed namespace:** `ohm`

Place IDs would follow the pattern:
- Nodes: `ohm:n{osm_id}` (e.g. `ohm:n123456`)
- Ways: `ohm:w{osm_id}` (e.g. `ohm:w789012`)
- Relations: `ohm:r{osm_id}` (e.g. `ohm:r345678`)

This mirrors the existing `osm:` namespace convention.

### 10.2 Data Access

OHM provides:
- **Full planet PBF dump:** Available via OHM download servers
  (similar to OSM's planet.osm.pbf but much smaller — likely <5 GB).
- **Overpass API:** For querying, but not suitable for bulk ingestion.
- **OSM API clone:** Individual feature access.

**Recommended approach:** Download the PBF dump and process with
`osmium` (same library used for OSM ingestion in `osm-places.py`).

### 10.3 Ingestion Script Structure

The script (`authorities/ohm-places.py`) should follow the same
architecture as `osm-places.py` with these additions:

1. **Temporal extraction:** Parse `start_date` and `end_date` tags
   into WHG `timespans` format.
2. **Extended type keys:** Include all Tier 1 + Tier 2 keys from
   this inventory (not limited to OSM's current 6 keys).
3. **Deduplication with OSM:** Features present in both OSM and OHM
   should be linked, not duplicated.  Cross-referencing via
   geographic proximity + name matching, or OHM's occasional
   `ref:osm` tags.
4. **Historical name handling:** OHM supports `name:date` and
   `old_name` tags — extract these as additional toponyms with
   appropriate timespans.

### 10.4 Estimated Record Counts

Based on the inventory:

| Category | Est. records | Notes |
|----------|------------:|-------|
| Tier 1 keys | ~825K | place + historic + boundary + natural + waterway |
| Tier 2 keys (selected) | ~100K | Filtered amenity, tourism, leisure, etc. |
| Tier 3 keys (selected) | ~50K | Bridges, building allowlist, stations |
| **Total (estimated)** | **~500K–1M** | After dedup with OSM and filtering |

**Note:** The raw 1.7M count includes significant overlap between keys
(a feature can carry multiple tags) and some features of limited
gazetteer relevance.  After filtering and deduplication, expect
500K–1M unique place records — a valuable addition to WHG's
~47M-record index.

---

## 11. Implementation Priority

| Phase | Task | Effort |
|-------|------|--------|
| 1 | Fetch and archive OHM PBF dump | Low |
| 2 | Create `ohm-places.py` (adapt from `osm-places.py`) | Medium |
| 3 | Add temporal extraction (start_date/end_date → timespans) | Medium |
| 4 | Extend tag key extraction to Tier 1 + Tier 2 | Low |
| 5 | Reuse OSM AAT mapping tables for OHM types | Low |
| 6 | OSM↔OHM deduplication/linking | High |
| 7 | Ingest and verify | Medium |

**Dependency:** Phase 6 (deduplication) should follow the clustering
pipeline — OHM and OSM records for the same place should be linked
via clusters, not deduplicated at ingestion time.  This matches the
WHG philosophy of preserving all attestations and resolving identity
through clustering.

