# OSM Tag Inventory for WHG Place Type Mapping

> **Purpose:** Comprehensive catalogue of all OSM primary feature tag
> keys and values relevant to a historical gazetteer, with AAT mapping
> assessments and implementation priority.
>
> **Status:** Reference document.  Feeds into `type-mapping-plan.md`
> §2.3 (Pass 0c) and guides future expansion of `authorities/osm-places.py`.
>
> **Last updated:** 4 April 2026

---

## 1. Data Source and Methodology

All tag value inventories and feature counts in this document are
sourced from the **TagInfo API**
(`https://taginfo.openstreetmap.org/api/4/`), queried on 4 April 2026.
Raw JSON data is archived at `developer/osm-taginfo-data.json`.

TagInfo reports:
- **Total features** carrying each tag key (all OSM objects globally)
- **Features with `name` tag** — the count of features that also carry
  a `name=*` tag (i.e. the subset relevant for WHG, which requires
  `name` for ingestion)
- **Value counts** — how many features carry each specific `key=value`
  combination

**Important caveat:** The "with `name`" count is a key-level aggregate;
TagInfo does not directly report per-value named counts.  The
per-value counts in the tables below are total features (named +
unnamed).  For keys like `place` where most features have names, the
total is a good proxy.  For keys like `natural` or `building` where
many features are unnamed (e.g. individual trees, generic `building=yes`),
the named count is much lower than the total.

The fetch script is at `scripts/fetch_osm_taginfo.py`.

---

## 2. Current State

The OSM ingestion script (`authorities/osm-places.py`) currently
extracts types from **6 tag keys**:

| # | Tag key | Filter gate | Type extraction |
|---|---------|-------------|-----------------|
| 1 | `place` | Primary (line 170) | ✓ (line 129) |
| 2 | `natural` | Secondary (line 172) | ✓ (line 129) |
| 3 | `water` | Secondary (line 172) | ✓ (line 129) |
| 4 | `waterway` | Secondary (line 172) | ✓ (line 129) |
| 5 | `historic` | Secondary (line 172) | ✓ (line 129) |
| 6 | `landuse` | Secondary (line 172) | ✓ (line 129) |

The `process_tags()` function (line 165) **requires** the `name` tag
AND at least one of these 6 keys.  Features carrying other primary
keys (e.g. `amenity`, `tourism`, `leisure`) but none of the 6 are
**silently skipped** — even if they have names and represent
significant places.

Note: `process_tags()` already extracts `boundary` and `admin_level`
into the tag dict (line 184) but these are not used for filtering or
type extraction.

### 2.1 What we're missing

A named cathedral tagged `amenity=place_of_worship` +
`building=cathedral` but without `historic=*` is currently skipped.
A named national park tagged `boundary=national_park` but without
`landuse=*` is skipped.  A named railway station, airport, museum,
castle (tagged `tourism=castle` rather than `historic=castle`), park,
or university is skipped unless it also carries one of the 6 current
keys.

Many features carry multiple tags — a castle might have both
`historic=castle` and `tourism=castle`.  But many do not.  The
following sections inventory all tag keys that could yield
gazetteer-relevant place types.

---

## 3. Tier Structure

| Tier | Description | Implementation phase |
|------|-------------|---------------------|
| **Tier 1** | Currently extracted (6 keys) | Done |
| **Tier 2** | High priority: clearly place-like named features with historical significance | Next |
| **Tier 3** | Medium priority: named infrastructure and facilities | Later |
| **Tier 4** | Low priority: specialist, modern-only, or minor features | Deferred |

**Tier assignment criteria:**
- Would a scholar or general user look this up in a gazetteer?
- Does the feature type have historical depth (not purely modern)?
- Does AAT have a plausible mapping concept?
- How many named features carry this tag globally?

---

## 4. Tier 1: Currently Extracted Keys

These are already documented in `type-mapping-plan.md` §2.3.
Counts below are from TagInfo (total features, not just named).

### 4.1 `place`

**9.3M total features, 7.7M with `name` tag**, 712 distinct values.

The core settlement and administrative hierarchy.  Almost all
`place=*` features have names.

| Value | Total | AAT concept | AAT ID |
|-------|------:|-------------|--------|
| `hamlet` | 2,087K | hamlets | 300008197 |
| `locality` | 1,944K | inhabited places (broad) | 300008347 |
| `village` | 1,741K | villages | 300008372 |
| `isolated_dwelling` | 851K | dwellings | 300005433 |
| `islet` | 701K | islets | 300008792 |
| `neighbourhood` | 532K | neighborhoods | 300000745 |
| `plot` | 308K | — | — |
| `farm` | 282K | farms (inhabited places) | 300000206 |
| `suburb` | 160K | suburbs | 300000745 |
| `quarter` | 159K | quarters | 300000745 |
| `town` | 117K | towns | 300008375 |
| `square` | 108K | plazas | 300008066 |
| `city_block` | 102K | — | — |
| `island` | 100K | islands (landforms) | 300008791 |
| `allotments` | 22K | — | — |
| `municipality` | 17K | municipalities | 300265612 |
| `city` | 15K | cities | 300008389 |
| `county` | 9K | counties | 300000771 |
| `civil_parish` | 4K | parishes | 300000778 |
| `district` | 4K | districts | — |
| `region` | 3K | regions (administrative) | 300387178 |
| `archipelago` | 3K | archipelagos | 300386854 |
| `state` | 2K | states (political divisions) | 300000776 |
| `subdivision` | 1K | — | — |
| `subdistrict` | 1K | — | — |
| `province` | 617 | provinces | — |
| `borough` | 590 | boroughs | 300000778 |
| `department` | 353 | departments | — |
| `country` | 227 | nations | 300128207 |
| `polder` | 158 | polders | — |
| `sea` | 148 | seas | 300008694 |
| `ocean` | — | oceans | 300008687 |
| `continent` | — | continents | 300128176 |

