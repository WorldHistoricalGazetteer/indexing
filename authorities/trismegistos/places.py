# authorities/trismegistos/places.py

"""
Stage Trismegistos Geo places to the staged extract directory.

Reads from the pre-built SQLite database (tm_geo.db) produced by
build_database.py. Each geo record with valid coordinates becomes a
place document; georelations are mapped to relations (WHG authorities)
and links (external partners).

Output: ``{STAGED_BASE_DIR}/tm/extract/places.jsonl``

ES indexing for this authority happens later via ``index_from_stage`` —
this script no longer talks to Elasticsearch.

Records: ~24K places with coordinates (out of ~65K geo entries total).
"""

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

from processing.helpers import enrich_geometry, write_staged_place_doc

NAMESPACE = "tm"
DIR = Path(__file__).resolve().parent
DB_FILE = DIR / "tm_geo.db"

# -------------------------------------------------------------------------
# Country name → ISO 3166-1 alpha-2 mapping
# Covers every country value in the TM database (including uncertain "?")
# -------------------------------------------------------------------------
COUNTRY_TO_CCODE: dict[str, str] = {
    "Afghanistan": "AF",
    "Albania": "AL",
    "Algeria": "DZ",
    "Armenia": "AM",
    "Austria": "AT",
    "Azerbaijan": "AZ",
    "Bahrain": "BH",
    "Belgium": "BE",
    "Bosnia and Herzegovina": "BA",
    "Bulgaria": "BG",
    "Cameroon": "CM",
    "Chad": "TD",
    "China": "CN",
    "Croatia": "HR",
    "Cyprus": "CY",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Djibouti": "DJ",
    "Egypt": "EG",
    "Eritrea": "ER",
    "Ethiopia": "ET",
    "France": "FR",
    "Georgia": "GE",
    "Germany": "DE",
    "Greece": "GR",
    "Hungary": "HU",
    "India": "IN",
    "Iran": "IR",
    "Iraq": "IQ",
    "Ireland": "IE",
    "Israel": "IL",
    "Italy": "IT",
    "Jordan": "JO",
    "Kazakhstan": "KZ",
    "Kosovo": "XK",
    "Kuwait": "KW",
    "Kyrgyzstan": "KG",
    "Lebanon": "LB",
    "Libya": "LY",
    "Luxembourg": "LU",
    "Malta": "MT",
    "Moldova": "MD",
    "Montenegro": "ME",
    "Morocco": "MA",
    "Nepal": "NP",
    "Netherlands": "NL",
    "Niger": "NE",
    "Nigeria": "NG",
    "North Macedonia": "MK",
    "Norway": "NO",
    "Oman": "OM",
    "Pakistan": "PK",
    "Palestine": "PS",
    "Poland": "PL",
    "Portugal": "PT",
    "Qatar": "QA",
    "Romania": "RO",
    "Russia": "RU",
    "Saudi Arabia": "SA",
    "Senegal": "SN",
    "Serbia": "RS",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "Somalia": "SO",
    "South Africa": "ZA",
    "Spain": "ES",
    "Sri Lanka": "LK",
    "Sudan": "SD",
    "Sweden": "SE",
    "Switzerland": "CH",
    "Syria": "SY",
    "Tajikistan": "TJ",
    "Tanzania": "TZ",
    "Tunisia": "TN",
    "Turkey": "TR",
    "Turkmenistan": "TM",
    "Uganda": "UG",
    "Ukraine": "UA",
    "United Arab Emirates": "AE",
    "United Kingdom": "GB",
    "Uzbekistan": "UZ",
    "Yemen": "YE",
}

