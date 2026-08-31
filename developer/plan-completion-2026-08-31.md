# Plan — completing the July/August re-ingestion

> **Written:** 31 August 2026. The sequenced form of
> [`HANDOVER-2026-08-31-rebuild-audit.md`](HANDOVER-2026-08-31-rebuild-audit.md)
> §3, which is the evidence for every claim here. Read that first; this document
> only orders the work and says how each step is verified.
>
> **Decisions taken by SG, 31 August 2026:**
> 1. **`un` first** — restore its geometries to the store, then retile all 27
>    buckets. No permanent exception.
> 2. **Full scope** — the publication tail, the pipeline debt, *and* the data
>    residuals.
> 3. **The retile is LAST.** *"The tilesets are at present consumed only by the
>    Atlas UI, which is Beta gated and so is not a priority fix: it's more
>    important to get everything updated properly and stable."* So Phase 2 —
>    correctness of the live corpus — runs ahead of Phase 3, even though Phase 3
>    fixes the only visibly-broken thing.
> 4. **Delete the broken geom store now**; keep the rollback index until the
>    retile is verified.
> 5. **The whg id map** (§2.3) — accepted after measuring the alternative: the
>    extract emits `place_key → place_id` and `contributor_replay` joins through
>    it. It also fixes a pre-existing defect, 10,732 dangling overlay edges.
>
> **The standing rule this campaign earned:** verify every step against an
> independent measure, never against the pipeline's own status. Each step below
> names its check for that reason.
>
> **And its sharper form, from S4, 31 Aug: _a verification that has never been
> run against a known-bad input isn't a verification._** Run each check first
> where you know it should fail — against the pre-change state, the stale
> artefact, the empty store — and confirm it says so. Two of this plan's own
> checks were written without that and were wrong in opposite directions: §2.1
> named counters the code tracked but never printed (so the check could not have
> passed or failed, only appeared to), and the audit's first containment test
> could not distinguish a working exact path from one silently degrading to
> fuzzy, because both return a plausible hit for the same query (§2b). Neither
> error survives a run against a known-bad input; neither was caught by
> re-reading the code.

---

## Session map

Run these as **separate Claude Code sessions**, one per row. Not ritual: every
fault in this campaign came from a long session losing track of what it had
verified — the 7 August retile deployed poly-less tilesets because the run
reported success and nothing re-checked the geometry. A fresh session starts cold
from `CLAUDE.md` → the audit → this plan, which is what those documents are for.

