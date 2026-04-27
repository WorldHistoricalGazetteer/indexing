#!/usr/bin/env python3
"""Per-namespace ccode enrichment (Batch 7).

For each non-UN namespace's H3-enriched staged snapshot, derive a list of
country codes (ISO 3166-1 alpha-2) from the spatial overlap with the staged
``un`` gazetteer's country geometries. Output is a JSONL patch consumed by
``processing/ccode_merge.py`` (Batch 5).

Two-phase approach:

1. **H3 pre-filter.** UN's per-geometry ``h3_cover`` (computed in Batch 6) is
   normalised to a single resolution and inverted into ``{cell: set[ccode]}``.
   Each place geometry's ``h3_cover`` cells are walked to that same resolution;
   the union of overlapping ccodes is the candidate set.
2. **Precise containment.** Each candidate ccode is verified against the UN
   country's full geometry (loaded lazily from the geometry store):
     - for places represented by a single point (``repr_point``): point-in-polygon;
     - for areas: any non-trivial intersection — Shapely ``intersects`` against the
       prepared geometry, with majority-overlap as the tie-break when an area
       straddles multiple countries.

The resulting ``ccodes`` list is **authoritative** — ``ccode_merge.py``
overwrites whatever was on the source document.

Output: ``{STAGED_BASE_DIR}/{namespace}/ccode/places.ccode.jsonl``
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import pyarrow.parquet as pq
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep

from processing.geom_store import GeomStoreReader
from processing.helpers import geojson_to_shapely
from processing.settings import (
    GEOM_STORE_DIR,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import (
    record_script_wall_time,
    write_runtime_history_event,
    write_stage_event,
)
from processing.staging_orchestrator import update_namespace_stage_status

try:
    import h3 as _h3
    _H3_AVAILABLE = True
except ImportError:
    _H3_AVAILABLE = False


# Resolution at which the UN H3 prefilter index is materialised. Trades index
# size against query cost: r4 ≈ 22 km hexagons gives a manageable global cell
# count (~290 k cells worldwide) while keeping per-place walk-up from r7 cheap.
PREFILTER_RESOLUTION = 4
SOURCE_LABEL = "un-h3-overlap"
UN_NAMESPACE = "un"


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _h3_merged_dir(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / namespace / "h3_merged"


def _iter_staged_docs(namespace: str) -> Iterator[dict[str, Any]]:
    src_dir = _h3_merged_dir(namespace)
    parquet_path = src_dir / "places.parquet"
    jsonl_path = src_dir / "places.jsonl"

    if parquet_path.exists():
        parquet = pq.ParquetFile(parquet_path)
        for batch in parquet.iter_batches(batch_size=2000):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row
        return

    if jsonl_path.exists():
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
        return

    raise FileNotFoundError(
        f"No H3-merged source found for namespace '{namespace}' in {src_dir}"
    )


# ---------------------------------------------------------------------------
# H3 prefilter
# ---------------------------------------------------------------------------


def _normalise_cells_to_resolution(
    cells: Iterable[str], target_res: int
) -> set[str]:
    """Walk an iterable of H3 cells to ``target_res``.

    Cells finer than ``target_res`` are walked up via ``cell_to_parent``.
    Cells coarser than ``target_res`` are descended to all children at the
    target resolution. Cells already at ``target_res`` are passed through.
    Malformed cells are silently skipped.
    """
    if not _H3_AVAILABLE:
        return set()

    out: set[str] = set()
    for cell in cells:
        if not isinstance(cell, str) or not cell:
            continue
        try:
            res = _h3.get_resolution(cell)
        except Exception:
            continue
        if res == target_res:
            out.add(cell)
        elif res > target_res:
            try:
                out.add(_h3.cell_to_parent(cell, target_res))
            except Exception:
                continue
        else:
            try:
                out.update(_h3.cell_to_children(cell, target_res))
            except Exception:
                continue
    return out


def build_un_prefilter(
    un_records: list[dict[str, Any]],
    *,
    target_res: int = PREFILTER_RESOLUTION,
) -> tuple[dict[str, set[str]], dict[str, list[dict[str, Any]]]]:
    """Build (cell→ccodes, ccode→[un_geom_metadata]) from UN h3_merged docs.

    Each UN doc carries ``ccodes=[<ISO_A2>]`` and one or more geometries with
    ``h3_cover``. We collapse all the country's geometries' coverage into a
    single set of cells at ``target_res`` and invert into a per-cell ccode
    lookup. The second return is a per-ccode list of geometry references
    (``geom_ref`` / ``repr_point`` / ``hull``) used for precise containment.
    """
    cell_to_ccodes: dict[str, set[str]] = {}
    ccode_to_geoms: dict[str, list[dict[str, Any]]] = {}

    for doc in un_records:
        ccodes = doc.get("ccodes") or []
        if not ccodes:
            continue
        ccode = ccodes[0]
        if not isinstance(ccode, str) or not ccode:
            continue

        place_id = doc.get("place_id")
        for idx, geom in enumerate(doc.get("geometries") or []):
            if not isinstance(geom, dict):
                continue
            cover = geom.get("h3_cover") or []
            if not cover:
                centroid = geom.get("h3_centroid")
                if centroid:
                    cover = [centroid]
            if not cover:
                continue

            normalised = _normalise_cells_to_resolution(cover, target_res)
            for cell in normalised:
                cell_to_ccodes.setdefault(cell, set()).add(ccode)

            ccode_to_geoms.setdefault(ccode, []).append(
                {
                    "place_id": place_id,
                    "geometry_index": geom.get("geometry_index", idx),
                    "geom_ref": geom.get("geom_ref"),
                    "repr_point": geom.get("repr_point"),
                    "hull": geom.get("hull"),
                    "bounds": geom.get("bounds"),
                }
            )

    return cell_to_ccodes, ccode_to_geoms


def candidate_ccodes_for_cells(
    place_cells: Iterable[str],
    cell_to_ccodes: dict[str, set[str]],
    *,
    target_res: int = PREFILTER_RESOLUTION,
) -> set[str]:
    """Return ccodes whose UN coverage may overlap the given place cells."""
    candidates: set[str] = set()
    normalised = _normalise_cells_to_resolution(place_cells, target_res)
    for cell in normalised:
        match = cell_to_ccodes.get(cell)
        if match:
            candidates.update(match)
    return candidates


# ---------------------------------------------------------------------------
# Precise containment
# ---------------------------------------------------------------------------


class _UnGeometryCache:
    """Lazy, LRU-backed loader of UN country geometries from the geom store."""

    def __init__(self, ccode_to_geoms: dict[str, list[dict[str, Any]]]):
        self._ccode_to_geoms = ccode_to_geoms
        self._reader: GeomStoreReader | None = None
        self._prepared_per_ccode: dict[str, list[BaseGeometry]] = {}

    def _ensure_reader(self) -> GeomStoreReader | None:
        if self._reader is not None:
            return self._reader
        try:
            self._reader = GeomStoreReader(GEOM_STORE_DIR)
        except FileNotFoundError:
            self._reader = None
        return self._reader

    def _load(self, ccode: str) -> list[BaseGeometry]:
        cached = self._prepared_per_ccode.get(ccode)
        if cached is not None:
            return cached

        geoms: list[BaseGeometry] = []
        reader = self._ensure_reader()
        for entry in self._ccode_to_geoms.get(ccode, []):
            geom_ref = entry.get("geom_ref")
            full = None
            if reader is not None and isinstance(geom_ref, str) and geom_ref:
                full = reader.get(geom_ref)
            # Fall back to the staged hull when the geom store is unavailable
            # or the key is missing. The hull is a coarser approximation but
            # still a closed polygon that supports point-in-polygon tests.
            if full is None:
                full = entry.get("hull")
            if not full:
                continue
            shp = geojson_to_shapely(full)
            if shp is not None and not shp.is_empty:
                geoms.append(shp)

        self._prepared_per_ccode[ccode] = geoms
        return geoms

    def geoms_for(self, ccode: str) -> list[BaseGeometry]:
        return self._load(ccode)


# ---------------------------------------------------------------------------
# Per-place enrichment
# ---------------------------------------------------------------------------


def _extract_place_h3_cells(doc: dict[str, Any]) -> list[str]:
    cells: list[str] = []
    for geom in doc.get("geometries") or []:
        if not isinstance(geom, dict):
            continue
        cover = geom.get("h3_cover")
        if isinstance(cover, list):
            cells.extend(c for c in cover if isinstance(c, str) and c)
        centroid = geom.get("h3_centroid")
        if isinstance(centroid, str) and centroid:
            cells.append(centroid)
    return cells


def _extract_place_geometry(doc: dict[str, Any], reader: GeomStoreReader | None) -> BaseGeometry | None:
    """Return one Shapely geometry for the place — full geom if available,
    otherwise the staged hull, otherwise the repr_point."""
    for idx, geom in enumerate(doc.get("geometries") or []):
        if not isinstance(geom, dict):
            continue
        geom_ref = geom.get("geom_ref")
        if reader is not None and isinstance(geom_ref, str) and geom_ref:
            full = reader.get(geom_ref)
            if full:
                shp = geojson_to_shapely(full)
                if shp is not None and not shp.is_empty:
                    return shp
        hull = geom.get("hull")
        if hull:
            shp = geojson_to_shapely(hull)
            if shp is not None and not shp.is_empty:
                return shp
        rp = geom.get("repr_point")
        if rp:
            try:
                return Point(float(rp["lon"]), float(rp["lat"]))
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _filter_by_containment(
    place_geom: BaseGeometry,
    candidate_ccodes: Iterable[str],
    un_cache: _UnGeometryCache,
) -> list[str]:
    """Filter candidate ccodes by precise spatial test against UN geometries.

    For points: ``intersects`` is sufficient (point-in-polygon).
    For areas: collect every ccode whose UN geometry intersects the place
    geometry. When more than one passes, sort by descending overlap area
    (majority-overlap tie-break) but keep all matches — a region that
    legitimately straddles a border should attest to all its countries.
    """
    is_point = place_geom.geom_type == "Point"
    matches: list[tuple[str, float]] = []

    for ccode in candidate_ccodes:
        for un_geom in un_cache.geoms_for(ccode):
            try:
                prepared = prep(un_geom)
                if not prepared.intersects(place_geom):
                    continue
                if is_point:
                    matches.append((ccode, 1.0))
                    break
                inter = un_geom.intersection(place_geom)
                if inter.is_empty:
                    continue
                area = inter.area
                if area <= 0:
                    # Touch-only intersection (shared border, zero area).
                    continue
                matches.append((ccode, area))
                break
            except Exception:
                continue

    if not matches:
        return []
    if is_point:
        return sorted({c for c, _ in matches})
    matches.sort(key=lambda t: t[1], reverse=True)
    return [c for c, _ in matches]


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def _load_un_records() -> list[dict[str, Any]]:
    return list(_iter_staged_docs(UN_NAMESPACE))


def run_ccode_enrichment(
    *,
    run_id: str,
    namespace: str,
    manifest_path: Path | None = None,
    slurm_job_id: str | None = None,
) -> dict[str, Any]:
    if namespace == UN_NAMESPACE:
        raise ValueError(
            "ccode enrichment is not applicable to the UN namespace itself"
        )
    if not _H3_AVAILABLE:
        raise RuntimeError("h3 library is required for ccode enrichment")

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(manifest_path, namespace, "ccode", "running")

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="ccode-enrichment",
        status="running",
        stage="ccode",
    )
    write_runtime_history_event(
        run_id=run_id,
        event="ccode_enrichment",
        status="running",
        namespace=namespace,
        stage="ccode",
        slurm_job_id=slurm_job_id,
    )

    started_at = datetime.now(timezone.utc)

    un_records = _load_un_records()
    cell_to_ccodes, ccode_to_geoms = build_un_prefilter(un_records)
    un_cache = _UnGeometryCache(ccode_to_geoms)

    try:
        place_reader: GeomStoreReader | None = GeomStoreReader(GEOM_STORE_DIR)
    except FileNotFoundError:
        place_reader = None

    out_dir = Path(STAGED_BASE_DIR) / namespace / "ccode"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "places.ccode.jsonl"

    docs_seen = 0
    docs_with_ccodes = 0
    docs_no_geom = 0
    docs_no_candidate = 0
    docs_no_match = 0

    with out_path.open("w", encoding="utf-8") as fh:
        for doc in _iter_staged_docs(namespace):
            docs_seen += 1
            place_id = doc.get("place_id")
            if not place_id:
                continue

            cells = _extract_place_h3_cells(doc)
            if not cells:
                docs_no_geom += 1
                continue

            candidates = candidate_ccodes_for_cells(cells, cell_to_ccodes)
            if not candidates:
                docs_no_candidate += 1
                continue

            place_geom = _extract_place_geometry(doc, place_reader)
            if place_geom is None:
                docs_no_geom += 1
                continue

            ccodes = _filter_by_containment(place_geom, candidates, un_cache)
            if not ccodes:
                docs_no_match += 1
                continue

            patch = {
                "place_id": place_id,
                "ccodes": ccodes,
                "source": SOURCE_LABEL,
            }
            fh.write(json.dumps(patch, ensure_ascii=True) + "\n")
            docs_with_ccodes += 1

    finished_at = datetime.now(timezone.utc)
    wall_seconds = (finished_at - started_at).total_seconds()

    metrics = {
        "docs_seen": docs_seen,
        "docs_with_ccodes": docs_with_ccodes,
        "docs_no_geom": docs_no_geom,
        "docs_no_candidate": docs_no_candidate,
        "docs_no_match": docs_no_match,
        "patch_path": str(out_path),
        "un_records": len(un_records),
        "un_prefilter_cells": len(cell_to_ccodes),
        "wall_seconds": round(wall_seconds, 1),
    }

    try:
        record_script_wall_time(
            namespace=namespace,
            script_id="ccode-enrichment",
            run_id=run_id,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            wall_seconds=wall_seconds,
            status="completed",
            slurm_job_id=slurm_job_id,
            extra={"docs_with_ccodes": docs_with_ccodes},
        )
    except Exception:
        pass  # Non-fatal — history write failure must not abort the stage

    if manifest_path and manifest_path.exists():
        update_namespace_stage_status(
            manifest_path, namespace, "ccode", "completed", metrics=metrics
        )

    write_stage_event(
        run_id=run_id,
        namespace=namespace,
        script_id="ccode-enrichment",
        status="completed",
        stage="ccode",
        metrics=metrics,
    )
    write_runtime_history_event(
        run_id=run_id,
        event="ccode_enrichment",
        status="completed",
        namespace=namespace,
        stage="ccode",
        slurm_job_id=slurm_job_id,
        details=metrics,
    )

    if place_reader is not None:
        place_reader.close()

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-namespace ccode enrichment using staged UN H3 coverage"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--namespace", required=True, help="Namespace to enrich")
    parser.add_argument("--manifest-path", help="Explicit run manifest path")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
            runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
        )
    )

    import os
    slurm_job_id = os.getenv("SLURM_JOB_ID")

    metrics = run_ccode_enrichment(
        run_id=args.run_id,
        namespace=args.namespace,
        manifest_path=manifest_path if manifest_path.exists() else None,
        slurm_job_id=slurm_job_id,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
