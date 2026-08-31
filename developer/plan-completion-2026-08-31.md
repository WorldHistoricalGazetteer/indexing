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
| **S2** | 2.1 | `un` extract → geom-store merge → gateway restart. One Slurm job, three independent verifications. **Gates S5.** | ⬜ |
| **S3** | 2.3 | The big one: id-map code change, re-ingest, geom merge, overlay rebuild, registry push. Deserves a session to itself. | 🟡 **in progress 31 Aug** — code done (`4d763b8`), extract done + verified (228,918 docs, id map 1:1, `yukon100` probe passes); geom merge held behind S2; nothing written to prod ES yet |
| **S4** | 2.6, then 2.5 | Submit the ~9 h toponyms stage 1 **first**, then do the `gn` extract / `wd` promotion while it runs. If stage 1 has not finished, **verify it at the head of the next session** rather than holding one open. | ⬜ |
| **S5** | 3.1 | The retile. Needs S2 done and, for `gn`/`wd`, either S4 done or `TILE_ES_DOC_NAMESPACES=gn,wd`. Verify polygons per bucket **before** deploying. | ⬜ |
| **S6** | 3.2 | `whg3` — a different repository, so a separate session by necessity. | ⬜ |
| **S7** | 3.3 | Post-retile cleanup, once the deployed map has been looked at (clio gains 2,986 polygons that have never rendered). | ⬜ |

**Order:** S1, S2, S3 and S4 are mutually independent *in their work*; see the
concurrency rules below before running them at the same time. Only S2 gates S5.
S5 → S6 → S7 is strict.

⚠️ **The condition that makes this work: every session updates its row's status
and the step's own notes before it ends.** A session that finishes work without
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
| **Production ES load** | S3 (delete-by-query + re-index of 229 k docs, plus the toponym augment), S4's 2.6 (full scan of the 72.7 M toponyms index), S5 (corpus-wide streaming for tiles) | **One heavy ES job at a time.** Heap saturation from heavy indexing has already taken faceted `/api/search` to 500s once, and `dense_vector` merges on the toponyms index are the known OOM driver. Restart ES after any of them. |
| **Gateway restarts** | S1 (2.2 needs one to deploy the scope fix) and S2 (2.1 ends with one) | **One restart owner.** Whichever finishes second performs the restart; the other says in its notes that its change is on disk but not yet loaded. A restart mid-test silently invalidates the other session's verification. |
| **Staged manifest writes** | S2, S3, S4 all write `staged/<ns>` and the run manifest | The `/vast` lock is `O_CREAT|O_EXCL` with **proceed-with-warning on timeout**, because `flock` returns `ENOLCK` there under fan-out. So two sessions *can* both proceed. Keep concurrent sessions on **different namespaces** — which S2/S3/S4 already are — and never let two touch the same one. |
| **The pitt VM** | any step running inline rather than via Slurm | Heavy work goes to Slurm. Several steps do small inline work on pitt; eight parallel inline resolvers once OOM-thrashed the VM into a ~1 h production outage. Two or three sessions doing "just a little" inline work is that same pattern. |

**Preconditions, as checks rather than prose.** A session must run its own before
starting, and must not trust this document's status boxes — they are only as
fresh as the last session that remembered to tick one.

| session | check before starting | expected |
|---|---|---|
| **S1** | none | — |
| **S2** | none | — |
| **S3** | none | — |
| **S4** | no bulk indexing in flight: `GET _cat/thread_pool/write?v` on prod | `active` and `queue` at 0 |
| **S5** | `sqlite3 /vast/ishi/geom/index.sqlite "select count(*) from geom where k >= 'un:' and k < 'un;'"` | **247** — S2 is done. Anything less and the retile repeats the §2 failure on the country boundaries |
| **S5** | `gn`/`wd` staged trees restored (S4) **or** `TILE_ES_DOC_NAMESPACES=gn,wd` exported | either |
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
2. Delete any `un.bin` / `un.index.json` from the geom staging dir — `GeomStoreWriter` opens `"ab"` and would double every entry. **And check what else is in there**: `consolidate_geom_store` merges *every* `*.index.json` in `GEOM_STORE_STAGING_DIR`, not only the namespace you are working on. It currently holds `ukhc_counties.bin` + `.index.json` (92 entries, 55 MB) left by the 20 August names refresh; those keys are already in the store, so re-merging them rewrites them into a fresh shard for nothing and muddies "what did this merge add". Move them aside for the run and put them back after (found by S2, 31 Aug).
3. Re-extract `un`, then `geom_store --merge --keep-staging`.
4. `es gateway-restart` so the gateway re-reads the store index.

