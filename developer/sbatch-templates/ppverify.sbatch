#!/bin/bash
#SBATCH --job-name=ipa-ppverify
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=04:00:00
#SBATCH --output=/vast/ishi/ipa-v8/logs/ppverify-%j.out
#SBATCH --error=/vast/ishi/ipa-v8/logs/ppverify-%j.err
# Verification is a job, not a login-node command. And it reads the candidate
# INDEPENDENTLY of the process that wrote it — a merge reporting its own success
# is one witness; this is the second.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH=/vast/ishi/ipa-v8/code:/vast/ishi/elastic
cd /vast/ishi/ipa-v8/code
python -u -m phonetics.ipa.panphon_repop inspect \
  --inventory-db /ix1/ishi/data/toponyms-panphon-CANDIDATE.db \
  --out /vast/ishi/ipa-v8/logs/candidate_schema.json
echo "=== provenance + blob layout, read independently ==="
python -u - <<'PY'
import duckdb, struct
con = duckdb.connect()
con.execute("ATTACH '/ix1/ishi/data/toponyms-panphon-CANDIDATE.db' AS c (READ_ONLY)")
for row in con.execute("SELECT * FROM c.panphon_provenance").fetchall():
    for name, v in zip([d[0] for d in con.description], row):
        print(f"  {name}: {v}")
print("--- blob layout: must be a whole number of 24-float32 segments ---")
bad = con.execute("""
    SELECT count(*) FROM c.toponyms
    WHERE panphon_features IS NOT NULL AND octet_length(panphon_features) % 96 <> 0
""").fetchone()[0]
print(f"  ragged blobs: {bad}  (of {con.execute('SELECT count(panphon_features) FROM c.toponyms').fetchone()[0]:,})")
print("--- orphans: rows WITH ipa but WITHOUT features ---")
o = con.execute("""
    SELECT count(*) FROM c.toponyms
    WHERE ipa IS NOT NULL AND ipa <> '' AND panphon_features IS NULL
""").fetchone()[0]
print(f"  {o}   (expected 139 — PanPhon could not segment those)")
print("--- three real rows ---")
for t, ipa, n in con.execute("""
    SELECT toponym_id, ipa, octet_length(panphon_features) FROM c.toponyms
    WHERE panphon_features IS NOT NULL LIMIT 3""").fetchall():
    print(f"  {t!r} ipa={ipa!r} bytes={n} segments={n//96}")
PY
