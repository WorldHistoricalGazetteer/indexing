# clustering/runner.py
"""
CLI entry point for the clustering pipeline.

Usage:
    python -m clustering.runner --full             # Full initial run
    python -m clustering.runner --incremental      # Since last run
    python -m clustering.runner --full --dry-run   # Compute but don't index
    python -m clustering.runner --stats            # Report index state

    # With explicit ES connection (used by es.sh wrapper):
    python -m clustering.runner --full --es-host http://localhost:9201 --es-pass-file /path/to/pw
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

from .config import ClusterConfig
from .es_client import es_client, scroll_index
from .indexer import (
    ensure_clusters_index,
    index_membership_docs,
    index_pairwise_docs,
    delete_stale_memberships,
)
from .schemas import PairwiseDoc
from .scoring import score_pairwise_docs
from .state import ClusterState, HighWaterMarks, RunStatistics, load_state, save_state

logger = logging.getLogger("clustering.runner")


def _setup_logging(verbose: bool = False) -> None:
    """Configure structured logging."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
    # Silence noisy libraries
    logging.getLogger("elasticsearch").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def _get_current_hwm(
    client, cfg: ClusterConfig
) -> HighWaterMarks:
    """Get current high-water marks from the ES indices."""
    hwm = HighWaterMarks()

    # Get max indexed_at from places index
    try:
        resp = await client.search(
            index=cfg.places_index,
            body={
                "size": 0,
                "aggs": {
                    "max_indexed": {"max": {"field": "indexed_at"}}
                },
            },
        )
        val = resp["aggregations"]["max_indexed"]["value_as_string"]
        if val:
            hwm.places_indexed_at = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception as e:
        logger.warning("Could not get places max indexed_at: %s", e)

    # Get max indexed_at from toponyms index
    try:
        resp = await client.search(
            index=cfg.toponyms_index,
            body={
                "size": 0,
                "aggs": {
                    "max_indexed": {"max": {"field": "indexed_at"}}
                },
            },
        )
        val = resp["aggregations"]["max_indexed"]["value_as_string"]
        if val:
            hwm.toponyms_indexed_at = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception as e:
        logger.warning("Could not get toponyms max indexed_at: %s", e)

    hwm.contributor_links_modified_at = datetime.now(timezone.utc)
    return hwm


async def _load_all_pairwise_docs(
    client, cfg: ClusterConfig
) -> list[PairwiseDoc]:
    """Load all existing pairwise docs from the clusters index."""
    docs = []
    query = {"term": {"doc_type": "pairwise"}}
    async for raw in scroll_index(client, cfg.clusters_index, query):
        try:
            raw.pop("_id", None)
            docs.append(PairwiseDoc(**raw))
        except Exception:
            pass
    return docs


async def _fetch_place_coords(
    client, cfg: ClusterConfig, place_ids: set[str]
) -> dict[str, tuple[float, float] | None]:
    """Fetch repr_point coordinates for a set of place_ids."""
    coords: dict[str, tuple[float, float] | None] = {}
    pids = list(place_ids)

    for i in range(0, len(pids), 10_000):
        chunk = pids[i : i + 10_000]
        try:
            resp = await client.search(
                index=cfg.places_index,
                body={
                    "size": len(chunk),
                    "query": {"terms": {"place_id": chunk}},
                    "_source": ["place_id", "geometries.repr_point"],
                },
            )
            for hit in resp["hits"]["hits"]:
                src = hit["_source"]
                pid = src.get("place_id", "")
                repr_point = None
                for geom in src.get("geometries", []):
                    rp = geom.get("repr_point")
                    if rp:
                        if isinstance(rp, dict):
                            repr_point = (rp.get("lat", 0), rp.get("lon", 0))
                        elif isinstance(rp, (list, tuple)) and len(rp) >= 2:
                            repr_point = (rp[1], rp[0])
                        break
                coords[pid] = repr_point
        except Exception as e:
            logger.warning("Coord fetch failed for chunk: %s", e)

    # Fill missing with None
    for pid in place_ids:
        if pid not in coords:
            coords[pid] = None

    return coords