**Verify (all three, not one):**
* the run's own counters print `from_geoboundaries=229, from_bnda=18` — anything else means the checkout wasn't read. ⚠️ **This plan originally asserted those counters were printed; they were only tracked.** `stage_un_countries` accumulated both in `stats` and the COMPLETE block printed everything except them, so the check as first written could not be read off a run. S2 added the print, and a hard guard: staging now exits non-zero when `load_geoboundaries_geoms` returns `{}` unless `WHG_ALLOW_BNDA_ONLY=1` is set by name. The silent fallback is now impossible rather than merely warned about;
* `select count(*) from geom where k >= 'un:' and k < 'un;'` returns **247**;
* a sample of stored polygons matches the live index's `bounds` for the same `geom_ref` — a mis-keyed rebuild still "resolves" every key, which is why the 9 Aug verification counted bounds mismatches rather than lookups.

**Note this is not urgent for search.** `un`'s absence from the store is invisible
today because `resolve_region` borrows a `sameAs` co-referent's polygon —
`contained_in: ["un:fra"]` scopes correctly right now (audit §2b). It is urgent
only as a Phase 3 precondition.

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

Worth recording because it cuts against the argument for the map: **both id edge
cases are empty in today's corpus**, so the rule is momentarily reproducible in
SQL. That is an accident of the current data, not a property of the rule — the
duplicate case still turns on stream order the moment one contributor re-uploads
a dataset with a repeated `src_id`.

**Remaining in this step:** geom merge (serialised behind S2, who owns the store
this session — my staging is at `/vast/ishi/geom/staging_whg_s3`, deliberately
outside the directory `consolidate_geom_store` scans) → h3 chain → `final/` →
`index_namespace --replace` on pitt → Symphonym backfill for new toponyms →
overlay rebuild → registry push from CRC (the token is unreadable from pitt).

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

### 2.5 Restore the `gn` / `wd` staged trees  — **S4**

Collateral from the `unittest discover` accident: `staged/gn` is 6.5 KB and
`staged/wd` 14 KB — stubs. Staging is the pipeline's canonical input, so the next
rebuild regresses both without this.

* `wd` — a re-run extract already exists at `/vast/ishi/staged_geomrebuild/wd`
  (9.7 GB); **promote it** rather than re-run.
* `gn` — needs a fresh extract.

**Verify:** both trees' `extract/places.jsonl` doc counts match the live index's
per-namespace counts (`gn` ~11.6 M after alt names, `wd` 11,459,393), not merely
that the files are large.

### 2.6 Re-run toponyms stage 1 for `ipa` / `panphon_features`  — **S4**

`toponyms-temporal-20260731T160000Z.db` (39 GB) has neither: stage 1 timed out at
its 12 h wall. Nothing in the search stack reads them — **the next Symphonym
training run does**, and it is the only consumer, so this is scheduled here
rather than treated as an outage. Allow ~9 h and raise the wall.

**Verify:** `COUNT(*) == COUNT(ipa) == COUNT(panphon_features)` on the vocabulary
table. (Sibling precedent: 93% of Symphonym embeddings once carried a null
`doc_id` while every stage logged success.)

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
2. **`TILE_ES_DOC_NAMESPACES=gn,wd`** — their staged trees are stubs until 2.5;
   `71bcc39` lets the builder read those two from the places index instead.
3. The tileserver is at **83% / 8.5 GB free**, and the last push briefly hit 99%
   because `tiler.service` held descriptors on the old inodes. **Restart the
   tileserver promptly after the push**, and watch the disk during it.

**Verify — the check that would have caught this in the first place:** assert a
non-zero `poly=` count per polygon-bearing bucket in the job log *and* a non-zero
polygon count in the built tileset, before deploying. A tile job that reports
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
| 4.5 | AAT coverage 4,436 / 15,448 = 28.7% (place#142). |
| 4.6 | Legacy per-dataset contributed tilesets (`whg-*.mbtiles`, 23 July) awaiting migration — `plan-outstanding-2026-07.md` §8. |
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

Only **S2** gates the retile — S4 does too, but only softly (`gn`/`wd` can be
read from the index with `TILE_ES_DOC_NAMESPACES` instead). S1, S2, S3 and S4 are
otherwise mutually independent and may run concurrently.

Everything in Phase 2 is ordered ahead of Phase 3 by decision (3), not by
dependency — so if the Atlas Beta needs its boundaries back sooner, **S2 → S5
alone is a valid short path**, leaving S1, S3 and S4 to follow.
