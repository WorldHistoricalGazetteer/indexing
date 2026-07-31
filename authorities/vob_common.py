"""Shared builder for the Vision of Britain / GB Historical GIS boundary authorities.

Four UK administrative-boundary levels from the *Great Britain Historical GIS
Project* (GBHGIS, University of Portsmouth), each deposited as a separate OPEN,
CC-BY-SA 4.0 study at the UK Data Service and set up here as its own authority /
namespace (place#135):

    vob_rd   SN 9032  Registration districts of England & Wales, 1851-1911
                      (== poor-law unions; ``UNITTYPE=PR_DIST`` — coterminous,
                      so a single namespace covers both)
    vob_rc   SN 9033  Registration counties of England & Wales, 1851-1911
    vob_cty  SN 9179  Administrative counties of England & Wales, 1911-1971
    vob_lgd  SN 9321  Local-government districts of England & Wales, 1911-1971

**Licence (all four): CC-BY-SA-4.0** — attribution + share-alike, commercial use
and redistribution permitted. Parishes are the deliberate restricted exception
and are NOT handled here (see the ``kain_par`` authority / issue #135).

**Source shape.** Each study is a zip of per-census-year ESRI shapefiles in the
**OSGB National Grid (EPSG:27700)**; every feature carries the standard GBHGIS
attributes ``G_UNIT`` (stable unit id, constant across census years), ``G_NAME``,
``G_YEAR`` (census year), ``UNITTYPE``, ``NATION`` (ENGLAND/WALES), ``G_LANGUAGE``.

**Model (multi-snapshot per unit, per issue #135 decision).** We group every
census-year snapshot by its stable ``G_UNIT`` and emit ONE ``places`` doc per
unit carrying one ``geometries[]`` entry per census year — each reprojected to
WGS84 and stamped with that decade's timespan. This makes ``contained_in X at
date Y`` containment reconciliation resolve to the boundary current at Y, and
lets the Atlas date filter (place#131) show the right snapshot. Reprojection is
pure ``pyproj`` + ``shapely`` (no GDAL/geopandas — neither ``whg`` env has them);
``pyshp`` reads the shapefiles straight out of the zip.

Run a level standalone via its thin wrapper, e.g. ``python -m authorities.vob_rd-places``.
"""
from __future__ import annotations

import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from processing.helpers import (
    compute_area_km2,
    enrich_geometry,
    write_staged_place_doc,
)
from processing.settings import AUTHORITIES, DATA_DIR
from processing.temporal import bounded

# Decennial-census cadence. Each snapshot is stamped as valid for the decade it
# heads: census year Y -> [Y, Y+DECADE]. Consecutive snapshots share an endpoint
# (no gaps); a unit absent from a census simply has no geometry for that decade.
DECADE = 10

# GBHGIS ``G_LANGUAGE`` (ISO 639-3-ish) -> WHG toponym lang (ISO 639-1).
_LANG_MAP = {"eng": "en", "cym": "cy", "wel": "cy", "gle": "ga", "sco": "sco"}

# GBHGIS stores names in ALL CAPS (e.g. ``LLANDILO FAWR``, ``CHESTER LE STREET``).
# Recase to conventional English place-name form for display + matching. Small
# connective words stay lower-case unless first; the affix ``Le``/``La`` in
# "X-le-Y" names stays lower; apostrophe/hyphen segments are cased correctly
# (``KING'S NORTON`` -> ``King's Norton``, not ``King'S``).
_LOWER_WORDS = frozenset({
    "le", "la", "de", "upon", "on", "under", "in", "the", "and", "of", "cum",
    "next", "by", "with", "juxta",
})


def _cap_token(tok: str) -> str:
    # Capitalise the first alphabetic run only; keep the rest (post-apostrophe
    # 's', ordinal letters) lower-case.
    for i, ch in enumerate(tok):
        if ch.isalpha():
            return tok[:i] + ch.upper() + tok[i + 1:].lower()
    return tok