async def run_full(cfg: ClusterConfig, dry_run: bool = False) -> RunStatistics:
    """Execute a full clustering run (all phases)."""
    stats = RunStatistics()
    t0 = time.monotonic()

    async with es_client(cfg) as client:
        if not dry_run:
            await ensure_clusters_index(client, cfg)

        # Phase 1A: Authority hard links
        print(flush=True)
        print("=" * 60, flush=True)
        print("  Phase 1A: Authority hard links", flush=True)
        print("=" * 60, flush=True)
        from .harvest.hard_links import harvest_authority_hard_links

        phase1a_docs = await harvest_authority_hard_links(client, cfg)
        stats.phase_1a_pairs = len(phase1a_docs)
        print(f"  → {stats.phase_1a_pairs:,} pairs", flush=True)

        # Phase 1B: Contributor reconciliation links (via SSH tunnel to PG)
        phase1b_docs = []
        try:
            print(flush=True)
            print("=" * 60, flush=True)
            print("  Phase 1B: Contributor reconciliation links", flush=True)
            print("=" * 60, flush=True)
            from .pg_client import pg_connection
            from .harvest.contributor_links import harvest_contributor_links

            async with pg_connection() as conn:
                phase1b_docs = await harvest_contributor_links(conn, cfg)
            stats.phase_1b_pairs = len(phase1b_docs)
            print(f"  → {stats.phase_1b_pairs:,} pairs", flush=True)
        except Exception as e:
            logger.warning("Phase 1B failed (PG connection issue): %s", e)
            print(f"  ⚠ Phase 1B skipped (PG connection issue): {e}", flush=True)

        # Deduplicate Phase 1A + 1B
        all_hard_links = _deduplicate_hard_links(phase1a_docs, phase1b_docs)

        # Phase 2: Exact toponym co-attestation
        print(flush=True)
        print("=" * 60, flush=True)
        print("  Phase 2: Exact toponym co-attestation", flush=True)
        print("=" * 60, flush=True)
        from .harvest.exact_coattest import harvest_exact_coattestations

        phase2_docs = await harvest_exact_coattestations(client, cfg)
        stats.phase_2_pairs = len(phase2_docs)
        print(f"  → {stats.phase_2_pairs:,} pairs", flush=True)

        # Collect already-clustered place_ids (from phases 1+2)
        clustered_pids: set[str] = set()
        for doc in all_hard_links:
            clustered_pids.add(doc.place_id_a)
            clustered_pids.add(doc.place_id_b)
        for doc in phase2_docs:
            clustered_pids.add(doc.place_id_a)
            clustered_pids.add(doc.place_id_b)

        # Phase 3: Phonetic similarity (only un-clustered places)
        print(flush=True)
        print("=" * 60, flush=True)
        print("  Phase 3: Phonetic similarity", flush=True)
        print("=" * 60, flush=True)
        from .harvest.phonetic import harvest_phonetic_links

        phase3_docs = await harvest_phonetic_links(
            client, cfg, clustered_pids
        )
        stats.phase_3_pairs = len(phase3_docs)
        print(f"  → {stats.phase_3_pairs:,} pairs", flush=True)

        # Phase 4: Scoring + Clustering
        print(flush=True)
        print("=" * 60, flush=True)
        print("  Phase 4: Composite scoring and clustering", flush=True)
        print("=" * 60, flush=True)
        all_pairwise = all_hard_links + phase2_docs + phase3_docs

        # Score algorithmic soft links
        all_pairwise = score_pairwise_docs(all_pairwise, cfg.scoring)

        # Fetch coordinates for clustering
        all_pids: set[str] = set()
        for d in all_pairwise:
            all_pids.add(d.place_id_a)
            all_pids.add(d.place_id_b)

        print(f"  Fetching coordinates for {len(all_pids):,} places...", flush=True)
        place_coords = await _fetch_place_coords(client, cfg, all_pids)

        # Compute clusters
        from .clustering import compute_clusters

        membership_docs = compute_clusters(
            all_pairwise, place_coords, cfg.scoring, cfg.algorithm_version
        )
        stats.clusters_formed = len(set(m.cluster_id for m in membership_docs))
        print(f"  → {stats.clusters_formed:,} clusters, "
              f"{len(membership_docs):,} membership docs", flush=True)

        # Index results
        if not dry_run:
            print(flush=True)
            print("=" * 60, flush=True)
            print("  Indexing results", flush=True)
            print("=" * 60, flush=True)
            pw_ok, pw_err = await index_pairwise_docs(client, cfg, all_pairwise)
            mb_ok, mb_err = await index_membership_docs(client, cfg, membership_docs)
            print(f"  Pairwise: {pw_ok:,} indexed, {pw_err:,} errors", flush=True)
            print(f"  Membership: {mb_ok:,} indexed, {mb_err:,} errors", flush=True)

            # Save state
            hwm = await _get_current_hwm(client, cfg)
            stats.duration_seconds = time.monotonic() - t0
            state = ClusterState(
                last_run_mode="full",
                algorithm_version=cfg.algorithm_version,
                high_water_marks=hwm,
                run_statistics=stats,
            )
            await save_state(client, cfg, state)
        else:
            stats.duration_seconds = time.monotonic() - t0
            print("\n  DRY RUN — no documents indexed", flush=True)

    elapsed = stats.duration_seconds
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    print(flush=True)
    print("=" * 60, flush=True)
    print(f"  Complete in {h}h {m}m {s}s", flush=True)
    print(f"  Phase 1A: {stats.phase_1a_pairs:,} | "
          f"1B: {stats.phase_1b_pairs:,} | "
          f"2: {stats.phase_2_pairs:,} | "
          f"3: {stats.phase_3_pairs:,}", flush=True)
    print(f"  Clusters: {stats.clusters_formed:,}", flush=True)
    print("=" * 60, flush=True)
    return stats


