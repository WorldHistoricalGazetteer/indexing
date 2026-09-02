#!/usr/bin/env python
"""place#233 — measure whether coastal admin boundaries SHARE coastline ways.

The issue's central claim, and the whole reason for extracting from our own
PBF rather than taking pre-built water polygons, is topological:

    "Coastal national borders in OSM very commonly share the coastline way:
     the boundary=administrative relation references the same way IDs as
     natural=coastline. From one PBF edition, coast and coastal border are
     vertex-identical."

That is an assertion about the DATA, and it is testable directly — by way ID
and node ID — without rendering anything and without deploying anything.
Shared identifiers are stronger evidence than any visual comparison: two
lines that look coincident might merely be close, but two lines built from
the same node IDs ARE the same geometry.

This matters for the acceptance criteria. The post-build visual check
("coast and boundary are visually coincident") cannot run until the tileset
is deployed, which is out of scope here. This check measures the underlying
property now, on the same PBF edition the tiles will be built from.

Reports:
  * how many way IDs are referenced by BOTH a coastline way and an
    admin boundary relation, against the denominator of each;
  * how many admin-boundary nodes are also coastline nodes.

A low sharing figure is not necessarily a defect — inland stretches of an
admin boundary legitimately share nothing with the coast — so the figure is
reported per relation, and restricted to boundary relations that touch the
coast at all.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys

import osmium


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf", help="a SMALL extract (bbox) of the pinned planet")
    ap.add_argument("--out")
    ap.add_argument("--min-admin-level", type=int, default=2)
    ap.add_argument("--max-admin-level", type=int, default=6)
    args = ap.parse_args()

    coastline_way_ids: set[int] = set()
    coastline_node_ids: set[int] = set()
    way_nodes: dict[int, list[int]] = {}

    # Pass 1: every way, so we can resolve relation members to nodes, plus
    # the coastline way/node sets.
    for w in osmium.FileProcessor(args.pbf, osmium.osm.WAY):
        refs = [n.ref for n in w.nodes]
        way_nodes[w.id] = refs
        if w.tags.get("natural") == "coastline":
            coastline_way_ids.add(w.id)
            coastline_node_ids.update(refs)

    # Pass 2: admin boundary relations.
    rows = []
    for r in osmium.FileProcessor(args.pbf, osmium.osm.RELATION):
        tags = {t.k: t.v for t in r.tags}
        if tags.get("boundary") != "administrative":
            continue
        try:
            lvl = int(tags.get("admin_level", "99"))
        except ValueError:
            continue
        if not (args.min_admin_level <= lvl <= args.max_admin_level):
            continue

        member_ways = [m.ref for m in r.members if m.type == "w"]
        if not member_ways:
            continue
        shared_ways = [wid for wid in member_ways if wid in coastline_way_ids]

        nodes: set[int] = set()
        for wid in member_ways:
            nodes.update(way_nodes.get(wid, ()))
        shared_nodes = nodes & coastline_node_ids
        if not shared_ways and not shared_nodes:
            continue  # wholly inland boundary: nothing to say about the coast

        rows.append({
            "name": tags.get("name", f"relation/{r.id}"),
            "admin_level": lvl,
            "member_ways": len(member_ways),
            "member_ways_that_are_coastline": len(shared_ways),
            "boundary_nodes": len(nodes),
            "boundary_nodes_on_coastline": len(shared_nodes),
        })

    rows.sort(key=lambda d: -d["boundary_nodes_on_coastline"])
    result = {
        "pbf": args.pbf,
        "coastline_ways_in_extract": len(coastline_way_ids),
        "coastline_nodes_in_extract": len(coastline_node_ids),
        "coast_touching_boundaries": len(rows),
        "rows": rows,
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(result, indent=2) + "\n")

    print(f"coastline ways in extract : {len(coastline_way_ids):,}")
    print(f"coastline nodes in extract: {len(coastline_node_ids):,}")
    print(f"admin boundaries touching the coast: {len(rows)}")
    print()
    if not rows:
        print("*** No admin boundary shares any node with a coastline way here.")
        print("*** Either the bbox contains no coastal boundary, or the")
        print("*** shared-way premise does not hold at this location.")
        return 4

    print(f"{'boundary':40s} {'lvl':>3s} {'ways':>6s} {'coastl':>7s} "
          f"{'nodes':>8s} {'on coast':>9s} {'share':>7s}")
    tot_nodes = tot_shared = 0
    for d in rows[:20]:
        share = d["boundary_nodes_on_coastline"] / d["boundary_nodes"] \
            if d["boundary_nodes"] else 0.0
        tot_nodes += d["boundary_nodes"]
        tot_shared += d["boundary_nodes_on_coastline"]
        print(f"{d['name'][:40]:40s} {d['admin_level']:3d} "
              f"{d['member_ways']:6,} {d['member_ways_that_are_coastline']:7,} "
              f"{d['boundary_nodes']:8,} {d['boundary_nodes_on_coastline']:9,} "
              f"{share:6.1%}")
    print()
    print(f"Across the {min(len(rows),20)} listed: {tot_shared:,} of {tot_nodes:,} "
          f"boundary nodes lie on a coastline way "
          f"({tot_shared/tot_nodes:.1%} of boundary vertices).")
    print()
    print("Interpretation: every shared NODE ID is a vertex the coast and the")
    print("boundary hold in common by construction, not by proximity. Water")
    print("built from these same ways is aligned with these boundaries exactly,")
    print("at every zoom, which is the property Natural Earth cannot provide.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