# -------------------------------------------------------------------------
# TM status → type identifier normalisation
# -------------------------------------------------------------------------
# The status field contains free-text type descriptions, sometimes with
# qualifiers like "village: kome", "city: oppidum", etc. We extract a
# canonical identifier from the first word/phrase.
STATUS_TYPE_MAP: dict[str, str] = {
    "city": "city",
    "village": "village",
    "topos": "topos",
    "kleros": "kleros",
    "fundus": "fundus",
    "people": "people",
    "church": "church",
    "monastery": "monastery",
    "region": "region",
    "island": "island",
    "nome": "nome",
    "fort": "fort",
    "quarry": "quarry",
    "mine": "mine",
    "port": "port",
    "harbour": "port",
    "temple": "temple",
    "road": "road",
    "canal": "canal",
    "mountain": "mountain",
    "oasis": "oasis",
    "desert": "desert",
    "well": "well",
    "lake": "lake",
    "river": "river",
    "spring": "spring",
    "bath": "bath",
    "camp": "camp",
    "customs": "customs",
    "market": "market",
    "station": "station",
    "district": "district",
    "province": "province",
    "amphitheatre": "amphitheatre",
    "military": "military",
    "border": "border",
    "garden": "garden",
    "farm": "farm",
    "tower": "tower",
    "circus": "circus",
    "gate": "gate",
    "bridge": "bridge",
}

# Script/language tag for non-Latin name fields
NAME_FIELD_LANGS = {
    "greek_unicode": "grc",     # Ancient Greek
    "coptic_unicode": "cop",    # Coptic
    "egyptian_unicode": "egy",  # Ancient Egyptian
}


def parse_coordinates(coord_str: str) -> tuple[float, float] | None:
    """Parse TM coordinate string (lat,lon) → (lon, lat) for GeoJSON.

    Returns (lon, lat) tuple or None if unparseable.
    """
    if not coord_str:
        return None
    parts = coord_str.split(",")
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
    except ValueError:
        return None

    # Basic sanity check
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    return (lon, lat)


def parse_status(status: str) -> dict | None:
    """Parse TM status string into a type dict.

    Returns: {identifier, label, sourceLabel} or None.
    """
    if not status:
        return None

    # Clean trailing '?' marker
    clean = status.rstrip("?").strip()

    # Try to match the first word against known types
    first_word = clean.split(":")[0].split("(")[0].strip().lower()
    # Also try stripping a leading question mark word
    first_word = first_word.rstrip("?").strip()

    identifier = STATUS_TYPE_MAP.get(first_word)
    if not identifier:
        # Try matching any key as a substring
        for key, val in STATUS_TYPE_MAP.items():
            if key in clean.lower():
                identifier = val
                break

    if not identifier:
        identifier = "place"

    return {
        "identifier": identifier,
        "label": "trismegistos",
        "sourceLabel": status,
    }


def parse_name_variants(name_str: str) -> list[str]:
    """Split a TM name field that may contain variants separated by ' - '.

    Also handles parenthetical alternates. TM uses two patterns:
    - Latin: "Aegae - Aegaeae" (simple variants) and "(var. Fryx)" (strip)
    - Egyptian: "Pȝy-ỉw-wʿb (Pr-ỉw-wʿb - Pr-wʿb)" (variants inside parens)

    Returns a deduplicated list of non-empty name strings.
    """
    if not name_str:
        return []

    # First extract parenthetical groups that contain variant separators " - "
    # These are additional variants, not qualifiers to strip
    extra_variants: list[str] = []
    def extract_paren_variants(m):
        inner = m.group(1)
        if inner.startswith("var."):
            return ""  # Strip "(var. ...)" qualifiers entirely
        elif " - " in inner:
            # Parenthetical contains variants — extract them
            for part in inner.split(" - "):
                part = part.strip().rstrip("?")
                if part:
                    extra_variants.append(part)
            return ""  # Remove from main string
        else:
            return ""  # Strip other parentheticals (location qualifiers)

    cleaned = re.sub(r"\(([^)]*)\)", extract_paren_variants, name_str)

    # Split the remaining text on " - "
    parts = re.split(r"\s+-\s+", cleaned)

    names = []
    seen: set[str] = set()
    for part in parts + extra_variants:
        part = part.strip().rstrip("?").strip()
        if part and part not in seen:
            seen.add(part)
            names.append(part)

    return names


