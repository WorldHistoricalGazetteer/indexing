# authorities/kain_par-places.py
"""Ancient parishes & places of England & Wales, pre-1850 (place#135).

Kain & Oliver's *Historic Parishes of England and Wales* boundaries (UKDS
SN 4348, 2001), converted to a single ~28k-polygon GIS by Burton et al.
(SN 4828, 2004) and error-corrected + census-attributed by the Cambridge Group
(CAMPOP) as *1851 England and Wales census parishes, townships and places*
(SN 852232 / ReShare, 2023) — the version indexed here. ~23k parish/township/
place polygons in **OSGB National Grid (EPSG:27700)**, reprojected to WGS84.

**RESTRICTED.** Obtained under the UK Data Service End User Licence
(registration-gated); **not redistributable** (index-in-place only — no
WHG-download) and commercial use is prohibited. Encoded as
``redistributable=False`` in ``settings.AUTHORITIES`` so the inventory push
marks it non-downloadable (see ``processing.push_gazetteer_inventory``).

Each source row is one mapped unit — a place (``PLA``) within a parish (``PAR``),
typed by ``CAT`` (P=parish, T=township, C=chapelry, EP=extra-parochial, …).
One ``places`` doc per stable ``ID``, single geometry, one open-start timespan
ending at the 1851 census the geography is aligned to. These are reference /
containment geographies (``boundary="ancient-parish"``) and a *regional*
(England & Wales) source — deliberately not in ``GLOBAL_COVERAGE_NAMESPACES``.

Place the UKDS zip (or loose shapefile) in ``{DATA_DIR}/authorities/kain_par/``
(safeguarded — not auto-fetchable), then run:

    python -m authorities.kain_par-places
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Iterator

from authorities.vob_common import _reproject_to_wgs84, _titlecase_name
from processing.helpers import (
    compute_area_km2,
    enrich_geometry,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR

NAMESPACE = "kain_par"

# Boundaries are "before 1850", aligned to the 1851 census enumeration. Modelled
# with an OPEN start (ancient parishes predate any single datable origin) and an
# end at the census the geography snapshots — mirrors ukhc's open-start pattern.
PARISH_END_YEAR = 1851

# Kain & Oliver unit categories (``CAT``) → readable type. Only the well-attested
# codes are expanded; anything else keeps its raw code as the type identifier so
# nothing is mis-asserted.
_CAT_TYPE = {
    "P": "parish",
    "T": "township",
    "C": "chapelry",
    "EP": "extra-parochial place",
    "H": "hamlet",
    "B": "borough",
}


def _cat_type(cat: str) -> tuple[str, str]:
    """Return ``(identifier, sourceLabel)`` for a raw ``CAT`` code."""
    raw = (cat or "").strip()
    key = raw.upper().split(",")[0].strip()  # 'P, EP' -> 'P'; 'p' -> 'P'
    label = _CAT_TYPE.get(key)
    if label:
        return label.replace(" ", "-"), f"{label} ({raw})"
    return "ancient-parish", raw or "ancient parish"


def _find_source() -> Path:
    """Locate the placed UKDS parish zip (or a loose .shp) in the data dir."""
    ns_dir = Path(DATA_DIR) / "authorities" / NAMESPACE
    for pattern in ("*.zip", "*.shp"):
        hits = sorted(ns_dir.glob(pattern))
        if hits:
            return hits[0]
    raise FileNotFoundError(
        f"No parish shapefile/zip in {ns_dir} — download UKDS SN 852232 "
        f"(EULA-gated) and place it there."
    )


def _iter_records(source: Path) -> Iterator[tuple[dict, dict]]:
    """Yield ``(record_dict, osgb_geojson_geometry)`` from a .zip or .shp path."""
    import shapefile  # pyshp

    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            names = zf.namelist()
            shp_n = next((n for n in names if n.lower().endswith(".shp")), None)
            if not shp_n:
                raise FileNotFoundError(f"No .shp inside {source}")
            base = shp_n[:-4]
            dbf_n = next((n for n in names if n.lower() == (base + ".dbf").lower()), None)
            shx_n = next((n for n in names if n.lower() == (base + ".shx").lower()), None)
            reader = shapefile.Reader(
                shp=io.BytesIO(zf.read(shp_n)),
                dbf=io.BytesIO(zf.read(dbf_n)) if dbf_n else None,
                shx=io.BytesIO(zf.read(shx_n)) if shx_n else None,
                encoding="latin-1",
            )
            yield from _emit(reader)
    else:
        reader = shapefile.Reader(str(source), encoding="latin-1")
        yield from _emit(reader)


def _emit(reader) -> Iterator[tuple[dict, dict]]:
    for sr in reader.iterShapeRecords():
        geom = sr.shape.__geo_interface__
        if not geom or not geom.get("coordinates"):
            continue
        yield sr.record.as_dict(), geom


def build_docs(source: Path) -> Iterator[dict]:
    """Yield one ``places`` doc per parish/place polygon."""
    timespans = [{"end": {"in": PARISH_END_YEAR}}]  # open start

    for rec, osgb_geom in _iter_records(source):
        uid = rec.get("ID")
        place = _titlecase_name(str(rec.get("PLA") or "").strip())
        parish = _titlecase_name(str(rec.get("PAR") or "").strip())
        name = place or parish
        if uid is None or not name:
            continue
        wgs = _reproject_to_wgs84(osgb_geom)
        if not wgs:
            continue

        place_id = f"{NAMESPACE}:{int(uid)}"
        geom_entry = enrich_geometry(wgs, timespans=timespans, geom_key=f"{place_id}_0")
        if not geom_entry:
            continue

        # Toponyms: the mapped place, plus the containing parish when different.
        seen = set()
        toponyms = []
        for nm in (name, parish):
            if nm and nm not in seen:
                seen.add(nm)
                toponyms.append({"toponym_id": f"{nm}@en", "timespans": timespans})

        type_id, type_src = _cat_type(rec.get("CAT"))
        doc = {
            "place_id": place_id,
            "namespace": NAMESPACE,
            "title": name,
            "toponyms": toponyms,
            "geometries": [geom_entry],
            "types": [{
                "identifier": type_id,
                "label": NAMESPACE,
                "sourceLabel": type_src,
            }],
            "ccodes": ["GB"],
            "boundary": "ancient-parish",
        }
        area = compute_area_km2(wgs)
        if area:
            doc["area_km2"] = round(area, 2)
        yield doc


def _clear_extract() -> None:
    from processing.settings import STAGED_BASE_DIR
    extract = Path(STAGED_BASE_DIR) / NAMESPACE / "extract" / "places.jsonl"
    if extract.exists():
        extract.unlink()


def stage_kain_par() -> None:
    """Stage the ancient parishes to the extract JSONL + geom store."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    print("=" * 80)
    print("ANCIENT PARISHES OF ENGLAND & WALES (kain_par) — STAGING")
    print("=" * 80)

    source = _find_source()
    print(f"Reading {source}")
    _clear_extract()

    staged = skipped = 0
    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, NAMESPACE) as gsw:
        configure_module_writer(gsw)
        try:
            for doc in build_docs(source):
                try:
                    write_staged_place_doc(namespace=NAMESPACE, doc=doc)
                    staged += 1
                except Exception as e:  # noqa: BLE001 — one bad unit must not abort
                    print(f"  ERROR {doc.get('place_id')}: {e}")
                    skipped += 1
        finally:
            configure_module_writer(None)

    print(f"\nComplete. Parishes staged: {staged:,}  Skipped: {skipped:,}  "
          f"Geometries in VAST store: {gsw.count:,}")


if __name__ == "__main__":
    stage_kain_par()
