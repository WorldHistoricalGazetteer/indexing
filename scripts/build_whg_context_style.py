"""Build tileserver/whg-context.style.json with banding metadata.

This script is the source-of-truth generator for the WHG context style
served at /styles/whg-context on the tileserver. It writes:

  - Vector sources for all tilegen buckets
  - Per-tileset layers (line, fill, label) with whg:switchgroup/switchlabel
    metadata so the UI layer switcher exposes each toggle
  - z=0-1 visibility for continental + country admin lines (the new
    addition for global views)
  - A top-level metadata.whg:tilegen.buckets block declaring the BANDS
    that processing/generate_tiles.py uses to partition features for
    multi-band tippecanoe runs (then tile-join'd into the canonical
    bucket mbtiles)

Layer visibility (minzoom/maxzoom) and band visibility (in the metadata
block) are kept in sync — the band's max/min zoom must cover every
layer that filters on its features, otherwise some style layers would
have no underlying tile data.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent.parent / "tileserver" / "whg-context.style.json"

# ─── tilegen banding metadata (read by generate_tiles.py) ─────────────
# Bands are partitions of the features destined for one bucket; each
# band gets its own tippecanoe run with band-specific min/max zoom, and
# the resulting mbtiles are tile-join'd into the canonical bucket file.
# Where clause: ``{"property": "X", "in": [...]}`` matches features with
# property X in the listed values. Features matching no band are dropped.
ADMIN_BANDS = [
    {"name": "continental", "minzoom": 0, "maxzoom": 4,
     "where": {"property": "boundary", "in": ["0", "1"]}},
    {"name": "country",     "minzoom": 0, "maxzoom": 8,
     "where": {"property": "boundary", "in": ["2"]}},
    {"name": "state",       "minzoom": 3, "maxzoom": 10,
     "where": {"property": "boundary", "in": ["3", "4"]}},
    {"name": "district",    "minzoom": 5, "maxzoom": 10,
     "where": {"property": "boundary", "in": ["5", "6"]}},
    {"name": "local",       "minzoom": 7, "maxzoom": 10,
     "where": {"property": "boundary", "in": ["7", "8", "9", "10", "11"]}},
]

# osm_misc bands — based on actual `boundary` tag distribution observed
# in osm_misc.mbtiles (44,261 features, 33 distinct values, all admin/
# historical varieties — NOT water/forest/park as the original draft
# guessed). Mutually exclusive: each historic_* value lands in
# `historical`, parish/civil values in `parish-local`, broad regions in
# `broad-regional`. Any unmatched value is dropped (none observed yet).
HISTORICAL_VALUES = [
    "historic", "historical",
    "historic-parish", "historic:administrative", "historic:county",
    "historical:administrative", "historic_administrative",
    "historic_church_province", "historic_county", "historic_diocese",
    "historic_locality", "historic_lot", "historic_parish",
    "historic_parish_union", "historic_political", "historic_riding",
    "historic_site", "political",
]
PARISH_LOCAL_VALUES = [
    "civil", "civil_parish", "barony", "parish",
    "cofi_parish", "rc_parish", "local_authority",
    "aboriginal_lands", "indigenous_administration",
]
BROAD_REGIONAL_VALUES = [
    "region", "geographic", "environment", "climatic_zone",
    "obsolete_administrative", "old_administrative",
]
OSM_MISC_BANDS = [
    {"name": "broad-regional", "minzoom": 2, "maxzoom": 6,
     "where": {"property": "boundary", "in": BROAD_REGIONAL_VALUES}},
    {"name": "historical",     "minzoom": 4, "maxzoom": 10,
     "where": {"property": "boundary", "in": HISTORICAL_VALUES}},
    {"name": "parish-local",   "minzoom": 6, "maxzoom": 10,
     "where": {"property": "boundary", "in": PARISH_LOCAL_VALUES}},
]

# Single-band buckets — the simple ones with no internal categorisation.
# Listed explicitly so generate_tiles.py knows the canonical zoom range
# even though there's only one tippecanoe run.
SINGLE_BAND_2_10 = [{"name": "all", "minzoom": 2, "maxzoom": 10,
                      "where": None}]   # None = match all features

TILEGEN_BUCKETS = {
    "osm_admin": ADMIN_BANDS,
    "ohm_admin": ADMIN_BANDS,
    "osm_misc":  OSM_MISC_BANDS,
    "po":        SINGLE_BAND_2_10,
    "clio":      SINGLE_BAND_2_10,
    "nl":        SINGLE_BAND_2_10,
}


# ─── style helper builders ────────────────────────────────────────────
def admin_line_layer(*, src, level_id, label, level_filter, color, layer_minzoom, layer_maxzoom):
    src_group = {"osm_admin": "OSM Boundaries", "ohm_admin": "OHM Boundaries"}[src]
    return {
        "id": f"{src}-line-{level_id}",
        "type": "line",
        "metadata": {"whg:switchgroup": src_group, "whg:switchlabel": label},
        "source": src, "source-layer": src,
        "minzoom": layer_minzoom, "maxzoom": layer_maxzoom,
        "filter": level_filter,
        "layout": {"visibility": "none", "line-join": "round"},
        "paint": {
            "line-color": color,
            "line-width": {"stops": [[2, 0.3], [10, 0.8]]},
        },
    }


def label_layer(*, src, level_id, label, level_filter, lbl_minzoom, lbl_maxzoom):
    src_group = {"osm_admin": "OSM Boundaries", "ohm_admin": "OHM Boundaries"}[src]
    return {
        "id": f"{src}-label-{level_id}",
        "type": "symbol",
        "metadata": {"whg:switchgroup": src_group + " Labels", "whg:switchlabel": label},
        "source": src, "source-layer": src,
        "minzoom": lbl_minzoom, "maxzoom": lbl_maxzoom,
        "filter": level_filter,
        "layout": {
            "visibility": "none",
            "text-field": ["coalesce", ["get", "name_local"], ["get", "name"]],
            "text-size": 11,
            "symbol-placement": "point",
        },
        "paint": {
            "text-color": "#444",
            "text-halo-color": "#fff",
            "text-halo-width": 1,
        },
    }


def make_layers():
    layers = []
    # Existing pre-tileset layers (basemap + ice + water + terrain etc).
    # Keep these light here — the existing whg-context style has a much
    # richer pre-tileset stack; this generator focuses on the boundary
    # additions that are the only thing changing this round. The existing
    # layers will be preserved from the live style.json on push (we
    # merge into existing 'layers' array rather than overwrite).

    admin_palette = {"osm_admin": "#7090b0", "ohm_admin": "#a07060"}
    admin_levels = [
        # (level_id, label, filter,                                                   layer_minzoom, layer_maxzoom, lbl_min, lbl_max)
        ("continental", "Continental", ["match", ["get", "boundary"], ["0", "1"], True, False],   0, 4, 0, 5),
        ("country",     "Country",     ["==", ["get", "boundary"], "2"],                             0, 8, 2, 8),
        ("state",       "State / Region", ["match", ["get", "boundary"], ["3", "4"], True, False], 3, 10, 3, 10),
        ("district",    "District",    ["match", ["get", "boundary"], ["5", "6"], True, False],     5, 10, 5, 10),
        ("local",       "Local",       ["match", ["get", "boundary"], ["7", "8", "9", "10", "11"], True, False],  7, 10, 7, 10),
    ]
    for src in ("osm_admin", "ohm_admin"):
        color = admin_palette[src]
        for lev_id, label, lev_filter, lyr_min, lyr_max, lbl_min, lbl_max in admin_levels:
            layers.append(admin_line_layer(
                src=src, level_id=lev_id, label=label, level_filter=lev_filter,
                color=color, layer_minzoom=lyr_min, layer_maxzoom=lyr_max,
            ))
            layers.append(label_layer(
                src=src, level_id=lev_id, label=label, level_filter=lev_filter,
                lbl_minzoom=lbl_min, lbl_maxzoom=lbl_max,
            ))

    # osm_misc: one fill layer per band, color-coded.
    misc_paint = {
        "broad-regional": "#9080a0",
        "historical":     "#a07a4a",
        "parish-local":   "#5a8eb0",
    }
    misc_zooms = {
        "broad-regional": (2, 6),
        "historical":     (4, 10),
        "parish-local":   (6, 10),
    }
    misc_filters = {
        "broad-regional": ["match", ["get", "boundary"], BROAD_REGIONAL_VALUES, True, False],
        "historical":     ["match", ["get", "boundary"], HISTORICAL_VALUES,     True, False],
        "parish-local":   ["match", ["get", "boundary"], PARISH_LOCAL_VALUES,   True, False],
    }
    for band_id, color in misc_paint.items():
        zmin, zmax = misc_zooms[band_id]
        layers.append({
            "id": f"osm_misc-fill-{band_id}",
            "type": "fill",
            "metadata": {"whg:switchgroup": "OSM/OHM Misc Boundaries",
                          "whg:switchlabel": band_id.replace("-", " ").title()},
            "source": "osm_misc", "source-layer": "osm_misc",
            "minzoom": zmin, "maxzoom": zmax,
            "filter": misc_filters[band_id],
            "layout": {"visibility": "none"},
            "paint": {"fill-color": color, "fill-opacity": 0.18, "fill-outline-color": color},
        })

    # po / clio / nl: one fill+outline per source (single band each).
    historical_sources = [
        ("po",   "PeriodO Period Extents",        "#a07ab8"),
        ("clio", "Clio-Infra Historical States",  "#b88a52"),
        ("nl",   "Native Land Territories",        "#5a8470"),
    ]
    for src_id, label, color in historical_sources:
        layers.append({
            "id": f"{src_id}-fill",
            "type": "fill",
            "metadata": {"whg:switchgroup": "Historical / Indigenous", "whg:switchlabel": label},
            "source": src_id, "source-layer": src_id,
            "minzoom": 2, "maxzoom": 10,
            "layout": {"visibility": "none"},
            "paint": {"fill-color": color, "fill-opacity": 0.16, "fill-outline-color": color},
        })
        layers.append({
            "id": f"{src_id}-line",
            "type": "line",
            "metadata": {"whg:switchgroup": "Historical / Indigenous",
                          "whg:switchlabel": label + " (outline)"},
            "source": src_id, "source-layer": src_id,
            "minzoom": 2, "maxzoom": 10,
            "layout": {"visibility": "none", "line-join": "round"},
            "paint": {"line-color": color, "line-width": 0.6, "line-opacity": 0.7},
        })

    return layers


def main():
    style = {
        "version": 8,
        "name": "WHG Context",
        "metadata": {
            "whg:backgroundcolour": "#c3ddec",
            "mapbox:autocomposite": False,
            "mapbox:type": "template",
            "whg:tilegen": {
                "buckets": TILEGEN_BUCKETS,
            },
        },
        "center": [20.73, 51.91],
        "zoom": 2.3,
        "bearing": 0,
        "pitch": 0,
        "sources": {
            # Carry over the basemap/terrain/etc. sources from the
            # currently-live whg-context style so this draft is a
            # drop-in replacement, not a degraded one. Deploy script
            # will MERGE this into the live style rather than overwrite.
            "basemap": {
                "type": "raster", "maxzoom": 6,
                "url": "mbtiles://{natural-earth-2-landcover}",
            },
            "terrarium-local": {
                "type": "raster-dem", "url": "mbtiles://{terrarium}",
                "minzoom": 0, "maxzoom": 8, "tileSize": 256,
                "encoding": "terrarium",
            },
            "terrarium-aws": {
                "type": "raster-dem",
                "tiles": ["https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"],
                "minzoom": 9, "maxzoom": 15, "tileSize": 256,
                "encoding": "terrarium",
            },
            "natural_earth": {
                "type": "vector", "maxzoom": 7,
                "url": "mbtiles://{whg-ne-basic}",
            },
            "osm_admin": {"type": "vector", "url": "mbtiles://{osm_admin}", "minzoom": 0, "maxzoom": 10},
            "ohm_admin": {"type": "vector", "url": "mbtiles://{ohm_admin}", "minzoom": 0, "maxzoom": 10},
            "osm_misc":  {"type": "vector", "url": "mbtiles://{osm_misc}",  "minzoom": 2, "maxzoom": 10},
            "po":        {"type": "vector", "url": "mbtiles://{po}",        "minzoom": 2, "maxzoom": 10},
            "clio":      {"type": "vector", "url": "mbtiles://{clio}",      "minzoom": 2, "maxzoom": 10},
            "nl":        {"type": "vector", "url": "mbtiles://{nl}",        "minzoom": 2, "maxzoom": 10},
        },
        "glyphs": "{fontstack}/{range}.pbf",
        "sprite": "{styleJsonFolder}/sprite",
        "layers": make_layers(),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        json.dump(style, f, indent=2)

    # Sanity-check JSON round-trip.
    with OUT.open() as f:
        json.load(f)

    print(f"wrote {OUT}")
    print(f"  sources: {sorted(style['sources'])}")
    print(f"  layers:  {len(style['layers'])}")
    print(f"  buckets in metadata.whg:tilegen.buckets: "
          f"{sorted(style['metadata']['whg:tilegen']['buckets'])}")


if __name__ == "__main__":
    main()
