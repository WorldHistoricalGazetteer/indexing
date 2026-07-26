"""Migrate the anchor set from legacy signatures to inventory faces, using the reviewed corrections.

The anchors were labelled under the old base_style-fill-decor scheme, which is why the primary pass could
only ever propose five classes for a fifteen-face inventory, and why it could not distinguish anything that
scheme had no vocabulary for. Reviewing them in the crop QC produces an accept / reject / correct decision per
anchor; this applies those to `pool_labels.json` and writes a `face` alongside the legacy `sig`.

The legacy `sig` is KEPT, not overwritten. It records what the anchor was originally called, which is the only
way to tell later whether a face's samples arrived by direct judgement or by mapping — and the two are not
equally trustworthy.

Rejected anchors are dropped: a crop the reviewer judged unusable should not train anything.

    python apply_corrections.py --corrections labels/anchor_corrections.json
"""
import argparse, json, os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from propose_faces import SIG_FACE
from weak_sig import is_numeral


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="labels/pool_labels.json")
    ap.add_argument("--corrections", default="labels/anchor_corrections.json")
    ap.add_argument("--inventory", default="labels/face_inventory.json")
    ap.add_argument("--decisions", default="labels/face_decisions.json",
                    help="confirmed proposals — spots a human accepted, which become anchors in their own right")
    ap.add_argument("--out", default="labels/pool_labels_faced.json")
    a = ap.parse_args()

    inv = json.load(open(a.inventory))
    faces = set(inv["faces"])
    alias = inv.get("aliases", {})
    corr = {key(c["gcx"], c["gcy"]): c for c in json.load(open(a.corrections))["corrections"]}
    lab = json.load(open(a.labels))
    print(f"{len(lab)} anchors, {len(corr)} reviewed", flush=True)

    out, stats, unresolved = [], Counter(), Counter()
    for l in lab:
        c = corr.get(key(l["gcx"], l["gcy"]))
        if c is None:
            # Distinguish WHY it was never reviewed: a numeral anchor is out of scope by design, whereas one
            # that simply never reached the page is data we are choosing to lose and should know about.
            stats["numeral — out of scope" if is_numeral(l.get("text", ""))
                  else "never reviewed — dropped"] += 1
            continue
        if c.get("action") == "reject" or c.get("reject"):
            stats["rejected"] += 1
            continue
        # An ACCEPT records no face when the legacy signature was ambiguous at review time. If a later merge
        # has since made it unambiguous — as the blackletter merge did — the accept resolves after all, so
        # fall back to the signature map rather than discarding a judgement that is now usable.
        face = c.get("face") or c.get("sig")
        if not face:
            cand = [alias.get(x, x) for x in SIG_FACE.get(c.get("was", ""), [])]
            if len(set(cand)) == 1:
                face = cand[0]
        face = alias.get(face, face)                 # faces merge; a review made before a merge still counts
        if not face:
            unresolved[l.get("sig", "?")] += 1
            stats["accepted but unresolved"] += 1
            continue
        if face not in faces:
            unresolved[face] += 1
            stats["face not in inventory"] += 1
            continue
        out.append(dict(l, face=face, review=c.get("action", "accept")))
        stats[c.get("action", "accept")] += 1

    for k, v in stats.most_common():
        print(f"  {v:>4d}  {k}")
    if unresolved:
        print("  unresolved:", dict(unresolved))
    print(f"\n{len(out)} anchors carry an inventory face:")
    for f, n in Counter(x["face"] for x in out).most_common():
        via = Counter(x["review"] for x in out if x["face"] == f)
        print(f"  {f:26s} {n:>4d}   ({', '.join(f'{k} {v}' for k, v in via.items())})")
    # Confirmed proposals are anchors too. This is the loop closing: the descriptor proposes, a human
    # confirms, and the confirmation deepens the very anchor set the next pass matches against. It is only
    # legitimate because a human sat in the middle — folding in UNconfirmed proposals would train the
    # classifier on its own output.
    if a.decisions and os.path.exists(a.decisions):
        have = {key(x["gcx"], x["gcy"]) for x in out}
        added = Counter()
        for d in json.load(open(a.decisions))["decisions"]:
            if d.get("reject") or not d.get("face"):
                continue
            face = alias.get(d["face"], d["face"])
            if face not in faces or is_numeral(d.get("text", "")):
                continue
            k = key(d["gcx"], d["gcy"])
            if k in have:
                continue
            have.add(k)
            out.append(dict(gcx=d["gcx"], gcy=d["gcy"], text=d.get("text", ""),
                            face=face, sig=None, review="confirmed-proposal"))
            added[face] += 1
        if added:
            print(f"\n+{sum(added.values())} confirmed proposals folded in as anchors:")
            for f, n in added.most_common():
                print(f"  {f:26s} {n:>4d}")

    print(f"\nfinal anchor set: {len(out)}")
    for f, n in Counter(x["face"] for x in out).most_common():
        via = Counter(x["review"] for x in out if x["face"] == f)
        print(f"  {f:26s} {n:>4d}   ({', '.join(f'{k} {v}' for k, v in via.items())})")
    json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(f"\nwrote {a.out}")
    print("APPLYDONE")


if __name__ == "__main__":
    main()
