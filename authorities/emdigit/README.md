# EMDigIt — Early Modern Digital Itineraries (NEW FEATURE / not yet ingested)

> **Status:** raw data **captured**, metadata **stubbed**. This is a **new
> gazetteer "breed" (itineraries / traces)** — see the tracking GitHub issue on
> `WorldHistoricalGazetteer/place`. **Not yet ingested; no authority script yet.**
> The itinerary-edge modelling is deliberately deferred (design decision for later).

## What it is

**Early Modern Digital Itineraries (EMDigIt)** — a Virginia Tech digital-humanities
project mapping early-modern (~1560s–1720s) travel itineraries through Italy and
Europe as a **network of waypoints connected by route edges**. Discovered via the
public "EMDigIt Demonstration Map" ArcGIS Instant App.

## Source (all public, ArcGIS Online org `V2PQwgZMTFfgM0Xu`, owner `rmidura_virginiatech`)

- **App:** https://virginiatech.maps.arcgis.com/apps/instant/sidebar/index.html?appid=d6561b1d25584b82948a52c2d6de01e1
- **Web map:** item `347c303662c540b892f54006da585c35`
- **Waypoints (points):** `Early_Modern_Digital_Itineraries_(EMDigIt)__Core_Italian_Itinerary_Waypoints/FeatureServer/0`
  (ArcGIS REST — `?where=1=1&outFields=*&f=geojson`, paginate `resultOffset` @ 1000/req)
- **Routes (line edges):** `EMDigIt_(Early_Modern_Digital_Itineraries)_Core_Italian_Itinerary_Routes/OGCFeatureServer`
  (OGC API Features — `/collections/0/items?f=json&limit=1000&offset=N`)

## Captured data (this folder)

- `data/emdigit_waypoints.geojson` — **3,782** point features (2.0 MB)
- `data/emdigit_routes.geojson` — **6,075** LineString edge features (5.3 MB)

Fetched 2026-07-15 via `scratchpad/emdigit_fetch.py` (paginated REST/OGC pulls).

## Data shape

**Waypoints** (`FeatureServer/0`) — fields:
`node_id`, `id`, `Location_Name` (pipe-separated historical name variants, e.g.
`"a coruña|corvigna|la corugna"`), `geoname`, **`geonameId`** (GeoNames id),
`location_lat`, `location_lng`, `state`, **`country_code`** (ISO A2), `features`
(place type, e.g. `"City"`), **`min_date`** / **`max_date`** (int years),
`observations`, `itinerary_names`, `point_status`.

**Routes** (`OGCFeatureServer`) — a directed **graph of edges**, one feature per hop:
`source` (node_id) → `target` (node_id), `edge_id`, `source_lat/lng`, `target_lat/lng`,
`distance_km`, elevation/slope stats (mostly null). Geometry = 2-point LineString.
So itineraries are a **node→node network**, not pre-drawn polylines; an itinerary is a
path through this graph (waypoints also carry `itinerary_names`).

## Proposed WHG mapping (for whenever this is built)

**Waypoints → `places` docs** (namespace `emdigit`):
- `place_id` = `emdigit:<node_id>`
- `title` = `geoname` (or first `Location_Name` variant)
- `toponyms[]` = the pipe-separated `Location_Name` variants + `geoname`
- `geometries[]` = point from `location_lng/lat`
- `ccodes` = `[country_code]`
- `types[]` = from `features` (AAT-mappable — e.g. City → AAT settlement)
- `timespans` = `{start:{in:min_date}, end:{in:max_date}}`
- `links[]` = **`seeAlso` → `gn:<geonameId>`** — a *hard link* to the existing `gn`
  authority (instant clustering interlink; no phonetic guessing). ~most waypoints
  carry a `geonameId`.

**Routes → itinerary edges = the "new breed" (DEFERRED).** WHG's `places` schema has
no native journey/trace type. Candidate representations (decide later):
1. fold edges into `relations[]` on each waypoint (`relation_type: itinerary_next`/
   `connected`, `related_place_id`, itinerary name, `distance_km`); or
2. a first-class **itineraries/traces** structure/index (ordered waypoint sequences +
   the edge network) — truer to the "new breed", bigger schema/gateway/UI work.

## Proposed AUTHORITIES entry (stub — not yet added to `processing/settings.py`)

```python
{
    'dataset_name': 'Early Modern Digital Itineraries (EMDigIt)',
    'namespace': 'emdigit',
    'citation_text': 'Early Modern Digital Itineraries (EMDigIt), Virginia Tech.',
    'license_spdx': 'TBD',            # confirm — VT/EMDigIt terms (public ArcGIS layers)
    'license_url': 'TBD',
    'rights_holder': 'Virginia Tech (EMDigIt project)',
    'source_url': 'https://virginiatech.maps.arcgis.com/apps/instant/sidebar/index.html?appid=d6561b1d25584b82948a52c2d6de01e1',
    'contributors': [],
    'files': [],  # captured directly from the ArcGIS/OGC REST endpoints above
},
```

## Open questions / TODO (tracked in the `place` issue)

- Confirm **licence / attribution** with the EMDigIt team (defer gating per WHG policy).
- Decide the **itinerary-edge representation** (relations vs a traces model).
- Whether the itinerary graph should drive a dedicated **itinerary UI** in Atlas.
- Refresh strategy (the ArcGIS layers are live — re-pull vs snapshot).
