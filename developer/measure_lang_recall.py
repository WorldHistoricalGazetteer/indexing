#!/usr/bin/env python3
"""place#324: top-3 recall of POST /api/reconcile with and without `lang` (read-only).

Run it against a gateway that has the place#324 build (the `lang` field is silently ignored by an
older one, which would make the two arms identical and the result look like "lang does nothing").
It reads two local files and only POSTs searches; it writes nothing anywhere.

  python3 developer/measure_lang_recall.py --gateway http://index.whgazetteer.org:9200 \
      [--api-key TOKEN] [--set a|b|both] [--n-deep 300] [--lang en] [--km 10]

Set A  DEEP (English Place-Name Society, itself in WHG): a seeded sample of settlement headwords from
       deep-plato.jsonl.gz whose label is unique in the file, so the target is unambiguous. Query =
       the label. Hit = one of the top 3 candidates IS that DEEP record (matched on county+serial,
       e.g. 02/000001, inside the candidate place_id). The first sample of candidate ids is printed so
       the id shape can be eyeballed before trusting the number.
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


def deep_sample(n, seed):
    heads = {}
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
    cnt = Counter(heads.values())
    rows = [(i, l) for i, l in heads.items() if l and cnt[l] == 1 and len(l) > 3]
    random.Random(seed).shuffle(rows)
    return rows[:n]


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
    ap.add_argument("--km", type=float, default=10.0)
    a = ap.parse_args()
    arms = [("without", None), ("with " + a.lang, a.lang)]

    if a.set in ("a", "both"):
        rows = deep_sample(a.n_deep, a.seed)
        res = {n: {} for n, _ in arms}
        shown = False
        for did, label in rows:
            for n, lg in arms:
                hits = run(a.gateway, a.api_key, label, lg)
                if not shown:
                    print("sample candidate ids for", repr(label), [h["place_id"] for h in hits[:5]], "target", did)
                    shown = True
                k = key_of(did)
                res[n][did] = any(k in re.sub(r"\D", "", h["place_id"]) and "epns" in h["place_id"].lower()
                                  or h["place_id"].lower().replace("/", "").endswith(k) for h in hits[:3])
        print(f"\nSET A  DEEP, {len(rows)} unique-label headwords, seed {a.seed}, top-3 is the DEEP record itself")
        for n, _ in arms:
            print(f"  {n:>10}: {sum(res[n].values())}/{len(rows)}")
        ch = [(l, res[arms[0][0]][i], res[arms[1][0]][i]) for i, l in rows if res[arms[0][0]][i] != res[arms[1][0]][i]]
        print("  changed rows (label, without, with):", ch[:40])

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
        print("  changed rows:", [(truth[r]["name"], res[arms[0][0]][r], res[arms[1][0]][r])
                                  for r in truth if res[arms[0][0]][r] != res[arms[1][0]][r]])


if __name__ == "__main__":
    main()
