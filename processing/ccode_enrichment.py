#!/usr/bin/env python3
"""Per-namespace ccode enrichment (Batch 7).

**Memory:** loads the full UN per-geometry ``h3_cover`` and country
polygons into memory. Empirical floor is ~16 GiB; recommended budget is
**24 GiB** (matches ``submit_ccode_slurm._build_sbatch_script``).
``run_ccode_enrichment`` warns at startup when the cgroup limit is below
that floor — heed the warning or expect an OOM-kill mid-run on namespaces
with many candidate ccodes (e.g. po crashed at 8 GiB on 2026-05-02).

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

from processing.ccode_tiers import (
    BndaFallbackIndex,
    apply_overlay,
    load_disputed_claims,
)
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

# BNDA marks disputed / special territories with a LOWERCASE ``iso2cd`` whose
# naive upper-casing would be bogus or *collide* with a real ISO code — e.g.
# ``xk`` (Jammu and Kashmir) upper-cases to ``XK``, which every ISO consumer
# reads as **Kosovo**. Map each to its claimant ISO 3166-1 alpha-2 countries so
# places there attest to *all* claimants (politically neutral, and consistent
# with the pre-existing multi-tagging). Any lowercase code NOT listed here is
# skipped rather than emitting a non-ISO ccode.
BNDA_DISPUTED_CLAIMANTS: dict[str, list[str]] = {
    "xk": ["IN", "PK"],   # Jammu and Kashmir  (India / Pakistan)
    "xc": ["CN", "IN"],   # Aksai Chin         (China-administered, India-claimed)
    "xp": ["IN", "CN"],   # Arunachal Pradesh / South Tibet (India-admin, China-claimed)
    "xr": ["RU", "JP"],   # Kuril Islands      (Russia-administered, Japan-claimed)
}


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _h3_merged_dir(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / namespace / "h3_merged"


def _iter_staged_docs(
    namespace: str,
    *,
    prefer_jsonl: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield staged docs for ``namespace`` from h3_merged.

    Defaults to the parquet sidecar when present (faster). Pass
    ``prefer_jsonl=True`` when the caller depends on fields that
    ``processing.staged_parquet.write_parquet_from_jsonl`` strips for
    pyarrow compatibility — chiefly ``geometries[].hull``, which is
    needed by ``_load_un_records`` for point-in-polygon containment when
    the geom store hasn't been consolidated yet (so ``geom_ref`` lookups
    return None and hull is the only source of polygon truth).
    """
    src_dir = _h3_merged_dir(namespace)
    parquet_path = src_dir / "places.parquet"
    jsonl_path = src_dir / "places.jsonl"

    if not prefer_jsonl and parquet_path.exists():
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


