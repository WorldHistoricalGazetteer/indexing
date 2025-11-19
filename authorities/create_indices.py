# authorities/create_indices.py

import json
from elasticsearch import Elasticsearch
from authorities.settings import ES_HOST

es = Elasticsearch(ES_HOST)


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
    # Create places index
    create_index("places", "schemas/places.json")

    # Create toponyms index
    create_index("toponyms", "schemas/toponyms.json")

    print("\nAll indices created successfully!")
    print("\nNext steps:")
    print("1. Run: python authorities/geonames-places.py")
    print("2. Run: python authorities/geonames-toponyms.py")