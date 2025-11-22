# processing/create_indices.py

import json
from elasticsearch8 import Elasticsearch
from processing.settings import ES_HOST

es = Elasticsearch(ES_HOST)


def create_pipeline(pipeline_name, pipeline_file):
    """
    Create an Elasticsearch ingest pipeline.
    """
    # Read pipeline from file
    with open(pipeline_file, 'r') as f:
        pipeline = json.load(f)

    # Delete pipeline if it exists
    try:
        if es.ingest.get_pipeline(id=pipeline_name):
            print(f"Pipeline '{pipeline_name}' already exists. Deleting...")
            es.ingest.delete_pipeline(id=pipeline_name)
    except:
        pass  # Pipeline doesn't exist

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
    # Read schema from file
    with open(schema_file, 'r') as f:
        schema = json.load(f)

    # Add default pipeline if specified
    if pipeline:
        if 'settings' not in schema:
            schema['settings'] = {}
        schema['settings']['default_pipeline'] = pipeline

    # Delete index if it exists
    if es.indices.exists(index=index_name):
        print(f"Index '{index_name}' already exists. Deleting...")
        es.indices.delete(index=index_name)

    # Create index
    print(f"Creating index '{index_name}'...")
    es.indices.create(index=index_name, body=schema)
    print(f"Index '{index_name}' created successfully.")


def update_index_settings_for_production(index_name):
    """
    Update index settings for production use.
    This sets replicas to 1 and refresh interval to 1s.
    """
    print(f"Updating {index_name} settings for production...")

    # First, ensure index is not write-blocked
    es.indices.put_settings(
        index=index_name,
        body={
            "index.blocks.read_only_allow_delete": None
        }
    )

    # Update settings for production
    es.indices.put_settings(
        index=index_name,
        body={
            "number_of_replicas": 1,
            "refresh_interval": "1s"
        }
    )
    print(f"✓ Updated {index_name} settings for production")


if __name__ == "__main__":
    print("=" * 80)
    print("ELASTICSEARCH INDEX SETUP")
    print("=" * 80)

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

    print("\n" + "=" * 80)
    print("SETUP COMPLETE!")
    print("=" * 80)
    print("\nNext steps (examples):")
    print("1. Run: python -m processing.geonames-places")
    print("2. Run: python -m processing.geonames-toponyms")
    print("3. Run: python -m processing.tgn-places")
    print("4. Run: python -m processing.pleiades-places")
    print("5. Run: python -m processing.gb1900-places")
    print("6. Run: python -m processing.wikidata-places")
    print("7. Run: python -m processing.un-countries")
    print("\nTo prepare for production after all ingestion:")
    print("python -m processing.prepare_for_production")