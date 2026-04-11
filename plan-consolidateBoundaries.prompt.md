# Plan: Consolidate Boundaries into Places via Typed Geometry + Post-Indexing Tilesets

Eliminate the `boundaries` ES index by enriching the `places` index: add `hull` and `bounds` to the `geometries[]` nested object for all authorities; add a top-level `boundary` string field to identify boundary-qualifying records. For OSM/OHM `boundary=administrative` features, store the `admin_level` value as a string ("0"–"11"), extending the standard OSM range (2–11) with "0" (continent) and "1" (sub-continent) — safe because OSM policy reserves level 1 unused. For a curated set of miscellaneous OSM/OHM `boundary=<type>` tags, store the type value itself (e.g. `"aboriginal_lands"`, `"parish"`). For non-OSM boundary authorities, store `"polity"` (Cliopatria), `"native"` (NativeLand), or `"period"` (PeriodO). Remove all geometry simplification from canonical scripts. Decouple tileset generation into a standalone post-indexing step. Add new `po:` (PeriodO) and `clio:` (Cliopatria) authority scripts. Create a shared AAT-mapping helper that loads the `types` ES index per authority for fast lookups, usable both at ingestion and as a standalone mapping-update process.

## Steps

### 1. Augment the `places` schema in `schemas/places.json`

Within `geometries[]` (nested), add `hull` (`geo_shape` — convex hull) and `bounds` (`float` array — `[west, south, east, north]`) alongside the existing `geom`, `repr_point`, and `timespans`. Compute both on ingestion for **all** geometries from **all** authorities — minor bloat, useful for spatial pre-screening everywhere. Add a new **top-level** `boundary` field (`keyword`) on the place document. Values fall into three categories:

**(a) Admin levels** — string "0" through "11", where "0" = continent and "1" = sub-continent (both synthesized by WHG), "2"–"11" = standard OSM `admin_level` tag values.

**(b) Miscellaneous OSM/OHM boundary types** — the `boundary=<type>` tag value stored verbatim for the curated set listed below; flagged only when the feature does **not** also carry an `admin_level`.

**(c) Non-OSM authorities** — `"polity"` (Cliopatria), `"native"` (NativeLand), `"period"` (PeriodO).

The field is absent on non-boundary records. Note: the semantics of admin levels 3–6 vary between countries; the UI level selector must communicate this.

**Curated miscellaneous boundary types** (OSM/OHM `boundary=<type>` values to flag): `aboriginal_lands`, `barony`, `civil`, `civil_parish`, `climatic_zone`, `cofi_parish`, `environment`, `geographic`, `histori*` (prefix match — includes `historic`, `historic:administrative`, `historic_diocese`, etc.), `indigenous_administration`, `local_authority`, `native_reservation`, `obsolete_administrative`, `old_administrative`, `parish`, `political`, `rc_parish`, `region`. These will be treated as a distinct source in the UI, labelled "OSM/OHM (Miscellaneous)", separate from "OSM (Modern)" and "OHM (Historical)". All other `boundary=<type>` records (e.g. `postal_code`, `census`, `marker`) are ingested as places but **without** a `boundary` flag.

Note: OSM features tagged `boundary=continent` and `boundary=country_border` may be useful for identifying level 0 and level 2 entities respectively when explicit `admin_level` tags are absent.

### 2. Create a shared AAT-mapping helper module (`processing/aat_lookup.py`)

This replaces the obsolete `typesystem/merge_mappings.py`, which batch-applied data-file mappings; the `types` index is now updated incrementally by the Django mapping UI. The new module provides `load_aat_mappings(es_client, vocabulary: str) → dict`, which queries the `types` index for all docs where the relevant cross-vocabulary field is non-empty (`gn_fcodes` for GeoNames, `osm_tags` for OSM, `wd_qids` for Wikidata, etc.) and builds an in-memory reverse lookup dict (e.g. `"place=city" → [300008389]`). A second function `apply_aat_mappings_to_index(es_client, vocabulary, places_index)` scrolls existing place docs for a given namespace and bulk-updates their `types[]` entries with current AAT mappings — enabling periodic re-application when mappings change without re-ingesting authority data. Both the ingestion scripts and this standalone updater share the same lookup logic.

### 3. Create shared `enrich_geometry()` helper in `processing/helpers.py`

Accepts a GeoJSON geometry dict and returns `{geom, repr_point, hull, bounds}`. Computes convex hull via Shapely and extracts the envelope as `[west, south, east, north]`. Replaces the current ad-hoc `compute_representative_point()` pattern. All authority scripts call this instead of building geometry entries manually.

### 4. Remove all geometry simplification from canonical authority scripts

Delete `geom.simplify(...)` calls in `authorities/osm-places.py` (lines 222, 242), `authorities/ohm-places.py` (lines 307, 329), `authorities/nativeland-places.py` (lines 42, 80, 115), and `authorities/un-countries.py` (line 165). Retain `make_valid()` calls. Canonical ES geometry must be faithful to the source; simplification belongs only in derivative artefacts like tilesets.

### 5. Create an OSM/OHM boundary-pass script (`authorities/osm-boundary-pass.py`)

