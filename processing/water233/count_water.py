#!/usr/bin/env python
"""place#233 — count water objects in a PBF, split by TYPE and CLOSEDNESS.

Why the split matters (issue controls 1 and 2):

* Control 1 asks whether ``osmium tags-filter`` kept everything. A filter
  expression that silently matches nothing produces a small, valid, wholly
  useless PBF, and every later step would succeed on it. So we compare the
  filtered file's counts against the planet's, per tag, as a RATIO.

* Control 2 asks whether area assembly is picking up multipolygon RELATIONS,
  not just closed ways. Simple ponds dominate ``natural=water`` numerically,
  so dropping EVERY relation still yields a large, healthy-looking polygon
  count while losing precisely the big complex lakes that matter most
  visually. No total can see this. Only ways-vs-relations can.

Every number is reported with its denominator.
"""
from __future__ import annotations

import argparse
import json
import sys

import osmium


TAGS = {
    "natural=water":      lambda t: t.get("natural") == "water",
    "water=*":            lambda t: "water" in t,
    "waterway=*":         lambda t: "waterway" in t,
    "waterway=riverbank": lambda t: t.get("waterway") == "riverbank",
    "landuse=reservoir":  lambda t: t.get("landuse") == "reservoir",
    "natural=coastline":  lambda t: t.get("natural") == "coastline",
}


def _tagdict(obj) -> dict:
    return {t.k: t.v for t in obj.tags}


def count(path: str, progress_every: int = 0) -> dict:
    counts: dict[str, dict[str, int]] = {
        k: {"way": 0, "way_closed": 0, "way_open": 0, "relation": 0,
            "relation_multipolygon": 0}
        for k in TAGS
    }
    seen = 0

    fp = osmium.FileProcessor(path, osmium.osm.WAY | osmium.osm.RELATION)
    # Push the tag test into C++ where possible so only candidate objects
    # cross the interpreter boundary. Falls back to a pure-Python scan.
    try:
        fp = fp.with_filter(
            osmium.filter.KeyFilter("natural", "water", "waterway", "landuse")
        )
        filter_mode = "c++ KeyFilter"
    except Exception as exc:  # noqa: BLE001
        filter_mode = f"python fallback ({exc})"

    for obj in fp:
        seen += 1
        if progress_every and seen % progress_every == 0:
            print(f"  ... {seen:,} candidate objects", file=sys.stderr, flush=True)
        tags = _tagdict(obj)
        if not tags:
            continue
        is_way = isinstance(obj, osmium.osm.Way)
        for name, pred in TAGS.items():
            try:
                if not pred(tags):
                    continue
            except Exception:  # noqa: BLE001
                continue
            c = counts[name]
            if is_way:
                c["way"] += 1
                nodes = obj.nodes
                closed = len(nodes) > 3 and nodes[0].ref == nodes[-1].ref
                c["way_closed" if closed else "way_open"] += 1
            else:
                c["relation"] += 1
                if tags.get("type") in ("multipolygon", "boundary"):
                    c["relation_multipolygon"] += 1

    return {
        "path": path,
        "filter_mode": filter_mode,
        "candidate_objects_examined": seen,
        "counts": counts,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("--out")
    ap.add_argument("--progress-every", type=int, default=0)
    args = ap.parse_args()

    res = count(args.pbf, args.progress_every)
    text = json.dumps(res, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    print("\n--- summary (denominators shown) ---")
    for name, c in res["counts"].items():
        print(f"  {name:22s} ways={c['way']:>10,} "
              f"(closed={c['way_closed']:,} open={c['way_open']:,})  "
              f"relations={c['relation']:>8,} "
              f"(multipolygon={c['relation_multipolygon']:,})")
    print(f"  candidate objects examined: {res['candidate_objects_examined']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
