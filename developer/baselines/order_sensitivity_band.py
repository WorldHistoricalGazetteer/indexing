"""
Does the deployed Symphonym v7 encoder distinguish ORDER?

0.8895 for London/Nodlon is uninterpretable alone. This measures the band:
  V  true variant   - another name attested on the SAME place (corpus-derived,
                      never typed by hand)
  P  permutation    - a character permutation of the query: identical length,
                      identical character multiset, ONLY order differs
  R  reversal       - the maximal order perturbation
  U  unrelated      - a name from a different place, length-matched (the model
                      has an explicit length-bucket embedding, so length must
                      be controlled or it becomes the discriminator)

Controls that let this fail:
  cos(Q,Q) must be 1.0        - if not, the harness is wrong, not the model
  embed(Q) != embed(P)        - if identical, order is discarded outright
"""
import json, random, subprocess, sys, itertools, os
import urllib.request

random.seed(20260911)
ES = "http://localhost:9201"
GW = "http://localhost:9200"
PW = open("/ix1/ishi/es/config/elastic.password").read().strip()
import base64
AUTH = "Basic " + base64.b64encode(f"elastic:{PW}".encode()).decode()

def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())

def latin(s):
    return s and all(ord(c) < 0x250 for c in s) and any(c.isalpha() for c in s)

# ---- 1. corpus-derived positives: names attested on the same place ----------
# random_score over places with >=3 nested toponyms
q = {
  "size": 6000,
  "query": {"function_score": {
      "query": {"bool": {"filter": [
          {"nested": {"path": "toponyms", "query": {"exists": {"field": "toponyms.label"}}}}
      ]}},
      "random_score": {"seed": 20260911, "field": "_seq_no"}}},
  "_source": ["place_id", "toponyms.label"]
}
hits = post(f"{ES}/places/_search", q)["hits"]["hits"]

places = []
for h in hits:
    labs = [t.get("label") for t in (h["_source"].get("toponyms") or []) if t.get("label")]
    names = sorted({l for l in labs if latin(l) and 4 <= len(l) <= 18})
    if len(names) >= 2:
        places.append((h["_source"]["place_id"], names))

print(f"places sampled: {len(hits)}   with >=2 distinct Latin names: {len(places)}")

# ---- 2. build the quadruples ------------------------------------------------
def permute(s):
    """A character permutation != s. Holds length and multiset exactly."""
    chars = list(s)
    for _ in range(40):
        p = chars[:]
        random.shuffle(p)
        cand = "".join(p)
        if cand != s:
            return cand
    return None

pool = [n for _, ns in places for n in ns]
items, meta = [], []

def add(name, lang="und"):
    items.append([name, lang])
    return len(items) - 1

rows = []
for pid, names in places:
    Q = max(names, key=len)
    V = next((n for n in names if n != Q), None)
    if not V:
        continue
    P = permute(Q)
    if not P:
        continue
    R = Q[::-1]
    if R == Q:
        continue
    cands = [n for n in pool if abs(len(n) - len(Q)) <= 1 and n not in names]
    if not cands:
        continue
    U = random.choice(cands)
    rows.append({"pid": pid, "Q": Q, "V": V, "P": P, "R": R, "U": U,
                 "iQ": add(Q), "iQ2": add(Q), "iV": add(V),
                 "iP": add(P), "iR": add(R), "iU": add(U)})

print(f"quadruples built: {len(rows)}   embed calls: {len(items)}")

# ---- 3. embed through the DEPLOYED model (int8, as served) ------------------
vecs = []
B = 500
for i in range(0, len(items), B):
    r = post(f"{GW}/api/embed", {"items": items[i:i+B]})
    vecs.extend(r["embeddings"])
print(f"embeddings returned: {len(vecs)}  dim: {len(vecs[0])}")

def cos(a, b):
    da = sum(x*x for x in a) ** 0.5
    db = sum(x*x for x in b) ** 0.5
    if da == 0 or db == 0:
        return None
    return sum(x*y for x, y in zip(a, b)) / (da*db)