After the main single-pass `osm-places.py` ingestion (which indexes all named OSM features including boundary features as point/crude-geometry docs), this script runs as a second pass to assemble full multipolygon geometry for boundary relations. Pre-filters the PBF with `osmium tags-filter` for `r/boundary=administrative` plus all curated miscellaneous types (reusing logic from `authorities/osm-boundaries.py`), then uses `FileProcessor.with_areas()` for two-pass multipolygon assembly. For each assembled relation, issues a partial `_update` to the existing `osm:r{id}` (or `ohm:r{id}`) doc in the `places` index: replaces the geometry entry using `enrich_geometry()`, and sets the top-level `boundary` field — the `admin_level` tag value (as a string "2"–"11") for `boundary=administrative`, or the boundary type string for curated miscellaneous types. Features with both `boundary=administrative` + `admin_level` AND a miscellaneous type use the admin level. Accepts `--source osm|ohm` flag. Does **not** produce GeoJSONL or mbtiles.

### 6. Refactor UN Geoscheme (`authorities/un-geoscheme-boundaries.py`)

Change namespace from `m49:` to `osm:` with synthetic deterministic IDs (e.g. `osm:m49_africa`). Write into the `places` index. Set `boundary: "0"` for continents and `boundary: "1"` for sub-continental M49 regions. Add a type entry like `{identifier: "synthetic_backfill", label: "aat", sourceLabel: "m49-derived"}` to signal these are synthesised. Change `fetch_country_geometries()` to query the `places` index filtering on `boundary` values "2"–"11" instead of the `boundaries` index. A full set of UN countries must still be present in order to assemble the M49 sub-continental regions. Add a verification step checking all M49 country codes are accounted for (investigate France's `ISO3166-1` vs `ISO3166-1:alpha2` tag discrepancy). Confirm Antarctica is included.

### 7. Add PeriodO authority script (`authorities/periodo-places.py`, namespace `po:`)

Fetch the PeriodO JSON-LD dataset. Adapt correction/processing logic from `/home/stephen/Documents/GitHub/whg3/periods`. PeriodO periods have spatial coverage polygons and temporal extents — map to `geometries[]` with `timespans`. All PeriodO records are boundary-type: set `boundary: "period"`. Use `enrich_geometry()` and the AAT lookup helper. Add to `AUTHORITIES` in `processing/settings.py`.

### 8. Add Cliopatria authority script (`authorities/cliopatria-places.py`, namespace `clio:`)

Fetch `cliopatria.geojson.zip` from `https://github.com/Seshat-Global-History-Databank/cliopatria/blob/main/cliopatria.geojson.zip`. All records are boundary polygons with temporal data. Code from scratch. Set `boundary: "polity"`. Use `enrich_geometry()` and the AAT lookup helper. Add to `AUTHORITIES` in `processing/settings.py`.

### 9. Create standalone tileset generator (`processing/generate_tiles.py`)

Accept `--es-host` and optional `--authority`. Identify boundary-qualifying places by querying on `boundary` field existence (`exists` filter). Group results by namespace prefix from `place_id`. For each authority with results: scroll matching docs, write geometry + properties (`title`, `boundary`, `namespace`, `tippecanoe:minzoom` derived from `boundary` value) as GeoJSON Lines to a temp file, invoke `tippecanoe` to produce `{namespace}.mbtiles`. The miscellaneous OSM/OHM boundaries should produce a separate tileset (e.g. `osm_misc.mbtiles`) distinct from the admin-level `osm.mbtiles`. Add `es -generate-tiles` command in `scripts/ingest.sh` as a Slurm job. Add an `scp`-based deploy step to push `.mbtiles` to TileServer GL light (via `ssh tileserver`), update its `config.json`, and restart the service if a new tileset was added.

### 10. Remove obsolete code

Delete `schemas/boundaries.json`. Delete `authorities/osm-boundaries.py` (logic split between the new boundary-pass script and the tileset generator). Delete or archive `typesystem/merge_mappings.py` (superseded by the AAT lookup helper and Django UI). Remove `BOUNDARIES_INDEX` from `processing/settings.py`, `gateway/config.py`, and `processing/utilities.py`. Remove boundary-specific branching (`is_boundary_script`, `boundary_scripts`, `OSM_BOUNDARY_STATE_FILE`, `OHM_BOUNDARY_STATE_FILE`) from `processing/ingest_all_authorities.py` and `processing/settings.py`. Remove `do_ingest_boundaries()` and the old `do_generate_tiles()` from `scripts/ingest.sh`; replace with `do_boundary_pass()` (Slurm job for step 5) and the new `do_generate_tiles()` (Slurm job for step 9). Update `CLAUDE.md`.

## Further Considerations

1. **Admin level semantics**: Levels 3–6 vary significantly between countries (e.g. France's *département* is level 6, Germany's *Kreis* is also 6 but functionally different). The Space filter UI level selector should display levels as numeric ranges with a caveat rather than implying universal semantic equivalence. Country-specific label mapping could be a later enhancement.

2. **France and UN Geoscheme completeness**: France's level-2 OSM relation likely uses `ISO3166-1=FR` rather than `ISO3166-1:alpha2=FR`, or overseas territories have separate relations. The verification step should try multiple tag patterns and aggregate all relations with `ccodes` containing `FR`.

3. **Cliopatria data structure**: The GeoJSON zip at the Seshat GitHub URL needs inspection to determine the property schema (feature IDs, temporal fields, type vocabulary). This should be done early, before coding the authority script.