| session | steps | shape of the work | status |
|---|---|---|---|
| **S1** | 1.1, 1.2, 2.2, 2.4 | Two deletions plus two small code-and-test fixes. No infrastructure, no long jobs — good use of the wait while something else runs. | ✅ **done 31 Aug** — 78 GB reclaimed; 2.2 deployed and verified on prod (`4286a0f`); 2.4 fixed with a test (`0c2819c`) |
| **S2** | 2.1 | `un` extract → geom-store merge → gateway restart. One Slurm job, three independent verifications. **Gates S5.** | ✅ **done 31 Aug** — job 11074309, `un:` keys **247**, bounds delta 0.0 against the live index; S5's gate is met. Store released to S3. Also found and fixed a live wrong answer: `containment=exact` had been degrading to fuzzy for every country container |
| **S3** | 2.3 (⚠️ its overlay rebuild now waits on S4's 2.5 — see the hazard table) | The big one: id-map code change, re-ingest, geom merge, overlay rebuild, registry push. Deserves a session to itself. | ✅ **done 31 Aug except the overlay publish, which 2.5 blocks** — code `4d763b8`; extract + geom merge (whg 0 → 9,849); prod re-index 0 errors, `whg:1052:8` live and the old id gone; ES restarted (heap 9%); registry pushed (48 datasets, prod + dev); join verified against live PG — **1,935 of 1,935 endpoints resolve, 0 dangling** (was 2,734 of 13,466). Side fixes `adc7345`, `42b6e4a`, `1f5aa50` |
| **S4** | **2.5, then 2.6** (order corrected 31 Aug — 2.5 is 2.6's input) | Restore the `gn`/`wd`/`nl` staged trees, then the ~9 h vocabulary rebuild that reads them. Not an ES job. | 🟡 **2.5 two-thirds done 31 Aug** — `wd` promoted (hard-linked, 0 bytes copied) and `nl` re-extracted; **both verified PASS against the live index, delta 0** (Slurm 11074356). `gn` extract running (11074352, 36 h wall; 500k staged at 26 min, ~12 h + an alt-names pass to go). 2.6 not started and **must not start until `gn` verifies** — the one-command verifier is parked at `/vast/ishi/staged/s4_verify_staged.sbatch` and needs no S4 session |
| **S5** | 3.1, **plus the 47 `whg-*` buckets** (4.6) | The retile. ⚠️ **Blocked on S4's 2.5** — wait for `gn`/`wd`/`nl`, do not start streaming from ES instead. Prove the verifier FAILS on the preserved fixtures before deploying. | ⬜ **deferred by SG, 31 Aug** — not spun up, rather than spun up and idling for `gn`'s several-hour extract |
| **S6** | 3.2 | `whg3` — a different repository, so a separate session by necessity. | ⬜ |
| **S7** | 3.3 | Post-retile cleanup, once the deployed map has been looked at (clio gains 2,986 polygons that have never rendered). | ⬜ |

**Order:** S1, S2, S3 and S4 are mutually independent *in their work*; see the
concurrency rules below before running them at the same time. Only S2 gates S5.
S5 → S6 → S7 is strict.

⚠️ **The condition that makes this work: every session sets its row's status when
it STARTS, and updates it plus the step's own notes before it ends.** Marking
only on completion leaves a started-and-waiting session indistinguishable from an
unstarted one — which is what the ⬜ against S4 meant for its first hour. Use
⬜ not started · ◐ running or holding · ✅ done. A session that finishes work without
recording it leaves the next one inheriting stale state — which is exactly how
the 9 August handover came to say "the retile is deferred" when a partial retile
had already deployed broken tilesets.

### Running sessions concurrently — read before starting more than one

**Safe to start together right now: S1, S2, S3.** Hold **S4** until S3's indexing
has finished, and do not start **S5** until S2 is verified. The dependency graph
above is necessary but not sufficient — these sessions also share three
resources it does not model.

| hazard | who collides | rule |
|---|---|---|
| **Production ES load** | S3 (delete-by-query + re-index of 229 k docs, plus the toponym augment) and S5 (corpus-wide streaming for tiles). ⚠️ **S4's 2.6 is NOT one of them** — corrected 31 Aug: it runs `--skip-es-index` and never consults ES, so it neither contends here nor needs a restart. The hold placed on S4 behind S3 was unnecessary, though harmless and correct on the plan's word at the time | **One heavy ES job at a time.** Heap saturation from heavy indexing has already taken faceted `/api/search` to 500s once, and `dense_vector` merges on the toponyms index are the known OOM driver. Restart ES after any of them. |
| **Gateway restarts** | S1 (2.2 needs one to deploy the scope fix) and S2 (2.1 ends with one) | **One restart owner.** Whichever finishes second performs the restart; the other says in its notes that its change is on disk but not yet loaded. A restart mid-test silently invalidates the other session's verification. |
| **Staged manifest writes** | S2, S3, S4 all write `staged/<ns>` and the run manifest | The `/vast` lock is `O_CREAT|O_EXCL` with **proceed-with-warning on timeout**, because `flock` returns `ENOLCK` there under fan-out. So two sessions *can* both proceed. Keep concurrent sessions on **different namespaces** — which S2/S3/S4 already are — and never let two touch the same one. |
| **Degraded staged trees** ⚠️ NEW 31 Aug | S3's overlay rebuild, S4's 2.6, and any future rebuild | `hard_links_staged.py` and toponyms stage 1 both read `staged/<ns>/final/places.parquet` through the stage chain, and `gn`/`wd` are **one row each** with `nl` absent. **98.9% of the overlay's 7,596,959 rows touch `wd` and 67.0% touch `gn`**, so a harvest today replaces the gateway's live co-reference store with a fraction of one. **Both now depend on 2.5.** The published overlay is intact only because it was built on 6 Aug, the day before the accident |
| **The pitt VM** | any step running inline rather than via Slurm | Heavy work goes to Slurm. Several steps do small inline work on pitt; eight parallel inline resolvers once OOM-thrashed the VM into a ~1 h production outage. Two or three sessions doing "just a little" inline work is that same pattern. |

**Preconditions, as checks rather than prose.** A session must run its own before
starting, and must not trust this document's status boxes — they are only as
fresh as the last session that remembered to tick one.

| session | check before starting | expected |
|---|---|---|
| **S1** | none | — |
| **S2** | none | — |
| **S3** | none | — |
| **S4** | **2.5 complete before 2.6 starts** — `gn`, `wd` and `nl` staged trees restored and counted against the live index. (The former "no bulk indexing in flight" check is withdrawn: 2.6 never touches ES) | `gn` **13,454,817**, `wd` 11,459,393, `nl` 4,363 staged docs — measured, not the stale "~11.6 M" this table used to carry |
| **S5** | `sqlite3 /vast/ishi/geom/index.sqlite "select count(*) from geom where k >= 'un:' and k < 'un;'"` | **247** — S2 is done. Anything less and the retile repeats the §2 failure on the country boundaries |
| **S5** | ⚠️ **2.5 COMPLETE — a hard gate, not an either/or (SG, 31 Aug).** Verify with the pipeline's **own** resolver, not `ls`: a stub is a valid file of the right name. `gn` **13,454,817**, `wd` **11,459,393**, `nl` **4,363** staged docs, each delta 0 against the live index | all three PASS |
| **S5** | *(escape hatch, deliberate override only)* `TILE_ES_DOC_NAMESPACES=gn,wd` tiles those two from the index instead. **Costs a ~24.5 M-document scan of production ES** and leaves the staged trees still wrong for the next consumer. Use only if the Beta genuinely cannot wait for `gn`'s extract | not the default |
| **S6** | 3.1 deployed and verified | polygons present in all 27 deployed tilesets |
| **S7** | S6 done and the map looked at | — |

---

## Phase 1 — reclaim, now (hours, nothing depends on it)  — **S1**

| # | action | verify |
|---|---|---|
| 1.1 | ✅ **done 31 Aug.** `rm -rf /ix1/ishi/DELETABLE-AFTER-2026-08-31--geom-broken` (**57 GB**) — SG-approved. It is unreadable (index truncated to 2 rows, shards keyless), so it is insurance in name only | `crc-quota` group `ishi`: ix1 **3.24 TB → 3.18 TB**. Confirmed unreadable before deleting rather than trusting the README: `index.sqlite` held 2 rows, `index.json` 145 bytes naming only `un:fra_0` / `un:esp_0` |
| 1.2 | ✅ **done 31 Aug.** `rm -rf /vast/ishi/geom_rebuild/staging /vast/ishi/geom_rebuild/staging_pending` (**22 GB**) — redundant since the 9 Aug merge. ⚠️ **Read the path twice.** These are under `geom_rebuild/`. `GEOM_STORE_STAGING_DIR` is `/vast/ishi/geom/staging` — a *different* directory, and it is S2's live input while 2.1 runs. Deleting that one mid-run destroys the extract | vast **759.5 GB → 737.8 GB**; live store still **11,758,768** rows with per-namespace counts unmoved. "Redundant" was **measured, not assumed**: 1,373 keys sampled across all 82 small staging index files resolve in `index.sqlite`, 0 missing, plus the leading keys of the two large `osm`/`ohm` files. `/vast/ishi/geom/staging` untouched (still held S2's `ukhc_counties.*` at the time) |

**Do NOT yet delete** `/vast/ishi/tiles-verify` (17 GB) or
`/ix1/ishi/data/tiles/_step0` (2.7 GB) — the `ohm` band `.geojsonl` files in them
are the cheap fixtures for exercising the tile builder before Phase 3. They go in
step 3.5.

---

## Phase 2 — correctness and stability of the live corpus  ★ the priority

### 2.1 `un` — geometries into the geom store  — **S2**

✅ **Done 31 Aug — Slurm `htc` job 11074309, COMPLETED in 25:03.** Store
released to S3 afterwards; `/vast/ishi/geom/staging` left empty.

```
from_geoboundaries=229, from_bnda=18        the live index's exact split
kept 11,758,768 + wrote 247 = 11,759,015    index.sqlite total
index.sqlite 'un:' keys                247  <- S5's gate
resolved from store                247/247
worst bounds delta            0.000e+00 deg (tolerance 1e-05)
repr_point outside its polygon           0
```

The 11,758,768 baseline was measured independently by S1, S2 and S3 before
the merge, and moved by exactly 247.


**The source question is already settled in code and needs no decision.**
`authorities/un-countries.py` takes identifiers, names and timespans from BNDA
and *overrides the polygon* with geoBoundaries HPSC wherever it has one
(`boundary_source='geoboundaries'`), keeping BNDA only for the territories
geoBoundaries lacks. That is exactly the "much more precise geoBoundaries
dataset" SG meant, and the live index already reflects it: **229 of 247 `un`
docs are `geoboundaries`, 18 are `bnda`**. Only the *store* is empty.

The geoBoundaries checkout is present at the default path
(`/vast/ishi/data/authorities/geoboundaries/repo`, 6.9 GB), so a re-extract will
use it — but `load_geoboundaries_geoms` **silently returns `{}` and falls back to
pure BNDA if that path is missing**, which would quietly downgrade every country
outline. That is the failure mode to guard.

1. Rotate `staged/un.bnda-baseline` and any `staged/un` aside — `write_staged_place_doc` **appends**.
2. Delete any `un.bin` / `un.index.json` from the geom staging dir — `GeomStoreWriter` opens `"ab"` and would double every entry. **And check what else is in there**: `consolidate_geom_store` merges *every* `*.index.json` in `GEOM_STORE_STAGING_DIR`, not only the namespace you are working on. It currently holds `ukhc_counties.bin` + `.index.json` (92 entries, 55 MB) left by the 20 August names refresh; those keys are already in the store, so re-merging them rewrites them into a fresh shard for nothing and muddies "what did this merge add". Move them aside for the run (found by S2, 31 Aug). **They were not put back**, and neither was `un`'s own staging pair afterwards: leaving either in `GEOM_STORE_STAGING_DIR` re-arms the `"ab"` double-append trap this very step exists to avoid. Both now sit in `/vast/ishi/geom/staging-parked/` with a README recording what they are and that they can simply be deleted — their keys are in `index.sqlite`, and the store cannot be rebuilt from staging anyway, so they are not insurance.
3. Re-extract `un`, then `geom_store --merge --keep-staging`.
4. `es gateway-restart` so the gateway re-reads the store index. On the VM this
   is `scripts/gaz_request.sh gateway-restart` (the gazetteer account cannot be
   SSH'd); it git-pulls before restarting, so whatever is on `main` goes live
   with it — check with whoever else has a gateway change in flight first.

**Two things in the run log that will mislead you if you compare against 5 Aug.**

*"Geometries in VAST store: 741"* — that is what `un_rebuild.sbatch` reported on
5 August for the same 247 countries. It was not 741 geometries. `GeomStoreWriter`
opens `{ns}.bin` with `"ab"` **and reloads an existing `{ns}.index.json` on open**,
so that run was silently resuming two earlier attempts and counting three
generations of the same 247 keys. Nothing was ever wrong in the store — the final
index is a dict, so the duplicates collapsed — but the number could not be read.
Clearing the staging pair in preflight, which step 2 asks for and the 5 Aug
sbatch did not do, is what makes this run's **247** mean 247.

*`total vertices: 16,998,208` against 5 August's `16,505,112`* — a 3% difference
in a diagnostic counter, with `geoBoundaries: loaded 231` identical on both runs
and every one of the 247 bounding boxes matching the live index to **0.0**. Most
likely GEOS noding differences in `unary_union` between environment versions,
which change vertex counts without moving extents. Not chased, because the
decisive measures all agree; noted so the next person does not read it as drift
in the source data.

**Verify (all three, not one):**
* the run's own counters print `from_geoboundaries=229, from_bnda=18` — anything else means the checkout wasn't read. ⚠️ **This plan originally asserted those counters were printed; they were only tracked.** `stage_un_countries` accumulated both in `stats` and the COMPLETE block printed everything except them, so the check as first written could not be read off a run. S2 added the print, and a hard guard: staging now exits non-zero when `load_geoboundaries_geoms` returns `{}` unless `WHG_ALLOW_BNDA_ONLY=1` is set by name. The silent fallback is now impossible rather than merely warned about;
* `select count(*) from geom where k >= 'un:' and k < 'un;'` returns **247**;
* a sample of stored polygons matches the live index's `bounds` for the same `geom_ref` — a mis-keyed rebuild still "resolves" every key, which is why the 9 Aug verification counted bounds mismatches rather than lookups. Now a module rather than a sample: **`python -m processing.verify_un_geom_store`** (`dump` on the VM where ES is reachable, `check` on the compute node) compares **every** entry's bounds *and* `repr_point` against the live index and exits non-zero on any disagreement, so it can gate an sbatch step. `repr_point` is the sharper of the two: two neighbours can share a bbox to 1e-5 and cannot both contain each other's representative point.

~~**Note this is not urgent for search.**~~ ⚠️ **Wrong — corrected by S2,
31 Aug, by measurement.** It was urgent for search, and had been for three
weeks. The borrowed-`sameAs`-polygon reasoning holds only for
`containment=fuzzy`, which works off the `h3_cover` in ES and never touches the
store. For `containment=exact` `apply_containment` does something else, and its
own docstring says so: *"if the region geometry could not be loaded, exact
silently degrades to the fuzzy test."* With no `un` geometry in the store, every
exact query scoped to a country silently answered fuzzily.

Measured on prod, the same request before the merge and after the restart:

```
contained_in:["un:fra"], containment=fuzzy -> 15 hits   (both times)
contained_in:["un:fra"], containment=exact -> 15 hits   BEFORE  <- identical to fuzzy
                                           ->  4 hits   AFTER
```

The 11 records that dropped are Swiss-only (`wd:Q71 Geneva ['CH']`,
`gn:2660646`, `tgn:7007279` …); the 4 that survive are exactly the `['CH','FR']`
cross-border features that really do intersect France. Both responses carried
`scope: {applied: true, mode: "polygon"}` throughout, so nothing in the API said
the constraint had been weakened — this is the same shape of fault as the
7 August retile, a component reporting success for work it had not done.

**The general form is worth carrying forward:** a namespace whose geometries are
missing from the store does not fail exact containment, it *quietly downgrades*
it. Any future audit of the store should treat "which namespaces claim
`has_geom` but hold no keys" as a search-correctness question, not only a
tile-generation one.

### 2.2 Gateway — an unresolvable scope must fail closed (audit §2b)  — **S1**

✅ **Done 31 Aug — `4286a0f`, deployed to prod and verified live.**

`contained_in: ["un:not_a_real_place"]` returned Paris in Turkey and Gabon with
`scope: null`. Two fixes in `search.py` / `reconcile.py`: fail closed when
`resolve_region` yields nothing for *every* requested id, and populate the
`scope` object (it was `null` even on the successful path).

**Worse than the audit recorded, measured before the fix.** The bad-id request
did not merely return *some* wrong hits: it returned the **byte-identical
400-hit result set** of a bare unscoped `"Paris"` query, same hits in the same
order. So the scope was not being degraded or widened — it was being discarded
entirely, and the response was an unscoped search wearing a scoped request's
clothes.

`ScopeInfo`, `scope_message` and the builder moved out of `reconcile.py` into
`gateway/spatial.py`, beside the `resolve_region` whose outcome they describe.
The two endpoints answered the same question differently for four months
*because* each owned its own copy; sharing the implementation is what stops that
recurring, and `reconcile.py` keeps `ScopeInfo` as a re-export so its published
response shape is unchanged.

**Verified live on prod (`localhost:9200`), after the relay restart pulled the
commit:**

| request | before | after |
|---|---|---|
| `contained_in: ["un:not_a_real_place"]` | 400 hits — identical to unscoped | **0 hits**, `scope.applied=false`, `containers_unresolved:["un:not_a_real_place"]`, message |
| `contained_in: ["un:fra"]` | 73 FR hits, `scope: null` | **73 FR hits unchanged**, `scope.applied=true, mode:"polygon", containers_polygon:["un:fra"]` |
| no scope requested | 400 hits, no `scope` | **unchanged**, `scope: null` |

`/api/reconcile` re-verified on the same three shapes — unchanged, as intended,
since only its scope *implementation* moved.

⚠️ The container id is itself a trap: the audit's original repro used
`un:FRA_0`, and the real place_id is `un:fra`. Both halves of a scope test can
"pass" for the wrong reason with a wrong id, so the probe asserts
`GET /places/_doc/un:fra` → `found: true, geom_class: ["area"]` and
`un:not_a_real_place` → `found: false` **before** it interprets either answer.

Pinned by `tests/test_search_scope_fail_closed.py` (10 cases). The endpoint test
asserts not merely that the failed-closed response is empty but that it queried
Elasticsearch **not at all** — zero hits from a global search would satisfy an
emptiness assertion while still being exactly this bug. Two further cases assert
search and reconcile produce *identical* `ScopeInfo` for the same region, which
is the property the shared builder exists to hold.

#### Follow-up — the other half of the class, `177ba72` (deployed, verified)

S2 found the twin while measuring 2.1 and handed it over rather than editing a
verified step. 2.2 was a scope that *could not* be applied answering globally;
this is one that **can** be applied answering at the wrong precision.
`hit_matches` falls through to the H3 branch whenever no prepared geometry is
available, and said nothing: before the `un` merge, `contained_in:["un:fra"]`
with `containment=exact` returned the fuzzy answer while reporting
`applied: true, mode: "polygon", approximate: false` — every word true of the
region, none of it saying its geometry was absent.

`spatial.exact_degraded` + `mark_scope_degraded`, called from both routers
**after** the containment pass (geometry loads lazily, so before it there is
nothing to have failed). It reuses `approximate` — "coarser than what was asked
for" is exactly this — rather than minting a field, and keeps `applied: true`,
because the scope did apply.

**Verified live on a container proven absent from the store**, not on an
inference: `og:10209` is areal in the index and has **0** entries in
`index.sqlite`, and its exact and fuzzy answers are the *same 80 hits* —
now `approximate: true` with the reason. The control is `un:fra`, whose polygon
S2 had just merged: exact refines 73 → 72 and is **not** flagged, so the flag
is not simply always-on. `/api/reconcile` matches on both. 2.2's own three cases
re-checked after the restart and unchanged.

This changes the standing advice, which is the part worth carrying: until today
the only way to detect the degradation was to run a query whose exact and fuzzy
answers *should* differ and notice identical counts — an inference nobody made
for three weeks. `scope.approximate` now says it outright.

Two deliberate edges: a radial `h3-disc` region asked for `exact` **is** flagged
(the fuzzy test is that path's design, but the client still asked for boundary
precision and got cells; `mode` says which), and a failed-closed scope is never
relabelled `approximate` — that flag describes a constraint that was applied,
and there isn't one.

### 2.3 `whg` — re-ingest so ids match the reconciliation service  — **S3**

Production carries `whg:<dataset>:<WHG place key>`; `f835b26` (18 Aug) mints
`whg:<dataset>:<src_id>`. Confirmed live: `whg:1052:6954931`, whose `src_id` is
`8`. ~229 k docs.

⚠️ **This is not a one-command re-ingest — the id is a join key in three places.**

* `processing.index_namespace --namespace whg --replace --execute` handles the
  index side: `delete_by_query` on the `whg:` prefix, then re-index, then strip
  the stale toponym attestations. Without `--replace` the old docs survive as
  orphans under their old ids.
* **The geom store keys change too** (`{place_id}_{n}`), so the old `whg:` keys
  become unreachable garbage and the new ones must be merged in. There is still
  no prune step (§4.4), so plan to leave the old keys and note them.
* **`clustering/harvest/contributor_replay.py:115` mints the same id
  independently in SQL** — `('whg:' || d.id || ':' || pl.place_id)` — and the
  overlay holds **2,933 + 26,971 `whg:` rows**. Re-ingesting without changing
  that query points ~30 k hard-link edges at ids that no longer exist.

  ⚠️ **There is a third source, and it is not SQL-minted.** `contributor_replay`
  also reads `_ACTIVE_ATTESTATION_QUERY` over `api_contributorattestation`, whose
  `place_a` / `place_b` arrive **already namespaced from Django** rather than
  being built in the query. Whether those stored ids need the same map join
  depends on which form Django wrote them in: old `whg:<ds>:<place key>` rows
  need it, and rows Django already emits as `whg:<ds>:<src_id>` must be left
  alone. **Measure before writing the join** — raised by S3, 31 Aug.

  And it cannot simply be changed to `src_id`: the authority's duplicate rule
  (`whg:<ds>:<src_id>:<place_key>` for a repeat within a dataset) depends on
  **stream order** — first occurrence wins the plain id — so SQL cannot reproduce
  it.

#### The id map — SG-accepted 31 August 2026

**The whg extract emits a `place_key → place_id` map; `contributor_replay` joins
through it and drops (with a count) any row that does not match.** Accepted after
measuring the alternative and the status quo.

*Why not make the id derivable instead?* The disambiguation could be made
reproducible in SQL — sort by place key, `ROW_NUMBER() OVER (PARTITION BY
dataset, src_id ORDER BY place key)`, append the key where the number > 1. It
would need no artifact. But it puts the id rule in **two implementations that
must agree forever**, across a Python stream and a Postgres window function, over
an LPF feed whose order is not guaranteed to match `ORDER BY p.id`. That is the
exact failure mode this campaign was created to stop: *"the per-source transforms
already exist in the fixed authority scripts, so a backfill would be a second
implementation that drifts."* One implementation plus an explicit join is the
more robust shape.

*And the join fixes a defect that already exists.* Measured 31 Aug against the
live overlay and index:

```
distinct whg endpoints in overlay : 13,466
resolve in live places index      :  2,734
DANGLING today                    : 10,732   (79.7%)
datasets referenced 89 | with dangling refs 51
```

**Four out of five `whg:` edges in the published overlay already point at places
that do not exist**, and this predates any id change. The cause is in the code:
`contributor_replay` gates on `d.ds_status = ANY('indexed','accessioning',
'wd-complete')` with **no `authority` / `public` check**, while `whg-places.py`
ingests only what `/reconcile/authority-datasets` returns
(`authority=True AND public AND ds_status ∈ {accessioning, indexed}`) — 48
datasets in the index against 89 referenced by the overlay. None of the sampled
dangling datasets is in the index.

So the id map is not merely a way to survive the re-mint: **it makes an edge
expressible only for a place that was actually indexed**, which is the correct
semantics and turns 10,732 silent danglers into a reported drop count.

Requirements, so the artifact cannot itself drift:

* the map is written by the extract, stamped with the run id, and lives beside
  the staged tree — never hand-maintained;
* `contributor_replay` takes the map path as an argument and **fails loudly if it
  is absent**, rather than falling back to minting ids itself;
* unmatched rows are dropped and **counted per dataset** in the run report — a
  build that suddenly drops 80% should say so, not look like a clean one;
* the overlay rebuild runs *after* the whg extract, and the two must carry the
  same run id.

Then re-publish the overlay and re-push the registry inventory for `whg`.

**Whether those 41 non-ingested datasets *should* be in the index at all is a
separate question** — `Dataset.authority=True` is a Django-side gate, not repo
code. Tracked as 4.8; it does not block this step, and the map join is correct
either way.

**Verify:** a known record dereferences on both sides (the `yukon100` example —
prod should hold `whg:1052:8`, not `whg:1052:6954931`); **every** `whg:`
endpoint in the rebuilt overlay resolves in the index (today 2,734 of 13,466 do —
the post-fix number must be 100% of what survives the join, with the drop
counted); and the run's `no_src_id` / `duplicate_src_id` counters are reported,
not swallowed.

#### S3 progress — 31 August 2026

Run id **`whg-idmap-20260831T071935Z`**. Code in `4d763b8`.

**Corrections to this section, measured rather than assumed.**

*The geom-store sub-step above is wrong, and its replacement is larger.* There
were never any old `whg:` keys to become garbage: `index.sqlite` holds **0** of
them (11,758,768 total). The cause is a defect in the extract, not in the id
change — `authorities/whg-places.py` passed `geom_key` to `enrich_geometry`
without ever calling `configure_module_writer`, and `enrich_geometry` writes only
when a module writer is configured, so every whg geometry was dropped on the
floor from the first ingest. **Never written, not lost.** Fourteen other
authorities configure the writer; `grep -L configure_module_writer` over the
authorities that compute geometry is the check that finds this class (it also
finds `og` — see §4.1). SG approved fixing it inside this step. The re-extract
therefore writes **9,849** geometries where the store held none.

That count is four times the 2,320 the `geom_class` aggregation suggests, because
`enrich_geometry` stores anything that is not a bare `Point` while `geom_class`
folds MultiPoint into `point`. Reading the WKB type byte of all 9,849 staged
entries settles what that difference is actually worth:

| WKB type | entries | |
|---|---:|---|
| MultiPoint | 7,529 | invisible to the `geom_class` predicate |
| LineString | 1,044 | } 1,072 — matches `geom_class:line` exactly |
| MultiLineString | 28 | } |
| Polygon | 770 | } 1,248 — matches `geom_class:area` exactly |
| MultiPolygon | 478 | } |