### 4.2 `natural`

**181.2M total features, 2.5M with `name` tag**, 1,034 distinct values.

Most features are unnamed (trees, scrub areas, bare rock).  Named
features concentrate in peaks, water bodies, springs, and major
landforms.

| Value | Total | Named? | AAT concept | AAT ID |
|-------|------:|:------:|-------------|--------|
| `tree` | 32,741K | Rarely | — | — |
| `water` | 22,600K | Often | bodies of water | 300008680 |
| `wood` | 12,233K | Often | forests | 300008838 |
| `scrub` | 5,444K | Rarely | — | — |
| `wetland` | 4,465K | Often | wetlands | 300008899 |
| `grassland` | 2,278K | Rarely | — | — |
| `tree_row` | 2,000K | Rarely | — | — |
| `coastline` | 1,300K | — | — | — |
| `bare_rock` | 1,300K | Rarely | — | — |
| `peak` | 1,094K | **Yes** | peaks (landforms) | 300008816 |
| `cliff` | 980K | Sometimes | cliffs | 300008749 |
| `heath` | 647K | Sometimes | heaths | 300008877 |
| `sand` | 589K | Sometimes | dunes | 300008755 |
| `scree` | 375K | Rarely | — | — |
| `spring` | 277K | **Yes** | springs | 300008697 |
| `ridge` | 246K | **Yes** | ridges | 300266640 |
| `rock` | 246K | Sometimes | — | — |
| `beach` | 242K | **Yes** | beaches | — |
| `reef` | 117K | Sometimes | reefs | 300008808 |
| `glacier` | 115K | **Yes** | glaciers | 300008771 |
| `bay` | 89K | **Yes** | bays | 300132316 |
| `stone` | 85K | Sometimes | — | — |
| `saddle` | 78K | Often | cols | — |
| `cave_entrance` | 71K | **Yes** | caves | 300008746 |
| `hill` | 63K | **Yes** | hills | — |
| `valley` | 62K | **Yes** | valleys | 300008830 |
| `cape` | 56K | **Yes** | capes | 300008850 |
| `sinkhole` | 54K | Sometimes | sinkholes | — |
| `fell` | 28K | **Yes** | — | — |
| `volcano` | 10K | **Yes** | volcanoes | 300132325 |
| `strait` | 8K | **Yes** | straits | 300266559 |
| `arete` | 8K | **Yes** | arêtes | — |
| `mountain_range` | 8K | **Yes** | mountain ranges | — |
| `hot_spring` | 7K | **Yes** | hot springs | 300008700 |
| `peninsula` | 5K | **Yes** | peninsulas | 300008804 |
| `isthmus` | — | **Yes** | — | — |

### 4.3 `water`

**10.6M total features, 542K with `name` tag**, 1,465 distinct values.

Used on areas tagged `natural=water` to classify the water body type.

| Value | Total | AAT concept | AAT ID |
|-------|------:|-------------|--------|
| `pond` | 2,293K | ponds | 300008688 |
| `lake` | 886K | lakes | 300008680 |
| `reservoir` | 859K | reservoirs | 300006191 |
| `river` | 513K | rivers | 300008707 |
| `basin` | 318K | basins | — |
| `wastewater` | 122K | — | — |
| `canal` | 68K | canals (waterways) | 300006075 |
| `stream` | 55K | streams | 300008699 |
| `oxbow` | 53K | oxbow lakes | — |
| `ditch` | 40K | ditches | 300006176 |
| `lagoon` | 13K | lagoons | — |
| `drain` | 11K | — | — |
| `fishpond` | 11K | fishponds | — |
| `rapids` | 5K | rapids | — |
| `lock` | 4K | locks (hydraulic structures) | — |
| `moat` | 3K | moats | 300006295 |
| `reflecting_pool` | 2K | — | — |
| `harbour` | 1K | harbors | 300008678 |

### 4.4 `waterway`

**78.4M total features, 5.9M with `name` tag**, 589 distinct values.

Linear water features.  `stream` dominates (29M features).

| Value | Total | AAT concept | AAT ID |
|-------|------:|-------------|--------|
| `stream` | 28,877K | streams | 300008699 |
| `ditch` | 4,581K | ditches | 300006176 |
| `river` | 2,006K | rivers | 300008707 |
| `drain` | 1,884K | — | — |
| `canal` | 956K | canals (waterways) | 300006075 |
| `dam` | 264K | dams | 300006072 |
| `weir` | 148K | weirs | — |
| `rapids` | 122K | rapids | — |
| `waterfall` | 86K | waterfalls | 300008736 |
| `tidal_channel` | 29K | — | — |
| `lock_gate` | 24K | lock gates | — |
| `drystream` | 17K | — | — |
| `dock` | 7K | docks | 300120582 |
| `boatyard` | 5K | boatyards | — |
| `wadi` | 3K | wadis | — |
| `fish_pass` | 3K | — | — |
| `fuel` | 2K | — | — |

