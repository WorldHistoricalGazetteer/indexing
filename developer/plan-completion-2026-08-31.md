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
> **Write the test against the CALLER, not against the thing you just built.**
> S9's first `og` test called `enrich_geometry` directly with a hand-written
> `geom_key` and claimed to fail pre-change. It could not: supplying your own key
> exercises the *helper*, not `og`. It passed at HEAD. Driving `process_row`
> instead fails at HEAD with `has_geom False` through og's own call path. Same
> family as the presence-vs-position trap, one level up — and only the known-bad
> run exposed it, which is the third time in a day that rule caught something in
> its author's own work rather than someone else's.
>
> **Compare SETS, not counts — a wrong answer can be the right size.** `limuw`'s
> stored cover is 55 cells and a fresh recompute is also 55 — **a different 55**.
> S8 read stored-vs-prod count agreement as "covers look sound" and it was wrong;
> S9 notes its own `stored 278 vs fresh 376` reasoning would have failed silently
> on a `limuw`-shaped case. Cardinality agreement is not set agreement, and
> wherever this campaign compares artefacts it should compare membership.
>
> **`squeue` cannot tell you a job never started.** It lists pending and running
> only, so an empty queue is equally consistent with *never submitted* and with
> *already completed* — and a `df` that has not moved is equally consistent with
> both when the namespace is 5 MB. `sacct` discriminates; `squeue` cannot. I
> reported "nothing has started" to SG as fact from those two measures on 1 Sep,
> while `nl`'s h3 stage had already completed (job 11097899_0, exit 0:0, 2:29).
> Caught by S8. The rule below, from the person who wrote it into this document.
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
| **S4** | **2.5, then 2.6** (order corrected 31 Aug — 2.5 is 2.6's input) | Restore the `gn`/`wd`/`nl` staged trees, then the ~9 h vocabulary rebuild that reads them. Not an ES job. | ✅ **2.5 COMPLETE & VERIFIED** 31 Aug (S4 closed; verified from `indexing-5e`). 2.6 ⬜ not started |
| **S9** | 2.8 | All four priority-chain writers made atomic behind one helper, plus tests that fail on the pre-change code. | ✅ **DONE** (`554e43a` + `e37c93b`) — `atomic_staged_snapshot` called in `update_merge`, `boundary_merge`, `h3_merge` and `ccode_merge` (×3 each; `open("w")` count **0** in all four — verified 1 Sep); parquet renamed first then jsonl; cleanup wrapped so a failing unlink can never mask the failure that caused it. Verified here: 17/17 pass, all four call sites on the helper |
| **Auditor** | document audit (not a plan step) | **Read the plan COLD and find every claim superseded by a later one that is not marked as such.** Read-only: no code, no plan writes — reports findings to `indexing-5e`, which makes the edits. Assigned by SG, 1 Sep. | ◐ **running** (`indexing-13`) — reading at a pinned SHA; batching findings highest-risk first |
| **S9** (cont.) | 2.10 | **Diagnose the ccode H3 prefilter** — why small islands whose country polygon contains them are dropped before any polygon test. Code-reading, no staged writes. **Gates 2.7's `gn`/`wd`.** ⚠️ ~~Resolve the mainland-control contradiction first~~ — **ANSWERED**; see §2.10. Questions 2 and 3 only. | ◐ **assigned by SG, 1 Sep** |
| **S9** (cont.) | 2.9 | Code-only residuals. | ✅ **DONE** — `a4ada2d` ccode preload (+ position-asserting test), `dbf789f` ukhc backfill (read fix **and** the silent-zero report), `1179664` `_unlink_quietly` narrowness pin, `4b1f8ca` og `geom_key` **and** writer, `3225fc6` symlink spec. Verified here. ⚠️ Resolver hoist still withheld behind 2.7 |
| **S8** | 2.7 + the `un` recompute | Give `gn`/`wd`/`nl` a real `final/`; **and recompute `un`'s cover** (SG, 1 Sep). **Gates S5 and the overlay publish.** | ◐ **RUNNING — SG ruled 1 Sep:** S8 takes the `un` recompute, and `update_merge` on **`wd` first**, then `gn` |
| **S5** | 3.1, **plus the 47 `whg-*` buckets** (4.6) | The retile. Prove the verifier FAILS on the preserved fixtures before deploying. ⚠️ Its post-2.7 eligibility re-check is **necessary but not sufficient** — `final/` existing cannot show whether `gn`/`wd`'s update patch landed, because that is a **name** count, not a document count (see 2.7). | ⬜ **BLOCKED on 2.7 (S8)** |
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

### 🔥 LIVE INFRASTRUCTURE HAZARD — `/ix1` is wedged on some CRC clients (31 Aug)

**Measured, not inferred** (S3, confirmed independently here): `/ix1` `statfs` and
`ls` time out from **`htc-n56`** and the **`crc1` login node**, and are **healthy
from `crc2` and `pitt`**. `/vast`, `/ihome` and `/software` are fine everywhere
checked. The filer is alive; specific NFS clients have hung. **Not waitable-out on
a given node — move nodes.**

⚠️ **`/ix1` is mounted `hard`, so a blocked process waits forever rather than
erroring.** Job 11091158 sat in state `D` in `folio_wait_bit` with **CPU time
frozen at `00:08:30`** while elapsed passed 26 minutes. `squeue` said RUNNING. Its
log looked fine. It would have burned a 20 h wall producing nothing.

**A `hard`-mounted NFS hang is invisible to every status signal this campaign
trusts.** What *did* discriminate: **CPU time and syscall counters frozen across
samples** (`rchar`/`wchar`/`syscr`/`syscw` byte-identical over 40 s). File size did
not — it plateaus normally during SQLite batching. Nor did `squeue`, nor the job's
own log. Put it beside "a tile job that reports success is not evidence it read any
geometry": **a job in state RUNNING is not evidence it is running.**

Mitigations in force: `--exclude=htc-n56`, and build on `/vast` not `/ix1` — the
`/ix1` build took 13 min to reach 236 MB, the `/vast` build did 41.9 → 58.5 MB in
**25 s**. `submit_hardlinks_slurm` defaults `db_path` to `IX1_BASE` for no reason:
`publish_local` does a `shutil.copyfile`, so it publishes fine across filesystems.
**Production is unaffected** — the gateway reads `/ix1/ishi/hardlinks` from `pitt`,
which is healthy.

---

### 📎 Citation convention — cite `module.symbol`, treat the line number as a hint

**Measured, not asserted** (Auditor batch 3, 1 Sep): of 38 `file:line` citations in
this plan, **26 resolved and 12 had drifted** under 2.8's and 2.9's commits. The
pattern in which survived is clean:

> **Every citation that survived carries a symbol name. Every one that broke is a
> bare line number on moving code.**

`h3_merge._source_dir:88` survived and `h3_stage._extract_stage_dir:108` did not —
same commit, same kind of code — because the name still located the first when its
number drifted. `staging_contract:194`, `repair_staged_docs.py:247` and
`settings.py:158` all survived because they name a constant grep finds regardless.

**So: write `module.symbol` and treat `:NNN` as a hint, not an address.** This is
the past-tense rule from batch 2 in another register — *a citation should stay
findable after the thing it points at moves, exactly as a finished step should
stay readable after the work is done.*

⚠️ **And line numbers here refer to `main`.** Jobs run from `/vast/ishi/elastic`,
which lags `main` by however long since the last pull — so a session verifying a
citation *there* may find something different again and conclude the document is
wrong when the clone is merely stale.

---

### 📋 The Auditor's brief (SG, 1 Sep) — auditing the DOCUMENT, not the agents

**Why this exists.** This plan is a **chronological** record of a campaign in
flight: ~50 commits today, ~3,100 lines, 18 supersession markers, and several
corrections layered on corrections. **I wrote the superseded parts, so I am the
worst-placed reader to find them** — and a successor quoting a retracted figure is
the live risk in a document this size.

**Scope — find, do not fix:**

1. **Claims superseded by a later claim but not marked as such.** The danger is a
   confident earlier statement still reading as current.
2. **The same figure appearing in two places with different values.**
3. **Internal contradictions** between sections written hours apart.

**Known-dangerous areas, because each was revised at least once:** the blast-radius
table (**three** versions, two superseded); the `update_merge` memory figures
(16 G / 48 G / 64 G / 128 G all appear, and all four are correct *somewhere*); the
`h3_cover` informative threshold (≈100 cells, later corrected to ≥400); "`osm` and
`ohm` are clean" (**retracted**); the `nl` scope-region claim (**struck**); the
`un` cover attribution (corrected from 2.1's window to my own chain, a timezone
error); and the tier-2 prediction (**refuted** by measurement).

⚠️ **Do NOT flag deliberately-kept records as inconsistencies.** Several rows are
retained *because* they were wrong — struck, refuted or retracted **and labelled**
— so a successor does not re-derive them or "fix" a defect that does not exist.
The distinction to report is **superseded-and-unmarked**, never
superseded-and-marked.

**Method:** quote section headings and exact lines rather than paraphrasing, since
the point is what a cold reader would take from the text. Read-only — no code, no
plan writes. Report to `indexing-5e`, which owns the document and makes the edits.

---

### Running sessions concurrently — read before starting more than one

⚠️ **STALE SCHEDULING, corrected 1 Sep (Auditor F16).** This read *"safe to start
together: S1, S2, S3 … do not start S5 until S2 is verified"*. S1–S4 are **closed**
and S2 **is** verified, so the only condition it placed on S5 is satisfied — the
same false green light as the Critical path carried. **S5 is blocked on 2.7**, and
2.7 on the `un` recompute. The live sessions are S8 (2.7 + `un`) and the Auditor. The dependency graph
above is necessary but not sufficient — these sessions also share three
resources it does not model.

| hazard | who collides | rule |
|---|---|---|
| **Production ES load** | S3 (delete-by-query + re-index of 229 k docs, plus the toponym augment) and S5 (corpus-wide streaming for tiles). ⚠️ **S4's 2.6 is NOT one of them** — corrected 31 Aug: it runs `--skip-es-index` and never consults ES, so it neither contends here nor needs a restart. The hold placed on S4 behind S3 was unnecessary, though harmless and correct on the plan's word at the time | **One heavy ES job at a time.** Heap saturation from heavy indexing has already taken faceted `/api/search` to 500s once, and `dense_vector` merges on the toponyms index are the known OOM driver. Restart ES after any of them. |
| **Gateway restarts** | S1 (2.2 needs one to deploy the scope fix) and S2 (2.1 ends with one) | **One restart owner.** Whichever finishes second performs the restart; the other says in its notes that its change is on disk but not yet loaded. A restart mid-test silently invalidates the other session's verification. |
| **Staged manifest writes** | S2, S3, S4 all write `staged/<ns>` and the run manifest — **and so does S5's retile, which is not read-only** (S5, 31 Aug, verified). `generate_tiles_from_staged` calls `update_namespace_stage_status(manifest_path, ns, "tiles", …)` at `:1858` and `:2144` for **every contributing namespace** — all 27 on a full run — plus `write_stage_event` / `write_runtime_history_event`. Two sessions independently assumed "the retile is an output stage, therefore it only reads the corpus"; only reading the code settled it. **Scope of the write is the per-namespace `tiles` stage key and appended events only** — never `final/`, `h3_merged/`, `boundary_merged/`, `extract/` or any `places.parquet` — so a concurrent parquet *read* (the overlay harvest) is safe. Both writes are guarded on `manifest_path.exists()` and a non-empty `run_id`, so a retile invoked without them touches nothing | The `/vast` lock is `O_CREAT|O_EXCL` with **proceed-with-warning on timeout**, because `flock` returns `ENOLCK` there under fan-out. So two sessions *can* both proceed. ⚠️ **"Keep concurrent sessions on different namespaces" is no longer sufficient, and stopped being true on 31 Aug.** S8's 2.7 *writes* `final/` for `gn`/`wd`/`nl` while the overlay harvest *reads* those same trees — the first time two sessions have wanted the same namespaces rather than merely the same manifest. ⚠️ **UPDATED 1 Sep — 2.8 HAS LANDED (`554e43a` + `e37c93b`), so the zero-byte
window described below is GONE.** All four writers publish via
`atomic_staged_snapshot`. **Serialisation is now belt-and-braces rather than
load-bearing — but do NOT drop it**: two things survive 2.8. A brief
**complete-but-mismatched pair** exists between the two renames (new parquet
beside old jsonl), and `write_parquet_from_jsonl` **strips hull**, so the pair
genuinely disagrees for hull-consumers whichever rename order is used. The
historical description follows. ~~**Read-versus-write is the collision, and
`ccode_merge` is not atomic**: `:181` opens `final/places.jsonl` with mode `"w"` and derives the parquet afterwards at `:201`, with no temp-and-rename. Every resolver prefers `final/` over `extract/`, so a reader that resolves inside that window silently reads a partial file where a complete one existed. `wd` is 98.9% of the overlay and `gn` 67%. **Serialise: no session may write a namespace's staged tree while another is reading it.**~~ |
| **Degraded staged trees** ⚠️ NEW 31 Aug | S3's overlay rebuild, S4's 2.6, and any future rebuild | `hard_links_staged.py` and toponyms stage 1 both read `staged/<ns>/final/places.parquet` through the stage chain, and `gn`/`wd` are **one row each** with `nl` absent. **98.9% of the overlay's 7,596,959 rows touch `wd` and 67.0% touch `gn`**, so a harvest today replaces the gateway's live co-reference store with a fraction of one. **Both now depend on 2.5.** The published overlay is intact only because it was built on 6 Aug, the day before the accident |
| **The pitt VM** | any step running inline rather than via Slurm | Heavy work goes to Slurm. Several steps do small inline work on pitt; eight parallel inline resolvers once OOM-thrashed the VM into a ~1 h production outage. Two or three sessions doing "just a little" inline work is that same pattern. |

### ⚠️ Before ANY session runs pipeline code: `git pull` the CRC clone, and fetch first

⚠️ **Corrected by S1 at closing:** this is **one clone shared by pitt and CRC**,
not a CRC clone distinct from a pitt one — same inode `474440114616486586` from
both hosts, verified. The distinction matters because **the relay's
`gateway-restart` does `git pull --ff-only`**, so gateway code is refreshed
whenever anyone restarts, and **the hazard is entirely Slurm-side: a job launched
from that clone runs whatever the last restart happened to pull.** The two real
clones are the workstation checkout and this shared one.

`/vast/ishi/elastic` is what every Slurm job runs from, and **it lies about being
up to date**. Measured 31 Aug: it sits at `177ba72`, **22 commits behind
`main`** — including `1f5aa50` and `42b6e4a`, both real fixes to the hard-link
job — while `git rev-list --count HEAD..origin/main` there returns **0**, because
its remote-tracking ref is only as fresh as its last fetch. A session that checks
whether it is current, without fetching, gets a reassuring zero.

⚠️ **The command this plan first prescribed CANNOT WORK, and it fails in exactly
the way the paragraph above warns about** (S8, 31 Aug; verified here). `origin` is
an SSH remote (`git@github.com:…`) and `stg135` has no registered key on pitt, so
`git fetch origin` dies with `Permission denied (publickey)` — and
`git rev-list --count HEAD..origin/main` then returns **0** off the untouched
stale ref. The reassuring zero, produced by the command written to prevent it.

Use the HTTPS refspec — the repo is public and reachable from pitt. Verified
working: it moved the tracking ref and the clone then honestly reported
`[behind 5]`.

```bash
ssh pitt 'cd /vast/ishi/elastic && \
  git fetch https://github.com/WorldHistoricalGazetteer/indexing.git \
    "+refs/heads/main:refs/remotes/origin/main" && git status -sb'
# then, ONLY when no Slurm job is running from the clone:
ssh pitt 'cd /vast/ishi/elastic && git merge --ff-only origin/main'
```

**Fetch is safe at any time** — it touches refs, not the working tree. **The merge
is not**: Slurm jobs execute the working tree, so a fast-forward under a running
job swaps code beneath it. Check for running jobs first.

⚠️ **`crc0` is stalling, live as of 31 Aug.** Read-only `squeue` / `sacct` / `ls`
calls to it hang past 120 s with zero output, while `crc1` answers the identical
command in under a second — observed independently by `indexing-5e` and S5. Fall
back through `crc1` → `crc2` → `crc3`; **a hang there is the login node, not a
busy cluster**, and reading it as load will send you to the wrong conclusion.

S3 hit this inside 2.3 — the clone lagged by exactly the three commits that fixed
the job it was about to run — and recorded it in that runbook. It is not a 2.3
problem: it is the precondition for **every** step that executes code on CRC,
which is most of what is left.

✅ **The hold is LIFTED (Auditor F15).** It read: *"not right now — `gn`'s extract
(Slurm 11074352) is running from that clone"*. **That job COMPLETED**, and the
stale hold sat at the end of the section every session must read *before* running
pipeline code, contradicting the section's own instruction. **Pull, and still not
under a running job** — check `squeue` first.

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
| **S5** | ❌ **WITHDRAWN — does not work.** `TILE_ES_DOC_NAMESPACES=gn,wd` was offered here as an escape hatch; `submit_tiles_slurm` **never consults it** (§3.1), so it cannot make an ineligible bucket eligible. Kept struck so nobody re-offers it. **Costs a ~24.5 M-document scan of production ES** and leaves the staged trees still wrong for the next consumer. Use only if the Beta genuinely cannot wait for `gn`'s extract | not the default |
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
step 3.3 (Auditor F21 — this read 3.5, which does not exist; 3.3 already lists both).

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

~~### ✅ CLOSED 31 Aug — `staged/un` now has `final/` (S2 raised it; run after S2 closed)

Before 2.1 `staged/un` did not exist; it now exists holding **extract only**. Both
`index_from_stage` and `index_namespace` fall back through
`("final", "ccode_merged", "h3_merged", "extract")`, so a full rebuild would reach
for `extract` and try to index `un` geometries **with no `h3_cover` and no
ccodes**. `index_namespace` guards against exactly that, so the likely outcome is
a confusing mid-rebuild refusal rather than bad data — but *a directory that looks
like data and is not* is this campaign's whole subject, and CLAUDE.md's Fault 12
note says `un` must come through `ccode_merge` to `final` precisely so this cannot
arise.

Two closes, either a few minutes:

1. **Exactly reversible** — `rm -rf /vast/ishi/staged/un/extract`, restoring the
   pre-2.1 state. The geometry is already in the store and verified, the live
   index needs nothing from it, and it regenerates in ~20 minutes.
2. **Correct** — run `h3_stage` → `h3_merge` → `ccode_merge` (pass-through) so
   `final/` exists and is consistent with the store, which also recomputes `un`'s
   h3 from the real geoBoundaries polygons rather than a hull.

**SG took (2), run as Slurm 11075438 after S2 had closed** (`un-final-chain`,
COMPLETED in ~75 s, run id `un-final-20260831T145706Z`). The chain went
`h3_stage` (247 docs, 247 geometries with h3) → `h3_merge` (247 written, 0
patches unmatched) → `ccode_merge` with `allow_missing_patch=True`, whose
pass-through printed `no patch … — ccodes copied through unchanged` and wrote
both `final/places.jsonl` and `final/places.parquet`.

Verified independently of the job's own report, using the pipeline's **own**
resolver rather than a reimplementation of it:

```
_staged_namespace_source("un")  →  /vast/ishi/staged/un/final/places.parquet
final/ docs 247   with ccodes 247   geometries 247   with h3_cover 247
```

That is the whole point of the fix: before the chain the resolver returned
`extract/places.jsonl`, so a rebuild would have reached for un-enriched
geometries. It now returns `final/`. Two notes for whoever runs this chain next:
the code is identical between `main` and the shared `/vast/ishi/elastic` clone,
so no pull was needed under the running `gn` job (checked, not assumed); and
pitt's *system* python has no `pyarrow` — use
`/home/gazetteer/miniconda/envs/whg/bin/python` for anything that imports the
staged-parquet path.

**Original recommendation, kept for the record: (2), because a rebuild is coming.** `un` is the namespace that
supplies `contained_in` regions, 2.5 is currently making the other three staged
trees whole, and the reconciliation identity wants all 27 complete rather than 26
plus one that merely counts. Take (1) only if no rebuild is in prospect. What
must not happen is leaving it undecided, because the next reader sees a populated
`staged/un` and reasonably assumes it is finished.

**Note this is not urgent for search.**~~ ⚠️ **Wrong — corrected by S2,
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
* ✅ **RESOLVED by 2.3 — the SQL mint is GONE** (Auditor C1): `:115` is now an unrelated comment, and the only surviving trace is a docstring at `contributor_replay:70` describing the mint as the thing it replaced. The hazard below was live when written and is not now. ~~**`clustering/harvest/contributor_replay.py:115` mints the same id
  independently in SQL** — `('whg:' || d.id || ':' || pl.place_id)` — and the
  overlay holds **2,933 + 26,971 `whg:` rows**. Re-ingesting without changing
  that query points ~30 k hard-link edges at ids that no longer exist.~~

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

#### Rebuilding the overlay — the runbook, and the number that gates it

### 🛑 THE OVERLAY REBUILD ALSO DEPENDS ON 2.7 — `gn`'s relations are only in its patch

**Found by S3 (`indexing-7e`), 31 Aug. Confirmed here structurally, which is
stronger than its own empirical proof.** `authorities/geonames-places.py`
**never emits relations at all** — the grep is empty. `geonames-toponyms.py`
writes them exclusively as `relations_to_add` into the update patch (`:134-144`).
So `gn`'s identity relations cannot be in `extract/` by construction, and every
`gn` edge in the live overlay must have come from the patch.

S3's measurement agrees to the unit: 1,111,147 `sameAs` in `gn/update_patch`,
1,111,147 overlay rows asserted by `gn`, and **0** relations of any kind across
`extract/`'s 13,454,817 lines.

**So a harvest run today prints `gn: attempted=0` again** — same symptom as the
31 Aug near-miss, **different cause**. Then the tree was a one-row stub; now it is
complete and correct and the relations were never in it. **2.5 was verified by
document counts, and `gn` reconciles exactly — 13,454,817, delta 0 — while
contributing nothing the overlay needs.** A namespace can reconcile perfectly on
doc count and still be empty of the thing a given consumer reads.

**The 🛑 gate will therefore fail, correctly**, at roughly
7,596,959 − 1,111,147 (`gn`) − ~24,943 (the whg id-map drop, which is the fix
working) ≈ **6.46 M against a 7.6 M threshold, ~15% short**. That is the gate
doing its job and the "something *else* is wrong — stop and investigate" case
firing as designed. It has been investigated; this is the something else.

**Sequence (S3's, and it is clean — not a deadlock):**

1. the current harvest finishes **as a measurement**, publishing nothing — it also
   exercises the never-run LOC and publish-path prerequisites, which is worth
   having on its own;
2. S3 releases S8;
3. 2.7 runs `update_merge` → `h3_stage` → `h3_merge` → `ccode_merge`;
4. **re-harvest** from the real `final/` trees;
5. *then* the gate, and only then a publish decision.

**The overlay publish is not completable until 2.7 lands.**

⚠️ **Correction to this section's own arithmetic, and the error is mine.** The
figures "`wd` accounts for 7,516,092 and `gn` for 5,092,751" are
**endpoint-touching** counts — rows where the namespace appears at *either* end —
which is why they sum past the total. They are correct as measured and wrong for
the purpose I used them for. The **asserting-source** breakdown is what predicts
what a harvest produces. **S3 measured it; I verified it by summation rather than
by re-scanning** — the ten categories partition to **exactly 7,596,959**, the
total I had measured independently, with zero remainder. That is strong evidence
the partition is complete and correctly attributed, since a wrong or partial
measurement would be unlikely to land on a known total to the row, and it
decisively separates this framing from the endpoint one, which *over*-sums by
construction. It would not catch a systematic swap between two categories that
preserved the sum, so it is weaker than an independent scan — two of which I
started, both killed on a 7.6 M-row group-by over NFS. Not worth a third. The
breakdown:
`wd` 3,968,404 (52.2%), `osm` 2,295,659 (30.2%), `gn` 1,111,147 (14.6%), `ohm`
98,569, `iv` 68,935, `tm` 25,665, `loc` 1,129, `clio` 248, `og` 222, plus 26,981
contributor rows. **"`gn` is 67% of the overlay" and "`gn` asserted 14.6% of it"
are both true and lead to different predictions** — say which you mean.

> ### 🛑 The publish gate
>
> **The rebuilt overlay must come out within a few percent of `7,596,959` rows.
> Below that, STOP — do not publish, investigate.**
>
> `publish_hardlinks` computes `row_count` from the *new* database and **never
> opens the incumbent**, so nothing downstream will object. This is the only
> check standing between a degraded harvest and the store the gateway reads on
> every `include_hard_links` request.
>
> ⚠️ **CORRECTED (Auditor F3): those two are ENDPOINT-TOUCHING counts** — the
> wrong measure for predicting a harvest, as this section says above and below.
> The asserting-source figures are `wd` **3,968,404** (52.2%) and `gn`
> **1,111,147** (14.6%); reason with those. The "something else is wrong" case
> this gate describes is also now **closed**: the harvest came in at 6,460,869 —
> exactly 1,111,147 + 24,943 short, nothing unexplained.
>
> ~~Reason with it rather than just comparing: `wd` accounts for 7,516,092 of
> those rows and `gn` for 5,092,751.~~ So if 2.5 has genuinely restored both,
> an overlay materially below 7.6 M means something *else* is wrong — a third
> namespace, a stage-chain resolution, a silent harvest failure — and the right
> move is to stop rather than publish and investigate afterwards.

Today's near-miss cost nothing for two reasons, and a successor will not know to
keep either of them:

1. **Build and publish were kept as two decisions.** `submit_hardlinks_slurm` was
   invoked deliberately **without** `--pitt-user/--pitt-host/--pitt-dir` and
   **without** `--publish-local`, so the job builds a database and stops.
   `processing.publish_hardlinks --execute` is a separate, later command. Keep it
   that way — a single fused invocation would have shipped the degraded overlay
   before anyone could look at it.
2. **The harvest printed `gn: attempted=0` as it went**, and a human read it.
   That is not a guard, it is luck with good logging.

The rest of the runbook:

* `git pull` in `/vast/ishi/elastic` **first, and confirm it landed.** That clone
  is what `submit_hardlinks_slurm` runs from, and as of 31 Aug it lagged `main`
  by exactly the three commits that fix this job (`42b6e4a`, `1f5aa50`, and the
  `--id-map` wiring). The pre-fix version hands a 6-12 h harvest to node roulette
  and mislabels the drop counts.
* The id-map change needs no flag — `submit_hardlinks_slurm` passes `--id-map`
  automatically. The map is at `staged/whg/extract/id_map.jsonl`, 228,918 records
  stamped `whg-idmap-20260831T071935Z`; check that stamp rather than assuming the
  file is the one you mean.
* **Expect the contributor layer at roughly 2,038 rows, not 26,981.** That drop
  is the fix working, not a loss — see above. It is the one number that *should*
  fall.
* `wd` and `gn` now resolve to `extract/places.jsonl` with no parquet, so Phase 1A
  streams a 10.3 GB file. A long Phase 1A is expected, not alarming.

The guard this all argues for, not built here: **compare a rebuilt overlay's row
count and per-namespace endpoint counts against the overlay it is about to
replace, and refuse an unexplained shrink** — the sibling of the geom-store guard
in `adc7345`.

##### What this runbook has NOT been exercised on

Written by S3, who is the worst-placed person to notice what it assumes. Read
this before trusting the procedure to be complete.

* **`--no-enforce-barrier` is required, and it looks alarming.**
  `submit_hardlinks_slurm` runs `check_global_barrier` and **exits 1** unless
  every namespace in the manifest is complete. Any fresh run id fails that — 26
  of 27 namespaces are incomplete — so the flag is correct here and was used on
  31 Aug. Do **not** "fix" the barrier by marking stages complete to get past it;
  that would falsify the state the rest of the campaign reads.
* **The LOC phase and the entire publish path are unvalidated as of 31 Aug.** The
  harvest was cancelled *during* Phase 1A, so `loc_links` (a 4.5 GB gzip),
  `finalise_local`, `publish_hardlinks`, the ship marker and the live-delta prune
  were never reached. Only `contributor_replay` was validated, in isolation. The
  runbook above should not be read as an end-to-end rehearsal.
* **Phase 1A's duration is unmeasured for the restored corpus.** The cancelled
  run did `osm` + `ohm` in ~13 minutes with `gn`/`wd` contributing nothing. With
  both real — and `wd` streaming a 10.3 GB JSONL because it has no parquet —
  Phase 1A will be substantially longer. No estimate exists; size the wall
  generously rather than from the 6 August history.
* **The renamed drop-ledger fields have never actually run.** The 31 Aug
  validation ran from the CRC clone at `177ba72`, i.e. *before* `1f5aa50`, so its
  output used the old key `"total": 25997`. After the mandatory `git pull` the
  same numbers appear as `rows_dropped` / `unresolved_endpoint_refs`. Unit tests
  cover the rename; no CRC run has.
* **DO Postgres access worked without configuration and is therefore
  undiagnosed.** `contributor_replay` reached `whgv3beta` from an htc compute node
  via `clustering/pg_client.py` (asyncpg + an SSH tunnel) with nothing set by
  hand. If it fails for you, S3 has no diagnostic to offer — that path was never
  troubleshot.
* **Reusing run id `whg-idmap-20260831T071935Z` is probably right but not free.**
  Its manifest already exists with all 27 namespaces selected, which is what
  `hard_links_staged` iterates. But `submit_hardlinks_slurm` derives both
  `--db-path` and the ship marker from the run id, so a rerun overwrites them.
  Creating a *new* run id instead needs a manifest to exist first — and the
  obvious way to make one, `submit_extract_slurm`, **also rotates staged trees**,
  which is emphatically not wanted here.


##### The overlay rebuild — S3 (second session), 31 August 2026  ◐ running

Harvest **Slurm `htc` 11091158**, submitted 21:01 UTC, `htc-n56`, build only.
Nothing published; the live `/ix1/ishi/hardlinks/hard_links.sqlite` is still the
6 August build and has not been opened.

**The mandatory `git pull` does not work as written, and the reason is worse than
staleness.** The documented command fails:

```
$ ssh pitt 'cd /vast/ishi/elastic && git fetch origin'
git@github.com: Permission denied (publickey)
```

`origin` is an **SSH** remote, and the only key on pitt for `stg135`
(`~/.ssh/id_ed25519`) is the **DO tileserver key** — it is not registered with
GitHub. So a `git fetch` as `stg135` has never been able to succeed and cannot;
this is not a ref that went stale and would be cured by fetching again.

That is the actual mechanism behind the trap this plan describes. Watched
directly: the fetch errored, and the **very next command** still reported

```
$ git rev-list --count HEAD..origin/main
0
```

So the reassuring zero is a *failed fetch* being read as currency. `c730d32`
diagnosed the symptom correctly and the cause incorrectly — worth correcting
because "stale ref" implies a fetch would fix it, and no fetch on that path ever
will. It also means the clone was never verifiably current at any point in this
campaign's history except immediately after a gateway restart.

**The working route needs no key, no config change and no gateway restart** — the
repo is public over HTTPS and reachable from pitt:

```bash
git fetch https://github.com/WorldHistoricalGazetteer/indexing.git \
    "+refs/heads/main:refs/remotes/origin/main"
git merge --ff-only origin/main
```

`origin` itself was left untouched: rewriting a remote on shared production
infrastructure is SG's call, not an agent's. **Someone should decide the
permanent fix** (register a key for `stg135`, or point `origin` at HTTPS); until
then every session hits this, and any session that does not check the fetch's
exit status will conclude the clone is current.

The only other update path is the relay's `gateway-restart` —
`scripts/gateway_ctl.sh:131` pulls as `gazetteer`, who *does* have working
credentials. So the shared clone has only ever been refreshed as a side effect of
restarting the gateway, which explains how it drifts so far.

It was **29 commits behind, not 22** (that figure was measured earlier in the
day). Now at `95fd1ae`, 0 behind, with `1f5aa50` and `42b6e4a` verified present
in the working-tree *files*, not merely reachable in the log.

**`42b6e4a` is not theoretical, and it affects a node the plan did not name.**
`import sqlite3` failed with `GLIBCXX_3.4.30 not found` on **login node crc1**
during preflight — not only the compute nodes. The `LD_LIBRARY_PATH=
"$CONDA_PREFIX/lib"` export clears it, and the job's own probe now prints
`sqlite3 ok 3.50.3` in its first second. Any session running repo code that
imports `sqlite3` — including anything using `GeomStoreReader` — should take the
same export on login nodes too.

**⚠️ The wall floor is the same trap as 2.6's, and it fires here.** The generated
sbatch came out at `--time=06:00:00`, which is `_MIN_WALL_SECONDS` — i.e. the
estimator's median was *below* the floor. The reason is measured, not inferred:
the last full build, `whg-hardlinks-h3ccode-20260805T120000Z` on 6 August,
COMPLETED in **00:38:48** — but it read `gn`/`wd` as **parquet**. This run streams
them as **JSONL**. The history is honest about the job that ran and misleading
about the job being run, which is exactly 2.6's `_none_` poisoning wearing
different clothes. **Wall raised by hand to `20:00:00`**, still inside the
`htc-htc-s` ≤1-day tier; over-asking costs backfill priority only. The generated
file is preserved as `hardlinks.sbatch.bak-6h` and the diff against it is the
single `--time` line.

**`gn` and `wd` resolve at *extract* depth, not `final/`.** Measured with the
pipeline's own resolver rather than `ls`:

| ns | resolves to | size |
|---|---|---:|
| `gn` | `extract/places.jsonl` | 7.38 GB |
| `wd` | `extract/places.jsonl` | 10.26 GB |
| `nl` | `extract/places.jsonl` | 0.01 GB |
| everything else sampled | `final/places.parquet` | — |

Harmless for this harvest — `hard_links_staged` walks the chain and reads what it
finds, which is why the wall matters and the depth does not. **But it is not
harmless for S5**, and passing it on turned out to matter more than the sizing
note it was attached to: S5 measured that `submit_tiles_slurm._eligible_buckets`
(218–226) **skips any per-namespace bucket lacking `final/places.*` with a bare
`continue`**, so a 27-bucket retile would silently become 24 — `gn`, `wd` and
`nl` dropped — and report success. Their finding, recorded here because this
step is where the resolution depth was first measured.

**Preflight, all measured before submitting:**

| check | result |
|---|---|
| id map records / run stamp | 228,918 + 1 `_meta`; single run id `whg-idmap-20260831T071935Z` ✓ |
| manifest namespaces | 27, `gn`/`wd`/`nl` all present ✓ |
| target db `hard_links_whg-idmap-…sqlite` | absent ✓ |
| ship marker `…hardlink_ship.json` | absent ✓ |
| LOC source | `names.madsrdf.jsonld.gz`, 4.3 GB ✓ |
| `/ix1` headroom | 1.9 TB free ✓ |

**On the leftover-`-wal` trap: the two files now in `/ix1/ishi/hardlinks/` are NOT
it, and must not be deleted.** `hard_links.sqlite-shm` (32 KB, owned by
`gazetteer`) and `hard_links.sqlite-wal` (0 bytes) sit beside the **live**
overlay and are the gateway's own open WAL connection — the predecessor's trap
was a 16 MB `-wal` beside a *deleted* database under a run id. The distinguishing
test is which database the pair sits beside and who owns it, not merely that a
`-wal` exists. The run-id-derived paths were the ones that needed to be clean,
and they were.

**Build and publish remain two decisions.** Submitted without
`--pitt-user/--pitt-host/--pitt-dir` and without `--publish-local`; the generated
script contains no `ship_to_pitt`, no `publish_hardlinks`, no `prune_live_delta`
— only `finalise_local`, which prints `row_count` and is how the 🛑 gate gets its
number **without** anything being published. `--no-enforce-barrier` was used, as
the runbook requires; no stage was marked complete to satisfy it.

**Next, and not to be skipped:** when the job completes, compare `row_count` and
the per-namespace endpoint counts against the incumbent **before** any publish
decision. The gate is ~7,596,959 rows; below that, stop. Expect the contributor
layer at ~2,038 rows rather than 26,981 — that fall is the id-map fix working.

##### ⛔ 2.5 did NOT unblock this step — `gn` needs `update_merge` first

**Measured 31 Aug by S3, reproduced independently by S8. Three numbers that agree
to the unit:**

```
gn/update_patch identity relations (full scan) : 1,111,147 sameAs
published overlay rows ASSERTED by gn          : 1,111,147
gn/extract relations of ANY kind               :         0   (over 13,454,817 lines)
```

A seven-figure exact match is not coincidence. **Every `gn` edge in the live
overlay came from the update patch**, and `staged/gn/extract/` — which is what
2.5 restored and what `hard_links_staged` resolves to — contains no `relations`
key at all. `gn`'s full patch is 8,125,650 lines, 1,654,555 of them carrying
`relations_to_add`, totalling 1,831,130 relation entries: **1,111,147 `sameAs`**
(identity, consumed by the overlay) + 719,983 `describedBy` (not an identity
type).

So a harvest run today prints `gn: attempted=0`. **Same symptom as the 31 August
near-miss, different cause** — then the tree was a one-row stub; now it is
complete, correct, and never contained the data. The distinction matters because
the first cause was fixed and the second was not even visible.

⚠️ **The lesson, and it is the sharpest form this campaign has produced:
`gn` reconciles EXACTLY on document count — 13,454,817, delta 0 against the live
index — and contributes nothing.** 2.5's verification was sound, honest, and
passed for the right reasons. It counted rows, and what the overlay needs is not
rows. *A doc-count check passes perfectly in the world where the content its
consumer needs is absent.* Any successor verifying a staged tree should assert
the thing that would be missing, not the documents that would still be there.

**Consequence for the 🛑 gate: it fails, correctly.** Expected
≈ 7,596,959 − 1,111,147 (`gn`) − ~24,943 (the whg id-map drop, which is the fix
working) ≈ **6.46 M against a 7.6 M threshold, ~15% short**. That is precisely
the plan's "materially below 7.6 M means something *else* is wrong — stop rather
than publish" case. The something else is now named.

**Ordering, revised.** §2.3's publish is blocked on `update_merge` for `gn`,
which lives in **2.7** (S8). 2.7 is therefore on this step's critical path, not
beside it, and `update_merge` is a **required predecessor of `h3_stage`**, not an
optional one — `submit_h3_slurm:140` falls back to `extract/` silently when
`update_merged/` is absent, which is how the loss arose in the first place.

1. harvest 11092269 finishes — kept as a **measurement**; publishes nothing, and
   exercises the never-run LOC and publish-path prerequisites;
2. S3 releases S8;
3. `update_merge` → `h3_stage` → `h3_merge` → `ccode_merge` for `gn`/`wd`/`nl`;
4. **re-harvest** from the real `final/` trees;
5. gate, then a publish decision.

**Acceptance criteria for 2.7's `gn` half** (they read **zero in the broken world
while the job reports success**, which no document count can do):
`gn/update_merged/` carries 1,831,130 relation entries, of which 1,111,147 are
`sameAs`. `wd`'s half is the disjoint one: +58,657 geoshapes (measured; **58,658** appears elsewhere in this plan and is superseded — Auditor F9), **0 relations**.

⚠️ **`gn` and `wd` are not interchangeable and there is no cheap half.** `gn` is
relations + toponyms with **0 geometry**; `wd` is geometry + h3 with **0
relations**. A `wd`-only 2.7 restores the map and leaves the co-reference graph
14.6% short; a `gn`-only 2.7 does the reverse. Both or neither is the only
defensible split.

**A correction to this section's own gate arithmetic.** §2.3 quotes "`wd`
accounts for 7,516,092 of those rows and `gn` for 5,092,751". Those are
**endpoint-touching** counts — rows where the namespace appears at *either* end —
and they legitimately sum past the total. What predicts a harvest's output is the
**asserting-source** breakdown (`source_id`), which is different:

| source | rows | share |
|---|---:|---:|
| `wd` | 3,968,404 | 52.2% |
| `osm` | 2,295,659 | 30.2% |
| `gn` | 1,111,147 | 14.6% |
| `ohm` | 98,569 | 1.3% |
| `iv` | 68,935 | 0.9% |
| `tm` | 25,665 | 0.3% |
| `loc` | 1,129 | — |
| `clio` / `og` | 248 / 222 | — |
| `contributor:*` (27 ids) | 26,981 | 0.4% |

Both framings are true; they answer different questions. "`gn` is 67% of the
overlay" and "`gn` asserted 14.6% of it" are both correct and predict different
things. Reason about a rebuild with `source_id`.

**Method notes, recorded because both errors were mine and neither was caught by
re-reading code.** The first version of this test sampled a control that came
back **empty** and printed a confident verdict anyway — `if len(control) and …`
short-circuits to `False` on an empty list, skipping the inconclusive branch.
That is *absence of input treated as nothing-to-do*, written into the very check
meant to catch that pattern. The second version matched endpoint pairs in
**either order**, so `wd` asserting the reverse edge would have produced an
identical `400/400` — two different worlds, one output, decorative by this
campaign's own standard. `source_id` answers the question directly and
unambiguously. Both were found by asking *what would the broken world print?*

##### Harvest 11092269 — COMPLETE, and the 🛑 gate correctly REFUSES it

**Slurm `htc` 11092269, COMPLETED in 01:07:16** (20 h wall; the 6 h floor would in
fact have sufficed, which could not have been known beforehand). Built on
`/vast`, not `/ix1`. **Nothing published. The live overlay is still the 6 August
build and was never opened for writing.**

```
row_count 6,460,869
```

**Predicted before the run**: 7,596,959 − 1,111,147 (`gn`) − 24,943 (whg id-map
drop) = **6,460,869**. Exact to the unit.

**Per-namespace, every asserting source reproduces the published overlay exactly
except one:**

```
osm 2,295,659 ✓   wd 3,968,404 ✓   ohm 98,569 ✓   iv 68,935 ✓
tm 25,665 ✓       clio 248 ✓       og 222 ✓        loc 1,129 ✓
gn 0   vs   1,111,147 published        <-- the sole discrepancy
```

`gn: attempted=0 inserted=0 rejected=0` on a complete 13,454,817-document tree.

**The gate's verdict, from `processing.compare_hardlink_overlays`:**

```
TOTAL ROWS  7,596,959 -> 6,460,869   delta -1,136,090
VERDICT: FAIL   (exit 1)
```

**The delta is exactly `1,111,147 + 24,943`.** Ten namespaces are flagged, and
**all ten are the same cause**: the small authority namespaces (`bnf` −85.2%,
`gnd` −88.6%, `viaf` −90.9%, `loc` −90.5%, `tgn` −85.4%, `gov` −93.3%, `cerl`
and `wp` −100%) exist in the overlay almost *only* as targets of GeoNames
`sameAs`, so losing `gn`'s assertions removed their endpoints too. `wd`'s
−1,120,203 is the same effect — most `gn` edges point at Wikidata — while `wd`'s
own **asserted** count is unchanged at 3,968,404. `whg` −92.4% is reported as
*(shrink allowed)*, the id-map fix working. Nothing is unexplained.

**So the gate is doing exactly what it was written for**, and this is the
"materially below 7.6 M means something *else* is wrong — stop rather than
publish" case, with the something else named and quantified.

###### The guard, and the fact that it was validated before it was believed

`processing/compare_hardlink_overlays.py` is the guard §2.3 called for and did
not build. It reads both databases `mode=ro` (so the gateway's live file is never
written to, not even a hot journal), reports total rows plus per-namespace
*rows-touching* coverage, exits non-zero on an unexplained shrink, and takes
`--allow-shrink NS` for a fall that is supposed to happen.

It was run against inputs whose answers were known **before** being used to
decide anything:

| test | expected | got |
|---|---|---|
| incumbent vs **itself** | PASS, all deltas 0 | **PASS**, exit 0 |
| incumbent vs the **May overlay** | FAIL | **FAIL**, exit 1, naming `gn`/`wd`/`ohm`/`og` |
| sub-tolerance changes in that run | *not* flagged | `osm` −2.2%, `bnf`/`gnd`/`loc`/`viaf`/`whg` −0.1% — correctly silent |
| `--allow-shrink whg` on the real gate | exempts **only** `whg` | `whg` "(shrink allowed)", nine others still FAIL |

The known-good half matters as much as the known-bad: **a guard that cannot say
PASS is as useless as one that cannot say FAIL.**

⚠️ **It also reproduces the audit's independently-derived figures exactly** —
total 7,596,959, `wd` 7,516,092, `gn` 5,092,751, `osm` 2,318,576, `ohm` 98,569,
`iv` 68,935, `whg` 26,981. That is what validates the *metric*, not just the
code: the counting was rewritten mid-flight from a `UNION` of `(rowid, ns)` pairs
to inclusion–exclusion (`|a in X| + |b in X| − |both in X|`) because the former
materialised a ~15 M-row temp B-tree and was far too slow to gate a publish on.
"Identical answer" was arithmetic until these numbers landed on the audit's.

###### The runbook is no longer a rehearsal

§2.3 warned that `loc_links`, `finalise_local` and the publish path had never
been reached. All but the final publish have now run:

* **LOC** — 1,132 attempted, **1,129 inserted**, matching the published `loc`
  asserted count exactly. Source read from `/ix1` without incident.
* **contributor replay** — reached DO Postgres from an htc compute node with
  nothing configured by hand, as predicted. `place_link` 34,569 → 1,087,
  `close_match` 3,206 → 956, **2,038 inserted** (predicted ~2,038),
  `attestation_input` still **0** (the live flow remains dormant).
* **`1f5aa50`'s renamed drop-ledger fields genuinely ran on CRC for the first
  time**: `rows_dropped: 25,168`, `unresolved_endpoint_refs: 25,997` — previously
  only ever observed as the old single key `"total": 25997`.
* `id_map_entries: 228,918`, `id_map_run_ids: ['whg-idmap-20260831T071935Z']`.

**Only `publish_hardlinks --execute` remains untried.**

###### Timings, for whoever sizes the re-harvest

Phase 1A **3,240 s** for all 27 namespaces — `osm` alone ~37 min; `gn` + `wd`
streamed 7.38 GB + 10.26 GB of JSONL inside that. LOC + contributors under 13
min. Total 67 min. A re-harvest after 2.7 reads `gn`/`wd` as **parquet** rather
than JSONL and should be faster, but it will also have 1.1 M more rows to insert.

###### What must happen before this step can complete

1. **2.7** — `update_merge` (**required**, not optional) → `h3_stage` →
   `h3_merge` → `ccode_merge` for `gn`/`wd`/`nl`. Blocked behind 2.8 by SG.
2. **Re-harvest** with a fresh cutoff. Expect `gn: attempted=1,111,147` and a
   total near 7.57 M. **If `gn` comes back anything else, 2.7 did not do what it
   reported** — an independent downstream check on 2.7.
3. **Re-run the gate**, which must PASS.
4. **Then** `publish_hardlinks --execute --cutoff <the NEW harvest-start>`.
   ⚠️ The cutoff from this run (`2026-08-31T21:36:34.901990+00:00`) is **void** —
   it belongs to a build that will never be published.
5. ⚠️ **A publish is invisible until the gateway restarts.** `publish_local` is an
   atomic same-filesystem `os.replace`, and its docstring is explicit that *"the
   gateway's open descriptors against the previous inode stay valid until it
   re-opens."* Restart ownership is S5's. A publish without that restart changes
   nothing anyone can observe.

###### Two operational notes worth keeping

* **⚠️ `submit_hardlinks_slurm` defaults `--db-path` to `IX1_BASE`, and should
  not.** The `/ix1` build reached 236 MB in 13 minutes; the `/vast` build did
  41.9 → 58.5 MB in **25 seconds** — about an order of magnitude — and
  `publish_local` does a `shutil.copyfile` into the target, so building on
  `/vast` publishes across filesystems perfectly well. (Scoped claim: this is one
  ~1.3 GB SQLite. S5 correctly pushed back on generalising it to a 74-bucket
  retile, where `/vast`'s 275 GB is shared with production ES.)
* **`/ix1` was wedged on `htc-n56` and `crc1`** for part of this session while
  healthy on `crc2` and `pitt` — a per-NFS-client hang, not a filer outage. It is
  mounted `hard`, so the first harvest sat in `D`/`folio_wait_bit` with **CPU time
  frozen at `00:08:30`** while `squeue` reported RUNNING and the log sat still. It
  would have burned the full wall without failing or logging. **File size,
  `squeue` state and the job's own log all failed to discriminate; frozen CPU time
  and byte-identical `/proc/<pid>/io` counters did.** *A job in state RUNNING is
  not evidence that it is running.*
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
51,187,900 staged places — 51.3% of the corpus** (⚠️ this denominator previously
read 51,188,772, the 4 Aug run-log figure this plan later warns is the wrong
restoration target — Auditor F10) — and report success. Not "two
namespaces short": half the corpus, including *all* of GeoNames and *all* of
Wikidata, which are exactly the namespaces Symphonym trains on
(`--training-namespaces gn wd tgn`).

**~~Also noticed, not yours to fix:~~ ✅ RESOLVED** — `un` was given a `final/` by
Slurm 11075438 (`un-final-20260831T145706Z`), and 2.7 now uses
`un → final/places.parquet` as a control. ⚠️ *That same chain is what produced the
hull-derived cover — see §2.10.* The original note read: `un` has `extract/` but no `final/` — the
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

### ⚠️ 2.5 — DATA RESTORED, STAGE DEPTH NOT. Sufficient for 2.6 and the overlay; **NOT for S5**

**Correction, 31 Aug, from S5 — and the error was mine.** I recorded 2.5 as
complete and told SG it unblocked both the overlay rebuild and the retile. Half
of that was wrong. `gn`, `wd` and `nl` hold **only `extract/`** — no `final/`, no
`h3_merged/` — verified on disk against `un`/`ukhc`/`clio` as controls, which all
have `final` + `h3_merged` + `extract`, so the probe discriminates.

| consumer | resolver behaviour | status |
|---|---|---|
| 2.6 toponyms stage 1 | falls back `final → h3_merged → boundary_merged → extract` | ✅ satisfied |
| overlay harvest (`hard_links_staged`) | same chain, verified `:42-59` | ✅ satisfied |
| **S5's retile submitter** | `_eligible_buckets` requires `final/places.parquet` **or** `final/places.jsonl` and otherwise does a bare `continue` | ❌ **would silently drop gn, wd, nl** |

So a retile today tiles 24 buckets, reports success, and omits the largest
gazetteer in the corpus — this campaign's signature failure, in the step whose
deploy destroys its own evidence.

**Why my verification missed it, which is the part worth keeping.** 2.5's check
measured row counts and a delta against the live index. Those are satisfied at
`extract` depth exactly as well as at `final` depth: *the measure cannot
distinguish a completed stage chain from a bare extract*. The property S5 needs
is **stage depth**, and nothing measured it. This is the discrimination rule
again — ask what the broken world produces, and here it produces the identical
number — committed by me, in the check I had just finished writing the rule into.
Note also `166b74b` gave **`un`** a `final/` for precisely this reason and I did
not generalise it to `gn`/`wd`/`nl`.

**The remedy is a scope decision for SG**, not for S5: run `gn`, `wd` and `nl`
through `h3_stage → h3_merge → ccode_merge` as `un` was, which is hours of Slurm
on the two large ones. `ccode_merge` is the only writer of `final/`.

### ✅ 2.5 — the data half, verified 31 Aug after S4 closed

`gn`'s extract (Slurm 11074352) reached COMPLETED. S4's parked verifier
(`s4_verify_staged.sbatch`, Slurm 11082769) then passed all three against the
live index using **both** pipeline resolvers — `rebuild_toponyms_index`'s and
`h3_stage`'s, the two that carry different traps:

```
wd  11,459,393  delta 0   PASS      nl  4,363  delta 0   PASS
gn  13,454,817  delta 0   PASS      OVERALL: PASS
```

And the identity holds exactly (Slurm 11082822, all 27 namespaces):

```
TOTAL   51,187,900   ==   live index total   51,187,900
```

**2.6's baseline is confirmed STATE 1** in the same run — `ipa` and
`panphon_features` columns exist, `total 72,703,552 / with_ipa 0 / with_panphon 0`.
So the named check discriminates and the re-run is a clean from-scratch write,
not a resume.

⚠️ **Two traps hit while doing this, both already documented and both still
worth restating.** The census failed first with
`ImportError: GLIBCXX_3.4.30 not found` — an htc node whose `libstdc++` predates
the conda env's, exactly S3's `42b6e4a`; the parked `.sbatch` exports
`LD_LIBRARY_PATH="$CONDA_PREFIX/lib:…"` and a hand-rolled `--wrap` does not, so
**use the parked wrapper rather than reinventing it**. And `s4_baseline.py`
printed its target as "(4 Aug run scanned 51,188,772)" — the stale figure this
plan warns about — so it was corrected in place to the live total before the run.

**This unblocks the overlay rebuild. It does NOT unblock S5** — see the
correction above.

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

**Baseline established 31 Aug (S4, Slurm 11074343) — STATE 2 of the three below:
columns present, both empty.**

```
schema  toponym_id, name, lang, lang_variant, script, ipa VARCHAR, panphon_features BLOB
total            72,703,552
with_ipa                  0
with_panphon              0
```

So the named check **does** discriminate here (72,703,552 vs 0 vs 0 reads "bad"),
and it has now been watched failing against a known-bad input rather than assumed
to work. The partial state was expected and did not occur: the 4 Aug rerun was
killed mid-pass and an interrupted DuckDB transaction rolls back, so its 28 M
updates went with it. Consequence: **the re-run is a clean from-scratch write,
not a resume** — no idempotency question, and the named check is sufficient.

⚠️⚠️ **TWO DEFAULTS WILL EACH REPRODUCE THE ORIGINAL FAILURE. Override both.**

1. **`--for-retrain` is not optional here — it is the entire step.** Without it
   `submit_batch9_slurm` appends `--training-namespaces _none_`, which is exactly
   what made the columns empty in the first place. A 2.6 run that forgets it
   completes cleanly in ~3 h and changes nothing.
2. **The generated wall is `03:40:24`, and the work needs >12 h.** Measured, not
   inferred (Slurm 11074461, running the real estimator against the real history
   file): `_estimate_toponym_wall()` returns **13,224 s → `#SBATCH --time=03:40:24`**
   — byte-identical to the 31 July sbatch. The cause is that
   `estimate_wall_time_seconds` takes the median of the last 5 completed runs
   +20%, and **all three recorded `rebuild-toponyms-index` runs (11,020 s, 9,080 s,
   12,400 s) skipped IPA/PanPhon**. The history is poisoned by fast runs of a
   cheaper job wearing the same name, and the estimator cannot tell: its one guard
   skips records that "did real work but finished with zero output", and a
   `_none_` run legitimately produced output. So the *first* correctly-configured
   run of this step will be killed at roughly 30% unless the wall is overridden by
   hand. That is the same shape as the 4 Aug rerun's death, arriving through the
   sizing table instead of a hand-written sbatch.

After a successful run, the history self-corrects — but only for the next run,
which is no help to this one.

---

### 2.10 Diagnose the ccode H3 prefilter — **S9**  ⚠️ GATES 2.7's `gn`/`wd`

**Assigned by SG, 1 Sep**, on S8's recommendation. A **code-reading** job, not a
compute one: no staged writes, so it runs alongside S8's hold.

**The question.** `nl`'s ccode came out 4,348/4,363 against the live index's
4,363. The 15 losers are small offshore islands and coastal territories whose
**country polygons demonstrably contain them** (S8, via `prep().contains` on the
geom store). Tier 2 recovers **none** of them. So the exact answer exists and the
H3 prefilter is dropping them before any polygon test runs. **Why?**

**Established, and safe to build on:**

* `build_un_prefilter` (`ccode_enrichment:215`) builds cell→ccodes from the **`un`
  docs' own `h3_cover`**, collapsed to `PREFILTER_RESOLUTION = 4`. It does **not**
  read `un.h3_coverage.json`.
* `SOURCE_LABEL = "un-h3-overlap"` (`:79`) is a module-level constant stamped on
  every output at `:999`. It is **not** evidence of which tier ran. S8 was misled
  by it; do not repeat that.
* The geom store is healthy: 247 `un` keys, 11,768,864 total.
* Tier 2 is a separate program (`backfill_uncoded_ccodes`, own `SOURCE_LABEL`,
  own `main()`), and on `nl` it resolved **0 of 15** with `no_geom=0`.

**NOT established — and the reason this needs someone fresh.** I tried the obvious
mechanism and my own control refuted it. Reading `staged/un/final/places.jsonl`:
`un:usa` carries **278 compacted cells** (r0×1, r1×8, r2×70, r3×199), `un:nzl` 296
(r3×21, r4×72, r5×203). Testing whether a point's cell at each resolution present
is in the cover: all three islands **uncovered** — **but Denver, deep in the
continental USA, also tested uncovered**, which cannot be true when **1,532 `US`
resolutions** occurred in that same run. **Either my test is wrong or the cover is
far more incomplete than a working prefilter allows. Resolve that contradiction
FIRST** — until it is explained, the island result means nothing.

⚠️ **ANSWERED — do NOT resolve this first (Auditor F2, 1 Sep).** This brief was
written at 16:48; the answer landed 16:58–17:20 and the section was never
revisited. **The Denver control failing was NOT a broken test — it WAS the defect,
observed correctly.** `un:usa`'s `extract/` holds 376 cells with Denver TRUE, its
`h3_merged/` 278 with Denver FALSE. **Question 1 below is CLOSED, and so is
question 4**: the mechanism is `select_h3_cover_geometry:652`'s hull preference
plus the antimeridian, not `compute_h3_fields` simplification. **Questions 2 and 3
remain genuinely open.** And the "`nl` re-tests in two minutes" reproduction below
is stale — `nl`'s own covers are hull-derived, so it must be **RE-RUN**, not
re-tested.

**Questions, in the order they unlock each other:**

1. Why does a mainland point test as uncovered while 1,532 `US` resolutions
   happened? (Is my per-resolution containment test wrong, or is the cover
   genuinely that sparse?)
2. What is actually passed as `un_records`? I read
   `staged/un/final/places.jsonl` — the ccode run may not use that.
3. Is `un.h3_coverage.json` consumed anywhere at all? `submit_h3_slurm`'s comment
   calls it "the ccode enrichment pre-filter, when namespace == un", but
   `build_un_prefilter` does not read it. Stale comment, or a second path?
4. Unproven hypothesis, offered only as a starting point: `compute_h3_fields`
   simplifies large polygons before polyfill, which would drop small offshore
   islands from a country's cover. That fits the islands and **not** the control.

**Reproduction:** `nl` is a 4,363-record instance that re-tests in two minutes.
S8 has offered to run any measurement on it — **ask it rather than writing to any
staged tree yourself.**

**Also worth fixing while in here** (S8): `backfill_uncoded_ccodes` prints
`still uncoded 15 (genuinely outside every country: open ocean, Antarctica)` for
records the country polygon contains. It knows only that *its own tier* placed
nothing and asserts a fact about the world.

---

### 2.8 Make the staged merges atomic — **S9**  ⚠️ RUNS BEFORE 2.7

**SG's decision, 1 Sep: land this before 2.7 rather than manage the hazard by
serialisation.** Promoted here from residual 4.13, where S8 and S3 independently
converged on it.

**The defect.** `h3_merge.run_h3_merge` and `ccode_merge.run_ccode_merge` both `open("w")`ed their output
JSONL in place and derive the Parquet afterwards (`:227` / `:174`, via
`write_parquet_from_jsonl` at `ccode_merge:201`). Neither file contains a single
`os.replace`, `.tmp` or rename — verified, the grep returns **0** for both. Every
consumer walks `final → h3_merged → boundary_merged → update_merged → extract` and
tests `.exists()` only, with no size or completeness check. So `open("w")` creates
a **zero-byte** file that is *immediately preferred* over a complete earlier
stage, yielding **no rows at all**, silently, for as long as the merge runs —
hours, for `gn` and `wd`. Worse than truncation, because a partial JSONL is valid
JSONL to wherever it has reached.

**Why it belongs before 2.7 specifically, not merely in general.** `gn`, `wd` and
`nl` have **no `final/` or `h3_merged/` yet**. Temp files are invisible to every
resolver (different names), so with this fix in place readers keep resolving to
the complete, correct `extract/` for the whole of 2.7's run and the new stage
appears only when whole. That does not shrink the window — it **removes** it, and
turns the cross-session serialisation from load-bearing into belt-and-braces.

⚠️ **SCOPE CORRECTED, 1 Sep — it is FOUR writers, not two, and the two I first
named are the wrong two to stop at.** S3 spotted `update_merge`; S8 verified it and
found `boundary_merge` as well. Measured uncapped:

```
update_merge.py     open("w")=1   os.replace/.tmp/rename=0    ← was NOT in 2.8
h3_merge.py         open("w")=1   os.replace/.tmp/rename=0
ccode_merge.py      open("w")=1   os.replace/.tmp/rename=0
boundary_merge.py   open("w")=1   os.replace/.tmp/rename=0    ← was NOT in 2.8
```

All four write directories that outrank `extract/` in
`_STAGED_SOURCE_PRIORITY = (final, h3_merged, boundary_merged, update_merged, extract)`.

**The sequencing consequence is sharp, and it undercut the reason 2.8 was put
first.** 2.7's corrected chain **begins with `update_merge`** — on `gn`, collapsing
a 1.4 GB patch into a 7.38 GB snapshot, the longest window on the largest
namespace. Scoped to two files, 2.8 would have left the hazard exactly where 2.7
meets it first, and bought far less than it appeared to.

**Why the scope was wrong is worth recording** (S8's own framing): the original
4.13 proposal named `h3_merge` and `ccode_merge` because those were the two
writers traced at the time, and I carried that into 2.8 unchallenged. **A fix
scoped to the instances found rather than to the defect** — the same shape as the
truncated-grep errors, one level up.

**Extend to all four, preferably via one helper** (`write_staged_outputs_atomically`)
with four call sites, so the next writer added inherits it rather than repeating
the omission. If keeping 2.8 landable at two files is preferred, the *only*
acceptable alternative is a separate row for the other two **plus a hard
precondition on 2.7 that it cannot start until `update_merge` is covered** —
otherwise the ordering SG chose does not achieve what it was chosen for. Silently
proceeding on two is not an option.

**The change — ~10 lines per writer, and the pattern already exists in this repo**
(`repair_staged_docs.py:247` does `tmp_jsonl.replace(jsonl_path)` for this very
reason):

1. write `places.jsonl.tmp` (in **each** of the four writers);
2. `write_parquet_from_jsonl(jsonl_tmp, parquet_tmp)` — it takes explicit paths,
   so no change needed there;
3. `os.replace(parquet_tmp, parquet_path)` **then** `os.replace(jsonl_tmp, jsonl_path)` — see the ordering note below.

⚠️ **The rename ORDER matters, is not obvious, and was disputed — here is the
reasoning, so a successor can re-open it if they disagree.** A stage has two files,
the pair cannot be made atomic, and resolvers prefer `places.parquet` **over**
`places.jsonl` within a stage. **Both orders fix the primary hazard** — neither
ever exposes a partial file — so this is a second-order choice about which
*inconsistent-pair* state a crash between the two renames leaves behind.

**REQUIRED: parquet first, jsonl second.** ⚠️ **This reverses what this step
first specified, and the reversal is mine to own.** I argued jsonl-first on the
principle that a crashed merge should look like it never happened. S8 and S9
reached the opposite conclusion independently; looking for a decisive case, the
campaign's own history settles it against me.

Both orders are safe from the primary hazard — the temps are fully written before
either rename, so **neither order ever exposes partial data**. The choice is only
about which complete-but-mismatched pair a crash *between the two renames* leaves
behind, and that residue is durable:

* **parquet-first crash** → new Parquet + old JSONL. The resolver prefers Parquet,
  so **the authoritative read is the correct, current data.** Re-running is a
  no-op. Benign if nobody re-runs.
* **jsonl-first crash** → new JSONL + old Parquet. The resolver serves the **stale**
  Parquet **indefinitely and silently**, while the JSONL beside it says otherwise.

**That second state is Fault 12 exactly** — "`un`'s improved `h3_cover` sat in
`h3_merged` for three days while the index kept a stale copy, and the freshness
gate could not see it because `final/` was self-consistent." Serving stale data
silently is the failure this campaign exists to eliminate, and my principle
mistook an *early* publish of complete data for a *partial* one.

⚠️ **A limitation no rename order fixes** (S9): `write_parquet_from_jsonl` strips
hull, so the JSONL is canonical for hull-consumers. A crash between renames leaves
those two disagreeing whichever order is used. Worth stating rather than implying
the ordering makes the pair safe.

Clean up temps on failure.

**The test — it must fail today.** Do not attempt to test concurrency. Make the
merge raise partway through its document loop, then assert the target is either
**absent** (first write) or **byte-identical to the previous file** (re-write). A
crashed merge currently leaves a truncated file, so that test fails now and passes
after — deterministic, no timing, and it encodes the property directly.
`tests/test_h3_merge_helpers.py` and `tests/test_ccode_skip_regenerates_final.py`
are the natural homes; `tests/test_staged_pipeline_e2e.py` exercises both merges
end to end.

⚠️ **A trap that SURVIVES this fix, and would reintroduce it silently** (S9).
`_unlink_quietly` is now deliberately **asymmetric**: narrow (raises on anything
but `FileNotFoundError`) in the failed-conversion branch, which *relies* on the
stale sidecar genuinely being removed; and wrapped at the exception handler, so a
cleanup failure cannot mask the failure that caused it. **That asymmetry is the
correctness property, and it reads like an inconsistency.** If a future tidy-up
widens the helper to swallow everything, **every test still passes** — the
stale-sidecar path has no crash in it to catch, so nothing goes red, and the
stale-parquet bug that parquet-first exists to prevent comes back silently.

A comment is currently the only thing holding it. **A comment cannot fail.** Pin
the narrowness with a test — assert `_unlink_quietly` *propagates* a
`PermissionError` — so widening it turns something red. Small, and it is the
difference between a documented invariant and an enforced one.

**Explicitly OUT of scope:** hoisting the five duplicate `_STAGED_SOURCE_PRIORITY`
definitions into one shared module (`index_from_stage:71`, `generate_tiles:145`,
`aat_enrich:67`, `gazetteer_temporal_extent:54`, `hard_links_staged:42`). That is
right and it is a separate row — a fix landing mid-campaign must stay small.

**Preconditions.** Nothing may be writing a staged tree while this lands. Right
now every session is holding and the running harvest calls neither merge, so this
is the safest window there will be. Confirm the shared clone is current first
(§"Before ANY session runs pipeline code" — and note the prescribed `git fetch
origin` does not work; use the HTTPS refspec).

**Verify:** the new test fails on the pre-change code and passes after; both files
land; the existing suites still pass; and a manual run of one small namespace
through `h3_merge` leaves no `.tmp` behind.

⚠️ **The risk this carries:** a bug here corrupts the very step it protects.
2.7's own verification is the backstop — it counts names and polygons
independently of anything this change touches — but that is *after* the fact, so
the test above is what has to be right.

---

### 2.7 Give `gn` / `wd` / `nl` a real `final/` — **S8**  ⚠️ GATES S5

**Why:** `submit_tiles_slurm._eligible_buckets` requires `final/places.{parquet,jsonl}`
per per-namespace bucket and otherwise bare-`continue`s, and these three have only
`extract/`. A retile today would tile 24 buckets, report success and omit the
largest gazetteer in the corpus. 2.5 restored the *data*; this restores the
*stage depth* (see the 2.5 correction).

**SG chose the correct route over the fast one (31 Aug): index-adequate, not
merely tile-adequate.** Tiles carry no ccodes, so a pass-through `final/` would
have unblocked S5 with nothing rendered wrong — but `final/` is what a future
rebuild indexes from, and for `wd` a pass-through would bank a regression against
the live index's 97.3% ccode coverage. Measured on the staged extracts:

| ns | docs | ccodes in extract | h3_cover in extract |
|---|---:|---|---|
| `gn` | 13,454,817 | 20,000 / 20,000 sampled — **native**, GeoNames carries them | none |
| `wd` | 11,459,393 | **25 / 20,000** sampled | none |
| `nl` | 4,363 | **0 / 4,363** | none |

So all three need real `h3_stage` work (unlike `un`, whose extract already had
h3), and `wd`/`nl` need genuine ccode enrichment rather than a pass-through.

⚠️ **PRECONDITION — check for a reader before you write.** This nearly went
wrong on 31 Aug: S8 read the plan, correctly concluded 2.7 was startable, and the
only thing that prevented a corrupted overlay was that it happened to ask a peer
first. **The constraint existed solely in messages between sessions**, which is
this campaign's own signature failure wearing new clothes. So, in the document:

```bash
# no job may be reading staged/gn, staged/wd or staged/nl while 2.7 writes them
ssh crc1 'squeue -M htc -u $USER -o "%.10i %.30j %.8T %.10M"'
ssh crc1 'sacct -M htc -S today -o JobID,JobName%30,State,Elapsed | grep -i hardlink'
```

A running `hard_links_staged` harvest is the specific thing to look for — it
streams all 27 trees in manifest order with `gn` and `wd` third and fourth — but
any job reading those trees counts. A successor will not have been in this
conversation.

⚠️ **The colliding writer is `h3_merge`, NOT `ccode_merge` — corrected 31 Aug
(S8), because this step's own text misled on it.** Framing 2.7 around
"`ccode_merge` is the only writer of `final/`" invites the reading that the
earlier stages are safe to start. They are not: `h3_merged` **also outranks
`extract`** in every priority chain, and ⚠️ **SUPERSEDED BY 2.8 (landed `554e43a`+`e37c93b`) — the zero-byte window below
no longer exists; kept because it is why 2.8 was ordered first.** ~~`h3_merge.py:235` has the identical
non-atomic pattern — `jsonl_path.open("w")`, parquet derived afterwards, no
temp-and-rename in either file (verified: zero occurrences in both).

And the exposure is not a race. `open("w")` creates the file at **zero bytes on
the first instant**, and `_staged_namespace_source` tests `.exists()` only — no
size, no completeness, no manifest consult. So from the moment `h3_merge` starts
on `gn`, a concurrent reader prefers a **zero-byte** `h3_merged/places.jsonl`
over the complete 7.38 GB `extract/places.jsonl` and yields **no rows at all**,
silently. For `gn` and `wd` that window is *hours* wide. Worse than truncation,
because a partial JSONL is valid JSONL all the way to wherever it has reached.~~

### 🛑 2.7 CANNOT BE RUN AS FIRST WRITTEN — the chain omits `update_merge`

**Found by S8, 31 Aug, verified here in full.** Calling this "the standard chain,
nothing bespoke" was wrong for `gn` and `wd`, and wrong in the way that has
already cost production once.

`UPDATE_PATCH_NAMESPACES = frozenset({"gn", "wd"})` (`staging_contract:194`).
Both `h3_stage._extract_stage_dir` (`:135`, branch `:151-153` — ⚠️ this read `:108`, an unrelated comment; its twin resolved, which is the worst arrangement: the half that works lends credibility to the half that does not) and `h3_merge._source_dir:88` prefer
`update_merged/` for those two — and **fall through to `extract/` with a bare
`return` when it is absent**. No error, no warning. `submit_h3_slurm:140-144`
states the consequence in its own comment: *"that is how ~26.7M GeoNames
alternate names and 58,658 Wikidata geoshapes went missing from production and
from this rebuild."*

**The patches exist and are substantial, and no `update_merged/` does:**

```
gn/update_patch/places.update.jsonl   1,396,861,789 B   31 Aug 13:29  ← written today, by gn's own extract
wd/update_patch/places.update.jsonl      92,909,685 B    7 Aug 21:03
nl                                        (none — correct, nl emits no patch)
gn/update_merged  wd/update_merged  nl/update_merged     ALL ABSENT
```

So both available routes are bad, and **they fail differently — one invisibly.**
Manifest `staged-restore-20260831T0415Z`, read directly:

```
gn   extract=completed  update_merge=pending   → deferred by the barrier, WITH a printed reason
wd   extract=PENDING    update_merge=pending   → bare `continue`, NO message at all
nl   extract=completed  update_merge=skipped   → proceeds correctly
```

`wd`'s extract is marked **pending while its 10.26 GB file sits on disk**, so via
the submitter `wd` vanishes from the run without a line of output. And bypassing
the submitter to run `h3_stage` per namespace does not error either — it silently
builds `final/` from `extract/`, **discarding the 1.4 GB `gn` patch and the 93 MB
`wd` patch** and banking exactly the regression that comment describes.

⚠️ **`_pending_namespaces` reads the manifest dict with no `events.jsonl`
fallback**, so the standing "events beat manifest" rule does not rescue this. And
the older `h3ccode-20260805T120000Z` manifest still records `gn`/`wd`
`update_merge: completed` — true of the pre-accident tree, false now. A session
consulting that one concludes the barrier is satisfied.

⚠️ **The two patches are NOT interchangeable, so a partial 2.7 fails in two
different ways.** Measured directly from the patch files (31 Aug):

```
gn   200,000 sampled      toponyms_to_add 194,010 (97%)   relations_to_add 54,590 (27%)
wd    58,657 = WHOLE FILE  geometries_to_replace 58,657 (100%)   h3_cover/h3_centroid 58,656
```

* **`gn` loses names and relations** — invisible to any document count, which is
  why check 4 must count names.
* **`wd` loses polygons.** `update_merge.py:36`: `geometries_to_replace`
  **overwrites the entire `geometries` array**. So a `wd` `final/` built without
  `update_merge` keeps the un-enriched extract geometry, and those **58,657
  documents' polygons are simply absent** from anything built on it.

**That is the campaign's original failure arriving through a different door**
(S5). The nine boundary layers shipped as points because the tile job read a
destroyed geom store; this would ship `wd` as points because the geometry never
entered the staged document at all. Same visible outcome, different mechanism —
and it passes every check that existed before this paragraph.

`index_namespace.py:156` also guards on `has_geom and not h3_cover`, and the `wd`
patch supplies `h3_cover`/`h3_centroid` alongside the geometries — so the patch is
what makes those documents **indexable** under that guard, not merely richer.

**Corrected chain:**

```
gn, wd:  reconcile wd's extract status → update_merge → h3_stage → h3_merge → ccode → ccode_merge
nl:      h3_stage → h3_merge → ccode → ccode_merge            (as first written — correct for nl)
```

⚠️ **Read this as a RECURRENCE, not a discovery — S5's framing, and it matters.**
This is the `update_merge`-never-ran incident from earlier in the campaign, and
the barrier `gn`/`wd` are hitting **was added specifically to prevent a second
one**. It is working exactly as designed: deferring them because their patches
genuinely have not merged. **So the plan's fault is a missing step, not an
obstructive barrier.** A successor reading "2.7 was blocked by the h3 barrier"
could conclude the barrier is the problem and route around it — precisely the
failure it exists to stop. Do not disable it; satisfy it.

**`processing/reconcile_stage_status.py` is the built-for-purpose repair for the
`wd` half** (S5; verified here). Its docstring describes this failure in advance —
a manifest saying `pending`/`failed` while the artefact is complete on disk. It is
**evidence-based, not a blind setter**: it promotes a stage only when that stage's
artefact exists and is non-empty, prints path and record count for every change,
and will not demote or touch a stage whose artefact is missing. Its `--skip` is
guarded by `_stage_applies` (`:207-215`), which returns
`namespace in UPDATE_PATCH_NAMESPACES` for `update_merge` — so it **structurally
cannot** be used to skip the very step at issue. That guard is why it is safe to
recommend where a manual manifest edit would not be.

### 💾 CAPACITY — 2.7 needs ~95–110 GB of `/vast`'s 274 GB, on production's volume

**Measured by S8, confirmed here: `/vast/ishi` is 1.0T, 751G used, 274G available
(74%).** `staged/` is 120G of that. S8 projected 2.7's footprint from *real* growth
ratios of namespaces that have completed the chain, not from an estimate — point-
dominated namespaces grow ~1.0–1.06× per stage (`iv`, `tm`, `ofs`, `alc`, `dgsd`),
only polygon-heavy `clio` grows 2.2×, and Parquet runs ~28% of its JSONL. `gn` and
`wd` are point-dominated:

```
gn  extract 7.38 GB  → update_merged ~11 + h3_merged ~11.5 + final ~11.5 + parquets ~9.6  ≈ 44 GB
wd  extract 10.26 GB → ~12 + ~13 + ~13 + parquets ~11                                      ≈ 49 GB
nl  negligible; h3/ and ccode/ patch files across both                                     ≈  5 GB
                                                                                   TOTAL ≈ 95–110 GB
```

**This volume is shared with production ES and has a recorded history of being
driven flood-stage read-only, which took production down with it.** Rulings:

1. ✅ **Run `gn` and `wd` SEQUENTIALLY, not together.** Halves peak concurrent
   usage and allows a `df` between them. Slower, and the safer shape — the same
   trade SG already made in choosing the correct route over the fast one.
2. ✅ **2.7 and S5's `--output-dir` tile-intermediate proposal must NOT run
   concurrently.** 74 buckets of intermediates (28 GB on `/ix1` today, more once
   rebuilt) onto the same 274 GB. Each is defensible alone; together they are not.
   S8's framing is the one to keep: *that is how headroom gets consumed by two
   people each acting reasonably.*
3. ✅ **KEEP `update_merged/` and `h3_merged/` after `final/` exists.** They are
   dead weight for *resolution* — `final/` outranks both — and deleting them would
   return ~45 GB. Do not. They are the evidence for what 2.7 did, and this campaign
   has been burned repeatedly by discarding evidence before the dependent thing was
   checked. Delete only after **both** S3's re-harvest and S5's retile have
   consumed the result. At current headroom the cost is affordable.

### 💾 `update_merge` MEMORY — the 16G alarm is wrong; the structural point is right

**S8 measured `gn`'s patch dict at 14.4 GB** — `_load_patches:114` builds
`{place_id: merged_patch}` for the whole file before streaming any document
(confirmed), so it is all resident. It sampled 8,125,650 lines two ways and took
the **random** figure (1.86 KB/line → 14.4 GB) over the **head** figure
(1.68 KB/line → 13.0 GB), because the head sample was ~10% optimistic — the
ordered-file lesson, applied to itself.

⚠️ **But its conclusion — "will OOM at the 16G every sbatch in this chain
requests" — does not hold.** The submitters do not hardcode 16G:
`submit_h3_slurm:206` and `submit_ccode_slurm:256` both use
`array_memory_gb(namespaces, STAGED_BASE_DIR)`, which tiers off
`staged_source_bytes`. **`gn`'s largest staged artefact is 7,384,904,990 B =
6.88 GB, which falls in the `< 8 GB → 64 G` tier.** So `gn` gets **64 G**, which
covers a 14.4 GB dict with headroom. (S8 was likely reading a hand-written
template — mine requested 48 G — rather than submitter-generated output.)

**The structural point survives the arithmetic, and is the part worth keeping:**
`array_memory_gb` sizes from *the largest staged artefact*, which is a good proxy
for stages that **stream** and a bad one for `update_merge`, the one stage that
**accumulates**. `gn` landing at 64 G is luck, not design — a namespace with a
small snapshot and a large patch would be sized from the wrong quantity.

**The genuinely open number is the parquet step, and S8 is right that nobody has
it.** `write_parquet_from_jsonl` calls `paj.read_json(...)` then
`pq.write_table(...)` — a **full Arrow table in memory**, built *after* the patch
dict, so peak may be the **sum** rather than the max.

### ✅ RESULTS, 1 Sep — `un` recompute VALIDATED; `wd` measured; **`gn` needs 96 G, not 64**

**1. `un` recompute validated by SET comparison against production** (job
11103315_0, 29:45):

```
h3_merged  376 cells  SETS_EQUAL_TO_PROD=True   only_here=0  prod_only=0   all six probes TRUE
final      278 cells  SETS_EQUAL_TO_PROD=False  only_here=263 prod_only=361 Denver/NYC/… FALSE
```

⚠️ **`un`'s `final/` still holds the bad 278-cell cover**, because `ccode_merge`
has not run for `un` — **the Fault-12 shape exactly: a chain stopping short of
`final/`.** It does **not** block `nl`, and S8 checked why rather than assuming:
`ccode_enrichment._load_un_records` calls `_iter_staged_docs(UN_NAMESPACE)`, which
reads `_h3_merged_dir` — **`h3_merged`, not `final`** — so the prefilter is
live-correct now. Still to be brought current, because `final/` is what a future
rebuild indexes from and a known-bad artefact left there is how this campaign's
faults propagate. **A correctness chore, not a gate.**

**2. `wd` peak RSS — and it changes `gn`'s request.** Verified independently
(`sacct -j 11103334`): **MaxRSS 42,953,084 K = 40.96 GiB against 48 G, 85% of the
request.** `wd`'s patch is 93 MB so its dict was under a gigabyte — **essentially
all 41 GB was the Arrow/parquet conversion** of 11.46 M merged documents. That is
exactly the isolation the `wd`-first ordering was chosen to produce.

```
gn Arrow scaled  40.96 × (13,454,817 / 11,459,393) = 48.1 GiB
     + patch dict                          14.4 GiB
                                     total 62.5 GiB   →  98% of a 64 G request
                                                          65% of a 96 G request
```

🛑 **Request 96 G for `gn`'s `update_merge`, NOT 64 G.** At 64 it would sit at 98%
of its limit, before any allowance for `gn`'s merged documents being larger than
`wd`'s.

⚠️ **And the 200 k-sample extrapolation was ~3× optimistic** — it gave 1.07 KB/doc
against a real ~3.6 KB/doc for merged documents. S8 had adjusted its own estimate
upward for the sample being extract-shaped and *still* fell far short. **This is
the entire justification for `wd`-first: a 64 G `gn` run would have OOM'd hours
in, and no amount of extrapolation would have caught it.** My own "64 G covers a
14.4 GB dict with headroom" was right about the dict and wrong about the total — I
sized the component I had a number for.

**3. `wd` artefacts: complete pair, no degradation.**
`update_merged/places.jsonl` 10,338,890,657 + `places.parquet` 1,545,366,287, and
**`patches_unmatched: 0`** — so every patch entry matched a document, the 58,657
geoshapes landed rather than dangling, and `wd` is **not** in `ukhc`'s JSONL-only
shape.

### ✅ SG's RULING, 1 Sep — S8 takes the `un` recompute; `update_merge` on `wd` first

Both decisions to S8. The `un` recompute because it holds the validation gate, has
the three-way test working, is the consumer, and `un` has had no owner since S2
closed. **`wd` before `gn`** because `wd`'s patch is 93 MB (small dict) against
11.46 M documents (85% of `gn`'s 13.45 M) — so it **measures the Arrow/parquet
cost almost in isolation**, and `gn`'s figure becomes measured rather than
extrapolated from a 200 k sample. **Record `wd`'s actual peak RSS.**

Settled execution notes: `update_merge` by **hand-written sbatch** (no submitter
exists) at **48 G `wd` / 64 G `gn`**, built from `slurm_env` preamble constants so
it inherits the conda export; **do not** follow `submit_h3_slurm:153`'s inline
`Run:` line; `h3_stage`/`h3_merge`/`ccode` through the submitters with no
override; and the `un` recompute **validated against production's known-good
376-cell cover BEFORE it touches the staged tree**, by **set** comparison — `limuw`
is the standing reminder that 55 and 55 can be different 55s.

The two are independent — `staged/un` and `staged/wd` — and 247 documents will not
perturb a memory measurement.

✅ **The parquet number is now MEASURED (S8) — and read the `update_merge`
correction below before using this: the "submitter asks for enough" conclusion
holds for `h3`/`ccode` and is FALSE for `update_merge`, which has no submitter at
all** (Auditor F4).

**The measurement.** Running the real `write_parquet_from_jsonl` on a 200,000-doc `gn` sample:
peak delta **0.20 GB = 1.07 KB/doc → 13.8 GB** extrapolated. S8 then adjusted it
*upward* honestly: that sample is **extract**-shaped (~550 B/doc) while the
documents actually converted are **post-merge** (~820 B/doc, ~1.5×), so expect
**~20 GB**. And peak is nearer the **sum** than the max — `_load_patches`' 14.4 GB
dict drains via `pop()` but CPython does not return the arena to the OS, and the
conversion runs inside the same live frame. **Realistic peak ~30 GB+.**

⚠️ **S8 recommends 64 G for `gn` and 48 G for `wd`, still on the belief that the
sbatch asks for 16 G. It does not — and the submitter already asks for more than
it recommends:**

```
gn  largest staged artefact  6.88 GB  ->  array_memory_gb  =  64 G   (covers ~30 GB peak)
wd  largest staged artefact  9.56 GB  ->  array_memory_gb  = 128 G
```

**So for `h3_stage` / `h3_merge` / `ccode`: use the submitters, no override.**

⚠️ **BUT that instruction CANNOT be followed for `update_merge`, and correcting
S8's 16 G to 64 G did not change what `update_merge` needs — I over-generalised.**
Verified: there are **eight** submitters and **none of them is for
`update_merge`**; `array_memory_gb` has four call sites (`submit_batch9_slurm` ×2,
`submit_h3_slurm:206`, `submit_ccode_slurm:256`) and **`update_merge` is not among
them**. There is no `submit_update_slurm`, no `.sbatch`, no `.slurm`. **So the
accumulating stage — the only one whose memory profile is in doubt — receives no
tiering at all, and hand-writing an sbatch is the only way to run it on Slurm.**

**S8's measurement is therefore the operative number, not a redundant one:
hand-written sbatch at 64 G for `gn`, 48 G for `wd`**, written with the
`slurm_env` preamble constants rather than inlined, so it inherits the conda
export instead of repeating my `un` chain's omission.

⚠️ **And there is a trap in the same place — `submit_h3_slurm:153` tells the
operator, verbatim:**

```
Run: python -m processing.update_merge --namespace gn
```

**No `sbatch`. No submitter. No memory. No mention of Slurm.** Followed literally —
which is what a message in that form invites — that puts a **~30 GB peak** process
on whatever host the operator happens to be on: `pitt`, the **production VM**,
where heavy inline compute has already caused a ~1 h outage; or a login node,
which this campaign's own rules forbid. **The message is correct about *what* to
run and silent about *where*, and the where is load-bearing at 13.45 M
documents.** It should name the sbatch route and the memory. Same class as the
defects above: the accumulated knowledge lives in the submitters, and this is the
one stage that has none.

**Both of us generalised from the cases we had looked at** — S8 read `nl`'s
correct 16 G tier as a fixed default; I read `h3`/`ccode`'s submitters as covering
the chain. The arithmetic settles `h3` and `ccode`; the missing submitter settles
`update_merge`.

💡 **And note how both of this campaign's silent defects entered.** My `un` chain
ran from a **hand-written** sbatch that omitted the `LD_LIBRARY_PATH` export, which
is what produced the hull-derived cover. S8's 16 G alarm came from reading a
**hand-written** template rather than submitter output. The submitters are where
the accumulated knowledge lives — `array_memory_gb`'s tiers, the conda preamble,
the wall-time floors — and hand-rolling around them discards it silently.

⚠️ **The structural caveat still stands:** `array_memory_gb` tiers off
`staged_source_bytes`, a good proxy for stages that stream and a bad one for
`update_merge`, which accumulates. `gn` landing at 64 G against a ~30 GB peak is
**luck, not design** — a namespace with a small snapshot and a large patch would be
sized from the wrong quantity, and that wants fixing before it bites someone.

💡 **`wd` is the ideal experiment and isolates the unknown.** Its patch is 93 MB
(a small dict) but its document count is 11.46 M against `gn`'s 13.45 M — so
running `wd`'s `update_merge` first measures the **Arrow/parquet cost almost in
isolation**, at ~85% of `gn`'s scale, without the 14.4 GB dict confounding it.
That is the cheap experiment that produces the missing number.

⚠️ **And S8's second point stands entirely: the JSONL-only fallback is NOT a safe
landing.** If parquet conversion OOMs or fails schema inference, `6ad2640` leaves
JSONL-only with the stale sidecar removed — which puts `gn` in exactly **`ukhc`'s
shape**, and audit §3b establishes that a JSONL-only `final/` is *silently
excluded* from `backfill_uncoded_ccodes` and misreported by
`staging_orchestrator:748`. **"It degraded gracefully" would quietly drop the
largest gazetteer in the corpus out of tier 2.**

⚠️ **The temps do NOT double peak for 2.7 — but they will for a re-run** (S9,
corrected by S8's measurement). `gn`/`wd`/`nl` are extract-only, so every stage 2.7
writes is a **first** write and peak is one copy. Anyone *re-running* 2.7 over an
existing `final/` pays temps *plus* the incumbent, roughly double for the largest
namespace. The projection above is for the first run; a re-run needs re-sizing.

⚠️ **WITHIN-STAGE peak is higher than the finished-stage table shows, and the
mechanism is a third file nobody counted** (S9 raised it; mechanism verified here).
`write_parquet_from_jsonl` does not convert in place — it streams the JSONL through
`strip_hull_for_parquet`/`drop_nulls_for_parquet` into a **sibling
`*.parquet_input.jsonl`** (`staged_parquet.py:145`), feeds *that* to pyarrow, and
unlinks it at `:178`. So at the moment of conversion a stage transiently holds:

```
places.jsonl.tmp            full stage output
places.parquet_input.jsonl  hull-stripped near-copy of the SAME data   ← uncounted
places.parquet.tmp          ~28% of the JSONL
```

For `gn`'s largest stage (~11.5 GB) that is roughly **25 GB transient against
14.7 GB finished — about +70%** — sitting on top of every stage already written.
It is *within*-stage, so it does not accumulate across the three, and
"first-run-only" does not change it. Still comfortable at 274 GB, but it is
precisely the number that would surprise someone reading only the finished-stage
projection.

⚠️ **MEASURED AT `nl`, 1 Sep: the polygon growth ratio is WORSE than the table.**
`nl` went 5,160,153 → 22,919,497 through h3 — **4.44×**, against the 2.21× (`clio`)
the projection used as its polygon upper bound. `nl` is Native Land, territory
polygons, and it beat the assumed ceiling substantially. This does **not** change
`gn`/`wd`, which are point-dominated at ~1.05×, but `wd` gains 58,658 polygons
from its patch, so **re-measure at `wd` rather than carrying the estimate**.
Parquet ran 26.8% of its JSONL against the 28% assumed — the *method* is holding;
only the polygon ratio was wrong.

⚠️ **`submit_h3_slurm` HAS NO `--wall-hours` OVERRIDE — Fault 13's mitigation
exists only on `submit_ccode_slurm`** (S8, 1 Sep; verified). The h3 submitter
derives `--time` from `estimate_wall_time_seconds` at `:268` and formats it at
`:199`, with no way to override. That is the exact function behind Fault 13, and
for `gn` the history it medians is the 5 Aug run over the **pre-patch** extract —
before 26.7 M names were merged. Its evident unreliability: **it gave `nl` 24 hours
for 4,363 documents.**

**Ruling: use a hand-written sbatch with an explicit `--time` for `gn`.** It sits
inside S8's own step, changes no shared code mid-campaign, and needs no wider
decision. Adding `--wall-hours` to `submit_h3_slurm` is the right permanent fix
and belongs in the post-2.7 residual queue beside the resolver hoisting, **not**
in the middle of the run that needs it.

### ✅ RESOLVED 1 Sep (`a4ada2d`) — was: `submit_ccode_slurm` missing the `LD_LIBRARY_PATH` export

**Found by running, not by reading** (S8, 1 Sep; verified here). `python -m
processing.submit_ccode_slurm` fails to **import**, on pitt and on `crc2`:

```
submit_ccode_slurm:49 → ccode_enrichment:53 → geom_store:47 → import sqlite3
ImportError: /lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found
             (required by .../envs/whg/.../libicuuc.so.75)
```

The conda env ships `libstdc++.so.6.0.34` (GLIBCXX to 3.4.34); the system
`/lib64` one is 6.0.29 (to 3.4.29), missing the 3.4.30 `libicuuc.so.75` wants, and
the loader prefers the system copy. `LD_LIBRARY_PATH=$CONDA_PREFIX/lib` fixes it.

**The fix is one line and it already exists in this repo.**
`submit_hardlinks_slurm.py:164` is exactly
`export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH}"`, with a comment
recording the same fault on htc-n77 on 31 Aug and the conclusion *"other nodes run
the identical job fine, so it is the node image, not the env"* — **plus a
deliberate fail-fast probe** (`python -c 'import sqlite3; print(...)'`) so the job
dies in one second rather than after the harvest. `submit_ccode_slurm:264` does
`conda activate` and stops. **2.8's situation again: a known defect fixed in one
place and left in another, and the fix is a line we already wrote.**

Three consequences: it is **node-dependent**, so a `gn`/`wd` ccode array is a coin
flip — fast and loud rather than silent, which is the mercy, but a wasted queue
cycle on a multi-hour stage. It **probably explains S9's "4 pre-existing errors"**
on CRC, same three-hop trace, so those may not be environmentally unfixable —
worth re-checking with the export set before that is recorded as permanent. And it
**corrects the platform-limit note above**, which was mine.

**Do NOT set the variable in the submitting shell and rely on Slurm propagation** —
S8's reasoning, and it is right: that works today, leaves no trace, and breaks
silently for whoever runs this next.

**✅ Landed by S9 as `a4ada2d`**, export at `:278` **after** `conda activate` at
`:264`, with the precedent's fail-fast `sqlite3` probe at `:279`. Verified here.

**And S9 pinned it with a test that asserts POSITION, not presence** —
`tests/test_slurm_conda_lib_preload.py`, 6/6, covering both submitters. Its
reasoning is the sharper half and is worth reading: *a presence-only test passes
if someone hoists the export **above** `conda activate`, where `$CONDA_PREFIX` is
unset and the export is a silent no-op* — green test, dead fix. Same trap if the
probe drifts below the real work, since the probe exists to fail in one second
rather than after an enrichment pass. Verified against pristine HEAD: three ccode
assertions fail, three hardlinks pass, so it discriminates. Neither submitter had
any test before this, which is why the 31 Aug hardlinks fix could have regressed
silently for four days.

⚠️ **Consequential correction: the "4 pre-existing environmental errors" on CRC
were neither environmental nor unfixable.** S9 measured on htc-n88, same node and
commit, the export as the only variable: `test_ccode_skip_regenerates_final` goes
**FAILED (errors=4) → OK**. S9's original comparison was sound and its conclusion —
not a 2.8 regression — still stands; the word "environmental" carried an
implication of *permanent* and would have entered the record as a standing CRC
limitation. **True conclusion, false reason** — the third instance of that shape in
two days, after S8's `antimeridian` and my own `hard_links_staged` platform-limit
note, which S8 has likewise withdrawn. Same cause, same one-line fix, three
sightings.

### 🛑 `nl` CHECK 3 FAILED — 99.66% vs 100%, and the cause is a MISSING STEP, not a degradation

**S8's two-sided check earned its place on the smallest namespace, exactly as
intended.** `nl`'s `final/` came out 4,348/4,363 coded against the live index's
4,363/4,363 — 15 records that would lose their ccodes. Unlike `gn`'s undersea
residual these are **not** legitimately ccode-less: all 15 are small offshore
islands and coastal territories (the five Channel Islands, Block Island, San Juan
Islands, Great Barrier Island NZ, two treaty cessions), and the live index holds
the right answers for exactly them.

**Checks 1 and 2 passed cleanly** — resolver now returns `nl/final/places.parquet`
(was `extract/`), and `final/` is 4,363 rows, delta 0.

⚠️ **S8's hypothesis was that the ccode stage silently degraded to H3-only with no
Shapely refine, because the geom store was unavailable. Both halves are wrong, and
I checked rather than agreed:**

* **The store is fine.** `/vast/ishi/geom/index.sqlite` holds **247 `un` keys**
  (11,768,864 total). Its 03:37 timestamp is S2's merge writing it, not something
  predating it.
* **"4,348 of 4,348 from `un-h3-overlap`" is not a signal — it is a constant.**
  `ccode_enrichment.SOURCE_LABEL = "un-h3-overlap"` (`:79`) is module-level and
  stamped on **every** output at `:999`, whether or not the Shapely refine ran. No
  polygon tier appears in the output because *no label for one exists in that
  module*. A constant read as evidence.

**The actual cause: tier 2 is a SEPARATE PROGRAM that 2.7's chain does not run.**
`backfill_uncoded_ccodes` has its own `SOURCE_LABEL = "un-bnda-fallback"` (`:46`),
its own `main()` and its own argparse — it is not a stage of the ccode pass. The
live index's 100% includes records coded by that second pass; 2.7's chain stops at
tier 1. **So this is the `update_merge` omission again: a chain that leaves out a
step the original corpus run included** — not a regression in the code, a gap in
the plan.

**Predicted, not verified:** those 15 are tier-2 candidates and should resolve
under `backfill_uncoded_ccodes`. S8 should test that on `nl` before `gn`/`wd`
rather than take it from here. Note S9 has *just* repaired that tool
(`dbf789f`) — it now reads JSONL-only `final/` and reports a zero-scan instead of
printing nothing, so it is in better shape than when the corpus run used it.

**The hold on `gn`/`wd` was right and stands.** The cost of confirming now is
minutes; the cost of finding it after `gn` is hours of compute plus a `final/`
wrong in a way no row count can see.

#### 🛑 ROOT CAUSE — `h3_stage` polyfills the convex HULL, and I introduced the bad cover on 31 Aug

**Diagnosed by S9, completed here, and the run that broke it was mine.** Measured
across the stages my `un-final-chain` (job 11075438, 31 Aug) wrote:

```
un:usa  extract/    376 cells  r1×7  r2×55  r3×314   Denver covered = TRUE   ← correct
un:usa  h3_merged/  278 cells  r0×1  r1×8  r2×70 r3×199  Denver covered = FALSE  ← MY RUN
un:usa  final/      278 cells  (same)                    Denver covered = FALSE
```

**Mechanism:** `helpers.select_h3_cover_geometry:652` *prefers the convex hull*
for area geometries — "making H3 polyfill substantially cheaper for complex
polygons". The USA's hull spans Alaska to the Pacific territories and therefore
**crosses the antimeridian**, so the fill goes the long way round: it covers the
wrong hemisphere and misses the interior. That explains every observation — the
cover spanning lon −179.99…179.94, *more* r4 cells than a fresh run (9,960 vs
7,294: wrong area, not smaller), Denver uncovered at every resolution, and
`crosses_antimeridian` returning **False** when asked about the *polygon*, because
it is the **hull** that crosses. `un-countries.py` computes the extract's cover
inline from the real geometry, which is why `extract/` is correct.

**Production is NOT affected, and S8 has now measured BOTH sides** — the live
index holds the *correct* cover:

```
LIVE    un:usa  376 cells  12,942,667 km²  Denver ✓ NYC ✓ Anchorage ✓ Honolulu ✓ Guam ✓ limuw ✓
STAGED  un:usa  278 cells  17,146,048 km²  Guam ✓ — everything else ✗
        (across all 247 staged un docs, 58,945 cells, NOTHING covers Denver or Auckland)
```

Three things follow. **`limuw` IS covered by the live cover**, so tier 1 with a
correct cover resolves S8's 15 — an independent confirmation of the diagnosis.
**It explains the July run's 9.35 M resolutions**: tier 1 worked then because the
cover was correct then; it has been inert only since 31 Aug. And it hands us a
**discriminating validation gate that exists today** — any recompute can be
checked against production's known-good cover rather than judged by eye.

⚠️ **Attribution, corrected: this is MY run, not 2.1's, and the difference is a
timezone.** S8 placed the bad cover at "an h3 run at 10:58, inside 2.1's window,
after S2's geom restore", and read my `un-final` run's *UTC* id as 14:57 —
concluding it "left nothing newer". pitt reports **EDT (UTC−4)**, and my manifest
`un-final-20260831T145706Z.json` has mtime **10:58:20**. The 10:57–10:58 stamps on
`h3/`, `h3_merged/` and `final/` **are** my chain's four stages. **2.1 is not
implicated at all** — S2's geom merge at 03:37 was correct and remains correct.

#### ✅ PRODUCTION `osm`/`ohm` ARE CLEAN — and "affected" needs two numbers, not one

**Measured against the LIVE index** (job 11103617 + targeted `_mget`; alias
`places → places_h3ccode-20260805t120000z`), at ≥400-cell covers:

```
osm   30 tested   30 MATCH   0 DIFFER
ohm   30 tested   30 MATCH   0 DIFFER
staged cover == live cover for the same 60 features:  60 agree, 0 differ
```

The store was asserted open (**11,768,864 entries**) with a control fetch of
`un:usa_0` before any comparison ran, so a dead reader could not masquerade as a
corpus-wide difference — the instrument's own exposure to the defect it measures,
guarded.

⚠️ **This corrects MY premise, not S9's.** I redirected the pass to the live index
arguing staged was a poor proxy. For `osm`/`ohm` staged and live **agree on all
60**. The real distinction is not staged-versus-live in general — it is that
**`un` and `nl` were rewritten by the broken h3 runs and `osm`/`ohm` were not.**

⚠️ **REFRAMING — the test detects whether a cover is WRONG, not whether it was
HULL-DERIVED, and this plan has conflated them throughout** (S9). A hull-derived
cover that happens to equal the polygon-derived one **is not defective** — it is
the correct cover, arrived at cheaply. So the "false negatives" in the calibration
were never missed defects; they are features where hull substitution **made no
difference**. **Record affected-ness as two numbers:**

```
              provenance (hull-derived)     materially WRONG
un            100%                          ~96%   (244 tested, 9 coincidental)
nl            100%                          ~83%   (400 tested, 67 coincidental)
```

**Only the second sizes the remediation; only the first explains the cause.**
Supersedes "read `nl` as wholly affected" — its provenance is wholly, its effect
is ~83%.

**`kain_par`, `vob_lgd`, `vob_rd`: zero features with a ≥400-cell cover** across
23,177 / 9,765 / 4,418 geometries — so no ≥400-threshold sample exists for them.
⚠️ **But see the threshold note below: under the wrongness framing they may be
testable at any cover size.**

#### 📊 BLAST RADIUS — CALIBRATED (job 11103195). Third and current version

⚠️ **Supersedes the `selfEXCL` table AND the 18-sample fresh-vs-stored table. Do
not quote either.** Each revision has moved toward caution; this one retracts a
"clean" verdict I recorded in bold.

**The instrument summary — read before any row:** *every DIFFER is real; a MATCH
is informative only above **≥400 cells** and meaningless below ~25; neither
instrument can clear a namespace; and **a table of MATCHes without cover sizes
cannot be read at all**.*

⚠️ **This line previously said "~100 cells"** (Auditor F5) — the pooled figure the
section below then retracts. **≥400 is the conservative threshold**; ≥100 gives up
to 7% false-negative in the worse of the two calibration namespaces. Where later
text commissions "a targeted ≥100-cell pass", read **≥400**.

**How the threshold was measured rather than guessed.** `un` and `nl` are
independently proved **100% hull-derived**, so **every MATCH within them is a
false negative by construction** — the match rate per cover-size bucket *is* the
false-negative rate at that size:

```
cover size   tested   MISSED(FN)   false-neg rate   verdict
       1-4        5            5           100%     MATCH meaningless
       5-9       14           14           100%     MATCH meaningless
     10-24       20           14            70%     MATCH meaningless
     25-99      132           27            20%     MATCH weak
   100-399      365           16             4%     MATCH informative
     400+       108            0             0%     MATCH informative
```

Monotonic, and it explains S8's `namarin`: 7 cells, a 30-part MultiPolygon at 55%
of its hull area, matching anyway. A coarse cover cannot disagree.

⚠️ **BUT the pooled curve hides a real disagreement between its only two inputs —
do not quote "≈100 cells" as a constant** (S9, correcting itself after S8
cautioned the curve might not transfer between namespaces; it does not). Split out
at identical cover sizes:

```
cover size    un false-neg      nl false-neg     ratio
     10-24    3/8    = 38%      11/12  = 92%      2.4x
     25-99    2/42   =  5%      25/90  = 28%      5.6x
   100-399    0/142  =  0%      16/223 =  7%       —
     400+     0/48   =  0%       0/60  =  0%       —
```

`nl` misses **two to six times as often as `un` at the same cover size**. So:
**≥100 cells** gives ≤7% false-negative in the *worse* of the two, not the 4% the
pooled figure implied; **≥400 cells is the only band where both reach 0%** and is
the conservative threshold; below 100 stands as meaningless either way.

**Why this is signal rather than noise:** the curve is cover granularity against
shape complexity, and `un`'s countries are far larger and smoother relative to
their covers than `nl`'s territories. A namespace's position on the curve is a
property of **its own geometry** — which is exactly S8's caution, and means
`osm`'s administrative geometry could sit anywhere on it. **The curve is the right
instrument; two namespaces is not enough to make it universal.**

**Every namespace re-read against that threshold:**

```
ns                 tested  DIFFER  MATCH>=100  MATCH<100   reading
whg                    45      35           1          9    DEFECTIVE (78%)
un.bnda-baseline       45      45           0          0    DEFECTIVE (100%)
clio                   45      10          33          2    DEFECTIVE (22%)
kain_par               45       0           0         45    UNINFORMATIVE — no information at all
vob_lgd                45       0           0         45    UNINFORMATIVE — no information at all
osm                    45       0           1         44    barely evidenced
vob_rd                 45       0           1         44    barely evidenced
ohm                    45       0           3         42    barely evidenced
pl                     45       0           8         37    weakly evidenced
vob_cty, hgis, vob_rc, po, ukhc   37-43 matches >=100   see caveat below
```

⚠️ **"Genuinely clean" is stronger than the data licenses** (S9's own retraction).
Those five were tabulated against the **pooled ≥100** line; under a ≥400 line each
would shrink by an unrecorded amount, so the accurate phrasing is **"well
evidenced at ≥100, unquantified at ≥400"**. They remain the best-evidenced rows.
A ≥400 re-tabulation is a cheap re-run of part 2 with a second threshold column —
offered, not yet run, and it does not gate anything.

⚠️ **RETRACTED: "`osm` and `ohm` are CLEAN".** I recorded that in bold on
18/18 MATCH. At 45 samples `osm` has **1** informative match and `ohm` **3** — the
rest sit below the threshold where a match means nothing. The correct statement is
**"not shown to be defective, on very little evidence"**. `kain_par` and
`vob_lgd` are worse: **zero** informative matches, so those rows carry **no
information whatever** despite reading 45/45 MATCH. S9's own words: *that is the
same over-reading of a zero I have been warning about all day, and I did it to my
own table by not stratifying.*

**What strengthened:** `whg` is far worse than the 12-sample figure suggested —
**35 of 45 differ (78%)**, up from 9 of 12. `clio` holds at ~22% on the larger
sample. `un.bnda-baseline` is 45/45.

**Recompute set unchanged in membership — `un`, `nl`, `clio`, `whg` — but `whg` is
now known badly affected rather than marginally.**

⚠️ **The unresolved namespaces do NOT gate the retile — and the consumer that
actually matters is worse.** S8 flagged that "`osm` and `ohm` are the largest tile
buckets and unresolved is not a state to retile from". Checked: **`generate_tiles.py`
contains no reference to `h3_cover` or `h3_centroid` at all.** The tile builder
takes geometry from the geom store via `geom_ref`; covers are not a tile input. **S5
is not blocked by this.**

But `h3_cover` *is* consumed by **`gateway/spatial.py`, `search.py`,
`reconcile.py` and `es_helpers.py`** — the **live fuzzy-containment path** — plus
`clustering_payload.py`. So the exposure is **search-side, not tile-side**: a bad
cover in the live index degrades `containment=fuzzy` for those features, which is
the `un`/`limuw` failure arriving as a user-facing wrong answer rather than a
staging defect. And because the live index was built from these staged trees, a
staged cover that is bad for `osm`/`ohm` implies a live one that is too.

**That makes S9's targeted ≥100-cell pass more important than "before the retile",
not less — it is a question about production search correctness, and it has a
different owner and priority from 2.7.** It remains genuinely NOT a blocker for
2.7 or for S5.

**Open, and explicitly NOT clean:** `osm`, `ohm`, `kain_par`, `vob_lgd`, `vob_rd`
are **unresolved**. Settling them needs sampling that *deliberately targets
features with ≥100-cell covers* rather than sampling uniformly — most of their
area features are small, which is precisely why uniform sampling learns nothing
about them. That pass has not been run.

#### 🔴 `nl` MUST BE RE-RUN — its covers are hull-derived too, and its clearance was false

**S8 retracted its own clearance after the field-path correction, and the
consequence is larger than the retraction.** Its `nl` artefact was cleared on the
premise "`has_geom` is unset, therefore the store is never consulted, therefore
the hull fallback cannot apply". The premise was a **field-path error** —
`has_geom` and `geom_class` are **per-geometry**, not document-level — so the
clearance was false, and re-testing three ways shows the signature:

```
samish       stored 119 = hull 119  IDENTICAL    fresh-from-store 103
ngati-rehua  stored  74 = hull  74  IDENTICAL    fresh-from-store  82
limuw        stored  55 = hull  55  IDENTICAL    fresh-from-store  55 (DIFFERENT SET)
```

Verified independently here: `nl:territory:samish` is **103 cells live, 119
staged, not identical** — production holds the polygon-derived cover, the staged
tree holds the hull one. **`nl`'s `h3_merged/` and `final/` are both hull-derived
and must be RE-RUN, not re-verified.** That is a fourth stage of the chain to
redo, and S8's `nl` ccode losers may change once the covers are correct, so the
re-run becomes a genuine re-test rather than a confirmation.

**The recompute list is `un` AND `nl` — and the correct general statement is
wider than either** (S9, and it asked for the wider one to be carried): **every h3
run through `submit_h3_slurm` before `6ad2640` is suspect.** That submitter lacked
the conda export until then, and S8's `nl` run (11097899, *today*) went through it.
So this is not "confined to `staged/un` and what has read it" — that was my
framing and it was too narrow. Any namespace h3'd through that submitter, on any
node where `import sqlite3` failed, could carry a hull-derived cover.

**Production remains unaffected as far as anyone has shown**, and the evidence for
that got stronger: prod's `samish` is 103 and `ngati-rehua` 82, matching
fresh-from-store, so **prod holds polygon-derived covers while the staged tree
holds hull-derived ones**. That extends the validation gate — production is a
known-good reference for more than `un:usa`.

⚠️ **S8's discriminator was invalid and it is worth knowing why.** It compared
*counts* against production, saw "three match, three differ in both directions",
and read that as polyfill drift. **Counts are not sets** — `limuw` matches
production at 55 and is a *different* 55 — and the differences were exactly
hull-versus-polygon. A count comparison cannot distinguish those two worlds; the
three-way test (stored vs fresh-from-store vs from-hull) can, and does.

⚠️ **Stale documentation corroborated the wrong reading, and should be fixed.**
`helpers.compute_h3_fields`'s docstring (`:680`) still says these are
"**top-level** fields on the place document (not nested inside `geometries[]`)"
and shows `doc["h3_cover"] = h3_cover`. That is wrong for the current schema.
Anyone checking S8's reading against the docstring would have had it confirmed.
**Documentation that agrees with a wrong reading is worse than none** — a
✅ **FIXED 1 Sep (`56b947b`)** — the docstring now states the fields are
**per-geometry**, shows `geom_entry[...]` rather than `doc[...]`, and records *why*
it was wrong, since this plan's own finding is that documentation agreeing with a
wrong reading is worse than none. Found still-live by the Auditor's batch-3
code-claim check.

#### 🔴 THE TRIGGER — the same missing `LD_LIBRARY_PATH` export, failing silently instead of loudly

**S9 established the code takes the *good* path today and could not explain why my
run took the fallback. This is why, and it is the same defect S8 was blocked by.**

The complete chain, every link verified:

```
my un_final_chain.sbatch      conda activate whg; cd /vast/ishi/elastic     ← NO LD_LIBRARY_PATH export
geom_store:47                 import sqlite3
                              → ImportError: GLIBCXX_3.4.30 not found       (the S8 blocker, exactly)
h3_stage:46-51                try: from processing.geom_store import GeomStoreReader
                              except Exception: _GeomStoreReader = None     ← swallowed
h3_stage:60-67                if _GeomStoreReader is not None: ...          ← never constructs a reader
h3_stage:79   cover_geometry_for   if reader is not None and has_geom: ...  ← skipped
h3_stage:90                   return select_h3_cover_geometry(geom, hull)   ← HULL, silently
```

**Corroboration:** my job's log contains **no `geom-store: opened …` line at all**,
which is precisely what this path produces — the reader is never constructed, so
it never announces itself. And S9 confirmed that today, in a working env, the same
code yields the correct 376 cells.

⚠️ **Three stacked bare `except Exception`s** — the import (`:49`), the reader
construction (`:64`), and the lookup (`:82`) — each turning *"the authoritative
polygon is unavailable"* into *"use the hull"* without a word.

**The two halves of today's campaign are one defect.** S8's ccode submitter and my
hand-written h3 sbatch both lacked the export. **S8's failed loudly at import and
cost an hour. Mine failed silently, produced a plausible artefact, and cost the
`gn`/`wd` hold plus everything since.** Same missing line; the difference between
them is entirely whether the code swallowed the error. That is the argument for
S9's part 1 in its strongest form.

⚠️ **Labelled as a strongly-evidenced hypothesis, not a proof:** I have not
re-run `h3_stage` on a node without the export to reproduce it. That test is cheap
and would settle it — and S9's part 1 makes it unreproducible regardless, which is
why it should land without waiting for the trigger to be confirmed.

**S9's reframing of the fix is right and I would not soften it.** The defect is not
"we prefer a cheap approximation" — it is a **silent degradation to a known-inferior
geometry for a feature explicitly marked `has_geom=True`**, which then reports
success. `has_geom=True` is a *promise* the real polygon is retrievable; when the
store cannot honour it, the correct behaviour is to fail loudly.

1. `cover_geometry_for` must **not** substitute the hull when `has_geom` is true
   and the store lookup fails — that is an error condition, not a fallback.
2. Where the hull genuinely is the only geometry, **refuse it when it crosses the
   antimeridian and the polygon does not** — there it is not a coarser
   approximation but a wrong one.

**And the stale-clone lead is REFUTED, not merely unverified** (S9): the reflog
shows the clone sat at `177ba72` from 03:41 to 16:57 EDT, and `177ba72` contains
`cover_geometry_for` (afab7d6), the antimeridian fix (627ae79) and the outward
dilation (b764941). Right code, populated store, wrong output.

#### ⚠️ The live cover IS the extract cover — `h3_stage` had never run on `un` before my chain

S8 raised a constraint on the hypothesis: the live `un:usa` cover is correct
*despite* `select_h3_cover_geometry`'s hull path being live, so either `un:usa`
does not take the hull branch in prod or the branch is not unconditionally wrong.
**Tested, and it is neither — the live cover never went through that branch at
all:**

```
live cells       376
extract cells    376        live == extract    TRUE   (identical sets)
h3_merged cells  278        live == h3_merged  FALSE
```

The live index's `un:usa` cover **is** the extract's, computed inline by
`un-countries.py` through `compute_h3_fields` on the real geometry. `un` was
indexed from an extract-derived cover and **`h3_stage` had never run on it** —
consistent with `un` being the namespace whose stages get skipped (Fault 12). My
chain was the first time that code touched `un`, which is exactly when the cover
broke.

**This resolves S8's constraint in the worse direction.** The hull branch is not
conditionally safe; it simply had not been applied here before. And every
namespace that *did* go through `h3_stage` in the rebuild carries a hull-derived
cover — which is most of them.

**Bounding it, without overstating:** the exposure is features whose **convex
hull** crosses the antimeridian, not merely large ones. For a city, county or
parish the hull does not cross; it takes a country-scale multi-part geometry
spanning the Pacific (US, RU, NZ, FJ, KI). Those are common in `un` and rare
elsewhere — but "rare" is a prediction, not a measurement, and **which namespaces
hold such features is exactly the open corpus-wide question**, now sharper and
more pressing rather than less.

⚠️ **Run ids are UTC (`…Z`); the hosts report EDT (UTC−4)** (S8, and it cost it a
confident wrong causal chain in under a minute). **Every artefact in this campaign
is named in one frame and stamped in the other.** Convert before reasoning from
timestamps, and prefer comparing an artefact's mtime against another mtime rather
than against a run id.

**S8's lesson stands, correctly aimed at my chain rather than S2's step:** *a step
that rebuilds an input must verify what is derived from it, not only the input.*
2.1 was scoped to geometries and verified geometries. My chain recomputed the
cover and verified resolver-depth, doc count and ccodes — never the cover.

⚠️ **This is mine, and the way it got past me is the campaign's own lesson.** I ran
the chain, and verified it with three checks — resolver picks `final/`, 247 docs,
ccodes present — and recorded it as "verified independently of the job's own
report". **Not one of those checks looked at `h3_cover` content.** I verified the
properties I thought to name; the one I did not name is the one that broke. The
chain did exactly what the code says, so this is a latent code defect my run
*triggered* rather than one it invented — but the bad cover reached `final/`
because I put it there and did not look.

**And I mistrusted a correct result.** My Denver control failing was not a broken
test — it *was* the defect, observed correctly. I inferred "control failed,
therefore my test is wrong", when a failing control means either the test is wrong
**or the defect is wider than hypothesised**. I assumed the first and labelled a
true finding a dead end. S9 reproduced it independently through the production
`build_un_prefilter` path before forming any hypothesis.

**Remedy is clear; durability is not.** Recomputing `un`'s `h3_cover` from the
geom-store polygons makes every test point resolve. Whether anything still
*generates* a bad cover — and which other namespaces' covers are hull-derived
across the antimeridian — is open, and determines whether a recompute is a fix or
a reset. **`select_h3_cover_geometry`'s hull preference is the thing to look at.**

#### Tier 2 REFUTED my prediction; the exact answer was available all along

**S8 tested rather than accepting, and my proposed chain change would not have
worked.** `backfill_uncoded_ccodes` on `nl` (job 11102424, S9's repaired tool):
`scanned=4,363 uncoded=15 no_geom=0 **RESOLVED=0**`. It *had* their polygons and
placed none. So `… → ccode_merge → backfill_uncoded_ccodes` recovers nothing, and
2.7's chain does **not** gain that step.

**The decisive test — the country polygons DO contain them.** Read from the geom
store with `shapely.prepared.prep(...).contains(repr_point)`:

```
nl:territory:limuw       (-119.709985,  34.014690)  un:usa_0.contains = True
nl:territory:manissean    ( -71.572870,  41.227198)  un:usa_0.contains = True
nl:territory:ngati-rehua  ( 175.382163, -36.196741)  un:nzl_0.contains = True
```

**So the exact answer was in the store the whole time, and the loss is in the H3
approximation** — S8's original conclusion, reached on evidence that could not
support it (the `SOURCE_LABEL` constant) and now supported by a test that can.

⚠️ **`backfill_uncoded_ccodes` prints a claim it has not earned.** Its summary line
reads `still uncoded 15 (genuinely outside every country: open ocean, Antarctica)`
— **false for all 15, demonstrably**, since the country polygon contains them. The
tool knows only that *its own tier* placed nothing; it asserts a fact about the
world, and anyone reading it at face value closes the ticket. Should say what it
knows ("not placed by tier 2"). Its own instance of the campaign's pattern.

**My attempt at the mechanism, recorded WITH its failure so nobody builds on it.**
`build_un_prefilter` (`:215`) builds cell→ccode from the **`un` docs' own
`h3_cover`**, not from `un.h3_coverage.json` — so the aggregate's absence is a red
herring for *this* path. Testing whether the country cover contains the islands'
cells: `un:usa` has 278 compacted cells (r0×1, r1×8, r2×70, r3×199), `un:nzl` 296,
and none of the three islands' cells is covered at any resolution present. **But my
mainland control — Denver — also tested uncovered**, which cannot be right when
1,532 `US` resolutions occurred in the same run. **So the test is wrong, or the
country cover is far more incomplete than a working prefilter allows, and I could
not distinguish those two before handing over.** Do not treat "the country cover
excludes the islands" as established; treat the control failure as the next thing
to explain.

**Open, and it is a code-reading job rather than a compute one:** what does the
ccode prefilter actually consult for `un`, why is `un.h3_coverage.json` absent
when `submit_h3_slurm`'s own comment calls it the ccode prefilter, and why does a
mainland point test as uncovered? `nl` is a 4,363-record reproduction that
re-tests in two minutes.

**`gn`/`wd` stay held, and more firmly than before** — this is not an `nl` quirk.
Any small island or coastal feature whose cells miss the prefilter is exposed, and
`gn` is a global gazetteer full of them at 99.94% live.

⚠️ **A hard stop-line, since the projection is a projection.** Abort and clean up
if `/vast` free drops below **100 GB** — well above the ~51 GB at which the volume
goes flood-stage read-only, leaving room to recover rather than discovering it at
the boundary. And S8's checkpoint is the right shape: **measure `gn`'s real
`update_merged/` after `update_merge` and before `h3_stage`, and stop if it is
materially above projection** rather than finding out at `final/`. That number is
the least certain in the table — 26.7 M alternate names is a content addition with
no analogue among the measured namespaces, all of which had their patch already
merged or none to merge.

**Cost, revised:** `update_merge` on `gn` collapses a 1.4 GB patch into a 7.38 GB
snapshot *before* h3 starts. 2.7 is materially larger than first estimated and
S5's unblock is further away than anyone had assumed.

⚠️ **A fourth verification is required**, because none of the three would catch
the bypass route: **assert the `gn`/`wd` `final/` actually contains update-patch
content.** ⚠️ **It must be a NAME count, not a document count** (S5, sharpening
S8's version): `gn`'s ~26.7 M alternate names are *names*, not documents — a `gn`
doc with and without its alt names is **one document in both worlds**. So row
count, ccode coverage and every document-level measure read identically whether
the patch landed or not. Count names, or spot-check specific records the patch
adds.

**The original chain, correct for `nl` only:**

```
h3_stage → h3_merge → submit_ccode_slurm → ccode_merge → final/
```

`ccode` requires a **completed `h3_merge`** (`_pending_namespaces`), so the order
is not negotiable. `ccode_merge` is the only writer of `final/`.

⚠️ **Do NOT run `processing.apply_ccode_patch`.** It writes the **LIVE index** in
place. This step's entire output is `final/` on `/vast`; production already has
these ccodes and needs nothing from it. The patch this step produces is an input
to `ccode_merge`, not to prod.

⚠️ **Set `--wall-hours` explicitly.** `estimate_wall_time_seconds` medians past
runs, and its own docstring records why that misleads here: the
BNDA→geoBoundaries move took country outlines from 232 to 73,663 vertices and
made ccode roughly 5× dearer. This is Fault 13, which already killed `clio` and
`ohm` mid-run on a stale median. `--namespaces gn,wd,nl` exists for the same
family of reasons and should be used rather than relying on eligibility.

⚠️ **Confirm the shared clone was pulled** (`indexing-7e` was asked to; verify
rather than assume — it reports itself current when it is not).

**Cost, honestly:** `wd` alone is ~11.5 M documents needing spatial ccode
resolution, comparable to the whole of the previous corpus-wide pass. Hours, and
plausibly many of them. `gn`'s h3 stage streams a 7.4 GB JSONL. Size generously;
Slurm wall time is a ceiling, not a reservation.

**Verify — and note what 2.5's check could not see.** Row counts alone are
satisfied at `extract` depth exactly as at `final` depth; that is precisely how
this defect survived a PASS. So check all three properties:

1. **Stage depth** — `submit_tiles_slurm`-side: `final/places.{parquet,jsonl}`
   exists for each. And resolver-side, using the pipeline's own function, that
   `_staged_namespace_source(ns)` now returns a path under `final/`. **This runs
   on pitt** — use `/home/gazetteer/miniconda/envs/whg/bin/python` (pyarrow 25.0.0). ⚠️ **"Imports the pipeline" is too
   broad — narrowed by S8, 31 Aug.** `processing.generate_tiles` and
   `processing.index_from_stage` import fine; `clustering.harvest.hard_links_staged`
   fails with `ImportError: GLIBCXX_3.4.30 not found` via `sqlite3` →
   `libicuuc.so.75` — ⚠️ **but that is a MISSING EXPORT, not a platform limit, and
   recording it as the latter was my error** (corrected 1 Sep after S8 diagnosed
   it). The conda env ships `libstdc++.so.6.0.34` (GLIBCXX to 3.4.34); the system
   `/lib64` one is 6.0.29 (to 3.4.29) and the loader prefers it. With
   `LD_LIBRARY_PATH=$CONDA_PREFIX/lib` the same import returns **IMPORT OK** —
   verified on pitt. Nothing is off-limits on that host; it just needs the export. Use `generate_tiles`'s or `index_from_stage`'s copy — they give
   identical answers. The `whg` env named in `_common.sh` lives under
   `/ihome`, which is CRC-only and absent on the VM, so looking for that one and
   concluding no env exists is a wrong turn (S8 took it, 31 Aug). Verified
   pre-state via that interpreter: `gn`/`wd`/`nl` → `extract/places.jsonl`,
   `un` → `final/places.parquet` as the control.
2. **Counts unchanged** — 13,454,817 / 11,459,393 / 4,363, delta 0 against the
   live index, so the chain moved the data rather than losing some of it.
3. **ccode coverage in `final/`, as a TWO-SIDED test** — S8 sharpened this on
   31 Aug and the sharpening matters. Targets: `wd` ~97.3%, `gn` ~99.9%, `nl`
   measured at the time. But **`gn` reaching 100% is a FAILURE signal, not a
   success one.**

   `gn`'s ~0.06% residual (~8,000 docs) is overwhelmingly GeoNames *undersea*
   feature codes — the `U` family: `SMU` 1,371, `CNYU` 688, `BSNU` 523, `BNKU`
   487, `RFU` 404, `RDGU` 373, `KNLU` 191, `VALU` 189, plus `GULF` and `RKS`.
   Seamounts, canyons, basins, banks, reefs, ridges and knolls in international
   waters — `gn:80303` Gulf of Aden, `gn:145945` Gulf of Oman. **They have no
   country code because they correctly have none**, and spatial resolution will
   not give them one. So 100% would mean the resolver had assigned countries to
   open-ocean features, which is a real error class that a one-sided `≥ 99.9%`
   threshold waves straight through. **Assert the shape of the residual, not
   just its size.**

   ⚠️ **Sample without bias.** S8 nearly filed a false alarm here: head-only
   sampling gave `gn` 100.00% and tail-only 65.83%, which read as the plan's
   figure being a head-sampling artefact and `gn` needing ~4.6 M extra spatial
   resolutions. Random-offset sampling (seek, discard the partial line, take the
   next) gives 99.94%, agreeing with the live index. The tail was the
   unrepresentative end. `wd` agrees with itself at 0.12% across all three
   methods. **Sizing is therefore unaffected — `wd` remains the entire cost.**

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

🛑 **GATE — 2.7 MUST BE COMPLETE BEFORE ANY OF THIS.** Added 1 Sep (Auditor F6),
deliberately *outside* the numbered list: a gate that blocks the whole step is not
one trap among four, and as a list item it renumbered and broke its own
cross-references. This list named 2.1 and 2.5 only, and every item in it is
currently satisfied — so a cold S5 would conclude it may proceed. **It may not.**
§2.7's heading is "GATES S5": `gn`/`wd`/`nl` have no `final/`, so the submitter
would silently drop them. 2.5 restored the *data*; 2.7 restores the *stage depth*.

Preconditions and traps:

1. **2.1 must be done.** With `un` at 0 in the store, retiling it replaces the
   country boundaries with points — the §2 failure, repeated.
2. ⚠️ **WAIT FOR 2.5 — necessary but NOT sufficient; see the 🛑 gate above.** `gn`, `wd` and `nl` must be restored and counted
   (13,454,817 / 11,459,393 / 4,363, delta 0 against the live index) before you
   tile. Check with the pipeline's own resolver rather than by looking at the
   directory: every one of the six stage-preference chains tests
   `path.exists()` (4.11), so a 4,925-byte stub satisfies an `ls` perfectly.
   `nl` matters as much as the other two here — it is a tiled bucket, and its
   4,363 polygons are currently in the deployed tileset *only* because the
   7 August run read a staged tree that has since vanished.

   ⚠️ **`TILE_ES_DOC_NAMESPACES=gn,wd` DOES NOT WORK for this, and this plan was
   wrong to offer it** (S5, 31 Aug, verified by grep): the setting is read only in
   `settings.py:158` and `generate_tiles.py:949-951`, and
   **`submit_tiles_slurm.py` never consults it**. It changes where a worker reads
   documents *after* a bucket has been submitted; it cannot make an ineligible
   bucket eligible. A session following the old advice would set the override,
   watch the submitter skip `gn` and `wd` anyway, and get a successful-looking
   run missing both. The only route is a real `final/`.
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

⚠️ **Two named polygon deltas to assert after 2.7, not one.** Beside `clio`'s
+2,986, assert that **`wd`'s tileset gains polygons for ~58,657 documents**
against today's (S5, 31 Aug). That discriminates the way this campaign keeps
asking for: in the broken world — `update_merge` skipped for `wd` — the count does
**not** move, and the difference is visible *before* deploying. Doc counts, ccode
figures and eligibility tests read identically either way, which is what makes
this the first assertion on S5's row that can actually fail.

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
| 4.2 | ⚠️ **`og`'s CODE is fixed (`4b1f8ca`) but its live hulls are STILL UNSERVABLE** — the fix takes effect only on an `og` re-extract, which nobody has run. Do not read this row as closed. It was **two** missing halves, not one: no `geom_key` *and* no configured module writer, and either alone stores nothing — `enrich_geometry` ignores the key without a writer and discards the polygon without the key. The staging log said "with computed geometry: N" throughout because that counter counts *entries*; it now prints stored polygons separately and warns on divergence, since equality of those two numbers is the only thing distinguishing a stored hull from a discarded one. | **Diagnosed 31 Aug, no longer a mystery tail.** The 2,569 `has_geom=false` docs are two authority bugs: `whg` (1,248 area + 1,072 line) passed `geom_key` but never configured a module writer — **fixed inside 2.3**; `og` (249 area) calls `enrich_geometry` with no `geom_key` at all, so its hulls are never keyed. The geometry was never written, not lost. `og`'s half is a small fix but needs a re-extract, so it sits here rather than in Phase 2 — take it with 4.1. Plus 1 areal doc with no `h3_cover`, unrelated. |
| 4.3 | `authorities/backfill_admin_levels.py` has a broken `BOUNDARIES_INDEX` import; not in `INGESTION_ORDER`, so not a rebuild blocker. |
| 4.4 | `geom_store --merge` grows every rebuild and has no prune step for keys absent from the current corpus. ~~2.3 will add ~229 k more orphans.~~ ⚠️ **Refuted by measurement (Auditor F18):** there were never any old `whg:` keys to orphan — `index.sqlite` held **0** — and 2.3 added **9,849 first-time keys**, not 229 k of garbage. This is the row where a successor would size the prune work. |
| 4.9 | **Make the store cross-check permanent — S2's proposal, run once and answered.** Nobody had ever asked which namespaces claim retrievable geometry the store does not hold; `un` was found by a tile failure and `whg` by S3 reading an authority script, both by accident. I ran it 31 Aug, after S2's merge: **clean — all 15 namespaces claiming areal/line `has_geom=true` have keys**, `un` now 247/247. But note it takes **two** predicates, not one, and S2's framing catches only the first: a namespace whose index *claims* `has_geom=true` with zero keys (the `un` class, silent exact-containment degradation), **and** docs with `geom_class ∈ {area,line}` and `has_geom=false` (the `whg`/`og` class, 4.2 — geometry never written, so the index claims nothing and the first check sees nothing wrong). `processing/audit_rebuild.py` already computes the second and cannot do the first, because it never opens `index.sqlite`. Adding that is small and makes both run on every rebuild instead of waiting for the next accident. **⚠️ The pair is still not complete, and the gap cannot be closed from ES at all** (S2, 31 Aug). `geom_class_of` folds `MultiPoint` into `point`, so a multi-part point feature whose store entry is missing reads `geom_class:point, has_geom:false` — indistinguishable from an ordinary point, invisible to the second predicate. And it cannot be caught by the first either: I checked, and `whg` and `og` carry **0** docs with a `geometries.geom_ref`, because the ref is written only when the store write happens. A never-written geometry therefore leaves *nothing in the index that says it should have existed* — no claim, no ref, no class that differs from a point. S3 measured 690 such coordinates in `whg`: trivial there, arbitrary in a namespace of genuine multi-part point features. **So the third check has to sit upstream, not in the audit:** the static `grep -L configure_module_writer` over authorities that compute geometry, and per-extract reconciliation of geometries *computed* against the `Geometries in VAST store: N` the writer already prints. 4.9 is two of three; state it that way rather than as a closed question. |
| 4.10 | ⚠️ **THE PATTERN BEHIND EVERY FAILURE IN THIS CAMPAIGN — a destructive publish that never compares what it writes against what it replaces.** S3's observation, 31 Aug, verified: `processing/publish_hardlinks.publish` computes `row_count` from the **new** database, writes it into the completion marker, and never opens the incumbent at the target — so replacing a 7,596,959-row overlay with ~100 k lands looking exactly like a clean run. Four instances today alone, all the same shape: this one; `consolidate_geom_store` returning 0 and exiting 0 on a mis-pointed staging directory (S2, fixed in `adc7345`); the 7 August tile deploy pushing `poly=0` for every bucket; and, in May, a two-feature synthetic store overwriting the live geom index. The counter-example S3 spotted is instructive — `h3_stage` raises loudly on the *identical* bad variable three lines later, so the difference is a habit, not a constraint. **The fix is one shape applied in several places: before a destructive write, read the incumbent, compare magnitude, and refuse a material shrink unless overridden by name** (`--allow-shrink`, as `WHG_ALLOW_BNDA_ONLY` now guards `un`). Sites: `publish_hardlinks`, `consolidate_geom_store`, the tile deploy, `index_from_stage`. Note the overlay publish does keep one generation as `.previous`, so this class is recoverable — but only once, and silently consumed. |
| 4.11 | **Stage-preference chains test existence, not content — audited 31 Aug, and it is every one of them.** S4 found the third instance (`h3_stage._extract_stage_dir` returns `update_merged/` on `.exists()`, so a 1-row stub tree would have been fed to H3 while the correct 10.26 GB `extract/` sat unread) and asked whether `generate_tiles` and `index_namespace` behave the same way. They do, and so does the rest: **`index_from_stage._staged_namespace_source:88`, `h3_stage._extract_stage_dir:135`, `generate_tiles._staged_namespace_source:856`, `index_namespace.SOURCE_STAGES:84`, `gazetteer_temporal_extent._staged_namespace_source:106`, and `rebuild_toponyms_index`** — six chains, all `if path.exists()`, none checking that the file it selects holds a plausible number of rows. Each prefers `places.parquet` over `places.jsonl` in the same directory, which is what makes a 4,925-byte stub a poison pill rather than a nuisance. **Fix once, apply six times:** the chain should skip a candidate whose row count is implausible for the namespace, or at minimum log the count and source it chose so a 1-row selection is visible in the log. Related to 4.10 — that one is about not overwriting good data with bad, this one about not *reading* bad data as good. ⚠️ **"Apply six times" is right but "fix once" needs care: the six chains are not the same chain** (S4, 31 Aug). Four are byte-identical — `index_from_stage`, `generate_tiles`, `hard_links_staged`, `gazetteer_temporal_extent` all use `(final, h3_merged, boundary_merged, update_merged, extract)`. Three do not: `rebuild_toponyms_index` omits `update_merged` (defensible — toponyms are populated at extract time and not mutated later); `h3_stage` is per-namespace and tests **directory** existence rather than a file, which is why it was the worst of them; and **`index_namespace` uses `(final, ccode_merged, h3_merged, extract)`** — which has two problems. `ccode_merged` **is written by nothing**: it appears in exactly two places in the tree, this tuple and `staging_orchestrator:58`, both readers, because the ccode stage's output is `final/` (as this plan says in the incremental-add workflow). So that entry is dead, and it makes the chain *look* like it covers the ccode stage when it does not. Meanwhile it omits `boundary_merged` and `update_merged`, which **are** written — so for `osm`/`ohm` with no `final/`, `index_namespace` skips the enriched `boundary_merged/` and falls through to the raw `extract/`, and `--source-stage` cannot even be asked for those stages because they are not in its `choices`. A shared helper must therefore take the chain as a parameter, and `index_namespace`'s wants fixing on its own account, not just refactoring. ⚠️ **And this audit was itself incomplete, in an instructive direction (S5, 31 Aug): it looked at *resolvers* and never at *gates*.** Within the tile subsystem the two disagree. `generate_tiles._staged_namespace_source` (`:856`) is tolerant like the rest and would have streamed `gn`'s and `wd`'s `extract/` perfectly well — but `submit_tiles_slurm._eligible_buckets` is **strict**, requiring `final/` and otherwise bare-`continue`ing. The strictness sits *upstream* of the tolerance, so the bucket is never submitted and **the failure presents as an absence rather than an error**: no log line, no exception, 24 tilesets where 27 were expected. A seventh site with the opposite polarity to the other six, and the reason a "which resolver does it use" sweep could not have found it. The general form worth inheriting: **anything that SELECTS work deserves the same audit as anything that READS data.** |
| 4.13 | ⚠️ **A partial newer stage is preferred over a complete older one, for the whole duration of a merge — and the fix is ~10 lines** (S3/S8, 31 Aug). `h3_merge.run_h3_merge` and `ccode_merge.run_ccode_merge` both `open("w")`ed in place and derived the parquet afterwards, with no `os.replace`, `.tmp` or rename. ⚠️ **Historical — 2.8 fixed this; those line numbers now land on the fix itself** (Auditor C3). Verified 1 Sep: `open("w")` count **0** in all four writers, each calling `atomic_staged_snapshot` ×3. Every consumer walking `final → h3_merged → boundary_merged → update_merged → extract` — `index_from_stage`, `generate_tiles`, `gazetteer_temporal_extent`, `hard_links_staged`, toponyms stage 1 — therefore prefers a zero-byte or half-written newer stage over a complete older one, with no error on either side, for as long as the merge runs. **Proposed fix: write `places.{jsonl,parquet}.tmp` and `os.replace()` into position when complete.** Rename is atomic within a filesystem, so a concurrent resolver sees either the old complete state or the new complete state and never a partial one — the existence-only preference becomes safe *by construction* rather than safe by every session remembering to serialise, and it protects every future rebuild rather than tonight's overlap. Sized at ~10 lines across two files — **and the pattern already exists in this repo**: `repair_staged_docs.py:247` does `tmp_jsonl.replace(jsonl_path)` for exactly this reason, so the fix is applying an in-house precedent rather than introducing one. **The strongest form of the argument, from S8 and stronger than it knew:** if anyone counters that the zero-byte window should be fixed **reader-side** (skip empty files, check completeness), that is not one edit — `_STAGED_SOURCE_PRIORITY` is defined **five** times, byte-identical, each with its own `_staged_namespace_source`: `index_from_stage:71`, `generate_tiles:145`, `aat_enrich:67`, `gazetteer_temporal_extent:54`, `hard_links_staged:42`. S8 found four and missed `aat_enrich`, which rather makes the point. Five edits across two packages that must stay in sync forever — and one of them, `clustering.harvest.hard_links_staged`, needs `LD_LIBRARY_PATH=$CONDA_PREFIX/lib` to import at all on the VM. ⚠️ **This argument originally said that module "cannot even be imported on the VM to test", which was wrong** — it is a missing export, not a platform limit (corrected 1 Sep). The conclusion stands on the remaining reasons; one of its supporting facts did not. Precisely the true-conclusion-false-premise shape recorded elsewhere in this plan. The **writer-side** fix is two edits, needs no coordination, and matches `repair_staged_docs.py:247`. So it is not merely the tidier option, it is the only one with a maintainable surface. (Hoisting the resolver into one shared module is also right and is deliberately **not** part of 4.13 — that is scope creep on a fix that has to be small enough to land mid-campaign. Its own row later.) ✅ **PROMOTED to step 2.8 (S9) by SG, 1 Sep** — landing before 2.7 rather than managed by serialisation. This row is retained for the reasoning; the work is 2.8. |
| 4.12 | **"Published" does not mean "live" for the overlay** (S3/`indexing-7e`, 31 Aug). `publish_local` is an atomic same-filesystem `os.replace`, and its own docstring records that **the gateway's open descriptors against the previous inode stay valid until it re-opens**. So a successful overlay publish changes nothing any user sees until the gateway restarts — and restart ownership is a separate session's. A publish step that silently has no effect is worth naming, because the natural reading of "published" is that it is serving. Pair the publish with the restart, or say plainly in the record that the new overlay is on disk and not yet in use. |
| 4.14 | ⚠️ **761 `clio` features whose stored `h3_cover` does not contain their own `repr_point`** (S9's probe; framing by S8, and the framing is the contribution). This is **not** the antimeridian defect — `hullX = 0` for all of them. It matters because it is not a data oddity but **761 counter-examples to an invariant two subsystems are documented as relying on**: `gateway/spatial.py:11` and `:900` ("Cheap, exact fast-reject: `repr_point` is guaranteed within the…"), `ccode_enrichment.py:518` ("`repr_point` is guaranteed to lie within the geometry"), and CLAUDE.md at `:359` and `:664`. If the guarantee does not hold, `containment=exact` can return a wrong answer for those features and the ccode refine can discard a correct candidate — **and neither would look like an error**. Triage as a correctness question, not a curiosity. No explanation offered by anyone yet; `clio` being one of the nine point-only boundary layers may or may not bear on it. |
| ~~4.15~~ | ❌ **REFUTED — do not record. `nl` CAN serve as a `contained_in` scope.** S8 reported that all 4,363 `nl` docs carry `has_geom=None` and `geom_class=None` in both its `final/` and the live index, so no Native Land territory could be used as a scope region. **Checked on both sides and it does not hold.** Its own `staged/nl/final/places.jsonl` reads `geom_class='area'`, `has_geom=True`; and in the live index `exists` returns **4,363 of 4,363** for each of `geometries.geom_class` and `geometries.has_geom` (ES `exists` is false for nulls, so they are set). Kept as a struck row rather than deleted, because a plausible user-facing defect that does not exist is worse in an audit than no row at all — and because whatever S8 read to get `None` is worth knowing (a parquet sidecar, or nested `_source` filtering, both of which can present set fields as absent). |
| 4.5 | AAT coverage 4,436 / 15,448 = 28.7% (place#142). |
| 4.6 | ⚠️ **PROMOTED 31 Aug from housekeeping to REQUIRED — and CORRECTED: this is S5's work, not a separate migration.** My first write-up filed the `whg-*` tilesets with the genuinely legacy `datasets-*` / `collections-*` family. They are not: `generate_tiles` builds them natively as **per-WHG-dataset buckets** (`_whg_dataset_sub_ids`, `whg-<dataset_sub_id>.mbtiles`, "one per contributor dataset discovered at submit time"), and the current 47 were produced by that same pipeline on 22–23 July. So S5 rebuilds them by naming those buckets — no separate project, no migration. The `datasets-*` / `collections-*` tilesets are the actual legacy family and stay in `plan-outstanding-2026-07.md` §8. S3 flagged that the `whg` tiles now carry dead place ids after 2.3's re-mint, and expected §3.1's 27-bucket retile to cover it. It does not: **there is no `whg` bucket**. `whg` is served as **47 legacy per-dataset tilesets** (`whg-<dataset_id>.mbtiles`, 22–23 July), which sit outside the 27 and are untouched by 3.1. Verified by decoding `whg-1052.mbtiles`: it carries `whg:1052:6954924`, `whg:1052:6954927` … — the old place-key form, which after 2.3 returns `found:false`. So **every click-through from those 47 layers is now dead**, and regenerating them is the completion of 2.3. **Add the 47 `whg-*` buckets to S5's run.** |
| 4.7 | Merge stages still hold whole patches in memory; the allocations are tiered, the profile is unchanged. |
| 4.8 | **41 of the 89 datasets referenced by contributor links are not in the index** (48 are). `contributor_replay` accepts `ds_status ∈ {indexed, accessioning, wd-complete}`; ingestion requires `Dataset.authority=True AND public`. 2.3's id map makes the mismatch harmless and visible, but the underlying question — publish them, or narrow the replay filter to match? — is a Django-side call for SG. |

---

## Critical path

```
S9  2.8 atomic merges  ──┐   (SG, 1 Sep: before 2.7, not alongside it)
S8  2.7 gn/wd/nl final/ ─┴─→ unblocks BOTH the overlay publish (2.3) and S5's retile
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

⚠️ **CORRECTED 1 Sep — this paragraph was dangerously stale (Auditor F1).** It
read: *"Only S2 gates the retile — S4 does too, but only softly (`gn`/`wd` can be
read from the index with `TILE_ES_DOC_NAMESPACES` instead)"*. **Both halves are
now false.** `TILE_ES_DOC_NAMESPACES` **does not work** — `submit_tiles_slurm`
never consults it (§3.1) — and **S5 is BLOCKED on 2.7**, not on S2. A session
reading this summary for a green light would have run the 24-bucket silent-drop
retile. S1, S2, S3 and S4 are
otherwise mutually independent and may run concurrently.

⚠️ **"S2 → S5 alone is a valid short path" was true when written and is now
FALSE** (Auditor F1). **S5 is blocked on 2.7**, which is blocked on the `un`
recompute. There is no short path to the retile.

---

## Closure record — 31 August 2026

Four sessions closed on SG's instruction, each having given a closing statement
verified against the tree and the hosts rather than from memory. No session can
terminate another, so this is the record; SG closed the terminals.

**Nothing is in flight anywhere except Slurm 11074352 (`extract-gn`)**, which
`indexing-5e` watches. Every other job of every session is terminal and read; no
monitors, no background shells, no uncommitted edits. `main` carried one unpushed
commit at closing (`c730d32`), which three separate sessions each declined to
push because it was not theirs — the right call, and worth recording as the norm.

| session | completed | left behind |
|---|---|---|
| **S1** `indexing-81` | 1.1, 1.2 (`f94b8b8`); 2.2 verified on prod (`4286a0f`, `177ba72`); 2.4 with a test (`0c2819c`) | Nothing outstanding. Three follow-ups nobody had written down — see below |
| **S2** `indexing-ab` | 2.1: `un` 247/247 in the store, 0 bounds mismatch; the refuse-to-stage guard + the counters that were tracked and never printed (`b05d5b0`, `6a632a1`) | ✅ **RESOLVED, not open** — `staged/un` was given a `final/` (Slurm 11075438). Reads as live outstanding work otherwise (Auditor F7). `staging-parked/` is a deletion queue with a README, inert |
| **S3** `indexing-c7` | 2.3 live and verified on prod: `whg:1052:8` resolves, 0 dangling of 1,935 endpoints; id map; `adc7345`, `42b6e4a`, `1f5aa50` | The overlay rebuild, gated and documented (`d10ef97`, `344c66b`) — **but its publish path has never been executed** |
| **S4** `indexing-2f` | 2.5 two-thirds: `wd` 11,459,393 and `nl` 4,363 restored, both delta 0; the staged census; the parked verifier (`adff6dc`, `dc40957`) | `gn` extracting; 2.6 unstarted. Both finishable without an S4 session |

### What was only in their heads, and is not any more

Each was asked specifically for knowledge that is **not** recoverable from git.
The yield was high enough to justify asking, and these are the items no document
would otherwise have carried:

* **Pushing is deploying, on someone else's schedule** (S1). The relay's
  `gateway-restart` does `git pull --ff-only`, so anything on `main` goes live at
  the next restart by whoever triggers it — your unverified push can ride a
  peer's restart under their name. Verify before you push, or push only when you
  own the next restart.
* **A 2.6 submission today asks for `--time=03:40:24` for work needing >12 h**
  (S4, measured — Slurm 11074461). `estimate_wall_time_seconds` medians the last
  five runs, and all three recorded `rebuild-toponyms-index` runs skipped
  IPA/PanPhon, so the history is poisoned by fast runs of a cheaper job wearing
  the same name. The first correctly-configured run dies at ~30%. Paired with it:
  `--for-retrain` is not a refinement, it **is** the step.
* **Any incremental `geom_store --merge` is a full index rebuild** (S2) — ~5.4 GB
  RSS and ~6 minutes whether you add 247 keys or 9,849. Never size a merge by the
  keys being added; it belongs on Slurm.
* **`hits.total.value` caps at 10,000** unless `track_total_hits: true`, and a
  malformed query returns an error object with no `hits` key that reads as a
  legitimate zero (S1). For `gn` and `wd` that yields two wrong numbers agreeing
  with each other through a query reporting `_shards.failed == 0`.
* **A ready-made live fixture for the degradation path** (S1): `og:10209` is
  areal in the index with **0** entries in the store — exact and fuzzy return the
  same 80 hits. `un:fra` is the positive control (exact refines 73 → 72).
* **A freshness test passes for free unless you backdate** (S1). 2.4's assertion
  compares `final/` against `h3_merged/` mtimes; written in the same second it is
  vacuous. `os.utime` the stale artefact backwards or you are testing nothing.
* **`Errors: 3` in the `un` extract is unexplained** (S2) — identical on 5 and
  31 August, reconciling exactly to 247. Probably the `stscod=99` unnamed
  disputed zones, **not confirmed**. If a fourth ever appears, that reconciliation
  is the thing to re-derive.

### Judgement each offered on its own work

* **S1:** finished. Flagged that its approximate-flag decision for a radial
  `h3-disc` region asked for `exact` was made alone and deserves revisiting if a
  radial-scope consumer appears; and that
  `tests/test_tilegen_bands.py::test_default_style_parses_and_has_expected_buckets`
  fails **only when a sibling `../tileboss` clone exists** — pre-existing, not
  S1's, confirmed against a baseline worktree, and it will surprise whoever next
  runs the full suite.
* **S2:** finished but for the `staged/un` decision it created and declined to
  take unilaterally at closing time.
* **S3:** *"the gate is cold-readable; the procedure past the gate is documented
  but unexercised."* Its harvest was cancelled during Phase 1A, so `loc_links`,
  `finalise_local`, `publish_hardlinks`, the ship marker and the prune were never
  reached. **Expect to debug the publish path, not run it.** ⚠️ **Largely superseded (Auditor F19):** all but the final publish have since run — LOC 1,132 attempted / 1,129 inserted, contributor replay reached DO Postgres unconfigured, `finalise_local` ran, and `1f5aa50`'s renamed drop-ledger fields ran on CRC for the first time. **Only `publish_hardlinks --execute` remains untried.** Asking about
  in-flight state also turned up a stale 16 MB SQLite WAL beside the deleted
  partial database, which a rerun under the same run id would have adopted.
* **S4:** 2.5 and 2.6 are both finishable cold — *"the residual risk is not
  knowledge, it is discipline: both overrides are things you must do, not things
  the tooling will do for you."* Explicitly did **not** clear the 872-document
  gap or `index_namespace`'s stage chain (4.11).
