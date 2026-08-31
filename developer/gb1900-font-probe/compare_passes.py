"""Compare the old spot pass against the corpus-fed one — as a MEASUREMENT, not a regression test.

The handoff expected identical weights on identical imagery to reproduce the old boxes and merely add the
model's own baseline (`gline`). That expectation is void: until 29 July 2026 the spotter never read the
tile corpus at all (threaded sqlite fell back to S3, which then throttled), so the old pass ran on
partially-absent imagery. Same region, same weights: 15 boxes became 93.

So the question this answers is not "did we reproduce it" but "how much was the old pass missing, and is
the new pass a superset". Three numbers matter:

  reproduced   old boxes found again in the new pass  -> should be high; low means a real regression
  added        new boxes with no old counterpart      -> the dropout, recovered
  lost         old boxes with no new counterpart      -> should be near zero; investigate if not

Matching uses the pipeline's OWN notion of identity — the dedup key (gcx/8, gcy/8, lower(text)) that
spot_sheet uses to merge detections across overlapping mosaics. A second, looser pass matches by position
alone, which separates "the word was not detected" from "the word was read differently".

    python compare_passes.py --old .../spot --new .../spot2 --out compare_passes.json
"""
import argparse, glob, json, os
from collections import defaultdict


def load(path):
    recs = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    recs.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
    return recs


def key(r):
    return (round(r["gcx"] / 8), round(r["gcy"] / 8), str(r.get("text", "")).lower())


def near(r, pool, tol=24.0):
    """Any detection within `tol` global px of r's centroid, regardless of what it was read as."""
    for o in pool:
        if abs(o["gcx"] - r["gcx"]) <= tol and abs(o["gcy"] - r["gcy"]) <= tol:
            return o
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="/vast/ishi/gb1900/edition/spot")
    ap.add_argument("--new", default="/vast/ishi/gb1900/edition/spot2")
    ap.add_argument("--tol", type=float, default=24.0, help="px for the position-only match")
    ap.add_argument("--out", default="compare_passes.json")
    a = ap.parse_args()

    old_tags = {os.path.basename(p)[6:-6] for p in glob.glob(f"{a.old}/boxes_*.jsonl")}
    new_tags = {os.path.basename(p)[6:-6] for p in glob.glob(f"{a.new}/boxes_*.jsonl")}
    both = sorted(old_tags & new_tags)
    print(f"old {len(old_tags)} regions, new {len(new_tags)}, comparable {len(both)}")
    if not both:
        print("nothing to compare yet — the new pass has not reached any region the old one covered")
        return

    tot = defaultdict(int)
    per_region = []
    gline_new = gline_old = 0
    for tag in both:
        o = load(f"{a.old}/boxes_{tag}.jsonl")
        n = load(f"{a.new}/boxes_{tag}.jsonl")
        ok, nk = {key(r) for r in o}, {key(r) for r in n}
        exact = len(ok & nk)
        # Position-only, for the old boxes the exact key missed: same word, different reading?
        n_by = list(n)
        reread = sum(1 for r in o if key(r) not in nk and near(r, n_by, a.tol))
        lost = len(ok) - exact - reread
        added = len(nk) - exact
        gline_new += sum(1 for r in n if r.get("gline"))
        gline_old += sum(1 for r in o if r.get("gline"))
        tot["old"] += len(o); tot["new"] += len(n)
        tot["exact"] += exact; tot["reread"] += reread; tot["lost"] += max(0, lost); tot["added"] += added

        cov = f"{a.old}/cover_{tag}.json"
        mf = json.load(open(cov))["miss_frac"] if os.path.exists(cov) else None
        per_region.append(dict(tag=tag, old=len(o), new=len(n), exact=exact, reread=reread,
                               lost=max(0, lost), added=added, old_miss_frac=mf))

    o_, n_ = tot["old"], tot["new"]
    print(f"\nboxes: old {o_:,}  new {n_:,}   ({n_/max(1,o_):.2f}x)")
    print(f"  reproduced exactly     {tot['exact']:,}  ({tot['exact']/max(1,o_):.1%} of old)")
    print(f"  matched by position    {tot['reread']:,}  (same place, different reading)")
    print(f"  LOST (no new nearby)   {tot['lost']:,}  ({tot['lost']/max(1,o_):.1%} of old)")
    print(f"  ADDED by the new pass  {tot['added']:,}  ({tot['added']/max(1,n_):.1%} of new)")
    print(f"\ngline present: old {gline_old:,}/{o_:,}   new {gline_new:,}/{n_:,}")

    # The dropout hypothesis is testable: regions the old pass recorded as tile-starved should be exactly
    # the ones gaining most now. Only 16 of the old regions carry cover files, so treat this as indicative.
    withmf = [r for r in per_region if r["old_miss_frac"] is not None]
    if withmf:
        print(f"\nold miss_frac vs gain, {len(withmf)} regions with cover data:")
        for r in sorted(withmf, key=lambda r: -r["old_miss_frac"])[:12]:
            print(f"  {r['tag']}  miss_frac {r['old_miss_frac']:.3f}  old {r['old']:5d} -> new {r['new']:5d}"
                  f"  ({r['new']/max(1,r['old']):.2f}x)")

    worst = sorted(per_region, key=lambda r: -r["lost"])[:10]
    if worst and worst[0]["lost"]:
        print("\nregions losing the most old boxes (investigate before trusting the new pass):")
        for r in worst:
            print(f"  {r['tag']}  old {r['old']} new {r['new']} lost {r['lost']}")

    json.dump(dict(totals=dict(tot), regions=per_region), open(a.out, "w"), indent=1)
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