### 4.5 `historic`

**4.4M total features, 965K with `name` tag**, 4,031 distinct values.

Historical features.  Strong AAT coverage.  Note the large number of
distinct values (4,031) reflecting OSM's freeform tagging — most are
rare misspellings or niche terms.

| Value | Total | AAT concept | AAT ID |
|-------|------:|-------------|--------|
| `memorial` | 481K | memorials | 300006958 |
| `wayside_cross` | 229K | wayside crosses | — |
| `archaeological_site` | 223K | archaeological sites | 300000810 |
| `yes` | 188K | — (skip) | — |
| `ruins` | 178K | ruins | 300008057 |
| `wayside_shrine` | 163K | shrines | 300007558 |
| `boundary_stone` | 94K | boundary markers | 300006971 |
| `building` | 89K | buildings (generic historic) | 300004792 |
| `tomb` | 71K | tombs | 300005926 |
| `monument` | 71K | monuments | 300006958 |
| `castle` | 54K | castles (fortifications) | 300006891 |
| `charcoal_pile` | 51K | — | — |
| `shieling` | 22K | shielings | — |
| `bomb_crater` | 20K | — | — |
| `railway` | 19K | — | — |
| `manor` | 19K | manor houses | 300005366 |
| `citywalls` | 17K | city walls | — |
| `heritage` | 16K | — | — |
| `mine` | 13K | mines (extractive sites) | 300000390 |
| `church` | 12K | churches (buildings) | 300007466 |
| `mine_shaft` | 11K | mine shafts | — |
| `fort` | 9K | forts | 300006909 |
| `city_gate` | 9K | city gates | 300002837 |
| `milestone` | 9K | milestones | 300006973 |
| `house` | 7K | — | — |
| `aircraft` | 7K | — | — |
| `cannon` | 6K | — | — |
| `wreck` | 6K | shipwrecks | 300188189 |
| `hollow_way` | 5K | — | — |
| `stone` | 5K | — | — |
| `roman_road` | 4K | — | — |
| `tower` | 3K | towers (buildings) | 300004847 |
| `monastery` | 3K | monasteries | 300000641 |
| `farm` | 3K | farms | 300000206 |
| `bridge` | 3K | bridges (built works) | 300007836 |
| `district` | 2K | historic districts | — |
| `battlefield` | 2K | battlefields | 300000835 |
| `cemetery` | 2K | cemeteries | 300000632 |
| `aqueduct` | 1K | aqueducts | 300006165 |
| `lighthouse` | ~1K | lighthouses | 300007741 |

### 4.6 `landuse`

**101.1M total features, 2.6M with `name` tag**, 2,113 distinct values.

Land use zones.  Mostly large areas; named ones are gazetteer-relevant
(forests, cemeteries, ports, military land).

| Value | Total | AAT concept | AAT ID |
|-------|------:|-------------|--------|
| `farmland` | 11,328K | agricultural land | 300265699 |
| `residential` | 10,417K | residential districts | — |
| `grass` | 7,249K | — | — |
| `forest` | 5,847K | forests | 300008838 |
| `meadow` | 5,380K | meadows | 300008876 |
| `orchard` | 1,835K | orchards | 300000227 |
| `farmyard` | 1,568K | — | — |
| `industrial` | 1,393K | industrial districts | — |
| `vineyard` | 847K | vineyards | 300000233 |
| `cemetery` | 587K | cemeteries | 300000632 |
| `commercial` | 572K | commercial districts | — |
| `allotments` | 457K | allotment gardens | — |
| `retail` | 400K | — | — |
| `basin` | 274K | basins | — |
| `quarry` | 252K | quarries | 300000402 |
| `reservoir` | 197K | reservoirs | 300006191 |
| `recreation_ground` | 181K | recreation areas | — |
| `religious` | 151K | — | — |
| `military` | 94K | military installations | 300000455 |
| `village_green` | 95K | village greens | — |
| `salt_pond` | 21K | salt pans | — |
| `harbour` | 3K | ports (settlements) | 300120580 |

---

## 5. Tier 2: High-Priority Additions

These tag keys identify clearly place-like named features with
historical significance and generally good AAT coverage.

### 5.1 `amenity`

**66.3M total features, 10.8M with `name` tag**, 9,134 distinct values.

The `amenity` key covers a huge range of features.  The vast majority
(parking, benches, waste baskets, bicycle parking, vending machines,
etc.) are not gazetteer places.  But several values represent
significant named institutions.

Only values relevant to a historical gazetteer are listed here.

#### Religious and institutional (Tier 2)

| Value | Total | AAT concept | AAT ID |
|-------|------:|-------------|--------|
| `place_of_worship` | 1,600K | houses of worship | 300007391 |
| `monastery` | 16K | monasteries | 300000641 |
| `grave_yard` | 159K | burial grounds | 300000360 |

`place_of_worship` is further subdivided by `religion=*` and
`building=*` tags.  For AAT refinement:

| `religion` | `building` | AAT concept | AAT ID |
|------------|-----------|-------------|--------|
| `christian` | `church` (430K) | churches | 300007466 |
| `christian` | `cathedral` (3K) | cathedrals | 300007501 |
| `christian` | `chapel` (118K) | chapels | 300004590 |
| `muslim` | `mosque` (107K) | mosques | 300007544 |
| `jewish` | `synagogue` (2K) | synagogues | 300007590 |
| `buddhist` | `temple` (31K) | temples (buildings) | 300007595 |
| `hindu` | `temple` | temples (buildings) | 300007595 |
| `shinto` | — | shrines | 300007558 |

#### Educational (Tier 2–3)

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `school` | 1,435K | schools (buildings) | 300007144 | 3 |
| `university` | 57K | universities | 300000444 | 2 |
| `college` | 71K | colleges (institutions) | 300008331 | 2 |
| `library` | 111K | libraries (buildings) | 300007145 | 2 |
| `kindergarten` | 333K | — | — | 4 |

#### Cultural and civic (Tier 2)

| Value | Total | AAT concept | AAT ID |
|-------|------:|-------------|--------|
| `fountain` | 196K | fountains (structures) | 300006179 |
| `townhall` | 172K | town halls | 300007476 |
| `marketplace` | 96K | marketplaces | 300112366 |
| `theatre` | 52K | theaters (buildings) | 300007117 |
| `events_venue` | 33K | — | — |
| `cinema` | 32K | cinemas | — |
| `courthouse` | 31K | courthouses | 300007425 |
| `arts_centre` | 28K | art centers | — |
| `community_centre` | 211K | community centers | — |
| `clock` | 34K | — | — |

#### Medical and civic services (Tier 3)

| Value | Total | AAT concept | AAT ID |
|-------|------:|-------------|--------|
| `hospital` | 217K | hospitals | 300007145 |
| `clinic` | 212K | clinics | — |
| `prison` | 15K | prisons | 300343479 |
| `police` | 159K | — | — |
| `fire_station` | 142K | fire stations | — |
| `post_office` | 211K | post offices | — |

#### Not gazetteer-relevant (skip)

`parking` (6.6M), `parking_space` (4.6M), `bench` (3.2M),
`waste_basket` (1.1M), `bicycle_parking` (879K), `shelter` (645K),
`fast_food` (642K), `recycling` (552K), `toilets` (501K),
`drinking_water` (352K), `vending_machine` (344K),
`hunting_stand` (298K), `atm` (236K), `charging_station` (184K),
`parcel_locker` (90K), etc.

### 5.2 `tourism`

**7.8M total features, 2.2M with `name` tag**, 1,351 distinct values.

Tourist facilities and attractions.  Some overlap with `historic=*`
but many features are tagged *only* with `tourism`.

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `information` | 1,436K | — | — | skip |
| `hotel` | 446K | hotels | 300007166 | 4 |
| `artwork` | 346K | public art | — | 3 |
| `viewpoint` | 255K | overlooks | — | 4 |
| `attraction` | 251K | — (generic) | — | 3 |
| `guest_house` | 194K | — | — | 4 |
| `picnic_site` | 165K | — | — | 4 |
| `camp_site` | 157K | — | — | 4 |
| `museum` | 109K | museums (buildings) | 300005768 | **2** |
| `chalet` | 106K | — | — | 4 |
| `apartment` | 86K | — | — | 4 |
| `hostel` | 62K | — | — | 4 |
| `motel` | 49K | — | — | 4 |
| `caravan_site` | 35K | — | — | 4 |
| `gallery` | 21K | galleries (buildings) | 300005768 | **2** |
| `wilderness_hut` | 17K | — | — | 4 |
| `alpine_hut` | 14K | mountain huts | — | 4 |
| `theme_park` | 11K | amusement parks | — | 3 |
| `zoo` | 9K | zoos | 300005581 | **2** |
| `aquarium` | 2K | aquariums | — | 3 |

### 5.3 `leisure`

**22.6M total features, 1.7M with `name` tag**, 1,700 distinct values.

Recreational and green-space features.  Most features (swimming pools,
pitches, playgrounds) are not gazetteer places.  Parks, gardens, and
nature reserves are.

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `swimming_pool` | 2,836K | — | — | skip |
| `pitch` | 2,748K | — | — | skip |
| `garden` | 1,498K | gardens | 300008090 | **2** |
| `park` | 1,277K | parks (grounds) | 300008187 | **2** |
| `playground` | 979K | — | — | skip |
| `picnic_table` | 455K | — | — | skip |
| `sports_centre` | 262K | sports complexes | — | 4 |
| `track` | 158K | — | — | skip |
| `nature_reserve` | 148K | nature reserves | 300008076 | **2** |
| `fitness_centre` | 101K | — | — | skip |
| `stadium` | 52K | stadiums | 300007180 | 3 |
| `golf_course` | 40K | — | — | 4 |
| `marina` | 31K | marinas | 300120471 | 3 |
| `common` | 31K | commons (open spaces) | — | 3 |
| `dog_park` | 29K | — | — | skip |
| `resort` | 24K | resorts | — | 4 |
| `horse_riding` | 21K | — | — | 4 |
| `water_park` | 13K | — | — | 4 |
| `miniature_golf` | 11K | — | — | skip |
| `ice_rink` | 10K | — | — | 4 |
| `bandstand` | 8K | bandstands | — | 4 |
| `recreation_ground` | 7K | recreation areas | — | 4 |

