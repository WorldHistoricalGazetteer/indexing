"""
Test script to inspect the pairs Parquet file for missing scripts, and verify existence of panphon embeddings in Elasticsearch.

Usage:

srun -p htc --mem=64G --cpus-per-task=4 --pty bash
cd /ix1/whcdh/elastic
python -m testing.inspect_pairs

"""

import json
import duckdb
import os
from elasticsearch import Elasticsearch
from processing.settings import ES_HOST

# Paths
VOCAB_PATH = '/ix1/whcdh/models/phonetic/data/v5/vocab/script_vocab.json'
PARQUET_PATH = '/ix1/whcdh/models/phonetic/data/v5/pairs/positive_pairs.parquet'

# 1. Load the Master Script List from Vocab
with open(VOCAB_PATH, 'r') as f:
    vocab_data = json.load(f)
    master_scripts = set(vocab_data['script_to_id'].keys())

# 2. Extract Scripts found in the Parquet file
con = duckdb.connect()

print(f"Checking coverage in: {PARQUET_PATH}")
print("-" * 60)

# SQL to extract and split bin keys (e.g., 'LATIN:en|CYRILLIC:ru')
# We split by '|', then take the part before the ':'
scripts_in_pairs_query = f"""
    WITH split_bins AS (
        SELECT unnest(string_split(bin, '|')) as full_part
        FROM read_parquet('{PARQUET_PATH}')
    ),
    extracted_scripts AS (
        SELECT DISTINCT split_part(full_part, ':', 1) as script_name
        FROM split_bins
    )
    SELECT script_name FROM extracted_scripts
"""

try:
    results = con.execute(scripts_in_pairs_query).fetchall()
    found_scripts = set(r[0] for r in results)
except Exception as e:
    print(f"Error reading Parquet: {e}")
    exit(1)

# 3. Compare sets
missing_scripts = sorted(master_scripts - found_scripts)
present_scripts = sorted(master_scripts & found_scripts)
extra_scripts = sorted(found_scripts - master_scripts) # Scripts in data but not in vocab

# 4. Reporting
print(f"Vocab Scripts Found:   {len(present_scripts)} / {len(master_scripts)}")
print(f"Vocab Scripts Missing: {len(missing_scripts)}")

if present_scripts:
    print("\n[+] REPRESENTED SCRIPTS:")
    # Get counts for present scripts to check for "thin" representation
    counts_query = f"""
        WITH split_bins AS (
            SELECT unnest(string_split(bin, '|')) as full_part
            FROM read_parquet('{PARQUET_PATH}')
        )
        SELECT split_part(full_part, ':', 1) as script, count(*) as total
        FROM split_bins
        GROUP BY script
        ORDER BY total DESC
    """
    counts = con.execute(counts_query).fetchall()
    for script, total in counts:
        print(f"  {total:12,} pairs - {script}")

if missing_scripts:
    print("\n[-] MISSING SCRIPTS (Zero training pairs):")
    for script in missing_scripts:
        print(f"  !! MISSING !! - {script}")

if extra_scripts:
    print("\n[?] UNEXPECTED SCRIPTS (In data but not in script_vocab.json):")
    for script in extra_scripts:
        print(f"  ? UNKNOWN ?  - {script}")

# 5. Check for Cross-Script links (Crucial for "Londres/Лондон")
cross_query = f"""
    SELECT count(*) 
    FROM read_parquet('{PARQUET_PATH}')
    WHERE split_part(bin, '|', 1) != split_part(bin, '|', 2)
"""
cross_count = con.execute(cross_query).fetchone()[0]
print("-" * 60)
print(f"Total Cross-Script Pairs: {cross_count:,}")

# 6. Check Elasticsearch for missing scripts
print("\n" + "=" * 60)
print("ELASTICSEARCH PANPHON EMBEDDING CHECK")
print("=" * 60)

try:
    es = Elasticsearch([ES_HOST])
    print(f"Connected to ES at: {ES_HOST}")

    if missing_scripts:
        print("\nChecking missing scripts in ES 'toponyms' index...")
        print("(Looking for toponyms WITH panphon_embedding)")

        for script in missing_scripts:
            # Count toponyms with this script that HAVE panphon embeddings
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"script": script}},
                            {"exists": {"field": "panphon_embedding"}}
                        ]
                    }
                }
            }

            try:
                result = es.count(index="toponyms", body=query)
                count = result['count']

                if count > 0:
                    print(f"  ✓ {script:15} - {count:,} toponyms WITH panphon_embedding")

                    # Get a sample to see what languages are present
                    sample = es.search(
                        index="toponyms",
                        body={
                            "query": query["query"],
                            "size": 5,
                            "_source": ["name", "lang", "script"]
                        }
                    )

                    if sample['hits']['hits']:
                        langs = set(hit['_source'].get('lang', 'unknown') for hit in sample['hits']['hits'])
                        print(f"      Languages: {', '.join(sorted(langs))}")
                        print(f"      Sample: {sample['hits']['hits'][0]['_source']['name']}")
                else:
                    print(f"  ✗ {script:15} - 0 toponyms with panphon_embedding")

            except Exception as e:
                print(f"  ERROR checking {script}: {e}")

    # Also check total counts for missing scripts (with or without embeddings)
    print("\n" + "-" * 60)
    print("Total toponyms per missing script (regardless of panphon):")
    for script in missing_scripts:
        query = {"query": {"term": {"script": script}}}
        try:
            result = es.count(index="toponyms", body=query)
            count = result['count']
            print(f"  {script:15} - {count:,} total toponyms")
        except Exception as e:
            print(f"  {script:15} - ERROR: {e}")

except Exception as e:
    print(f"Failed to connect to Elasticsearch: {e}")
    print("Skipping ES checks.")
