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
| **S1** | 1.1, 1.2, 2.2, 2.4 | Two deletions plus two small code-and-test fixes. No infrastructure, no long jobs — good use of the wait while something else runs. | ⬜ |
| **S2** | 2.1 | `un` extract → geom-store merge → gateway restart. One Slurm job, three independent verifications. **Gates S5.** | ⬜ |
| **S3** | 2.3 | The big one: id-map code change, re-ingest, geom merge, overlay rebuild, registry push. Deserves a session to itself. | ⬜ |
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
| 1.1 | `rm -rf /ix1/ishi/DELETABLE-AFTER-2026-08-31--geom-broken` (**57 GB**) — SG-approved. It is unreadable (index truncated to 2 rows, shards keyless), so it is insurance in name only | `/ix1` free space rises ~57 GB |
| 1.2 | `rm -rf /vast/ishi/geom_rebuild/staging /vast/ishi/geom_rebuild/staging_pending` (**22 GB**) — redundant since the 9 Aug merge | live store still reports 11,758,768 rows |

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
2. Delete any `un.bin` / `un.index.json` from the geom staging dir — `GeomStoreWriter` opens `"ab"` and would double every entry.
3. Re-extract `un`, then `geom_store --merge --keep-staging`.
4. `es gateway-restart` so the gateway re-reads the store index.

**Verify (all three, not one):**
* the run's own counters print `from_geoboundaries=229, from_bnda=18` — anything else means the checkout wasn't read;
* `select count(*) from geom where k >= 'un:' and k < 'un;'` returns **247**;
* a sample of stored polygons matches the live index's `bounds` for the same `geom_ref` — a mis-keyed rebuild still "resolves" every key, which is why the 9 Aug verification counted bounds mismatches rather than lookups.

**Note this is not urgent for search.** `un`'s absence from the store is invisible
today because `resolve_region` borrows a `sameAs` co-referent's polygon —
`contained_in: ["un:fra"]` scopes correctly right now (audit §2b). It is urgent
only as a Phase 3 precondition.

### 2.2 Gateway — an unresolvable scope must fail closed (audit §2b)  — **S1**

`contained_in: ["un:not_a_real_place"]` currently returns Paris in Turkey and
Gabon with `scope: null`. Two fixes in `search.py` / `reconcile.py`:
fail closed when `resolve_region` yields nothing for *every* requested id, and
populate the `scope` object (it is `null` even on the successful path).

**Verify:** the bad-id request returns 0 hits and a `scope` recording why; the
`un:fra` request still returns its 1 FR hit, with `scope` populated. Add both as
test cases — this is a silent-wrong-answer class and deserves a regression pin.

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
| 4.1 | **`og` geometry is at its sources' ceiling, not broken** — 251 of 6,260, because ofs attests only 1,123 of og's admin units and **no og doc carries a wd link at all**. Raising it is a reconciliation task (establish og↔wd links), not a pipeline fix. Its 3.9% ccode coverage follows from this and needs no separate work. |
| 4.2 | 1,497 areal + 1,072 linear docs with `has_geom=false`, and 1 areal doc with no `h3_cover` — 0.005% of the corpus, the standing incomplete-ingestion predicate. |
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