### 5.4 `man_made`

**19.0M total features, 952K with `name` tag**, 3,540 distinct values.

Human-made structures and facilities.  The vast majority are unnamed
infrastructure (storage tanks, manholes, masts, utility poles,
surveillance cameras, pipelines).  Gazetteer-relevant items are
towers, bridges, lighthouses, and industrial works.

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `storage_tank` | 831K | — | — | skip |
| `pier` | 728K | piers | 300120399 | 3 |
| `cutline` | 609K | — | — | skip |
| `manhole` | 491K | — | — | skip |
| `mast` | 477K | — | — | skip |
| `tower` | 449K | towers | 300004847 | **2** |
| `surveillance` | 434K | — | — | skip |
| `utility_pole` | 401K | — | — | skip |
| `survey_point` | 390K | — | — | skip |
| `pipeline` | 357K | — | — | skip |
| `silo` | 352K | silos | — | 4 |
| `water_well` | 344K | wells | 300006209 | 3 |
| `embankment` | 333K | — | — | skip |
| `petroleum_well` | 322K | — | — | skip |
| `bridge` | 311K | bridges | 300007836 | **2** |
| `works` | 238K | factories | 300006232 | 3 |
| `water_tower` | 165K | water towers | 300007880 | 3 |
| `chimney` | 114K | — | — | 4 |
| `wastewater_plant` | 91K | — | — | 4 |
| `dyke` | 36K | dikes (embankments) | 300006177 | 3 |
| `pumping_station` | 36K | — | — | 4 |
| `cross` | 35K | — | — | 4 |
| `adit` | 23K | adits | — | 3 |
| `crane` | 22K | cranes | — | 4 |
| `mineshaft` | 18K | mine shafts | — | 3 |
| `windmill` | ~15K | windmills | 300005943 | **2** |
| `lighthouse` | ~15K | lighthouses | 300007741 | **2** |
| `observatory` | ~2K | observatories | 300007303 | **2** |
| `watermill` | ~5K | watermills | — | **2** |

### 5.5 `boundary`

**5.1M total features, 1.2M with `name` tag**, 580 distinct values.

Administrative and protected area boundaries.
**`boundary=administrative`** is ingested into the **`places` index**
(like all other named OSM features) and ALSO into a **separate admin
boundaries index** that feeds the "Space" filter in the search UI.
This dual-indexing approach means administrative areas are discoverable
as places (by name, type, location) and also available as spatial
filter geometries.

| Value | Total | AAT concept | AAT ID | Tier | Notes |
|-------|------:|-------------|--------|------|-------|
| `administrative` | 2,100K | admin. districts | 300000776 | **2** | **Places index + separate admin boundaries index** |
| `protected_area` | 132K | nature reserves | 300008076 | **2** | |
| `postal_code` | 57K | — | — | skip | |
| `marker` | 31K | — | — | skip | Physical markers |
| `census` | 26K | — | — | skip | |
| `forest_compartment` | 23K | — | — | skip | |
| `local_authority` | 16K | — | — | skip | |
| `political` | 14K | — | — | skip | |
| `historic` | 14K | — | — | 3 | Historical boundaries |
| `health` | 13K | — | — | skip | |
| `religious_administration` | 13K | — | — | 3 | Parish boundaries etc. |
| `place` | 13K | — | — | skip | Duplicate of `place=*` |
| `aboriginal_lands` | 6K | — | — | **2** | Indigenous territories |
| `national_park` | 6K | national parks | 300008069 | **2** | |
| `maritime` | 4K | — | — | 4 | |

#### Administrative levels (for `boundary=administrative`)

| `admin_level` | Typical entity | AAT concept | AAT ID |
|---------------|---------------|-------------|--------|
| 2 | Country | nations | 300128207 |
| 3 | Region (some countries) | regions | 300387178 |
| 4 | State/province | states | 300000776 |
| 5 | Region/department | departments | — |
| 6 | County/district | counties | 300000771 |
| 7 | Municipality/township | municipalities | 300265612 |
| 8 | City/town | cities | 300008389 |
| 9 | Borough/ward | boroughs | 300000778 |
| 10 | Neighbourhood | neighborhoods | 300000745 |

### 5.6 `military`

**398K total features, 54K with `name` tag**, 363 distinct values.

Military installations.  Many have historical significance.

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `bunker` | 107K | bunkers | — | 3 |
| `trench` | 27K | trenches | — | 3 |
| `barracks` | 16K | barracks | 300006691 | **2** |
| `office` | 9K | — | — | skip |
| `checkpoint` | 8K | — | — | 4 |
| `range` | 5K | — | — | 4 |
| `base` | 4K | military bases | — | **2** |
| `training_area` | 3K | — | — | 4 |
| `danger_area` | 3K | — | — | 4 |
| `nuclear_explosion_site` | 3K | — | — | 3 |
| `airfield` | 2K | airfields | 300007027 | **2** |
| `obstacle_course` | 1K | — | — | skip |
| `naval_base` | 526 | naval bases | — | **2** |
| `ammunition` | 449 | — | — | 4 |
| `academy` | 63 | military academies | — | 3 |

### 5.7 `building` (as fallback — curated allowlist only)

**1,366M total features**, 8,619 distinct values.

