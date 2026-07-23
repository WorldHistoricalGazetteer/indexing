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
from processing.gazetteer_temporal_extent import doc_temporal_range
from processing.settings import (
    DATA_DIR,
    GEOM_STORE_DIR,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)


TILES_OUTPUT_DIR = Path(DATA_DIR) / 'tiles'

# Per-feature temporal sentinels for the Atlas client-side date filter
# (place#131). The whg3 map filter is interval-overlap:
#   ['all', ['has','start'], ['<=',['get','start'],toYear], ['>=',['get','end'],fromYear]]
# It reads *both* ``start`` and ``end`` on every dated feature, so open-ended
# spans can't leave a bound absent (a missing ``end`` would wrongly hide every
# still-current place). We therefore emit a sentinel for the open side:
#   ongoing  (start, no end)  -> end   = TILE_OPEN_END_YEAR   (always >= fromYear)
#   open-start (end, no start)-> start = TILE_OPEN_START_YEAR (always <= toYear)
#   undated  (no start/end)   -> omit both; the client admits these only in
#                               "+ undated" mode via ['!',['has','start']].
# This mirrors the gateway's temporal-overlap + ``undated`` semantics
# (``gateway/es_helpers.py``). Fixed (not build-year) so tiles stay
# reproducible; the values only need to fall outside any real query window.
TILE_OPEN_END_YEAR = 9999
TILE_OPEN_START_YEAR = -9999