def _titlecase_name(raw: str) -> str:
    """Conventional-case a GBHGIS ALL-CAPS place name.

    Leaves already-mixed-case input untouched. Small connective words stay
    lower-case unless first; hyphen segments are cased individually so
    ``CHESTER LE STREET`` -> ``Chester le Street`` and ``STOKE-UPON-TRENT`` ->
    ``Stoke-upon-Trent``.
    """
    raw = raw.strip()
    if not raw or raw != raw.upper():
        return raw  # already mixed-case, or empty
    words = [w for w in raw.split() if w]
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        if i > 0 and low in _LOWER_WORDS:
            out.append(low)
            continue
        segs = low.split("-")
        cased = [
            s if (j > 0 and s in _LOWER_WORDS) else _cap_token(s)
            for j, s in enumerate(segs)
        ]
        out.append("-".join(cased))
    return " ".join(out)


@dataclass(frozen=True)
class VobLevel:
    """Per-level configuration for one GBHGIS boundary authority."""
    namespace: str
    study_sn: str
    dataset_name: str
    boundary: str            # ``boundary`` tag (Space filter; also a tile prop)
    type_identifier: str     # native type slug for ``types[].identifier``
    type_source_label: str   # human label for ``types[].sourceLabel``
    ccodes: tuple[str, ...] = ("GB",)


LEVELS: dict[str, VobLevel] = {
    "vob_rd": VobLevel(
        namespace="vob_rd", study_sn="9032",
        dataset_name="GBHGIS Registration Districts of England & Wales, 1851-1911",
        boundary="registration-district",
        type_identifier="registration-district",
        type_source_label="Registration District / Poor Law Union",
    ),
    "vob_rc": VobLevel(
        namespace="vob_rc", study_sn="9033",
        dataset_name="GBHGIS Registration Counties of England & Wales, 1851-1911",
        boundary="registration-county",
        type_identifier="registration-county",
        type_source_label="Registration County",
    ),
    "vob_cty": VobLevel(
        namespace="vob_cty", study_sn="9179",
        dataset_name="GBHGIS Administrative Counties of England & Wales, 1911-1971",
        boundary="administrative-county",
        type_identifier="administrative-county",
        type_source_label="Administrative County",
    ),
    "vob_lgd": VobLevel(
        namespace="vob_lgd", study_sn="9321",
        dataset_name="GBHGIS Local Government Districts of England & Wales, 1911-1971",
        boundary="local-government-district",
        type_identifier="local-government-district",
        type_source_label="Local Government District",
    ),
}


# ── Reprojection: OSGB National Grid (EPSG:27700) -> WGS84 (EPSG:4326) ────────
# Built lazily so importing this module never requires pyproj (e.g. on hosts
# that only consume the config). ``always_xy=True`` yields lon/lat output.
_TRANSFORMER = None


def _reproject_to_wgs84(geojson_geom: dict) -> dict | None:
    """Reproject a GeoJSON geometry from EPSG:27700 to EPSG:4326 (lon/lat)."""
    global _TRANSFORMER
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as shp_transform

    if _TRANSFORMER is None:
        _TRANSFORMER = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    geom = shape(geojson_geom)
    if geom.is_empty:
        return None
    return mapping(shp_transform(_TRANSFORMER.transform, geom))


# ── Reading ──────────────────────────────────────────────────────────────────
def _find_zip(namespace: str, study_sn: str) -> Path:
    """Locate the UKDS study zip placed in the namespace's data dir.

    These are Open UKDA downloads (CC-BY-SA, no registration) but the download
    URL is a one-shot session-tokened link, so — like the ``hgis`` LPFs — the
    zip is placed manually in ``{DATA_DIR}/authorities/{namespace}/`` rather than
    auto-fetched. Prefer a zip whose name references the study number.
    """
    ns_dir = Path(DATA_DIR) / "authorities" / namespace
    zips = sorted(ns_dir.glob("*.zip"))
    if not zips:
        raise FileNotFoundError(
            f"No UKDS zip found in {ns_dir} — download study SN {study_sn} "
            f"(Open UKDA Download, CC-BY-SA) and place the .zip there."
        )
    preferred = [z for z in zips if study_sn in z.name]
    return preferred[0] if preferred else zips[0]


_YEAR_RE = __import__("re").compile(r"(1[5-9]\d\d|20\d\d)")


