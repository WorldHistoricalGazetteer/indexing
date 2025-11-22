# authorities/create_indices.py

import json
from elasticsearch8 import Elasticsearch
from authorities.settings import ES_HOST

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


def create_index(index_name, schema_file):
    """
    Create an Elasticsearch index with the given schema.
    """
    # Read schema from file
    with open(schema_file, 'r') as f:
        schema = json.load(f)

    # Delete index if it exists
    if es.indices.exists(index=index_name):
        print(f"Index '{index_name}' already exists. Deleting...")
        es.indices.delete(index=index_name)

    # Create index
    print(f"Creating index '{index_name}'...")
    es.indices.create(index=index_name, body=schema)
    print(f"Index '{index_name}' created successfully.")


if __name__ == "__main__":
    print("=" * 80)
    print("ELASTICSEARCH INDEX SETUP")
    print("=" * 80)

    # Create ingest pipeline first (before indices that reference it)
    print("\n--- Creating Ingest Pipelines ---")
    create_pipeline("extract_namespace", "schemas/places_pipeline.json")

    # Create indices
    print("\n--- Creating Indices ---")

    # Create places index
    create_index("places", "schemas/places.json")

    # Create toponyms index
    create_index("toponyms", "schemas/toponyms.json")

    print("\n" + "=" * 80)
    print("SETUP COMPLETE!")
    print("=" * 80)
    print("\nNext steps (examoples):")
    print("1. Run: python -m authorities.geonames-places")
    print("2. Run: python -m authorities.geonames-toponyms")
    print("3. Run: python -m authorities.tgn-places")
    print("4. Run: python -m authorities.pleiades-places")
    print("5. Run: python -m authorities.gb1900-places")