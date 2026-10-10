#!/usr/bin/env python3
"""place#324: top-3 recall of POST /api/reconcile with and without `lang` (read-only).

Run it against a gateway that has the place#324 build (the `lang` field is silently ignored by an
older one, which would make the two arms identical and the result look like "lang does nothing").
It reads two local files and only POSTs searches; it writes nothing anywhere.

  python3 developer/measure_lang_recall.py --gateway http://index.whgazetteer.org:9200 \
      [--api-key TOKEN] [--set a|b|both] [--n-deep 300] [--lang en] [--km 10]

Set A  DEEP (English Place-Name Society): a seeded sample (seed 324) of settlement headwords from
       deep-plato.jsonl.gz whose label is unique in the file (county / administrative-division records
       excluded). Only headwords WITH A POINT are sampled (the first reprPoint found on any of the place's
       attestations); those without are skipped and counted. Query = the label. Hit = a top-3 candidate
       with a representative point within --km (default 10) of the headword's point. (The first version
       compared candidate ids with DEEP w3id URIs, which can never match: 0/300 in both arms.) DEEP is not
       believed to be in WHG, so nothing is dropped as a self-match; the namespaces seen in top-3 are
       printed so a DEEP/EPNS-looking one would show.
Set B  The 33 Index Villaris 1680 rows (truth.json): query = row name. Candidates in the `iv:`
       namespace are dropped before scoring (IV is in WHG; matching itself is not a result). Hit =
       a top-3 non-iv candidate with a representative point within --km (default 10) of the truth point.
Both arms use identical queries, mode=phonetic, size=10; the ONLY difference is `lang`.
Prints numerator/denominator per arm per set, and the rows that changed.
"""
import argparse, gzip, json, math, os, random, re, sys, urllib.request
from collections import Counter

DEEP = os.path.expanduser("~/PycharmProjects/deep/data/export/deep-plato.jsonl.gz")
TRUTH = ("/tmp/claude-1000/-home-stephen-PycharmProjects-plato-tools/01ff986e-dd3b-4b2e-988e-cc6f7c734293/"
         "scratchpad/realwhg/data/truth.json")


def post(gw, key, body):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(gw.rstrip("/") + "/api/reconcile", json.dumps(body).encode(), h)
    with urllib.request.urlopen(req, timeout=60) as r:
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


def deep_sample(n, seed):
    """-> (rows[(id,label,point)] of n headwords WITH a point, skipped_no_point, examined)"""
    heads, pts = {}, {}
    with gzip.open(DEEP, "rt") as f:
        next(f)
        for line in f:
            d = json.loads(line)
            att = (d.get("attestations") or [{}])[0]
            if "Headword" not in str(att.get("formStatus")):
                continue
            tys = " ".join(t.get("label", "") for t in att.get("types") or [])
            if any(w in tys for w in ("county", "administrative division")):
                continue
            heads[d["@id"]] = d.get("label")
            pts[d["@id"]] = deep_point(d)
    cnt = Counter(heads.values())
    rows = [(i, l) for i, l in heads.items() if l and cnt[l] == 1 and len(l) > 3]
    random.Random(seed).shuffle(rows)
    out, skipped = [], 0
    for i, l in rows:
        if pts[i] is None:
            skipped += 1
        else:
            out.append((i, l, pts[i]))
        if len(out) == n:
            break
    return out, skipped, len(out) + skipped


def near(hits, point):
    """nearest distance (km) from point to any hit's representative point, or None"""
    ds = [km(g["repr_point"], point) for h in hits for g in h.get("geometries") or [] if g.get("repr_point")]
    return min(ds) if ds else None


def sign_p(g, l):
    n = g + l
    if n == 0:
        return 1.0
    k = min(g, l)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def key_of(deep_id):  # https://w3id.org/whg-epns/02/000001 -> 02000001
    return "".join(deep_id.rstrip("/").split("/")[-2:])


def run(gw, key, q, lang, size=10):
    body = {"query": q, "mode": "phonetic", "size": size}
    if lang:
        body["lang"] = lang
    return post(gw, key, body).get("hits", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--api-key", default=os.environ.get("CRC_GATEWAY_API_KEY", ""))
    ap.add_argument("--set", default="both", choices=["a", "b", "both"])
    ap.add_argument("--n-deep", type=int, default=300)
    ap.add_argument("--seed", type=int, default=324)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--proof", action="store_true", help="print example hit and miss rows for Set A")
    ap.add_argument("--km", type=float, default=10.0)
    a = ap.parse_args()
    arms = [("without", None), ("with " + a.lang, a.lang)]

    if a.set in ("a", "both"):
        rows, skipped, examined = deep_sample(a.n_deep, a.seed)
        res = {n: {} for n, _ in arms}
        ns = Counter()
        log = []
        for did, label, pt in rows:
            for n, lg in arms:
                hits = run(a.gateway, a.api_key, label, lg)[:3]
                ns.update(h["place_id"].split(":")[0].lower() for h in hits)
                d = near(hits, pt)
                res[n][did] = d is not None and d <= a.km
                log.append((label, n, [h["place_id"] for h in hits], None if d is None else round(d, 1), res[n][did]))
        print(f"\nSET A  DEEP, {len(rows)} unique-label headwords with a point, seed {a.seed}; skipped {skipped} "
              f"with no point (of {examined} examined); hit = top-3 within {a.km} km of the headword's point")
        print("  candidate namespaces in top-3:", dict(ns))
        for n, _ in arms:
            print(f"  {n:>10}: {sum(res[n].values())}/{len(rows)}")
        w, x = arms[0][0], arms[1][0]
        gained = [l for i, l, _ in rows if not res[w][i] and res[x][i]]
        lost = [l for i, l, _ in rows if res[w][i] and not res[x][i]]
        print("  gained:", gained)
        print("  lost:", lost)
        print(f"  discordant pairs: {len(gained)} gained vs {len(lost)} lost; two-sided exact sign test p = {sign_p(len(gained), len(lost)):.4f}")
        if a.proof:
            print("  PROOF rows (label, arm, top3 ids, nearest km, hit):")
            for r in [r for r in log if r[4]][:6] + [r for r in log if not r[4] and r[3] is not None][:3]:
                print("   ", r)

    if a.set in ("b", "both"):
        truth = json.load(open(TRUTH))
        res = {n: {} for n, _ in arms}
        for rid, t in truth.items():
            for n, lg in arms:
                hits = [h for h in run(a.gateway, a.api_key, t["name"], lg, size=20)
                        if not h["place_id"].lower().startswith("iv:")][:3]
                res[n][rid] = any(g.get("repr_point") and km(g["repr_point"], t["point"]) <= a.km
                                  for h in hits for g in h.get("geometries") or [])
        print(f"\nSET B  Index Villaris, {len(truth)} rows, iv: candidates dropped, hit = top-3 within {a.km} km of truth")
        for n, _ in arms:
            print(f"  {n:>10}: {sum(res[n].values())}/{len(truth)}")
        g = [truth[r]["name"] for r in truth if not res[arms[0][0]][r] and res[arms[1][0]][r]]
        lo = [truth[r]["name"] for r in truth if res[arms[0][0]][r] and not res[arms[1][0]][r]]
        print(f"  discordant: {len(g)} gained vs {len(lo)} lost; sign-test p = {sign_p(len(g), len(lo)):.4f}")
        print("  changed rows:", [(truth[r]["name"], res[arms[0][0]][r], res[arms[1][0]][r])
                                  for r in truth if res[arms[0][0]][r] != res[arms[1][0]][r]])


if __name__ == "__main__":
    main()
