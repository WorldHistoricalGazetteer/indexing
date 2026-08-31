"""Stage B — match each read big-cap label against the administrative units that CONTAIN it.

The design point is that administrative areas NEST. A label sits inside an ancient parish, inside a
registration district, inside a county, and any one area carries several big-cap labels at different levels —
so clipping a detector to one polygon and expecting one label is wrong. Inverted, the nesting becomes the
mechanism rather than the obstacle: for a given detection the containing units supply a handful of candidate
names, one per level, and whichever name the label actually reads identifies WHICH LEVEL it belongs to. The
level then implies the face, via the Characteristic Sheet.

That is why this needs no gazetteer-wide search and no strong recogniser. Against five candidates, a noisy
read of ten characters is plenty.

Containment is a BOUNDING-BOX test against units exported once by `export_units.py`, not an ES geo query. The
live index has no searchable polygon for any of these namespaces — `geometries.hull` is empty for all of them,
and `h3_cover` is incomplete (one res-7 cell for a parish spanning several km), so both ES routes return
nothing or too little while looking like they worked. A bbox over-selects, which is the right error direction:
it proposes candidate names and the string match discriminates, so a candidate too many costs nothing while a
candidate missed loses the label. Where a unit has several snapshots (the VoB layers carry one geometry per
census year) the candidates collapse by name, so the extra snapshots cost nothing but do not resolve which
year's boundary applies either.

Two honest limits, both recorded per row rather than resolved by rule:

* NO LAYER IS CONTEMPORANEOUS with the 2nd edition (~1897-1900). SN 9179/9321 begin in 1911, the Kain
  parishes are pre-1834; only the registration layers straddle the map date. Boundaries moved. It is tolerable
  only because these labels sit well within their areas, so a modest shift rarely changes which unit contains
  one — but a label near a boundary can be attributed to the wrong unit, and the containment is approximate
  in ES anyway (non-point geometries are stored as reduced-precision hulls).
* SHARED NAMES. A parish, its township and its local-government district are frequently all "Headingley". Two
  levels offering the same string means the face is undetermined by name, and the row is flagged AMBIGUOUS
  for a human rather than silently resolved to whichever level was tried first.

Face assignment here is a SAMPLING PRIOR, never a label. The thesis under test is that typography carries
feature type; a face assigned from a gazetteer's category cannot then be evidence for that claim. This decides
what to put in front of a reviewer and pre-fills their answer. Only verified labels produce reported numbers.

The QC page is stage C (`nest_qc.py`) and runs back on CRC: rendering the crops needs the tile cache, which
this host does not have. Stage B emits the matches only.

    python nest_match.py --reads bigcaps_read.jsonl --units admin_units.json --out nest_matches.json
"""
import argparse, difflib, json, os, re, sys
from collections import Counter, defaultdict

# Namespace -> (OS designation, face implied, how much that implication can be trusted).
# `weak` entries are the ones where the layer does not resolve the OS category on its own: vob_lgd carries a
# flat `local-government-district` type with no way to separate municipal boroughs from urban/rural districts,
# and two different county concepts (historic vs administrative) both plausibly print as a county name.
LEVELS = {
    "kain_par": ("Parishes (Mother or Ancient)", "Upright-Outline-Serif", "strong"),
    "vob_rd":   ("Poor Law Unions (via registration districts)", "Upright-Diagonal-Plain", "proxy"),
    "vob_lgd":  ("Boroughs (Municipal) / districts", "Upright-Horizontal-Serif", "weak"),
    "ukhc":     ("County Names (historic)", "Upright-Outline-Ornate", "strong"),
    "vob_cty":  ("County Names (administrative)", "Upright-Outline-Ornate", "weak"),
    "vob_rc":   ("Registration counties", None, "no face"),
}
# Words the OS sets in ordinary lettering, not in an admin face. A detection reading STREET or ROAD is a
# street name that happens to be large; matching it to a parish of the same name would be a false positive.
STREETY = re.compile(r"\b(STREET|ST|ROAD|RD|LANE|AVENUE|AV|CRESCENT|CRES|TERRACE|PLACE|SQUARE|"
                     r"WORKS|MILL|FARM|HOUSE|SCHOOL|CHURCH|CH|INN|P\.?H)\b", re.I)


