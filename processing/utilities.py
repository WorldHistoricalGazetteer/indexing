import gzip
import time
import zipfile

from processing.settings import STAGING_REPO_NAME, PLACES_INDEX,  TOPONYMS_INDEX


def stream_file(file_path):
    """
    Generator yielding lines from .txt, .gz, or .zip files.
    ZIP files are streamed without extraction.
    """
    if file_path.endswith(".gz"):
        with gzip.open(file_path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")

    elif file_path.endswith(".zip"):
        with zipfile.ZipFile(file_path, 'r') as zf:
            # Assume there is only one relevant file in the zip (like alternateNamesV2.txt)
            for name in zf.namelist():
                if name.endswith('.txt'):
                    with zf.open(name, 'r') as f:
                        for line in f:
                            yield line.decode('utf-8').strip()

    else:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")


def create_checkpoint_snapshot(es, snapshot_name="checkpoint", repo_name=STAGING_REPO_NAME):
    """
    Create a checkpoint snapshot after completing a logical unit of work.

    Args:
        es: Elasticsearch client
        snapshot_name: Name for the snapshot (will be prefixed with timestamp)
        repo_name: Snapshot repository name

    Returns:
        dict with snapshot info or None on failure
    """
    from datetime import datetime
    import time

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    full_name = f"{snapshot_name}_{timestamp}"

    indices = [PLACES_INDEX, TOPONYMS_INDEX]

    print(f"\nCreating checkpoint snapshot: {full_name}")
    print(f"  Indices: {', '.join(indices)}")

    try:
        # 1. Start snapshot asynchronously (wait_for_completion=False)
        es.snapshot.create(
            repository=repo_name,
            snapshot=full_name,
            body={
                "indices": ",".join(indices),
                "ignore_unavailable": True,
                "include_global_state": False
            },
            wait_for_completion=False
        )

        # 2. Poll for completion
        print(f"  Snapshot in progress...", end="", flush=True)

        final_status = None
        while True:
            try:
                # Get specific snapshot status
                status = es.snapshot.get(repository=repo_name, snapshot=full_name)

                # Check if snapshot info is available yet
                if not status.get('snapshots'):
                    time.sleep(2)
                    continue

                snapshot_info = status['snapshots'][0]
                state = snapshot_info.get("state")

                if state == "SUCCESS":
                    final_status = snapshot_info
                    print(" Done.")  # Clean newline
                    break
                elif state in ["FAILED", "PARTIAL", "INCOMPATIBLE"]:
                    final_status = snapshot_info
                    print(f"\n  ✗ Snapshot finished with state: {state}")
                    break

                # If IN_PROGRESS, print dot and wait
                print(".", end="", flush=True)
                time.sleep(5)  # Check every 5 seconds

            except Exception as e:
                # If network blip, just retry loop
                print(f"!", end="", flush=True)
                time.sleep(5)

        # 3. Report result
        if final_status and final_status.get("state") == "SUCCESS":
            print(f"  ✓ Snapshot created successfully: {full_name}")
            return final_status
        else:
            print(f"  ✗ Snapshot failed.")
            return None

    except Exception as e:
        print(f"\n  ✗ Snapshot request failed: {e}")
        return None