**The MultiPoint recovery is much smaller than the entry count implies, and
smaller than I first reported.** Those 7,529 MultiPoints hold 8,219 member points
between them, so all but ~690 are single-member: the coordinates actually being
discarded across the whole namespace number **690**, not thousands of places
reduced to one point. The substantive repair is the 2,320 areal and linear
shapes; the MultiPoint half is a rounding error in data terms.

⚠ **But the detector is blind here, and that part generalises.** A MultiPoint
that lost every member but one reads `geom_class:point, has_geom:false` — exactly
like an ordinary point that never had a geometry. So the standing
`geom_class ∈ {area,line} AND NOT has_geom` predicate that CLAUDE.md offers as
*the* incomplete-ingestion detector cannot see this class at all. It happens to
cost 690 coordinates in `whg`; in a namespace of genuine multi-part point
features it would hide an arbitrary amount, and silently. Raised by S2, measured
by S3. Either the predicate needs a third arm, or `geom_class` should stop
folding MultiPoint into `point`.

*The attestation source needed handling and this section did not name it.*
`contributor_replay` has a third query, `_ACTIVE_ATTESTATION_QUERY`, whose ids
Django minted rather than this repo. The two errors are not symmetric — leaving a
legacy id alone yields a dangling edge, but rewriting an already-current one
corrupts a good edge — so the code reconciles instead of choosing:
`WhgIdMap.resolve_legacy_id` tests "already current" **first** and
unconditionally, translates only an unrecognised legacy form, drops anything that
is neither, and tallies the dispositions per run. Measured on the published
overlay, that path is currently dormant: all 26,981 contributor rows carry
`:legacy_v3_2`, so the live flow has contributed nothing yet and no measurement
could have settled the question — which is the case for reconciling rather than
guessing.