def _year_from_filename(basename: str) -> int | None:
    """Parse the census year from a GBHGIS shapefile basename.

    Two of the four studies (SN 9033 registration counties, SN 9179
    administrative counties) omit the ``G_YEAR`` attribute — the census year
    lives only in the filename (``ew1851_regcounties``, ``ew1911_admcounties``).
    """
    m = _YEAR_RE.search(basename)
    return int(m.group(1)) if m else None


def _iter_shp_records(zip_path: Path) -> Iterator[tuple[dict, dict, str]]:
    """Yield ``(record_dict, osgb_geojson_geometry, shp_basename)`` for every
    polygon feature across all per-census-year shapefiles in the UKDS study zip."""
    import shapefile  # pyshp

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        shp_members = sorted(n for n in names if n.lower().endswith(".shp"))
        for shp_n in shp_members:
            base = shp_n[:-4]
            basename = Path(shp_n).stem
            dbf_n = next((n for n in names if n.lower() == (base + ".dbf").lower()), None)
            shx_n = next((n for n in names if n.lower() == (base + ".shx").lower()), None)
            if not dbf_n:
                continue
            reader = shapefile.Reader(
                shp=io.BytesIO(zf.read(shp_n)),
                dbf=io.BytesIO(zf.read(dbf_n)),
                shx=io.BytesIO(zf.read(shx_n)) if shx_n else None,
                encoding="utf-8",
            )
            for sr in reader.iterShapeRecords():
                geom = sr.shape.__geo_interface__
                if not geom or not geom.get("coordinates"):
                    continue
                yield sr.record.as_dict(), geom, basename


# ── Building ─────────────────────────────────────────────────────────────────
@dataclass
class _Snapshot:
    year: int
    name: str
    lang: str
    nation: str
    unittype: str
    geom_wgs: dict


def _lang_for(code: str) -> str:
    return _LANG_MAP.get((code or "").strip().lower(), "und")


def _predecessor_map(years: list[int]) -> dict[int, int]:
    """Map each census year to the *previous* census year in the series.

    The first year maps to nothing (absent from the dict), which is correct:
    nothing bounds how long before the first census the unit had existed, so
    ``start.earliest`` stays unbounded there (place#164).
    """
    ordered = sorted(set(years))
    return {y: ordered[i - 1] for i, y in enumerate(ordered) if i > 0}


def _successor_map(years: list[int]) -> dict[int, int]:
    """Map each census year to the *next* census year in the dataset series,
    with the final year mapped to ``year + DECADE``.

    Snapshots are stamped ``[Y, successor(Y)]`` so coverage is contiguous even
    across irregular census gaps — e.g. the missing 1941 (WWII) census in the
    20th-century levels makes 1931 span [1931, 1951] rather than leaving a hole.
    """
    ordered = sorted(set(years))
    succ = {}
    for i, y in enumerate(ordered):
        succ[y] = ordered[i + 1] if i + 1 < len(ordered) else y + DECADE
    return succ


