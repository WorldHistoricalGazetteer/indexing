#!/usr/bin/env python
"""Splice a freshly re-extracted namespace's NAMES into its existing ``final``
snapshot, leaving every enrichment field untouched.

Usage
-----
    python -m processing.refresh_staged_names --namespace ukhc            # report
    python -m processing.refresh_staged_names --namespace ukhc --execute

Why this exists
---------------
Sometimes an authority's *name inventory* changes and nothing else does. The
``ukhc`` counties gained 1,017 alternative names (place#204) from exactly the
same 92 polygons: the geometry, the H3 cover, the country codes and the geom
store keys are all bit-identical to what is already staged and already live.

The documented incremental path would re-run the whole stage chain — h3_stage →
h3_merge → ccode_merge — to rebuild a ``final`` snapshot that differs from the
existing one only in two fields. ``h3_stage`` is driven by a run manifest and is
part of the orchestrated rebuild machinery, so that is a lot of moving parts for
a change that touches no geometry, and every one of them is a chance to lose the
enrichment that is already correct. (Recomputing H3 also means re-reading the
geom store, and an h3_cover that came back subtly different would be a silent
regression in spatial containment.)

So: re-run the authority's extract, and copy ONLY the name-bearing fields across
into the snapshot that already has everything else. The enrichment is not
recomputed because it does not need to be — and the guard below refuses to run
if that assumption is false.

Guards (all fatal, none of them optional)
-----------------------------------------
* the two snapshots must describe **exactly the same place_ids** — no additions,
  no disappearances. A namespace that actually gained or lost places needs the
  real stage chain, not this;
* every doc's **geometry must be unchanged** field-for-field except for the
  enrichment the extract stage cannot know (``h3_cover``, ``h3_centroid``,
  ``geom_ref``…). A geometry edit means H3 must be recomputed, so the tool stops;
* the rewritten snapshot must keep every enrichment key it started with.

Dry-run by default: prints what would change and writes nothing.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from processing.settings import STAGED_BASE_DIR

#: The fields this tool is allowed to move. Names, and the links that ride with
#: them — nothing spatial, nothing temporal, nothing derived.
NAME_FIELDS = ("toponyms", "title", "links")

#: Per-geometry keys that legitimately differ between the two stages, so a
#: difference in them must NOT count as "the geometry changed":
#:
#:   h3_cover / h3_centroid  the extract stage cannot compute them — that is the
#:                           whole point of h3_stage;
#:   hull                    the reverse: an extract-stage artefact (the convex
#:                           hull H3 falls back to), consumed and dropped
#:                           downstream.
#:
#: Everything else IS compared, deliberately including ``geom_ref`` (the geom
#: store key, i.e. the identity of the polygon itself), ``bounds`` and
#: ``repr_point`` — all three are produced at extract time, so comparing them
#: turns this into a real check on the geometry rather than a formality.
STAGE_ONLY_GEOM_KEYS = {"h3_cover", "h3_centroid", "hull"}


def _read_jsonl(path: Path) -> dict[str, dict]:
    docs: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            pid = doc.get("place_id")
            if pid:
                docs[pid] = doc
    return docs


def _drop_nulls(value):
    """Recursively remove null-valued keys.

    Downstream stages round-trip a doc through a schema that materialises absent
    optional fields as nulls, so ``{"start": {"latest": 1974}}`` comes back as
    ``{"start": {"latest": 1974, "in": None}}``. Those assert the same thing, and
    treating them as a difference would make the guard fire on every doc — which
    is exactly what it did before this normalisation.
    """
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


def _comparable_geoms(doc: dict) -> list[dict]:
    """A doc's geometries, normalised so only a real difference shows."""
    out = []
    for g in (doc.get("geometries") or []):
        out.append(_drop_nulls(
            {k: v for k, v in g.items() if k not in STAGE_ONLY_GEOM_KEYS}))
    return out