**Baseline, reproduced independently before any change** (`_mget` of every
overlay endpoint against the live index): 13,466 distinct `whg:` endpoints,
**2,734 resolve, 10,732 dangle**, 89 datasets referenced, 51 with dangling refs.
Two refinements the bare count hides: only **38 of the 48** indexed whg datasets
appear among the resolving endpoints, so the shortfall is not uniform; and every
one of the 13,466 has exactly **three** segments, none in the four-segment
duplicate form — so nothing in today's overlay can be mistaken for a current-form
id by a prefix test, which is the assumption a future reader is most likely to
make silently. The resolving-id list is kept so the post-fix comparison can be
endpoint-by-endpoint rather than by count.

**Extract — done, Slurm 11074319, COMPLETED in 5:54.**

| check | result |
|---|---|
| docs written / skipped | 228,918 / 0 — exactly the 228,918 whg docs in prod |
| distinct place_ids | 228,918 — the new id rule collides on nothing |
| id-map records | 228,918 + 1 run stamp; **staged-not-mapped 0, mapped-not-staged 0** |
| `no_src_id` / `duplicate_src_id` | **0 / 0** across all 48 datasets |
| the `yukon100` probe | map holds `1052 / 6954931 → whg:1052:8` ✅ |
| `geom_class` | point 213,081 / area 1,248 / line 1,072 — identical to prod |
| geometries staged for the store | **9,849** (was 0) — 2,320 areal/linear + 7,529 MultiPoint |
| places with ccodes | 224,118 (prod holds 224,232 — 114 fewer, upstream drift since 6 Aug, not a regression of this run) |

**Join pre-flight — the id map predicts the drop exactly, before touching PG or
prod.** Resolving all 13,466 overlay endpoints through the map alone:

```
WOULD RESOLVE : 2,734
WOULD DROP    : 10,732   across 51 datasets
```

Unit-identical to the baseline measured independently by `_mget` against the live
index (2,734 / 10,732 / 51). Two different paths — an ES lookup of each endpoint,
and a pure offline join against the extract's own artefact — agreeing to the unit
is about as good as this gets: the map's key set *is* the set of places that were
indexed, so the join drops precisely the danglers and nothing else. The top
dropped datasets are `1631` (4,793), `1611` (1,166) and `1415` (1,052).

The map also loads in **0.3 s** for 228,918 entries, so the join costs the
harvest nothing.

Worth recording because it cuts against the argument for the map: **both id edge
cases are empty in today's corpus**, so the rule is momentarily reproducible in
SQL. That is an accident of the current data, not a property of the rule — the
duplicate case still turns on stream order the moment one contributor re-uploads
a dataset with a repeated `src_id`.

**Geom merge + stage chain — done, Slurm 11074332, COMPLETED in 4:23.**

```
kept 11,759,015 existing + wrote 9,849 = 11,768,864 rows in index.sqlite
whg keys 0 → 9,849      un keys 247 (S2's, preserved)     ukhc 92
h3_merge   : 215,401 geometry updates, 0 unmatched patches
ccode_merge: 228,918 pass-through (whg ccodes come from the LPF source)
final audit: has_geom but NO h3_cover = 0
```

`215,401 geometry updates` equalling the geometry count exactly is what shows the
merge-before-h3 ordering actually held — every geometry took h3 from its real
polygon rather than from a convex hull. S2 re-ran their full `un` verification
after this rewrote `index.sqlite` wholesale and got PASS with 247/247 resolving
and a worst bounds delta of 0.000e+00, so the store is consistent after the
merge, not merely the right size.

**Production re-index — done, on pitt, zero errors.**

```
[places]   deleted 228,918   indexed ok=228,918   errors=0
[toponyms] stripped from 206,992   updated ok=207,028   errors=0
```

| verification (post-restart) | result |
|---|---|
| **the plan's headline check** | `whg:1052:8` → found, title "Edigiinjik"; `whg:1052:6954931` → **`found:false`** ✅ |
| whg places | 228,918 — no orphans, no duplicates |
| toponyms carrying a whg attestation | 207,028 |
| whg toponyms lacking an embedding | **0** (36 new ones backfilled, Symphonym v7) |
| whg geometries with `has_geom` | **9,849** (was 0) |

Then `gaz_request.sh es-restart` (EXIT 0): heap 54% at peak → **9%** (2.6 gb of
28 gb), write pool `active 0 / queue 0 / rejected 0`, 0 pending tasks — S4's
precondition met and independently re-measured by S4.

