# typesystem/extract_wikidata_p279.py

"""
Extract the Wikidata **P279** (subclass-of) class hierarchy from the full
Wikidata entity dump — the input to AAT type-mapping **Pass 2** (the P279 walk).

Pass 1 (P1014, Getty AAT ID) is *saturated*: only a minority of Wikidata type
concepts carry their own Getty id. But a specific unmapped type (``Q123``) is
usually a `subclass of` (P279) a broader type that IS mapped (``Q123 → … →
Q515`` city → AAT 300008389). Walking P279 upward to the nearest P1014-mapped
ancestor recovers AAT ids for that long tail (`type-mapping-plan.md` §3.2 Pass 2).

This module extracts the raw class graph; the walk itself lives in
``typesystem.aat_mapper wikidata-p279`` (which consumes this edge list + the
P1014 crosswalk from ``extract_wikidata_p1014``).

Output JSONL: one row per entity carrying P279 —
``{"qid": "Q123", "parents": ["Q515", "Q486972"]}``.

NOTE ON COST: unlike P1014 (a rare property → the ``"P1014"`` byte pre-filter
skips ~all lines), P279 is on **millions** of class items, so the pre-filter
matches a large fraction of the dump and the JSON parse runs on most of it —
this is a *slow* (hours) one-pass scan, not the ~40 min the P1014 scan took.
Run it via Slurm on an htc node, ideally with ``pigz -dc … | … --dump -``.

Usage:

    python -m typesystem.extract_wikidata_p279                  # default dump + out
    pigz -dc DUMP | python -m typesystem.extract_wikidata_p279 --dump -
"""

import sys

import orjson

from processing.settings import WIKIDATA_P279_FILE
from typesystem.extract_wikidata_p1014 import WIKIDATA_DUMP, _open_dump


def extract_p279_parents(claims):
    """Return the list of parent class Q-ids (P279 values) on an entity's claims.

    P279 values are entity references (``mainsnak.datavalue.value.id``), unlike
    P1014's plain strings. Returns ``[]`` when the property is absent.
    """
    out = []
    for claim in claims.get("P279") or ():
        try:
            pid = claim["mainsnak"]["datavalue"]["value"]["id"]
        except (KeyError, TypeError):
            continue
        if isinstance(pid, str) and pid.startswith("Q"):
            out.append(pid)
    return out


def iter_p279_entities(file_path):
    """Yield ``(qid, [parent_qid, …])`` for every dump entity carrying P279."""
    fh = _open_dump(file_path)
    try:
        for line in fh:
            line = line.strip()
            if not line or line in (b"[", b"]"):
                continue
            if line.endswith(b","):
                line = line[:-1]
            if b'"P279"' not in line:
                continue
            try:
                entity = orjson.loads(line)
            except Exception:
                continue
            qid = entity.get("id")
            if not qid:
                continue
            parents = extract_p279_parents(entity.get("claims", {}))
            if parents:
                yield qid, parents
    finally:
        if fh is not sys.stdin.buffer:
            fh.close()


def scan_dump(file_path, out_file):
    """One-pass scan of the dump → JSONL P279 edge list. Returns the row count."""
    n_rows = 0
    with open(out_file, "wb") as out:
        for qid, parents in iter_p279_entities(file_path):
            out.write(orjson.dumps({"qid": qid, "parents": parents}))
            out.write(b"\n")
            n_rows += 1
            if n_rows % 100000 == 0:
                sys.stdout.write(f"\r  P279 classes: {n_rows:,}")
                sys.stdout.flush()
    print(f"\n  Wrote {n_rows:,} P279 class rows → {out_file}")
    return n_rows


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Extract the Wikidata P279 (subclass-of) class graph from the dump")
    ap.add_argument("--dump", default=WIKIDATA_DUMP,
                    help="Path to latest-all.json.gz (or '-' for decompressed stdin)")
    ap.add_argument("--out", default=WIKIDATA_P279_FILE,
                    help="Output JSONL edge-list path")
    args = ap.parse_args()

    print(f"Scanning {args.dump} for P279 → {args.out}")
    scan_dump(args.dump, args.out)