def country_to_ccode(country: str) -> str | None:
    """Map TM country name to ISO 3166-1 alpha-2 code."""
    if not country:
        return None
    # Strip trailing '?' for uncertain attributions
    clean = country.rstrip("?").strip()
    return COUNTRY_TO_CCODE.get(clean)


def build_place_doc(row: dict, relations: list[tuple[str, str]]) -> dict:
    """Build an ES place document from a TM geo row + its georelations.

    Args:
        row: dict from the geo table
        relations: list of (partner, partner_id) tuples from georelations

    Returns:
        Place document dict. Geometry is optional when coordinates are missing.
    """
    coords = parse_coordinates(row["coordinates"])
    tm_id = row["tm_geo_id"]
    place_id = f"tm:{tm_id}"

    # ---- Title: prefer standard_name, then latin_name, then full_name ----
    title = row["standard_name"] or row["latin_name"] or row["full_name"]
    if not title:
        title = f"TM Geo {tm_id}"

    # ---- Toponyms ----
    toponyms = []
    seen_ids: set[str] = set()

    # Build timespans from TM dates (negative = BCE, positive = CE)
    begin = row["begin_date"]
    end = row["end_date"]
    timespans = None
    if begin != 0 or end != 0:
        ts: dict = {}
        if begin != 0:
            ts["start"] = {"in": begin}
        if end != 0:
            ts["end"] = {"in": end}
        timespans = [ts]

    def add_toponym(name: str, lang: str = "und"):
        """Add a toponym if not already seen."""
        tid = f"{name}@{lang}"
        if tid not in seen_ids:
            seen_ids.add(tid)
            entry: dict = {"toponym_id": tid}
            if timespans:
                entry["timespans"] = timespans
            toponyms.append(entry)

    # Standard name (primary)
    if row["standard_name"]:
        add_toponym(row["standard_name"], "und")

    # Latin name variants
    if row["latin_name"]:
        for name in parse_name_variants(row["latin_name"]):
            add_toponym(name, "la")

    # Full name often duplicates standard but may have extra context
    if row["full_name"]:
        # Full name has format "Country, Region - Name (Modern)"
        # Extract just the meaningful name part after the last " - "
        parts = row["full_name"].split(" - ")
        if len(parts) > 1:
            for part in parts[1:]:
                clean = re.sub(r"\([^)]*\)", "", part).strip()
                if clean:
                    add_toponym(clean, "und")

    # Greek Unicode names
    for field, lang in NAME_FIELD_LANGS.items():
        raw = row.get(field, "")
        if raw and raw != "a":  # Some records have placeholder "a"
            for name in parse_name_variants(raw):
                add_toponym(name, lang)

    # Ethnicon (inhabitants' name) — useful for search but different kind
    if row["ethnicon"]:
        for name in parse_name_variants(row["ethnicon"]):
            add_toponym(name, "und")

    if not toponyms:
        add_toponym(title, "und")

    # ---- Document ----
    doc: dict = {
        "place_id": place_id,
        "title": title,
        "toponyms": toponyms,
    }

    # ---- Geometry (optional) ----
    if coords:
        lon, lat = coords
        # Build the entry via enrich_geometry like every other authority: it is
        # what rounds the coordinates, and what writes `bounds` and `has_geom`.
        # Hand-rolling the dict here left all 24,538 tm geometries without a
        # `bounds` (place#145) — the field the gateway's region builder uses for
        # its bbox gate, and the one recompute_h3_index reads to decide whether a
        # feature is sub-cell. h3 fields are added later by `h3_stage`.
        geom_entry = enrich_geometry({"type": "Point", "coordinates": [lon, lat]},
                                     timespans=timespans or None)
        if geom_entry:
            doc["geometries"] = [geom_entry]

    # ---- Country codes ----
    ccode = country_to_ccode(row["country"])
    if ccode:
        doc["ccodes"] = [ccode]

    # ---- Types ----
    type_entry = parse_status(row["status"])
    if type_entry:
        doc["types"] = [type_entry]
    else:
        doc["types"] = [{"identifier": "place", "label": "trismegistos", "sourceLabel": "tm-geo"}]

    # ---- Relations & Links from georelations ----
    # WHG authority partners become relations (sameAs); others become links
    rel_list = []
    link_list = []

    for partner, partner_id in relations:
        if partner == "pleiades":
            rel_list.append({
                "relation_type": "sameAs",
                "related_place_id": f"pl:{partner_id}",
                "label": "Pleiades",
            })
        elif partner == "geonames":
            rel_list.append({
                "relation_type": "sameAs",
                "related_place_id": f"gn:{partner_id}",
                "label": "GeoNames",
            })
        elif partner == "wikidata":
            rel_list.append({
                "relation_type": "sameAs",
                "related_place_id": f"wd:{partner_id}",
                "label": "Wikidata",
            })
        else:
            # External links for non-WHG partners
            link_list.append({
                "type": partner,
                "identifier": str(partner_id),
            })

    if rel_list:
        doc["relations"] = rel_list
    if link_list:
        doc["links"] = link_list

    return doc


