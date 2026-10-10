# processing/generate_tiles.py

"""
Staged Tileset Generator (Batch 10).

Reads boundary-qualifying records from the staged snapshot pipeline
(``final/`` → ``h3_merged/`` → ``boundary_merged/`` → ``extract/``) and pulls
full polygon geometries from the external geometry store. No Elasticsearch
dependency.

The output layout has three families of bucket:

* **Fixed buckets** — boundary-gated:
  ``osm.mbtiles``, ``ohm.mbtiles`` (administrative levels — bucket name
  equals the authority namespace per the contract in
  ``whg3/developer/plan-tileset-namespace-rename.contract.md``) and
  ``osm_misc.mbtiles`` (mixed OSM/OHM misc-boundary features — outlier
  that stays under its category-cluster name because it spans both
  namespaces and isn't a namespace itself).
* **Per-namespace buckets** — one ``<ns>.mbtiles`` per authority namespace
  containing every doc with renderable geometry (point or polygon). Used by
  the redesigned WHG Atlas to render each gazetteer as its own layer.
* **Per-WHG-dataset buckets** — one ``whg-<dataset_sub_id>.mbtiles`` per WHG
  contributor dataset, discovered at submit time from
  ``staged/_aggregates/whg.datasets.json``.

Tile generation is **bucket-driven**: each bucket has a fixed (or computed)
list of contributing namespaces and a single owning writer, so concurrent
Slurm tasks (one per bucket) never race on the same output file.

Multilingual labels come from ``toponyms[]`` (``toponym_id`` in
``name@lang`` format).

Usage::

    python -m processing.generate_tiles
    python -m processing.generate_tiles --bucket osm --bucket ohm
    python -m processing.generate_tiles --bucket gn --bucket wd
    python -m processing.generate_tiles --bucket whg-1234
    python -m processing.generate_tiles --run-id <RUN_ID>
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import shutil
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import orjson

from processing.feature_ids import (
    encode_feature_id,
    encode_misc_feature_id,
)
from processing.geom_store import GeomStoreReader
from processing.osm_boundary_geometry import (
    is_admin_boundary_value,
    is_misc_boundary_value,
)
from processing.gazetteer_temporal_extent import doc_temporal_bounds
from processing.settings import (
    DATA_DIR,
    GEOM_STORE_DIR,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)


TILES_OUTPUT_DIR = Path(DATA_DIR) / 'tiles'

# Per-feature temporal sentinels for the Atlas client-side date filter
# (place#131, place#176). Features carry the possible envelope as
# ``start``/``end`` and, where a timespan pins both inner bounds, the attested
# core as ``start_def``/``end_def`` — so the map can express the SAME two modes
# as the gateway query (place#169):
#
#   possibly   ['any', ['!', ['has','start']],
#                      ['all', ['<=',['get','start'],toYear],
#                              ['>=',['get','end'],fromYear]]]
#   definitely ['all', ['has','start_def'], ['has','end_def'],
#                      ['<=',['get','start_def'],toYear],
#                      ['>=',['get','end_def'],fromYear]]
#
# ⚠️ CORRECTED 3 Sep. This sketch previously gave *definitely* as CONTAINMENT
# (``start_def <= fromYear AND end_def >= toYear`` — alive THROUGHOUT the
# window). That is wrong, and it contradicted both the gateway and this very
# comment's own *possibly* line, which is an overlap test. The gateway's
# ``es_helpers._temporal_filter`` is interval OVERLAP in BOTH modes — its
# docstring: *"The window is an interval, so the test is interval overlap...
# Which bound stands for 'the place's start' is what the mode selects"* — and
# the deployed client mirror ``atlas.js::temporalHitPasses`` is overlap too.
# **The mode selects WHICH BOUND is compared (inner vs outer), never the shape
# of the comparison.** A client built to the old sketch would disagree with the
# result list on every partially-overlapping record — precisely the map/list
# divergence place#169 exists to close. Caught by whg3-74 while implementing
# place#176's §3.2, which correctly followed the gateway over this comment.
#
# The envelope reads *both* ``start`` and ``end`` on every dated feature, so an
# open-ended span can't leave a bound absent (a missing ``end`` would wrongly
# hide every still-current place). We therefore emit a sentinel for the open
# side, and an attestation — unbounded on BOTH sides — gets both sentinels:
#   ongoing  (start, no end)  -> end   = TILE_OPEN_END_YEAR   (always >= fromYear)
#   open-start (end, no start)-> start = TILE_OPEN_START_YEAR (always <= toYear)
#   attested-at-an-instant    -> both sentinels; never hidden in *possibly*, and
#                               refused by *definitely* unless the core fits.
#   undated  (no timespans)   -> omit everything; the client admits these in
#                               *possibly* only, via ['!',['has','start']].
# The core carries NO sentinels: its absence is the signal that no *definitely*
# window can ever be satisfied, which is exactly what ``unbounded_passes=False``
# means on the query side. This mirrors the gateway's two modes + ``undated``
# semantics (``gateway/es_helpers.py``). Fixed (not build-year) so tiles stay
# reproducible; the values only need to fall outside any real query window.
TILE_OPEN_END_YEAR = 9999
TILE_OPEN_START_YEAR = -9999

# Low-zoom COVERAGE FOOTPRINT for polygon gazetteers (place#140). A polygon-only
# gazetteer renders a deceptively sparse scatter of tiny fills at low zoom even
# when it has tens of thousands of features. (An earlier cut fed the Atlas
# ``_heat`` layer with polygon centroid points, but that reads as a misleading
# sparse dot field — see the reopened #140.) Instead, for every bucket that
# streams ANY shape (place#166: formerly only polygon-*dominant* buckets) we
# emit ONE **dissolved** (unary_union) footprint polygon, tagged
# ``coverage: 1`` and capped at ``_COVERAGE_MAXZOOM``, so the Atlas can style it
# as a solid "this gazetteer covers this region" fill at low zoom; the real
# shapes are pinned to ``_BOUNDARY_MINZOOM`` and take over on zoom-in. The two
# passes are tiled separately and ``tile-join``'d into the single source-layer
# (see ``developer/place140-coverage-design/`` for the model + whg3 styling).
#
# ``_COVERAGE_MAXZOOM`` (footprint present z0..this) is one below
# ``_BOUNDARY_MINZOOM`` (boundaries present from here up) so there is exactly one
# clean hand-off zoom and the two never overlap.
_COVERAGE_MAXZOOM = 7
_BOUNDARY_MINZOOM = 8

# Simplification tolerance (degrees) applied to the dissolved footprint before
# tiling. ~0.008° (~900 m) keeps the outline light (footprints are ~0.2 MB even
# for the 23k-parish kain_par) while staying visually faithful at z0-7.
_COVERAGE_SIMPLIFY_DEG = 0.008

# Per-geometry CHANNELS (place#166). A bucket used to pick ONE tiling mode by
# majority vote (``polygon > point``), and that one switch decided four
# unrelated things at once: whether a low-zoom extent is emitted, whether the
# real features are pinned to z8+, whether points are clustered, and whether
# tippecanoe runs no-drop or coalesce. Those are properties of a geometry
# TYPE, not of a bucket, so every hybrid gazetteer got at least one wrong —
# ``wd``'s polygons had no extent, ``pl``'s lines had no representation at
# all below z8 (and were counted as points), and ``hgis``'s points vanished
# below z8 and never heated.
#
# Now every feature routes itself by geometry type into one of four channels,
# each its own tippecanoe pass with its own zoom range and flags, and all are
# ``tile-join``ed into the ONE source-layer named after the bucket, told apart
# by a property — the convention ``coverage: 1`` already established:
#
#   channel   contents                         zooms   tippecanoe          marker
#   points    Point features                   0-10    cluster, coalesce   (Point, unmarked)
#   shapes    Polygon / LineString features    8-10    preserve_all        (geometry type)
#   extent    one dissolved footprint          0-7     plain               coverage: 1
#   labels    one anchor per shape             0-10    preserve_all        label: 1
#
# A bucket emits every channel its data supports; there is no vote. The z8
# pin applies to ``shapes`` only, and only when an ``extent`` was actually
# tiled to own the zooms below it — otherwise the shapes are tiled from z0
# so nothing can be invisible everywhere. ``extent`` is
# ``unary_union(polygons ∪ buffer(lines, _LINE_BUFFER_DEG))``; points
# contribute nothing to it because the clustered heatmap IS their extent.
CHANNEL_POINTS = "points"
CHANNEL_SHAPES = "shapes"
CHANNEL_EXTENT = "extent"
CHANNEL_LABELS = "labels"
CHANNELS: tuple[str, ...] = (CHANNEL_POINTS, CHANNEL_SHAPES, CHANNEL_EXTENT,
                             CHANNEL_LABELS)

# Opt-in tippecanoe ``--drop-rate`` for the clustered points channel (e.g.
# "1" = never rate-drop, cluster instead). Empty = tippecanoe's default 2.5.
# See the comment at the ``cluster_points`` flags in ``generate_tileset``.
_POINTS_DROP_RATE: str = os.getenv("WHG_POINTS_DROP_RATE", "").strip()

# Lines are buffered to this width (degrees) before joining the extent union,
# so a route gazetteer reads at z0-7 as the same mottle a polygon gazetteer
# does. Deliberately about the footprint's own simplify tolerance: a buffer
# narrower than that is lost to the final simplify, and a wider one claims
# area the line never covered.
_LINE_BUFFER_DEG = 0.01

# Interior rings smaller than this (square degrees, ~1 km²) are dropped from
# the dissolved extent. Each polygon is simplified BEFORE the union so the
# union of ~50k shapes is tractable (``wd``), and independently simplified
# neighbours leave sub-tolerance slivers along shared borders that would
# otherwise survive as pinholes in the footprint. Nothing of that size is
# visible at z7 (~1.2 km/px), the last zoom the extent is drawn at.
_EXTENT_MIN_HOLE_AREA = _COVERAGE_SIMPLIFY_DEG ** 2

# Sidecar written beside every built ``<bucket>.mbtiles``: what the stream
# counted and which channels were tiled at which zooms. It is the build's own
# ledger, and ``processing/verify_tileset_channels.py`` holds the tileset to
# it — a tileset whose ledger says "shapes were streamed" and whose metadata
# carries no ``label`` field fails verification. Without the ledger a
# verifier can only check a tileset against itself, which a broken build
# passes just as well as a working one.
CHANNEL_LEDGER_SUFFIX = ".channels.json"

# tippecanoe ``--postfilter`` that dedupes the ``;``-delimited ``aat`` string on
# clustered points (``--accumulate-attribute=aat:concat`` concatenates member
# strings without collapsing repeats). Committed alongside this module; jq-based
# and stdin-streaming so it adds negligible per-tile cost. Dedup is a size
# optimisation only — the ANY-of ``['in',';id;',['get','aat']]`` filter is
# already correct on the un-deduped concatenation.
AAT_POSTFILTER_SCRIPT = Path(__file__).with_name('tilegen_aat_postfilter.sh')

# Stage preference order when locating the input snapshot for a namespace.
# The first existing directory wins; tile generation does not require ccode
# enrichment, so any of these snapshots is acceptable for boundary records.
_STAGED_SOURCE_PRIORITY = (
    "final",
    "h3_merged",
    "boundary_merged",
    "update_merged",
    "extract",
)

# Curated display language set
DISPLAY_LANGUAGES = {
    'en', 'fr', 'es', 'ar', 'zh', 'ru',  # UN official languages
    'de', 'pt', 'ja', 'ko', 'hi',         # widely-used languages
}

# Country → primary local language (ISO 3166 alpha-2 → ISO 639-1)
COUNTRY_LOCAL_LANG = {
    'FR': 'fr', 'DE': 'de', 'ES': 'es', 'PT': 'pt', 'IT': 'it',
    'NL': 'nl', 'PL': 'pl', 'RU': 'ru', 'JP': 'ja', 'KR': 'ko',
    'CN': 'zh', 'TW': 'zh', 'IN': 'hi', 'SA': 'ar', 'EG': 'ar',
    'BR': 'pt', 'MX': 'es', 'AR': 'es', 'CL': 'es', 'CO': 'es',
    'TR': 'tr', 'GR': 'el', 'TH': 'th', 'VN': 'vi', 'ID': 'id',
    'MY': 'ms', 'PH': 'tl', 'UA': 'uk', 'CZ': 'cs', 'SE': 'sv',
    'NO': 'no', 'DK': 'da', 'FI': 'fi', 'HU': 'hu', 'RO': 'ro',
    'BG': 'bg', 'HR': 'hr', 'RS': 'sr', 'SK': 'sk', 'SI': 'sl',
    'LT': 'lt', 'LV': 'lv', 'EE': 'et', 'IS': 'is', 'IE': 'ga',
    'GB': 'en', 'US': 'en', 'CA': 'en', 'AU': 'en', 'NZ': 'en',
    'IL': 'he', 'IR': 'fa', 'PK': 'ur', 'BD': 'bn', 'MM': 'my',
    'KH': 'km', 'LA': 'lo', 'GE': 'ka', 'AM': 'hy', 'AZ': 'az',
    'KZ': 'kk', 'UZ': 'uz', 'MN': 'mn', 'ET': 'am', 'KE': 'sw',
    'TZ': 'sw', 'ZA': 'zu', 'NG': 'ha',
}

# Admin level → tippecanoe minzoom
ADMIN_LEVEL_MINZOOM = {
    '0': 0, '1': 0, '2': 0, '3': 2, '4': 3, '5': 4,
    '6': 5, '7': 6, '8': 7, '9': 8, '10': 9, '11': 10,
}

# Fixed buckets — boundary-gated; require ``boundary`` field on every doc and
# pull full polygons from the geom store. The mixed ``osm_misc`` bucket is
# owned by one task that streams *both* OSM and OHM misc-boundary records.
#
# Bucket name = authority namespace for ``osm`` and ``ohm`` per the contract
# at ``whg3/developer/plan-tileset-namespace-rename.contract.md`` (the Atlas
# UI's ``tileSourceFor()`` collapses to identity once this holds for every
# non-outlier authority). ``osm_misc`` keeps its category-cluster name
# because it's not a namespace — it spans both osm and ohm and is purely a
# tileset/UI label for misc boundary tags.
_FIXED_BUCKETS: dict[str, tuple[str, ...]] = {
    "osm":      ("osm",),
    "ohm":      ("ohm",),
    "osm_misc": ("osm", "ohm"),
}

# Per-namespace buckets — one ``<ns>.mbtiles`` per authority namespace, with
# every doc that carries renderable geometry (full polygon via geom_store, or
# point via ``repr_point``). ``osm`` and ``ohm`` are excluded (handled by the
# fixed buckets); ``whg`` is excluded (handled per-dataset below). ``po``,
# ``clio`` and ``nl`` keep their existing filenames — under the new code path
# the inclusion rule is "every doc in the namespace with geometry", which for
# these three namespaces is identical to the legacy "boundary present" rule
# (every doc has ``boundary`` set).
_PER_NAMESPACE_BUCKETS: tuple[str, ...] = (
    "alc", "chgis", "hgis", "clio", "dgsd", "dp", "gb", "gn", "iv", "nl",
    "ofs", "og", "pl", "po", "tgn", "tm", "ukhc", "un", "wd",
    # Vision of Britain / GB Historical GIS boundary levels (place#135)
    "vob_rd", "vob_rc", "vob_cty", "vob_lgd",
    # Kain & Oliver ancient parishes (place#135)
    "kain_par",
)

# Prefix used by per-WHG-dataset buckets. The full bucket name is
# ``f"{_WHG_BUCKET_PREFIX}{dataset_sub_id}"`` (e.g. ``whg-1234``). The set of
# WHG buckets is enumerated at submit time from the staged sidecar at
# ``STAGED_BASE_DIR/_aggregates/whg.datasets.json``.
_WHG_BUCKET_PREFIX = "whg-"

# Context-overlay buckets — synthetic tilesets derived from a single
# authority namespace, filtered to a specific feature subset, and
# generated WITHOUT clustering so every selected feature survives at
# every zoom. They render as background context in the Atlas and are
# deliberately NOT part of the gazetteer registry: there is no
# ``GazetteerRegistryEntry`` for them and they never appear in the
# Batch 8 ``per_gazetteer`` inventory (which is built per authority
# namespace, not per bucket), so the Batch 11 push to Django leaves
# them alone. Their bucket names use a non-standard ``<namespace>_<tag>``
# form to make their derived nature obvious in logs and on disk.
#
# Each entry: bucket → {namespace contributor, fcode whitelist, zoom
# range, clustering flag}. The fcode filter is applied via
# ``_doc_belongs_to_bucket`` against ``types[0].identifier``.
_CONTEXT_OVERLAY_BUCKETS: dict[str, dict[str, Any]] = {
    "gn_capitals": {
        "namespace": "gn",
        # Present-day capitals only: PPLC (capital of a political
        # entity) and PPLG (seat of government), plus PPLA (first-order
        # admin seat) since GeoNames records some country capitals
        # under that code depending on their administrative role.
        # PPLCH (historical capital) was tried earlier but pulled in
        # noisy historical artefacts like "(former) Roman catholic
        # diocese of London" that don't belong on a capitals overlay,
        # so it's excluded here.
        "fcodes": frozenset(["PPLC", "PPLG", "PPLA"]),
        "minzoom": 0,
        "maxzoom": 10,
        "cluster_points": False,
        "description": "WHG context overlay: GeoNames capitals & first-order admin seats",
    },
}

# Back-compat: ``submit_tiles_slurm`` and a handful of tests still import this
# symbol. It now reflects only the **fixed** buckets — per-namespace and
# per-WHG-dataset buckets are resolved at submit time via ``resolve_buckets``.
TILE_BUCKETS: dict[str, tuple[str, ...]] = dict(_FIXED_BUCKETS)


def _bucket_contributors(bucket: str) -> tuple[str, ...]:
    """Return the contributing namespace tuple for any kind of bucket."""
    if bucket in _FIXED_BUCKETS:
        return _FIXED_BUCKETS[bucket]
    if bucket in _PER_NAMESPACE_BUCKETS:
        return (bucket,)
    if bucket in _CONTEXT_OVERLAY_BUCKETS:
        return (_CONTEXT_OVERLAY_BUCKETS[bucket]["namespace"],)
    if bucket.startswith(_WHG_BUCKET_PREFIX):
        return ("whg",)
    return ()


def _whg_dataset_sub_id(bucket: str) -> str | None:
    """Extract ``dataset_sub_id`` from a ``whg-<id>`` bucket name."""
    if not bucket.startswith(_WHG_BUCKET_PREFIX):
        return None
    sub_id = bucket[len(_WHG_BUCKET_PREFIX):]
    return sub_id or None


def _load_whg_dataset_sub_ids() -> list[str]:
    """Read ``staged/_aggregates/whg.datasets.json`` and return sub-IDs."""
    sidecar = Path(STAGED_BASE_DIR) / "_aggregates" / "whg.datasets.json"
    if not sidecar.exists():
        return []
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    sub_ids: list[str] = []
    for entry in payload.get("datasets") or ():
        ds_id = entry.get("id") or ""
        # Sidecar IDs are namespaced ("whg:1234"); strip the namespace.
        if ":" in ds_id:
            _, sub = ds_id.split(":", 1)
        else:
            sub = ds_id
        if sub:
            sub_ids.append(sub)
    return sub_ids


def resolve_buckets(manifest: dict | None = None) -> list[str]:
    """Enumerate every tile bucket eligible for the current staged corpus.

    Order: fixed buckets first (preserves historical Slurm task ordering),
    then per-namespace buckets, then context-overlay buckets, then
    per-WHG-dataset buckets in the order they appear in the sidecar. The
    ``manifest`` argument is accepted for forward compatibility but
    currently unused — the submitter applies its own eligibility filters
    via ``_required_stage_for``.
    """
    buckets: list[str] = list(_FIXED_BUCKETS)
    buckets.extend(_PER_NAMESPACE_BUCKETS)
    buckets.extend(_CONTEXT_OVERLAY_BUCKETS)
    for sub_id in _load_whg_dataset_sub_ids():
        buckets.append(f"{_WHG_BUCKET_PREFIX}{sub_id}")
    return buckets


def _is_known_bucket(bucket: str) -> bool:
    return (
        bucket in _FIXED_BUCKETS
        or bucket in _PER_NAMESPACE_BUCKETS
        or bucket in _CONTEXT_OVERLAY_BUCKETS
        or (bucket.startswith(_WHG_BUCKET_PREFIX) and bool(_whg_dataset_sub_id(bucket)))
    )


def _is_admin_level(boundary_value: str) -> bool:
    return is_admin_boundary_value(boundary_value)


def _is_misc_boundary(boundary_value: str) -> bool:
    return is_misc_boundary_value(boundary_value)


def _extract_source_id(place_id: str) -> int | str:
    """Extract the source ID from a place_id like 'osm:r12345'."""
    _, raw = place_id.split(':', 1)
    if raw and raw[0] in 'nwr' and raw[1:].isdigit():
        return int(raw[1:])
    try:
        return int(raw)
    except ValueError:
        return raw


def _extract_toponyms_by_lang(toponyms: list[dict]) -> dict[str, str]:
    by_lang: dict[str, str] = {}
    for t in toponyms:
        tid = t.get('toponym_id', '')
        if '@' in tid:
            name, lang = tid.rsplit('@', 1)
            if lang and name:
                by_lang.setdefault(lang, name)
    return by_lang


def generate_tileset(
    geojsonl_path,
    mbtiles_path,
    layer_name,
    description='',
    *,
    minzoom: int = 2,
    maxzoom: int = 10,
    cluster_points: bool = False,
    preserve_all: bool = False,
):
    """Generate .mbtiles from GeoJSON Lines file using tippecanoe.

    ``preserve_all`` (place#140 boundary pass) guarantees **every** feature
    survives at every zoom in its ``tippecanoe:minzoom``..maxzoom range: it drops
    ``--coalesce-densest-as-needed`` (which sheds features to fit tile budgets)
    and adds ``--no-tiny-polygon-reduction --no-feature-limit --no-tile-size-limit``.
    Used for the pinned real-boundary pass so that no non-degenerate polygon is
    missing at/above the z8 crossover. Mutually exclusive with ``cluster_points``.

    ``minzoom`` defaults to 2 because at z0/z1 a single tile must hold
    the entire world's boundary geometry, which forces tippecanoe into
    a multi-hour sparsification loop on dense corpora. Use a smaller
    minzoom (0 or 1) ONLY for sparse subsets — the band-aware caller in
    ``generate_tiles_from_staged`` handles this safely by partitioning
    features per band before calling here.

    ``cluster_points`` is for buckets that may contain point features
    (per-namespace and per-WHG-dataset buckets). When set, tippecanoe
    clusters points within ten pixels at zooms <= 8, attaching a
    ``point_count`` attribute to each surviving cluster point so the
    Atlas heatmap layer can weight by density. Above z8 individual
    points are emitted unchanged. Polygon/line features are unaffected.
    Fixed admin buckets (``osm``, ``ohm``, ``osm_misc``) are polygon-
    only and pass ``cluster_points=False``.
    """
    tippecanoe = shutil.which('tippecanoe')
    if not tippecanoe:
        print("  WARNING: tippecanoe not found — skipping .mbtiles generation")
        return False

    if not geojsonl_path.exists() or geojsonl_path.stat().st_size == 0:
        print("  WARNING: GeoJSON Lines file is empty — skipping")
        return False

    size_mb = geojsonl_path.stat().st_size / 1e6
    print(f"  Generating {mbtiles_path.name} from {size_mb:.1f} MB (z{minzoom}-{maxzoom}) ...")

    cmd = [
        tippecanoe,
        '--output', str(mbtiles_path),
        '--force',
        '--layer', layer_name,
        '--name', f'WHG {layer_name}',
        '--description', description or f'WHG {layer_name} boundaries',
        '--minimum-zoom', str(minzoom),
        '--maximum-zoom', str(maxzoom),
        '--simplification', '10',
        '--detect-shared-borders',
        # NOTE: ``--extend-zooms-if-still-dropping`` was tried and removed.
        # On dense corpora tippecanoe kept extending past z10 into z12+ in
        # a futile attempt to fit every feature, blowing past 24 h Slurm
        # walls. Admin boundaries don't need sub-z10 detail — at z10 each
        # tile is ~40 km, well above country/state/district line
        # resolution — so the densest-coalesce behaviour alone produces
        # the right size/quality tradeoff.
        '--no-tile-compression',
        '--read-parallel',
    ]
    if preserve_all:
        # place#140: keep every feature at every zoom in its min..max range —
        # no coalesce-dropping, no tiny-polygon reduction, no size/feature caps.
        cmd += [
            '--no-tiny-polygon-reduction',
            '--no-feature-limit',
            '--no-tile-size-limit',
            # None of the three flags above touches tippecanoe's POINT drop
            # rate (``-r``, default 2.5 per zoom below the base zoom), which
            # applies to Point features only — so a no-drop pass of polygons
            # was complete while the labels pass (one Point anchor per shape)
            # was silently thinned: measured 10 Oct 2026 on hgis, 892 anchors
            # → 399 distinct at z8, 701 at z9, all 892 only at z10, identical
            # in the 2 Sep build. ``--drop-rate 1`` is what makes preserve_all
            # true for anchors (place#166).
            '--drop-rate', '1',
        ]
    else:
        # --coalesce-densest-as-needed MERGES features that can be combined.
        # OHM's polygons each carry distinct name / place_id / date attributes,
        # so there is almost nothing to merge and the strategy runs out of room:
        # its low-zoom tiles wanted 2.4-3.5 MB against a 500 KB ceiling. What
        # happened then was whole-tile loss, not thinning (place#160).
        #
        # --drop-densest-as-needed sheds FEATURES until the tile fits, so an
        # over-large tile degrades to "fewer polities visible when zoomed out"
        # instead of "blank continent". It only engages on tiles that are over
        # the limit — precisely the tiles whose current outcome is worse — so
        # it is a no-op everywhere else.
        cmd += ['--coalesce-densest-as-needed', '--drop-densest-as-needed']
    if cluster_points:
        # Cluster point features at zooms <= 8 within 10 px. Tippecanoe
        # auto-attaches a ``point_count`` attribute to each surviving
        # cluster point — the Atlas heatmap layer reads it as the weight.
        # ``--cluster-densest-as-needed`` lets tippecanoe widen the
        # cluster radius if a tile is still too large after the initial
        # pass. Polygon/line features are unaffected by these flags.
        cmd += [
            '--cluster-distance', '10',
            '--cluster-maxzoom', '8',
            '--cluster-densest-as-needed',
        ]
        # The default drop rate (2.5 per zoom below z10) removes points
        # UNCOUNTED: a rate-dropped point is not folded into a cluster's
        # ``point_count``, so the low-zoom heat weight is whatever survived the
        # lottery. Measured 10 Oct 2026 (place#166): hgis z0 carried 2 of
        # 13,213 points and no cluster; with ``--drop-rate 1`` the same pass
        # carried 24 clusters standing for all 13,213 (max tile 42,628 bytes,
        # no measurable extra wall time at 13k points; wd at 11.4M points is
        # unmeasured). Opt-in via WHG_POINTS_DROP_RATE until that decision is
        # taken — it changes every point bucket's tiles, gn and tgn included.
        if _POINTS_DROP_RATE:
            cmd += ['--drop-rate', _POINTS_DROP_RATE]
        # Carry the per-feature temporal range onto the surviving cluster point
        # (place#131) so low-zoom clusters date-filter too, not just individual
        # features. ``start:min``/``end:max`` widen to the union of members'
        # spans (a cluster shows if *any* member overlaps the window).
        # The definite core widens the same way (place#176): a cluster passes a
        # *definitely* window if ANY member could, which is the same
        # err-toward-visible reading as the envelope. A cluster point stands for
        # many places; refusing to draw it because its members disagree would
        # hide all of them.
        cmd += [
            '--accumulate-attribute', 'start:min',
            '--accumulate-attribute', 'end:max',
            '--accumulate-attribute', 'start_def:min',
            '--accumulate-attribute', 'end_def:max',
        ]
        # NOTE: AAT is deliberately NOT accumulated onto cluster points.
        # ``aat:concat`` builds an *unbounded* string (every member's paths
        # concatenated) BEFORE any dedupe, and a dense bucket (kain_par: 23k
        # parishes) overflows tippecanoe's per-feature attribute budget →
        # tiling fails. The jq ``--postfilter`` dedupes only AFTER the tile is
        # assembled, too late to prevent the blow-up. Type-filtering therefore
        # applies to *individual* features (higher zoom, where it's meaningful);
        # low-zoom cluster points are not type-filtered — acceptable, since a
        # dense cluster spans many types anyway. ``tilegen_aat_postfilter.sh``
        # is kept for a future bounded-accumulation approach.
    cmd.append(str(geojsonl_path))

    start = time.time()
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr)
    elapsed = time.time() - start

    if result.returncode == 0 and mbtiles_path.exists():
        out_mb = mbtiles_path.stat().st_size / 1e6
        print(f"  ✓ {mbtiles_path.name}: {out_mb:.1f} MB ({elapsed:.0f}s)")
        return True
    print(f"  ✗ tippecanoe failed (exit code {result.returncode})")
    return False


def _running_on_proxy(proxy_host: str) -> bool:
    """True when this process is already running on ``proxy_host`` —
    in which case the SSH hop to itself would fail (the proxy host
    isn't a self-alias) AND is wasteful. Detection is conservative:
    we resolve both ``socket.gethostname()`` / ``getfqdn()`` and the
    given ``proxy_host`` and compare. Falls through to ``False`` on
    any resolution error (caller defaults to via_proxy=True)."""
    import socket
    try:
        local_names = {
            socket.gethostname().lower(),
            socket.getfqdn().lower(),
            socket.gethostbyname(socket.gethostname()),
        }
    except (socket.error, OSError):
        return False
    try:
        proxy_resolved = socket.gethostbyname(proxy_host)
    except (socket.gaierror, socket.error, OSError):
        # The proxy isn't DNS-resolvable here — almost always because it's
        # an SSH config alias (defined in ~/.ssh/config on hosts that ssh
        # INTO the proxy, but not as a real hostname). That means we are
        # NOT on the proxy; we just don't have the alias for SSH-internal
        # use. Let the caller's via_proxy default (True) stand so SSH
        # picks up the alias naturally.
        return False
    if proxy_resolved in local_names:
        return True
    if any(name == proxy_host.lower() for name in local_names):
        return True
    return False


# Buckets where an inline ``hull`` is the INTENDED geometry rather than a
# degraded fallback: WHG contributed datasets carry deliberate approximation
# polygons (e.g. ottgaz admin hulls) with ``has_geom=False``, kept out of the
# authoritative geom store on purpose. Everywhere else, a hull render means a
# store miss that degraded to a polygon instead of to a point.
_HULL_EXPECTED_PREFIXES: tuple[str, ...] = (_WHG_BUCKET_PREFIX,)


def publish_gate(
    bucket: str,
    tier_counts: dict[str, int],
    mbtiles: Path,
) -> tuple[bool, list[str]]:
    """Decide whether a freshly-built tileset may be pushed to the tileserver.

    Returns ``(ok, reasons)``; ``reasons`` is empty when ok.

    This exists because **a tile job that reports success is not evidence it
    read any geometry**. On 7 August 2026 nine boundary layers were built
    against a destroyed geom store, streamed ``poly=0`` for every bucket,
    exited 0, and deployed as points. Every check in place at the time passed,
    because each of them — job exit status, feature counts, a non-empty
    tileset — is satisfied just as well by the broken world as the working one.

    The three refusals below are chosen so that each has a **known-correct
    answer** and can therefore actually fail:

    1. **Total geom-store miss.** Docs that name a store entry, none of which
       resolve. This is 7 August exactly. A genuinely point-only namespace
       makes no attempt at all, so it cannot trip this.
    2. **Unexpected hull rendering.** ``_build_staged_feature`` falls back to
       the inline ``hull`` before it falls back to a point, so a store miss on
       a hull-bearing doc yields a *polygon* — the convex hull of a multi-part
       place, which for e.g. the Spanish Empire spans 232° of longitude and
       renders as a band smeared across the Pacific. Deliberate approximation
       polygons are expected only on ``whg-*`` buckets.
    3. **An empty tileset.** Guards the steps downstream of feature building,
       where ``tile-join`` is known to drop over-large tiles and still exit 0.

    Deliberately NOT a longitude-span heuristic. Measured 2 Sep 2026: a naive
    ``max(lon) - min(lon) > 180`` flags six *legitimate* ``un`` countries —
    ``ata``, ``rus``, ``fji``, ``kir``, ``nzl``, ``usa`` — which are
    circumpolar or genuinely cross the antimeridian, and it cannot be rescued
    by wrapping, since normalising longitudes tightens the Spanish Empire hull
    to ~140° and lets the real smear through. The tier is unambiguous where
    the resulting geometry is not.
    """
    reasons: list[str] = []
    attempts = tier_counts.get("store_attempt", 0)
    hits = tier_counts.get("store_hit", 0)
    hulls = tier_counts.get("hull", 0)

    if attempts and not hits:
        reasons.append(
            f"geom store returned nothing for all {attempts:,} lookups "
            f"(the 7 Aug failure: every feature would ship as a point)"
        )
    if hulls and not bucket.startswith(_HULL_EXPECTED_PREFIXES):
        reasons.append(
            f"{hulls:,} feature(s) rendered from the inline hull fallback, "
            f"which is not expected for '{bucket}' — a store miss has "
            f"degraded to convex hulls, not to points"
        )

    try:
        with sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True) as con:
            tiles = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        if not tiles:
            reasons.append("built tileset contains 0 tiles")
    except Exception as exc:                                   # noqa: BLE001
        reasons.append(f"built tileset unreadable ({exc})")

    return (not reasons), reasons


def _log_gate(bucket: str, tier_counts: dict[str, int], ok: bool,
              reasons: list[str]) -> None:
    """Print the tier tally for every bucket, passing or failing.

    Printed unconditionally: the tally is the evidence that the build read
    real geometry, and it is worth as much in the log of a run that passed as
    in one that did not.
    """
    print(
        f"  gate {bucket}: store {tier_counts.get('store_hit', 0):,}"
        f"/{tier_counts.get('store_attempt', 0):,}"
        f"  hull {tier_counts.get('hull', 0):,}"
        f"  point {tier_counts.get('point', 0):,}"
        f"  → {'PASS' if ok else 'REFUSED'}",
        flush=True,
    )
    for r in reasons:
        print(f"    ✗ {r}", flush=True)


def push_mbtiles_to_tileserver(
    mbtiles_path: Path,
    *,
    via_proxy: bool = True,
    proxy_host: str | None = None,
    remote_user: str | None = None,
    remote_host: str | None = None,
    remote_dir: str | None = None,
    proxy_rsync: str | None = None,
    timeout: int = 7200,
) -> bool:
    """Push a single ``.mbtiles`` file to the TileServer GL host.

    The push is routed through ``proxy_host`` (default: the Pitt VM —
    ``settings.TILESERVER_PROXY``) because CRC compute nodes don't carry an
    SSH key for the tileserver, but the proxy does. The proxy reads the
    source file from /ix1 (its NFS mount) and rsyncs to the tileserver.

    When invoked **on** the proxy host itself (e.g. running
    ``--redeploy-only`` directly from pitt), ``ssh pitt …`` would fail
    with "Could not resolve hostname pitt" — proxy hosts aren't usually
    self-aliases. We auto-detect this case via ``_running_on_proxy`` and
    fall back to a direct local scp/rsync, bypassing the SSH hop.

    Set ``via_proxy=False`` explicitly to force direct mode.

    Prefers rsync with ``--inplace`` — the incoming bytes are written
    **into the existing destination file** rather than to a sidecar that is
    renamed on completion. Falls back to ``scp -p`` if rsync isn't reachable
    (settings.TILESERVER_PROXY_RSYNC empty), which is already effectively
    in-place. On
    the Pitt VM rsync lives in the gazetteer/whg conda env and isn't on
    stg135's PATH, hence the absolute-path setting.

    Returns True on success, False on failure (does not raise — caller
    decides how to react).
    """
    from processing.settings import (
        TILESERVER_PROXY, TILESERVER_HOST, TILESERVER_USER,
        TILESERVER_TILES_DIR, TILESERVER_PROXY_RSYNC, TILESERVER_SSH_KEY,
    )

    proxy_host = proxy_host or TILESERVER_PROXY
    remote_user = remote_user or TILESERVER_USER
    remote_host = remote_host or TILESERVER_HOST
    remote_dir = remote_dir or TILESERVER_TILES_DIR
    if proxy_rsync is None:
        proxy_rsync = TILESERVER_PROXY_RSYNC

    # Direct-mode short-circuit: when ``TILESERVER_SSH_KEY`` is configured
    # the runtime has its own authentication to the tileserver, so the
    # proxy hop is unnecessary (and on CRC compute nodes actively
    # broken — they can't resolve the ``pitt`` alias). The auto-detect
    # for "running ON the proxy" still fires below for the local-box and
    # Pitt-VM cases that don't set the key.
    if TILESERVER_SSH_KEY:
        via_proxy = False

    # Auto-detect: if the caller asked for via_proxy but we ARE the proxy,
    # silently switch to direct mode rather than failing on self-SSH.
    if via_proxy and _running_on_proxy(proxy_host):
        via_proxy = False

    if not mbtiles_path.exists():
        print(f"  ✗ source missing: {mbtiles_path}")
        return False

    target = f"{remote_user}@{remote_host}:{remote_dir}/"
    size_mb = mbtiles_path.stat().st_size / 1e6
    use_rsync = bool(proxy_rsync) if via_proxy else bool(shutil.which("rsync"))
    tool = "rsync" if use_rsync else "scp"
    print(f"  → {tool} {mbtiles_path.name} ({size_mb:.1f} MB) → {target}", flush=True)

    # Build the ``-e`` argument once for direct mode so rsync/scp use the
    # configured key. ``StrictHostKeyChecking=accept-new`` lets the first
    # connection from a fresh compute node trust the host without prompt
    # while still failing if the host key changes later.
    direct_ssh_e: str | None = None
    if not via_proxy and TILESERVER_SSH_KEY:
        direct_ssh_e = (
            f"ssh -i {TILESERVER_SSH_KEY} -o BatchMode=yes "
            "-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30"
        )

    if via_proxy:
        if use_rsync:
            # Quote the rsync invocation since it's executed by the
            # remote shell.
            #
            # ``--inplace`` (was ``--partial --partial-dir=.tmp``, changed
            # 2 Sep 2026): write into the existing destination file instead
            # of staging a copy that is renamed on completion. The old mode
            # meant BOTH copies existed on disk for the duration of every
            # push — and the tileserver volume is at 86% (41 G of 48 G, 30 G
            # of that mbtiles), with the retile pushing 75 buckets from a
            # PARALLEL Slurm array, so those transients compound. A previous
            # push already hit 99%.
            #
            # ⚠️ The trade is real and deliberate: with ``--partial-dir`` an
            # interrupted transfer left the served file intact; with
            # ``--inplace`` it leaves a TORN mbtiles, and since
            # tileserver-gl opens on demand that tileset serves errors until
            # re-pushed. Recoverable — the file is still there to push over —
            # which delete-then-write would not be. ⚠️ ``--inplace`` and
            # ``--partial-dir`` are mutually exclusive: rsync exits with
            # "--inplace cannot be used with --partial-dir", so this is a
            # swap, never an addition.
            #
            # ``--info=stats1``: one tidy summary line at the end (no
            # progress noise that would flood Slurm logs).
            remote_cmd = (
                f"{proxy_rsync} -a --inplace "
                f"--info=stats1 {mbtiles_path} {target}"
            )
        else:
            remote_cmd = f"scp -p -B {mbtiles_path} {target}"
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
            proxy_host, remote_cmd,
        ]
    else:
        if use_rsync:
            # ``--inplace`` for the same reason as the proxy path above —
            # keep the two transfer modes identical, or a direct push and a
            # proxied push have different disk and failure characteristics.
            cmd = [
                "rsync", "-a", "--inplace",
                "--info=stats1",
            ]
            if direct_ssh_e:
                cmd += ["-e", direct_ssh_e]
            cmd += [str(mbtiles_path), target]
        else:
            cmd = ["scp", "-p", "-B"]
            if TILESERVER_SSH_KEY:
                cmd += ["-i", TILESERVER_SSH_KEY,
                        "-o", "StrictHostKeyChecking=accept-new"]
            cmd += [str(mbtiles_path), target]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"  ✗ push timed out after {timeout}s: {mbtiles_path.name}")
        return False

    if result.returncode == 0:
        # rsync's stats1 output goes to stdout; one short summary line.
        if use_rsync and result.stdout.strip():
            tail = result.stdout.strip().splitlines()[-1][:120]
            print(f"    ✓ {mbtiles_path.name} pushed  ({tail})", flush=True)
        else:
            print(f"    ✓ {mbtiles_path.name} pushed", flush=True)
        return True
    print(
        f"    ✗ push failed (rc={result.returncode}): {mbtiles_path.name}\n"
        f"      stderr: {result.stderr.strip()[:500]}",
        flush=True,
    )
    return False


def deploy_tilesets(mbtiles_paths, **kwargs) -> dict[str, bool]:
    """Push a list of .mbtiles files to the tileserver. Bulk wrapper.

    Returns ``{name: success_bool}`` so the caller can react per-file.
    Used by the post-array catch-up path; per-bucket pushes during a tile
    gen run go through ``push_mbtiles_to_tileserver`` directly.
    """
    # Say which route is actually taken. The banner claimed "via Pitt-VM
    # proxy" unconditionally while push_mbtiles_to_tileserver short-circuits
    # to DIRECT whenever TILESERVER_SSH_KEY is set — which it is on CRC. That
    # mislabelling is why a manual deploy was hand-rolled through pitt, a host
    # that has no rsync, instead of using this path from a compute node where
    # both ends do (measured delta speedup: 124x on an unchanged tileset).
    from processing.settings import TILESERVER_SSH_KEY as _K
    _route = "direct (rsync, delta transfer)" if _K else "via Pitt-VM proxy"
    print(f"\nDeploying {len(mbtiles_paths)} tilesets {_route} …")
    results: dict[str, bool] = {}
    for path in mbtiles_paths:
        results[path.name] = push_mbtiles_to_tileserver(path, **kwargs)
    ok = sum(1 for v in results.values() if v)
    print(f"  Deploy summary: {ok}/{len(mbtiles_paths)} succeeded")
    return results


def restart_tileserver(
    *,
    via_proxy: bool = True,
    proxy_host: str | None = None,
    remote_user: str | None = None,
    remote_host: str | None = None,
    services: list[str] | None = None,
    timeout: int = 120,
) -> bool:
    """Restart the TileServer GL services so newly-deployed mbtiles are picked up.

    Per the user's contract (2026-05-03), this is **only ever called once
    per rebuild**, after every gazetteer's mbtiles has been pushed AND the
    push was verified. Per-bucket tile-gen tasks must NOT call this.

    Delegates to ``/srv/restart_services.sh`` on the tileserver — the
    canonical restart script kept in the **whg-tileboss** repo, which
    knows the right ``service stop`` + ``pkill -f`` + ``service start``
    sequence for both the ``tileserver-gl-light`` and ``tiler`` units
    (the forever-service init scripts don't cooperate with bare
    ``systemctl restart``; see ``feedback_tileserver_restart.md`` memory
    for the history of that gotcha). Routing the work through the
    on-host script means restart tweaks happen in tileboss alongside the
    services they restart, not split across two repos.

    Authentication paths, in order:
    * ``TILESERVER_SSH_KEY`` set → direct SSH from the local host using
      that key (the CRC compute / Slurm auto-deploy path; no proxy hop).
    * ``via_proxy=True`` and the local host is the proxy → direct SSH
      via the proxy's ambient agent/key.
    * Otherwise → SSH through the proxy (``ssh pitt 'ssh whgadmin@…'``)
      so the operator's local box can drive the restart by alias.

    After the script returns, verifies each declared unit has at least
    one running process via ``pgrep -cf``. Returns True iff every unit
    reports ``> 0`` processes.
    """
    from processing.settings import (
        TILESERVER_PROXY, TILESERVER_HOST, TILESERVER_USER,
        TILESERVER_SERVICES, TILESERVER_SSH_KEY,
    )

    proxy_host = proxy_host or TILESERVER_PROXY
    remote_user = remote_user or TILESERVER_USER
    remote_host = remote_host or TILESERVER_HOST
    services = services or list(TILESERVER_SERVICES)

    # Direct-mode short-circuit (mirrors push_mbtiles_to_tileserver): when
    # the runtime has its own key for the tileserver, the proxy hop is
    # both unnecessary and broken (CRC compute can't resolve ``pitt``).
    if TILESERVER_SSH_KEY:
        via_proxy = False
    elif via_proxy and _running_on_proxy(proxy_host):
        via_proxy = False

    print(
        f"\nRestarting tileserver services: {services} on "
        f"{remote_user}@{remote_host} via /srv/restart_services.sh"
    )

    # Process patterns for verification (forever-service wraps these
    # binaries; ``pgrep -cf`` matches the monitor + the wrapped child).
    pkill_patterns = {
        "tileserver-gl-light.service": "tileserver-gl-light",
        "tiler.service":               "/srv/tiler/tiler.js",
    }

    # CRITICAL: ``restart_services.sh`` runs ``sudo pkill -f
    # tileserver-gl-light`` (and similar for tiler). If our SSH session's
    # argv contains those literal strings — e.g. via an appended
    # ``echo … pgrep -cf tileserver-gl-light`` — ``pkill -f`` kills our
    # own remote shell mid-flight and SSH returns rc=255 with empty
    # output. So the restart and the verification MUST run in separate
    # SSH sessions: session 1's argv only mentions the script, session 2
    # only mentions the verify pattern, and neither carries the
    # cross-pattern that pkill is hunting.
    restart_cmd = "cd /srv && bash restart_services.sh"
    verify_cmd = "; ".join(
        f"echo {svc!r}: $(pgrep -cf {pkill_patterns.get(svc, svc.replace('.service', ''))})"
        for svc in services
    )

    def _build_cmd(remote: str) -> list[str]:
        if TILESERVER_SSH_KEY:
            return [
                "ssh", "-i", TILESERVER_SSH_KEY,
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ServerAliveInterval=30",
                f"{remote_user}@{remote_host}", remote,
            ]
        if via_proxy:
            return [
                "ssh", "-o", "BatchMode=yes", proxy_host,
                f'ssh -o BatchMode=yes {remote_user}@{remote_host} "{remote}"',
            ]
        return ["ssh", "-o", "BatchMode=yes",
                f"{remote_user}@{remote_host}", remote]

    try:
        result = subprocess.run(
            _build_cmd(restart_cmd),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"  ✗ restart timed out after {timeout}s")
        return False

    if result.returncode != 0:
        print(
            f"  ✗ restart_services.sh failed (rc={result.returncode})\n"
            f"    stderr: {result.stderr.strip()[:500]}"
        )
        return False

    print("  ✓ /srv/restart_services.sh returned 0")

    # Give forever-service a moment to (re)spawn the wrapped node
    # children before we count processes.
    time.sleep(3)

    try:
        verify = subprocess.run(
            _build_cmd(verify_cmd),
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("  ⚠ verification SSH timed out — restart probably succeeded "
              "but the process counts could not be confirmed")
        return False

    if verify.returncode != 0:
        print(f"  ⚠ verification SSH rc={verify.returncode}; "
              f"stderr: {verify.stderr.strip()[:200]}")
        return False

    # Per-service process counts came back on stdout in the form
    # ``'tileserver-gl-light.service': 2``. Anything > 0 means the
    # service is back up; 0 is a hard failure to flag.
    print("  Verification:")
    healthy = True
    for line in verify.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        print(f"    {line}")
        if line.endswith(": 0"):
            healthy = False
    if not healthy:
        print(
            "    ⚠ at least one service has zero processes — restart probably failed"
        )
    return healthy


def _staged_namespace_source(namespace: str) -> Path | None:
    """Return the most-enriched staged snapshot file for ``namespace``.

    Walks the ``_STAGED_SOURCE_PRIORITY`` chain and returns the first existing
    Parquet (preferred) or JSONL file. ``None`` if the namespace has not yet
    been staged at any stage.
    """
    base = Path(STAGED_BASE_DIR) / namespace
    for stage in _STAGED_SOURCE_PRIORITY:
        parquet = base / stage / "places.parquet"
        if parquet.exists():
            return parquet
        jsonl = base / stage / "places.jsonl"
        if jsonl.exists():
            return jsonl
    return None


def _iter_es_docs(namespace: str) -> Iterator[dict[str, Any]]:
    """Stream a namespace's place documents from Elasticsearch.

    The tile builder only ever needed the *document* from staging — geometry
    already comes from the geom store via ``reader.get(geom_ref)`` — and the
    places index carries every field ``_build_staged_feature`` reads. So a
    namespace can be tiled straight from the index when its staged snapshot is
    gone, instead of re-running an extract to rebuild a copy of what the index
    already holds.

    Read-only, so there is nothing to isolate: no snapshot, no scratch cluster.
    Uses a point-in-time with ``search_after`` rather than a scroll, so the view
    stays consistent for the whole pass without pinning shard resources the way
    a long-lived scroll context does; sorting on ``_shard_doc`` is the cheapest
    total order ES offers and requires no field to be sortable.
    """
    from elasticsearch import Elasticsearch

    from processing.settings import ES_HOST, ES_PASSWORD_FILE, PLACES_INDEX

    kwargs: dict[str, Any] = {"request_timeout": 300, "max_retries": 5,
                              "retry_on_timeout": True}
    try:
        with open(ES_PASSWORD_FILE, encoding="utf-8") as fh:
            kwargs["basic_auth"] = ("elastic", fh.read().strip())
    except OSError:
        pass  # unauthenticated cluster, or creds supplied via ES_HOST URL
    es = Elasticsearch(ES_HOST, **kwargs)

    pit_id = es.open_point_in_time(index=PLACES_INDEX, keep_alive="10m")["id"]
    query = {"term": {"namespace": namespace}}
    search_after = None
    yielded = 0
    try:
        while True:
            body: dict[str, Any] = {
                "size": 2000,
                "query": query,
                "pit": {"id": pit_id, "keep_alive": "10m"},
                "sort": [{"_shard_doc": "asc"}],
                "track_total_hits": False,
            }
            if search_after is not None:
                body["search_after"] = search_after
            resp = es.search(body=body)
            hits = resp["hits"]["hits"]
            if not hits:
                break
            # The PIT id can be refreshed between pages; honour the new one.
            pit_id = resp.get("pit_id", pit_id)
            for hit in hits:
                src = hit.get("_source")
                if isinstance(src, dict):
                    yielded += 1
                    yield src
            search_after = hits[-1]["sort"]
            if yielded % 500_000 == 0:
                print(f"  ... read {yielded:,} {namespace} docs from ES",
                      flush=True)
    finally:
        try:
            es.close_point_in_time(body={"id": pit_id})
        except Exception:
            pass  # best-effort; the keep_alive expires on its own
    print(f"  read {yielded:,} {namespace} docs from Elasticsearch", flush=True)


def _iter_namespace_docs(namespace: str) -> Iterator[dict[str, Any]]:
    """Yield a namespace's tile documents from ES or from its staged snapshot.

    ES is used only for namespaces named in ``TILE_ES_DOC_NAMESPACES`` — an
    explicit opt-in, never an automatic fallback (see that setting for why).
    Otherwise the staged snapshot is used, and a namespace with no snapshot
    yields nothing, exactly as before.
    """
    from processing.settings import TILE_ES_DOC_NAMESPACES

    if namespace in TILE_ES_DOC_NAMESPACES:
        print(f"  {namespace}: reading tile documents from Elasticsearch "
              f"(TILE_ES_DOC_NAMESPACES)", flush=True)
        yield from _iter_es_docs(namespace)
        return

    src = _staged_namespace_source(namespace)
    if src is None:
        return
    yield from _iter_staged_docs(src)


def _iter_staged_docs(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=2000):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row
        return

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _has_renderable_geometry(geom_entry: Any) -> bool:
    """True when an entry in ``doc['geometries']`` can produce a feature.

    Either a polygon retrievable from the geom store (``geom_ref`` /
    ``has_geom``) or a point inline on the doc (``repr_point``) qualifies.
    """
    if not isinstance(geom_entry, dict):
        return False
    geom_ref = geom_entry.get("geom_ref")
    if isinstance(geom_ref, str) and geom_ref:
        return True
    if geom_entry.get("has_geom"):
        return True
    rp = geom_entry.get("repr_point")
    if isinstance(rp, dict) and "lon" in rp and "lat" in rp:
        return True
    return False


def _temporal_props(doc: dict[str, Any], namespace: str) -> dict[str, int]:
    """Per-feature date-filter props for the Atlas map (place#131, place#176).

    Emits up to four numbers, mirroring the gateway's **two query modes** so the
    map filter and the search filter can agree (place#169):

    ``start`` / ``end``
        The *possible envelope* — the outer bounds, with a sentinel on any side
        nothing bounds, so both are always present on a feature that has any
        temporal information at all. Drives *possibly* mode.
    ``start_def`` / ``end_def``
        The *attested core* — the inner bounds. Present only when a timespan
        pins both, which is exactly when a *definitely* test can be satisfied.
        Drives *definitely* mode; a feature lacking them can never be definitely
        alive and the client filters it out by testing ``has``.

    Reads :func:`doc_temporal_bounds`, **not** ``doc_temporal_range``. The
    latter pools every year found under an endpoint, so an attestation
    (``{"start": {"latest": 2026}}`` — began no later than 2026, unbounded
    before) collapsed to a lower bound of 2026 and every contemporary feature
    stamped ``(2026, 2026)``. Switching the map filter on against those stamps
    blanks ``osm`` / ``osm_misc`` / ``tgn`` / ``nl`` on any historical range —
    the precise defect place#164 exists to remove, arriving by a different road.
    Such a feature now gets ``start = -9999, end = 9999`` (unbounded: never
    hidden in *possibly*) plus ``start_def = end_def = 2026`` (correctly refused
    by any *definitely* window that doesn't contain 2026).

    Undated features — no timespans at all — omit every prop, and the client
    admits them only in *possibly* mode, matching the gateway's ``undated``
    branch. See ``TILE_OPEN_END_YEAR`` / ``TILE_OPEN_START_YEAR``.
    """
    start_earliest, start_latest, end_earliest, end_latest = doc_temporal_bounds(
        doc, namespace)
    if (start_earliest is None and start_latest is None
            and end_earliest is None and end_latest is None):
        return {}                       # genuinely undated
    props: dict[str, int] = {
        # Envelope: an absent outer bound is UNBOUNDED, never the inner one.
        "start": start_earliest if start_earliest is not None else TILE_OPEN_START_YEAR,
        "end": end_latest if end_latest is not None else TILE_OPEN_END_YEAR,
    }
    # Core: only where a timespan pins both sides. No sentinels — absence is
    # the signal that no *definitely* window can be satisfied.
    if start_latest is not None and end_earliest is not None:
        props["start_def"] = start_latest
        props["end_def"] = end_earliest
    return props


def _aat_prop(doc: dict[str, Any]) -> str | None:
    """A ``;``-bracketed union of every AAT path *segment* id on the doc's types
    (place#131 §2), e.g. ``";300008347;300387179;300000810;"``.

    Every id in every ``types[].aat_paths`` materialised path (root→leaf) is
    included, so selecting a parent AAT concept on the client matches all its
    descendants — the ancestor id is present in every descendant's path. The
    ``;``-bracketing lets the client filter be a pure substring test
    (``['in', ';<id>;', ['get','aat']]``) with no false-boundary hits, and keeps
    ``--accumulate-attribute=aat:concat`` correct on clustered points. Returns
    ``None`` when the doc carries no AAT-mapped type.
    """
    segments: set[str] = set()
    for t in doc.get("types") or []:
        if not isinstance(t, dict):
            continue
        for path in t.get("aat_paths") or []:
            if not isinstance(path, str):
                continue
            # aat_paths are materialised ancestor paths. The AAT hierarchy stores
            # them dot-delimited (e.g. "300264550.300000201.300000705"); tolerate
            # a slash-delimited variant too. Every segment is a numeric AAT id.
            for seg in path.replace("/", ".").split("."):
                seg = seg.strip()
                if seg:
                    segments.add(seg)
    if not segments:
        return None
    return ";" + ";".join(sorted(segments)) + ";"


def _build_staged_feature(
    doc: dict[str, Any],
    namespace: str,
    reader: GeomStoreReader,
    *,
    misc: bool = False,
    require_boundary: bool = True,
    tier_counts: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Build a tippecanoe-ready Feature from a staged place doc.

    ``tier_counts``, when given, is incremented to record WHICH geometry
    source produced each feature. The caller gates the tileserver push on
    these counts, because the tier is the thing that actually distinguishes
    a correct build from the two failures this pipeline has shipped:

    * ``store_attempt`` / ``store_hit`` — docs that named a geom-store entry,
      and those the store actually returned. ``attempt > 0, hit == 0`` is the
      7 August failure exactly: every feature degraded to a point because the
      store was unreadable, while the job reported success.
    * ``hull`` — features rendered from the inline ``hull`` approximation.
      Expected only where WHG computes one deliberately; anywhere else it
      means a store miss silently degraded to a convex hull, which for a
      multi-part place is a polygon smeared across the globe.
    * ``point`` — features that fell back to ``repr_point``.

    Counting the tier is strictly better than inspecting the finished
    geometry: a longitude-span heuristic cannot tell Russia (legitimately
    antimeridian-crossing) from a hull smear, but the tier is unambiguous.

    With ``require_boundary=True`` (fixed buckets) the doc must carry a
    ``boundary`` value and a full polygon retrievable from the geom store —
    matches the original boundary-only behaviour.

    With ``require_boundary=False`` (per-namespace and per-WHG-dataset
    buckets) the doc still needs *some* renderable geometry: the function
    first tries the geom store for a polygon and falls back to a Point
    geometry synthesised from ``repr_point`` when the store has nothing.
    Returns ``None`` when no geometry of either kind is available.

    ``misc=True`` re-encodes the feature ID under the OSM/OHM 1-bit
    discrimination scheme used for the mixed ``osm_misc`` tileset.
    """
    place_id = doc.get("place_id")
    if not place_id:
        return None
    boundary = doc.get("boundary")
    if require_boundary and not boundary:
        return None

    geometries = doc.get("geometries") or []
    if not geometries:
        return None
    geom_entry = geometries[0]
    if not isinstance(geom_entry, dict):
        return None

    def _bump(key: str) -> None:
        if tier_counts is not None:
            tier_counts[key] = tier_counts.get(key, 0) + 1

    full_geom: dict[str, Any] | None = None
    geom_ref = geom_entry.get("geom_ref")
    if isinstance(geom_ref, str) and geom_ref:
        _bump("store_attempt")
        full_geom = reader.get(geom_ref)
        if full_geom:
            _bump("store_hit")
    elif geom_entry.get("has_geom"):
        # Authority scripts emit JSONL via ``write_staged_place_doc`` which
        # bypasses ``_augment_doc_for_stage`` — so ``geom_ref`` is absent on
        # extracted docs even when the geom store has the entry. Synthesize
        # the canonical key (``"{place_id}_{idx}"``) when ``has_geom`` is set,
        # matching ``stage_writers._augment_doc_for_stage``.
        idx = geom_entry.get("geometry_index", 0)
        _bump("store_attempt")
        full_geom = reader.get(f"{place_id}_{idx}")
        if full_geom:
            _bump("store_hit")

    if not full_geom and not require_boundary:
        # WHG-computed approximation polygons (e.g. ottgaz admin hulls) live in
        # the inline ``hull`` field with ``has_geom=False`` — they are kept OUT
        # of the authoritative geom store. Render them as polygons; their
        # ``source``/``approximation`` provenance flags mark them as approximate.
        hull = geom_entry.get("hull")
        if isinstance(hull, dict) and hull.get("type") in ("Polygon", "MultiPolygon"):
            full_geom = hull
            _bump("hull")

    if not full_geom and not require_boundary:
        rp = geom_entry.get("repr_point")
        if isinstance(rp, dict) and "lon" in rp and "lat" in rp:
            full_geom = {
                "type": "Point",
                "coordinates": [rp["lon"], rp["lat"]],
            }
            _bump("point")

    if not full_geom:
        return None

    props: dict[str, Any] = {
        "place_id": place_id,
        "namespace": namespace,
    }
    if boundary:
        props["boundary"] = boundary

    if boundary and _is_admin_level(boundary):
        minzoom = ADMIN_LEVEL_MINZOOM.get(boundary, 0)
    elif boundary:
        minzoom = 3
    else:
        minzoom = 0
    if minzoom > 0:
        props["tippecanoe:minzoom"] = minzoom

    toponyms = doc.get("toponyms") or []
    names_by_lang = _extract_toponyms_by_lang(toponyms)
    props["name"] = doc.get("title") or ""
    for lang in DISPLAY_LANGUAGES:
        if lang in names_by_lang:
            props[f"name_{lang}"] = names_by_lang[lang]

    # Per-feature temporal range (place#131 §1) and AAT type set (§2) so the
    # Atlas map can be date- and type-filtered client-side, in sync with the
    # gateway-filtered Explore list. Undated features carry no start/end;
    # untyped features carry no aat.
    props.update(_temporal_props(doc, namespace))
    aat = _aat_prop(doc)
    if aat is not None:
        props["aat"] = aat

    ccodes = doc.get("ccodes") or []
    if ccodes:
        primary_cc = ccodes[0]
        local_lang = COUNTRY_LOCAL_LANG.get(primary_cc)
        if local_lang and local_lang in names_by_lang:
            props["name_local"] = names_by_lang[local_lang]
    if "name_local" not in props and "und" in names_by_lang:
        props["name_local"] = names_by_lang["und"]

    # Settlement-significance metadata for the GeoNames bucket only.
    # ``population`` is a top-level int and ``fcode`` is the GeoNames
    # feature-code (PPLC capital, PPL generic populated place, ADM1
    # admin-1, …) held in the staged doc's ``types[0].identifier``. The
    # Atlas settlement overlay reads them from the ``gn`` tileset; other
    # buckets don't need the bytes (the overlay is GN-driven), so we keep
    # them out to hold tile size down for the polygon-heavy authorities.
    if namespace == "gn":
        pop = doc.get("population")
        if isinstance(pop, (int, float)) and pop > 0:
            props["population"] = int(pop)
        types = doc.get("types") or []
        if types and isinstance(types[0], dict):
            fcode = types[0].get("identifier")
            if isinstance(fcode, str) and fcode:
                props["fcode"] = fcode

    source_id = _extract_source_id(place_id)
    if misc:
        if isinstance(source_id, int):
            feature_id = encode_misc_feature_id(namespace, source_id)
        else:
            import hashlib
            h = hashlib.sha256(source_id.encode("utf-8")).digest()
            numeric_id = int.from_bytes(h[:7], "big") & ((1 << 52) - 1)
            feature_id = encode_misc_feature_id(namespace, numeric_id)
    else:
        feature_id = encode_feature_id(namespace, source_id)

    return {
        "type": "Feature",
        "id": feature_id,
        "properties": props,
        "geometry": full_geom,
    }


def _polygonal_parts(geom):
    """Yield the Polygon/MultiPolygon parts of a shapely geometry (incl. the
    polygonal members of a GeometryCollection); non-polygonal parts are skipped."""
    t = geom.geom_type
    if t in ("Polygon", "MultiPolygon"):
        if not geom.is_empty:
            yield geom
    elif t == "GeometryCollection":
        for g in geom.geoms:
            yield from _polygonal_parts(g)


def _linear_parts(geom):
    """Yield the LineString parts of a shapely geometry (incl. the linear
    members of a GeometryCollection); everything else is skipped."""
    t = geom.geom_type
    if t == "LineString":
        if not geom.is_empty:
            yield geom
    elif t == "MultiLineString":
        for g in geom.geoms:
            if not g.is_empty:
                yield g
    elif t == "GeometryCollection":
        for g in geom.geoms:
            yield from _linear_parts(g)


_POINT_TYPES = frozenset(("Point", "MultiPoint"))
_POLYGON_TYPES = frozenset(("Polygon", "MultiPolygon"))
_LINE_TYPES = frozenset(("LineString", "MultiLineString"))


def _geometry_kind(geom_json) -> str | None:
    """Classify a GeoJSON geometry as ``"point"``, ``"polygon"`` or ``"line"``.

    This is the ONLY place the channel routing looks, so a geometry type the
    pipeline has never produced before lands somewhere deliberate rather than
    being counted as a point by elimination — which is what the old
    ``is_poly = type in (Polygon, MultiPolygon, GeometryCollection)`` test did
    to every LineString (``pl``: "poly=0 point=18,250" in the 7 Aug log, for a
    bucket whose tiles carried 230 lines).

    A GeometryCollection is classified by its most areal member: polygon if it
    holds any, else line, else point. ``None`` for anything unrecognised.
    """
    if not isinstance(geom_json, dict):
        return None
    t = geom_json.get("type")
    if t in _POINT_TYPES:
        return "point"
    if t in _POLYGON_TYPES:
        return "polygon"
    if t in _LINE_TYPES:
        return "line"
    if t == "GeometryCollection":
        kinds = {_geometry_kind(g) for g in geom_json.get("geometries") or []}
        for k in ("polygon", "line", "point"):
            if k in kinds:
                return k
    return None


def _channel_for(kind: str | None) -> str | None:
    """Which channel a feature of this geometry kind is written to."""
    if kind == "point":
        return CHANNEL_POINTS
    if kind in ("polygon", "line"):
        return CHANNEL_SHAPES
    return None


# Above this many parts, the pairwise bbox precheck goes through an STRtree
# rather than a double loop. Archipelago states have thousands of parts.
_DISSOLVE_TREE_THRESHOLD = 32


def _dissolve_parts(geom_json):
    """Per-feature dissolve (place#166 §3): a MultiPolygon whose parts touch
    or overlap is ``unary_union``ed so a place made of several adjacent
    fragments renders without internal borders.

    Guarded by a bbox-intersection precheck so the common cases — a single
    Polygon, or a MultiPolygon of genuinely disjoint islands — never pay for
    the union. Returns the input unchanged whenever there is nothing to do or
    anything fails: a dissolve is a rendering nicety, never a reason to lose
    the feature.
    """
    if not isinstance(geom_json, dict) or geom_json.get("type") != "MultiPolygon":
        return geom_json
    coords = geom_json.get("coordinates") or []
    if len(coords) < 2:
        return geom_json
    try:
        from shapely.geometry import shape
        from shapely.ops import unary_union
        from shapely.validation import make_valid

        g = shape(geom_json)
        parts = [p for p in g.geoms if not p.is_empty]
        if len(parts) < 2:
            return geom_json

        touching = False
        if len(parts) <= _DISSOLVE_TREE_THRESHOLD:
            bounds = [p.bounds for p in parts]
            for i in range(len(bounds)):
                ax0, ay0, ax1, ay1 = bounds[i]
                for j in range(i + 1, len(bounds)):
                    bx0, by0, bx1, by1 = bounds[j]
                    if ax0 <= bx1 and bx0 <= ax1 and ay0 <= by1 and by0 <= ay1:
                        touching = True
                        break
                if touching:
                    break
        else:
            from shapely import STRtree
            tree = STRtree(parts)
            left, right = tree.query(parts, predicate="intersects")
            touching = bool(((left != right)).any())
        if not touching:
            return geom_json

        fixed = [p if p.is_valid else make_valid(p) for p in parts]
        merged = unary_union(fixed)
        if merged.is_empty:
            return geom_json
        polys = list(_polygonal_parts(merged))
        if not polys:
            return geom_json
        if len(polys) == 1:
            out = polys[0]
        else:
            from shapely.geometry import MultiPolygon
            flat: list = []
            for p in polys:
                flat.extend(p.geoms if p.geom_type == "MultiPolygon" else [p])
            out = MultiPolygon(flat) if len(flat) > 1 else flat[0]
        return out.__geo_interface__
    except Exception:
        return geom_json


def _accumulate_coverage(geom_json, sink: list) -> None:
    """Add a feature geometry's EXTENT contribution to the union ``sink``.

    ``sink`` is a list of shapely polygons later dissolved via ``unary_union``
    into the ``coverage: 1`` footprint. Polygons contribute their (repaired,
    pre-simplified) rings; lines contribute a ``_LINE_BUFFER_DEG`` buffer so a
    route gazetteer has a low-zoom extent at all (place#166 — previously lines
    contributed nothing, and ``pl`` drew as a bare scatter below z8). Points
    contribute nothing: the clustered heatmap is their extent.

    Each polygon is simplified at the footprint's own tolerance BEFORE the
    union. The union's cost is in vertices, and the footprint is simplified
    at that tolerance afterwards anyway, so nothing visible is lost; without
    it the union of ``wd``'s ~51k boundaries is the long pole of its build.
    """
    if not isinstance(geom_json, dict):
        return
    kind = _geometry_kind(geom_json)
    if kind not in ("polygon", "line"):
        return
    try:
        from shapely.geometry import shape
        from shapely.validation import make_valid
        g = shape(geom_json)
        if not g.is_valid:
            g = make_valid(g)
        for part in _polygonal_parts(g):
            simplified = part.simplify(_COVERAGE_SIMPLIFY_DEG, preserve_topology=True)
            sink.append(simplified if not simplified.is_empty else part)
        for line in _linear_parts(g):
            buffered = (line.simplify(_COVERAGE_SIMPLIFY_DEG, preserve_topology=True)
                        .buffer(_LINE_BUFFER_DEG))
            if not buffered.is_empty:
                sink.append(buffered)
    except Exception:
        pass


def _drop_small_holes(geom):
    """Remove interior rings below ``_EXTENT_MIN_HOLE_AREA`` from a polygonal
    geometry. Pre-simplified neighbours leave sliver gaps along shared borders
    that the union preserves as pinholes; none is visible at z7."""
    from shapely.geometry import MultiPolygon, Polygon

    def _one(p):
        if not p.interiors:
            return p
        keep = [r for r in p.interiors
                if Polygon(r).area >= _EXTENT_MIN_HOLE_AREA]
        return p if len(keep) == len(p.interiors) else Polygon(p.exterior, keep)

    if geom.geom_type == "Polygon":
        return _one(geom)
    if geom.geom_type == "MultiPolygon":
        return MultiPolygon([_one(p) for p in geom.geoms])
    return geom


# Properties carried onto a label anchor. The style's label layers filter on
# `boundary`, and the whg3 date filter reads `start`/`end` plus the definite
# core `start_def`/`end_def` (place#176), so those must survive — a label whose
# polygon the date filter keeps must not itself vanish, and vice versa.
# `aat` / `population` / `fcode` are dropped: labels are not type-filtered and
# every omitted key is bytes on every anchor.
_LABEL_KEEP_PROPS = ("place_id", "namespace", "boundary", "name", "name_local",
                     "start", "end", "start_def", "end_def")

# Above this vertex count, simplify before polylabel. polylabel is iterative
# and unbounded on a 100k-vertex ring — Australia's outline is 1,655,696.
_LABEL_SIMPLIFY_VERTICES = 5_000


def _label_anchor(geom):
    """Pole of inaccessibility for a polygonal geometry, or None.

    NOT the staged ``repr_point``: that is shapely ``representative_point()``,
    guaranteed inside but often hard against an edge, which puts the label in
    the wrong place on a concave region — the very thing #159 asks us to fix.

    Falls back ``polylabel`` → ``representative_point`` → ``centroid`` so a
    degenerate ring yields a usable anchor rather than no label at all.
    """
    parts = [g for g in _polygonal_parts(geom) if not g.is_empty]
    if not parts:
        return None
    # polylabel takes a single Polygon; use the largest-area member so the
    # label lands on the mainland rather than an offshore islet.
    flat = []
    for g in parts:
        flat.extend(g.geoms if g.geom_type == "MultiPolygon" else [g])
    flat = [g for g in flat if not g.is_empty and g.area > 0]
    if not flat:
        return None
    target = max(flat, key=lambda g: g.area)

    try:
        n = len(target.exterior.coords)
    except Exception:
        n = 0
    if n > _LABEL_SIMPLIFY_VERTICES:
        simplified = target.simplify(_COVERAGE_SIMPLIFY_DEG,
                                     preserve_topology=True)
        if not simplified.is_empty and simplified.geom_type == "Polygon":
            target = simplified

    try:
        from shapely.ops import polylabel
        return polylabel(target, tolerance=_COVERAGE_SIMPLIFY_DEG)
    except Exception:
        pass
    try:
        return target.representative_point()
    except Exception:
        pass
    try:
        return target.centroid
    except Exception:
        return None


def _line_anchor(geom):
    """Label anchor for a linear geometry: the midpoint of the longest
    constituent segment (plan §3.3), which keeps the label on a straight
    stretch of a route rather than at a vertex or off the line entirely as a
    centroid would be. None when there is no segment of positive length."""
    best = None
    best_len = 0.0
    for line in _linear_parts(geom):
        coords = list(line.coords)
        for (x0, y0, *_), (x1, y1, *_) in zip(coords, coords[1:]):
            seg = (x1 - x0) ** 2 + (y1 - y0) ** 2
            if seg > best_len:
                best_len = seg
                best = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    if best is None:
        return None
    from shapely.geometry import Point
    return Point(best)


def _label_point_feature(feature: dict) -> dict[str, Any] | None:
    """One label anchor per SHAPE feature, marked ``label: 1`` (place#159).

    Polygons anchor at their pole of inaccessibility; lines (place#166) at
    the midpoint of their longest segment. Points yield nothing.

    A polygon is cut at every tile edge, and MapLibre draws one symbol per
    feature *per tile* — so Nebraska got five "Nebraska" labels and Italy
    eight "Italia". Nothing client-side can fix that: choosing the right
    fragment needs the label point, which is exactly what the tileset lacked.

    The anchor is emitted into the **same source-layer** as the shapes and
    distinguished by ``label: 1``, per plan-atlas-data-architecture.md §3.1 —
    which withdrew the earlier separate-``<bucket>_labels``-layer design. A
    separate vector layer would get its own heatmap from ``loadGazetteerStyle``
    (a density field made purely of label anchors), and would need tile-join's
    keep-only ``-l`` filter turned into a sequence, which silently discards a
    layer when got wrong. Sharing the layer also shares the feature id, so
    hovering a label highlights its polygon with no bookkeeping.

    Returns None for non-polygonal features: a point is already its own anchor,
    and emitting anchors for points would double gn's ~12M features for nothing.
    """
    geom_json = feature.get("geometry")
    if not isinstance(geom_json, dict):
        return None
    kind = _geometry_kind(geom_json)
    if kind not in ("polygon", "line"):
        return None
    try:
        from shapely.geometry import shape
        from shapely.validation import make_valid
        g = shape(geom_json)
        if not g.is_valid:
            g = make_valid(g)
        anchor = _label_anchor(g) if kind == "polygon" else _line_anchor(g)
    except Exception:
        return None
    if anchor is None or anchor.is_empty:
        return None

    src = feature.get("properties") or {}
    props: dict[str, Any] = {"label": 1}
    for k in _LABEL_KEEP_PROPS:
        if src.get(k) is not None:
            props[k] = src[k]
    # Localised names (name_en, name_fr, ...) drive multilingual labels.
    for k, v in src.items():
        if k.startswith("name_") and v is not None:
            props[k] = v
    if "name" not in props and "name_local" not in props:
        return None  # nothing to draw

    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Point",
                     "coordinates": [round(anchor.x, 6), round(anchor.y, 6)]},
    }


def _coverage_feature(poly_geoms: list, namespace: str) -> dict[str, Any] | None:
    """Build the single dissolved COVERAGE FOOTPRINT feature (place#140).

    ``unary_union`` of every polygon in the bucket → simplify → one feature
    tagged ``coverage: 1`` (no ``place_id`` — it is synthetic and must never be
    clickable) and capped at ``_COVERAGE_MAXZOOM``. Returns ``None`` when the
    bucket has no polygons or the union is empty.
    """
    if not poly_geoms:
        return None
    try:
        from shapely.ops import unary_union
        merged = unary_union(poly_geoms)
        if merged.is_empty:
            return None
        merged = merged.simplify(_COVERAGE_SIMPLIFY_DEG, preserve_topology=True)
        if merged.is_empty:
            return None
        merged = _drop_small_holes(merged)
        return {
            "type": "Feature",
            "properties": {
                "coverage": 1,
                "namespace": namespace,
            },
            "geometry": merged.__geo_interface__,
        }
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  ⚠ coverage union failed for {namespace}: {exc}")
        return None


def _doc_belongs_to_bucket(
    doc: dict[str, Any], bucket: str, namespace: str
) -> tuple[bool, bool]:
    """Return (matches, is_misc).

    For fixed buckets (``osm`` / ``ohm`` / ``osm_misc``) the doc must carry
    a ``boundary`` value of the right category. For per-namespace buckets
    the doc's namespace must match the bucket name and it must carry some
    renderable geometry. For per-WHG-dataset buckets the namespace must be
    ``whg`` and the ``place_id`` must start with ``whg:<sub_id>:``.

    ``is_misc`` toggles the alternate feature-id encoding used by ``osm_misc``.
    """
    # Admin-boundary fixed buckets — note ``osm`` and ``ohm`` here are the
    # FIXED-BUCKET keys (admin only), not the per-namespace catch-all path
    # that follows. The check is unambiguous because ``namespace == bucket``
    # AND ``boundary`` must be admin-level.
    if bucket in ("osm", "ohm"):
        boundary = doc.get("boundary")
        if not boundary:
            return False, False
        if namespace != bucket:
            return False, False
        return _is_admin_level(boundary), False
    if bucket == "osm_misc":
        boundary = doc.get("boundary")
        if not boundary:
            return False, False
        if namespace not in ("osm", "ohm"):
            return False, False
        return _is_misc_boundary(boundary), True

    if bucket in _PER_NAMESPACE_BUCKETS:
        if namespace != bucket:
            return False, False
        geoms = doc.get("geometries") or []
        return any(_has_renderable_geometry(g) for g in geoms), False

    if bucket in _CONTEXT_OVERLAY_BUCKETS:
        cfg = _CONTEXT_OVERLAY_BUCKETS[bucket]
        if namespace != cfg["namespace"]:
            return False, False
        geoms = doc.get("geometries") or []
        if not any(_has_renderable_geometry(g) for g in geoms):
            return False, False
        types = doc.get("types") or []
        first = types[0] if types and isinstance(types[0], dict) else None
        fcode = first.get("identifier") if first else None
        return (isinstance(fcode, str) and fcode in cfg["fcodes"]), False

    if bucket.startswith(_WHG_BUCKET_PREFIX):
        if namespace != "whg":
            return False, False
        sub_id = _whg_dataset_sub_id(bucket)
        if not sub_id:
            return False, False
        place_id = doc.get("place_id") or ""
        if not place_id.startswith(f"whg:{sub_id}:"):
            return False, False
        geoms = doc.get("geometries") or []
        return any(_has_renderable_geometry(g) for g in geoms), False

    return False, False


class BucketStream:
    """What ``_stream_bucket`` produced for one bucket (place#166).

    * ``paths`` — ``{channel: Path}`` for every channel that received at
      least one feature; empty channels have no entry and no file.
    * ``counts`` — ``{"point": n, "polygon": n, "line": n}`` of REAL features
      (synthetic extent and label anchors are not counted here).
    * ``written`` — ``{namespace: real-feature count}`` for metrics.
    * ``labels`` — label anchors written.
    * ``extent`` — True when a dissolved footprint was written.
    """

    __slots__ = ("paths", "counts", "written", "labels", "extent")

    def __init__(self) -> None:
        self.paths: dict[str, Path] = {}
        self.counts: dict[str, int] = {"point": 0, "polygon": 0, "line": 0}
        self.written: dict[str, int] = {}
        self.labels: int = 0
        self.extent: bool = False

    @property
    def shapes(self) -> int:
        return self.counts["polygon"] + self.counts["line"]

    def as_ledger(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "written": dict(self.written),
            "label_anchors": self.labels,
            "extent": self.extent,
            "channels_streamed": sorted(self.paths),
        }


def channel_paths(bucket: str, out_dir: Path) -> dict[str, Path]:
    """The GeoJSONL path for each channel of ``bucket`` under ``out_dir``."""
    return {ch: out_dir / f"{bucket}.{ch}.geojsonl" for ch in CHANNELS}


def _stream_bucket(
    bucket: str,
    reader: GeomStoreReader,
    *,
    out_dir: Path,
    emit_labels: bool = True,
    emit_extent: bool = True,
    tier_counts: dict[str, int] | None = None,
) -> BucketStream:
    """Stream every contributing namespace's docs into per-CHANNEL GeoJSONL.

    Each feature routes itself by geometry type (``_geometry_kind``) into the
    ``points`` or ``shapes`` channel; every shape additionally yields one
    label anchor into ``labels`` and contributes to the dissolved footprint
    written as the single ``extent`` feature. There is no per-bucket vote:
    a bucket with 11 M points and 51k polygons gets all four channels, and a
    bucket with 892 polygons and 13k points gets all four too.

    Files are truncated at the start so reruns are clean, and empty channels
    are removed afterwards so the caller never tiles an empty file. The z8
    pin for shapes is applied by the tippecanoe stage (``--minimum-zoom``),
    not here — tippecanoe ignores a per-feature minzoom in ``properties``.
    """
    stream = BucketStream()
    contributors = _bucket_contributors(bucket)
    if not contributors:
        return stream

    require_boundary = bucket in _FIXED_BUCKETS
    paths = channel_paths(bucket, out_dir)
    for p in paths.values():
        p.write_bytes(b"")
    written: dict[str, int] = defaultdict(int)
    extent_geoms: list = []

    handles = {
        CHANNEL_POINTS: open(paths[CHANNEL_POINTS], "ab"),
        CHANNEL_SHAPES: open(paths[CHANNEL_SHAPES], "ab"),
    }
    labels_fh = open(paths[CHANNEL_LABELS], "ab") if emit_labels else None
    try:
        for namespace in contributors:
            for doc in _iter_namespace_docs(namespace):
                place_id = doc.get("place_id") or ""
                ns = place_id.split(":", 1)[0] if ":" in place_id else namespace
                # Trust place_id over file location: cross-namespace docs in
                # the same snapshot (e.g. synthetic osm: rows from
                # un-geoscheme-boundaries) are still classified correctly.
                matches, is_misc = _doc_belongs_to_bucket(doc, bucket, ns)
                if not matches:
                    continue
                feature = _build_staged_feature(
                    doc, ns, reader,
                    misc=is_misc,
                    require_boundary=require_boundary,
                    tier_counts=tier_counts,
                )
                if feature is None:
                    continue
                geom = feature.get("geometry")
                kind = _geometry_kind(geom)
                channel = _channel_for(kind)
                if channel is None:
                    continue
                if kind == "polygon":
                    # Per-feature dissolve: adjacent fragments of one place
                    # render without internal borders (plan §3, use case 1).
                    feature["geometry"] = geom = _dissolve_parts(geom)
                stream.counts[kind] += 1
                if channel == CHANNEL_SHAPES:
                    if emit_extent:
                        _accumulate_coverage(geom, extent_geoms)
                    if labels_fh is not None:
                        lf = _label_point_feature(feature)
                        if lf is not None:
                            labels_fh.write(orjson.dumps(lf))
                            labels_fh.write(b"\n")
                            stream.labels += 1
                fh = handles[channel]
                fh.write(orjson.dumps(feature))
                fh.write(b"\n")
                written[ns] += 1
    finally:
        for h in handles.values():
            h.close()
        if labels_fh is not None:
            labels_fh.close()

    if emit_extent and extent_geoms:
        ns0 = contributors[0]
        cov_feature = _coverage_feature(extent_geoms, ns0)
        if cov_feature is not None:
            paths[CHANNEL_EXTENT].write_bytes(orjson.dumps(cov_feature) + b"\n")
            stream.extent = True

    stream.written = dict(written)
    for ch, p in paths.items():
        if p.exists() and p.stat().st_size > 0:
            stream.paths[ch] = p
        else:
            try:
                p.unlink()
            except OSError:
                pass
    return stream


def _stream_bucket_banded(
    bucket: str,
    reader: GeomStoreReader,
    bands: list,                       # list[Band] from tilegen_bands
    *,
    out_dir: Path,
    emit_labels: bool = True,
    tier_counts: dict[str, int] | None = None,
) -> tuple[dict[str, Path], dict[str, dict[str, int]], dict[str, Path]]:
    """Stream a bucket's features into ONE geojsonl per band.

    Returns ``(band_paths, per_band_counts)``:

    * ``band_paths`` — ``{band.name: Path}`` for each band that received
      at least one feature. Bands with zero features are omitted (no
      empty file is written).
    * ``per_band_counts`` — ``{band.name: {namespace: count}}`` for
      diagnostics.

    Features that match no band are dropped (logged via the "unmatched"
    counter, not a separate file).

    Note: the banded path intentionally does NOT emit the place#140 coverage
    footprint. It serves only the fixed admin buckets (``osm``/``ohm``/
    ``osm_misc``); a global admin footprint is meaningless, and these keep their
    per-band minzooms. The footprint pass lives in ``_stream_bucket``.
    """
    from processing.tilegen_bands import assign_band

    contributors = _bucket_contributors(bucket)
    if not contributors:
        return {}, {}

    require_boundary = bucket in _FIXED_BUCKETS
    band_paths = {b.name: out_dir / f"{bucket}.{b.name}.geojsonl" for b in bands}
    for p in band_paths.values():
        p.write_bytes(b"")  # truncate
    band_counts: dict[str, dict[str, int]] = {b.name: defaultdict(int) for b in bands}
    unmatched = 0

    # place#159: one label anchor per polygon, per band — label zoom windows
    # differ from the polygon bands (continental polygons stop at z4 but its
    # labels are wanted to z5), so they cannot share a pass.
    label_paths = ({b.name: out_dir / f"{bucket}.{b.name}.labels.geojsonl"
                    for b in bands} if emit_labels else {})
    for p in label_paths.values():
        p.write_bytes(b"")
    label_handles = {name: open(p, "ab") for name, p in label_paths.items()}

    handles = {name: open(p, "ab") for name, p in band_paths.items()}
    try:
        for namespace in contributors:
            for doc in _iter_namespace_docs(namespace):
                place_id = doc.get("place_id") or ""
                ns = place_id.split(":", 1)[0] if ":" in place_id else namespace
                matches, is_misc = _doc_belongs_to_bucket(doc, bucket, ns)
                if not matches:
                    continue
                feature = _build_staged_feature(
                    doc, ns, reader,
                    misc=is_misc, require_boundary=require_boundary,
                    tier_counts=tier_counts,
                )
                if feature is None:
                    continue
                band = assign_band(feature, bands)
                if band is None:
                    unmatched += 1
                    continue
                handles[band.name].write(orjson.dumps(feature))
                handles[band.name].write(b"\n")
                band_counts[band.name][ns] += 1
                lh = label_handles.get(band.name)
                if lh is not None:
                    lf = _label_point_feature(feature)
                    if lf is not None:
                        lh.write(orjson.dumps(lf))
                        lh.write(b"\n")
    finally:
        for h in handles.values():
            h.close()
        for h in label_handles.values():
            h.close()

    if unmatched:
        print(f"  ⚠ {unmatched:,} features matched no band (dropped)")

    # Drop empty bands from band_paths so callers don't waste tippecanoe
    # invocations on them.
    band_paths = {
        name: path for name, path in band_paths.items()
        if path.exists() and path.stat().st_size > 0
    }
    label_paths = {
        name: path for name, path in label_paths.items()
        if path.exists() and path.stat().st_size > 0
    }
    return (band_paths,
            {name: dict(c) for name, c in band_counts.items()},
            label_paths)


# tile-join announces a dropped tile on stderr and exits 0 regardless. The
# wording has varied across tippecanoe releases, so match on the two things
# every variant has: a z/x/y triple and a "skip" verb.
_TILE_SKIP_RE = re.compile(r"\b\d+/\d+/\d+\b.*\bskip", re.IGNORECASE)


def _tile_join_skips(stderr: str) -> list[str]:
    """Return tile-join stderr lines reporting a dropped tile."""
    return [ln.strip() for ln in (stderr or "").splitlines()
            if _TILE_SKIP_RE.search(ln)]


def tile_join(
    band_mbtiles: list[Path],
    output: Path,
    *,
    layer_name: str | None = None,
) -> bool:
    """Combine per-band ``.mbtiles`` into one canonical bucket mbtiles
    using ``tile-join`` from the tippecanoe suite.

    Returns True on success. Removes the per-band intermediate files
    on success (caller can keep them by setting an env var if needed).
    """
    if not band_mbtiles:
        print("  ✗ tile_join: no bands provided")
        return False
    if len(band_mbtiles) == 1:
        # Trivial case: just rename. tile-join works but is wasteful.
        if output.exists():
            output.unlink()
        band_mbtiles[0].rename(output)
        size_mb = output.stat().st_size / 1e6
        print(f"  ✓ {output.name} (1 band, renamed): {size_mb:.1f} MB")
        return True

    tj = shutil.which("tile-join")
    if not tj:
        print("  ✗ tile-join binary not found on PATH")
        return False

    if output.exists():
        output.unlink()
    cmd = [tj, "--force", "--no-tile-compression", "-o", str(output)]
    # -pk / --no-tile-size-limit: WITHOUT it, tile-join has no thinning
    # strategy — a merged tile over the 500 KB ceiling is DROPPED WHOLE, and
    # tile-join still exits 0. That is place#160: `ohm` lost 0/0/0 (the entire
    # world), both z1 northern-hemisphere tiles, and central Europe at z3,
    # while the job reported success and the tileset shipped with square holes.
    #
    # Each band can sit comfortably under the limit while their SUM does not,
    # which is exactly the observed shape — only the densest merged tiles
    # vanished. Keeping an oversize tile is a far better failure than losing a
    # continent; the per-band thinning below is what actually keeps sizes sane.
    cmd.append("-pk")
    if layer_name:
        # -l filters input features to the named layer (every band emits
        # only that layer, so this is a safety check). -n sets the output
        # mbtiles name field; without it tile-join concatenates the input
        # names with " + " and the tileserver tilejson ends up as e.g.
        # "WHG osm + WHG osm + ...".
        cmd.extend(["-l", layer_name, "-n", f"WHG {layer_name}"])
    cmd.extend(str(p) for p in band_mbtiles)

    print(f"  joining {len(band_mbtiles)} band mbtiles → {output.name} ...")
    start = time.time()
    # Capture stderr rather than passing it through: tile-join reports a
    # dropped tile on stderr and STILL EXITS 0, so streaming it to the console
    # and checking only the return code is precisely how place#160 shipped
    # silently. A skipped tile is a build failure, not a log line.
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=subprocess.PIPE,
                            text=True)
    elapsed = time.time() - start
    stderr = result.stderr or ""
    if stderr:
        sys.stderr.write(stderr)
    if result.returncode != 0:
        print(f"  ✗ tile-join failed (rc={result.returncode})")
        return False

    skipped = _tile_join_skips(stderr)
    if skipped:
        print(f"  ✗ tile-join SKIPPED {len(skipped)} tile(s) — refusing to "
              f"publish a tileset with holes (place#160). First few:")
        for line in skipped[:5]:
            print(f"      {line}")
        return False

    size_mb = output.stat().st_size / 1e6 if output.exists() else 0
    print(f"  ✓ {output.name}: {size_mb:.1f} MB ({elapsed:.0f}s, {len(band_mbtiles)} bands joined)")

    # Cleanup intermediates
    for p in band_mbtiles:
        try:
            p.unlink()
        except OSError:
            pass
    return True


def _channel_plan(
    stream: "BucketStream",
    *,
    ctx_cfg: dict[str, Any] | None = None,
    maxzoom: int = 10,
) -> list[dict[str, Any]]:
    """The tippecanoe passes a streamed bucket needs, one per non-empty channel.

    Pure: decides zoom ranges and flags from what was streamed, so it can be
    tested without tippecanoe. Each entry: ``{channel, minzoom, maxzoom,
    cluster_points, preserve_all, required}``. ``required`` says whether a
    failed pass fails the bucket (``points`` / ``shapes`` carry the real
    features) or is survivable with a warning (``extent`` / ``labels`` are
    derived and the map degrades gracefully without them).

    The shapes and labels passes start at ``_BOUNDARY_MINZOOM`` ONLY when an
    extent was streamed to own the zooms beneath them. A bucket whose extent
    union failed tiles its shapes from z0 instead — the old code pinned them
    to z8 and then deployed "boundaries without footprint", i.e. nothing at
    all below z8.

    Context-overlay buckets (``ctx_cfg``) keep their own zoom range and
    clustering choice for the points pass and emit no derived channels.
    """
    plan: list[dict[str, Any]] = []
    # Order matters for tile-join only in that the first input's metadata
    # name wins; every pass uses the same layer/name, so any order works.
    # Extent is listed first so its outcome is known before shapes decide
    # their minzoom — ``_build_channels`` honours this order.
    if CHANNEL_EXTENT in stream.paths:
        plan.append({"channel": CHANNEL_EXTENT, "minzoom": 0,
                     "maxzoom": _COVERAGE_MAXZOOM,
                     "cluster_points": False, "preserve_all": False,
                     "required": False})
    if CHANNEL_SHAPES in stream.paths:
        plan.append({"channel": CHANNEL_SHAPES,
                     "minzoom": _BOUNDARY_MINZOOM if CHANNEL_EXTENT in stream.paths else 0,
                     "maxzoom": maxzoom,
                     "cluster_points": False, "preserve_all": True,
                     "required": True})
    if CHANNEL_POINTS in stream.paths:
        if ctx_cfg is not None:
            plan.append({"channel": CHANNEL_POINTS,
                         "minzoom": ctx_cfg["minzoom"], "maxzoom": ctx_cfg["maxzoom"],
                         "cluster_points": ctx_cfg["cluster_points"],
                         "preserve_all": False, "required": True})
        else:
            plan.append({"channel": CHANNEL_POINTS, "minzoom": 0,
                         "maxzoom": maxzoom,
                         "cluster_points": True, "preserve_all": False,
                         "required": True})
    if CHANNEL_LABELS in stream.paths:
        # Labels share the shapes' minzoom: an anchor for a shape that is
        # not drawn below z8 is bytes in every low-zoom tile for nothing —
        # and at z0 ONE tile holds every anchor in the bucket (wd: 51k),
        # which is exactly the oversize-tile shape place#160 warned about.
        plan.append({"channel": CHANNEL_LABELS,
                     "minzoom": _BOUNDARY_MINZOOM if CHANNEL_EXTENT in stream.paths else 0,
                     "maxzoom": maxzoom,
                     "cluster_points": False, "preserve_all": True,
                     "required": False})
    return plan


def _build_channels(
    bucket: str,
    stream: "BucketStream",
    *,
    out_dir: Path,
    mbtiles: Path,
    description: str,
    ctx_cfg: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Tile every channel of ``stream`` and ``tile-join`` them into ``mbtiles``.

    Returns ``(built, failures)``. ``built`` is True when the final bucket
    tileset exists; ``failures`` names the passes that failed, including the
    survivable ones (``extent`` / ``labels``) so the caller can log them.

    When the extent pass fails the shapes pass is re-planned from z0, so a
    footprint failure degrades to "boundaries everywhere" rather than
    "boundaries nowhere below z8".
    """
    plan = _channel_plan(stream, ctx_cfg=ctx_cfg)
    if not plan:
        return False, []
    failures: list[str] = []
    built_mbtiles: list[Path] = []
    extent_ok = CHANNEL_EXTENT not in stream.paths
    ledger_channels: dict[str, dict[str, Any]] = {}

    for pass_ in plan:
        ch = pass_["channel"]
        src = stream.paths[ch]
        minzoom = pass_["minzoom"]
        if ch in (CHANNEL_SHAPES, CHANNEL_LABELS) and not extent_ok:
            minzoom = 0
        target = out_dir / f"{bucket}.{ch}.mbtiles"
        print(f"\n  channel '{ch}' (z{minzoom}-{pass_['maxzoom']}"
              f"{', clustered' if pass_['cluster_points'] else ''}"
              f"{', no-drop' if pass_['preserve_all'] else ''})")
        ok = generate_tileset(
            src, target, bucket, description,
            minzoom=minzoom, maxzoom=pass_["maxzoom"],
            cluster_points=pass_["cluster_points"],
            preserve_all=pass_["preserve_all"],
        )
        if ok:
            built_mbtiles.append(target)
            ledger_channels[ch] = {"minzoom": minzoom, "maxzoom": pass_["maxzoom"],
                                   "cluster_points": pass_["cluster_points"],
                                   "preserve_all": pass_["preserve_all"]}
            if ch == CHANNEL_EXTENT:
                extent_ok = True
        else:
            failures.append(f"{bucket}/{ch}")
            if ch == CHANNEL_EXTENT:
                print(f"  ⚠ {bucket}: extent pass failed — shapes will be "
                      f"tiled from z0 instead of pinned to z{_BOUNDARY_MINZOOM}")
            elif ch == CHANNEL_LABELS:
                print(f"  ⚠ {bucket}: label pass failed — tileset will have no anchors")
            if pass_["required"]:
                return False, failures

    if not built_mbtiles:
        return False, failures
    if not tile_join(built_mbtiles, mbtiles, layer_name=bucket):
        failures.append(f"{bucket}/join")
        return False, failures

    _write_channel_ledger(bucket, stream, ledger_channels, mbtiles)
    return True, failures


def _write_channel_ledger(bucket: str, stream: "BucketStream",
                          channels: dict[str, dict[str, Any]],
                          mbtiles: Path) -> Path:
    """Write ``<bucket>.channels.json`` beside the built tileset.

    The verifier reads this to know what the tileset SHOULD contain. The
    build writes it, the verifier never does — a ledger the checker could
    regenerate from the artefact would agree with the artefact by
    construction.
    """
    ledger = {
        "bucket": bucket,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mbtiles": mbtiles.name,
        "mbtiles_bytes": mbtiles.stat().st_size if mbtiles.exists() else None,
        "channels": channels,
        **stream.as_ledger(),
    }
    path = mbtiles.with_name(bucket + CHANNEL_LEDGER_SUFFIX)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
    return path


def generate_tiles_from_staged(
    *,
    buckets: list[str] | None = None,
    output_dir: Path | None = None,
    deploy: bool = False,
    run_id: str | None = None,
    manifest_path: Path | None = None,
    skip_tippecanoe: bool = False,
) -> list[Path]:
    """Tileset generation from staged snapshots — no ES dependency.

    Three families of bucket are produced, each owned by a single writer so
    concurrent Slurm tasks (one task per bucket) never race on the same
    output file:

    * Fixed buckets (``osm``, ``ohm``, ``osm_misc``) — boundary-gated,
      polygon-only, fed from the geom store.
    * Per-namespace buckets — one ``<ns>.mbtiles`` per authority namespace,
      every doc with renderable geometry (point or polygon).
    * Per-WHG-dataset buckets — ``whg-<dataset_sub_id>.mbtiles``, one per
      contributor dataset discovered at submit time.

    For each contributing namespace the function streams the most-enriched
    staged snapshot (``final/`` → ``h3_merged/`` → ``boundary_merged/`` →
    ``extract/``). Polygon geometries come from the geom store; point-only
    docs use the inline ``repr_point`` field. Fixed buckets refuse the
    point-fallback path so their output remains polygon-only.

    Args:
        buckets: Restrict to these tile buckets (default: every fixed +
            per-namespace + per-WHG-dataset bucket). Unknown values are
            silently dropped.
        output_dir: Override default output directory (``DATA_DIR/tiles``).
        deploy: rsync resulting ``.mbtiles`` to the tile server when True.
        run_id, manifest_path: Optional manifest hooks; the ``tiles`` stage
            status of every contributing namespace is updated and stage
            events are written when both are provided.
        skip_tippecanoe: Emit GeoJSONL only (used by smoke tests).

    Raises:
        FileNotFoundError: If the geometry store at ``GEOM_STORE_DIR`` is
            absent — tile generation cannot proceed without full polygons.
    """
    from processing.stage_writers import (
        record_script_wall_time,
        write_runtime_history_event,
        write_stage_event,
    )
    from processing.staging_orchestrator import update_namespace_stage_status

    print("=" * 80)
    print("TILESET GENERATION (STAGED SOURCE)")
    print("=" * 80)

    out_dir = Path(output_dir) if output_dir else TILES_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if buckets:
        selected = [b for b in buckets if _is_known_bucket(b)]
    else:
        selected = resolve_buckets()
    if not selected:
        print("No tile buckets selected.")
        return []

    contributing_namespaces: set[str] = set()
    for bucket in selected:
        contributing_namespaces.update(_bucket_contributors(bucket))

    reader = GeomStoreReader(GEOM_STORE_DIR)

    if manifest_path is not None and manifest_path.exists():
        for ns in contributing_namespaces:
            update_namespace_stage_status(manifest_path, ns, "tiles", "running")
    if run_id:
        for ns in contributing_namespaces:
            try:
                write_stage_event(
                    run_id=run_id,
                    namespace=ns,
                    script_id="tiles",
                    status="running",
                    stage="tiles",
                )
            except Exception:
                pass

    # Load band rules once. Buckets with an entry get multi-band
    # streaming + tippecanoe + tile-join; buckets without an entry use
    # the legacy single-pass path (back-compat, e.g. the per-namespace
    # point sources where banding adds no value).
    from processing.tilegen_bands import load_bands as _load_bands
    bands_per_bucket = _load_bands()

    bucket_counts: dict[str, int] = {}
    per_namespace_totals: dict[str, int] = defaultdict(int)
    bucket_band_paths: dict[str, dict[str, Path]] = {}   # bucket → {band_name: geojsonl}
    bucket_label_paths: dict[str, dict[str, Path]] = {}  # place#159 label anchors (banded)
    bucket_streams: dict[str, BucketStream] = {}          # place#166 per-channel streams
    bucket_uses_bands: dict[str, bool] = {}
    # bucket → which geometry tier produced each feature; the publish gate's
    # entire input. Populated by ``_build_staged_feature`` during streaming.
    bucket_tiers: dict[str, dict[str, int]] = {}

    started = datetime.now(timezone.utc)
    try:
        for bucket in selected:
            bands = bands_per_bucket.get(bucket)
            if bands and len(bands) > 1:
                bucket_uses_bands[bucket] = True
                print(f"\nStreaming bucket '{bucket}' (banded: {[b.name for b in bands]}) "
                      f"from {_bucket_contributors(bucket)} ...")
                tiers = bucket_tiers.setdefault(bucket, {})
                band_paths, band_counts, label_paths = _stream_bucket_banded(
                    bucket, reader, bands, out_dir=out_dir,
                    tier_counts=tiers,
                )
                bucket_band_paths[bucket] = band_paths
                bucket_label_paths[bucket] = label_paths
                bucket_counts[bucket] = sum(
                    sum(c.values()) for c in band_counts.values()
                )
                for band_name, ns_counts in band_counts.items():
                    total = sum(ns_counts.values())
                    if total:
                        print(f"  {bucket}/{band_name}: {total:,} features "
                              f"({', '.join(f'{ns}={n:,}' for ns, n in ns_counts.items())})")
                for ns_counts in band_counts.values():
                    for ns, n in ns_counts.items():
                        per_namespace_totals[ns] += n
            else:
                bucket_uses_bands[bucket] = False
                # place#166: every feature routes itself into a channel by
                # geometry type; no per-bucket vote. Context-overlay buckets
                # (pre-filtered point subsets such as world capitals) get no
                # derived channels — no footprint, no anchors.
                is_ctx = bucket in _CONTEXT_OVERLAY_BUCKETS
                print(f"\nStreaming bucket '{bucket}' (channels) from {_bucket_contributors(bucket)} ...")
                tiers = bucket_tiers.setdefault(bucket, {})
                stream = _stream_bucket(
                    bucket, reader, out_dir=out_dir,
                    emit_labels=not is_ctx, emit_extent=not is_ctx,
                    tier_counts=tiers,
                )
                bucket_streams[bucket] = stream
                bucket_counts[bucket] = sum(stream.written.values())
                c = stream.counts
                for ns, n in stream.written.items():
                    per_namespace_totals[ns] += n
                    print(f"  {ns} → {bucket}: {n:,} features "
                          f"(poly={c['polygon']:,} line={c['line']:,} point={c['point']:,})")
                if stream.labels:
                    print(f"  + {stream.labels:,} label anchors (place#159)")
                if stream.extent:
                    print(f"  + dissolved extent footprint (place#140/#166) "
                          f"from {c['polygon']:,} polygons and {c['line']:,} lines")
    finally:
        try:
            reader.close()
        except Exception:
            pass

    wall_seconds = (datetime.now(timezone.utc) - started).total_seconds()

    print("\nFeature totals per bucket:")
    for bucket in selected:
        print(f"  {bucket}: {bucket_counts.get(bucket, 0):,}")

    tilesets_generated: list[Path] = []
    bucket_failures: list[str] = []
    push_failures: list[str] = []
    # bucket → why the publish gate refused it. A refused bucket is BUILT but
    # deliberately not pushed: the .mbtiles stays on disk for inspection and
    # the previously-deployed tileset keeps serving, which is the safe side to
    # fail on when the alternative is publishing points over real boundaries.
    gate_refusals: dict[str, list[str]] = {}
    # Buckets that produced no tileset because their source genuinely has no
    # renderable geometry — NOT because anything went wrong. Tracked so the
    # caller can tell "nothing to tile" from "the build broke": both end with
    # zero tilesets, and conflating them fails a run over an empty dataset.
    empty_buckets: list[str] = []
    if skip_tippecanoe:
        print("\n--skip-tippecanoe specified; GeoJSONL written but no .mbtiles produced.")
    else:
        for bucket in selected:
            mbtiles = out_dir / f"{bucket}.mbtiles"
            description = f"WHG {bucket}"

            if bucket_uses_bands.get(bucket):
                # Multi-band: tippecanoe per band → tile-join into final
                bands = bands_per_bucket[bucket]
                band_paths = bucket_band_paths.get(bucket, {})
                band_mbtiles: list[Path] = []
                for band in bands:
                    if band.name not in band_paths:
                        continue   # empty band, no features
                    band_geojsonl = band_paths[band.name]
                    band_mbtile = out_dir / f"{bucket}.{band.name}.mbtiles"
                    print(f"\n  band '{band.name}' (z{band.minzoom}-{band.maxzoom})")
                    if generate_tileset(
                        band_geojsonl, band_mbtile, bucket, description,
                        minzoom=band.minzoom, maxzoom=band.maxzoom,
                    ):
                        band_mbtiles.append(band_mbtile)
                    else:
                        if band_geojsonl.exists() and band_geojsonl.stat().st_size > 0:
                            bucket_failures.append(f"{bucket}/{band.name}")

                # place#159: one label-anchor pass per band, SAME layer name so
                # tile-join folds it into the single source-layer; the anchors
                # are distinguished by `label: 1`, not by living in their own
                # vector layer (which would earn them a spurious heatmap).
                label_paths = bucket_label_paths.get(bucket, {})
                for band in bands:
                    lp = label_paths.get(band.name)
                    if lp is None:
                        continue
                    label_mbtile = out_dir / f"{bucket}.{band.name}.labels.mbtiles"
                    print(f"\n  band '{band.name}' LABELS "
                          f"(z{band.lbl_min}-{band.lbl_max})")
                    if generate_tileset(
                        lp, label_mbtile, bucket, description,
                        minzoom=band.lbl_min, maxzoom=band.lbl_max,
                        preserve_all=True,
                    ):
                        band_mbtiles.append(label_mbtile)
                    else:
                        bucket_failures.append(f"{bucket}/{band.name}-labels")

                if band_mbtiles and tile_join(band_mbtiles, mbtiles, layer_name=bucket):
                    tilesets_generated.append(mbtiles)
                    gate_ok, gate_reasons = publish_gate(
                        bucket, bucket_tiers.get(bucket, {}), mbtiles
                    )
                    _log_gate(bucket, bucket_tiers.get(bucket, {}), gate_ok, gate_reasons)
                    if not gate_ok:
                        gate_refusals[bucket] = gate_reasons
                    elif deploy and not push_mbtiles_to_tileserver(mbtiles):
                        push_failures.append(bucket)
                else:
                    bucket_failures.append(bucket)
            else:
                stream = bucket_streams.get(bucket)
                if stream is None or not stream.paths:
                    # Empty streams ("nothing to tile") are benign; a build
                    # that fails on a non-empty input is a real failure and
                    # must not be recorded as completed — otherwise wall-time
                    # estimators pick up the partial run and undersize the
                    # next attempt's Slurm budget.
                    empty_buckets.append(bucket)
                    print(f"  {bucket}: nothing to tile — source carries no "
                          f"renderable geometry (not a failure)")
                    continue
                ctx_cfg = _CONTEXT_OVERLAY_BUCKETS.get(bucket)
                tile_description = (ctx_cfg.get("description") or description
                                    if ctx_cfg is not None else description)
                built, channel_failures = _build_channels(
                    bucket, stream, out_dir=out_dir, mbtiles=mbtiles,
                    description=tile_description, ctx_cfg=ctx_cfg,
                )
                for f in channel_failures:
                    print(f"  ⚠ channel pass failed: {f}")
                if built:
                    tilesets_generated.append(mbtiles)
                    # Per-bucket auto-push to the tileserver. Push failure is
                    # non-fatal here — the .mbtiles is still on /ix1 for a
                    # later catch-up push and the pipeline can continue. The
                    # eventual tileserver service restart is the user's
                    # manual step and gates on every bucket having pushed
                    # (see ``push_failures`` below).
                    gate_ok, gate_reasons = publish_gate(
                        bucket, bucket_tiers.get(bucket, {}), mbtiles
                    )
                    _log_gate(bucket, bucket_tiers.get(bucket, {}),
                              gate_ok, gate_reasons)
                    if not gate_ok:
                        gate_refusals[bucket] = gate_reasons
                    elif deploy and not push_mbtiles_to_tileserver(mbtiles):
                        push_failures.append(bucket)
                else:
                    bucket_failures.append(bucket)

    # Distinguish per-namespace status: if any bucket the namespace contributes
    # to failed tippecanoe, mark its tiles stage failed; otherwise completed.
    def _ns_buckets(ns: str) -> list[str]:
        return [b for b in selected if ns in _bucket_contributors(b)]

    def _ns_status(ns: str) -> str:
        # A gate refusal counts as a failure for the manifest. Recording it as
        # "completed" would let the next run skip a bucket that was built but
        # deliberately never published — the manifest asserting success for
        # work that was withheld is the exact pattern this gate exists to stop.
        bad = set(bucket_failures) | set(gate_refusals)
        return "failed" if any(b in bad for b in _ns_buckets(ns)) else "completed"

    if manifest_path is not None and manifest_path.exists():
        for ns in contributing_namespaces:
            metrics = {
                "features_written": per_namespace_totals.get(ns, 0),
                "buckets": _ns_buckets(ns),
                "wall_seconds": round(wall_seconds, 1),
            }
            update_namespace_stage_status(
                manifest_path, ns, "tiles", _ns_status(ns), metrics=metrics
            )
    if run_id:
        for ns in contributing_namespaces:
            metrics = {
                "features_written": per_namespace_totals.get(ns, 0),
                "buckets": _ns_buckets(ns),
                "wall_seconds": round(wall_seconds, 1),
            }
            ns_status = _ns_status(ns)
            try:
                write_stage_event(
                    run_id=run_id,
                    namespace=ns,
                    script_id="tiles",
                    status=ns_status,
                    stage="tiles",
                    metrics=metrics,
                )
                write_runtime_history_event(
                    run_id=run_id,
                    event="tiles",
                    status=ns_status,
                    namespace=ns,
                    stage="tiles",
                    details=metrics,
                )
            except Exception:
                pass
            try:
                # Persistent cross-run history so the next submit_tiles_slurm
                # can size --time appropriately per namespace.
                record_script_wall_time(
                    namespace=ns, script_id="tiles", run_id=run_id,
                    started_at=started.isoformat(),
                    finished_at=(started + timedelta(seconds=wall_seconds)).isoformat(),
                    wall_seconds=wall_seconds, status=ns_status,
                    slurm_job_id=os.environ.get("SLURM_JOB_ID"),
                    extra={"features_written": metrics["features_written"]},
                )
            except Exception:
                pass

    print(f"\n{'=' * 80}")
    print("STAGED TILESET GENERATION COMPLETE")
    print(f"{'=' * 80}")
    print(f"  Tilesets generated: {len(tilesets_generated)}")
    for p in tilesets_generated:
        size_mb = p.stat().st_size / 1e6 if p.exists() else 0
        print(f"    {p.name}: {size_mb:.1f} MB")

    if gate_refusals:
        print(
            f"\n🛑 PUBLISH GATE REFUSED {len(gate_refusals)} bucket(s) — "
            f"built but NOT pushed:"
        )
        for b, why in gate_refusals.items():
            print(f"  {b}:")
            for r in why:
                print(f"    ✗ {r}")
        print(
            "  The .mbtiles are on disk and the previously-deployed tilesets are\n"
            "  untouched. Investigate before overriding; a refusal here means the\n"
            "  build did not read the geometry it was supposed to."
        )

    if deploy and tilesets_generated:
        if push_failures:
            print(
                f"\n⚠ {len(push_failures)} of {len(tilesets_generated)} per-bucket "
                f"pushes FAILED: {push_failures}\n"
                "  Re-run with --bucket <name> for each, or "
                "`python -m processing.generate_tiles --redeploy` to retry pushes only.\n"
                "  Tileserver service restart is GATED on all pushes succeeding "
                "— do NOT restart until this list is empty."
            )
        else:
            print(
                f"\n✓ All {len(tilesets_generated)} buckets pushed to the tileserver. "
                "Tileserver service restart is the user's separate, explicit step."
            )

    return tilesets_generated, gate_refusals, empty_buckets


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate .mbtiles tilesets from staged boundary places"
    )
    parser.add_argument('--bucket', '-b', action='append',
                        help='Restrict to tile bucket(s); pass multiple times. '
                             'Default: every fixed bucket + every per-namespace bucket '
                             '+ every WHG dataset bucket discovered in the staged sidecar.')
    parser.add_argument('--output-dir', help='Output directory for tilesets')
    # Deploy is ON by default — per-bucket auto-push to the tileserver as
    # each .mbtiles completes (via the Pitt VM proxy). Use --no-deploy
    # for testing / dry-runs that should not touch the tileserver.
    parser.add_argument('--no-deploy', dest='deploy', action='store_false',
                        default=True,
                        help='Disable per-bucket push to the tileserver '
                             '(default: push automatically)')
    parser.add_argument('--redeploy-only', action='store_true',
                        help='Skip tile generation entirely; just push the '
                             'already-built .mbtiles for the selected buckets '
                             'to the tileserver. Use as a catch-up after a '
                             'partial deploy or push failure.')
    parser.add_argument('--run-id', help='Run ID for manifest updates')
    parser.add_argument('--manifest-path',
                        help='Run manifest path; if omitted derives from --run-id')
    parser.add_argument('--skip-tippecanoe', action='store_true',
                        help='Write only GeoJSONL files, do not invoke tippecanoe')
    args = parser.parse_args()

    if args.redeploy_only:
        # Push existing .mbtiles only; no tile gen.
        out_dir = Path(args.output_dir) if args.output_dir else TILES_OUTPUT_DIR
        if args.bucket:
            paths = [out_dir / f"{b}.mbtiles" for b in args.bucket if (out_dir / f"{b}.mbtiles").exists()]
        else:
            paths = sorted(out_dir.glob("*.mbtiles"))
        if not paths:
            print(f"No .mbtiles found in {out_dir} for selected buckets.")
            return
        print(f"Redeploy-only: pushing {len(paths)} existing .mbtiles ...")
        results = deploy_tilesets(paths)
        failed = [n for n, ok in results.items() if not ok]
        if failed:
            print(f"\n⚠ {len(failed)} push(es) failed: {failed}")
            sys.exit(1)
        return

    manifest_path = None
    if args.manifest_path:
        manifest_path = Path(args.manifest_path)
    elif args.run_id:
        manifest_path = Path(
            STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
                runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
            )
        )

    produced, gate_refusals, empty_buckets = generate_tiles_from_staged(
        buckets=args.bucket,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        deploy=args.deploy,
        run_id=args.run_id,
        manifest_path=manifest_path,
        skip_tippecanoe=args.skip_tippecanoe,
    )
    # Exit non-zero when a requested bucket produced nothing. Without this a
    # tippecanoe failure printed "Tilesets generated: 0" and still exited 0, so
    # Slurm recorded COMPLETED — which is how a 24-bucket array reported 14
    # successes while writing 0-byte tilesets (7 Aug 2026, ulimit -n 1024 and
    # three tasks sharing node-local /tmp). Same silent-failure class as the
    # tile-join drop in place#160: the build must fail loudly or it ships.
    if args.bucket and not produced:
        # "No tileset" has two causes and only one is a failure. A bucket whose
        # source carries no renderable geometry has nothing to tile and must
        # not fail the task — whg datasets with no geometry are legitimate, and
        # the sidecar gains them over time (measured 2 Sep 2026: whg-1642 and
        # whg-1644 hold 3 and 15 docs, all with geometries: 0, and were absent
        # from the August generation for the same reason). Failing on them
        # breaks any afterok chain built on the run, which is how two empty
        # datasets blocked the 74-bucket retile.
        unexplained = [b for b in args.bucket if b not in empty_buckets]
        if unexplained:
            print(f"ERROR: no tileset produced for {', '.join(unexplained)}",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Nothing to tile for {', '.join(args.bucket)} "
              f"(no renderable geometry) — not a failure.")
    # A gate refusal must also exit non-zero. The trailing tileserver config
    # rewrite + restart is submitted ``afterok`` of every tile array, so
    # exiting 0 here would let a run that withheld a bucket go on to register
    # and restart as though it had published one.
    if gate_refusals:
        print(
            f"ERROR: publish gate refused {len(gate_refusals)} bucket(s): "
            f"{', '.join(sorted(gate_refusals))}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == '__main__':
    main()
