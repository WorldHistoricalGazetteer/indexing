#!/usr/bin/env python3
"""Plan parallel shards for OSM/OHM boundary stage.

The boundary stage's wall time is dominated by a small number of huge
multipolygon assemblies (oceans, continental admin units). On the OHM smoke
run a single Slurm task processed boundaries at ~380/s for the first minute
then dropped to ~4/s on the long tail — extrapolating to >100h to finish
the last 5% of relations.

This planner scans the prefiltered PBF, counts member ways per boundary
relation, and bin-packs them across N shards using the Longest Processing
Time (LPT) greedy heuristic on (member_count) as the cost proxy. The
biggest single relation always lands alone on the lightest-loaded shard,
so no shard can be slower than the single most expensive relation. With
N parallel shards, the long tail no longer serialises.

Member count is a coarse cost proxy — actual assembly cost depends on the
total number of nodes in member ways — but for our purposes (separating
the 5 continent-scale relations from the 70 000 small ones) it works well
enough. A future improvement would be to also bin by ``sum(way.nodes)``,
but that requires reading way nodes too.

Output (``shard_map.json``):

  {
    "namespace": "ohm",
    "shard_count": 16,
    "total_relations": 68301,
    "shards": [
      {"shard_id": 0, "relation_ids": [1234, 5678, ...], "estimated_cost": 12345},
      ...
    ]
  }

Run:

    python -m processing.boundary_shard_planner \\
        --pbf /path/to/planet-latest.osm.pbf \\
        --namespace ohm \\
        --shard-count 16 \\
        --output /path/to/shard_map.json
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import time
from pathlib import Path
from typing import Iterable

from processing.osm_boundary_geometry import (
    _require_osmium,
    is_admin_boundary_value,
    is_misc_boundary_value,
    prefilter_boundaries,
)


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


def enumerate_boundary_relations(pbf_path: Path) -> list[tuple[int, int]]:
    """Scan a (prefiltered) PBF and return ``[(relation_id, member_count), …]``.

    Reads only the relations section — does not assemble areas, does not
    touch member ways or nodes. Cheap.
    """
    osmium = _require_osmium()
    items: list[tuple[int, int]] = []

    fp = osmium.FileProcessor(str(pbf_path), osmium.osm.RELATION)
    for obj in fp:
        if obj.is_relation():
            tags = {tag.k: tag.v for tag in obj.tags}
            if not _is_boundary_candidate(tags):
                continue
            member_count = sum(1 for _ in obj.members)
            items.append((obj.id, member_count))

    return items


def plan_shards(
    items: list[tuple[int, int]],
    shard_count: int,
) -> list[dict]:
    """Bin-pack relations into shards using LPT (Longest Processing Time first).

    Sorts items by descending cost, then assigns each to the currently
    lightest-loaded shard. Result: max-shard-cost is minimised; the
    largest single relation always sits alone on the shard that ends
    up at peak load.
    """
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")

    sorted_items = sorted(items, key=lambda item: item[1], reverse=True)

    shard_loads: list[int] = [0] * shard_count
    shard_relations: list[list[int]] = [[] for _ in range(shard_count)]
    heap = [(0, i) for i in range(shard_count)]
    heapq.heapify(heap)

    for relation_id, cost in sorted_items:
        load, shard_id = heapq.heappop(heap)
        shard_relations[shard_id].append(relation_id)
        shard_loads[shard_id] = load + cost
        heapq.heappush(heap, (shard_loads[shard_id], shard_id))

    return [
        {
            "shard_id": i,
            "relation_ids": shard_relations[i],
            "estimated_cost": shard_loads[i],
        }
        for i in range(shard_count)
    ]


def write_shard_map(
    shard_map_path: Path,
    *,
    namespace: str,
    shards: list[dict],
    total_relations: int,
) -> None:
    payload = {
        "namespace": namespace,
        "shard_count": len(shards),
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
) -> dict:
    """Pre-filter the PBF (if needed), count members per relation, write shard map."""
    if not pbf_path.exists():
        raise FileNotFoundError(f"PBF file not found: {pbf_path}")

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
        items = enumerate_boundary_relations(scan_pbf)
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

    shards = plan_shards(items, shard_count)
    write_shard_map(
        output_path,
        namespace=namespace,
        shards=shards,
        total_relations=len(items),
    )

    elapsed = time.time() - started
    summary = {
        "namespace": namespace,
        "pbf_file": str(pbf_path),
        "shard_count": shard_count,
        "total_relations": len(items),
        "max_shard_relations": max(len(s["relation_ids"]) for s in shards),
        "max_shard_cost": max(s["estimated_cost"] for s in shards),
        "min_shard_cost": min(s["estimated_cost"] for s in shards),
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
                        help="Number of parallel shards to plan")
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
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
