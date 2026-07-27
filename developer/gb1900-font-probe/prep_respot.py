"""List the starved regions and move their stale output aside so the resume rule will run them again.

A region whose boxes file is non-empty is skipped forever by spot_full.sbatch. That is right for a region
that genuinely completed and right for one that found little, but wrong for one that completed while its
tiles were failing to arrive: re-spotting gb_4318_2824 produced 576 detections in two of nine mosaics
against 88 for the whole of the original run, with no tile misses at all. The stale files are moved rather
than deleted, so the claim stays checkable.
"""
import json, os, shutil

rows = json.load(open("/vast/ishi/gb1900/probe/font/imagery_check.json"))
centres = {}
for l in open("/vast/ishi/gb1900/probe/font/centres_all.txt"):
    p = l.split()
    if len(p) >= 3:
        centres[p[2]] = (p[0], p[1])

# The signature: plenty of text on the ground (GB1900 pinned it) and almost none reported.
starved = [r for r in rows if r["pins"] >= 50 and r["dets"] / max(1, r["pins"]) < 0.25]
starved.sort(key=lambda r: -r["pins"])
os.makedirs("/vast/ishi/gb1900/edition/spot/starved", exist_ok=True)
with open("/vast/ishi/gb1900/probe/font/centres_starved.txt", "w") as fh:
    n = 0
    for r in starved:
        t = r["tag"]
        if t not in centres:
            continue
        src = f"/vast/ishi/gb1900/edition/spot/boxes_{t}.jsonl"
        if os.path.exists(src):
            shutil.move(src, f"/vast/ishi/gb1900/edition/spot/starved/boxes_{t}.jsonl")
        lon, lat = centres[t]
        fh.write(f"{lon} {lat} {t} {r['pins']}\n")
        n += 1
print(f"{len(starved)} starved regions, {n} written to centres_starved.txt "
      f"(stale output moved to spot/starved/)")
print(f"  they hold {sum(r['pins'] for r in starved)} GB1900 pins but reported "
      f"{sum(r['dets'] for r in starved)} detections between them")