# Low-zoom COVERAGE FOOTPRINT for polygon gazetteers (place#140). A polygon-only
# gazetteer renders a deceptively sparse scatter of tiny fills at low zoom even
# when it has tens of thousands of features. (An earlier cut fed the Atlas
# ``_heat`` layer with polygon centroid points, but that reads as a misleading
# sparse dot field — see the reopened #140.) Instead, for polygon-*dominant*
# buckets we emit ONE **dissolved** (unary_union) footprint polygon, tagged
# ``coverage: 1`` and capped at ``_COVERAGE_MAXZOOM``, so the Atlas can style it
# as a solid "this gazetteer covers this region" fill at low zoom; the real
# boundaries are pinned to ``_BOUNDARY_MINZOOM`` and take over on zoom-in. The two
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
        ]
    else:
        cmd += ['--coalesce-densest-as-needed']
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
        # Carry the per-feature temporal range onto the surviving cluster point
        # (place#131) so low-zoom clusters date-filter too, not just individual
        # features. ``start:min``/``end:max`` widen to the union of members'
        # spans (a cluster shows if *any* member overlaps the window).
        cmd += [
            '--accumulate-attribute', 'start:min',
            '--accumulate-attribute', 'end:max',
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

    Prefers rsync (with ``--partial --partial-dir=.tmp`` so an interrupted
    transfer leaves the existing destination file intact and the partial
    in a sidecar dir; the next run resumes), but falls back to ``scp -p``
    if rsync isn't reachable (settings.TILESERVER_PROXY_RSYNC empty). On
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
            # remote shell. ``--partial`` + ``--partial-dir=.tmp``: an
            # interrupted transfer leaves the existing target file
            # intact and parks the partial in ``<dest>/.tmp/<name>``;
            # the next run resumes from there. ``--info=stats1``: one
            # tidy summary line at the end (no progress noise that
            # would flood Slurm logs).
            remote_cmd = (
                f"{proxy_rsync} -a --partial --partial-dir=.tmp "
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
            cmd = [
                "rsync", "-a", "--partial", "--partial-dir=.tmp",
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
    print(f"\nDeploying {len(mbtiles_paths)} tilesets via Pitt-VM proxy …")
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
    """Per-feature ``start``/``end`` props for the Atlas date filter (place#131).

    Uses the shared :func:`doc_temporal_range` so the map's per-feature filter
    and the registry ``temporal_extent`` derive from identical logic. Returns an
    empty dict for undated features (both bounds omitted → client shows them
    only in "+ undated" mode); otherwise fills the open side with a sentinel so
    both ``start`` and ``end`` are always present on a dated feature. See
    ``TILE_OPEN_END_YEAR`` / ``TILE_OPEN_START_YEAR``.
    """
    start, end = doc_temporal_range(doc, namespace)
    if start is None and end is None:
        return {}
    return {
        "start": start if start is not None else TILE_OPEN_START_YEAR,
        "end": end if end is not None else TILE_OPEN_END_YEAR,
    }


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
) -> dict[str, Any] | None:
    """Build a tippecanoe-ready Feature from a staged place doc.

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

    full_geom: dict[str, Any] | None = None
    geom_ref = geom_entry.get("geom_ref")
    if isinstance(geom_ref, str) and geom_ref:
        full_geom = reader.get(geom_ref)
    elif geom_entry.get("has_geom"):
        # Authority scripts emit JSONL via ``write_staged_place_doc`` which
        # bypasses ``_augment_doc_for_stage`` — so ``geom_ref`` is absent on
        # extracted docs even when the geom store has the entry. Synthesize
        # the canonical key (``"{place_id}_{idx}"``) when ``has_geom`` is set,
        # matching ``stage_writers._augment_doc_for_stage``.
        idx = geom_entry.get("geometry_index", 0)
        full_geom = reader.get(f"{place_id}_{idx}")

    if not full_geom and not require_boundary:
        # WHG-computed approximation polygons (e.g. ottgaz admin hulls) live in
        # the inline ``hull`` field with ``has_geom=False`` — they are kept OUT
        # of the authoritative geom store. Render them as polygons; their
        # ``source``/``approximation`` provenance flags mark them as approximate.
        hull = geom_entry.get("hull")
        if isinstance(hull, dict) and hull.get("type") in ("Polygon", "MultiPolygon"):
            full_geom = hull

    if not full_geom and not require_boundary:
        rp = geom_entry.get("repr_point")
        if isinstance(rp, dict) and "lon" in rp and "lat" in rp:
            full_geom = {
                "type": "Point",
                "coordinates": [rp["lon"], rp["lat"]],
            }

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


def _accumulate_coverage(geom_json, sink: list) -> None:
    """Add a feature geometry's polygonal parts to the coverage-union ``sink``.

    ``sink`` is a list of shapely polygons later dissolved via ``unary_union``.
    Invalid rings are repaired with ``make_valid`` so the union can't choke on a
    self-intersecting source polygon. Non-polygon geometries contribute nothing.
    """
    if not isinstance(geom_json, dict):
        return
    if geom_json.get("type") not in ("Polygon", "MultiPolygon", "GeometryCollection"):
        return
    try:
        from shapely.geometry import shape
        from shapely.validation import make_valid
        g = shape(geom_json)
        if not g.is_valid:
            g = make_valid(g)
        sink.extend(_polygonal_parts(g))
    except Exception:
        pass


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
        return {
            "type": "Feature",
            "properties": {
                "coverage": 1,
                "namespace": namespace,
                "tippecanoe:maxzoom": _COVERAGE_MAXZOOM,
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


def _stream_bucket(
    bucket: str,
    reader: GeomStoreReader,
    *,
    geojsonl_path: Path,
    collect_coverage: bool = False,
) -> tuple[dict[str, int], dict[str, int], list]:
    """Stream every contributing namespace's docs into one bucket output file.

    Truncates the output file once at the start so reruns are clean. Returns
    ``(written, geom_counts, coverage_geoms)``:

    * ``written`` — ``{namespace: real-feature count}`` (metrics / wall-time).
    * ``geom_counts`` — ``{"polygon": n, "point": n}`` used by the caller to pick
      the tiling mode (polygon-dominant → coverage footprint, else point heatmap).
    * ``coverage_geoms`` — shapely polygons accumulated for the dissolved footprint
      when ``collect_coverage`` is set (place#140), else empty.

    Every Polygon/MultiPolygon feature is pinned to ``tippecanoe:minzoom =
    _BOUNDARY_MINZOOM`` so real boundaries appear only from the crossover up; the
    low zooms are owned by the dissolved coverage footprint (a separate pass). This
    pin is harmless for point-dominant buckets (their few polygons simply start at
    z8, and no footprint is emitted for them).
    """
    contributors = _bucket_contributors(bucket)
    if not contributors:
        return {}, {"polygon": 0, "point": 0}, []

    require_boundary = bucket in _FIXED_BUCKETS
    geojsonl_path.write_bytes(b"")
    written: dict[str, int] = defaultdict(int)
    counts = {"polygon": 0, "point": 0}
    coverage_geoms: list = []

    with open(geojsonl_path, "ab") as fh:
        for namespace in contributors:
            src = _staged_namespace_source(namespace)
            if src is None:
                continue
            for doc in _iter_staged_docs(src):
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
                )
                if feature is None:
                    continue
                geom = feature.get("geometry") or {}
                is_poly = geom.get("type") in ("Polygon", "MultiPolygon", "GeometryCollection")
                if is_poly:
                    counts["polygon"] += 1
                    # Pin boundaries above the crossover; footprint owns z0-7.
                    feature["properties"]["tippecanoe:minzoom"] = _BOUNDARY_MINZOOM
                    if collect_coverage:
                        _accumulate_coverage(geom, coverage_geoms)
                else:
                    counts["point"] += 1
                fh.write(orjson.dumps(feature))
                fh.write(b"\n")
                written[ns] += 1

    return dict(written), counts, coverage_geoms


def _stream_bucket_banded(
    bucket: str,
    reader: GeomStoreReader,
    bands: list,                       # list[Band] from tilegen_bands
    *,
    out_dir: Path,
) -> tuple[dict[str, Path], dict[str, dict[str, int]]]:
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

    handles = {name: open(p, "ab") for name, p in band_paths.items()}
    try:
        for namespace in contributors:
            src = _staged_namespace_source(namespace)
            if src is None:
                continue
            for doc in _iter_staged_docs(src):
                place_id = doc.get("place_id") or ""
                ns = place_id.split(":", 1)[0] if ":" in place_id else namespace
                matches, is_misc = _doc_belongs_to_bucket(doc, bucket, ns)
                if not matches:
                    continue
                feature = _build_staged_feature(
                    doc, ns, reader,
                    misc=is_misc, require_boundary=require_boundary,
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
    finally:
        for h in handles.values():
            h.close()

    if unmatched:
        print(f"  ⚠ {unmatched:,} features matched no band (dropped)")

    # Drop empty bands from band_paths so callers don't waste tippecanoe
    # invocations on them.
    band_paths = {
        name: path for name, path in band_paths.items()
        if path.exists() and path.stat().st_size > 0
    }
    return band_paths, {name: dict(c) for name, c in band_counts.items()}


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
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr)
    elapsed = time.time() - start
    if result.returncode != 0:
        print(f"  ✗ tile-join failed (rc={result.returncode})")
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
    bucket_geojsonl: dict[str, Path] = {}                 # legacy single-band path
    bucket_coverage_geojsonl: dict[str, Path] = {}        # place#140 dissolved footprint
    bucket_uses_bands: dict[str, bool] = {}

    started = datetime.now(timezone.utc)
    try:
        for bucket in selected:
            bands = bands_per_bucket.get(bucket)
            if bands and len(bands) > 1:
                bucket_uses_bands[bucket] = True
                print(f"\nStreaming bucket '{bucket}' (banded: {[b.name for b in bands]}) "
                      f"from {_bucket_contributors(bucket)} ...")
                band_paths, band_counts = _stream_bucket_banded(
                    bucket, reader, bands, out_dir=out_dir,
                )
                bucket_band_paths[bucket] = band_paths
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
                geojsonl_path = out_dir / f"{bucket}.geojsonl"
                bucket_geojsonl[bucket] = geojsonl_path
                # place#140: for polygon-bearing buckets we accumulate a dissolved
                # low-zoom COVERAGE FOOTPRINT (a separate pass, tile-join'd back
                # into the one source-layer). Context-overlay buckets (point-only
                # capitals) and the banded fixed admin buckets are excluded — the
                # latter go through ``_stream_bucket_banded``. Whether a footprint
                # is actually emitted is decided AFTER streaming, from the
                # polygon/point mix (see the tippecanoe stage).
                collect_cov = bucket not in _CONTEXT_OVERLAY_BUCKETS
                print(f"\nStreaming bucket '{bucket}' (single-band) from {_bucket_contributors(bucket)} ...")
                written, geom_counts, cov_geoms = _stream_bucket(
                    bucket, reader, geojsonl_path=geojsonl_path,
                    collect_coverage=collect_cov,
                )
                bucket_counts[bucket] = sum(written.values())
                for ns, n in written.items():
                    per_namespace_totals[ns] += n
                    print(f"  {ns} → {bucket}: {n:,} features "
                          f"(poly={geom_counts['polygon']:,} point={geom_counts['point']:,})")
                # Polygon-dominant → emit the dissolved coverage footprint.
                if (collect_cov and cov_geoms
                        and geom_counts["polygon"] > geom_counts["point"]):
                    ns0 = _bucket_contributors(bucket)[0] if _bucket_contributors(bucket) else bucket
                    cov_feature = _coverage_feature(cov_geoms, ns0)
                    if cov_feature is not None:
                        cov_path = out_dir / f"{bucket}.coverage.geojsonl"
                        cov_path.write_bytes(orjson.dumps(cov_feature) + b"\n")
                        bucket_coverage_geojsonl[bucket] = cov_path
                        print(f"  + dissolved coverage footprint (place#140) "
                              f"from {geom_counts['polygon']:,} polygons")
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

                if band_mbtiles and tile_join(band_mbtiles, mbtiles, layer_name=bucket):
                    tilesets_generated.append(mbtiles)
                    if deploy and not push_mbtiles_to_tileserver(mbtiles):
                        push_failures.append(bucket)
                else:
                    bucket_failures.append(bucket)
            else:
                geojsonl = bucket_geojsonl[bucket]
                coverage_geojsonl = bucket_coverage_geojsonl.get(bucket)
                has_coverage = bool(
                    coverage_geojsonl and coverage_geojsonl.exists()
                    and coverage_geojsonl.stat().st_size > 0
                )
                # Context-overlay buckets carry their own per-bucket config (zoom
                # range, no clustering) — pre-filtered small subsets (e.g. world
                # capitals) that don't want density coalescing.
                ctx_cfg = _CONTEXT_OVERLAY_BUCKETS.get(bucket)
                if ctx_cfg is not None:
                    tile_minzoom = ctx_cfg["minzoom"]
                    tile_maxzoom = ctx_cfg["maxzoom"]
                    tile_cluster = ctx_cfg["cluster_points"]
                    tile_description = ctx_cfg.get("description") or description
                else:
                    tile_minzoom = 0
                    tile_maxzoom = 10
                    # Point-dominant buckets keep the point heatmap (clustering);
                    # polygon-dominant buckets (a coverage footprint was emitted)
                    # instead use the no-drop BASE pass so every pinned boundary
                    # survives from z8 up (place#140).
                    tile_cluster = not has_coverage
                    tile_description = description

                # place#140: polygon-dominant bucket → tile the pinned real
                # features to a BASE mbtiles (no-drop) and the dissolved footprint
                # to its OWN mbtiles (maxzoom 7), then ``tile-join`` both into the
                # single bucket source-layer. Point buckets skip this entirely.
                base_mbtiles = (out_dir / f"{bucket}.base.mbtiles") if has_coverage else mbtiles

                if generate_tileset(
                    geojsonl, base_mbtiles, bucket, tile_description,
                    minzoom=tile_minzoom,
                    maxzoom=tile_maxzoom,
                    cluster_points=tile_cluster,
                    preserve_all=has_coverage,
                ):
                    built = True
                    if has_coverage:
                        cov_mbtiles = out_dir / f"{bucket}.coverage.mbtiles"
                        # Single dissolved polygon, capped at z7 by its per-feature
                        # tippecanoe:maxzoom; render it down to z0.
                        if generate_tileset(
                            coverage_geojsonl, cov_mbtiles, bucket, tile_description,
                            minzoom=0, maxzoom=_COVERAGE_MAXZOOM,
                            cluster_points=False,
                        ):
                            built = tile_join(
                                [base_mbtiles, cov_mbtiles], mbtiles,
                                layer_name=bucket,
                            )
                        else:
                            # Footprint failed — keep the boundaries; the single-
                            # input join just renames base → final.
                            print(f"  ⚠ {bucket}: coverage pass failed — "
                                  "deploying boundaries without footprint")
                            built = tile_join([base_mbtiles], mbtiles, layer_name=bucket)

                    if built:
                        tilesets_generated.append(mbtiles)
                        # Per-bucket auto-push to the tileserver. Routes via the
                        # Pitt VM proxy because CRC compute nodes have no SSH key
                        # for the tileserver. Push failure is non-fatal here —
                        # the .mbtiles is still on /ix1 for a later catch-up
                        # push and the pipeline can continue. The eventual
                        # tileserver service restart is the user's manual step
                        # and gates on every bucket having pushed (see
                        # ``push_failures`` below).
                        if deploy and not push_mbtiles_to_tileserver(mbtiles):
                            push_failures.append(bucket)
                    else:
                        bucket_failures.append(bucket)
                else:
                    # Empty GeoJSONL ("nothing to tile") is benign;
                    # tippecanoe exiting non-zero on a non-empty input is
                    # a real failure (typically OOM) and must not be
                    # recorded as completed — otherwise wall-time
                    # estimators pick up the partial run and undersize
                    # the next attempt's Slurm budget.
                    if geojsonl.exists() and geojsonl.stat().st_size > 0:
                        bucket_failures.append(bucket)

    # Distinguish per-namespace status: if any bucket the namespace contributes
    # to failed tippecanoe, mark its tiles stage failed; otherwise completed.
    def _ns_buckets(ns: str) -> list[str]:
        return [b for b in selected if ns in _bucket_contributors(b)]

    def _ns_status(ns: str) -> str:
        return "failed" if any(b in bucket_failures for b in _ns_buckets(ns)) else "completed"

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

    return tilesets_generated


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

    generate_tiles_from_staged(
        buckets=args.bucket,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        deploy=args.deploy,
        run_id=args.run_id,
        manifest_path=manifest_path,
        skip_tippecanoe=args.skip_tippecanoe,
    )


if __name__ == '__main__':
    main()
