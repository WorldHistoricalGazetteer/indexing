import gzip
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
        indices: Index name(s) to snapshot (str or list)
        snapshot_name: Name for the snapshot (will be prefixed with timestamp)
        repo_name: Snapshot repository name

    Returns:
        dict with snapshot info or None on failure
    """
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    full_name = f"{snapshot_name}_{timestamp}"

    indices = [PLACES_INDEX, TOPONYMS_INDEX]

    print(f"\nCreating checkpoint snapshot: {full_name}")
    print(f"  Indices: {', '.join(indices)}")

    try:
        response = es.snapshot.create(
            repository=repo_name,
            snapshot=full_name,
            body={
                "indices": ",".join(indices),
                "ignore_unavailable": True,
                "include_global_state": False
            },
            wait_for_completion=True
        )

        state = response.get("snapshot", {}).get("state", "UNKNOWN")
        if state == "SUCCESS":
            print(f"  ✓ Snapshot created: {full_name}")
            return response
        else:
            print(f"  ✗ Snapshot state: {state}")
            return None

    except Exception as e:
        print(f"  ✗ Snapshot failed: {e}")
        return None