def refresh(namespace: str, execute: bool) -> int:
    base = Path(STAGED_BASE_DIR) / namespace
    extract_path = base / "extract" / "places.jsonl"
    final_path = base / "final" / "places.jsonl"
    for p in (extract_path, final_path):
        if not p.exists():
            print(f"ERROR: {p} does not exist", file=sys.stderr)
            return 2

    fresh = _read_jsonl(extract_path)
    staged = _read_jsonl(final_path)
    print(f"{namespace}: extract={len(fresh):,} docs  final={len(staged):,} docs")

    only_fresh = sorted(set(fresh) - set(staged))
    only_staged = sorted(set(staged) - set(fresh))
    if only_fresh or only_staged:
        print(f"ERROR: place_id sets differ — {len(only_fresh)} only in extract, "
              f"{len(only_staged)} only in final. This tool refreshes NAMES on an "
              f"unchanged place set; run the full stage chain instead.", file=sys.stderr)
        for pid in (only_fresh[:5] + only_staged[:5]):
            print(f"    {pid}", file=sys.stderr)
        return 2

    moved = 0
    changed_geom = []
    for pid, staged_doc in staged.items():
        fresh_doc = fresh[pid]
        if _comparable_geoms(fresh_doc) != _comparable_geoms(staged_doc):
            changed_geom.append(pid)
    if changed_geom:
        print(f"ERROR: {len(changed_geom)} doc(s) have a CHANGED geometry, so H3 "
              f"must be recomputed — run the full stage chain.", file=sys.stderr)
        for pid in changed_geom[:5]:
            print(f"    {pid}", file=sys.stderr)
        return 2

    before_keys = {k for d in staged.values() for k in d}
    topo_before = sum(len(d.get("toponyms") or []) for d in staged.values())
    for pid, staged_doc in staged.items():
        fresh_doc = fresh[pid]
        for field in NAME_FIELDS:
            if field in fresh_doc:
                if staged_doc.get(field) != fresh_doc[field]:
                    moved += 1
                staged_doc[field] = fresh_doc[field]
            elif field in staged_doc:
                # The re-extract deliberately dropped it (e.g. links removed).
                del staged_doc[field]
                moved += 1
    topo_after = sum(len(d.get("toponyms") or []) for d in staged.values())
    after_keys = {k for d in staged.values() for k in d}
    lost = before_keys - after_keys - set(NAME_FIELDS)
    if lost:
        print(f"ERROR: refresh would drop enrichment field(s): {sorted(lost)}",
              file=sys.stderr)
        return 2

    print(f"  field updates: {moved:,}")
    print(f"  toponyms: {topo_before:,} → {topo_after:,} "
          f"({topo_after - topo_before:+,})")
    if not execute:
        print("\nDRY RUN — nothing written. Re-run with --execute to apply.")
        return 0

    backup = final_path.with_suffix(".jsonl.bak")
    shutil.copy2(final_path, backup)
    tmp = final_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for pid in sorted(staged):
            fh.write(json.dumps(staged[pid], ensure_ascii=False) + "\n")
    tmp.replace(final_path)
    print(f"  wrote {final_path} (previous copy kept at {backup.name})")

    # The parquet twin is what index_from_stage reads; a stale one alongside a
    # refreshed jsonl is exactly the kind of silent divergence that makes a
    # rebuild reintroduce the old names. Removed rather than rewritten — this
    # tool feeds `index_namespace --source-stage final`, which reads the jsonl.
    parquet = final_path.with_suffix(".parquet")
    if parquet.exists():
        stale = parquet.with_suffix(".parquet.stale")
        parquet.rename(stale)
        print(f"  renamed stale {parquet.name} → {stale.name} "
              f"(regenerate it with the stage chain before any full rebuild)")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--execute", action="store_true",
                    help="apply the refresh (default: report only)")
    args = ap.parse_args()
    raise SystemExit(refresh(args.namespace, args.execute))


if __name__ == "__main__":
    main()