def norm(s):
    """Uppercase, letters only. The recogniser emits '#' for characters it cannot resolve and the gazetteer
    punctuates differently ('St. Mary' vs 'ST MARY'), so both sides collapse to the same alphabet."""
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def sim(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def load_units(path):
    u = json.load(open(path))
    import numpy as np
    B = np.array([x["bounds"] for x in u], np.float64)
    return u, B


def containing(units, B, lon, lat):
    """Units whose bounding box contains the point, deduplicated by (namespace, name)."""
    import numpy as np
    m = (B[:, 0] <= lon) & (lon <= B[:, 2]) & (B[:, 1] <= lat) & (lat <= B[:, 3])
    seen, out = set(), []
    for i in np.flatnonzero(m):
        u = units[i]
        k = (u["namespace"], (u.get("title") or "").upper())
        if k in seen:
            continue
        seen.add(k)
        out.append(u)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reads", default="bigcaps_read.jsonl")
    ap.add_argument("--units", default="admin_units.json",
                    help="from export_units.py, run where prod ES is reachable")
    ap.add_argument("--min-sim", type=float, default=0.72,
                    help="string similarity to accept a match. Deliberately not 1.0: the recogniser emits "
                         "'#' for unresolved characters and gazetteer spelling differs from the engraver's")
    ap.add_argument("--min-len", type=int, default=5,
                    help="letters a candidate name needs. Short names match by chance — 'LEE' against 'LEA' "
                         "is 0.67 similarity and means nothing")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="labels/nest_matches.json")
    a = ap.parse_args()

    reads = [json.loads(l) for l in open(a.reads)]
    if a.limit:
        reads = reads[: a.limit]
    print(f"{len(reads)} read big-cap detections", flush=True)

    units, B = load_units(a.units)
    print(f"{len(units)} bounded admin geometries", flush=True)

    out, stats = [], Counter()
    for i, r in enumerate(reads):
        if i and i % 250 == 0:
            print(f"  {i}/{len(reads)} ({stats['matched']} matched)", flush=True)
        if r.get("lon") is None:
            stats["no coordinates"] += 1
            continue
        cand_units = containing(units, B, r["lon"], r["lat"])
        if not cand_units:
            stats["no containing unit"] += 1
            continue
        # A read can normalise to nothing at all — the recogniser returns '#' runs for lettering it cannot
        # resolve, and those collapse to the empty string once punctuation is stripped.
        reads_try = [x for x in ([norm(r.get("text"))] +
                                 [norm(t["text"]) for t in r.get("tokens", [])]) if len(x) >= 3]
        if not reads_try:
            stats["read is unusable after normalising"] += 1
            continue
        best = []
        for u in cand_units:
            cand = norm(u.get("title"))
            if len(cand) < a.min_len:
                continue
            s = max(sim(x, cand) for x in reads_try)
            if s >= a.min_sim:
                best.append(dict(namespace=u["namespace"], title=u.get("title"),
                                 place_id=u.get("place_id"), sim=round(s, 3)))
        if not best:
            stats["no name match"] += 1
            continue
        best.sort(key=lambda x: -x["sim"])
        top = best[0]
        # Shared names across levels: the same string at two levels leaves the face undetermined.
        tied = [b for b in best if norm(b["title"]) == norm(top["title"])
                and b["namespace"] != top["namespace"]]
        des, face, trust = LEVELS.get(top["namespace"], (None, None, "unknown"))
        if STREETY.search(r.get("text", "")):
            stats["rejected: reads as a street name"] += 1
            continue
        if face is None:
            stats[f"matched {top['namespace']} — no face for this level"] += 1
            continue
        stats["matched"] += 1
        stats[f"  via {top['namespace']}"] += 1
        if tied:
            stats["  AMBIGUOUS (same name at 2+ levels)"] += 1
        out.append(dict(sheet=r.get("sheet"), gcx=r.get("gcx"), gcy=r.get("gcy"),
                        lon=r.get("lon"), lat=r.get("lat"), cap=r.get("cap"), gpoly=r.get("gpoly"),
                        text=r.get("text"), match=top, alts=best[1:4],
                        designation=des, face=face, trust=trust,
                        ambiguous=bool(tied),
                        ambiguous_with=[t["namespace"] for t in tied]))

    for k, v in stats.most_common():
        print(f"  {v:>6d}  {k}")
    json.dump(dict(matches=out), open(a.out, "w"), ensure_ascii=False, indent=1)
    print(f"\n{len(out)} matches -> {a.out}")
    byface = Counter(x["face"] for x in out)
    print("\nfaces reached:")
    for f, n in byface.most_common():
        amb = sum(1 for x in out if x["face"] == f and x["ambiguous"])
        tr = Counter(x["trust"] for x in out if x["face"] == f)
        print(f"  {f:26s} {n:>5d}  ({amb} ambiguous; {dict(tr)})")
    print("NESTDONE", flush=True)


if __name__ == "__main__":
    main()
