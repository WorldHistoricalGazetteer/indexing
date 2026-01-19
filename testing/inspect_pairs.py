import json
import duckdb
import os

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