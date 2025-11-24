import json
import time
import os

from elasticsearch import Elasticsearch
from dotenv import load_dotenv

load_dotenv()
ES_HOST = os.getenv("ES_HOST_URL")

STAGING_REPO_NAME = os.getenv("ES_STAGING_REPO_NAME")
STAGING_REPO_LOCATION = os.getenv("ES_STAGING_REPO_LOCATION")

BACKUP_REPO_NAME = os.getenv("ES_BACKUP_REPO_NAME")
BACKUP_REPO_LOCATION = os.getenv("ES_BACKUP_REPO_LOCATION")

if not ES_HOST:
    raise EnvironmentError("ES_HOST_URL not found in environment variables.")

es = Elasticsearch(ES_HOST, request_timeout=180)


def create_pipeline(pipeline_name, pipeline_file):
    """
    Create an Elasticsearch ingest pipeline.
    """
    # Read pipeline from file
    with open(pipeline_file, 'r') as f:
        pipeline = json.load(f)

    # Delete pipeline if it exists
    try:
        # Use get_pipeline() to check existence, or use exists_pipeline() in newer ES versions
        es.ingest.delete_pipeline(id=pipeline_name)
        print(f"Pipeline '{pipeline_name}' already existed. Deleting...")
    except Exception as e:
        # Handle case where the pipeline does not exist (404 error)
        if 'not found' not in str(e):
            # Only print if it's an unexpected error
            print(f"Error checking/deleting pipeline '{pipeline_name}': {e}")
        pass

    # Create pipeline
    print(f"Creating pipeline '{pipeline_name}'...")
    es.ingest.put_pipeline(id=pipeline_name, body=pipeline)
    print(f"Pipeline '{pipeline_name}' created successfully.")


def create_index(index_name, schema_file, pipeline=None):
    """
    Create an Elasticsearch index with the given schema.

    Args:
        index_name: Name of the index to create
        schema_file: Path to the schema JSON file
        pipeline: Name of the default ingest pipeline (optional)
    """
    with open(schema_file, 'r') as f:
        schema = json.load(f)

    if pipeline:
        schema.setdefault('settings', {})['default_pipeline'] = pipeline

    if es.indices.exists(index=index_name):
        print(f"Index '{index_name}' already exists. Deleting...")
        # The API-level timeout is set here to ensure the server has time to complete the deletion.
        es.indices.delete(index=index_name, timeout="60s")
        # Wait for deletion completion
        for _ in range(30):
            if not es.indices.exists(index=index_name):
                break
            time.sleep(1)

    print(f"Creating index '{index_name}'...")
    # This timeout is for the server-side operation; the client timeout is now set globally.
    es.indices.create(index=index_name, body=schema, timeout="60s")
    print(f"Index '{index_name}' created successfully.")


def create_snapshot_repository(repo_name, location):
    """
    Register a shared file system snapshot repository at a specific location.
    """
    print(f"\n--- Checking Snapshot Repository '{repo_name}' ---")

    # Ensure the physical subdirectory exists (important for shared filesystem repos)
    import os
    os.makedirs(location, exist_ok=True)
    print(f"Ensured directory exists: {location}")

    try:
        es.snapshot.get_repository(repository=repo_name)
        print(f"Repository '{repo_name}' already exists.")
        return
    except Exception as e:
        if 'repository_not_found_exception' not in str(e):
            pass

    print(f"Creating repository '{repo_name}' at location '{location}'...")
    try:
        es.snapshot.create_repository(
            repository=repo_name,
            body={
                "type": "fs",
                "settings": {
                    "location": location
                }
            }
        )
        print(f"Repository '{repo_name}' created successfully.")
    except Exception as e:
        print(f"ERROR: Failed to create repository '{repo_name}'. Check ES logs. Error: {e}")


def create_index_snapshot(index_name, repo_name="staging", suffix="_staging"):
    """
    Create an immediate snapshot of a specific index, using an optional suffix.
    """
    # Snapshot names are unique; use index name, timestamp, and the staging suffix
    timestamp = time.strftime('%Y%m%d%H%M%S')

    # Construct the snapshot name: index_name + suffix + timestamp
    # Example: places_staging_20251124155200
    snapshot_name = f"{index_name}{suffix}_{timestamp}"

    print(f"Triggering snapshot '{snapshot_name}' for index '{index_name}'...")

    try:
        response = es.snapshot.create(
            repository=repo_name,
            snapshot=snapshot_name,
            body={
                "indices": index_name,
                "ignore_unavailable": True,
                "include_global_state": False  # Only snapshot the index, not cluster metadata
            },
            wait_for_completion=True  # Script waits until snapshot is done
        )
        # Check if the snapshot finished successfully
        if response.get("snapshot", {}).get("state") == "SUCCESS":
            print(f"Snapshot '{snapshot_name}' (Index: {index_name}) created successfully.")
        else:
            print(
                f"WARNING: Snapshot '{snapshot_name}' finished with state: {response.get('snapshot', {}).get('state')}")

    except Exception as e:
        print(f"ERROR: Failed to create snapshot for '{index_name}'. Error: {e}")


if __name__ == "__main__":
    print("=" * 80)
    print("ELASTICSEARCH INDEX & SNAPSHOT SETUP")
    print("=" * 80)

    # 1. Create BOTH snapshot repositories
    print("\n--- Registering Snapshot Repositories ---")
    create_snapshot_repository(STAGING_REPO_NAME, STAGING_REPO_LOCATION)
    create_snapshot_repository(BACKUP_REPO_NAME, BACKUP_REPO_LOCATION)

    # Create ingest pipelines first (before indices that reference them)
    print("\n--- Creating Ingest Pipelines ---")
    create_pipeline("extract_namespace", "schemas/places_pipeline.json")
    create_pipeline("extract_language", "schemas/toponyms_pipeline.json")

    # Create indices with their default pipelines
    print("\n--- Creating Indices ---")

    # Create places index with extract_namespace pipeline
    create_index("places", "schemas/places.json", pipeline="extract_namespace")

    # Create toponyms index with extract_language pipeline
    create_index("toponyms", "schemas/toponyms.json", pipeline="extract_language")

    # Create immediate snapshots of both indices in the staging repository
    print("\n--- Creating Immediate Snapshots of Indices ---")
    create_index_snapshot("places", repo_name=STAGING_REPO_NAME, suffix="_staging")
    create_index_snapshot("toponyms", repo_name=STAGING_REPO_NAME, suffix="_staging")

    print("\n" + "=" * 80)
    print("SETUP COMPLETE!")
    print("=" * 80)