# ---- 4. controls ------------------------------------------------------------
ident = [cos(vecs[r["iQ"]], vecs[r["iQ2"]]) for r in rows]
bad = [c for c in ident if c is None or abs(c - 1.0) > 1e-9]
print(f"\nCONTROL identity cos(Q,Q): {len(ident)-len(bad)}/{len(ident)} exactly 1.0")
if bad:
    print(f"  !! harness broken: {len(bad)} identity pairs not 1.0, e.g. {bad[:3]}")
    sys.exit(1)

same_vec = sum(1 for r in rows if vecs[r["iQ"]] == vecs[r["iP"]])
print(f"CONTROL embed(Q) == embed(P) (order fully discarded): {same_vec}/{len(rows)}")

# ---- 5. the bands -----------------------------------------------------------
import statistics as st
def band(key):
    xs = [c for r in rows if (c := cos(vecs[r["iQ"]], vecs[r["i"+key]])) is not None]
    xs.sort()
    n = len(xs)
    return (n, st.mean(xs), st.median(xs), xs[n//20], xs[-max(1,n//20)])

print(f"\n{'band':22} {'n':>5} {'mean':>7} {'median':>7} {'p5':>7} {'p95':>7}")
for label, key in (("V true variant", "V"), ("P permutation", "P"),
                   ("R reversal", "R"), ("U unrelated", "U")):
    n, m, med, p5, p95 = band(key)
    print(f"{label:22} {n:5d} {m:7.4f} {med:7.4f} {p5:7.4f} {p95:7.4f}")

# ---- 5b. the production gate: KNN retrieves at similarity >= 0.7 -----------
GATE = 0.7
print(f"\nfraction clearing the production KNN gate (cos >= {GATE}):")
for label, key in (("V true variant", "V"), ("P permutation", "P"),
                   ("R reversal", "R"), ("U unrelated", "U")):
    xs = [c for r in rows if (c := cos(vecs[r["iQ"]], vecs[r["i"+key]])) is not None]
    k = sum(1 for c in xs if c >= GATE)
    print(f"  {label:22} {k:4d}/{len(xs):4d}  ({100*k/len(xs):5.1f}%)")

# ---- 6. the paired question that actually decides it -----------------------
wins = sum(1 for r in rows
           if cos(vecs[r["iQ"]], vecs[r["iV"]]) > cos(vecs[r["iQ"]], vecs[r["iP"]]))
print(f"\ntrue variant scores ABOVE its permutation: {wins}/{len(rows)} "
      f"({100*wins/len(rows):.1f}%)")

# a same-place name can be a TRANSLATION, not a spelling variant, which would
# depress the positive band for reasons that are not the model's fault.
def overlap(a, b):
    sa, sb = set(a.lower()), set(b.lower())
    return len(sa & sb) / max(1, len(sa | sb))
sub = [r for r in rows if overlap(r["Q"], r["V"]) >= 0.6]
if sub:
    w = sum(1 for r in sub
            if cos(vecs[r["iQ"]], vecs[r["iV"]]) > cos(vecs[r["iQ"]], vecs[r["iP"]]))
    print(f"  restricted to plausible spelling variants (>=0.6 char overlap): "
          f"{w}/{len(sub)} ({100*w/len(sub):.1f}%)")

perm_above_var = [(r["Q"], r["P"], r["V"],
                   round(cos(vecs[r["iQ"]], vecs[r["iP"]]), 4),
                   round(cos(vecs[r["iQ"]], vecs[r["iV"]]), 4))
                  for r in rows
                  if cos(vecs[r["iQ"]], vecs[r["iP"]]) > cos(vecs[r["iQ"]], vecs[r["iV"]])]
print(f"\nexamples where a PERMUTATION beat the true variant ({len(perm_above_var)}):")
for e in perm_above_var[:8]:
    print(f"  {e[0]:18} perm {e[1]:18} {e[3]:.4f}  >  variant {e[2]:18} {e[4]:.4f}")

json.dump({"rows": len(rows),
           "bands": {k: band(k) for k in ("V","P","R","U")},
           "variant_beats_perm": [wins, len(rows)]},
          open("/tmp/order_band_result.json","w"), indent=1)
print("\nwrote /tmp/order_band_result.json")