def load_georelations(conn: sqlite3.Connection) -> dict[int, list[tuple[str, str]]]:
    """Load all georelations into a dict keyed by tm_geo_id."""
    cur = conn.cursor()
    cur.execute("SELECT tm_geo_id, partner, partner_id FROM georelations ORDER BY tm_geo_id")

    rels: dict[int, list[tuple[str, str]]] = {}
    for tm_id, partner, partner_id in cur:
        rels.setdefault(tm_id, []).append((partner, partner_id))

    return rels


def stage_trismegistos():
    """Read TM SQLite database and write staged place docs."""
    if not DB_FILE.exists():
        print(f"ERROR: Database not found: {DB_FILE}")
        print("Run 'python -m authorities.trismegistos.build_database' first.")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row

    print("Loading georelations...")
    georelations = load_georelations(conn)
    print(f"  {sum(len(v) for v in georelations.values()):,} links for "
          f"{len(georelations):,} places")

    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM geo
        WHERE country != 'ghost name'
    """)
    total = cur.fetchone()[0]
    print(f"\nStaging {total:,} TM places")

    cur.execute("""
        SELECT * FROM geo
        WHERE country != 'ghost name'
        ORDER BY tm_geo_id
    """)

    staged = 0
    skipped = 0
    with_geometry = 0
    without_geometry = 0
    start_time = time.time()

    for row in cur:
        row_dict = dict(row)
        tm_id = row_dict["tm_geo_id"]

        try:
            rels = georelations.get(tm_id, [])
            doc = build_place_doc(row_dict, rels)
            if doc.get("geometries"):
                with_geometry += 1
            else:
                without_geometry += 1

            write_staged_place_doc(NAMESPACE, doc)
            staged += 1

            if staged % 1000 == 0:
                elapsed = time.time() - start_time
                rate = staged / elapsed if elapsed > 0 else 0
                sys.stdout.write(
                    f"\r  {staged:,}/{total:,} staged ({rate:.0f}/s)"
                )
                sys.stdout.flush()

        except Exception as e:
            print(f"\nError processing TM {tm_id}: {e}")
            skipped += 1
            continue

    conn.close()
    elapsed = time.time() - start_time

    print(f"\n\n{'=' * 60}")
    print(f"  TRISMEGISTOS STAGING COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Staged:   {staged:,}")
    print(f"  Skipped:  {skipped:,}")
    print(f"  With geometry:    {with_geometry:,}")
    print(f"  Without geometry: {without_geometry:,}")
    print(f"  Time:     {elapsed:.0f}s ({elapsed / 60:.1f}m)")
    if elapsed > 0:
        print(f"  Rate:     {staged / elapsed:.0f} docs/s")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage Trismegistos Geo places")
    args = parser.parse_args()

    print("=" * 60)
    print("TRISMEGISTOS GEO PLACES STAGING")
    print("=" * 60)
    print(f"Source: {DB_FILE}")
    print()

    stage_trismegistos()
    print("COMPLETE")