The `building` key is near-universal (`building=yes` on 542M features)
and is usually a *secondary* tag alongside `amenity`, `tourism`, or
`historic`.  The TagInfo API reports 0 "with `name`" at the key level
because the `name` combination falls outside the top returned
combinations for such a massive key.

For WHG, `building` is relevant only as a **fallback** — capturing
named buildings of significant types that slipped through other tag
filters.  Only extract values in the allowlist below.

**Allowlist (significant building types):**

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `church` | 430K | churches | 300007466 | **2** |
| `chapel` | 118K | chapels | 300004590 | **2** |
| `mosque` | 107K | mosques | 300007544 | **2** |
| `university` | 175K | universities | 300000444 | **2** |
| `hospital` | 169K | hospitals | 300007145 | 3 |
| `hotel` | 157K | hotels | 300007166 | 4 |
| `school` | 1,307K | schools | 300007144 | 3 |
| `temple` | 31K | temples | 300007595 | **2** |
| `government` | 32K | government buildings | 300007429 | 3 |
| `stadium` | 12K | stadiums | 300007180 | 3 |
| `tomb` | 11K | tombs | 300005926 | 3 |
| `shrine` | 6K | shrines | 300007558 | **2** |
| `wayside_shrine` | 7K | shrines | 300007558 | 3 |
| `tower` | 7K | towers | 300004847 | 3 |
| `military` | 6K | — | — | 3 |
| `castle` | 4K | castles | 300006891 | **2** |
| `monastery` | 4K | monasteries | 300000641 | **2** |
| `cathedral` | 3K | cathedrals | 300007501 | **2** |
| `museum` | 2K | museums | 300005768 | **2** |
| `barracks` | 2K | barracks | 300006691 | 3 |
| `prison` | 2K | prisons | 300343479 | 3 |
| `synagogue` | 2K | synagogues | 300007590 | **2** |
| `water_tower` | 2K | water towers | 300007880 | 3 |
| `library` | 2K | libraries | 300007145 | 3 |
| `theatre` | 1K | theaters | 300007117 | 3 |
| `palace` | 870 | palaces | 300005734 | **2** |
| `basilica` | 832 | basilicas | — | **2** |
| `pagoda` | 1K | pagodas | 300007587 | **2** |

**Not in allowlist (skip):** `yes` (542M), `house` (65M),
`residential` (16M), `detached` (10M), `garage` (8M),
`apartments` (8M), `shed` (4M), `industrial` (3M), `hut` (2M),
`farm_auxiliary` (2M), `terrace` (2M), `commercial` (2M),
`retail` (1M), `construction` (1M), `outbuilding` (1M),
`greenhouse` (802K), `barn` (799K), etc.

---

## 6. Tier 3: Medium-Priority Additions

### 6.1 `aeroway`

**2.2M total features, 93K with `name` tag**, 274 distinct values.

Airports and airfields are significant named places.

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `aerodrome` | 46K | airports | 300007026 | **3** |
| `helipad` | 69K | helipads | — | 4 |
| `hangar` | 75K | hangars | — | 4 |
| `terminal` | 10K | terminals | — | 4 |
| `airstrip` | 6K | airstrips | — | 4 |
| `heliport` | 755 | heliports | — | 4 |
| `spaceport` | 48 | spaceports | — | 4 |

Not gazetteer-relevant: `taxiway` (289K), `navigationaid` (237K),
`parking_position` (125K), `runway` (66K), `apron` (48K),
`holding_position` (41K), etc.

### 6.2 `railway`

**16.5M total features, 1.9M with `name` tag**, 631 distinct values.

Railway stations are significant named places, often with long
histories.  Most features are track geometry (`rail` 2.8M,
`switch` 1.2M, `level_crossing` 1M) or signals.

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `station` | 104K | railroad stations | 300007783 | **3** |
| `tram_stop` | 55K | — | — | 4 |
| `subway_entrance` | 52K | — | — | 4 |
| `halt` | 35K | railroad stations (broad) | 300007783 | **3** |
| `signal_box` | 11K | signal boxes | — | 4 |
| `junction` | 7K | junctions | — | 4 |
| `yard` | 6K | rail yards | — | 4 |
| `turntable` | 4K | — | — | 4 |
| `funicular` | 3K | funicular railways | — | 4 |
| `preserved` | 2K | heritage railways | — | 3 |
| `roundhouse` | ~500 | roundhouses | — | 3 |

Not gazetteer-relevant: `rail` (2.8M), `switch` (1.2M),
`level_crossing` (1M), `signal` (478K), `buffer_stop` (275K),
`milestone` (259K), `crossing` (218K), `platform` (192K), etc.

### 6.3 `geological`

**30K total features, 4K with `name` tag**, 117 distinct values.

Geological features.  Very specialist; few have AAT mappings.

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `moraine` | 3K | moraines | — | 4 |
| `outcrop` | 2K | outcrops | — | 4 |
| `volcanic_caldera_rim` | 2K | calderas | — | 3 |
| `fault` | 1K | geological faults | — | 4 |
| `glacial_erratic` | 1K | — | — | 4 |
| `volcanic_lava_field` | 951 | — | — | 4 |
| `volcanic_vent` | 879 | volcanic vents | — | 3 |
| `palaeontological_site` | 834 | archaeological sites (broad) | 300000810 | 3 |
| `inselberg` | 393 | inselbergs | — | 4 |
| `meteor_crater` | 155 | craters | — | 3 |
| `volcanic_lava_tube` | 131 | lava tubes | — | 4 |

