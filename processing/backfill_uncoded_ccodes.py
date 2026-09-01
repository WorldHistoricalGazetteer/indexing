#!/usr/bin/env python
"""Resolve places that ended with NO country code, using the full-BNDA tier 2.

Places that fall outside every geoBoundaries polygon got nothing, because tier 2
was keyed on "geoBoundaries lacks this country" rather than on "this place is
unresolved". 464 places in ``VI``, ``AS``, ``GU``, ``MP`` and ``BQ`` came out
uncoded on the 5 Aug 2026 corpus run — every one in a territory geoBoundaries
*does* cover, so the fallback could never fire for them.

Two causes, both answered by the widened tier:

* **Precision.** The uncoded ``AS``/``VI`` places are overwhelmingly coastal —
  capes, bays, coves, rocks, piers — whose representative point sits a few
  metres seaward. A 232-vertex outline swallowed them; a 73,663-vertex one
  correctly does not. Greater accuracy *creates* uncoded coastal places.
* **Omission.** geoBoundaries models ``BQ`` as ONE polygon covering Bonaire
  only; Saba and Sint Eustatius are absent entirely, including their own
  administrative polygons.

Scans the staged ``final/`` snapshots (all on ``/vast``, so this runs on a
compute node) and emits a ``{place_id, ccodes}`` JSONL patch for every doc that
tier 2 can now place. Apply it with::

    python -m processing.apply_ccode_patch --es-host <PROD> \\
        --patch <PATCH> --index places --execute

``apply_ccode_patch`` fills only where ``ccodes`` is empty by default, so this
cannot disturb a place the primary tier already answered.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq

from processing.ccode_enrichment import _extract_place_geometry
from processing.ccode_tiers import BndaFallbackIndex, load_full_bnda_tier
from processing.geom_store import GeomStoreReader
from processing.settings import GEOM_STORE_DIR, STAGED_BASE_DIR

SOURCE_LABEL = "un-bnda-fallback"


def _final_parquet(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / namespace / "final" / "places.parquet"


def _final_source(namespace: str) -> Path | None:
    """Return the readable ``final/`` snapshot, parquet preferred, else None.

    The parquet sidecar is **best-effort**: ``write_parquet_from_jsonl``
    returns False rather than raising when pyarrow's schema inference fails
    (ragged GeoJSON ``coordinates``, mixed LPF timespan types), leaving a
    complete canonical JSONL and no sidecar. Every other consumer in the repo
    reads "parquet if it exists, else jsonl" for exactly that reason; this
    tool required the parquet and so skipped such a namespace entirely.

    ``ukhc`` is the one namespace in that shape today, and it was silently
    excluded from the un-BNDA backfill — undiscovered by default, and
    contributing zero rows with no error when named explicitly. It is a
    coastal-boundary authority, which is precisely the population tier 2
    exists to resolve.
    """
    base = Path(STAGED_BASE_DIR) / namespace / "final"
    parquet = base / "places.parquet"
    if parquet.exists():
        return parquet
    jsonl = base / "places.jsonl"
    if jsonl.exists():
        return jsonl
    return None


def _iter_final(namespace: str) -> Iterator[dict[str, Any]]:
    path = _final_source(namespace)
    if path is None:
        return
    if path.suffix == ".parquet":
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=2000):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                yield row


def _namespaces(explicit: str | None) -> list[str]:
    if explicit:
        return [n.strip() for n in explicit.split(",") if n.strip()]
    base = Path(STAGED_BASE_DIR)
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and _final_source(p.name) is not None
        and p.name != "un"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--namespaces", help="Comma-separated; default all staged")
    ap.add_argument("--out", required=True, help="Patch JSONL to write")
    ap.add_argument("--limit", type=int, help="Stop after N resolved (testing)")
    args = ap.parse_args()

    entries = load_full_bnda_tier()
    if not entries:
        print("No BNDA source available — cannot build tier 2", file=sys.stderr)
        return 1
    fb = BndaFallbackIndex(entries)
    print(f"tier 2: {len(fb)} BNDA features", flush=True)

    try:
        reader: GeomStoreReader | None = GeomStoreReader(GEOM_STORE_DIR)
    except FileNotFoundError:
        reader = None

    selected = _namespaces(args.namespaces)

    # An explicitly-named namespace with no readable final/ must not pass as a
    # zero contribution. This tool's per-namespace line is gated on
    # uncoded>0, so a namespace that scanned nothing printed nothing at all —
    # indistinguishable from one that scanned everything and found no work.
    # That is this campaign's signature defect: absent input treated as
    # nothing-to-do.
    if args.namespaces:
        missing = [ns for ns in selected if _final_source(ns) is None]
        if missing:
            print(f"No readable final/ snapshot for: {', '.join(missing)} — "
                  f"expected places.parquet or places.jsonl under "
                  f"{Path(STAGED_BASE_DIR)}/<ns>/final/. Refusing to report a "
                  f"zero contribution for a namespace that was never read.",
                  file=sys.stderr)
            return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    totals = {"scanned": 0, "uncoded": 0, "no_geom": 0, "resolved": 0,
              "still_unresolved": 0}
    per_ns: dict[str, dict[str, int]] = {}

    with out_path.open("w", encoding="utf-8") as fh:
        for ns in selected:
            n = {"scanned": 0, "uncoded": 0, "no_geom": 0, "resolved": 0,
                 "still_unresolved": 0}
            for doc in _iter_final(ns):
                n["scanned"] += 1
                if doc.get("ccodes"):
                    continue
                n["uncoded"] += 1

                geom = _extract_place_geometry(doc, reader)
                if geom is None or geom.is_empty:
                    n["no_geom"] += 1
                    continue

                ccodes = fb.ccodes_for(geom)
                if not ccodes:
                    n["still_unresolved"] += 1
                    continue

                fh.write(json.dumps({
                    "place_id": doc.get("place_id"),
                    "ccodes": ccodes,
                    "source": SOURCE_LABEL,
                }, ensure_ascii=True) + "\n")
                n["resolved"] += 1

                if args.limit and totals["resolved"] + n["resolved"] >= args.limit:
                    break

            per_ns[ns] = n
            for k in totals:
                totals[k] += n[k]
            if n["uncoded"]:
                print(f"  {ns:10s} scanned={n['scanned']:>10,d} "
                      f"uncoded={n['uncoded']:>9,d} "
                      f"no_geom={n['no_geom']:>9,d} "
                      f"RESOLVED={n['resolved']:>8,d} "
                      f"still_unresolved={n['still_unresolved']:>8,d}",
                      flush=True)
            elif not n["scanned"]:
                # Distinguish "read it, nothing to do" from "never read it".
                # Only the second is a defect, and without this line they look
                # identical in the log.
                print(f"  {ns:10s} scanned=         0  "
                      f"** NOTHING READ — no final/ snapshot **", flush=True)

    print("\n" + "=" * 78)
    print(f"scanned          {totals['scanned']:>12,d}")
    print(f"uncoded          {totals['uncoded']:>12,d}")
    print(f"  no geometry    {totals['no_geom']:>12,d}  (unresolvable)")
    print(f"  RESOLVED       {totals['resolved']:>12,d}  -> {out_path}")
    print(f"  still uncoded  {totals['still_unresolved']:>12,d}  "
          f"(genuinely outside every country: open ocean, Antarctica)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
