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

**2. Explicit contract — 🛑 ASSERTED, NOT OBSERVED. DO NOT RELAY AS FACT.**
A docstring in the CCEd repo (`reconcile_cced_whg.py:133`, written 8 Sep by a
session that has since ended) states that prod emits a `gateway` object carrying
`answered: false` from 8 Sep 18:53 UTC, keyed only on failure. **Provenance stops
there.** `cced-39` read it, relayed it to me in the confident register of
something checked, and then retracted:

```
scanned 21,630 cached queries for a `gateway` key of ANY kind:  ZERO
```

🛑 **So `gateway_failed()` — which the CCEd repo has promoted to its PRIMARY
check, demoting the key-set test beneath it — has never fired on a single cached
response. Its true branch is unexercised.** By the rule this repo already holds,
an exclusion is evidence only if the same test could have produced an inclusion,
and that cannot be shown here.

⚠ **Neither session has ever seen a degraded response.** Both of us spent several
messages reasoning about the shape of one. **If it is real it is a better
contract than the key-set heuristic — but it needs one grep by whoever owns the
Django view, and that is the right place to settle it.**

`cced-39`'s handling of the fallback does stand and is worth copying: its
exception names the `exclude_defaults` failure mode explicitly, so a future build
that breaks the key-set test raises loudly after bounded retries rather than
retrying into a corner.

🛑 **3. AND THE BACKPRESSURE PREDICATE — this session relayed the WRONG one.**
"Treat a batch where **no** query is answered as backpressure" is insufficient:
a batch of ten with eight failures and two successes passes it and caches eight
rows indistinguishable from honest misses. **Partial failure is the common case.**

```python
if qs and (failed or not any(answered(q) for q in qs)):
    # backpressure — retry with backoff, do NOT cache
```

## ⚠ A CONTRACT CHANGE THAT BREAKS CALLERS

🛑 **RESOLVED: BOTH CLAIMS LIVE IN ONE FILE, TWENTY-TWO LINES APART, AND
NEITHER WAS WITHDRAWN WHEN THE OTHER LANDED.**

```
reconcile_cced_whg.py:133   gateway_failed()  — on failure `namespaces_searched` is OMITTED
                                                (live, from 8 Sep 18:53 UTC)
reconcile_cced_whg.py:155   answered()        — the degraded 200 SYNTHESISES it from the request
                                                (superseded 8 Sep)
```

⚠ **So this was never prose against observation.** It was one half of a
self-contradiction quoted without the other half. Zero observed instances of
either shape in a 21,630-query cache.

✅ **And the layering reading is supported from inside the file.** Line 155
reaches its claim by appealing to *"the gateway's own documented
echo-on-every-path invariant"* — which is exactly what a source read of
`gateway/reconcile.py` finds. It then asserts that Django's degraded 200
synthesises the field regardless. **The gateway, Django's degraded 200, and the
8 September failure contract are three different objects**, and the file reads
as contradictory only because no sentence says which one it is about.
**Plausible, NOT established.**

✅ **Nothing operational turns on it.** One check keys on
`variants_used`/`derived_forms`, the other on the `gateway` object, **neither on
`namespaces_searched`.** Whoever owns the Django view settles it in one grep.

🛑 **THIS IS THE SUPERSEDED-INSTRUCTION SHAPE, and it is a recurrence rather than
a novelty.** A superseded statement keeps executing unless it is explicitly
withdrawn; publishing the new position is not the same as retracting the old
one. That is how one file came to hold two mutually exclusive statements **with
nobody wrong at any step** — each was true when written, and the earlier one was
never marked dead. ⚠ **The tell is that a reader cannot date a sentence from its
text.** Both are now annotated in place with source, date and status.

⚠ **IF TRUE it inverts a lesson this session recorded and relayed** — I had
written that the degraded path *synthesises* that field ("the guaranteed field is
the field that lies") and told others so, and code assuming it is always present
would break on exactly the responses that matter. ⚠ **But my version was
observation-based and this one is not, so the older claim is currently the
better-evidenced of the two.** Settle it by grep, not by recency.

🛑 **The transmission failure is the lesson, and it happened twice in four
messages.** I asserted a cause for being called wedged that I had invented;
`cced-39` corrected me, then relayed a docstring in the same confident register
one message later; I committed it to this repository as fact **ninety seconds**
before the retraction arrived. **A claim gains no evidence by being passed on,
but it gains the authority of each hop it survives** — and a git commit is a hop
that outlives every session in the chain.

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

⚠ **And note what that scan does and does not rest on.** The clean result stands
on the **observed presence** of `variants_used`/`derived_forms` across 20,923
answered queries — a positive control. It does **not** rest on `gateway_failed()`,
which never fired, and a check that has never fired has not been shown to work.
