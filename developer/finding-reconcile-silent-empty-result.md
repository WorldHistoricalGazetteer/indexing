# `/reconcile` returned 200 with an empty result, and the detection rule changed twice

**Recorded 9 September 2026.** Diagnosed across four sessions — `markets-95`,
`whg3-67`, `cced-39` and this one — **all but one of which have since ended.**
Written down because the corrections below arrived after the sessions holding
them were gone, and because one of them is a live contract change that breaks
callers.

## The fault

`POST https://whgazetteer.org/reconcile` (Django's OpenRefine view, **not** the
gateway's `/api/reconcile`) returned **HTTP 200 with an empty `result`**,
indistinguishable from a genuine no-match. No 429, no 5xx, no `messages`, no
`Retry-After`.

🛑 **A CCEd run cached 1,944 of 2,494 queries (78%) as honest misses**, with no
error at any layer. **That is a silent data-corruption path, not a performance
issue** — and it degrades worst on the largest jobs, which are the least likely
to be checked by hand afterwards.

**Not load-shedding.** Prod logs showed **2,043 `requests.Timeout` and zero
`ConnectionError`**, confined to **17:00–18:17 UTC on 8 Sep**, with successes
interleaving inside the window (3 OK, 8 timeouts, 6 OK at 18:05). The day's
busiest hour — 151 POSTs at 14:00 — was clean against 71 in the failing hour.

⚠ **Nor "upstream unreachable", which is what this session wrongly concluded.**
An unreachable host refuses connections and yields no successes. That reading
came from probing **port 22** and generalising to **port 9200** — different
ports, different ACLs. Interleaved successes and pure timeouts mean a slow
upstream, not an absent one.

## Where it is NOT

The gateway cannot produce either response shape. `ReconcileResponse`
(`gateway/reconcile.py`) declares `hits, namespaces, namespaces_searched, scope,
variants_used, derived_forms, edges, clustering_params, toponym_stoplist` —
**no `result`, no `geojson`.** Both observed responses carry `result`; the
degraded one carries `geojson`, which the gateway emits on no path. **So both
are Django-generated, and the degraded one is a different Django branch rather
than the same path returning empty.**

## Detecting it — three generations of rule, each superseding the last

**1. Key-set heuristic (superseded).** A real answer carries `variants_used` and
`derived_forms`; a degraded one does not. Sound, because both are Pydantic
fields defaulting to `[]` with no `exclude_defaults`/`exclude_unset`/
`exclude_none` anywhere, so the gateway emits them on **every** return path —
including the earliest bail-out at `reconcile.py:517`. Their absence therefore
cannot mean "nothing to report", only that no gateway body was incorporated.

⚠ **Strong in practice, weak as a guarantee: adding `exclude_defaults` to tidy a
response would silently break it with no test failing.** ⚠ And the invariant
comment at `:515` ("echoed on every return path, empty ones included",
place#157) attaches to `namespaces_searched`, **not** to those two fields — a
correct conclusion resting on a wrong citation, which is worse than being wrong
outright because the plausible reference stops the next person checking.

**2. Explicit contract (current).** Prod has emitted a `gateway` object carrying
**`answered: false`** since **8 Sep 18:53 UTC**, keyed only on failure. That is a
contract rather than an inference from which keys happen to serialise. `cced-39`
demoted the key-set test to a labelled fallback, and made its exception name the
`exclude_defaults` failure mode so a future build that breaks the fallback raises
loudly after bounded retries instead of retrying into a corner.

🛑 **3. AND THE BACKPRESSURE PREDICATE — this session relayed the WRONG one.**
"Treat a batch where **no** query is answered as backpressure" is insufficient:
a batch of ten with eight failures and two successes passes it and caches eight
rows indistinguishable from honest misses. **Partial failure is the common case.**

```python
if qs and (failed or not any(answered(q) for q in qs)):
    # backpressure — retry with backoff, do NOT cache
```

## ⚠ A CONTRACT CHANGE THAT BREAKS CALLERS

**On a failed call, Django now OMITS `namespaces_searched` rather than falsely
populating it from the request** (reported by `cced-39`, not measured here).

🛑 **This inverts a lesson recorded and relayed by this session.** I had written
that the degraded path *synthesises* that field — "the guaranteed field is the
field that lies" — and told others so. If it is now omitted, **code assuming it
is always present breaks on exactly the responses that matter.**

⚠ **Both statements can be true of different layers, and confusing them is easy:
the GATEWAY still echoes it unconditionally** (`CLAUDE.md:568` remains correct
about `/api/reconcile`); it is **Django's wrapper** that omits it on gateway
failure. Check which layer you are reading before acting on either.

## ✅ The question a write-path fix does not answer

**A fix on the write path does not evict poison already on disk**, and `post()`
returns a cache hit before testing anything — so pre-8-September degraded batches
would be served silently for ever. `cced-39` scanned read-only: **2,145 batch
files, 21,630 queries, zero refused by today's test, zero carrying the
gateway-failure key** — 20,923 answered, 697 honest misses. That repo is clean.

⚠ **Anything else that consumed `/reconcile` during 17:00–18:17 UTC on 8 Sep and
cached the results is still serving them, and no endpoint fix will touch it.**

**General form worth keeping: when a validation moves to the write path, ask what
is already stored that will never be revalidated.**
