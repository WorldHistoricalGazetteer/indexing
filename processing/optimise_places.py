import sys
import os
import time
from elasticsearch import Elasticsearch

# --- IMPORT SETUP ---
sys.path.append(os.getcwd())

try:
    from processing.settings import ES_HOST
except ImportError:
    print("❌ Could not import settings.")
    print("   Run from project root: python processing/optimize_places.py")
    sys.exit(1)

# --- CONFIGURATION ---
ORIGINAL_INDEX = "places"
STAGING_INDEX = "places-staging"
SHARDS = 4

# Connect
print(f"Connecting to {ES_HOST}...")
es = Elasticsearch(ES_HOST)


def check_disk_space():
    """Ensures we have disk space for the double-copy strategy."""
    print("\n🛡️  Checking Disk Space...")
    try:
        stats = es.indices.stats(index=ORIGINAL_INDEX)
        index_size = stats['_all']['primaries']['store']['size_in_bytes']
        required_space = index_size * 2.5

        node_stats = es.nodes.stats(metric='fs')
        node_id = list(node_stats['nodes'].keys())[0]
        available_space = node_stats['nodes'][node_id]['fs']['total']['available_in_bytes']

        gb_required = required_space / (1024 ** 3)
        gb_available = available_space / (1024 ** 3)

        print(f"   Need: ~{gb_required:.2f}GB | Available: {gb_available:.2f}GB")

        if available_space < required_space:
            print("   ❌ FATAL: Not enough disk space. Aborting.")
            sys.exit(1)
        print("   ✅ Disk space sufficient.")

    except Exception as e:
        print(f"   ⚠️  Could not verify disk stats: {e}")


def create_optimized_index(index_name, source_mapping_index):
    """Creates index with 4 shards, best compression, and no refresh."""
    print(f"    Fetching mappings from {source_mapping_index}...")
    mappings = es.indices.get_mapping(index=source_mapping_index)[source_mapping_index]['mappings']

    settings = {
        "number_of_shards": SHARDS,
        "number_of_replicas": 0,
        "refresh_interval": "-1",
        "index.codec": "best_compression",
        "translog.durability": "async"
    }

    if es.indices.exists(index=index_name):
        print(f"⚠️  Index {index_name} already exists. Deleting it...")
        es.indices.delete(index=index_name)

    es.indices.create(index=index_name, mappings=mappings, settings=settings)
    print(f"✅  Created {index_name} (4 shards, best_compression).")


def run_reindex(src, dest):
    """Runs a parallel sliced reindex with robust monitoring."""
    print(f"\n🚀 Starting Reindex (Sliced): {src} -> {dest}")

    response = es.reindex(
        body={"source": {"index": src}, "dest": {"index": dest}},
        slices=SHARDS,
        wait_for_completion=False
    )
    task_id = response['task']
    print(f"   Task started: {task_id}")

    failures = 0
    while True:
        try:
            task = es.tasks.get(task_id=task_id)
            status = task['task']['status']

            # Simplified progress tracking
            processed = status.get('created', 0)
            total = status.get('total', 0)

            if total > 0:
                percent = (processed / total) * 100
                sys.stdout.write(f"\r   Progress: {percent:.2f}% ({processed:,} docs)")
                sys.stdout.flush()

            if task['completed']:
                print("\n✅ Reindex complete.")
                break

            # Reset failure counter on successful check
            failures = 0

        except Exception as e:
            failures += 1
            print(f"\n⚠️  Monitor glitch ({failures}/10): {e}")
            if failures >= 10:
                print("❌ FATAL: Lost contact with task monitor too many times. Aborting script to be safe.")
                sys.exit(1)

        time.sleep(5)

    # Sanity Check
    es.indices.refresh(index=dest)
    count_src = es.count(index=src)['count']
    count_dest = es.count(index=dest)['count']
    print(f"   Sanity Check: {src}({count_src:,}) vs {dest}({count_dest:,})")

    if count_dest < (count_src * 0.90):
        print("❌ CRITICAL: Data loss detected. Aborting.")
        sys.exit(1)


def main():
    print("==============================================")
    print("      PLACES INDEX OPTIMIZER (Safe Mode)      ")
    print("==============================================")

    if not es.ping():
        print(f"❌ Could not connect to {ES_HOST}")
        sys.exit(1)

    # 1. DISK CHECK
    check_disk_space()

    # 2. STAGING COPY
    print("\n--- STEP 1: Copy Original -> Staging ---")
    create_optimized_index(STAGING_INDEX, ORIGINAL_INDEX)
    run_reindex(ORIGINAL_INDEX, STAGING_INDEX)

    # 3. DELETE ORIGINAL
    print("\n--- STEP 2: Delete Original Index ---")
    confirm = input(f"Are you sure you want to DELETE '{ORIGINAL_INDEX}'? (Type 'DELETE'): ")
    if confirm != "DELETE":
        sys.exit(0)

    es.indices.delete(index=ORIGINAL_INDEX)
    print(f"🗑️  Deleted {ORIGINAL_INDEX}.")

    # 4. RESTORE COPY
    print("\n--- STEP 3: Copy Staging -> New Original ---")
    create_optimized_index(ORIGINAL_INDEX, STAGING_INDEX)
    run_reindex(STAGING_INDEX, ORIGINAL_INDEX)

    # 5. CLEANUP
    print("\n--- STEP 4: Cleanup & Optimize ---")
    es.indices.delete(index=STAGING_INDEX)

    print("Triggering Force Merge (max_num_segments=1)...")
    es.indices.forcemerge(index=ORIGINAL_INDEX, max_num_segments=1)

    print("\n🎉 SUCCESS: 'places' optimized (4 shards, best_compression, refresh=-1).")


if __name__ == "__main__":
    main()