import json, subprocess, sys, unicodedata as ud
P = open("/ix1/ishi/es/config/elastic.password").read().strip()

def es(path, body):
    r = subprocess.run(["curl", "-s", "-u", "elastic:" + P,
                        "http://localhost:9201/" + path,
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps(body)], capture_output=True, text=True)
    d = json.loads(r.stdout)
    # 🛑 An ES error used to fall through `.get("hits", {}).get("hits", [])` as an
    # empty list, so a failed query was indistinguishable from a query that
    # matched nothing — which is how the first run wrote a file of zeros and
    # exited 0. Raise instead.
    if isinstance(d, dict) and "error" in d:
        raise RuntimeError(f"ES error on {path}: "
                           f"{json.dumps(d['error'])[:300]}")
    return d

def es_get(path):
    r = subprocess.run(["curl", "-s", "-u", "elastic:" + P,
                        "http://localhost:9201/" + path],
                       capture_output=True, text=True)
    d = json.loads(r.stdout)
    if isinstance(d, dict) and "error" in d:
        raise RuntimeError(f"ES error on GET {path}: {json.dumps(d['error'])[:200]}")
    return d


def compat(n):
    return ud.normalize("NFC", n) == n and ud.normalize("NFKC", n) != n

MAX_WINDOW = 10000   # index.max_result_window; size beyond this is an ERROR, not a big page

def sample(script, n, pred, seed):
    """Gather n matching docs, paging over SEEDS rather than one oversized request."""
    out, seen, page = [], set(), min(MAX_WINDOW, 5000)
    for attempt in range(40):
        d = es("toponyms/_search", {
            "size": page, "_source": ["name", "embedding", "attestations"],
            "query": {"function_score": {
                "query": {"bool": {"filter": [{"term": {"script": script}},
                                              {"exists": {"field": "embedding"}}]}},
                "random_score": {"seed": seed + attempt, "field": "_seq_no"}}}})
        hits = d["hits"]["hits"]
        if not hits:
            break
        for h in hits:
            if h["_id"] in seen:
                continue
            seen.add(h["_id"])
            src = h["_source"]; nm = src.get("name") or ""
            if not pred(nm) or not src.get("attestations"):
                continue
            out.append((h["_id"], nm, src["embedding"], src["attestations"]))
            if len(out) >= n:
                return out
    return out

def partner(tid, name, atts):
    d = es("toponyms/_search", {
        "size": 8, "_source": ["name"],
        "query": {"bool": {
            "filter": [{"terms": {"attestations": atts[:3]}},
                       {"exists": {"field": "embedding"}}],
            "must_not": [{"ids": {"values": [tid]}}]}}})
    for h in d["hits"]["hits"]:
        if (h["_source"].get("name") or "") != name:
            return h["_id"], h["_source"].get("name")
    return None, None

def rank_of(vec, target, k=1000, exclude=None):
    """Rank of `target` among KNN neighbours, EXCLUDING the query itself.

    🛑 The query vector IS the query document's stored vector, so the query is
    always its own nearest neighbour and occupies rank 1. Counting it made R@1
    structurally 0.0000 in every group and inflated every rank by one.
    """
    d = es("toponyms/_search", {
        "size": k, "_source": False,
        "knn": {"field": "embedding", "query_vector": vec,
                "k": k, "num_candidates": k * 2}})
    i = 0
    for h in d["hits"]["hits"]:
        if h["_id"] == exclude:
            continue
        i += 1
        if h["_id"] == target:
            return i
    return None

N = int(sys.argv[1]); OUT = sys.argv[2]; SEED = 20260910
groups = {
    "A_thai_nfkc_changed": ("THAI", lambda n: compat(n)),
    "B_thai_nfkc_stable":  ("THAI", lambda n: not compat(n)),
    "C_latin_case_only":   ("LATIN", lambda n: (not compat(n)) and n.casefold() != n),
}
res = {"captured_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
       "concrete_index": list(es_get("_alias/toponyms"))[0],
       "seed": SEED, "k": 1000, "groups": {}}
for g, (sc, pred) in groups.items():
    rows = sample(sc, N, pred, SEED)
    recs = []
    for tid, nm, vec, atts in rows:
        pid, pname = partner(tid, nm, atts)
        if not pid:
            continue
        recs.append({"id": tid, "name": nm, "partner_id": pid,
                     "partner_name": pname, "rank": rank_of(vec, pid, exclude=tid)})
    paired = len(recs)
    hit = [r["rank"] for r in recs if r["rank"]]
    res["groups"][g] = {
        "sampled": len(rows), "paired": paired,
        "R@1":   sum(1 for r in hit if r <= 1) / max(paired, 1),
        "R@10":  sum(1 for r in hit if r <= 10) / max(paired, 1),
        "R@200": sum(1 for r in hit if r <= 200) / max(paired, 1),
        "R@1000": len(hit) / max(paired, 1),
        "median_rank_when_found": (sorted(hit)[len(hit)//2] if hit else None),
        "records": recs}
    m = res["groups"][g]
    print("  %-22s sampled=%-5d paired=%-5d R@1=%.4f R@10=%.4f R@200=%.4f R@1000=%.4f med=%s"
          % (g, m["sampled"], m["paired"], m["R@1"], m["R@10"], m["R@200"],
             m["R@1000"], m["median_rank_when_found"]), flush=True)
empty = [g for g, m in res["groups"].items() if m["paired"] == 0]
if empty:
    raise SystemExit(f"REFUSING TO WRITE: {empty} paired 0 rows. A baseline of "
                     f"zeros is worse than no baseline — it looks like a result.")
json.dump(res, open(OUT, "w"), ensure_ascii=False, indent=1)
print("  wrote", OUT)