async def run_incremental(cfg: ClusterConfig) -> RunStatistics:
    """Execute an incremental clustering run (since last run)."""
    stats = RunStatistics()
    t0 = time.monotonic()

    async with es_client(cfg) as client:
        await ensure_clusters_index(client, cfg)

        # Load previous state
        prev_state = await load_state(client, cfg)
        hwm = prev_state.high_water_marks
        logger.info("Loaded state: last run %s", prev_state.last_run_timestamp)

        if not prev_state.last_run_timestamp:
            logger.warning("No previous run found — falling back to full run")
            return await run_full(cfg)

        # Phase 1A: incremental
        logger.info("=== Phase 1A: Incremental authority hard links ===")
        from .harvest.hard_links import harvest_authority_hard_links

        phase1a_docs = await harvest_authority_hard_links(
            client, cfg, since=hwm.places_indexed_at
        )
        stats.phase_1a_pairs = len(phase1a_docs)

        # Phase 1B: incremental (since last contributor timestamp)
        phase1b_docs = []
        try:
            from .pg_client import pg_connection
            from .harvest.contributor_links import harvest_contributor_links

            async with pg_connection() as conn:
                phase1b_docs = await harvest_contributor_links(
                    conn, cfg, since=hwm.contributor_links_modified_at
                )
            stats.phase_1b_pairs = len(phase1b_docs)
        except Exception as e:
            logger.warning("Phase 1B skipped: %s", e)

        new_hard_links = _deduplicate_hard_links(phase1a_docs, phase1b_docs)

        # Phase 2: incremental
        logger.info("=== Phase 2: Incremental toponym co-attestation ===")
        from .harvest.exact_coattest import harvest_exact_coattestations

        phase2_docs = await harvest_exact_coattestations(
            client, cfg, since=hwm.toponyms_indexed_at
        )
        stats.phase_2_pairs = len(phase2_docs)

        # Index new pairwise docs
        if new_hard_links or phase2_docs:
            new_pairwise = new_hard_links + phase2_docs
            new_pairwise = score_pairwise_docs(new_pairwise, cfg.scoring)
            await index_pairwise_docs(client, cfg, new_pairwise)

        # Phase 3: only for NEW places not yet clustered
        new_pids: set[str] = set()
        for d in new_hard_links + phase2_docs:
            new_pids.add(d.place_id_a)
            new_pids.add(d.place_id_b)

        if new_pids:
            logger.info("=== Phase 3: Phonetic for %d new places ===", len(new_pids))
            from .harvest.phonetic import harvest_phonetic_links

            phase3_docs = await harvest_phonetic_links(
                client, cfg, new_pids, since=hwm.places_indexed_at
            )
            stats.phase_3_pairs = len(phase3_docs)
            if phase3_docs:
                phase3_docs = score_pairwise_docs(phase3_docs, cfg.scoring)
                await index_pairwise_docs(client, cfg, phase3_docs)

        # Phase 4: Full recomputation of clusters from all pairwise docs
        logger.info("=== Phase 4: Full cluster recomputation ===")
        all_pairwise = await _load_all_pairwise_docs(client, cfg)
        logger.info("Loaded %d total pairwise docs", len(all_pairwise))

        all_pids: set[str] = set()
        for d in all_pairwise:
            all_pids.add(d.place_id_a)
            all_pids.add(d.place_id_b)

        place_coords = await _fetch_place_coords(client, cfg, all_pids)

        from .clustering import compute_clusters

        membership_docs = compute_clusters(
            all_pairwise, place_coords, cfg.scoring, cfg.algorithm_version
        )
        stats.clusters_formed = len(set(m.cluster_id for m in membership_docs))

        # Re-index all membership docs
        await index_membership_docs(client, cfg, membership_docs)

        # Clean up stale memberships
        valid_pids = set(m.place_id for m in membership_docs)
        await delete_stale_memberships(client, cfg, valid_pids)

        # Save state
        new_hwm = await _get_current_hwm(client, cfg)
        stats.duration_seconds = time.monotonic() - t0
        state = ClusterState(
            last_run_mode="incremental",
            algorithm_version=cfg.algorithm_version,
            high_water_marks=new_hwm,
            run_statistics=stats,
        )
        await save_state(client, cfg, state)

    logger.info("Incremental run complete in %.1f seconds", stats.duration_seconds)
    return stats


