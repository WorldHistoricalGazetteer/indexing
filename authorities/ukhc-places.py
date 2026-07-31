# authorities/ukhc-places.py
"""
Index UK Historic Counties from the Historic County Borders Project.

Source : https://county-borders.co.uk/  (Historic Counties Trust)
License: free for personal, educational, non-commercial and commercial use,
         with attribution to the Historic County Borders Project.

The dataset is the 92 historic counties of the UK as WGS84 polygons
(``UKDefinitionA`` = whole counties; Yorkshire / Lincolnshire are single
counties rather than split into ridings / parts). Each county becomes one
``places`` document keyed by its stable Historic Counties Standard code
(``HCS_CODE``), e.g. ``ukhc:YRK`` (Yorkshire), ``ukhc:LNC`` (Lincolnshire).

These are reference/containment geographies: they carry no per-county dates in
the source, so they are left untemporal (they surface in non-temporal search and
serve as ``contained_in`` regions for spatial-containment queries). They are
marked ``boundary: "historic-county"`` for the Space filter, and are a *regional*
(UK-only) coverage source — deliberately not in ``GLOBAL_COVERAGE_NAMESPACES``.

Reads the shapefile straight out of the configured ``*.zip`` (no manual unzip)
and stages to ``{STAGED_BASE_DIR}/ukhc/extract/places.jsonl`` plus the geom store,
exactly like the other staging-aware authority scripts. Run standalone:

    python -m authorities.ukhc-places
"""
import zipfile
from pathlib import Path