### 6.4 `power`

**97.5M total features**, 706 distinct values.

Overwhelmingly poles (19M) and towers (18M) — not places.  Only
power stations are gazetteer-relevant.

| Value | Total | AAT concept | AAT ID | Tier |
|-------|------:|-------------|--------|------|
| `plant` | 144K | power plants | — | 3 |
| `substation` | 842K | substations | — | 4 |

Not gazetteer-relevant: `pole` (19M), `tower` (18M),
`generator` (6M), `minor_line` (2M), `line` (1M), etc.

---

## 7. Summary Statistics (from TagInfo)

### 7.1 Key-level overview

| Tag key | Total features | With `name` | Distinct values | Status |
|---------|---------------:|------------:|----------------:|--------|
| `place` | 9.3M | 7.7M | 712 | Tier 1 (extracted) |
| `natural` | 181.2M | 2.5M | 1,034 | Tier 1 (extracted) |
| `water` | 10.6M | 542K | 1,465 | Tier 1 (extracted) |
| `waterway` | 78.4M | 5.9M | 589 | Tier 1 (extracted) |
| `historic` | 4.4M | 965K | 4,031 | Tier 1 (extracted) |
| `landuse` | 101.1M | 2.6M | 2,113 | Tier 1 (extracted) |
| `amenity` | 66.3M | **10.8M** | 9,134 | **Tier 2** |
| `tourism` | 7.8M | 2.2M | 1,351 | **Tier 2** |
| `leisure` | 22.6M | 1.7M | 1,700 | **Tier 2** |
| `man_made` | 19.0M | 952K | 3,540 | **Tier 2** |
| `boundary` | 5.1M | 1.2M | 580 | **Tier 2**† |
| `military` | 398K | 54K | 363 | **Tier 2** |
| `building` | 1,366M | — | 8,619 | **Tier 2*** |
| `aeroway` | 2.2M | 93K | 274 | Tier 3 |
| `railway` | 16.5M | 1.9M | 631 | Tier 3 |
| `geological` | 30K | 4K | 117 | Tier 3 |
| `power` | 97.5M | — | 706 | Tier 3 |

† `boundary`: all values including `administrative` for places index;
`administrative` also feeds a separate admin boundaries index.
\* `building`: allowlist only.

### 7.2 Estimated new named features by tier

| Tier | Approach | Est. new named features |
|------|----------|------------------------|
| Tier 2 (amenity selected) | place_of_worship, university, college, library, marketplace, theatre, townhall, etc. | ~2–3M |
| Tier 2 (tourism) | museum, gallery, zoo | ~150K |
| Tier 2 (leisure) | park, garden, nature_reserve | ~1–2M |
| Tier 2 (man_made) | tower, bridge, lighthouse, windmill, observatory | ~200K |
| Tier 2 (boundary) | administrative, national_park, protected_area, aboriginal_lands | ~2.2M |
| Tier 2 (military) | barracks, base, airfield, naval_base | ~25K |
| Tier 2 (building fallback) | cathedral, castle, palace, mosque, synagogue (only when no other tag captured) | uncertain |
| **Tier 2 total** | | **~3.5–5.5M** |
| Tier 3 (aeroway) | aerodrome | ~46K |
| Tier 3 (railway) | station, halt | ~140K |
| Tier 3 (geological) | all | ~4K |
| Tier 3 (power) | plant | ~144K |
| **Tier 3 total** | | **~330K** |

**Note on overlap:** Many Tier 2 features already carry one of the 6
current keys.  For example, a castle tagged `historic=castle` +
`tourism=castle` is already indexed.  The *net new* features will
be lower than these totals — they represent features that have ONLY
the new key and none of the current 6.  The overlap rate varies by
key; `amenity=place_of_worship` has relatively low overlap with
`historic`, while `tourism=castle` has very high overlap.

---

## 8. Deduplication and Precedence

Many OSM features carry multiple primary tags.  A medieval church
might have:
```
amenity=place_of_worship
religion=christian
building=church
historic=church
tourism=attraction
```

The current type extraction logic (line 129 of `osm-places.py`)
emits **one type entry per matching tag key**.  The places index
`types[]` array supports multiple entries, so this is correct
behaviour — the same feature can be typed as both `historic=church`
and `amenity=place_of_worship`.

**Recommended approach when expanding tag keys:**

1. **Emit all applicable types.** If a feature matches
   `historic=castle` AND `tourism=castle`, emit both as separate type
   entries.  The `sourceLabel` field (`historic=castle` vs
   `tourism=castle`) disambiguates them.

2. **AAT deduplication happens in the mapping layer**, not at
   ingestion.  Both `historic=castle` and `tourism=castle` map to the
   same AAT concept (300006891), which is fine — the mapping table
   handles this.

3. **Feature gate expansion.** The `process_tags()` filter (lines
   170–175) must be extended to accept the new tag keys.  A feature
   tagged `amenity=place_of_worship` but not tagged with any of the
   current 6 keys should no longer be skipped.

4. **`building` as fallback.** Because `building=yes` is near-universal
   and meaningless for typing, only extract `building` types when the
   value is in the curated allowlist (see §5.7).  Do not extract
   `building=yes`, `building=residential`, `building=house`, etc.