async def show_stats(cfg: ClusterConfig) -> None:
    """Print current state and index statistics."""
    async with es_client(cfg) as client:
        state = await load_state(client, cfg)
        print(f"Last run: {state.last_run_timestamp}")
        print(f"Mode: {state.last_run_mode}")
        print(f"Algorithm: {state.algorithm_version}")
        print(f"HWM places: {state.high_water_marks.places_indexed_at}")
        print(f"HWM toponyms: {state.high_water_marks.toponyms_indexed_at}")
        print(f"HWM contributor: {state.high_water_marks.contributor_links_modified_at}")

        rs = state.run_statistics
        print(f"\nStatistics:")
        print(f"  Phase 1A pairs: {rs.phase_1a_pairs:,}")
        print(f"  Phase 1B pairs: {rs.phase_1b_pairs:,}")
        print(f"  Phase 2 pairs:  {rs.phase_2_pairs:,}")
        print(f"  Phase 3 pairs:  {rs.phase_3_pairs:,}")
        print(f"  Clusters:       {rs.clusters_formed:,}")
        print(f"  Duration:       {rs.duration_seconds:.1f}s")

        # Check clusters index
        try:
            exists = await client.indices.exists(index=cfg.clusters_index)
            if exists:
                count_resp = await client.count(index=cfg.clusters_index)
                print(f"\nClusters index docs: {count_resp['count']:,}")

                # Breakdown by doc_type
                resp = await client.search(
                    index=cfg.clusters_index,
                    body={
                        "size": 0,
                        "aggs": {
                            "types": {"terms": {"field": "doc_type"}}
                        },
                    },
                )
                for bucket in resp["aggregations"]["types"]["buckets"]:
                    print(f"  {bucket['key']}: {bucket['doc_count']:,}")
            else:
                print(f"\nClusters index does not exist yet")
        except Exception as e:
            print(f"\nCould not query clusters index: {e}")


def _deduplicate_hard_links(
    phase1a: list[PairwiseDoc],
    phase1b: list[PairwiseDoc],
) -> list[PairwiseDoc]:
    """
    Merge Phase 1A and 1B docs, deduplicating by canonical pair.

    If the same pair appears in both, prefer authority_sameAs link_class
    and merge link_method values.
    """
    merged: dict[str, PairwiseDoc] = {}

    for doc in phase1a:
        doc_id = PairwiseDoc.make_id(doc.place_id_a, doc.place_id_b)
        merged[doc_id] = doc

    for doc in phase1b:
        doc_id = PairwiseDoc.make_id(doc.place_id_a, doc.place_id_b)
        if doc_id in merged:
            existing = merged[doc_id]
            if "whg_reconciliation" not in existing.link_method:
                existing.link_method = f"{existing.link_method},whg_reconciliation"
        else:
            merged[doc_id] = doc

    return list(merged.values())


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="WHG Place Cluster Pipeline",
        prog="python -m clustering.runner",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--full", action="store_true", help="Full initial run")
    group.add_argument("--incremental", action="store_true", help="Incremental run")
    group.add_argument("--stats", action="store_true", help="Show statistics")

    parser.add_argument("--dry-run", action="store_true", help="Don't index results")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    # ES connection overrides (used by es.sh wrapper)
    parser.add_argument("--es-host", type=str, default=None,
                        help="Elasticsearch URL (e.g. http://localhost:9201)")
    parser.add_argument("--es-pass-file", type=str, default=None,
                        help="Path to file containing the elastic password")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    cfg = ClusterConfig()

    # Apply CLI overrides
    if args.es_host:
        cfg.es_url = args.es_host
    if args.es_pass_file:
        try:
            cfg.es_password = Path(args.es_pass_file).read_text().strip()
        except FileNotFoundError:
            logger.error("Password file not found: %s", args.es_pass_file)
            sys.exit(1)

    if args.stats:
        asyncio.run(show_stats(cfg))
    elif args.full:
        asyncio.run(run_full(cfg, dry_run=args.dry_run))
    elif args.incremental:
        asyncio.run(run_incremental(cfg))


if __name__ == "__main__":
    main()

