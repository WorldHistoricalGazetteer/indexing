import duckdb
import os

# Configuration
PARQUET_PATH = '/ix1/whcdh/models/phonetic/data/v5/pairs/positive_pairs.parquet'

if not os.path.exists(PARQUET_PATH):
    print(f"Error: File not found at {PARQUET_PATH}")
    exit(1)

# Connect to DuckDB (in-memory)
con = duckdb.connect()

print(f"Analyzing: {PARQUET_PATH}")
print("-" * 50)

# 1. Check for specific scripts
# ILIKE is case-insensitive
scripts_to_check = ['GREEK', 'HEBREW', 'HIRAGANA', 'KATAKANA']

for script in scripts_to_check:
    query = f"""
        SELECT count(*) 
        FROM read_parquet('{PARQUET_PATH}') 
        WHERE bin ILIKE '%{script}%'
    """
    count = con.execute(query).fetchone()[0]
    print(f"{script:<10}: {count:,} pairs")

print("-" * 50)

# 2. Extract unique scripts from all bin keys
# This uses DuckDB's powerful string splitting and unnesting
print("Extracting all unique scripts in dataset...")

unique_scripts_query = f"""
    WITH bin_parts AS (
        SELECT DISTINCT unnest(string_split_regex(bin, '[|]')) as part
        FROM read_parquet('{PARQUET_PATH}')
    ),
    script_names AS (
        SELECT DISTINCT split_part(part, ':', 1) as script
        FROM bin_parts
    )
    SELECT script FROM script_names ORDER BY script
"""

results = con.execute(unique_scripts_query).fetchall()
all_scripts = [r[0] for r in results]

print(f"\nFound {len(all_scripts)} unique scripts:")
print(", ".join(all_scripts))

# 3. Quick count of top 10 bins for context
print("\nTop 10 Bins by size:")
top_bins = con.execute(f"""
    SELECT bin, count(*) as total 
    FROM read_parquet('{PARQUET_PATH}') 
    GROUP BY bin 
    ORDER BY total DESC 
    LIMIT 10
""").fetchall()

for bin_name, total in top_bins:
    print(f"  {total:,} - {bin_name}")