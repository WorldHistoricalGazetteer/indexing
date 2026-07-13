# typesystem/extract_wikidata_p1014.py

"""
Extract the Wikidata → Getty AAT crosswalk (property **P1014**) from the full
Wikidata entity dump.

P1014 ("Getty AAT ID") lives on *type-concept* entities — e.g. ``Q515`` (city)
→ ``300008389`` (AAT "cities") — which the place-ingest geographic filter
discards (they have no coordinates / geoshape, so they are not places). The
crosswalk we need to fold AAT ids onto ``wd`` place docs is therefore physically
present in the dump but dropped at ingest.

This module owns the *raw* extraction, used two ways so the parse logic is
written once:

  1. **As a side-output of the one-pass place-ingest scan**
     (``authorities/wikidata-places.py``) — free, since that pass already
     decompresses the ~148 GB dump. See ``extract_p1014_ids``.
  2. **As a standalone one-pass scan** (this module's ``__main__``) — for a
     between-rebuild top-up without a full Wikidata re-ingest.

Both write a JSONL crosswalk (``{"qid": "Q515", "aat_id": "300008389"}``) that
``typesystem.aat_mapper wikidata`` consumes to populate each ``wikidata.json``
entry's ``aat_mapping``. This replaces the retired WDQS/SPARQL P1014 lookup,
which was rate-limited and outage-prone.

Usage (standalone scan — run via Slurm, NOT on a login node):

    python -m typesystem.extract_wikidata_p1014                 # default dump + out
    python -m typesystem.extract_wikidata_p1014 --dump PATH --out PATH
    pigz -dc PATH/latest-all.json.gz | \
        python -m typesystem.extract_wikidata_p1014 --dump -    # external parallel gunzip
"""

import gzip
import sys

import orjson

from processing.settings import DATA_DIR, WIKIDATA_P1014_FILE

# Default dump location (matches authorities/wikidata-places.py).
WIKIDATA_DUMP = f"{DATA_DIR}/wikidata/latest-all/latest-all.json.gz"


def extract_p1014_ids(claims):
    """Return the list of Getty AAT IDs (P1014 values) on an entity's claims.

    P1014 is a plain string value (``"300008389"``). Most concepts carry exactly
    one; a few carry several. Returns ``[]`` when the property is absent.
    """
    out = []
    for claim in claims.get("P1014") or ():
        try:
            val = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
    return out


def _open_dump(file_path):
    """Open the dump for binary line iteration. ``"-"`` reads stdin (already
    decompressed, e.g. piped from ``pigz -dc``); a ``.gz`` path is gunzipped;
    anything else is read raw."""
    if file_path == "-":
        return sys.stdin.buffer
    if file_path.endswith(".gz"):
        return gzip.open(file_path, "rb")
    return open(file_path, "rb")


def iter_p1014_entities(file_path):
    """Yield ``(qid, [aat_id, …])`` for every dump entity carrying a P1014 claim.

    A cheap byte pre-filter (``"P1014"`` present in the raw line) skips the
    JSON parse for the >99.9% of lines with no Getty AAT ID.
    """
    fh = _open_dump(file_path)
    try:
        for line in fh:
            line = line.strip()
            if not line or line in (b"[", b"]"):
                continue
            if line.endswith(b","):
                line = line[:-1]
            if b'"P1014"' not in line:
                continue
            try:
                entity = orjson.loads(line)
            except Exception:
                continue
            qid = entity.get("id")
            if not qid:
                continue
            aat_ids = extract_p1014_ids(entity.get("claims", {}))
            if aat_ids:
                yield qid, aat_ids
    finally:
        if fh is not sys.stdin.buffer:
            fh.close()


def scan_dump(file_path, out_file):
    """One-pass scan of the dump → JSONL crosswalk. Returns the row count."""
    n_entities = 0
    n_rows = 0
    with open(out_file, "wb") as out:
        for qid, aat_ids in iter_p1014_entities(file_path):
            n_entities += 1
            for aat_id in aat_ids:
                out.write(orjson.dumps({"qid": qid, "aat_id": aat_id}))
                out.write(b"\n")
                n_rows += 1
            if n_entities % 1000 == 0:
                sys.stdout.write(
                    f"\r  P1014 entities: {n_entities:,}  rows: {n_rows:,}")
                sys.stdout.flush()
    print(f"\n  Wrote {n_rows:,} P1014 rows from {n_entities:,} entities "
          f"→ {out_file}")
    return n_rows


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Extract the Wikidata P1014 (Getty AAT ID) crosswalk from the dump")
    ap.add_argument("--dump", default=WIKIDATA_DUMP,
                    help="Path to latest-all.json.gz (or '-' for decompressed stdin)")
    ap.add_argument("--out", default=WIKIDATA_P1014_FILE,
                    help="Output JSONL crosswalk path")
    args = ap.parse_args()

    print(f"Scanning {args.dump} for P1014 → {args.out}")
    scan_dump(args.dump, args.out)
