#!/bin/bash
#SBATCH --job-name=water233-coastrel2
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/14-coastrel2-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/14-coastrel2-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=32G

# place#233 — decide the natural=coastline relation row properly.
#
# Three things job 13 does not settle, all of which change the decision:
#
# 1. WHAT ARE THESE RELATIONS? Their type=* tag says in one glance whether
#    they are multipolygons, mistagged groupings, or something else.
#    natural=coastline on a relation is not a convention, it is an error, and
#    187 probable mistakes may not deserve machinery.
#
# 2. HOW BIG IS THE HOLE, IN WAYS? 187 relations could be 187 stray ways or
#    several thousand. Those are different decisions. The way count is the
#    size of the thing, and the relation count is not.
#
# 3. ⚠️ ARE THE TAGGED MEMBER WAYS ACTUALLY IN THE FILTERED FILE? Job 13 can
#    only show they are TAGGED, from which it follows that `w/` *would* have
#    selected them. That is reasoning. Whether they are IN water-source.osm.pbf
#    is the finding, and this campaign has been repeatedly wrong in exactly
#    that gap. So look them up in the filtered file by id.
#
# ⚠️ Note for whoever reads this next: adding `r/natural=coastline` to Step 0c
# does NOT fix a hole if one exists. It would pull the member ways in as
# referenced objects, but osmcoastline keys on natural=coastline being on the
# WAY, so it would still ignore them. The re-filter changes what is in the
# file without changing what osmcoastline reads. (Caught by indexing-5e after
# I proposed exactly that remedy.)

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
REL="${RUN_ROOT}/data/coastline-relations.osm.pbf"
FILTERED="${RUN_ROOT}/data/water-source.osm.pbf"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
[ -s "${REL}" ] || { echo "FATAL: ${REL} missing (job 13 did not produce it)" >&2; exit 1; }

python - "${REL}" "${FILTERED}" <<'PY'
import collections, sys
import osmium

rel_path, filtered_path = sys.argv[1], sys.argv[2]

# --- what are they, and how big is the hole? -------------------------
way_tagged = {}
for w in osmium.FileProcessor(rel_path, osmium.osm.WAY):
    way_tagged[w.id] = (w.tags.get("natural") == "coastline")

types = collections.Counter()
tagged_ids, untagged_ids = set(), set()
rels = 0
for r in osmium.FileProcessor(rel_path, osmium.osm.RELATION):
    if r.tags.get("natural") != "coastline":
        continue
    rels += 1
    types[r.tags.get("type") or "(no type tag)"] += 1
    for m in r.members:
        if m.type != "w":
            continue
        if way_tagged.get(m.ref):
            tagged_ids.add(m.ref)
        elif m.ref in way_tagged:
            untagged_ids.add(m.ref)

print(f"  natural=coastline relations        : {rels:,}")
print(f"  their type=* values:")
for t, n in types.most_common():
    print(f"      {t:28s} {n:6,}")
print()
print(f"  distinct member ways TAGGED coastline   : {len(tagged_ids):,}")
print(f"  distinct member ways NOT tagged (HOLE)  : {len(untagged_ids):,}")
print()

# --- are the tagged ones actually in the filtered file? --------------
# The finding, not the inference.
print("  scanning the filtered file for those way ids ...", flush=True)
present = set()
want = tagged_ids | untagged_ids
for w in osmium.FileProcessor(filtered_path, osmium.osm.WAY):
    if w.id in want:
        present.add(w.id)

t_present = len(tagged_ids & present)
u_present = len(untagged_ids & present)
print(f"  tagged member ways present in filtered file   : {t_present:,} of {len(tagged_ids):,}")
print(f"  untagged member ways present in filtered file : {u_present:,} of {len(untagged_ids):,}")
print()

ok = True
if tagged_ids and t_present != len(tagged_ids):
    print(f"  *** {len(tagged_ids) - t_present:,} TAGGED coastline ways are MISSING from")
    print(f"  *** the filtered file. That is a real hole in the ocean input and")
    print(f"  *** contradicts the natural=coastline way row reconciling at 1.0000.")
    ok = False
else:
    print("  All tagged member ways are present in the filtered file (verified by")
    print("  lookup, not inferred from the planet).")

hole = len(untagged_ids) - u_present
if hole > 0:
    print()
    print(f"  {hole:,} member ways are neither tagged nor in the filtered file.")
    print(f"  osmcoastline would not have used them even if present, because it")
    print(f"  keys on the tag being on the WAY. So this is a gap in OSM's own")
    print(f"  tagging, not one the filter created, and re-filtering cannot close it.")
    print(f"  Decision required: accept and document a {hole:,}-way gap, or build")
    print(f"  machinery to synthesise way tags from relation membership.")
print()
print("  VERDICT INPUTS ABOVE. No action taken by this job.")
sys.exit(0 if ok else 3)
PY
echo "=== DONE $(date -Is) ==="
