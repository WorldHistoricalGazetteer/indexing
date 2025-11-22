# processing/prepare_for_production.py

"""
Prepare Elasticsearch indices for production use.

This script:
1. Updates replica settings from 0 to 1
2. Updates refresh interval from -1 to 1s
3. Forces a merge to optimize segments
4. Creates a snapshot backup
"""

from elasticsearch8 import Elasticsearch
from processing.settings import ES_HOST
import sys

es = Elasticsearch(ES_HOST)


def update_production_settings(index_name):
    """Update index settings for production."""
    print(f"Updating {index_name} settings...")

    try:
        # Update settings
        es.indices.put_settings(
            index=index_name,
            body={
                "number_of_replicas": 1,
                "refresh_interval": "1s"
            }
        )
        print(f"  ✓ Updated replicas and refresh interval")
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def force_merge_index(index_name, max_segments=1):
    """Force merge to optimize index segments."""
    print(f"Force merging {index_name}...")

    try:
        es.indices.forcemerge(
            index=index_name,
            max_num_segments=max_segments
        )
        print(f"  ✓ Force merge complete")
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def get_index_stats(index_name):
    """Get and display index statistics."""
    try:
        stats = es.indices.stats(index=index_name)
        index_stats = stats['indices'][index_name]

        # Document count
        doc_count = index_stats['primaries']['docs']['count']

        # Size
        size_bytes = index_stats['primaries']['store']['size_in_bytes']
        size_gb = size_bytes / (1024 ** 3)

        # Settings
        settings = es.indices.get_settings(index=index_name)
        replicas = settings[index_name]['settings']['index']['number_of_replicas']
        refresh = settings[index_name]['settings']['index'].get('refresh_interval', '1s')

        print(f"\n{index_name} Statistics:")
        print(f"  Documents: {doc_count:,}")
        print(f"  Size: {size_gb:.2f} GB")
        print(f"  Replicas: {replicas}")
        print(f"  Refresh interval: {refresh}")

        return True
    except Exception as e:
        print(f"Error getting stats for {index_name}: {e}")
        return False


def create_snapshot(repository_name="whg_backup", snapshot_name=None):
    """Create a snapshot of all indices."""
    import datetime

    if snapshot_name is None:
        snapshot_name = f"production_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print(f"\nCreating snapshot: {snapshot_name}")

    try:
        # Register repository if needed
        try:
            es.snapshot.get_repository(name=repository_name)
        except:
            print(f"  Creating repository: {repository_name}")
            es.snapshot.create_repository(
                name=repository_name,
                body={
                    "type": "fs",
                    "settings": {
                        "location": "/ix1/whcdh/es/repo"
                    }
                }
            )

        # Create snapshot
        es.snapshot.create(
            repository=repository_name,
            snapshot=snapshot_name,
            body={
                "indices": "places,toponyms",
                "ignore_unavailable": True,
                "include_global_state": False
            }
        )
        print(f"  ✓ Snapshot created: {snapshot_name}")
        return True
    except Exception as e:
        print(f"  ✗ Error creating snapshot: {e}")
        return False


def main():
    """Main function to prepare indices for production."""
    print("=" * 80)
    print("PREPARE INDICES FOR PRODUCTION")
    print("=" * 80)

    indices = ["places", "toponyms"]

    # Check if indices exist
    print("\nChecking indices...")
    for index in indices:
        if not es.indices.exists(index=index):
            print(f"  ✗ Index '{index}' does not exist!")
            print("\nPlease run ingestion scripts first.")
            sys.exit(1)
        print(f"  ✓ Index '{index}' exists")

    # Show current stats
    print("\n--- Current Statistics ---")
    for index in indices:
        get_index_stats(index)

    # Confirm with user
    print("\n" + "-" * 80)
    print("This script will:")
    print("1. Update number_of_replicas from 0 to 1")
    print("2. Update refresh_interval from -1 to 1s")
    print("3. Force merge indices to optimize segments")
    print("4. Create a snapshot backup")
    print("-" * 80)

    response = input("\nProceed with production preparation? (y/n): ")
    if response.lower() != 'y':
        print("Cancelled.")
        return

    # Update settings
    print("\n--- Updating Settings ---")
    for index in indices:
        update_production_settings(index)

    # Force merge
    print("\n--- Force Merging ---")
    print("This may take 30-60 minutes...")
    for index in indices:
        force_merge_index(index)

    # Create snapshot
    print("\n--- Creating Backup ---")
    create_snapshot()

    # Show final stats
    print("\n--- Final Statistics ---")
    for index in indices:
        get_index_stats(index)

    print("\n" + "=" * 80)
    print("PRODUCTION PREPARATION COMPLETE")
    print("=" * 80)
    print("\nYour indices are now ready for production use!")
    print("\nRemember to:")
    print("1. Monitor cluster health: curl http://localhost:9200/_cluster/health?pretty")
    print("2. Check snapshot status: curl http://localhost:9200/_snapshot/whg_backup/_all?pretty")
    print("3. Set up regular snapshot schedule if needed")


if __name__ == "__main__":
    main()