**Registry inventory — pushed**, 48 datasets, prod and dev, `HTTP 200`,
`{"upserted":47}` + `{"upserted":1}` on each, no errors.

**Two operational faults found and fixed while running this step**, both worth
more than the step itself:

* `consolidate_geom_store` **reported success having merged nothing** (`adc7345`).
  A mis-quoted `--staging-dir "\$STAGING"` in an sbatch heredoc reached bash as a
  literal `$STAGING`; the tool glob'd a directory that does not exist, printed
  `no entries found`, returned 0 and exited 0, and the job carried on to h3 having
  merged none of 9,849 geometries. The contrast three lines later is the useful
  part: `h3_stage` hit the *identical* unexpanded variable and raised
  `FileNotFoundError`, which `set -e` turned into a clean abort. One tool treated
  "found nothing" as "did nothing wrong". Now a missing staging directory is a
  `FileNotFoundError` and the CLI exits 3 on a zero-entry merge (`--allow-empty`
  for the deliberate no-op). The early return sits above every write, so the bad
  run touched shared state **zero times** — a pure false success signal. S2's
  generalisation is the transferable one: *assert on the directory you are about
  to consume, before you consume it.*
* An htc node killed a job in one second (`42b6e4a`). `import sqlite3` on htc-n77
  raised `GLIBCXX_3.4.30 not found` — the node's `/lib64/libstdc++` predates what
  the env's `libicuuc` needs, while other htc nodes run the identical job. The
  env ships its own; `submit_hardlinks_slurm` now prefers it and probes the import
  in the first second rather than after the LOC harvest.

Also worth recording, since it is a source-data fact rather than a pipeline one:
the 36 toponyms created without embeddings are all **malformed at source** —
comma-joined lists of twenty-odd name variants crammed into a single LPF
`toponym` field (dataset `whg:1760`). The same data produced the one toponym
whose `_id` exceeds Elasticsearch's 512-byte limit and is skipped by design. Not
caused by this re-ingest, and not fixed by it.

**The geometry repair is live in the gateway, and it bought a capability rather
than just tidiness.** A whg polygon now works as a `contained_in` region:

```
POST /api/search {"contained_in":["whg:1155:hc_11"],"containment":"exact","relation":"within"}
→ scope: {"applied": true, "mode": "polygon", "approximate": false,
          "containers_polygon": ["whg:1155:hc_11"]}
```

`approximate: false` is the whole point. With the store empty of `whg` this could
only ever have degraded to the fuzzy H3 test — the same silent degradation S2
measured for `un`, where `containment=exact` scoped to France returned the
byte-identical *fuzzy* answer (15 hits) until the merge, and 4 afterwards. So
contributed-dataset polygons have never been usable as search scopes until today.
The gateway picked the new `index.sqlite` up from S1's restart, which happened to
follow the merge; had it not, this would have needed one of its own.

⚠️ **A consequence for S5 that is not in §3.1: the `whg` tileset now carries
stale place ids.** Its features were baked on 23 July against
`whg:<dataset>:<place key>`, and those ids no longer exist in the index, so a
click-through from the map cannot dereference. The retile fixes it because it
rebuilds all 27 buckets — but `whg` has moved from "would be nice to refresh" to
**mandatory**, and it should not be dropped from the list to save time.

**The id-map join, run against the real DO Postgres tables** (Slurm 11074345,
7 seconds — contributor replay alone, into a scratch database that is never
published):

```
place_link_input  34,569 → converted  1,087
close_match_input  3,206 → converted    956
attestation_input      0 → converted      0     ← the live flow is still dormant
dropped_unmapped : 25,997 refs across 51 datasets   (top: 1631=9,846, 1611=2,439)
id_map_entries   : 228,918   run_ids: ['whg-idmap-20260831T071935Z']
```

**And the verification §2.3 asks for, against the live index:**

```
BEFORE (published overlay): 2,734 of 13,466 resolve, 10,732 DANGLING
AFTER  (id-map join)      : 1,935 of  1,935 resolve,      0 DANGLING
```

**Zero.** Every `whg:` endpoint the join emits dereferences.

Two results to read carefully rather than at a glance, because both look like
losses:

* **Contributor rows fall from 26,981 to 2,038 — a 92% drop, and that is the
  correct answer.** It is *worse* than the 79.7% endpoint figure because the
  dangling endpoints are the ones carrying the most edges: 24,058 `place_link`
  rows in the published overlay become 1,087. So the published overlay's
  contributor layer is roughly **92% edges pointing at places that do not
  exist** — a sharper statement of §2.3's finding than the endpoint count gives.
  Nobody should read the smaller number as a regression.
* **`attestation_input` is 0**, confirming from the source side what the overlay
  showed: the `api_contributorattestation` flow has no active rows yet. The
  disposition tally is therefore empty — and populates itself the first time
  Django writes one, which is the point of tallying rather than assuming.

**The overlay publish is NOT done, and 2.5 is why.** §2.3 ends "then re-publish
the overlay"; it cannot be done in this session. `hard_links_staged` iterates
every namespace's staged snapshot, and the full rebuild (Slurm 11074337,
cancelled) reported `osm: attempted=2,295,659`, `ohm: 98,569`, **`gn: 0`,
`wd: 0`** — those being exactly the trees 2.5 restores. By endpoint namespace
across the published overlay's 7,596,959 rows: `wd` 7,516,092 and `gn` 5,092,751
against `osm` 2,318,576, `ohm` 98,569, `iv` 68,935, `whg` 29,904. **`gn` and `wd`
are not a component of the overlay, they are very nearly all of it.** Publishing
that rebuild would have replaced a 7.6 M-row overlay with a fraction of one —
and the ship step would have reported success, because the marker records the new
row count and nothing compares it to what it replaced. The same shape as the
7 August retile.

So the build was cancelled, **nothing was published**, and
`/ix1/ishi/hardlinks/hard_links.sqlite` is untouched — still the 6 August build,
which predates the staged-tree loss and therefore still holds the `gn`/`wd`
links. That is the right state to leave it in.

**For whoever rebuilds the overlay after 2.5:** the id-map change is on `main`
and `submit_hardlinks_slurm` passes `--id-map` automatically, so an ordinary
invocation does the right thing — it simply has to happen *after* the `gn`/`wd`
trees are back. Expect the contributor layer at roughly 2,000 rows rather than
26,981; that is the fix working, not a loss. The natural guard, not added here:
**compare a rebuilt overlay's row count and per-namespace endpoint counts against
the overlay it is about to replace, and refuse an unexplained shrink** — the
sibling of the geom-store guard in `adc7345`.

### 2.4 Fault 12 — a skipped stage is a stage not regenerated  — **S1**

Still unfixed in code: `submit_ccode_slurm._mark_un_skipped` only marks `un`'s
`ccode` / `ccode_merge` stages `skipped`. But `final/` is written by
`ccode_merge`, so skipping it also skips `un`'s `final/` regeneration — its
improved `h3_cover` sat in `h3_merged` for three days while the index kept a
stale copy, and the freshness gate could not see it because `final/` was
self-consistent. `un` is the namespace that supplies `contained_in` regions, so
this alone nullified the #174 fix until it was found by hand.

Fix: when ccode is skipped, regenerate `final/` from `h3_merged` rather than
leaving it. **Verify** with a unit test that a skipped-ccode namespace still ends
with `final/` newer than `h3_merged/`. (Fault 13, the wall-time floors, is
already committed — `_MIN_CCODE_WALL_SECONDS`, `_MIN_WALL_SECONDS`.)

✅ **Done 31 Aug — `0c2819c`.** `_mark_un_skipped` now marks only `ccode`
skipped and runs `ccode_merge` as a **pass-through**: `run_ccode_merge` gained
`allow_missing_patch`, which treats an absent patch as empty instead of raising.
That is the same "copy through untouched" semantics the incremental
single-namespace workflow already gets from an empty `places.ccode.jsonl`,
without the fake artifact. Inline, because `un` is ~250 documents; every other
namespace still goes through the Slurm array, and **for them a missing patch
stays a hard error** — there it means the enrichment produced nothing, and
passing those documents through would publish a corpus with no ccodes.

Two behaviours worth knowing before the next rebuild:

* a stale-check (`final/` older than `h3_merged/`) keeps a re-submitted array
  idempotent — a resume does not redo a merge that is already current;
* with no `h3_merged/` to derive from it falls back to the old `skipped`, so the
  global barrier still passes, but it **says so on stdout**. A silent skip is
  precisely how the stale `final/` hid for three days.

