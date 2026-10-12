#!/usr/bin/env python3
"""Does a positive `namespaces` scope starve a small source in phonetic discovery? (read-only)

Until 2026-10-12 the phonetic KNN passes in POST /api/reconcile were sent to ES with NO namespace
filter: each took the 200 nearest toponyms from every source and only THEN dropped the ones outside
`namespaces`. The lexical passes were already filtered. So a query scoped to a small source could
find nothing phonetically, because 200 bigger-source neighbours filled the window first.

Run it twice against the same gateway, once before and once after the change, with identical
queries, then compare. It only POSTs searches; it writes nothing except its own --out file.

  python3 developer/measure_ns_knn_prefilter.py --gateway http://localhost:9200 --out before.json
  python3 developer/measure_ns_knn_prefilter.py --gateway http://localhost:9200 --out after.json
  python3 developer/measure_ns_knn_prefilter.py --compare before.json after.json

Sample  DEEP (English Place-Name Society): a seeded sample (--seed, default 1012) of --n headwords
        whose label is unique in the file and that have a point. For each, ONE historical attested
        form is drawn (seeded) from the headword's own attestations: a form that differs from the
        label (case-insensitively) and has at most three words, e.g. "Gateherst" for Gayhurst.
        Historical spellings are the case where KNN matters: an exact spelling is found by the
        (already filtered) lexical pass either way.
Arms    The form is queried, mode=phonetic, size=10, scoped to each of --namespaces in turn
        (default kain_par, iv: small English sources; gn as a large-source control).
Hit     A top-3 candidate (all candidates are in the scoped namespace) with a representative
        point within --km (default 10) of the headword's point. Also counted: any candidate at all.
"""
import argparse, gzip, json, math, os, random, sys, urllib.request
from collections import Counter

DEEP = os.path.expanduser("~/PycharmProjects/deep/data/export/deep-plato.jsonl.gz")


def post(gw, key, body):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(gw.rstrip("/") + "/api/reconcile", json.dumps(body).encode(), h)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def km(a, b):  # [lon, lat]
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


def deep_point(d):
    for att in d.get("attestations") or []:
        for g in att.get("geometries") or []:
            p = g.get("reprPoint")
            if p and len(p) >= 2:
                return [p[0], p[1]]
    return None


def sample(n, seed):
    """-> (rows[{id,label,form,point}], examined, skipped_no_point, skipped_no_form)"""
    recs = []
    with gzip.open(DEEP, "rt") as f:
        next(f)
        for line in f:
            d = json.loads(line)
            att = (d.get("attestations") or [{}])[0]
            if "Headword" not in str(att.get("formStatus")):
                continue
            label = d.get("label") or ""
            forms = sorted({nm.get("toponym", "").strip()
                            for a in (d.get("attestations") or [])[1:]
                            for nm in a.get("names") or []})
            forms = [x for x in forms if x and x.casefold() != label.casefold()
                     and len(x.split()) <= 3 and len(x) > 3]
            recs.append((d["@id"], label, forms, deep_point(d)))
    cnt = Counter(r[1] for r in recs)
    rows = [r for r in recs if r[1] and cnt[r[1]] == 1 and len(r[1]) > 3]
    rng = random.Random(seed)
    rng.shuffle(rows)
    out, no_pt, no_form, examined = [], 0, 0, 0
    for i, label, forms, pt in rows:
        examined += 1
        if pt is None:
            no_pt += 1
        elif not forms:
            no_form += 1
        else:
            out.append({"id": i, "label": label, "form": rng.choice(forms), "point": pt})
        if len(out) == n:
            break
    return out, examined, no_pt, no_form


def nearest(hits, point):
    ds = [km(g["repr_point"], point) for h in hits for g in h.get("geometries") or [] if g.get("repr_point")]
    return min(ds) if ds else None


def measure(a):
    rows, examined, no_pt, no_form = sample(a.n, a.seed)
    print(f"sample: {len(rows)} forms from {examined} unique headwords examined "
          f"({no_pt} without a point, {no_form} without a usable historical form)", file=sys.stderr)
    out = {"seed": a.seed, "km": a.km, "namespaces": a.namespaces, "rows": []}
    for k, r in enumerate(rows):
        res = {}
        for ns in a.namespaces:
            hits = post(a.gateway, a.api_key, {"query": r["form"], "mode": "phonetic", "size": 10,
                                               "namespaces": [ns]}).get("hits", [])
            stray = [h["place_id"] for h in hits if not h["place_id"].startswith(ns + ":")]
            d3 = nearest(hits[:3], r["point"])
            res[ns] = {"n": len(hits), "top": [h["place_id"] for h in hits[:3]],
                       "hit": d3 is not None and d3 <= a.km, "km": d3, "stray": stray}
        out["rows"].append(dict(r, res=res))
        if (k + 1) % 50 == 0:
            print(f"  {k + 1}/{len(rows)}", file=sys.stderr)
    with open(a.out, "w") as f:
        json.dump(out, f)
    summarise(out)


def summarise(o):
    n = len(o["rows"])
    for ns in o["namespaces"]:
        h = sum(r["res"][ns]["hit"] for r in o["rows"])
        e = sum(r["res"][ns]["n"] == 0 for r in o["rows"])
        s = sum(bool(r["res"][ns]["stray"]) for r in o["rows"])
        print(f"{ns:10s} top-3 within {o['km']:g} km: {h}/{n}   no candidate at all: {e}/{n}"
              f"   rows with an out-of-scope candidate: {s}/{n}")


def sign_p(g, l):
    n = g + l
    if n == 0:
        return 1.0
    k = min(g, l)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def compare(pa, pb):
    A, B = json.load(open(pa)), json.load(open(pb))
    assert [r["form"] for r in A["rows"]] == [r["form"] for r in B["rows"]], "different samples"
    n = len(A["rows"])
    print(f"{n} queries; before = {pa}, after = {pb}")
    for ns in A["namespaces"]:
        ha = sum(r["res"][ns]["hit"] for r in A["rows"])
        hb = sum(r["res"][ns]["hit"] for r in B["rows"])
        ea = sum(r["res"][ns]["n"] == 0 for r in A["rows"])
        eb = sum(r["res"][ns]["n"] == 0 for r in B["rows"])
        gain = [(a, b) for a, b in zip(A["rows"], B["rows"]) if b["res"][ns]["hit"] and not a["res"][ns]["hit"]]
        loss = [(a, b) for a, b in zip(A["rows"], B["rows"]) if a["res"][ns]["hit"] and not b["res"][ns]["hit"]]
        print(f"\n{ns}: hits {ha}/{n} -> {hb}/{n}; empty {ea}/{n} -> {eb}/{n}; "
              f"gained {len(gain)}, lost {len(loss)}, sign test p={sign_p(len(gain), len(loss)):.3g}")
        for tag, lst in (("gained", gain), ("lost", loss)):
            for a, b in lst[:8]:
                print(f"  {tag}: {b['form']!r} ({b['label']}) before {a['res'][ns]['top']} after {b['res'][ns]['top']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway")
    ap.add_argument("--api-key", default=os.environ.get("CRC_GATEWAY_API_KEY", ""))
    ap.add_argument("--out")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1012)
    ap.add_argument("--km", type=float, default=10.0)
    ap.add_argument("--namespaces", nargs="+", default=["kain_par", "iv", "gn"])
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    a = ap.parse_args()
    if a.compare:
        compare(*a.compare)
    elif a.gateway and a.out:
        measure(a)
    else:
        ap.error("--gateway and --out, or --compare BEFORE AFTER")


if __name__ == "__main__":
    main()