def split_by_tier(
    un_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition `un` records into (primary, fallback) by ``boundary_source``.

    The primary source (geoBoundaries HPSC) is ADM0 at sovereign-state level and
    does not carve out twelve ISO territories — ``HK``, ``SJ``, ``TF``, ``GS``,
    ``JE``, ``MO``, ``PM``, ``MF``, ``SX``, ``CC``, ``BV``, ``HM``, together
    32,703 places. Those keep their BNDA polygon and form tier 2.

    The tiers must NOT be merged into one polygon set. A 232-vertex BNDA outline
    beside a 73,663-vertex geoBoundaries one along a shared border turns every
    disagreement into a sliver where a place is claimed by both countries or by
    neither. Consulting tier 2 only when tier 1 returned nothing makes that
    impossible.

    Records with no ``boundary_source`` (a pre-place#173 extract) all count as
    primary, which reproduces the previous single-tier behaviour exactly.
    """
    primary, fallback = [], []
    for doc in un_records:
        sources = {(g or {}).get("boundary_source")
                   for g in (doc.get("geometries") or [])}
        (fallback if sources == {"bnda"} else primary).append(doc)
    return primary, fallback


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
        self._geoms_per_ccode: dict[str, list[BaseGeometry]] = {}
        # Shapely PreparedGeometry per country, built ONCE. ``prep()`` builds an
        # STRtree over the polygon: with BNDA's 232-vertex outlines that was
        # nearly free, but geoBoundaries HPSC averages 73,663 vertices per
        # country (Australia has 1,655,696), so rebuilding it per place per
        # candidate is catastrophic. It is what stalled `clio` — continent-scale
        # polities intersecting dozens of countries — and made `osm` decelerate
        # from 1.8 MB/min to 0.67 MB/min, a ~13 h projection.
        self._prepared_per_ccode: dict[str, list] = {}

    def _ensure_reader(self) -> GeomStoreReader | None:
        if self._reader is not None:
            return self._reader
        try:
            self._reader = GeomStoreReader(GEOM_STORE_DIR)
        except FileNotFoundError:
            self._reader = None
        return self._reader

    def _load(self, ccode: str) -> list[BaseGeometry]:
        cached = self._geoms_per_ccode.get(ccode)
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

        self._geoms_per_ccode[ccode] = geoms
        return geoms

    def geoms_for(self, ccode: str) -> list[BaseGeometry]:
        return self._load(ccode)

    def prepared_for(self, ccode: str) -> list:
        """(prepared, raw) pairs for a country, prepared built once and reused.

        The raw geometry is returned alongside because ``PreparedGeometry``
        supports only predicates — ``intersection()`` for the overlap measure
        still needs the original.
        """
        cached = self._prepared_per_ccode.get(ccode)
        if cached is not None:
            return cached
        # Built from geoms_for(), not _load(), so that geoms_for remains the
        # single override point for subclasses and test doubles.
        pairs = [(prep(g), g) for g in self.geoms_for(ccode)]
        self._prepared_per_ccode[ccode] = pairs
        return pairs


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


def _repr_point_of(doc: dict[str, Any]) -> tuple[float, float] | None:
    """(lon, lat) of the doc's first usable representative point.

    Used only by the disputed-claims overlay, which asks "is this place inside
    a contested territory?" — a point test, not a shape test. A place whose
    geometry straddles a disputed boundary is attested by where it is
    represented, which is the same basis the rest of the pipeline uses for
    ``h3_centroid``.
    """
    for geom in (doc.get("geometries") or []):
        if not isinstance(geom, dict):
            continue
        rp = geom.get("repr_point")
        if isinstance(rp, dict):
            try:
                return float(rp["lon"]), float(rp["lat"])
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
    is_point = place_geom.geom_type in ("Point", "MultiPoint")
    is_linear = place_geom.geom_type in (
        "LineString", "MultiLineString", "LinearRing")
    matches: list[tuple[str, float]] = []

    # The place's own measure, in its own dimension. For any place that a
    # country wholly covers, this IS the measure of the intersection.
    place_measure = place_geom.length if is_linear else place_geom.area

    for ccode in candidate_ccodes:
        for prepared, un_geom in un_cache.prepared_for(ccode):
            try:
                if not prepared.intersects(place_geom):
                    continue
                if is_point:
                    matches.append((ccode, 1.0))
                    break
                # Fast path: the overwhelmingly common case is a place that
                # lies wholly inside one country. Then intersection(place) ==
                # place, so the overlay computes a result we already have.
                #
                # This is not an approximation — the measure is exact — but it
                # replaces a full polygon overlay against a 73,663-vertex
                # geoBoundaries outline with a prepared predicate answered from
                # the STRtree ``intersects`` has already built. The overlay is
                # then reserved for genuine border-straddlers, which are rare.
                #
                # Overlay cost scales with the COUNTRY's vertex count, not the
                # place's, so the BNDA→geoBoundaries precision upgrade (232 →
                # 73,663 vertices/country) made it ~300x dearer. That is what
                # collapsed `osm` from ~206,000 docs/min across its points to
                # 4,223/min once it reached areal ways — a ~36 h projection.
                if prepared.covers(place_geom):
                    measure = place_measure
                    if measure <= 0:
                        continue
                    matches.append((ccode, measure))
                    break
                inter = un_geom.intersection(place_geom)
                if inter.is_empty:
                    continue
                # Measure the intersection in the dimension of the PLACE, not
                # of the intersection.
                #
                # A line inside a polygon intersects as a *line*: zero area,
                # non-zero length. Testing area alone discarded every linear
                # feature as a border touch — 10,078,925 osm and 681,970 ohm
                # ways had NO ccode at all (0.0%, against 94% for points and
                # 96% for areas in the same namespaces).
                #
                # But "area or length" is not the fix: two polygons sharing a
                # border also intersect as a line, and crediting that would
                # give every place the countries next door. Keying off the
                # place's own dimension separates the two — an areal place
                # still needs areal overlap, a linear place needs linear
                # overlap.
                measure = inter.length if is_linear else inter.area
                if measure <= 0:
                    continue
                matches.append((ccode, measure))
                break
            except Exception:
                continue

    if not matches:
        return []
    if is_point:
        return sorted({c for c, _ in matches})
    matches.sort(key=lambda t: t[1], reverse=True)
    return [c for c, _ in matches]


def _synth_cells_from_repr_points(doc: dict[str, Any], res: int) -> list[str]:
    """Synthesise covering H3 cells from each geometry's ``repr_point``.

    Used when a doc's geometries carry no ``h3_cover``/``h3_centroid`` (e.g. a
    point authority that skipped the H3 stage, such as ``tgn``). The
    ``repr_point`` is guaranteed to lie within the geometry, so its cell at the
    prefilter resolution is a valid candidate-country probe.
    """
    cells: list[str] = []
    for geom in doc.get("geometries") or []:
        if not isinstance(geom, dict):
            continue
        rp = geom.get("repr_point")
        if not rp:
            continue
        try:
            cells.append(_h3.latlng_to_cell(float(rp["lat"]), float(rp["lon"]), res))
        except (KeyError, TypeError, ValueError):
            continue
    return cells


def resolve_ccodes_for_doc(
    doc: dict[str, Any],
    cell_to_ccodes: dict[str, set[str]],
    un_cache: "_UnGeometryCache",
    place_reader: "GeomStoreReader | None",
    *,
    synth_res: int | None = None,
) -> tuple[list[str], str]:
    """Resolve ISO ccodes for one place doc via the shared UN-overlap engine.

    This is the single seam used by *both* the staged ingestion pass
    (:func:`run_ccode_enrichment`) and the live-index backfill
    (``processing.backfill_ccodes``), so containment logic never diverges
    between the two contexts.

    Returns ``(ccodes, outcome)`` with ``outcome`` one of
    ``"ok" | "no_geom" | "no_candidate" | "no_match"``.

    ``synth_res`` (e.g. :data:`PREFILTER_RESOLUTION`) makes the resolver robust
    to docs whose geometries lack ``h3_cover``/``h3_centroid``: a covering cell
    is synthesised from each geometry's ``repr_point`` at that resolution. Left
    ``None`` (the default) the staged behaviour is unchanged.
    """
    cells = _extract_place_h3_cells(doc)
    if not cells and synth_res is not None and _H3_AVAILABLE:
        cells = _synth_cells_from_repr_points(doc, synth_res)
    if not cells:
        return [], "no_geom"
    candidates = candidate_ccodes_for_cells(cells, cell_to_ccodes)
    if not candidates:
        return [], "no_candidate"
    place_geom = _extract_place_geometry(doc, place_reader)
    if place_geom is None:
        return [], "no_geom"
    ccodes = _filter_by_containment(place_geom, candidates, un_cache)
    if not ccodes:
        return [], "no_match"
    return ccodes, "ok"


class UnCountryIndex:
    """Exact point/area → ccode resolver backed by an STRtree over **every** UN
    country geometry.

    Motivation: the h3 prefilter (:func:`build_un_prefilter` +
    :func:`candidate_ccodes_for_cells`) relies on UN ``h3_cover``, which has
    large interior gaps for big countries (the polyfill is truncated /
    simplified for huge polygons — e.g. the US West Coast is absent from the US
    cover). Any place in an under-covered region gets *no candidate* and is
    silently dropped. This index instead queries the country polygons directly
    (bounding-box candidates via STRtree, then exact containment), so coverage
    has no gaps. Full country polygons are loaded eagerly, so this is the
    ~24 GiB-resident path — intended for the Slurm resolve stage.
    """

    def __init__(
        self,
        un_records: list[dict[str, Any]],
        place_reader: "GeomStoreReader | None" = None,
    ):
        from shapely.strtree import STRtree

        geoms: list[BaseGeometry] = []
        ccodes: list[str] = []
        for doc in un_records:
            ccs = doc.get("ccodes") or []
            if not ccs or not isinstance(ccs[0], str) or not ccs[0]:
                continue
            ccode = ccs[0]
            place_id = doc.get("place_id")
            for idx, g in enumerate(doc.get("geometries") or []):
                if not isinstance(g, dict):
                    continue
                gi = g.get("geometry_index", idx)
                shp: BaseGeometry | None = None
                # Prefer the REAL Natural-Earth polygon from the geom store
                # (keyed ``{place_id}_{geometry_index}``). The staged UN doc's
                # ``geom_ref`` is None and its ``hull`` is a convex hull — using
                # the hull over-assigns at borders (Lisbon∈Spain's hull) and,
                # for antimeridian countries, spans the globe (US/RU on every
                # point). The real multipolygon's parts are local, so exact
                # ``intersects`` is accurate even though its envelope is wide.
                if place_reader is not None and place_id:
                    gj = place_reader.get(f"{place_id}_{gi}")
                    if gj:
                        shp = geojson_to_shapely(gj)
                if shp is None:
                    hull = g.get("hull")
                    if hull:
                        shp = geojson_to_shapely(hull)
                if shp is None or shp.is_empty:
                    continue
                # Decompose MultiPolygons into their constituent Polygons so
                # each STRtree entry has a LOCAL envelope. Otherwise the US/RU
                # multipolygons (whose parts straddle ±180) have a globe-spanning
                # envelope and get exact-tested against every point on Earth —
                # ~300 docs/s. Per-part local envelopes let the tree prune, and
                # the union of parts is identical.
                if shp.geom_type == "MultiPolygon":
                    for part in shp.geoms:
                        if not part.is_empty:
                            geoms.append(part)
                            ccodes.append(ccode)
                else:
                    geoms.append(shp)
                    ccodes.append(ccode)
        self._set_geoms(geoms, ccodes)

    def _set_geoms(self, geoms: list[BaseGeometry], ccodes: list[str]) -> None:
        from shapely.strtree import STRtree

        self._geoms = geoms
        self._ccodes = ccodes
        self._prepared = [prep(g) for g in geoms]
        self._tree = STRtree(geoms) if geoms else None

    @classmethod
    def from_bnda_geojson(cls, path: str, iso_field: str = "iso2cd") -> "UnCountryIndex":
        """Build the country index from the UN BNDA country-boundary GeoJSON
        (``processing/data/un_bnda_countries.geojson``) — the authoritative,
        politically-neutral ISO 3166-1 alpha-2 source. Preferred over the
        ``un``/Natural-Earth path: native ISO2 for every country (no NE ``-99``
        France/Norway/Kosovo quirk), dependent territories as separate features,
        Antarctica included, antimeridian handled, topologically coherent (no
        border slivers). Self-contained — needs no geom store or ES."""
        from shapely.geometry import shape

        self = cls.__new__(cls)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        geoms: list[BaseGeometry] = []
        ccodes: list[str] = []
        for feat in data.get("features") or []:
            props = feat.get("properties") or {}
            code = props.get(iso_field)
            if not isinstance(code, str) or len(code) != 2 or not code.isalpha():
                continue
            # Resolve the ISO code(s) this feature contributes. BNDA disputed
            # territories carry a lowercase code → expand to claimant ISO codes;
            # normal countries are uppercase → use as-is; any other lowercase/
            # mixed code is skipped (never emit a bogus/colliding ccode).
            low = code.lower()
            if low in BNDA_DISPUTED_CLAIMANTS:
                target_codes = BNDA_DISPUTED_CLAIMANTS[low]
            elif code.isupper():
                target_codes = [code]
            else:
                continue
            g = feat.get("geometry")
            if not g:
                continue
            try:
                shp = shape(g)
            except Exception:
                continue
            if shp is None or shp.is_empty:
                continue
            # Per-part entries → local STRtree envelopes (fast, and correct for
            # antimeridian countries whose overall envelope spans the globe).
            parts = list(shp.geoms) if shp.geom_type == "MultiPolygon" else [shp]
            for part in parts:
                if not part.is_empty:
                    for tc in target_codes:
                        geoms.append(part)
                        ccodes.append(tc)
        self._set_geoms(geoms, ccodes)
        return self

    def ccodes_for(
        self, place_geom: BaseGeometry, snap_tol_deg: float = 0.0
    ) -> tuple[list[str], bool]:
        """Return ``(ccodes, snapped)`` for a place geometry.

        Points → all containing countries; areas → every overlapping country
        ordered by descending overlap area.

        ``snap_tol_deg`` > 0 enables an **unambiguous** nearest-country snap for
        points that fall in no country (Natural Earth's non-topological borders
        leave sub-km slivers between neighbours). When the point lies within the
        tolerance of *exactly one* country, that ccode is returned with
        ``snapped=True``; if two+ countries are that close (a true border) or
        the nearest is farther, nothing is returned — so a wrong side of a
        border can never be assigned."""
        if self._tree is None or place_geom is None or place_geom.is_empty:
            return [], False
        is_point = place_geom.geom_type == "Point"
        matches: list[tuple[str, float]] = []
        for i in self._tree.query(place_geom):
            idx = int(i)
            try:
                if not self._prepared[idx].intersects(place_geom):
                    continue
                if is_point:
                    matches.append((self._ccodes[idx], 1.0))
                    continue
                inter = self._geoms[idx].intersection(place_geom)
                if inter.is_empty:
                    continue
                area = inter.area
                if area <= 0:
                    continue
                matches.append((self._ccodes[idx], area))
            except Exception:
                continue
        if matches:
            if is_point:
                return sorted({c for c, _ in matches}), False
            matches.sort(key=lambda t: t[1], reverse=True)
            seen: set[str] = set()
            out: list[str] = []
            for c, _ in matches:
                if c not in seen:
                    seen.add(c)
                    out.append(c)
            return out, False

        # No containment: try the unambiguous nearest-country snap for points.
        if is_point and snap_tol_deg > 0:
            near: set[str] = set()
            try:
                for i in self._tree.query(place_geom.buffer(snap_tol_deg)):
                    idx = int(i)
                    if self._geoms[idx].distance(place_geom) <= snap_tol_deg:
                        near.add(self._ccodes[idx])
            except Exception:
                return [], False
            if len(near) == 1:
                return sorted(near), True
        return [], False


def resolve_ccodes_for_doc_exact(
    doc: dict[str, Any],
    country_index: "UnCountryIndex",
    place_reader: "GeomStoreReader | None",
    snap_tol_deg: float = 0.0,
) -> tuple[list[str], str]:
    """Gap-free variant of :func:`resolve_ccodes_for_doc` using
    :class:`UnCountryIndex` (no h3 prefilter). Returns ``(ccodes, outcome)``
    with outcome ``"ok" | "snap" | "no_geom" | "no_match"``. ``snap`` marks an
    unambiguous nearest-country recovery (see :meth:`UnCountryIndex.ccodes_for`);
    ``no_match`` is a *correct* empty result — the place is genuinely outside
    every country (at sea, Antarctica, disputed)."""
    place_geom = _extract_place_geometry(doc, place_reader)
    if place_geom is None:
        return [], "no_geom"
    ccodes, snapped = country_index.ccodes_for(place_geom, snap_tol_deg=snap_tol_deg)
    if not ccodes:
        return [], "no_match"
    return ccodes, ("snap" if snapped else "ok")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def _load_un_records() -> list[dict[str, Any]]:
    """Read all UN docs from the JSONL specifically.

    The parquet sidecar drops ``geometries[].hull`` (see
    ``staged_parquet.strip_hull_for_parquet``); without hull, point-in-
    polygon containment can't run unless the consolidated geom_store
    index is available. Reading the JSONL keeps hull intact and lets
    containment work in either configuration.
    """
    return list(_iter_staged_docs(UN_NAMESPACE, prefer_jsonl=True))


_RECOMMENDED_MEM_GIB = 24


def _warn_if_under_provisioned() -> None:
    """Emit a loud warning when the cgroup memory limit is below the
    empirical floor for ccode_enrichment.

    No effect outside cgroup-bounded environments (e.g. interactive runs on
    a host without cgroup memory controllers); only Slurm tasks under a
    ``--mem`` budget are constrained, and that's where OOM-kills happen.
    """
    import os
    candidates = (
        "/sys/fs/cgroup/memory.max",                        # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",      # cgroup v1
    )
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            raw = open(path).read().strip()
        except OSError:
            continue
        if raw in ("", "max"):
            return
        try:
            limit_bytes = int(raw)
        except ValueError:
            return
        # cgroup v1 reports an absurdly large value when no limit is set;
        # treat anything ≥ 1 PiB as effectively unlimited.
        if limit_bytes >= (1 << 50):
            return
        limit_gib = limit_bytes / (1024 ** 3)
        if limit_gib < _RECOMMENDED_MEM_GIB:
            import sys
            print(
                f"WARNING: ccode_enrichment cgroup memory limit is "
                f"{limit_gib:.1f} GiB, below the recommended "
                f"{_RECOMMENDED_MEM_GIB} GiB. Loading UN h3_cover and "
                "country polygons typically peaks at 16-20 GiB. Likely "
                f"to OOM mid-run — increase to --mem={_RECOMMENDED_MEM_GIB}G.",
                file=sys.stderr, flush=True,
            )
        return


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

    _warn_if_under_provisioned()

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

    # Tier 1 (geoBoundaries HPSC) and tier 2 (the territories it does not carve
    # out, which keep their BNDA polygon) are built as SEPARATE candidate sets
    # and consulted in order. Merging them would put a 232-vertex BNDA outline
    # beside a 73,663-vertex geoBoundaries one along a shared border, and every
    # disagreement between them becomes a sliver where a place is claimed by
    # both countries or by neither. Fallback-on-empty makes that impossible.
    primary_records, fallback_records = split_by_tier(un_records)
    cell_to_ccodes, ccode_to_geoms = build_un_prefilter(primary_records)
    un_cache = _UnGeometryCache(ccode_to_geoms)

    # Tier 2 is the FULL BNDA set, not merely the countries geoBoundaries does
    # not carve out. Keying tier 2 on "geoBoundaries lacks this country" left a
    # place just outside geoBoundaries' finer coastline with no country at all,
    # because its own country was in tier 1 and so absent from tier 2: 464
    # places across VI, AS, GU, MP and BQ on the 5 Aug 2026 run.
    #
    # Two causes, both answered by the same fallback:
    #   * coastal features (capes, bays, rocks, piers) whose repr_point sits a
    #     few metres seaward — a 232-vertex outline swallowed them, a
    #     73,663-vertex one correctly does not;
    #   * genuine omissions — geoBoundaries models BQ as ONE polygon covering
    #     Bonaire only, so Saba and Sint Eustatius, including their own
    #     administrative polygons, fall outside every primary geometry.
    fb_index = BndaFallbackIndex()
    print(f"  tiers: primary={len(primary_records)} "
          f"(prefilter cells {len(cell_to_ccodes):,}); "
          f"fallback=full BNDA, {len(fb_index)} features "
          f"({len(fallback_records)} countries absent from the primary)")

    # Disputed-territory overlay, applied IN ADDITION to tier 1 so that where a
    # source picks a single claimant all claimants are still attested. Without
    # it Western Sahara's 4,387 places silently become Moroccan, because the
    # primary DOES answer and no fallback fires.
    territories = load_disputed_claims()
    if territories:
        print(f"  disputed-claims overlay: {len(territories)} territory/ies "
              f"({', '.join(t.get('name', '?') for t in territories)})")

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
    docs_from_fallback = 0
    docs_overlay_applied = 0

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
            # No H3 prefilter for tier 2: it is an STRtree over the whole BNDA
            # set, queried by bounding box. A place with no tier-1 candidate is
            # therefore still worth resolving — which is the point, since those
            # are exactly the places that previously ended uncoded.
            if not candidates and not fb_index:
                docs_no_candidate += 1
                continue

            place_geom = _extract_place_geometry(doc, place_reader)
            if place_geom is None:
                docs_no_geom += 1
                continue

            # Tier 1 first; tier 2 ONLY when tier 1 answers nothing.
            ccodes = (_filter_by_containment(place_geom, candidates, un_cache)
                      if candidates else [])
            tier = "primary" if ccodes else "none"
            if not ccodes and fb_index:
                ccodes = fb_index.ccodes_for(place_geom)
                if ccodes:
                    tier = "fallback"
                    docs_from_fallback += 1

            # Additive: never removes a code the source returned. Asserting a
            # source is wrong about who ADMINISTERS a territory is a far larger
            # claim than asserting a territory is CONTESTED.
            if territories:
                rp = _repr_point_of(doc)
                if rp is not None:
                    before = list(ccodes)
                    ccodes = apply_overlay(ccodes, rp[0], rp[1], territories)
                    if ccodes != before:
                        docs_overlay_applied += 1

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
            "docs_from_fallback": docs_from_fallback,
        "docs_overlay_applied": docs_overlay_applied,
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
