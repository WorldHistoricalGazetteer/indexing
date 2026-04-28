#!/usr/bin/env python3
"""Plan parallel shards for OSM/OHM boundary stage.

The boundary stage's wall time is dominated by a small number of huge
multipolygon assemblies (oceans, continental admin units). On the OHM smoke
run a single Slurm task processed boundaries at ~380/s for the first minute
then dropped to ~4/s on the long tail — extrapolating to >100h to finish
the last 5% of relations.

This planner scans the prefiltered PBF, computes a cost proxy per boundary
relation, and produces a hybrid shard map:

* The top ``--mega-shard-count`` relations (by cost) each get their own
  dedicated single-relation shard. These shards run on a separate Slurm
  array with a long wall budget.
* The remaining relations are bin-packed across ``--shard-count`` regular
  shards using the Longest Processing Time (LPT) greedy heuristic. Regular
  shards run on a normal Slurm array with a shorter wall budget.

This compresses the worst-shard wall toward the median: the long tail of
mega-relations no longer determines the per-shard wall budget for the
99.9% of well-behaved relations.

Cost proxies (``--cost-proxy``):

* ``member-count`` (cheap, single PBF pass) — counts member ways per
  relation. Coarse: actual osmium assembly cost is dominated by node
  density, not member count.
* ``node-count`` (default; two PBF passes) — sums member-way node counts
  per relation. Closely tracks osmium ``with_areas()`` assembly cost.

Output (``shard_map.json``)::

  {
    "namespace": "osm",
    "cost_proxy": "node-count",
    "shard_count": 32,
    "regular_shard_count": 22,
    "mega_shard_count": 10,
    "total_relations": 5000000,
    "shards": [
      {"shard_id": 0, "mega": true, "relation_ids": [148838],
       "estimated_cost": 9876543},
      ...
      {"shard_id": 10, "mega": false, "relation_ids": [...],
       "estimated_cost": 1234567},
      ...
    ]
  }

Mega shards always come first (``shard_id`` 0..M-1); regular shards come
next (``shard_id`` M..M+R-1). The Slurm submitter relies on this ordering
to split the array.

Run::

    python -m processing.boundary_shard_planner \\
        --pbf /path/to/planet-latest.osm.pbf \\
        --namespace osm \\
        --shard-count 22 \\
        --mega-shard-count 10 \\
        --cost-proxy node-count \\
        --output /path/to/shard_map.json
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import time
from pathlib import Path

from processing.osm_boundary_geometry import (
    _require_osmium,
    is_admin_boundary_value,
    is_misc_boundary_value,
    prefilter_boundaries,
)


COST_PROXY_MEMBER_COUNT = "member-count"
COST_PROXY_NODE_COUNT = "node-count"
_COST_PROXIES = (COST_PROXY_MEMBER_COUNT, COST_PROXY_NODE_COUNT)


def _is_boundary_candidate(tags: dict[str, str]) -> bool:
    """Return True if a relation's tags identify it as a boundary we'd assemble.

    Mirrors the filter logic in
    ``osm_boundary_geometry.process_relation_tags`` (without requiring
    ``name`` — the planner is conservative and accepts any tagged
    boundary so the worker stage decides what to keep). Used to skip
    non-boundary relations that survived the loose ``r/boundary``
    osmium-tool filter.
    """
    if "boundary" not in tags:
        return False
    boundary_value = tags.get("boundary", "")
    if not boundary_value:
        return False
    if boundary_value == "administrative":
        return is_admin_boundary_value(tags.get("admin_level", ""))
    if boundary_value in ("continent", "country_border"):
        return True
    return is_misc_boundary_value(boundary_value)


def _enumerate_with_member_count(pbf_path: Path) -> list[tuple[int, int]]:
    """Single-pass: count member ways per boundary relation."""
    osmium = _require_osmium()
    items: list[tuple[int, int]] = []

    fp = osmium.FileProcessor(str(pbf_path), osmium.osm.RELATION)
    for obj in fp:
        if not obj.is_relation():
            continue
        tags = {tag.k: tag.v for tag in obj.tags}
        if not _is_boundary_candidate(tags):
            continue
        member_count = sum(1 for _ in obj.members)
        items.append((obj.id, member_count))

    return items


def _enumerate_with_node_count(pbf_path: Path) -> list[tuple[int, int]]:
    """Two-pass: sum member-way node counts per boundary relation.

    Pass 1 (relations): for each boundary candidate, collect the set of
    member way IDs.
    Pass 2 (ways): build ``way_id → node_count`` for the union of needed
    way IDs.
    Then compute ``cost = sum(node_count(w) for w in members)`` per
    relation.

    A member way that is not present in the PBF (rare, but possible if
    the prefilter dropped it) contributes 0 — that's a strict
    underestimate, which is fine: the LPT packer just sees that relation
    as cheaper.
    """
    osmium = _require_osmium()

    relation_members: dict[int, list[int]] = {}
    needed_ways: set[int] = set()

    fp = osmium.FileProcessor(str(pbf_path), osmium.osm.RELATION)
    for obj in fp:
        if not obj.is_relation():
            continue
        tags = {tag.k: tag.v for tag in obj.tags}
        if not _is_boundary_candidate(tags):
            continue
        way_refs = [m.ref for m in obj.members if m.type == "w"]
        relation_members[obj.id] = way_refs
        needed_ways.update(way_refs)

    way_node_counts: dict[int, int] = {}
    if needed_ways:
        fp = osmium.FileProcessor(str(pbf_path), osmium.osm.WAY)
        for obj in fp:
            if not obj.is_way():
                continue
            wid = obj.id
            if wid in needed_ways:
                way_node_counts[wid] = len(obj.nodes)

    items: list[tuple[int, int]] = []
    for rid, way_refs in relation_members.items():
        cost = sum(way_node_counts.get(w, 0) for w in way_refs)
        # Floor of 1 so a relation with no resolvable ways still gets
        # placed (won't sit in a special bucket of cost-0 outliers).
        items.append((rid, max(cost, 1)))
    return items


def enumerate_boundary_relations(
    pbf_path: Path,
    cost_proxy: str = COST_PROXY_NODE_COUNT,
) -> list[tuple[int, int]]:
    """Scan a (prefiltered) PBF and return ``[(relation_id, cost), …]``.

    ``cost_proxy`` controls how cost is computed; see module docstring.
    """
    if cost_proxy == COST_PROXY_MEMBER_COUNT:
        return _enumerate_with_member_count(pbf_path)
    if cost_proxy == COST_PROXY_NODE_COUNT:
        return _enumerate_with_node_count(pbf_path)
    raise ValueError(
        f"Unknown cost_proxy {cost_proxy!r}; expected one of {_COST_PROXIES}"
    )


def _lpt_pack(
    items: list[tuple[int, int]], shard_count: int,
) -> tuple[list[list[int]], list[int]]:
    """Greedy Longest-Processing-Time bin-packing.

    Returns ``(shard_relations, shard_loads)`` parallel lists of length
    ``shard_count``. ``items`` is consumed in descending-cost order; each
    item is assigned to the currently lightest shard. Result: max-shard
    cost is minimised.
    """
    sorted_items = sorted(items, key=lambda item: item[1], reverse=True)

    shard_relations: list[list[int]] = [[] for _ in range(shard_count)]
    shard_loads: list[int] = [0] * shard_count
    heap = [(0, i) for i in range(shard_count)]
    heapq.heapify(heap)

    for relation_id, cost in sorted_items:
        load, shard_id = heapq.heappop(heap)
        shard_relations[shard_id].append(relation_id)
        shard_loads[shard_id] = load + cost
        heapq.heappush(heap, (shard_loads[shard_id], shard_id))

    return shard_relations, shard_loads


def plan_shards(
    items: list[tuple[int, int]],
    shard_count: int,
    *,
    mega_shard_count: int = 0,
) -> list[dict]:
    """Build the hybrid mega + regular shard list.

    The top ``mega_shard_count`` relations (by cost) each get their own
    dedicated single-relation shard. The remaining relations are
    LPT-packed into ``shard_count`` regular shards.

    Mega shards are emitted first (``shard_id`` 0..M-1) followed by
    regular shards (``shard_id`` M..M+R-1). The Slurm submitter uses
    this ordering to split the array.

    With ``mega_shard_count=0`` the result is the original behaviour:
    ``shard_count`` LPT-packed shards, none flagged as mega.
    """
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    if mega_shard_count < 0:
        raise ValueError("mega_shard_count must be non-negative")

    sorted_items = sorted(items, key=lambda item: item[1], reverse=True)

    # If we have fewer relations than mega slots, only the relations we
    # actually have become mega shards.
    effective_mega = min(mega_shard_count, len(sorted_items))
    mega_items = sorted_items[:effective_mega]
    rest_items = sorted_items[effective_mega:]

    out: list[dict] = []

    for offset, (relation_id, cost) in enumerate(mega_items):
        out.append({
            "shard_id": offset,
            "mega": True,
            "relation_ids": [relation_id],
            "estimated_cost": cost,
        })

    regular_relations, regular_loads = _lpt_pack(rest_items, shard_count)
    for i in range(shard_count):
        out.append({
            "shard_id": effective_mega + i,
            "mega": False,
            "relation_ids": regular_relations[i],
            "estimated_cost": regular_loads[i],
        })

    return out


def write_shard_map(
    shard_map_path: Path,
    *,
    namespace: str,
    shards: list[dict],
    total_relations: int,
    cost_proxy: str = COST_PROXY_NODE_COUNT,
) -> None:
    mega_count = sum(1 for s in shards if s.get("mega"))
    regular_count = len(shards) - mega_count
    payload = {
        "namespace": namespace,
        "cost_proxy": cost_proxy,
        "shard_count": len(shards),
        "regular_shard_count": regular_count,
        "mega_shard_count": mega_count,
        "total_relations": total_relations,
        "shards": shards,
    }
    shard_map_path.parent.mkdir(parents=True, exist_ok=True)
    shard_map_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_planner(
    *,
    pbf_path: Path,
    namespace: str,
    shard_count: int,
    output_path: Path,
    skip_prefilter: bool = False,
    mega_shard_count: int = 0,
    cost_proxy: str = COST_PROXY_NODE_COUNT,
) -> dict:
    """Pre-filter the PBF (if needed), score relations, write shard map."""
    if not pbf_path.exists():
        raise FileNotFoundError(f"PBF file not found: {pbf_path}")
    if cost_proxy not in _COST_PROXIES:
        raise ValueError(
            f"Unknown cost_proxy {cost_proxy!r}; expected one of {_COST_PROXIES}"
        )

    started = time.time()

    if skip_prefilter:
        scan_pbf = pbf_path
        prefiltered_path = None
    else:
        scratch = os.environ.get("SLURM_SCRATCH") or os.environ.get("TMPDIR", "/tmp")
        prefiltered_path = Path(scratch) / f"{namespace}_boundary_planner_filtered.osm.pbf"
        result = prefilter_boundaries(pbf_path, str(prefiltered_path))
        scan_pbf = Path(result) if result else pbf_path

    try:
        items = enumerate_boundary_relations(scan_pbf, cost_proxy=cost_proxy)
    finally:
        if (
            prefiltered_path is not None
            and prefiltered_path.exists()
            and prefiltered_path != pbf_path
        ):
            try:
                prefiltered_path.unlink()
            except OSError:
                pass

    if not items:
        raise RuntimeError(
            f"No boundary candidate relations found in {scan_pbf} — "
            "either the PBF lacks boundary relations or the filter is too strict."
        )

    shards = plan_shards(items, shard_count, mega_shard_count=mega_shard_count)
    write_shard_map(
        output_path,
        namespace=namespace,
        shards=shards,
        total_relations=len(items),
        cost_proxy=cost_proxy,
    )

    elapsed = time.time() - started
    regular_shards = [s for s in shards if not s.get("mega")]
    mega_shards = [s for s in shards if s.get("mega")]
    summary = {
        "namespace": namespace,
        "pbf_file": str(pbf_path),
        "cost_proxy": cost_proxy,
        "shard_count": len(shards),
        "regular_shard_count": len(regular_shards),
        "mega_shard_count": len(mega_shards),
        "total_relations": len(items),
        "max_regular_shard_relations": (
            max((len(s["relation_ids"]) for s in regular_shards), default=0)
        ),
        "max_regular_shard_cost": (
            max((s["estimated_cost"] for s in regular_shards), default=0)
        ),
        "min_regular_shard_cost": (
            min((s["estimated_cost"] for s in regular_shards), default=0)
        ),
        "max_mega_shard_cost": (
            max((s["estimated_cost"] for s in mega_shards), default=0)
        ),
        "shard_map_path": str(output_path),
        "elapsed_seconds": round(elapsed, 1),
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan parallel shards for the OSM/OHM boundary stage."
    )
    parser.add_argument("--pbf", required=True, help="Path to OSM/OHM PBF file")
    parser.add_argument("--namespace", required=True, choices=["osm", "ohm"])
    parser.add_argument("--shard-count", type=int, required=True,
                        help="Number of regular (LPT-packed) shards")
    parser.add_argument("--mega-shard-count", type=int, default=0,
                        help="Number of dedicated single-relation mega shards "
                             "for the costliest relations (default: 0)")
    parser.add_argument("--cost-proxy", choices=_COST_PROXIES,
                        default=COST_PROXY_NODE_COUNT,
                        help="Cost proxy for ranking and packing relations "
                             f"(default: {COST_PROXY_NODE_COUNT})")
    parser.add_argument("--output", required=True,
                        help="Where to write the shard_map.json")
    parser.add_argument("--skip-prefilter", action="store_true",
                        help="Skip osmium tags-filter (input is already filtered)")
    args = parser.parse_args()

    summary = run_planner(
        pbf_path=Path(args.pbf),
        namespace=args.namespace,
        shard_count=args.shard_count,
        output_path=Path(args.output),
        skip_prefilter=args.skip_prefilter,
        mega_shard_count=args.mega_shard_count,
        cost_proxy=args.cost_proxy,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
