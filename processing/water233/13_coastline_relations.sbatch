#!/bin/bash
#SBATCH --job-name=water233-coastrel
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/13-coastrel-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/13-coastrel-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=32G

# place#233 — CONTROL 1 FAILED on one row. Investigate before deciding.
#
#     natural=coastline  relation   planet 187   filtered 0   ratio 0.0000
#
# Cause is not in doubt: the filter expression is `w/natural=coastline`,
# ways only, exactly as the issue specifies. So relations carrying that tag
# were never selected. The question that matters is whether any coastline
# GEOMETRY was lost with them, and that depends on something not yet
# measured: whether their member ways are themselves tagged
# natural=coastline. If they are, the ways were selected on their own account
# and nothing is missing. If they are not, real coast is absent from the
# ocean input and osmcoastline will produce a hole.
#
# ⚠️ The wrong response here would be to relax the control until it passes.
# `natural=coastline` on a relation is unusual tagging and probably harmless,
# but "probably harmless" is a belief and this is the ocean, which is the
# headline deliverable. Measure it.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
OSMIUM="env LD_LIBRARY_PATH=$CONDA_PREFIX/lib osmium"

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
PIN=/ix1/ishi/data/authorities/osm/.pin-place233/planet-pinned.osm.pbf
OUT="${RUN_ROOT}/data/coastline-relations.osm.pbf"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0

echo "=== pulling the 187 natural=coastline RELATIONS plus their members ==="
${OSMIUM} tags-filter "${PIN}" r/natural=coastline \
    --output "${OUT}" --output-format pbf --overwrite
ls -la "${OUT}"
${OSMIUM} fileinfo -e "${OUT}" 2>&1 | grep -E "Number of (nodes|ways|relations)"

echo
echo "=== are their member ways ALSO tagged natural=coastline? ==="
python - "${OUT}" <<'PY'
import collections, sys
import osmium

path = sys.argv[1]
way_is_coastline = {}
for w in osmium.FileProcessor(path, osmium.osm.WAY):
    way_is_coastline[w.id] = (w.tags.get("natural") == "coastline")

rels = 0
member_ways = 0
members_tagged = 0
members_untagged = 0
untagged_examples = []
per_rel = []
for r in osmium.FileProcessor(path, osmium.osm.RELATION):
    if r.tags.get("natural") != "coastline":
        continue
    rels += 1
    ws = [m.ref for m in r.members if m.type == "w"]
    tagged = sum(1 for wid in ws if way_is_coastline.get(wid))
    untagged = sum(1 for wid in ws if way_is_coastline.get(wid) is False)
    member_ways += len(ws)
    members_tagged += tagged
    members_untagged += untagged
    per_rel.append((len(ws), untagged, r.tags.get("name") or f"relation/{r.id}", r.id))
    if untagged:
        for wid in ws:
            if way_is_coastline.get(wid) is False and len(untagged_examples) < 10:
                untagged_examples.append((r.id, wid))

print(f"  natural=coastline relations found : {rels:,}")
print(f"  member ways (all)                 : {member_ways:,}")
print(f"    members TAGGED natural=coastline: {members_tagged:,}")
print(f"    members NOT so tagged           : {members_untagged:,}")
print()
per_rel.sort(key=lambda t: -t[1])
print("  relations with the most untagged member ways:")
for nways, unt, name, rid in per_rel[:10]:
    print(f"    r{rid:<12} {str(name)[:38]:40s} ways={nways:5,} untagged={unt:5,}")
print()
if members_untagged == 0:
    print("  VERDICT: every member way of every natural=coastline relation is")
    print("  ITSELF tagged natural=coastline, so all of them were selected by")
    print("  the w/natural=coastline filter on their own account. NO COASTLINE")
    print("  GEOMETRY WAS LOST. The 187 relations carry no geometry of their")
    print("  own -- a relation is a grouping, and osmcoastline works from ways.")
    print("  The control-1 row is a true difference with no consequence.")
    sys.exit(0)
print(f"  *** VERDICT: {members_untagged:,} member ways are NOT tagged")
print(f"  *** natural=coastline and were therefore NOT selected. Real coast")
print(f"  *** may be missing from the ocean input. The filter needs")
print(f"  *** r/natural=coastline adding, and Step 0c must be re-run.")
sys.exit(3)
PY
echo "=== DONE $(date -Is) ==="
