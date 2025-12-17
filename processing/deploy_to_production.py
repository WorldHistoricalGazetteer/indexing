#!/usr/bin/env python3
"""
Deploy indices from staging snapshots to production.

This script:
1. Finds the latest staging snapshot
2. Restores to new timestamped indices
3. Adjusts settings for production query workload
4. Switches aliases atomically
5. Optionally deletes old indices
"""

import sys
from datetime import datetime

from elasticsearch import Elasticsearch
from processing.settings import ES_HOST_PRODUCTION, STAGING_REPO_NAME

es = Elasticsearch(ES_HOST_PRODUCTION, request_timeout=300)


def get_latest_snapshot(prefix=None):
    """Find the most recent successful snapshot."""
    snapshots = es.snapshot.get(
        repository=STAGING_REPO_NAME,
        snapshot="_all"
    )["snapshots"]

    # Filter to successful snapshots
    successful = [s for s in snapshots if s["state"] == "SUCCESS"]

    # Filter by prefix if specified
    if prefix:
        successful = [s for s in successful if s["snapshot"].startswith(prefix)]

    if not successful:
        return None

    # Sort by start time, most recent first
    successful.sort(key=lambda s: s["start_time"], reverse=True)
    return successful[0]["snapshot"]


def restore_snapshot(snapshot_name, index_suffix):
    """
    Restore snapshot to new indices with timestamp suffix.

    Returns dict mapping original index names to new names.
    """
    # Get snapshot info
    info = es.snapshot.get(
        repository=STAGING_REPO_NAME,
        snapshot=snapshot_name
    )["snapshots"][0]

    original_indices = info["indices"]

    # Build rename pattern
    # places -> places_20241216, toponyms -> toponyms_20241216
    index_mapping = {}
    rename_pattern = []

    for idx in original_indices:
        new_name = f"{idx}_{index_suffix}"
        index_mapping[idx] = new_name
        rename_pattern.append(f"{idx}:{new_name}")

    print(f"Restoring {snapshot_name}...")
    print(f"  Index mapping: {index_mapping}")

    # Restore with rename
    es.snapshot.restore(
        repository=STAGING_REPO_NAME,
        snapshot=snapshot_name,
        body={
            "indices": ",".join(original_indices),
            "ignore_unavailable": True,
            "include_global_state": False,
            "rename_pattern": "(.+)",
            "rename_replacement": "$1_" + index_suffix,
            "index_settings": {
                "index.number_of_replicas": 0  # Single node
            }
        },
        wait_for_completion=True
    )

    print("  ✓ Restore complete")
    return index_mapping


def configure_for_production(index_name):
    """Adjust index settings for production query workload."""
    print(f"Configuring {index_name} for production...")

    es.indices.put_settings(
        index=index_name,
        body={
            "index": {
                "refresh_interval": "1s",  # Restore real-time search
                "translog": {
                    "durability": "request",  # Restore data safety
                    "flush_threshold_size": "512mb"
                }
            }
        }
    )

    # Force refresh to make all docs searchable
    es.indices.refresh(index=index_name)

    # Force merge to optimize for queries (reduces segment count)
    print(f"  Running force merge (this may take a while)...")
    es.indices.forcemerge(index=index_name)  # Quicker with max_num_segments=5, but slower subsequent querying

    print(f"  ✓ {index_name} configured")


def switch_aliases(index_mapping):
    """
    Atomically switch aliases to point to new indices.

    index_mapping: {"places": "places_20241216", "toponyms": "toponyms_20241216"}
    """
    print("Switching aliases...")

    actions = []

    for alias_name, new_index in index_mapping.items():
        # Remove alias from any existing indices
        try:
            current = es.indices.get_alias(name=alias_name)
            for old_index in current.keys():
                actions.append({
                    "remove": {"index": old_index, "alias": alias_name}
                })
        except:
            pass  # Alias doesn't exist yet

        # Add alias to new index
        actions.append({
            "add": {"index": new_index, "alias": alias_name}
        })

    if actions:
        es.indices.update_aliases(body={"actions": actions})
        print(f"  ✓ Aliases switched: {list(index_mapping.keys())}")


def cleanup_old_indices(keep_current_alias=True, keep_count=2):
    """
    Remove old timestamped indices, keeping the most recent ones.
    """
    for base_name in ["places", "toponyms"]:
        # Find all timestamped versions
        indices = list(es.indices.get(index=f"{base_name}_*").keys())

        if len(indices) <= keep_count:
            continue

        # Sort by name (timestamp suffix means alphabetical = chronological)
        indices.sort(reverse=True)

        # Get current alias target
        try:
            alias_target = list(es.indices.get_alias(name=base_name).keys())[0]
        except:
            alias_target = None

        # Delete old indices
        for idx in indices[keep_count:]:
            if keep_current_alias and idx == alias_target:
                continue
            print(f"  Deleting old index: {idx}")
            es.indices.delete(index=idx)


def main():
    print("=" * 70)
    print("DEPLOY TO PRODUCTION")
    print("=" * 70)

    # Check staging repo is registered
    try:
        es.snapshot.get_repository(name=STAGING_REPO_NAME)
    except:
        print(f"Registering snapshot repository: {STAGING_REPO_NAME}")
        es.snapshot.create_repository(
            name=STAGING_REPO_NAME,
            body={
                "type": "fs",
                "settings": {
                    "location": "/ix1/whcdh/es/repo/staging"
                }
            }
        )

    # Find latest snapshot
    snapshot = get_latest_snapshot(prefix="complete")
    if not snapshot:
        snapshot = get_latest_snapshot()  # Fall back to any snapshot

    if not snapshot:
        print("ERROR: No snapshots found in staging repository")
        sys.exit(1)

    print(f"\nLatest snapshot: {snapshot}")

    # Confirm
    response = input("\nDeploy this snapshot to production? (y/n): ")
    if response.lower() != "y":
        print("Cancelled.")
        sys.exit(0)

    # Generate timestamp suffix for new indices
    suffix = datetime.now().strftime("%Y%m%d")

    # Restore
    index_mapping = restore_snapshot(snapshot, suffix)

    # Configure each index
    for new_index in index_mapping.values():
        configure_for_production(new_index)

    # Switch aliases
    switch_aliases(index_mapping)

    # Show final state
    print("\n" + "=" * 70)
    print("DEPLOYMENT COMPLETE")
    print("=" * 70)

    for alias_name in index_mapping.keys():
        count = es.count(index=alias_name)["count"]
        print(f"  {alias_name}: {count:,} documents")

    # Offer cleanup
    print("\nOld indices can be cleaned up to free disk space.")
    response = input("Delete old indices (keeping 2 most recent)? (y/n): ")
    if response.lower() == "y":
        cleanup_old_indices(keep_count=2)
        print("  ✓ Cleanup complete")


if __name__ == "__main__":
    main()