5. **`amenity` value filtering.** Unlike `place` or `historic` where
   almost all values are gazetteer-relevant, `amenity` has many
   non-place values (parking, bench, waste_basket, etc.).  The
   ingestion script should either:
   - Use an allowlist of gazetteer-relevant amenity values, or
   - Accept all amenity values but apply the allowlist at the AAT
     mapping stage (simpler, but bloats the index)

---

## 9. Feature Count Discovery

After expanded ingestion, run this ES aggregation to enumerate all
distinct OSM type values in the index with their document counts:

```json
{
  "size": 0,
  "query": {
    "nested": {
      "path": "types",
      "query": { "term": { "types.label": "osm" } }
    }
  },
  "aggs": {
    "type_values": {
      "nested": { "path": "types" },
      "aggs": {
        "osm_only": {
          "filter": { "term": { "types.label": "osm" } },
          "aggs": {
            "by_source_label": {
              "terms": { "field": "types.sourceLabel", "size": 1000 }
            }
          }
        }
      }
    }
  }
}
```

This reveals the actual tag values present in the indexed data, their
frequencies, and any unexpected long-tail values.

---

## 10. Implementation Plan

### Phase 1: Expand type extraction (no re-ingestion)

Update `osm-places.py` to extract types from additional tag keys.
This only affects **new** ingestions and re-ingestions.

1. Extend the `process_tags()` feature gate to accept Tier 2 keys.
2. Extend the `create_doc()` type extraction loop to include Tier 2
   keys.
3. Add a `BUILDING_ALLOWLIST` for significant building types.
4. Add an `AMENITY_ALLOWLIST` or accept all amenity values.
5. Test with a regional extract (e.g. `great-britain-latest.osm.pbf`)
   before full planet ingestion.

### Phase 2: Build AAT mapping table

1. After expanded ingestion, run the discovery aggregation (§9).
2. Match discovered `sourceLabel` values against the tables in this
   document to assign AAT IDs.
3. For unmapped values, attempt label matching against AAT terms or
   assign the nearest broader AAT type.
4. Load into the `type_mappings` index as described in
   `type-mapping-plan.md` §5.3.

### Phase 3: Administrative boundaries

`boundary=administrative` features are ingested into the **`places`
index** during normal OSM ingestion (same pipeline as all other
boundary types, using the expanded tag key list from Phase 1).  They
are ALSO indexed into a dedicated **`boundaries` index** with the
`admin_level` field, which feeds the "Space" filter in the search UI.

Requirements for the boundaries index:
- `admin_level` (integer 2–10) as a filterable field
- Boundary polygon geometries (from OSM relations)
- Cross-referencing with `place=*` settlement data where applicable
- Name and country-code fields for spatial filter UI display

The `places` index ingestion does not require separate handling — the
expanded `process_tags()` filter gate (Phase 1) already accepts
`boundary` as a qualifying key.  The `create_doc()` type extraction
loop emits `{identifier: "administrative", label: "osm",
sourceLabel: "boundary=administrative"}` alongside any other types
present on the feature.

---

## Appendix A: Tag Key Quick Reference

All OSM primary feature keys assessed for WHG relevance:

| Key | Gazetteer relevant? | Current status | Priority |
|-----|---------------------|----------------|----------|
| `place` | ✓ Core | Extracted | Tier 1 |
| `natural` | ✓ Core | Extracted | Tier 1 |
| `water` | ✓ Core | Extracted | Tier 1 |
| `waterway` | ✓ Core | Extracted | Tier 1 |
| `historic` | ✓ Core | Extracted | Tier 1 |
| `landuse` | ✓ Core | Extracted | Tier 1 |
| `amenity` | ✓ Institutional/cultural | Not extracted | Tier 2 |
| `tourism` | ✓ Cultural landmarks | Not extracted | Tier 2 |
| `leisure` | ✓ Parks and reserves | Not extracted | Tier 2 |
| `man_made` | ✓ Landmark structures | Not extracted | Tier 2 |
| `boundary` | ✓ Parks/reserves/admin | Not extracted | Tier 2† |
| `military` | ✓ Military installations | Not extracted | Tier 2 |
| `building` | Partial (allowlist fallback) | Not extracted | Tier 2* |
| `aeroway` | ✓ Airports | Not extracted | Tier 3 |
| `railway` | ✓ Stations | Not extracted | Tier 3 |
| `geological` | Marginal | Not extracted | Tier 3 |
| `power` | Marginal (plants only) | Not extracted | Tier 3 |
| `highway` | ✗ Roads, not places | — | — |
| `shop` | ✗ Commercial, not places | — | — |
| `office` | ✗ Not places | — | — |
| `craft` | ✗ Not places | — | — |
| `barrier` | ✗ Not places | — | — |
| `telecom` | ✗ Not places | — | — |
| `route` | ✗ Not places (linear routes) | — | — |
| `sport` | ✗ Usually on leisure/amenity | — | — |
| `emergency` | ✗ Modern services | — | — |
| `healthcare` | Marginal (mostly via amenity) | — | Tier 4 |
| `public_transport` | ✗ Mostly via railway | — | — |

† `boundary`: all values including `administrative` go into places
index; `administrative` also feeds a separate admin boundaries index.
\* `building`: only curated allowlist of significant types.