from processing.helpers import (
    enrich_geometry,
    compute_area_km2,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR, AUTHORITIES
from processing.temporal import lifespan

NAMESPACE = "ukhc"
UKHC_CONFIG = next((a for a in AUTHORITIES if a["namespace"] == NAMESPACE), None)

# Validity window applied to every county polygon and toponym.
#
#   end   1974 — the Local Government Act 1972 (in force 1 April 1974) reorganised
#                local government in England & Wales, the watershed that ended the
#                historic counties' administrative validity and these boundaries.
#                (Scotland's equivalent reform took effect 1975 and NI's 1973; 1974
#                is used as the single representative terminus.) Asserted for all.
#   start 1542 — only for the 13 Welsh counties, which were created by the Tudor
#                "Laws in Wales" Acts (1535 / 1542); before that Wales was not
#                shired into these counties. Every other historic county (English
#                shires, Scottish sheriffdoms, Irish counties) long predates any
#                single datable origin, so its start is left OPEN.
#
# The ES timespan schema supports only integer-year ``in`` bounds. Adjust here if
# a different convention is preferred.
HISTORIC_COUNTY_END = 1974
WELSH_COUNTY_START = 1542
WELSH_HCS_CODES = frozenset({
    "AGL", "BRN", "CRD", "CRM", "CRN", "DBH", "FLT",   # Anglesey … Flintshire
    "GLM", "MNM", "MRN", "MTG", "PMB", "RDN",          # Glamorgan … Radnorshire
})


def _timespans_for(code: str) -> list[dict]:
    """End-1974 for every county; Welsh counties also carry a 1542 start.

    place#164 closure rule: a county with only an end could never test as
    *definitely* alive at any year, because the definite test needs an upper
    bound on the start — plainly wrong for a county that existed in 1973.
    :func:`~processing.temporal.lifespan` supplies ``start.latest = end`` where
    no real start is known; the Welsh counties keep their real 1542 start.
    """
    if code in WELSH_HCS_CODES:
        return lifespan(WELSH_COUNTY_START, HISTORIC_COUNTY_END)
    return lifespan(end=HISTORIC_COUNTY_END)


def _find_zip() -> Path:
    """Locate the configured county-borders zip in the ukhc data directory."""
    ukhc_dir = Path(DATA_DIR) / "authorities" / NAMESPACE
    if UKHC_CONFIG and UKHC_CONFIG.get("files"):
        name = UKHC_CONFIG["files"][0].get("name")
        if name and (ukhc_dir / name).exists():
            return ukhc_dir / name
    zips = sorted(ukhc_dir.glob("*.zip"))
    if not zips:
        raise FileNotFoundError(
            f"No county-borders zip found in {ukhc_dir} "
            f"(run: python -m processing.fetch_authorities -n {NAMESPACE})"
        )
    return zips[0]


def _read_counties(zip_path: Path):
    """Yield ``(properties, geojson_geometry)`` for each county in the zip's shapefile.

    Uses pyshp (pure-Python; no GDAL) and reads the .shp/.dbf/.shx straight from
    the zip via in-memory buffers. The configured file is the WGS84 ("WG84")
    variant, so coordinates are already lon/lat — no reprojection is needed (and
    pyshp does none). pyshp is present in both the CRC and pitt ``whg`` envs.
    """
    import io
    import shapefile  # pyshp

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

        def _member(ext: str) -> str | None:
            hits = [n for n in names if n.lower().endswith(ext)]
            return hits[0] if hits else None

        shp_n, dbf_n, shx_n = _member(".shp"), _member(".dbf"), _member(".shx")
        if not shp_n or not dbf_n:
            raise FileNotFoundError(f"Missing .shp/.dbf inside {zip_path}")

        reader = shapefile.Reader(
            shp=io.BytesIO(zf.read(shp_n)),
            dbf=io.BytesIO(zf.read(dbf_n)),
            shx=io.BytesIO(zf.read(shx_n)) if shx_n else None,
        )
        for sr in reader.iterShapeRecords():
            geom = sr.shape.__geo_interface__
            if not geom or not geom.get("coordinates"):
                continue
            yield sr.record.as_dict(), geom


def process_county(props: dict, geometry: dict) -> dict | None:
    """Build a `places` document for one historic county."""
    name = str(props.get("NAME") or props.get("COUNTY") or "").strip()
    code = str(props.get("HCS_CODE") or "").strip()
    if not name or not code:
        return None

    place_id = f"{NAMESPACE}:{code}"

    timespans = _timespans_for(code)

    # Only the canonical full county name is a safe toponym. The short ``COUNTY``
    # form (e.g. "York" for Yorkshire, "Aberdeen" for Aberdeenshire) would shadow
    # the like-named cities, so it is deliberately omitted.
    toponyms = [{"toponym_id": f"{name}@en", "timespans": timespans}]

    geom_entry = enrich_geometry(geometry, timespans=timespans, geom_key=f"{place_id}_0")
    area = compute_area_km2(geometry)

    doc = {
        "place_id": place_id,
        "namespace": NAMESPACE,
        "title": name,
        "toponyms": toponyms,
        "geometries": [geom_entry] if geom_entry else [],
        "types": [{
            "identifier": "historic-county",
            "label": "ukhc",
            "sourceLabel": "historic county",
        }],
        "ccodes": ["GB"],
        "boundary": "historic-county",
    }
    if area:
        doc["area_km2"] = round(area, 2)
    return doc


def stage_ukhc() -> None:
    """Stage all UK historic counties to the extract + geom store."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    print("=" * 80)
    print("UK HISTORIC COUNTIES STAGING")
    print("=" * 80)

    zip_path = _find_zip()
    print(f"Reading historic counties from {zip_path}")

    staged = skipped = 0
    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, f"{NAMESPACE}_counties") as gsw:
        configure_module_writer(gsw)
        try:
            for props, geometry in _read_counties(zip_path):
                try:
                    doc = process_county(props, geometry)
                    if not doc:
                        skipped += 1
                        continue
                    write_staged_place_doc(namespace=NAMESPACE, doc=doc)
                    staged += 1
                except Exception as e:  # noqa: BLE001 — one bad feature must not abort the run
                    print(f"  ERROR {props.get('HCS_CODE')}: {e}")
                    skipped += 1
        finally:
            configure_module_writer(None)

    print(f"\nComplete. Staged: {staged:,}  Skipped: {skipped:,}  "
          f"Geometries in VAST store: {gsw.count:,}")


if __name__ == "__main__":
    stage_ukhc()