def build_docs(level: VobLevel, zip_path: Path) -> Iterator[dict]:
    """Group census-year snapshots by ``G_UNIT`` and yield one multi-snapshot
    ``places`` doc per stable unit."""
    units: dict[int, list[_Snapshot]] = defaultdict(list)
    all_years: set[int] = set()

    for rec, osgb_geom, basename in _iter_shp_records(zip_path):
        g_unit = rec.get("G_UNIT")
        name = _titlecase_name(str(rec.get("G_NAME") or "").strip())
        # Year: prefer the G_YEAR attribute (SN 9032/9321), else the filename
        # (SN 9033/9179 omit G_YEAR — see _year_from_filename).
        year = rec.get("G_YEAR")
        if not isinstance(year, (int, float)):
            year = _year_from_filename(basename)
        if g_unit is None or not name or not isinstance(year, (int, float)):
            continue
        year = int(year)
        wgs = _reproject_to_wgs84(osgb_geom)
        if not wgs:
            continue
        all_years.add(year)
        units[int(g_unit)].append(_Snapshot(
            year=year, name=name, lang=_lang_for(rec.get("G_LANGUAGE")),
            nation=str(rec.get("NATION") or "").strip(),
            unittype=str(rec.get("UNITTYPE") or "").strip(),
            geom_wgs=wgs,
        ))

    succ = _successor_map(sorted(all_years))
    pred = _predecessor_map(sorted(all_years))

    for g_unit, snaps in units.items():
        snaps.sort(key=lambda s: s.year)
        place_id = f"{level.namespace}:{g_unit}"

        # Geometries: one snapshot per census year, stamped [Y, next-census].
        geometries = []
        for idx, s in enumerate(snaps):
            # place#164: a 1911 census attests the unit AT 1911; it says
            # nothing about 1915. But the neighbouring snapshots do bound when
            # the configuration can have begun and ended, so encode all four:
            #   definitely alive at 1911, possibly alive 1901-1921.
            # The old {"start": {"in": 1911}, "end": {"in": 1921}} over-claimed
            # every intervening year as definite.
            ts = bounded(
                start_earliest=pred.get(s.year),
                start_latest=s.year,
                end_earliest=s.year,
                end_latest=succ[s.year],
            )[0]
            ge = enrich_geometry(
                s.geom_wgs, timespans=[ts], geom_key=f"{place_id}_{idx}",
            )
            if ge:
                geometries.append(ge)
        if not geometries:
            continue

        # Toponyms: distinct (name, lang) across the unit's snapshots, each
        # spanning [first attested year, next census after the last].
        name_years: dict[tuple[str, str], list[int]] = defaultdict(list)
        for s in snaps:
            name_years[(s.name, s.lang)].append(s.year)
        toponyms = []
        for (name, lang), ys in name_years.items():
            toponyms.append({
                "toponym_id": f"{name}@{lang}",
                # Attested at every snapshot the name appears in: definitely
                # in use from its first to its last attestation, possibly from
                # the preceding census to the following one.
                "timespans": bounded(
                    start_earliest=pred.get(min(ys)),
                    start_latest=min(ys),
                    end_earliest=max(ys),
                    end_latest=succ[max(ys)],
                ),
            })

        # Title / ccodes from the most recent snapshot.
        latest = snaps[-1]
        doc = {
            "place_id": place_id,
            "namespace": level.namespace,
            "title": latest.name,
            "toponyms": toponyms,
            "geometries": geometries,
            "types": [{
                "identifier": level.type_identifier,
                "label": level.namespace,
                "sourceLabel": latest.unittype or level.type_source_label,
            }],
            "ccodes": list(level.ccodes),
            "boundary": level.boundary,
        }
        area = compute_area_km2(latest.geom_wgs)
        if area:
            doc["area_km2"] = round(area, 2)
        yield doc


# ── Staging ──────────────────────────────────────────────────────────────────
def _clear_extract(namespace: str) -> None:
    """Truncate the extract JSONL so re-runs are idempotent (``write_staged_place_doc``
    appends — see the PeriodO re-run lesson)."""
    from processing.settings import STAGED_BASE_DIR
    extract = Path(STAGED_BASE_DIR) / namespace / "extract" / "places.jsonl"
    if extract.exists():
        extract.unlink()


def stage_level(namespace: str) -> None:
    """Stage one GBHGIS boundary level to the extract JSONL + geom store."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    level = LEVELS[namespace]
    print("=" * 80)
    print(f"GBHGIS {level.dataset_name} — STAGING ({namespace}, SN {level.study_sn})")
    print("=" * 80)

    zip_path = _find_zip(level.namespace, level.study_sn)
    print(f"Reading {zip_path}")
    _clear_extract(namespace)

    staged = skipped = 0
    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, namespace) as gsw:
        configure_module_writer(gsw)
        try:
            for doc in build_docs(level, zip_path):
                try:
                    write_staged_place_doc(namespace=namespace, doc=doc)
                    staged += 1
                except Exception as e:  # noqa: BLE001 — one bad unit must not abort
                    print(f"  ERROR {doc.get('place_id')}: {e}")
                    skipped += 1
        finally:
            configure_module_writer(None)

    print(f"\nComplete. Units staged: {staged:,}  Skipped: {skipped:,}  "
          f"Geometry snapshots in VAST store: {gsw.count:,}")
