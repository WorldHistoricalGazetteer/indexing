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
from processing.settings import (
    DATA_DIR,
    GEOM_STORE_DIR,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)


TILES_OUTPUT_DIR = Path(DATA_DIR) / 'tiles'

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
    "chgis", "clio", "dgsd", "dp", "gb", "gn", "iv", "nl",
    "pl", "po", "tgn", "tm", "un", "wd",
)

# Prefix used by per-WHG-dataset buckets. The full bucket name is
# ``f"{_WHG_BUCKET_PREFIX}{dataset_sub_id}"`` (e.g. ``whg-1234``). The set of
# WHG buckets is enumerated at submit time from the staged sidecar at
# ``STAGED_BASE_DIR/_aggregates/whg.datasets.json``.
_WHG_BUCKET_PREFIX = "whg-"

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

    Order: fixed buckets first (preserves historical Slurm task ordering), then
    per-namespace buckets, then per-WHG-dataset buckets in the order they
    appear in the sidecar. The ``manifest`` argument is accepted for forward
    compatibility but currently unused — the submitter applies its own
    eligibility filters via ``_required_stage_for``.
    """
    buckets: list[str] = list(_FIXED_BUCKETS)
    buckets.extend(_PER_NAMESPACE_BUCKETS)
    for sub_id in _load_whg_dataset_sub_ids():
        buckets.append(f"{_WHG_BUCKET_PREFIX}{sub_id}")
    return buckets


def _is_known_bucket(bucket: str) -> bool:
    return (
        bucket in _FIXED_BUCKETS
        or bucket in _PER_NAMESPACE_BUCKETS
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
):
    """Generate .mbtiles from GeoJSON Lines file using tippecanoe.

    ``minzoom`` defaults to 2 because at z0/z1 a single tile must hold
    the entire world's boundary geometry, which forces tippecanoe into
    a multi-hour sparsification loop on dense corpora. Use a smaller
    minzoom (0 or 1) ONLY for sparse subsets — the band-aware caller in
    ``generate_tiles_from_staged`` handles this safely by partitioning
    features per band before calling here.
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
        '--coalesce-densest-as-needed',
        # NOTE: ``--extend-zooms-if-still-dropping`` was tried and removed.
        # On dense corpora tippecanoe kept extending past z10 into z12+ in
        # a futile attempt to fit every feature, blowing past 24 h Slurm
        # walls. Admin boundaries don't need sub-z10 detail — at z10 each
        # tile is ~40 km, well above country/state/district line
        # resolution — so the densest-coalesce behaviour alone produces
        # the right size/quality tradeoff.
        '--no-tile-compression',
        '--read-parallel',
        str(geojsonl_path),
    ]

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
        TILESERVER_TILES_DIR, TILESERVER_PROXY_RSYNC,
    )

    proxy_host = proxy_host or TILESERVER_PROXY
    remote_user = remote_user or TILESERVER_USER
    remote_host = remote_host or TILESERVER_HOST
    remote_dir = remote_dir or TILESERVER_TILES_DIR
    if proxy_rsync is None:
        proxy_rsync = TILESERVER_PROXY_RSYNC

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
                "--info=stats1", str(mbtiles_path), target,
            ]
        else:
            cmd = ["scp", "-p", "-B", str(mbtiles_path), target]

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
    settle_seconds: int = 5,
) -> bool:
    """Restart the TileServer GL services so newly-deployed mbtiles are picked up.

    Per the user's contract (2026-05-03), this is **only ever called once
    per rebuild**, after every gazetteer's mbtiles has been pushed AND the
    push was verified. Per-bucket tile-gen tasks must NOT call this.

    The forever-service init scripts on the tileserver host (whg-tileboss)
    don't cooperate with ``systemctl restart``: the init script loses PID
    tracking and the long-lived node child keeps running with the stale
    config; successive restarts just spawn zombie forever monitors. So
    this helper:

    1. ``pkill -9 -f <unit>`` on the tileserver to kill ALL forever
       monitors AND the wrapped node children for each service.
    2. Sleeps ``settle_seconds`` so the OS releases sockets/pids.
    3. ``systemctl restart`` (or ``start``) to spawn ONE clean instance
       per service.
    4. Verifies a single forever monitor + single child remain.

    Returns True on success, False on any step failure (does not raise —
    caller decides). See ``feedback_tileserver_restart.md`` memory for the
    history of the gotcha that drove this design.
    """
    from processing.settings import (
        TILESERVER_PROXY, TILESERVER_HOST, TILESERVER_USER, TILESERVER_SERVICES,
    )

    proxy_host = proxy_host or TILESERVER_PROXY
    remote_user = remote_user or TILESERVER_USER
    remote_host = remote_host or TILESERVER_HOST
    services = services or list(TILESERVER_SERVICES)

    print(
        f"\nRestarting tileserver services: {services} on "
        f"{remote_user}@{remote_host} (pkill + restart)"
    )

    # Map .service unit name → process pattern to pkill on the worker
    # (forever-service wraps these binaries). Chosen to match both the
    # forever monitor process and the wrapped node child.
    pkill_patterns = {
        "tileserver-gl-light.service": "tileserver-gl-light",
        "tiler.service":               "/srv/tiler/tiler.js",
    }

    # Build the chained remote command: kill each service, sleep, then
    # restart each unit, then enumerate to confirm exactly one
    # forever-monitor + one worker per service.
    pkill_steps = " && ".join(
        f"sudo pkill -9 -f {pkill_patterns.get(svc, svc.replace('.service', ''))} || true"
        for svc in services
    )
    restart_steps = " && ".join(
        f"sudo systemctl restart {svc}" for svc in services
    )
    verify_steps = "; ".join(
        f"echo {svc!r}: $(pgrep -cf {pkill_patterns.get(svc, svc.replace('.service', ''))})"
        for svc in services
    )
    remote_cmd = (
        f"{pkill_steps}; sleep {settle_seconds}; "
        f"{restart_steps}; sleep {settle_seconds}; {verify_steps}"
    )

    if via_proxy:
        cmd = [
            "ssh", "-o", "BatchMode=yes", proxy_host,
            f'ssh -o BatchMode=yes {remote_user}@{remote_host} "{remote_cmd}"',
        ]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes",
               f"{remote_user}@{remote_host}", remote_cmd]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"  ✗ restart timed out after {timeout}s")
        return False

    if result.returncode != 0:
        print(
            f"  ✗ restart failed (rc={result.returncode})\n"
            f"    stderr: {result.stderr.strip()[:500]}"
        )
        return False

    # Per-service process counts came back on stdout in the form
    # ``'tileserver-gl-light.service': 2``. Expect exactly 2 (monitor +
    # child) per service for a healthy outcome. Anything else is
    # noteworthy but not strictly fatal — flag it.
    print("  ✓ tileserver services restarted; verification:")
    healthy = True
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        print(f"    {line}")
        # Lines look like "'tileserver-gl-light.service': 2"
        if line.endswith(": 2"):
            continue
        # A 0 means nothing came back up — that's bad
        if line.endswith(": 0"):
            healthy = False
    if not healthy:
        print(
            "    ⚠ at least one service has zero processes — restart probably failed"
        )
    return healthy
    return False


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

    ccodes = doc.get("ccodes") or []
    if ccodes:
        primary_cc = ccodes[0]
        local_lang = COUNTRY_LOCAL_LANG.get(primary_cc)
        if local_lang and local_lang in names_by_lang:
            props["name_local"] = names_by_lang[local_lang]
    if "name_local" not in props and "und" in names_by_lang:
        props["name_local"] = names_by_lang["und"]

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
) -> dict[str, int]:
    """Stream every contributing namespace's docs into one bucket output file.

    Truncates the output file once at the start so reruns are clean. Returns a
    breakdown of feature counts per contributing namespace.
    """
    contributors = _bucket_contributors(bucket)
    if not contributors:
        return {}

    require_boundary = bucket in _FIXED_BUCKETS
    geojsonl_path.write_bytes(b"")
    written: dict[str, int] = defaultdict(int)

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
                fh.write(orjson.dumps(feature))
                fh.write(b"\n")
                written[ns] += 1

    return dict(written)


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
                print(f"\nStreaming bucket '{bucket}' (single-band) from {_bucket_contributors(bucket)} ...")
                written = _stream_bucket(bucket, reader, geojsonl_path=geojsonl_path)
                bucket_counts[bucket] = sum(written.values())
                for ns, n in written.items():
                    per_namespace_totals[ns] += n
                    print(f"  {ns} → {bucket}: {n:,} features")
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
                if generate_tileset(geojsonl, mbtiles, bucket, description):
                    tilesets_generated.append(mbtiles)
                    # Per-bucket auto-push to the tileserver. Routes via the
                    # Pitt VM proxy because CRC compute nodes have no SSH key
                    # for the tileserver. Push failure is non-fatal here —
                    # the .mbtiles is still on /ix1 for a later catch-up
                    # push and the pipeline can continue. The eventual
                    # tileserver service restart is the user's manual step
                    # and gates on every bucket having pushed (see
                    # ``push_failures`` below).
                    if deploy:
                        if not push_mbtiles_to_tileserver(mbtiles):
                            push_failures.append(bucket)
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
