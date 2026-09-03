#!/usr/bin/env python
"""place#233 Step 2 — assemble inland water areas from a PBF into GeoJSONL.

Emits one GeoJSON Feature per line, tagged with the layer it belongs in
(``lakes`` or ``rivers``), ready to stream into tippecanoe.

⚠️ The thing this script must not silently get wrong is **multipolygon
relations**. ``natural=water`` is dominated numerically by simple closed-way
ponds, so an assembler that drops every relation still emits a large,
healthy-looking polygon count while losing precisely the big complex lakes —
the ones with islands, the ones that matter visually. A bare total cannot
detect that. So this script reports its output split by the SOURCE of each
area (way vs relation), and the caller compares those against the input
counts from ``count_water.py``.

pyosmium note: area assembly is ``osmium.area.AreaManager`` in 4.x.
``MultipolygonManager`` is the 3.x name and does not exist here.
"""
from __future__ import annotations

import argparse
import json
import sys

import osmium


def classify(tags: dict) -> str | None:
    """Return the tile layer this object belongs to, or None to skip.

    ``ice`` is named to match whg-ne-basic's existing source-layer, as
    ``ocean``/``lakes``/``rivers`` are, so repointing the style stays a
    ``source`` change with ``source-layer`` untouched on all five layers.
    """
    if tags.get("natural") == "glacier" or tags.get("landuse") == "glacier":
        return "ice"
    if tags.get("natural") == "water":
        return "lakes"
    if "water" in tags:
        return "lakes"
    if tags.get("landuse") == "reservoir":
        return "lakes"
    waterway = tags.get("waterway")
    if waterway == "riverbank":
        # Vertex-dense and the first thing dropped if the tileset is too big
        # (issue Step 3 fallback order), so it is kept separable by layer.
        return "rivers"
    if waterway in {"river", "stream", "canal", "ditch", "drain"}:
        return "rivers"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("--out", required=True, help="GeoJSONL output path")
    ap.add_argument("--stats-out")
    ap.add_argument("--progress-every", type=int, default=250_000)
    args = ap.parse_args()

    factory = osmium.geom.GeoJSONFactory()
    stats = {
        "areas_seen": 0,
        "areas_written": 0,
        "skipped_untagged": 0,
        "skipped_geometry_error": 0,
        "from_way": 0,
        "from_relation": 0,
        "by_layer": {},
        "from_relation_by_layer": {},
    }

    fp = osmium.FileProcessor(args.pbf).with_areas()

    with open(args.out, "w", encoding="utf-8") as fh:
        for obj in fp:
            if not isinstance(obj, osmium.osm.Area):
                continue
            stats["areas_seen"] += 1
            if args.progress_every and stats["areas_seen"] % args.progress_every == 0:
                print(f"  ... {stats['areas_seen']:,} areas seen, "
                      f"{stats['areas_written']:,} written",
                      file=sys.stderr, flush=True)

            tags = {t.k: t.v for t in obj.tags}
            layer = classify(tags)
            if layer is None:
                stats["skipped_untagged"] += 1
                continue

            try:
                geom = json.loads(factory.create_multipolygon(obj))
            except Exception:  # noqa: BLE001
                stats["skipped_geometry_error"] += 1
                continue
            if not geom or not geom.get("coordinates"):
                stats["skipped_geometry_error"] += 1
                continue

            # Area ids are derived: even ids come from ways, odd from relations.
            # This is how we prove relations survived assembly rather than
            # assuming it from a healthy-looking total.
            from_relation = bool(obj.from_way() is False) if hasattr(obj, "from_way") \
                else bool(obj.id % 2)
            if from_relation:
                stats["from_relation"] += 1
                stats["from_relation_by_layer"][layer] = \
                    stats["from_relation_by_layer"].get(layer, 0) + 1
            else:
                stats["from_way"] += 1
            stats["by_layer"][layer] = stats["by_layer"].get(layer, 0) + 1

            props = {"layer": layer}
            for k in ("name", "natural", "water", "waterway", "landuse"):
                if k in tags:
                    props[k] = tags[k]
            fh.write(json.dumps(
                {"type": "Feature", "properties": props, "geometry": geom},
                separators=(",", ":")) + "\n")
            stats["areas_written"] += 1

    text = json.dumps(stats, indent=2)
    print(text)
    if args.stats_out:
        with open(args.stats_out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    print("\n--- assembly summary (denominators shown) ---")
    print(f"  areas seen:            {stats['areas_seen']:,}")
    print(f"  areas written:         {stats['areas_written']:,}")
    print(f"    from closed ways:    {stats['from_way']:,}")
    print(f"    from relations:      {stats['from_relation']:,}   <-- the control")
    print(f"  skipped (not water):   {stats['skipped_untagged']:,}")
    print(f"  skipped (bad geom):    {stats['skipped_geometry_error']:,}")
    print(f"  by layer:              {stats['by_layer']}")
    print(f"  relations by layer:    {stats['from_relation_by_layer']}")
    if stats["from_relation"] == 0 and stats["areas_written"] > 0:
        print("\n  *** CONTROL FAILED: zero areas came from relations. ***")
        print("  *** Assembly is dropping multipolygons; the big complex   ***")
        print("  *** lakes are missing and the total cannot show it.       ***")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
