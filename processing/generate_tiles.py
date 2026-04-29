# processing/generate_tiles.py

"""
Staged Tileset Generator (Batch 10).

Reads boundary-qualifying records from the staged snapshot pipeline
(``final/`` → ``h3_merged/`` → ``boundary_merged/`` → ``extract/``) and pulls
full polygon geometries from the external geometry store. No Elasticsearch
dependency.

The output layout matches the master plan:

    osm_admin.mbtiles   ohm_admin.mbtiles   osm_misc.mbtiles  (mixed OSM/OHM)
    po.mbtiles          clio.mbtiles        nl.mbtiles

Tile generation is **bucket-driven**: each bucket has a fixed list of
contributing namespaces (see ``TILE_BUCKETS``) and a single owning writer to
avoid file-write contention on the mixed ``osm_misc`` bucket.

Multilingual labels come from ``toponyms[]`` (``toponym_id`` in
``name@lang`` format).

Usage::

    python -m processing.generate_tiles
    python -m processing.generate_tiles --bucket osm_admin --bucket ohm_admin
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

# Bucket → list of input namespaces that contribute features. Each bucket has
# a single owning Slurm task to avoid file-write contention; the mixed
# ``osm_misc`` bucket is owned by one task that streams *both* OSM and OHM
# misc-boundary records into a single file.
TILE_BUCKETS: dict[str, tuple[str, ...]] = {
    "osm_admin": ("osm",),
    "ohm_admin": ("ohm",),
    "osm_misc":  ("osm", "ohm"),
    "po":        ("po",),
    "clio":      ("clio",),
    "nl":        ("nl",),
}


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


def generate_tileset(geojsonl_path, mbtiles_path, layer_name, description=''):
    """Generate .mbtiles from GeoJSON Lines file using tippecanoe."""
    tippecanoe = shutil.which('tippecanoe')
    if not tippecanoe:
        print("  WARNING: tippecanoe not found — skipping .mbtiles generation")
        return False

    if not geojsonl_path.exists() or geojsonl_path.stat().st_size == 0:
        print("  WARNING: GeoJSON Lines file is empty — skipping")
        return False

    size_mb = geojsonl_path.stat().st_size / 1e6
    print(f"  Generating {mbtiles_path.name} from {size_mb:.1f} MB ...")

    cmd = [
        tippecanoe,
        '--output', str(mbtiles_path),
        '--force',
        '--layer', layer_name,
        '--name', f'WHG {layer_name}',
        '--description', description or f'WHG {layer_name} boundaries',
        '--minimum-zoom', '0',
        '--maximum-zoom', '10',
        '--simplification', '10',
        '--detect-shared-borders',
        '--coalesce-densest-as-needed',
        '--extend-zooms-if-still-dropping',
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


def deploy_tilesets(mbtiles_paths, remote_host='134.209.177.234',
                    remote_user='whgadmin',
                    remote_dir='/data/tileserver/mbtiles'):
    """Deploy .mbtiles to TileServer GL light via rsync."""
    print(f"\nDeploying {len(mbtiles_paths)} tilesets to {remote_user}@{remote_host} ...")

    for path in mbtiles_paths:
        if not path.exists():
            continue
        target = f"{remote_user}@{remote_host}:{remote_dir}/{path.name}"
        print(f"  rsync {path.name} → {target}")
        result = subprocess.run(
            ['rsync', '-az', '--progress', str(path), target],
            stdout=sys.stdout, stderr=sys.stderr,
        )
        if result.returncode != 0:
            print(f"  ✗ rsync failed for {path.name}")

    print("  Deploy complete")


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


def _build_staged_feature(
    doc: dict[str, Any],
    namespace: str,
    reader: GeomStoreReader,
    *,
    misc: bool = False,
) -> dict[str, Any] | None:
    """Build a tippecanoe-ready Feature from a staged place doc.

    Returns ``None`` when the doc has no boundary value, no geometries, or no
    full polygon retrievable from the geom store. The geom store is the only
    accepted source for tile geometry — staged hulls (coarser, simplified
    bounding shapes) are not used.

    ``misc=True`` re-encodes the feature ID under the OSM/OHM 1-bit
    discrimination scheme used for the mixed ``osm_misc`` tileset.
    """
    place_id = doc.get("place_id")
    if not place_id:
        return None
    boundary = doc.get("boundary")
    if not boundary:
        return None

    geometries = doc.get("geometries") or []
    if not geometries:
        return None
    geom_entry = geometries[0]
    if not isinstance(geom_entry, dict):
        return None
    geom_ref = geom_entry.get("geom_ref")
    if not isinstance(geom_ref, str) or not geom_ref:
        # Authority scripts emit JSONL via ``write_staged_place_doc`` which
        # bypasses ``_augment_doc_for_stage`` — so ``geom_ref`` is absent on
        # extracted docs even when the geom store has the entry. Synthesize
        # the canonical key (``"{place_id}_{idx}"``) when ``has_geom`` is set,
        # matching ``stage_writers._augment_doc_for_stage``.
        if not geom_entry.get("has_geom"):
            return None
        idx = geom_entry.get("geometry_index", 0)
        geom_ref = f"{place_id}_{idx}"
    full_geom = reader.get(geom_ref)
    if not full_geom:
        return None

    props: dict[str, Any] = {
        "place_id": place_id,
        "boundary": boundary,
        "namespace": namespace,
    }

    if _is_admin_level(boundary):
        minzoom = ADMIN_LEVEL_MINZOOM.get(boundary, 0)
    else:
        minzoom = 3
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

    A doc qualifies for ``bucket`` when its ``boundary`` field falls into the
    bucket's category. ``is_misc`` toggles the alternate feature-id encoding
    used by ``osm_misc``.
    """
    boundary = doc.get("boundary")
    if not boundary:
        return False, False

    if bucket in ("osm_admin", "ohm_admin"):
        if namespace not in ("osm", "ohm"):
            return False, False
        return _is_admin_level(boundary), False
    if bucket == "osm_misc":
        if namespace not in ("osm", "ohm"):
            return False, False
        return _is_misc_boundary(boundary), True
    if bucket in ("po", "clio", "nl"):
        return namespace == bucket, False
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
    contributors = TILE_BUCKETS.get(bucket, ())
    if not contributors:
        return {}

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
                feature = _build_staged_feature(doc, ns, reader, misc=is_misc)
                if feature is None:
                    continue
                fh.write(orjson.dumps(feature))
                fh.write(b"\n")
                written[ns] += 1

    return dict(written)


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

    Each *bucket* (``osm_admin``, ``ohm_admin``, ``osm_misc``, ``po``,
    ``clio``, ``nl``) has a fixed list of contributing namespaces (see
    ``TILE_BUCKETS``); a single call writes one tileset per bucket. This
    bucket-driven design keeps each output file owned by exactly one writer
    so concurrent Slurm tasks (one task per bucket) never race on the same
    GeoJSONL file.

    For each contributing namespace the function streams the most-enriched
    staged snapshot (``final/`` → ``h3_merged/`` → ``boundary_merged/`` →
    ``extract/``) and fetches full polygon geometries from the geom store.
    The geom store is required: if a doc lacks a ``geom_ref`` or the store
    cannot resolve it, that doc is dropped (no hull approximation).

    Args:
        buckets: Restrict to these tile buckets (default: all of
            ``TILE_BUCKETS``). Unknown values are silently dropped.
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

    selected = [b for b in (buckets or list(TILE_BUCKETS)) if b in TILE_BUCKETS]
    if not selected:
        print("No tile buckets selected.")
        return []

    contributing_namespaces: set[str] = set()
    for bucket in selected:
        contributing_namespaces.update(TILE_BUCKETS[bucket])

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

    bucket_counts: dict[str, int] = {}
    per_namespace_totals: dict[str, int] = defaultdict(int)
    bucket_paths: dict[str, Path] = {}

    started = datetime.now(timezone.utc)
    try:
        for bucket in selected:
            geojsonl_path = out_dir / f"{bucket}.geojsonl"
            bucket_paths[bucket] = geojsonl_path
            print(f"\nStreaming bucket '{bucket}' from {TILE_BUCKETS[bucket]} ...")
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
    if skip_tippecanoe:
        print("\n--skip-tippecanoe specified; GeoJSONL written but no .mbtiles produced.")
    else:
        for bucket in selected:
            geojsonl = bucket_paths[bucket]
            mbtiles = out_dir / f"{bucket}.mbtiles"
            description = f"WHG {bucket}"
            if generate_tileset(geojsonl, mbtiles, bucket, description):
                tilesets_generated.append(mbtiles)

    if manifest_path is not None and manifest_path.exists():
        for ns in contributing_namespaces:
            metrics = {
                "features_written": per_namespace_totals.get(ns, 0),
                "buckets": [b for b in selected if ns in TILE_BUCKETS[b]],
                "wall_seconds": round(wall_seconds, 1),
            }
            update_namespace_stage_status(
                manifest_path, ns, "tiles", "completed", metrics=metrics
            )
    if run_id:
        for ns in contributing_namespaces:
            metrics = {
                "features_written": per_namespace_totals.get(ns, 0),
                "buckets": [b for b in selected if ns in TILE_BUCKETS[b]],
                "wall_seconds": round(wall_seconds, 1),
            }
            try:
                write_stage_event(
                    run_id=run_id,
                    namespace=ns,
                    script_id="tiles",
                    status="completed",
                    stage="tiles",
                    metrics=metrics,
                )
                write_runtime_history_event(
                    run_id=run_id,
                    event="tiles",
                    status="completed",
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
                    wall_seconds=wall_seconds, status="completed",
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
        deploy_tilesets(tilesets_generated)

    return tilesets_generated


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate .mbtiles tilesets from staged boundary places"
    )
    parser.add_argument('--bucket', '-b', action='append',
                        help='Restrict to tile bucket(s); pass multiple times. Defaults to all of '
                             f'{tuple(TILE_BUCKETS)}.')
    parser.add_argument('--output-dir', help='Output directory for tilesets')
    parser.add_argument('--deploy', action='store_true',
                        help='Deploy tilesets to TileServer GL via rsync')
    parser.add_argument('--run-id', help='Run ID for manifest updates')
    parser.add_argument('--manifest-path',
                        help='Run manifest path; if omitted derives from --run-id')
    parser.add_argument('--skip-tippecanoe', action='store_true',
                        help='Write only GeoJSONL files, do not invoke tippecanoe')
    args = parser.parse_args()

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