`tests/test_ccode_skip_regenerates_final.py` (6 cases) asserts the independent
measure the plan asked for — `final/` newer than the `h3_merged/` it derives
from, and carrying its content (the fresh `h3_cover`, not the previous run's) —
rather than the stage's own status, which reported success throughout the run
that broke this.

### 2.5 Restore the `gn` / `wd` staged trees  — **S4**  ⚠️ RUNS BEFORE 2.6

Collateral from the `unittest discover` accident: `staged/gn` is 6.5 KB and
`staged/wd` 14 KB — stubs. Staging is the pipeline's canonical input, so the next
rebuild regresses both without this.

**Scope is three namespaces, not two** (S4's census, Slurm 11074343, 31 Aug):

* `wd` — a re-run extract already exists at `/vast/ishi/staged_geomrebuild/wd`
  (9.7 GB); **promote it** rather than re-run. ⚠️ **The promotion must DELETE the
  stub `places.parquet`, not merely add the real `places.jsonl` beside it.**
  `_staged_namespace_source` (`index_from_stage.py:88`) tests `places.parquet`
  *before* `places.jsonl` within each stage directory, so leaving the 4,925-byte
  stub in place makes the whole promotion a silent no-op.
* `gn` — needs a fresh extract. It is not merely small: it is **one row**.
* `nl` — **`/vast/ishi/staged/nl` does not exist at all**, while prod holds 4,363
  `nl` places. Worse than a stub, because a stub is at least implausibly small on
  sight, whereas a missing directory makes `_staged_namespace_source` return None
  and `_count_staged_places` skip it inside `except Exception: continue`, silently.
  ⚠️ **Attribution — I got this wrong once, in both directions; here is the
  evidence.** I first told S4 that `nl` predated the `unittest discover` accident,
  citing `HANDOVER-2026-08-09-geom-store.md` §5 ("`nl` and `un` staged data was
  already missing before any of this"). That handover was written on 9 August and
  its "before any of this" is looser than it reads. The tile job log
  `tiles-ns-10756173_*.out`, timestamped **2026-08-07T01:21**, contains
  `nl → nl: 4,363 features (poly=4,363 point=0)` — and the tile builder reads
  staged trees (`TILE_ES_DOC_NAMESPACES` did not exist until `71bcc39` on
  8 August). So `staged/nl` **existed with its full 4,363 places on 7 August** and
  vanished afterwards, which puts it with the accident, not before it. Treat the
  handover's phrasing as the unreliable witness and the log as the record.

**How much is missing, measured:** a stage-1 run today would scan **26,269,329 of
51,188,772 staged places — 51.3% of the corpus** — and report success. Not "two
namespaces short": half the corpus, including *all* of GeoNames and *all* of
Wikidata, which are exactly the namespaces Symphonym trains on
(`--training-namespaces gn wd tgn`).

**Also noticed, not yours to fix:** `un` now has `extract/` but no `final/` — the
staged-tree residue of Fault 12, distinct from S1's code fix for it. Harmless for
2.6 (toponyms are populated at extract time), but a future rebuild following the
normal preference chain will not find a `final/` for the namespace that supplies
`contained_in` regions. Tracked here rather than fixed.

**Verify:** each tree's `extract/places.jsonl` doc count matches the live index's
per-namespace count, not merely that the file is large. ⚠️ The counts are
**`gn` 13,454,817, `wd` 11,459,393, `nl` 4,363**, measured against prod on 31 Aug.
This document previously said `gn` "~11.6 M after alt names"; that figure is
wrong by 1.9 M and, being a plausible-looking near-miss, is exactly the kind of
expected value that gets a correct result written off as a mismatch. Take the
target from `_count` at the time, not from here.

**And prefer one identity over three checks (S4, 31 Aug).** The per-namespace
counts localise a failure; they should not be what *declares success*, because
three named checks only find the three failures somebody already thought to name
— which is precisely how `nl` went unnoticed. The completion test is that the
staged corpus reconciles to the live index **to the document**:

```
staged total across all 27 namespaces  ==  places index total  (51,187,900)
```

It closes on paper already: the pre-restore census of 26,269,329, less the two
stub rows, plus 13,454,817 + 11,459,393 + 4,363 = 51,187,900 exactly. One
equality, and it catches a fourth namespace drifting that nobody asked about.

⚠️ **Do not take the target from the 4 August run log** — it records 51,188,772,
872 more than the index holds today. The same trap as "~11.6 M" from the other
direction: a number that reads authoritative because it genuinely was measured,
just not of the thing you are comparing. The 872 are unexplained and not implied
to matter.

**Done, 31 Aug (S4) — two of three:**

| ns | action | result |
|---|---|---|
| `wd` | promoted from `/vast/ishi/staged_geomrebuild/wd` | **PASS** — 11,459,393 = 11,459,393, delta 0 |
| `nl` | fresh extract (Slurm 11074353, 13 s) | **PASS** — 4,363 = 4,363, delta 0 |
| `gn` | fresh extract (Slurm 11074352, 8 cpu / 64 G / 36 h) | ⏳ running at time of writing — 500,000 staged at 26 min, so ~12 h for the places pass **plus** the `geonames-toponyms` alt-names pass that follows it |

**To finish 2.5 — one command, and it does not need S4's session.** The verifier
is parked on shared storage, so any session can run it:

```bash
ssh crc1 'sacct -M htc -j 11074352 --format=State'          # expect COMPLETED
ssh crc1 'sbatch -M htc /vast/ishi/staged/s4_verify_staged.sbatch \
              gn=13454817 wd=11459393 nl=4363'
```

PASS on all three closes 2.5 and unblocks both 2.6 and S5. It calls the
pipeline's own resolvers rather than reimplementing them, and asserts the last
line parses as JSON. ⚠️ **`gn` runs two scripts** (`geonames-places` then
`geonames-toponyms`); a `COMPLETED` after only the first would still leave the
count short, so check the state, not the clock. And the whole-corpus form is the
stronger test: the 27 staged counts should sum to **51,187,900**, the live index
total.

Verified by Slurm 11074356, which calls the pipeline's **own** resolvers
(`_staged_namespace_source`, `h3_stage._extract_stage_dir`) rather than a
reimplementation of them, and additionally asserts the last line parses as JSON —
a truncated JSONL counts fine, so a line count alone cannot see it.

**Is anything ELSE quietly degraded? No — asked and answered, 31 Aug (S4).** Once
it was established that the accident's blast radius was larger than recorded, the
open question became whether a fourth namespace had gone quiet without anyone
noticing — which is really the question of whether 2.5 is *complete*, so it was
worth settling rather than tracking. Comparing the staged census (Slurm 11074343,
taken **before** any restore) against the live index's per-namespace counts:

* **24 of 27 namespaces match the live index exactly** — `osm` 20,622,228,
  `tgn` 2,991,143, `gb` 1,174,449, `ohm` 945,156, `whg` 228,918, … down to
  `vob_rc` 55. Not "close": equal, every one.
* **3 are damaged, and they are the three already known** — `gn` (1 row),
  `wd` (1 row), `nl` (absent).

And the arithmetic closes: 26,269,329 staged today, less the 2 stub rows, plus
the three real counts (13,454,817 + 11,459,393 + 4,363) = **51,187,900 — exactly
the live index total**. So when `gn` lands, the staged corpus should reconcile to
the index to the document, and that equality is a stronger completion test for
2.5 than three separate per-namespace checks.

⚠️ One residual, deliberately not chased: the 4 Aug stage-1 run scanned
**51,188,772**, which is **872 more** than the index holds today. Small, and
plausibly ordinary churn since, but it means **51,188,772 is the wrong target to
restore to** — take the target from the live index at the time, not from that log
line. (This is the same failure the `gn` "~11.6 M" figure would have caused,
arriving from a different direction: a stale number that looks authoritative
because it was once measured.)

Three notes for whoever finishes or repeats this:

* The `wd` promotion was done with **hard links**, not a copy: `/vast/ishi` is one
  filesystem, so it cost 0 bytes and left `staged_geomrebuild/wd` valid. It also
  means the data survives an `rm -rf` of *either* path.
* The stub tree was **preserved, not deleted**, at
  `/vast/ishi/staged/wd.stubs-preserved-20260831T040511Z`.
* ⚠️ **The stub `update_merged/` had to go too, and for a different reason than
  the parquet trap.** `h3_stage._extract_stage_dir` returns `update_merged/` for
  `gn`/`wd` on `if update_merged.exists()` — **existence, not content** — so a
  1-row stub directory would have fed the next H3 stage a 1-row snapshot while
  `extract/` sat there correct and unread. Absent, it correctly falls through to
  `extract/`. Two independent resolvers, two different traps, one stub tree.

⚠️ **This is 2.6's input, which is why it now runs first — see 2.6.** The failure
if you skip it is silent by construction: `_staged_namespace_source` walks
final → h3_merged → boundary_merged → extract and takes the first hit, so `gn`
resolves to a **2,539-byte `extract/places.parquet`** — a *valid* Parquet file
with almost no rows, not an error — and `_count_staged_places` swallows the rest
in `except Exception: continue`.

### 2.6 Re-run toponyms stage 1 for `ipa` / `panphon_features`  — **S4**  ⚠️ AFTER 2.5

⚠️ **Three corrections, all measured by S4 on 31 Aug. The original text of this
step was wrong in its cause, its hazard and its position.**

**It did not time out — the columns were skipped by design.** The preserved
sbatch (`/vast/ishi/staged/runs/temporal-20260731T160000Z/toponyms.sbatch`) ends
`--skip-es-index --confirm --training-namespaces _none_`, an unmatched sentinel
that `submit_batch9_slurm` passes by default unless `--for-retrain` is given, and
has since `ef31016` (2 May): *"IPA + PanPhon are training-only artefacts… default
to skipping the Epitran/Phonikud/CharsiuG2P pipeline"*. So the columns are empty
because the submitter meant them to be.

**The 12 h timeout was a different job.** `sacct -M htc` shows both
`whg-toponyms-temporal-20260731T160000Z` jobs COMPLETED inside a 3:41 wall
(02:31:30 and 03:26:50); the TIMEOUT was `whg-toponyms-rerun` on 4 August, a
later backfill attempt that reached `Updated 28,000,000 / 31,942,400` (87.7%)
before being killed. The audit's "stage 1 timed out at its 12 h wall" merged two
jobs with different names, walls and outcomes. "Allow ~9 h and raise the wall"
remains right — for the rerun's reason, not stage 1's.

**It never touches Elasticsearch.** The run opens `Skipping ES connection
(--skip-es-index set; staged-only mode)` and then scans staged places;
`extract_toponyms_to_db`'s docstring says "Per Batch 9, Elasticsearch is **not**
consulted here". So this is not an ES-heavy job, it never contended with S3, and
there is no ES restart to take afterwards. The hazard table is corrected.

**What it does read is every namespace's staged tree — which is what 2.5
repairs.** Run before 2.5 it builds the training vocabulary from a corpus missing
its two largest namespaces (~23 M of 51 M places) and reports success: a nine-hour
job, a plausible-looking DuckDB, and the only consumer is the next Symphonym
training run, which would train on a corpus with no GeoNames and no Wikidata in
it. My original ordering — 2.6 first, to use its wall-clock for 2.5 — assumed the
two were independent; 2.5 is 2.6's input.

**Verify — and establish the baseline first (§ the standing rule).** The named
check is `COUNT(*) == COUNT(ipa) == COUNT(panphon_features)`, but **confirm it
still discriminates in the state you actually find**. Three states, and the
middle one breaks the check: columns absent; columns present and empty; columns
**partially** populated — in which case a resume that skips rows already carrying
an `ipa` yields `COUNT(ipa) == COUNT(*)` and passes while leaving the killed
run's output unexamined. Given the rerun died at 87.7% of an UPDATE pass, expect
the partial state. Record `PRAGMA table_info` alongside the counts, and report
which state and whether the check separates good from bad in it.

---

## Phase 3 — publication (Atlas, Beta-gated)

### 3.1 Retile all 27 buckets  — **S5**

```bash
python -m processing.submit_tiles_slurm --run-id h3ccode-20260805T120000Z
```

Settles four things at once: restores the nine lost boundary layers (§2 of the
audit); gives the eight wave-1 buckets the `start_def` / `end_def` props they
lack, which is why the Atlas date filter is still switched off; publishes the
place#159 label anchors, which wave 2 also lost; and puts `clio`'s 2,986
newly-addressable polygons on the map for the first time — **a visible change to
the Cliopatria layer, worth a look before it ships**.

Preconditions and traps:

1. **2.1 must be done.** With `un` at 0 in the store, retiling it replaces the
   country boundaries with points — the §2 failure, repeated.
2. ⚠️ **WAIT FOR 2.5 — this is now a hard gate (SG, 31 Aug), not the either/or
   this step first described.** `gn`, `wd` and `nl` must be restored and counted
   (13,454,817 / 11,459,393 / 4,363, delta 0 against the live index) before you
   tile. Check with the pipeline's own resolver rather than by looking at the
   directory: every one of the six stage-preference chains tests
   `path.exists()` (4.11), so a 4,925-byte stub satisfies an `ls` perfectly.
   `nl` matters as much as the other two here — it is a tiled bucket, and its
   4,363 polygons are currently in the deployed tileset *only* because the
   7 August run read a staged tree that has since vanished.

   `TILE_ES_DOC_NAMESPACES=gn,wd` (from `71bcc39`) remains available as a
   deliberate override, but it is no longer the plan's answer: it scans ~24.5 M
   documents out of production ES and leaves the staged trees wrong for the next
   consumer, which is how the overlay harvest nearly published an empty file.
   Expect to wait several hours — `gn` runs two scripts (`geonames-places`, then
   `geonames-toponyms`) inside a 36 h wall.

3. **Retile the 47 `whg-*` per-dataset buckets too** (4.6). They are
   `generate_tiles` buckets like any other, and after 2.3's id re-mint every
   click-through from them is dead.
4. The tileserver is at **83% / 8.5 GB free**, and the last push briefly hit 99%
   because `tiler.service` held descriptors on the old inodes. **Restart the
   tileserver promptly after the push**, and watch the disk during it.

**⚠️ FIRST — prove the verifier can fail, and do it before you deploy, because
deploying destroys the evidence.** S4's point (31 Aug) is that Phase 3 looks like
the hard case for "run the check against a known-bad input", since nobody keeps a
poly-less tileset around to test with. But we have nine of them, they are on the
live tileserver right now, and **the retile is what overwrites them**. So:

1. Run your polygon verifier against the **preserved fixtures** (and, while they
   last, the deployed `po`, `clio`, `kain_par`) and confirm it reports **FAIL**
   on every one. If it passes any of them the verifier is broken, and you would
   otherwise have discovered that only by deploying a second poly-less
   generation. Running it against the preserved copies rather than the originals
   does double duty — it proves the verifier can fail *and* proves the copies are
   readable, which matters because a silently truncated fixture would only be
   discovered after the originals were gone.
2. ✅ **The fixtures are already captured — this is no longer your step, only a
   check.** `/vast/ishi/tiles-fixtures/` holds `ukhc.mbtiles` (135,168 B, md5
   `87b79add5b1ea4f549809b105bfe56c5`) and `vob_cty.mbtiles` (114,688 B, md5
   `efcd57a06d64a9bd84e2039370a4e057`), copied from the live tileserver on
   31 Aug **before** the retile, with a README recording what makes them
   known-bad. Checksums verified identical at source, in transit and at rest,
   and both decode on `/vast` to POLYGON=0. Confirm the directory is there and
   the checksums still match; if it is missing, **stop** — the originals are
   irreplaceable once you deploy.

   Captured now rather than left as your first step, on S4's argument: a fixture
   whose preservation is a precondition of the run that destroys it depends on
   the destroying session remembering to do it, which is the same failure shape
   as 7 August. As a precondition it is now "are the files there", answerable in
   a second and failing safe.

**Then verify — the check that would have caught this in the first place:**
assert a non-zero `poly=` count per polygon-bearing bucket in the job log *and* a
non-zero polygon count in the built tileset, before deploying. A tile job that reports
success is not evidence it read any geometry. Then re-run the audit's decode
across all 27 deployed tilesets and confirm: polygons present where expected,
`start_def`/`end_def` on every bucket, `label` on the banded ones.

### 3.2 whg3 — the two-mode `setFilter`  — **S6**

The last client-side piece of place#176 (*definitely* vs *possibly* alive), a few
lines, spelled out in the issue. Pointless before 3.1, since the props it filters
on only exist afterwards.

### 3.3 Delete the rollback index and the tile scratch  — **S7**

Once 3.1 is verified: `places_temporal-20260731t160000z` (23 GB),
`/vast/ishi/tiles-verify` (17 GB), `/ix1/ishi/data/tiles/_step0` (2.7 GB), and the
stale `*.geojsonl` in `/ix1/ishi/data/tiles` (~20 GB, including `osm_admin.*` from
the May 2025 pre-rename era). Confirm a SUCCESS snapshot exists and the index
holds no alias before deleting it.

---

## Phase 4 — tracked, not scheduled

| | |
|---|---|
| 4.1 | **`og` geometry is at its sources' ceiling, not broken** — 251 of 6,260, because ofs attests only 1,123 of og's admin units and **no og doc carries a wd link at all**. Raising it is a reconciliation task (establish og↔wd links), not a pipeline fix. Its 3.9% ccode coverage follows from this and needs no separate work. **But the 249 hulls it does compute are not retrievable either** (4.2), so fixing the writer is the cheaper half and comes first: it makes the geometry og already has usable, before any effort goes into acquiring more. |
| 4.2 | **Diagnosed 31 Aug, no longer a mystery tail.** The 2,569 `has_geom=false` docs are two authority bugs: `whg` (1,248 area + 1,072 line) passed `geom_key` but never configured a module writer — **fixed inside 2.3**; `og` (249 area) calls `enrich_geometry` with no `geom_key` at all, so its hulls are never keyed. The geometry was never written, not lost. `og`'s half is a small fix but needs a re-extract, so it sits here rather than in Phase 2 — take it with 4.1. Plus 1 areal doc with no `h3_cover`, unrelated. |
| 4.3 | `authorities/backfill_admin_levels.py` has a broken `BOUNDARIES_INDEX` import; not in `INGESTION_ORDER`, so not a rebuild blocker. |
| 4.4 | `geom_store --merge` grows every rebuild and has no prune step for keys absent from the current corpus. 2.3 will add ~229 k more orphans. |
| 4.9 | **Make the store cross-check permanent — S2's proposal, run once and answered.** Nobody had ever asked which namespaces claim retrievable geometry the store does not hold; `un` was found by a tile failure and `whg` by S3 reading an authority script, both by accident. I ran it 31 Aug, after S2's merge: **clean — all 15 namespaces claiming areal/line `has_geom=true` have keys**, `un` now 247/247. But note it takes **two** predicates, not one, and S2's framing catches only the first: a namespace whose index *claims* `has_geom=true` with zero keys (the `un` class, silent exact-containment degradation), **and** docs with `geom_class ∈ {area,line}` and `has_geom=false` (the `whg`/`og` class, 4.2 — geometry never written, so the index claims nothing and the first check sees nothing wrong). `processing/audit_rebuild.py` already computes the second and cannot do the first, because it never opens `index.sqlite`. Adding that is small and makes both run on every rebuild instead of waiting for the next accident. **⚠️ The pair is still not complete, and the gap cannot be closed from ES at all** (S2, 31 Aug). `geom_class_of` folds `MultiPoint` into `point`, so a multi-part point feature whose store entry is missing reads `geom_class:point, has_geom:false` — indistinguishable from an ordinary point, invisible to the second predicate. And it cannot be caught by the first either: I checked, and `whg` and `og` carry **0** docs with a `geometries.geom_ref`, because the ref is written only when the store write happens. A never-written geometry therefore leaves *nothing in the index that says it should have existed* — no claim, no ref, no class that differs from a point. S3 measured 690 such coordinates in `whg`: trivial there, arbitrary in a namespace of genuine multi-part point features. **So the third check has to sit upstream, not in the audit:** the static `grep -L configure_module_writer` over authorities that compute geometry, and per-extract reconciliation of geometries *computed* against the `Geometries in VAST store: N` the writer already prints. 4.9 is two of three; state it that way rather than as a closed question. |
| 4.10 | ⚠️ **THE PATTERN BEHIND EVERY FAILURE IN THIS CAMPAIGN — a destructive publish that never compares what it writes against what it replaces.** S3's observation, 31 Aug, verified: `processing/publish_hardlinks.publish` computes `row_count` from the **new** database, writes it into the completion marker, and never opens the incumbent at the target — so replacing a 7,596,959-row overlay with ~100 k lands looking exactly like a clean run. Four instances today alone, all the same shape: this one; `consolidate_geom_store` returning 0 and exiting 0 on a mis-pointed staging directory (S2, fixed in `adc7345`); the 7 August tile deploy pushing `poly=0` for every bucket; and, in May, a two-feature synthetic store overwriting the live geom index. The counter-example S3 spotted is instructive — `h3_stage` raises loudly on the *identical* bad variable three lines later, so the difference is a habit, not a constraint. **The fix is one shape applied in several places: before a destructive write, read the incumbent, compare magnitude, and refuse a material shrink unless overridden by name** (`--allow-shrink`, as `WHG_ALLOW_BNDA_ONLY` now guards `un`). Sites: `publish_hardlinks`, `consolidate_geom_store`, the tile deploy, `index_from_stage`. Note the overlay publish does keep one generation as `.previous`, so this class is recoverable — but only once, and silently consumed. |
| 4.11 | **Stage-preference chains test existence, not content — audited 31 Aug, and it is every one of them.** S4 found the third instance (`h3_stage._extract_stage_dir` returns `update_merged/` on `.exists()`, so a 1-row stub tree would have been fed to H3 while the correct 10.26 GB `extract/` sat unread) and asked whether `generate_tiles` and `index_namespace` behave the same way. They do, and so does the rest: **`index_from_stage:88`, `h3_stage:93`, `generate_tiles:862`, `index_namespace:112`, `gazetteer_temporal_extent:106`, and `rebuild_toponyms_index`** — six chains, all `if path.exists()`, none checking that the file it selects holds a plausible number of rows. Each prefers `places.parquet` over `places.jsonl` in the same directory, which is what makes a 4,925-byte stub a poison pill rather than a nuisance. **Fix once, apply six times:** the chain should skip a candidate whose row count is implausible for the namespace, or at minimum log the count and source it chose so a 1-row selection is visible in the log. Related to 4.10 — that one is about not overwriting good data with bad, this one about not *reading* bad data as good. ⚠️ **"Apply six times" is right but "fix once" needs care: the six chains are not the same chain** (S4, 31 Aug). Four are byte-identical — `index_from_stage`, `generate_tiles`, `hard_links_staged`, `gazetteer_temporal_extent` all use `(final, h3_merged, boundary_merged, update_merged, extract)`. Three do not: `rebuild_toponyms_index` omits `update_merged` (defensible — toponyms are populated at extract time and not mutated later); `h3_stage` is per-namespace and tests **directory** existence rather than a file, which is why it was the worst of them; and **`index_namespace` uses `(final, ccode_merged, h3_merged, extract)`** — which has two problems. `ccode_merged` **is written by nothing**: it appears in exactly two places in the tree, this tuple and `staging_orchestrator:58`, both readers, because the ccode stage's output is `final/` (as this plan says in the incremental-add workflow). So that entry is dead, and it makes the chain *look* like it covers the ccode stage when it does not. Meanwhile it omits `boundary_merged` and `update_merged`, which **are** written — so for `osm`/`ohm` with no `final/`, `index_namespace` skips the enriched `boundary_merged/` and falls through to the raw `extract/`, and `--source-stage` cannot even be asked for those stages because they are not in its `choices`. A shared helper must therefore take the chain as a parameter, and `index_namespace`'s wants fixing on its own account, not just refactoring. |
| 4.5 | AAT coverage 4,436 / 15,448 = 28.7% (place#142). |
| 4.6 | ⚠️ **PROMOTED 31 Aug from housekeeping to REQUIRED — and CORRECTED: this is S5's work, not a separate migration.** My first write-up filed the `whg-*` tilesets with the genuinely legacy `datasets-*` / `collections-*` family. They are not: `generate_tiles` builds them natively as **per-WHG-dataset buckets** (`_whg_dataset_sub_ids`, `whg-<dataset_sub_id>.mbtiles`, "one per contributor dataset discovered at submit time"), and the current 47 were produced by that same pipeline on 22–23 July. So S5 rebuilds them by naming those buckets — no separate project, no migration. The `datasets-*` / `collections-*` tilesets are the actual legacy family and stay in `plan-outstanding-2026-07.md` §8. S3 flagged that the `whg` tiles now carry dead place ids after 2.3's re-mint, and expected §3.1's 27-bucket retile to cover it. It does not: **there is no `whg` bucket**. `whg` is served as **47 legacy per-dataset tilesets** (`whg-<dataset_id>.mbtiles`, 22–23 July), which sit outside the 27 and are untouched by 3.1. Verified by decoding `whg-1052.mbtiles`: it carries `whg:1052:6954924`, `whg:1052:6954927` … — the old place-key form, which after 2.3 returns `found:false`. So **every click-through from those 47 layers is now dead**, and regenerating them is the completion of 2.3. **Add the 47 `whg-*` buckets to S5's run.** |
| 4.7 | Merge stages still hold whole patches in memory; the allocations are tiered, the profile is unchanged. |
| 4.8 | **41 of the 89 datasets referenced by contributor links are not in the index** (48 are). `contributor_replay` accepts `ds_status ∈ {indexed, accessioning, wd-complete}`; ingestion requires `Dataset.authority=True AND public`. 2.3's id map makes the mismatch harmless and visible, but the underlying question — publish them, or narrow the replay filter to match? — is a Django-side call for SG. |

---

## Critical path

```
S1  1.1 1.2 reclaim + 2.2 gateway scope + 2.4 fault 12
S2  2.1 un → geom store ──────────────────┐
S3  2.3 whg re-ingest (biggest)           ├─ S5  3.1 retile
S4  2.6 toponyms ipa (~9 h) + 2.5 gn/wd ──┘        │
                                                   ├─ S6  3.2 whg3
                                                   └─ S7  3.3 cleanup
```

⚠️ **Amended 31 Aug: S4's 2.5 now gates S3's overlay rebuild too** — the harvest
reads the same staged trees 2.5 repairs, and 98.9% of the overlay's rows touch
`wd`. That edge did not exist when this graph was drawn.

Only **S2** gates the retile — S4 does too, but only softly (`gn`/`wd` can be
read from the index with `TILE_ES_DOC_NAMESPACES` instead). S1, S2, S3 and S4 are
otherwise mutually independent and may run concurrently.

Everything in Phase 2 is ordered ahead of Phase 3 by decision (3), not by
dependency — so if the Atlas Beta needs its boundaries back sooner, **S2 → S5
alone is a valid short path**, leaving S1, S3 and S4 to follow.
