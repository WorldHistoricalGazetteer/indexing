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
> run against a known-bad input isn't a verification._** ⚠️ **BOTH HALVES,
> corrected 2 Sep: also run it where you know it should PASS.** A check that
> cannot say FAIL is useless; **a check that cannot say PASS manufactures
> confidence, which is worse.** The tile span assertion did both — it rejected
> the known-bad hull at 232.63° **and** flagged six legitimate `un` countries,
> and only the good-input half revealed it. Run each check first
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
| **S9** (`indexing-98`) | 2.8 | All four priority-chain writers made atomic behind one helper, plus tests that fail on the pre-change code. | ✅ **DONE** (`554e43a` + `e37c93b`) — `atomic_staged_snapshot` called in `update_merge`, `boundary_merge`, `h3_merge` and `ccode_merge` (×3 each; `open("w")` count **0** in all four — verified 1 Sep); parquet renamed first then jsonl; cleanup wrapped so a failing unlink can never mask the failure that caused it. Verified here: 17/17 pass, all four call sites on the helper |
| **Auditor** | document audit (not a plan step) | **Read the plan COLD and find every claim superseded by a later one that is not marked as such.** Read-only: no code, no plan writes — reports findings to `indexing-5e`, which makes the edits. Assigned by SG, 1 Sep. | ◐ **running** (`indexing-13`) — reading at a pinned SHA; batching findings highest-risk first |
| **S9** (cont.) | 2.10 | **Diagnose the ccode H3 prefilter** — why small islands whose country polygon contains them are dropped before any polygon test. Code-reading, no staged writes. **Gates 2.7's `gn`/`wd`.** ⚠️ ~~Resolve the mainland-control contradiction first~~ — **ANSWERED**; see §2.10. Questions 2 and 3 only. | ✅ **PURPOSE SERVED 2 Sep** — it gated 2.7's `gn`/`wd`, and 2.7 is complete. The causal chain was demonstrated end to end (`un` hull cover → tier 1 inert → islands dropped; fix it and all 15 `nl` territories resolve). Questions 1 and 4 CLOSED; **2 and 3 remain open but gate nothing and are now UNOWNED, not S9's** |
| **S9** (cont.) | 2.9 | Code-only residuals. | ✅ **DONE** — `a4ada2d` ccode preload (+ position-asserting test), `dbf789f` ukhc backfill (read fix **and** the silent-zero report), `1179664` `_unlink_quietly` narrowness pin, `4b1f8ca` og `geom_key` **and** writer, `3225fc6` symlink spec. Verified here. ⚠️ Resolver hoist still withheld behind 2.7 |
| **S8** (`indexing-c0`) | ✅ **2.7 DONE 2 Sep** + the `un` recompute | Give `gn`/`wd`/`nl` a real `final/`; **and recompute `un`'s cover** (SG, 1 Sep). **Gates S5 and the overlay publish.** | ✅ **DONE 2 Sep** — all four namespaces have a real `final/`; row counts verified independently by the coordinator (`un` 247 / `nl` 4,363 / `gn` 13,454,817 / `wd` 11,459,393). Three residuals recorded, none blocking. **S8 dischargeable** |
| **S5** | 3.1, **plus the 47 `whg-*` buckets** (4.6) | The retile. Prove the verifier FAILS on the preserved fixtures before deploying. ⚠️ Its post-2.7 eligibility re-check is **necessary but not sufficient** — `final/` existing cannot show whether `gn`/`wd`'s update patch landed, because that is a **name** count, not a document count (see 2.7). | ⬜ **BLOCKED on 2.7 (S8)** |
| **S6** | 3.2 | `whg3` — a different repository, so a separate session by necessity. | ⬜ |
| **S7** (`indexing-57`) | 3.3 | Post-retile cleanup. ⚠️ **Scope revised 3 Sep — re-read §3.3 before acting; the original target list predates the second tile generation.** | 🛑 **WAIT** — 3.1 is done, but `/ix1` has a live writer (`indexing-db`, ~250 GB peak) and **both** tile generations must survive. `/vast` half (~40 GB) is the valuable half and is independent of that writer |

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

⚠️ **S8 and S9's session ids were never written down** (found by the Auditor,
2 Sep; verified — `grep` for any of them across this plan returns nothing).
S1–S4 and both S3 sessions are attributed inline; **the two sessions carrying
the campaign's critical path are the two that cannot be found in the map whose
premise is that a successor finds their session in it.** Known for certain and
recorded: **S8 = `indexing-c0`**, **coordinator = `indexing-5e`**, S3 =
`indexing-7e` (and `indexing-c7` for its first session), Auditor =
`indexing-13`. **Now confirmed from the sessions' own statements rather than inferred:
S9 = `indexing-98`, S5 = `indexing-78`.**

⚠️ **And a session's listed state is a statement about an instant, not about
the session.** `indexing-c0` reads "idle" between jobs, which is
indistinguishable from stopped — that misreading produced a false "S8 never
woke" from the coordinator this morning and a mis-identification from the
Auditor this afternoon. **Ask the session; do not infer it from a listing.**

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
| **Gateway restarts** | ~~S1 / S2~~ — **both closed; restart ownership is now S5's** (see §2.3's publish steps). Row retained for the rule, not the assignment. | **One restart owner.** Whichever finishes second performs the restart; the other says in its notes that its change is on disk but not yet loaded. A restart mid-test silently invalidates the other session's verification. |
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
> 🛑 **CORRECTED 2 Sep — read "within a few percent" literally, NOT as a floor.**
> The expected post-2.7 total is **≈7,572,016** (7,596,959 − ~24,943 whg id-map
> drop), which is **0.33% BELOW** 7,596,959 — correct, and *below the number*.
> **A landing at ~7,596,959 would now be suspicious**, not reassuring. The
> discriminating check is **`gn: attempted = 1,111,147`**, not the total.
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
decision. ~~The gate is ~7,596,959 rows; below that, stop.~~ 🛑 **CORRECTED
2 Sep — that instruction would REJECT A CORRECT RESULT.** The gate is a
**TARGET of ≈7,572,016**, not a floor: 7,596,959 is a **post-`update_merge`**
figure (its asserting-source partition sums to exactly 7,596,959, `gn`'s
1,111,147 included, built 6 Aug pre-accident), and the expected post-2.7 total
is **7,596,959 − ~24,943** (the whg id-map drop — *the fix working*).
**~7.57 M is SUCCESS; ~7,596,959 would now be SUSPICIOUS.** ⚠️ **And the total
is the weaker check: the discriminating one is `gn: attempted = 1,111,147`,
because a total can be right for compensating wrong reasons and a per-namespace
count cannot.** Expect the contributor
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

~~**Only `publish_hardlinks --execute` remains untried.**~~ ✅ **RUN 2 Sep — see §2.3 COMPLETE.**

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

##### Re-harvest after 2.7 — COMPLETE. Both predictions exact; the gate's FAIL is one known cause

**Slurm `htc` 11107043, COMPLETED in 01:48:23** (6 h wall, kept at the floor this
time *because* the previous run measured 67 min — same decision procedure,
better evidence). Built to
`/vast/ishi/hardlinks/hard_links_whg-idmap-20260831T071935Z-postmerge.sqlite`.
**Nothing published; the live overlay is still the 6 August build.**

⚠️ **The adoption trap was live in this step's own path.** The *completed*
1,137 MB build from the pre-2.7 run sat at exactly the path
`submit_hardlinks_slurm` derives from this run id. A rerun would have opened it
and `INSERT OR IGNORE`d on top, yielding a plausible total assembled from two
different corpora. Hence the `-postmerge` suffix; the earlier build is kept as
the pre-merge reference. **The trap is not only stale WAL files — a *successful*
previous build at the same run id is the more dangerous form**, because it looks
like exactly what you want.

**Both predictions landed exactly.**

```
gn: attempted=1,111,147  inserted=1,111,147  rejected=0     (was 0)
row_count = 7,572,016                                       (predicted 7,572,016)
```

Phase 1A `attempted 7,568,850 / inserted 7,568,849 / rejected 0` (one duplicate
collapsed); LOC `1,132 / 1,129`; contributors `2,043 fetched / 2,038 inserted`.
Every namespace reproduces the published overlay's **asserting-source** count
exactly. **2.7 is therefore confirmed by an independent downstream route**, not
only by its own report.

###### The gate returns FAIL — and it is a tool limitation, not a data defect

```
TOTAL  7,596,959 -> 7,572,016   delta -24,943   (exactly the whg id-map drop)
gn     5,092,751 -> 5,084,613   -0.2%    restored
wd     7,516,092 -> 7,507,036   -0.1%    restored
```

Eight namespaces flagged — `bnf`, `cerl`, `gnd`, `gov`, `loc`, `tgn`, `viaf`,
`wp`. **They are the counterpart endpoints of the dropped whg contributor
edges**, proved rather than assumed:

| ns | incumbent rows | of which **contributor** | dropped | predicted survivor | candidate |
|---|---:|---:|---:|---:|---:|
| `bnf` | 812 | **812** | 692 | 120 | **120** |
| `gnd` | 1,275 | **1,275** | 1,130 | 145 | **145** |
| `loc` | 1,450 | **1,450** | 1,312 | 138 | **138** |
| `viaf` | 2,112 | **2,112** | 1,919 | 193 | **193** |
| `tgn` | 581 | **581** | 496 | 85 | **85** |
| `gov` / `wp` / `cerl` | 15 / 10 / 1 | **15 / 10 / 1** | 14 / 10 / 1 | 1 / 0 / 0 | **1 / 0 / 0** |

**Each of those namespaces' entire presence in the overlay is contributor
edges** — its total equals its contributor count exactly — so a 92% contributor
drop takes 92% of the namespace with it. Every candidate figure is predicted to
the unit. `gn` −8,138 and `wd` −9,056 are the same cascade, bounded by their
8,312 and 9,245 contributor rows.

⚠️ **Known limitation of `compare_hardlink_overlays`, stated rather than papered
over:** `--allow-shrink NS` exempts the named namespace but cannot know that
dropping a row also removes its **counterpart endpoint**. A future version should
cascade the exemption. **The tool was deliberately NOT patched to convert this
FAIL into a PASS** — altering a gate so it passes the artefact in front of you is
the precise move this campaign exists to prevent.

**Assessment (a recommendation, not a decision): the overlay is correct and
publishable.** Total exactly right, `gn`/`wd` restored, every deviation traced to
the intended whg fix with arithmetic that predicts each figure. **The gate
nevertheless says FAIL, so this step stops here.** 🛑 **The publish is SG's alone.**

###### For whoever publishes

* cutoff **`2026-09-02T14:21:36.274562+00:00`** — the 31 Aug one is **void**;
* ~~`publish_hardlinks --execute` is still the one step in this row never executed~~ ✅ **EXECUTED 2 Sep** — overlay published, gateway restarted, serving verified;
* ⚠️ **a publish is invisible until the gateway restarts** — `publish_local` is an
  atomic `os.replace` and the gateway's open descriptors against the previous
  inode stay valid until it re-opens. Restart ownership is S5's.

###### ⚠️ Correction to the wedge diagnostic this session put into circulation

Earlier I gave S5, S8 and 5e "**CPU time frozen across two samples**" as the test
for a `hard`-mount NFS wedge. **That is incomplete and it produced a false alarm
on my own gate job.** A purely I/O-bound process legitimately shows `00:00:00`
CPU and state `D` while making real progress — `compare_hardlink_overlays`
reading a 1.33 GB SQLite over NFS did exactly that.

**The correct discriminator is that NO counter advances**, with
`/proc/<pid>/io` `rchar`/`syscr` as the primary signal and CPU time secondary:

| | genuine wedge (11091158) | slow but alive (11110966) |
|---|---|---|
| CPU time | frozen `00:08:30` | frozen `00:00:00` |
| `rchar` / `syscr` | **frozen** | **advancing** (+4.3 MB/15 s) |
| `wchan` | `folio_wait_bit` | `filemap_update_page` |
| mount check | `/ix1` **timed out** | `/ix1` **OK** |

`/ix1` read throughput was measured at **~290 KB/s** during that window — slow,
not hung. The fix was to `cp` the incumbent to `/vast` once and compare locally:
the same job then finished in **2:21** against an hour of no progress.

##### ✅ PUBLISHED — 2 September 2026. §2.3 complete

**SG authorised the publish over a FAIL with a documented cause**, having been
given the recommendation and the gate's verdict side by side. Confirmed directly
in the session rather than acted on via relay: a peer session's message is not
the owner's approval, and this was the least reversible action in the campaign.

🛑 **The gate was NOT relaxed.** `compare_hardlink_overlays` is untouched and its
**FAIL stands on record** as a verdict a human overrode with reasons. Changing a
gate so it accepts the artefact in front of it is the move this campaign exists
to prevent; the record should show an override, not an engineered PASS.

```
python -m processing.publish_hardlinks \
    --run-id whg-idmap-20260831T071935Z \
    --db-path /vast/ishi/hardlinks/hard_links_whg-idmap-20260831T071935Z-postmerge.sqlite \
    --cutoff 2026-09-02T14:21:36.274562+00:00 --execute
```

**Verified in the window BEFORE the gateway restart** — `publish_local`'s
`os.replace` leaves the gateway on the previous inode, so there is an interval in
which the new file is in place and nothing is serving it. Use it:

| check | result |
|---|---:|
| published total rows | **7,572,016** ✓ |
| `gn` asserted rows | **1,111,147** ✓ |
| new live inode | `64626` |
| 6 Aug build preserved | inode `59781` → `.previous` ✓ |
| `.incoming` temp | gone — clean atomic rename ✓ |

Marker: `staged/runs/whg-idmap-20260831T071935Z.hardlink_ship.json`.

**Rollback is one `mv`** — `hard_links.sqlite.previous` (inode 59781, the 6 Aug
build) back over `hard_links.sqlite`, then restart. The live file was never
modified in place, so this is a real rollback rather than a hoped-for one.

⚠️ **The live-delta prune FAILED, and it is benign *here* but a latent defect.**

```
WARNING: live-delta prune failed: attempt to write a readonly database
```

`prune_live_delta_local` cannot write `/vast/ishi/hardlinks/hard_links_live.sqlite`
— owned by `gazetteer`, the job runs as `stg135`. The code swallows this by
design so a prune failure cannot block a completed publish. **Checked rather than
assumed: the live delta holds 0 rows**, so nothing needed removing and there is
no duplication risk — consistent with `attestation_input: 0` (the live-forwarding
flow has never written anything).

**It becomes a real defect the moment that flow activates**: a publish would then
leave already-folded rows in the delta for the gateway to double-count, and the
warning is easy to miss. Fix by running the prune as `gazetteer` (via the relay,
as the restarts are) or by setting the file's group write bit. Tracked as a
residual.

**§2.3 is complete** — extract, geom merge, production re-index, registry push,
overlay rebuild, gate, publish. `publish_hardlinks --execute` had never been run
before today; it has now.

###### How `publish_hardlinks --execute` actually behaved (it had never been run)

**It ran clean first time — no debugging, no retries, no flag-hunting, no path
surprises.** ~~Expect to debug the publish path rather than run it.~~ **That
warning is RETIRED.** It was well-founded when written (the step had never been
executed) and it turned out to be wrong. `publish_hardlinks` is well-behaved:
**dry-run is the default** and names all four destinations explicitly
(`would_publish`, `to`, `would_prune`, `marker`), so every target is confirmable
before committing, and `--execute` then did exactly what the dry-run said.
Approach it as a normal command.

⚠️ **But it never checks that `--db-path` was built by `--run-id`.** They are
independent arguments: `publish()` ships whatever `--db-path` points at and
stamps the marker with whatever `--run-id` says, validating only
`db_path.exists()`. **A mismatched pair publishes one corpus while recording the
provenance of another** — silently, with a plausible marker.

This was live today. The build went to `…-postmerge.sqlite` (deliberately, to
dodge the adoption trap), so `--db-path` was mandatory. Omitting it falls back to
`IX1_BASE/hardlinks/hard_links_{run_id}.sqlite` — which had already been deleted,
so the fallback would have raised `FileNotFoundError`, **loudly, which is the
right failure**. Had it *not* been deleted, the default would have published the
abandoned 248 MB partial from the wedged run under a clean run id's marker.
**This campaign's signature fault — a plausible substitute for an absent input —
sitting in the final step of the final row.** A `run_id` written inside the
database and checked at publish would close it.

⚠️ **Same shape: `--cutoff` must come from the log of the run that produced that
database, and nothing checks that it does.** It is printed as `Live-delta prune
cutoff (harvest start): …` at the head of the harvest log. A void cutoff
(`2026-08-31T21:36:34`, from the abandoned build) was in hand and had to be
consciously discarded for `2026-09-02T14:21:36.274562+00:00`.

###### Three operational facts from this row that generalise

* **The harvest sbatch runs FOUR sequential Python processes** —
  `hard_links_staged`, `loc_links`, `contributor_replay`, then a `finalise_local`
  heredoc. So *"the modules are already loaded, a mid-run `git pull` is safe"* is
  **false** here: the later three import fresh from the clone. What actually makes
  a pull safe is `git diff --name-only <clone HEAD>..origin/main` over the modules
  the **remaining** phases import.
* **`py-spy dump` hangs against a `D`-state process** (it needs ptrace), so the
  recommended progress tool is unavailable in exactly the wedge case it is wanted
  for. Use `/proc/<pid>/io` + `wchan` + `timeout 8 ls <mount>` instead.
* **`/ix1` fell to ~290 KB/s today without being wedged.** A 1.33 GB comparison
  that had shown no progress in an hour finished in **2:21** after one `cp` of the
  incumbent to `/vast` and comparing locally. *Staging a read-heavy input* to
  `/vast` is cheap and reversible; it is not the same as *writing output* there,
  which is the thing S5 correctly refused on production-capacity grounds.

`processing/compare_hardlink_overlays.py` is committed and validated (known-good,
known-bad, sub-tolerance noise, and the `--allow-shrink` exemption). **It is
reusable for any future overlay publish, not a one-off.**

###### 🛑 CORRECTION — no gateway restart is needed; the publish is live on write

**Everything above (and in §2.3's earlier notes) saying *"a publish is invisible
until the gateway restarts"* is WRONG.** It was inferred from `publish_local`'s
docstring — *"the gateway's open descriptors against the previous inode stay
valid until it re-opens"* — which is a true statement about POSIX and a **false
inference about this consumer**, because nobody checked what the gateway does.

**What the gateway actually does** (`gateway/hard_link_expansion.py`):

* `expand_hard_links` calls `_connect_ro(path)` **per invocation** and
  `conn.close()` in a `finally`. **No module-level connection, no `lru_cache`, no
  pooling.**
* `_connect_ro` opens `file:{path}?mode=ro` **by path**, and its own docstring
  anticipates exactly this: *"Any error — missing file, **mid-atomic-swap**,
  permissions — is logged and skipped."*

So the overlay is re-opened on **every** `include_hard_links` request. `os.replace`
swapped the path; the next request opened the new inode. **There were never any
long-lived descriptors to invalidate.**

**Verified three ways rather than read once:**

1. **No process on `pitt` holds `hard_links.sqlite` open** — every `/proc/*/fd`
   scanned. The gateway (`python -m gateway`, pid 1865149) is running and holds
   nothing.
2. **On-disk is inode `64626`** — the published file.
3. **A live query serves it**: `POST /api/search {"query":"Paris",
   "include_hard_links":true}` → gateway `ok`, 3 hits, **10 edges**, including
   `{'a':'gn:4402452','b':'wd:Q960025','relation_type':'sameAs','source':'gn'}`.

**The 7,572,016-row overlay is serving production now.** A restart would have been
a production action taken on incorrect advice.

⚠️ **The transferable lesson, which is this campaign's own shape in new clothes:**
a downstream consequence was asserted from a **producer's** docstring without
checking the **consumer**. The docstring was accurate about what `publish_local`
guarantees; it could not know whether any reader held a descriptor. **A statement
about what a writer does is not a statement about what a reader sees** — the same
error as reading permission bits instead of taking a write lock, and as counting
documents instead of the content the consumer needs. Three instances in one day.
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
| `gn` | fresh extract (Slurm 11074352, 8 cpu / 64 G / 36 h) | ✅ COMPLETED (was ⏳ at time of writing) — 500,000 staged at 26 min, so ~12 h for the places pass **plus** the `geonames-toponyms` alt-names pass that follows it |

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

### ⛔ 2.6 DEPRECATED — SG's ruling, 2 Sep: no retrain planned

**Do not run.** `ipa` / `panphon_features` are **training-only artefacts** whose
sole consumer is the next Symphonym training run, and **SG has confirmed no
retrain is planned.** The columns are empty *by design* —
`submit_batch9_slurm` has defaulted to `--training-namespaces _none_` since
`ef31016` (2 May) — so this was never a defect to repair, and it blocked nothing.

**If a retrain is ever scheduled, the three corrections below still apply** and
are the reason this row is kept rather than deleted: it never timed out, it
never touches Elasticsearch, and it must run **after** 2.5 or it builds the
training vocabulary from a corpus missing `gn` and `wd` (~23 M of 51 M places)
and reports success. It also needs `--for-retrain` explicitly and a **hand-set
wall** (the estimator gives 03:40:24 for work exceeding 12 h — Fault 13).

<details><summary>Original step, retained for the day a retrain is scheduled</summary>

### 2.6 Re-run toponyms stage 1 for `ipa` / `panphon_features`  — ~~**S4**~~  ⚠️ AFTER 2.5

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

> ℹ️ **The total drifts; the check does not depend on it.** Measured 2 Sep the
> live toponyms count is **72,703,777** — 225 above this 31 Aug baseline. That
> is a day's drift, **not a discrepancy**, and a future re-run will get a third
> number. The discriminating half is `with_ipa 0 / with_panphon 0`, which is
> what makes this STATE 2 rather than STATE 1 or 3; do not read a moved total
> as a problem.

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

### 2.8 Make the staged merges atomic — **S9**  ✅ **LANDED**

> ✅ **DONE — `554e43a` + `e37c93b` (17/17 tests), and `1179664`** for the
> narrowness pin (a comment was the only thing holding it, and *a comment
> cannot fail*). **The body below is written in the present tense as the
> defect stood before the fix — read it as diagnosis, not as pending work.**
> Its “must fail today” test and “nothing may be writing a staged tree while
> this lands” precondition are both discharged.

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

✅ **`un`'s `final/` HELD the bad 278-cell cover until 2 Sep — now corrected and verified (see the blockquote below).** ~~Still holds~~, because `ccode_merge`
has not run for `un` — **the Fault-12 shape exactly: a chain stopping short of
`final/`.** It does **not** block `nl`, and S8 checked why rather than assuming:
`ccode_enrichment._load_un_records` calls `_iter_staged_docs(UN_NAMESPACE)`, which
reads `_h3_merged_dir` — **`h3_merged`, not `final`** — so the prefilter is
live-correct now. Still to be brought current, because `final/` is what a future
rebuild indexes from and a known-bad artefact left there is how this campaign's
faults propagate. **A correctness chore, not a gate.**

> ✅ **CLOSED 2 Sep — verified independently by the coordinator, read-only.**
> The chore self-resolved: `_mark_un_skipped` ran the `ccode_merge`
> pass-through inline during submission, regenerating `final/` from the
> corrected `h3_merged/`. Measured across all 247 docs, not a probe:
> place_id sets equal, **0 docs whose covers differ**, **88,169 cover cells on
> each side**, **0 docs with an empty cover**; largest are `un:pyf` 2,782 /
> `un:slb` 2,497 / `un:bhs` 2,054 — scattered archipelagos, the right shape.
> `un`'s `final/` therefore carries the 376-cell cover, not the 278-cell one.
> Recorded as *verified*, not *reported*: S8 stated it, and this is the
> coordinator's own measurement of the artefact.

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

> ❌ **REFUTED BY MEASUREMENT, 2 Sep. `gn` peaked at 23,617,000 K = 22.5 GiB in
> 00:07:30.** 64 G was never at risk; 32 G would have done. The request was
> harmless — Slurm wall/memory are ceilings, not reservations — but **every
> claim in this section about what 64 G would have done is wrong**, and the
> reasoning is corrected below rather than only in the post-mortem.

⚠️ **And the 200 k-sample extrapolation was ~3× optimistic** — it gave 1.07 KB/doc
against a real ~3.6 KB/doc for merged documents. S8 had adjusted its own estimate
upward for the sample being extract-shaped and *still* fell far short. ~~**This is the entire justification for `wd`-first: a 64 G `gn` run would have
OOM'd hours in, and no amount of extrapolation would have caught it.**~~ My own
"64 G covers a 14.4 GB dict with headroom" was right about the dict and wrong
about the total — I sized the component I had a number for.

> ❌ **That struck sentence is now precisely backwards, and the inversion is the
> lesson.** **Two** independent extrapolations were made — S8's ratio-scaling
> (62.5 GiB) and my decomposition (~58 GiB) — and **both were ~3× wrong in the
> safe direction** against a measured 22.5 GiB. Extrapolation was not
> impossible; **both routes were anchored to `wd`, whose 40.96 GiB came from a
> term that does not transfer to `gn` at all.** `gn` carries 139× the patch rows
> and finished in **under half** `wd`'s 17:11 at **half** its peak — `wd`'s cost
> lived in its Wikidata geoshapes (`geometries_to_replace`), which are large
> polygon blobs. **The defect was the reference namespace, not the method.**
> `wd`-first remains justified — it was the only way to learn this — but not by
> the argument originally given for it.

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

> ✅ **RESOLVED 2 Sep — historical, not a live blocker.** `nl` re-ran in full;
> ccode is now **100.00%**, equal to the live index, all 15 territories
> resolved. See *“2.7 — `nl` COMPLETE”* below. Read this section for the
> diagnosis, not for the status.

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

**Production is NOT affected** ⚠️ **— TRUE OF `un` ONLY; this reads as global and is false as such (see Phase 3, 2 Sep: `clio` 309/309 DIFFER in prod).** S8 measured BOTH sides — the live
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

#### ✅ FINAL — every namespace resolves; five defective, the rest bounded at ≤7%

**This REPLACES the `selfEXCL`, 18-sample and calibrated tables. None of the three
should be quoted.** The threshold that shaped all of them was measuring the wrong
property.

**The correction that settles it.** The instrument compares
`set(stored) == set(fresh_from_geom_store_polygon)`. **Set equality is exact at any
cardinality** — equal means the stored cover *is* what the authoritative polygon
yields. Cover size only ever changed the probability that a hull-derived cover
**coincided** with the right answer, and a coincident cover **is correct**. So:

* the test **detects wrongness, not provenance**, and has **no false negatives for
  wrongness at any cover size**;
* what was calibrated as a "false-negative rate" is a **coincidence rate** and is
  relabelled as such;
* the ≥400 threshold was gating a *provenance* question that does not size
  remediation, and is **withdrawn**.

```
DEFECTIVE (measured, materially wrong)
  un.bnda-baseline    45/45   100%
  un                235/244    96%
  nl                333/400    83%
  whg                35/45     78%
  clio               10/45     22%

NO DEFECT FOUND — 0 of 45 sampled; prevalence ≤ ~7% at 95% confidence (rule of three, 3/45)
  osm, ohm, pl, po, ukhc, hgis, kain_par, vob_cty, vob_lgd, vob_rc, vob_rd
  osm and ohm additionally confirmed against the LIVE index, 30/30 each, 0 DIFFER
```

⚠️ **`kain_par`, `vob_lgd` and `vob_rd` were recorded as "structurally
untestable". That was wrong** — they were already **tested** (45 sampled each,
0 DIFFER in job 11103195) and mislabelled UNINFORMATIVE only because their matches
sat below a threshold answering the wrong question. They are clean rows.

**Affected-ness takes two numbers** — *provenance* (hull-derived) and *material
wrongness*. `un` is 100% hull-derived and ~96% wrong; `nl` 100% and ~83%. **Only
the second sizes remediation; only the first explains the cause.** This supersedes
"read `nl` as wholly affected".

⚠️ **The claim's real scope, narrower than "correct"** (S9): "correct" here means
**consistent with what today's `compute_h3_fields` produces from the polygon
currently in the geom store**. Two ways that could mislead — if the current code
were itself wrong, every stored cover would have to be wrong *identically* to
score MATCH (implausible, not impossible); and if a polygon were **replaced** in
the store after its cover was computed, a DIFFER would be flagging **staleness**
rather than hull substitution. ⚠️ **Still hypothetical — deliberately UNLINKED
2 Sep.** It was briefly wired to §4.14's 45 residual `clio` features as its
first concrete instance; **that instance was refuted within the hour** (job
11105905, shard interleaving). The caveat remains a legitimate qualifier in
principle and **has no evidence behind it** — leaving it evidenced-by-the-45
would have been worse than leaving it hypothetical. Neither changes a verdict above.

💡 **And the rule to carry forward is not the one I nearly recorded.** I redirected
this pass to the live index arguing *"staged is an unreliable proxy"*. For
`osm`/`ohm`, staged and live agree on all 60. The true rule is narrower and more
useful: **an artefact is unreliable if a known-broken run rewrote it.** `un` and
`nl` were rewritten; `osm`/`ohm` were not. **Provenance of the artefact, not its
location.**

**Recompute set (defective covers): `un`, `nl`, `clio`, `whg`.** ⚠️ **This is
not the same list as “the recompute list is `un` AND `nl`” ~25 lines below,
and the two answer different questions.** *This* list is every namespace
measured to carry a hull-derived cover. *That* one is the subset this
campaign re-runs staged trees for — `clio` and `whg` are on this list and
gate nothing, so they are not in 2.7's chain. Neither supersedes the other.
Unchanged throughout every revision,
because it rests on DIFFER results, which were always real.

#### 🔴 `nl` MUST BE RE-RUN — its covers are hull-derived too, and its clearance was false

> ✅ **DONE 2 Sep.** The re-run completed and its covers are polygon-derived,
> confirmed by the three-way test (`samish` 103 vs hull's 119, `ngati-rehua`
> 82 vs 74, `limuw` 55 matching the *fresh* set at equal cardinality).

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

~~**Production remains unaffected as far as anyone has shown**~~ ❌ **FALSE —
see the PRODUCTION IS AFFECTED entry in Phase 3 (2 Sep): `clio` is 309/309
DIFFER in the live index.** The `nl` evidence below is still correct *for `nl`*:
prod's `samish` is 103 and `ngati-rehua` 82, matching fresh-from-store.

🛑 **THE VALIDATION GATE THIS SENTENCE ESTABLISHED IS NAMESPACE-CONDITIONAL AND
WAS RECORDED AS GENERAL — sessions are working against it now.** ~~production is
a known-good reference for more than `un:usa`~~. **Production is a known-good
reference ONLY for a namespace whose live covers were written by a run that
POSTDATES the hull-fallback fix.** `un` qualifies (its bad covers are 31 Aug,
after the 5–6 Aug cutover, and never shipped). **`clio` does not** — its
`h3_merged` is `2026-08-05T18:14:49Z`, *is* the cutover run, and shipped. For
every other namespace it is **unmeasured**.

✅ **THE SOUND FORMULATION (S8, 2 Sep; extended by the Auditor after 2.11) — my
caution above was over-broad, and S8's original wording is now itself one
category short.** **Production is a valid *corroborator* for any namespace, and
a valid *reference* where its provenance postdates the cutover — OR where it
has been remediated and independently verified. The gate that is always sound
is FRESH-FROM-POLYGON.**

⚠️ **The discriminator is provenance OR verified remediation — not provenance
alone**, which after 2.11 leaves a real category with no slot:

```
provenance POSTDATES the fix          -> reference          un, nl
provenance predates, NOT remediated   -> corroborator only  osm, ohm, all unmeasured
provenance predates, REMEDIATED       -> reference          clio, whg   <- added 2 Sep
```

Without the third row, the next session validating a `clio` recompute reads
*"provenance predates ⇒ corroborator only"* and **needlessly distrusts a
reference that is now correct.**

S8's `un` gate was never production-as-ground-truth. It computed
`fresh = compute_h3_fields(repr_point, geom_store_polygon)` as the **reference**
and used prod only as the **corroborator** — so on a `clio`-shaped namespace the
two would have **disagreed and the gate would have failed loudly**, which is the
opposite of the failure mode below. Its soundness comes from computing the
answer independently, not from where the comparison came from. Same for `nl`,
whose primary evidence was the prod-independent three-way test (stored vs
fresh-from-store vs hull-derived); production agreeing was a bonus, not the
basis.

⚠️ **The failure mode below is real but applies to the *reference* use, not the
corroborator use. The failure mode is that the gate confirms the defect.** A `clio`-shaped
namespace validated "against production" would have its **hull-derived cover
certified as correct**. Before using production as a reference for any
namespace, **establish that namespace's live-cover provenance first** — a cheap
triage pass is to ask which namespaces' live covers were written by
`h3ccode-20260805T120000Z` versus a later run; the definitive test is a
fresh-from-polygon recompute on a sample.

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

### ❌ COORDINATOR ERROR, 2 Sep — a false "S8 never woke", and the instrument that caused it

Recorded because SG asked for what went wrong, and this one is mine.

At 01:31 EDT I checked for overnight progress, found an empty `squeue`, ran
`sacct -M htc -S 2026-09-01T20:00`, got nothing, and reported to SG that **"S8
never woke"** and that I was taking over its row. **Both jobs it had run were
real.** `sacct -S` parses in the *host's local* time; the jobs started
18:53:02 **EDT**; my cutoff excluded them by construction. Worse, a job
(`11105808_0 whg-ccode-nl-rerun`) launched at 01:33:42 — my `squeue` simply
predated it. **S8 was mid-flight when I declared it idle.**

**Every input was accurate and the conclusion was false.** "No jobs after
20:00 EDT" was true; "no jobs" was not; I reported the second. The same
substitution had already happened once: `squeue`-empty could not distinguish
*not-started* from *finished*, and I read it as the former (the `nl` h3 was
already COMPLETED). One layer down, `sacct -S` could not distinguish *nothing
ran* from *my window was wrong by four hours*.

**The generalisation, which is the campaign's own defect class turned on the
observer:** *absence of evidence read as evidence of absence, because the
instrument answered a narrower question than the one asked of it.* That is the
hull fallback (no store → use hull), the existence-not-content resolvers (file
present → stage done), and this, in one shape. **A tool that filters silently
cannot report an empty result as "nothing happened".**

**Fixes carried forward, not just noted:**
* State the frame in the query — `sacct -S now-24hours`, or `-S` in UTC with
  `TZ=UTC`, never a bare local wall-clock cutoff against UTC-named run ids.
* Never infer a session's state from the scheduler alone. `squeue` describes an
  instant; `sacct` describes a window; **neither describes a session.** Ask it.
* **The collision guard was still right.** It cost nothing, it was the reason
  S8 spoke up, and it is what kept two sessions off one staged tree. Firing a
  cheap guard on a bad measurement is a good trade; acting on the bad
  measurement without one is not.

Handled: row returned to S8 intact, nothing started, tree untouched. The one
side-effect — a `git fetch`/`merge --ff-only` of the shared clone from
`6ad2640` to `cc451dc` minutes before `11105808` launched — was checked rather
than assumed benign: the sole non-doc file is `processing/helpers.py`, and an
**AST comparison with docstrings stripped shows the executable code is
identical**, so the running job is behaviourally unaffected.

### ✅ 2.7 — `nl` COMPLETE, 2 Sep; the causal chain demonstrated end to end

S8's three checks against a baseline locked before the change, all passing:

```
stage depth   nl -> final/places.parquet (was extract/places.jsonl); gn still extract/, wd update_merged/  -> still discriminates
counts        4,363   delta 0 against the live index
ccode         100.00% (was 99.66%)  == live index exactly
              previously-unresolved 15 -> RESOLVED 15, still unresolved 0
```

All 15 are small offshore territories — `ngati-rehua` `['NZ']`, `limuw`,
`samish`, `manissean`, `tuqan`/`anyapax`/`wima`/`nicoleno`, `shoalwater-bay`,
`cession-349`/`-493` `['US']`. **The chain is now measured at every link rather
than argued:** `un`'s hull-derived cover → tier-1 prefilter inert → everything
falls through to tier 2's 232-vertex BNDA outlines → small islands swallowed.
Fix the cover, tier 1 engages, the islands resolve.

Covers underneath verified polygon-derived by the three-way test: `samish` 103
(hull gave 119), `ngati-rehua` 82 (hull 74), and **`limuw` 55 matching the
*fresh* set rather than the *hull* set at the same cardinality** — the case no
count test could have caught, and the reason the three-way test exists.

### ⚠️ `gn`'s 96 G is right, and the reasoning published for it is not

Checked before authorising S8 to proceed. **`update_merge._load_patches` holds
the entire patch file in memory** as `dict[place_id → merged_patch]`, lists
included, before any document streams — a **fixed additive term that does not
scale with the corpus ratio.** The two namespaces are in different regimes:

```
wd patch     89 MB      58,657 lines        gn patch   1.4 GB   8,125,650 lines
```

`gn`'s is GeoNames' alternate names — the ~26.7M the corpus went without until
`update_merge` was fixed. Measured by replicating the merge semantics on real
rows and taking the marginal slope (per-row cost amortises: 2,006 → 1,652 →
1,493 B/row, marginal 1,335):

```
200,000 rows -> 0.37 GiB    400,000 -> 0.62 GiB    800,000 -> 1.11 GiB
extrapolated full patch dict: 10.2 GiB      (1.00 rows/place, no key collapse)
```

Decomposing `wd`: 40.96 GiB peak with a ~0.15 GiB dict ⇒ ~40.8 GiB non-dict
(the Arrow/parquet peak). For `gn` that scales by merged bytes (~34.7 GiB) or
by row count (~48 GiB) — undetermined without measuring, so assume the worse:
**~58 GiB, ~70 GiB if both compound. Under 96 G with margin.**

**Why this is recorded rather than dropped as a non-event:** the two routes
agree at 96 G and *disagree at 64 G*. The ratio argument said 62.5 GiB — "98%
of 64 G", i.e. it fits. The decomposition says a 10.2 GiB floor plus a peak
reaching 48 GiB, i.e. ~~**it would have OOMed after hours on a 13M-doc
corpus.**~~ ❌ **Measured 2 Sep: 22.5 GiB. Both routes ~3× high; 64 G would have
been ample.** Kept visible because the *disagreement* between the routes was
real and worth recording — it was the magnitudes that were wrong, in the safe
direction, from a shared bad anchor.
The decision was right; the published argument for it would mislead the next
person to re-derive it. **Permanent fix: `_load_patches` should stream or spill
— a whole-file in-memory dict is an unbounded term keyed to patch size, not
corpus size, and nothing in the request estimator knows about it.**

### ⚠️ Minor submitter defect — `un`'s ccode plants a FAILED row for work that succeeded

`11103929`, FAILED in 7 s, exit 1 — investigated rather than assumed benign,
and it **is** benign: `ccode_enrichment.py:859` raises `ValueError: ccode
enrichment is not applicable to the UN namespace itself`.
`submit_ccode_slurm` marks `un` skipped, runs the pass-through **inline**
(which is what regenerated `un`'s `final/`), and *then* still submits an array
task the guard correctly refuses.

The work succeeded; the job was redundant. But it leaves **`un ccode FAILED,
exit 1` in `sacct`** for a namespace whose `final/` is exactly what this
campaign spent days establishing you can trust. A later auditor either chases
it or concludes `un` is untrustworthy. **A guard expected to fire belongs
before submission, not after.**

### ✅ `gn`'s name-count check, PRE-COMPUTED and cold-readable (2 Sep)

Derived from the **patch**, before the merge produced anything, so it is
independent of what the merge reports about itself. This is the check that
discriminates: `update_merge` adds names to **existing** documents, so
`count(before) == count(after)` holds whether or not the patch landed, and a
document-count check cannot fail here even in principle.

```
gn/update_patch/places.update.jsonl     1.4 GB   8,125,650 rows
  toponym entries  ("toponym_id")      16,907,445
  relation entries ("relation_type")    1,831,130
  rows carrying toponyms_to_add         7,686,422
patch row shape: place_id + toponyms_to_add | relations_to_add | title
```

**Pass condition:** `update_merged` toponym count exceeds `extract`'s by a
figure approaching **16,907,445**, and relations by approaching **1,831,130**.
Both are *upper* bounds, not equalities — `_dedupe_toponyms` and
`_dedupe_relations` drop patch entries already present on the document, and the
patch never overwrites. **A delta of 0 is the failure mode this check exists to
catch**, and is exactly what "counts match" would have reported as success.

⚠️ Do not silently reconcile this against the "~26.7M GeoNames alt names"
figure carried in project memory: **16.9M is what this patch contains**,
measured. The two may differ legitimately (names already present in `extract`
are not in the patch) or one may be wrong. Measure, do not assume they agree.

### ✅ `gn`'s `update_merge` PASSES the name-count gate (2 Sep) — relations exactly, toponyms with explicable dedupe

```
                docs         toponym_id     relation_type     bytes
extract      13,454,817      13,454,817           0        7,384,904,990
update_merged 13,454,817      26,460,645   1,831,130        8,222,731,668   (+838 MB, +11.3%)
```

**Relations are an EXACT match.** `extract` carried **zero** relations, so
`_dedupe_relations` had nothing to absorb and the check is an equality rather
than a bound: the patch offered **1,831,130** and `update_merged` contains
**1,831,130**. **Zero loss.**

**Toponyms: +13,005,828 against 16,907,445 offered — 3,901,617 deduped
(23.1%), and the shortfall is explicable rather than tolerated.** `extract` has
exactly **one** toponym per document (13,454,817 of each, a clean 1:1), and
GeoNames' alternate-names file routinely repeats a record's primary name. The
absorbed count is **0.51 per patched row** across 7,686,422 rows — i.e. about
half the patched documents were offered one name identical to the one they
already had. `_dedupe_toponyms` dropping exactly those is the designed
behaviour.

**Document count is identical either side, as predicted** — the demonstration,
on real data, of why the document-count check cannot fail here and why this gate
had to be pre-computed from the patch.

**⚠️ The "~26.7M GeoNames alt names" figure in project memory reconciles as a
TOTAL, not a delta.** The measured post-merge total is **26,460,645**. The
*added* count is 13.0M. Anyone checking a future run against "26.7M" must know
which of the two it is; recorded here because assuming the wrong one would
either mask a 13M-name loss or invent one.

### ✅ `gn`'s h3 VERIFIED — 100% cover coverage, and the covers cannot be hull-derived

`whg-h3-gn-h3` COMPLETED 00:19:18, exit 0. Census over the **whole** file, with
denominators rather than a sample:

```
docs             13,454,817        == extract and update_merged; no doc lost
geometries keys  13,454,817        exactly one geometry per document
h3_cover keys    13,454,817        exactly one cover per document — 100.00%
"hull": keys              0        <- no hull anywhere in the source
```

**The covers cannot be hull-derived, and this is structural rather than an
appeal to the exit code.** `gn/update_merged/` holds a parquet, `h3_merge`
prefers the parquet (`h3_merge:100`), and every parquet is hull-stripped — so
`h3_stage` ran on hull-less input. Since `6ad2640` makes `cover_geometry_for`
**raise** instead of falling back to the hull, there was no substitution
available: the stage read the geom store or it died. It produced 13,454,817
covers. **`gn` is therefore a stronger case than `nl`, whose source did still
carry a hull to fall back on.**

⚠️ **An instrument note that nearly cost a correct prediction.** A first pass
with `grep -o hull` returned **2,950**, which reads as "hull survived" and would
have refuted the above. The precise pattern `"hull":` returns **0** — the 2,950
are **substring matches inside place names** (`gn` is global; names containing
"hull" are ordinary). Note the direction: today's other instrument failures
returned false **zeros**; this one returned a false **non-zero**. The class is
*an instrument answering a narrower or wider question than the one asked*, and
it is not only about zeros — **a match count is not a key count.**

### ✅ PRODUCTION WAS AFFECTED (2 Sep, since FIXED) — the campaign's "staging-only" premise was FALSE for `clio`

✅ **RESOLVED SAME DAY — see §2.11 COMPLETE below: 5,268 remediated, re-census 0 defective of 18,248 examined.** Kept in the present tense of its discovery; this is HISTORY, not status.

**Found by S9; every element re-measured independently here before recording.**

```
LIVE  clio:es_spanish_emp_1_1572_1578_v1  cells=832   index=places_h3ccode-20260805t120000z
LIVE  clio:es_spanish_emp_1_1579_1581_v2  cells=863   index=places_h3ccode-20260805t120000z
STAGED h3_merged  ...1572_1578_v1  cells=832      <- live == staged, exactly
STAGED h3_merged  ...1579_1581_v2  cells=863
S9, whole intersection: 309 of 309 DIFFER from fresh-from-polygon; 0 MATCH
correct counts: 469 / 472 / 631  ->  live covers are ~1.8x too many cells
```

**Production's `clio` covers are the hull-derived ones.** They span the Pacific.

🛑 **This WAS a live wrong answer, not a staging defect** (✅ fixed same day,
§2.11). `gateway/spatial.py` reads `h3_cover` for `containment=fuzzy`, so a
fuzzy containment scoped to these polities **was** answering from a cover
roughly 1.8× too large. **That is why it was remediated immediately rather than
deferred.**

**Why the reasoning that made us confident does not cover this — and the rule
that replaces it.** The standing argument was that the live index was cut over
5–6 Aug from `h3ccode-20260805T120000Z`, so a hull fallback in a **31 Aug** run
is confined to `staged/`. True for `un`. **`clio`'s covers were not written by a
post-cutover run** — staged mtimes, in UTC:

```
clio/extract    2026-07-31T15:38:10Z
clio/h3_merged  2026-08-05T18:14:49Z   <- IS the h3ccode-20260805T120000Z run
clio/final      2026-08-06T02:48:58Z
```

**That run is the live index.** So `clio`'s hull fallback happened *before* the
cutover and shipped with it; `un`'s happened after and did not. ⚠️ **The
"cutover date" argument protects only namespaces whose bad covers POSTDATE it —
it is not a general clearance, and it was being used as one.**

⚠️ **Corrects the framing of one entry above.** `clio` was called "the fourth
namespace of the same fault", implying a single incident. It is **the same class
in at least two separate occurrences, months apart, and the earlier one reached
production.**

**NOT established — do not read this as bounded:**

* **`whg` has never been checked against live.** It is **78% defective in
  staging** and is the obvious next candidate. **Highest-value outstanding
  measurement in the campaign.**
* **How many of `clio`'s 15,690 are affected in production** is unknown. The
  309 all differ; an earlier uniform sample put staged `clio` at ~22%.
* `nl` and `un` appear **fine in production** (`samish` live 103 == fresh;
  `un:usa` live 376 == extract), so this is **not universal** — which is exactly
  why per-namespace measurement replaces the date argument.

ℹ️ Two secondary results: the degenerate-hull hypothesis is **dead** (stored
hull type is `Polygon` for all 264 *and* all 45); and `clio` has **six** distinct
h3 run_ids, so code-version-at-write-time is **not** structurally excluded the
way staleness was.

### ✅ `gn` HAS NO POLYGONS — the hull mechanism is inapplicable to it by measurement

**S8's census over all 13,454,817 `gn` documents; corroborated here on a
2,000,000-document sample** (mine is a sample, S8's is the census — stated so the
denominators are not conflated):

```
has_geom True        0            (S8: zero in the entire corpus)
geom_class           point, all
h3_cover size > 1    0            every cover is a single cell
```

**No polygons ⇒ no hull to substitute and no store lookup to fail.**

⚠️ **This corrects an argument of mine.** I reasoned that `gn`'s covers cannot be
hull-derived because its `update_merged` parquet is hull-stripped and
`cover_geometry_for` now raises rather than falling back — so the stage "read the
store or died". **Sound in form, but it describes a path never taken:** `gn`
never needed the store at all. The conclusion holds; the reasoning was about a
mechanism that could not have fired. **A correct conclusion reached through an
inapplicable mechanism is still a defect in the reasoning** — the same standard
applied to `clio` earlier today.

🛑 **`wd` is where the caution actually bites**, and S8 has adjusted for it:
`wd`'s h3 has not run, it **does** carry geometry (58,658 geoshapes,
`patches_unmatched: 0`), and its live-cover provenance is **unestablished**. S8
will use **fresh-from-store set comparison as the primary gate** and will not
treat production as a reference. ✅ **And it has pre-committed to the right
reading of a null result:** if prod and staged agree for `wd`, that is *equally*
consistent with "both correct" and "both hull-derived", and it will say so
rather than report agreement as a pass.

### 🛑 PRODUCTION EXPOSURE, BOUNDED — with one denominator I cannot reconcile

**S9's uniform reservoir samples (not the intersection, not a prefix), fresh
covers from the geom-store polygon, set comparison, 400/400 found live, store
open asserted with a control fetch first:**

```
ns     tested  MATCH  DIFFER   rate   95% CI     S9's est. affected
clio      200    162      38    19%   14-24%     ~2,980 of 15,683
whg       200     78     122    61%   54-68%     ~1,565 of  2,565   <- denominator disputed
staged cover == live cover: 400 of 400
```

✅ **`clio` reconciles exactly.** Live `clio` docs with an area geometry =
**15,690**, matching S9's 15,683 frame to within rounding. **19%, not the 100%
the 309-intersection might have implied** — and S9 flagged that risk itself
before measuring. It also agrees with the earlier staged uniform sample (~22%),
a genuine cross-check that two sampling frames measure the same thing.

⚠️ **`whg`'s denominator does NOT reconcile, and it roughly halves the estimate.**
Measured here, staged `whg`, full pass:

```
docs 228,918   geometries 215,401
geom_class: point 213,081   area 1,248   line 1,072
docs with >=1 area geometry 1,248   AREA GEOMETRIES 1,248   (1:1, no doc has two)
LIVE whg docs with an area geometry: 1,248   <- independent ES count, agrees exactly
```

**1,248 area features, not 2,565** — and 1,248 is exactly the 770 Polygons +
478 MultiPolygons already recorded in S5's Check 1 derivation. `area + line` =
2,320, still not 2,565.

✅ **ANSWERED — S9's frame was `geom_ref` present AND `len(h3_cover) >= 2`, NOT
area geometries. S9 has WITHDRAWN the ~1,565 figure rather than defend it.**

**The frame was chosen deliberately and is not arbitrary:** S8 had shown
`geom_class` is **absent for whole namespaces** (all 4,363 `nl` records), so
keying a population on a classification field silently drops entire gazetteers.
`geom_ref` + multi-cell cover keys off the *data* instead, selecting "features
whose cover could possibly disagree with a stored polygon" — the genuinely
testable population. **It is simply a different set from `geom_class == "area"`,
and the rate was then extrapolated against an area-shaped denominator without
checking the frames matched.**

⚠️ **S9's caution does NOT undercut the 1,248 for `whg` specifically** — checked:
`point 213,081 + area 1,248 + line 1,072 = 215,401`, **exactly** the geometry
count, so `geom_class` is fully populated on `whg` with no absent values. The
`nl`-shaped hazard is real and does not apply here. And the mismatch is a real
set difference, not a counting artefact: S9's frame was 2,565 against
`area + line` = 2,320, so it contains ~245 geometries that are neither.

🔬 **Rather than reconcile the frames, S9 is REMOVING the need for one — `whg` is
small enough to census.** Job **11105929** tests **every** candidate rather than
200, broken down by `geom_class`: an exact affected count with **no rate, no
confidence interval and no extrapolation to dispute**, plus the per-class split
that would be needed to reconcile the frames anyway. It also exposes whether the
defect rate differs between area and line geometries, **which the pooled 61%
would hide.**

**Until it lands: the `whg` COUNT reads *disputed, census in flight*; the `whg`
RATE (122/200) stands independently of it.**

🛑 **Nothing downstream should quote a `whg` affected-count until that is
settled.** This is the campaign's own denominator rule biting on the campaign's
own headline number — *"0 defective" is meaningless without "of N examined"*,
and so is "1,565 affected" without the N it extrapolates from.

**Revised production picture, with the disputed figure marked:**

```
clio    ~2,980 of 15,690 wrong in the live index   (19%, CI 14-24)   RECONCILED
whg     rate 61% (CI 54-68); affected count DISPUTED — ~761 on a 1,248 frame
un      live cover CORRECT — bad covers postdate the cutover; staged recompute only
nl      live cover CORRECT — same reason; staged recompute only
osm/ohm live CORRECT, 30/30 each
```

✅ **`staged == live` for all 400** — so staged is a faithful proxy for production
for `clio` and `whg`. Now measured for `osm`/`ohm` (60/60), `clio` + `whg`
(400/400), and false only for `un` and `nl`, whose staged trees the 31 Aug runs
rewrote. **The rule holds as stated: an artefact is unreliable if a known-broken
run rewrote it — provenance, not location.**

⚠️ S9 has **retired its own earlier `whg` figure**: 78% came from n=45; **61% at
n=200 is the number to use**, and the CIs overlap at 63–68% so they are
consistent rather than contradictory.

### 🛑 `whg` CENSUSED — 1,746 of 2,565 live-defective. EXACT. And it corrects BOTH estimates

```
geom_class   tested  MATCH  DIFFER   rate   example (live/fresh)
area           1,247    359     888    71%   whg:975:117   67/70
line           1,070    460     610    57%   whg:1118:699  18/15
point            248      0     248   100%   whg:12:1794    3/1
TOTAL live-defective  1,746 of 2,565        2,565 of 2,565 docs found live
```

**No rate, no confidence interval, no extrapolation.** The frame question is
also fully answered: S9's frame splits `point 248 / area 1,247 / line 1,070` —
**the ~245 non-area/line geometries predicted from the arithmetic are 248
points**, and `area` is 1,247 of the 1,248 measured here (one lacks a `geom_ref`
or a multi-cell cover).

🛑 **My denominator would have been the WORSE error, and in the more dangerous
direction.** I argued 1,248 was the right frame and that ~761 followed. **An
area-only frame reports 888 and misses 858 defective geometries** — the line and
point classes entirely. Against a true 1,746 that is a **2.3× UNDER-count**,
where S9's withdrawn ~1,565 was an under-estimate by only 181. **Understating a
production exposure heading for a remediation decision is worse than
overstating it.**

✅ **So the lesson is NOT "use the area denominator". It is that neither of us
should have multiplied.** S9's *frame choice* was right and its *extrapolation*
was wrong — **separate things, and both are recorded.** The pooled 61% masked a
71% area rate, and **only the census could reveal that.** That is the argument
for censusing over reconciling frames, and it is now evidenced rather than
argued.

ℹ️ **Sampling check:** the census rate is 68% against the uniform sample's 61%
(CI 54–68) — **exactly at the CI's upper edge.** Consistent, and a fair reminder
that a point estimate at n=200 sits where it sits.

### 🛑 A SECOND DEFECT, NOT A LEFTOVER — 248 `point`-class geometries carry polygon-sized covers, 100% defective

⚠️ **This is NOT the hull fault, and the argument is decisive rather than
suggestive: a point has no areal hull to derive from.** `hull =
geom.convex_hull` of a Point **is a Point**, so a hull-derived cover is
physically incapable of producing an areal cover on a point-class geometry.
Whatever produced these, it was not the mechanism that produced the other
5,020. **It is a second defect that the census happened to expose** — and at
**248 of 248** it is the only one of the three open rows with a 100% rate.

**Do not file it as a leftover of the hull fault.** The three open rows have
three different causes: the **45** are a subset of the remediated set whose
byte-level mechanism is unknown but which were fixed anyway (a question about
bytes, not correctness); **these 248** are a distinct ingestion/classification
defect; the **top-level `h3_cover`** is a schema artefact unrelated to either.

**Flagged, not explained. Verified independently here — exactly 248 of `whg`'s
213,081 point-class geometries have a multi-cell cover, matching S9's count.**

```
cover sizes among the 248:  2 cells (28) … 3 (47) … 63 (2) … 204 … 264 … 310 … 476 … 892 … 1,230
all three sampled carry geom_ref = True
```

`compute_h3_fields` returns exactly `[centroid]` — **one cell** — for a Point.
**A `point`-class geometry with a 1,230-cell cover was not computed from a
point.** Whether the geometry changed class, the cover predates a class change,
or `geom_class` is simply wrong for them is **not established and nobody should
guess.** Small population, 100% consistent, which usually means one cause.

⚠️ **Its own row, NOT folded into the hull-fallback story — it does not look
like the same fault.** Compare the standing predicate in
`schemas/field-notes.md`: `geom_class ∈ {area,line} AND NOT has_geom` is a known
incomplete-ingestion defect. **This is its inverse** — `geom_class = point` with
an areal cover — and is not currently anybody's check.

**Production exposure as measured (✅ ALL REMEDIATED — §2.11):**

```
whg   1,746 of 2,565 geometries were live-defective   EXACT (census)
clio  ~2,980 of 15,683                            19%, n=200 uniform — still an ESTIMATE
un / nl / osm / ohm                               live correct
```

### ✅ FINAL PRODUCTION EXPOSURE AS MEASURED — both censused, ALL SINCE REMEDIATED (§2.11)

```
clio    3,522 of 15,683 live-defective   (22.5%)   CENSUS, 15,683/15,683 found live
whg     1,746 of  2,565 live-defective   (68.1%)   CENSUS, 2,565/2,565 found live
TOTAL   5,268 geometries were wrong in the live index   -> ALL FIXED, verified 0 remaining

un, nl                              live CORRECT — bad covers postdate the cutover
osm, ohm                            live CORRECT — 30/30 each at >=400 cells
kain_par, vob_*, pl, po, ukhc, hgis no defect found, 0/45 sampled each
```

Integrity: 8 slices merged with **zero duplicate keys**; 15,683 recomputed =
exactly the frame. `clio`'s frame is **100% area**, which is why its denominator
agreed with the independent live count of 15,690 all along — **the `whg`
divergence was specific to `whg`**, which carries 1,072 line and 248 anomalous
point geometries.

⚠️ **The `clio` estimate was low by 542** — 19% (n=200) against a true **22.5%**,
inside the 14–24% CI but above the point estimate. **Second time an estimate
moved once censused, both in the same direction (understating).** Two for two is
not a pattern, but it is twice.

### 🛑 THE DAMAGE IS MOSTLY *UNDER*-COVERING — which is the silent failure, not the visible one

```
of the 3,522 defective clio covers:
  live cover SMALLER than correct : 2,779   (79%)
  live cover LARGER  than correct :   730   (21%)
  same size, DIFFERENT SET        :    13
```

**This inverts the intuition the whole investigation was built on.** `un:usa`'s
hull-derived cover was **1.74× too large**, and "hull-derived ⇒ too big" became
the mental model. **At corpus scale 79% of the damage is the opposite.**

**Why it matters for how this is described to anyone deciding remediation:**

* an **over**-sized cover makes `containment=fuzzy` **over-inclusive** — it
  returns places it should not, and **a user can see that**;
* an **under**-sized cover makes it **miss places that should match** — a
  **false negative in search**, and **users do not report the result they never
  saw.**

**So the live symptom is mostly invisible, and "wrong covers" understates it.
The right description is: spatial scoping silently omits matching places.**

✅ **The 13 same-size-different-set cases are the direct vindication of set
comparison over cardinality** — a count-based test scores all 13 as MATCHES.
**This is the `limuw` shape at corpus scale**, and it is the trap S8 caught S9
heading into when stored-vs-fresh was being reported by cardinality.

**Two open items, neither affecting remediation scope:** the 45 of 309 with an
undetermined mechanism, and the 248 `point`-class geometries carrying covers up
to 1,230 cells.

### ✅ 2.11 COMPLETE — 5,268 live documents remediated, VERIFIED INDEPENDENTLY

**SG confirmed directly to S9 (who declined to act on my relay — correctly).
Write executed, and verified here rather than relayed.**

⚠️ **SCOPE NOTE ADDED 3 Sep — not a fault, a boundary of the specification.**
2.11 fixed over-coverage for **5,268** geometries and **introduced
under-coverage for 248 of them**, because *"recompute the cover from the stored
geometry"* is right for polygons and wrong for **MultiPoints**:
`select_h3_cover_geometry` returns non-polygonal geometry unchanged, so a
MultiPoint never reaches the areal path and `compute_h3_fields` yields a single
centroid cell whatever the extent. Established from 2.11's own retained
rollback (Auditor, 3 Sep): of the 546 live `whg` multi-point features with real
extent, **248 appear in the rollback and 248 of 248 were shrunk** — Danube
`whg:1361:9` **1,230 cells → 1**, Silk Roads `whg:1381:18` **892 → 1**. The
other 298 are absent from the rollback and correct as they stand (widest span
0.02°).

**Every other one of the 5,268 was improved**, and the arithmetic closes
exactly: the rollback's `whg` half is 1,746 = area 888 + line 610 + **point
248**, with the 248 derived independently from live geometry classes and bounds.

🛑 **The remedy is NOT to restore the rollback values** — those were
hull-derived, which is what 2.11 correctly removed. **2.11 was right to change
them and wrong about what to change them to.** Proposed: cover a MultiPoint's
**member cells**, not its hull (the Silk Roads hull is a 46°-wide swath the
corridor never touches — over-coverage by a new door), leaving
`geom_class = "point"` so these stay findable *within* a scope and can never
*define* one. **Awaiting SG's decision.**

```
frame            clio 15,683 + whg 2,565 = 18,248     recomputed 18,248, unresolved 0
DEFECTIVE        clio 3,522 + whg 1,746 =  5,268      matched the census EXACTLY
BULK             5,268 ok, 0 errors
re-census        0 defective of 18,248 examined       <- whole frame, not just the 5,268
rollback         5,268 docs -> /vast/ishi/elastic/logs/s9_rollback_geometries.json (31 MB, verified present)
```

✅ **The strongest available check, because the before and after came from
different sources.** I recorded these three live myself **before** the write;
the "correct" values came from S9's independent recompute. They now read as the
recompute predicted, exactly:

```
id                                   was (my measurement)   correct (S9)   NOW
clio:es_spanish_emp_1_1572_1578_v1          832                 469         469 ✅
clio:es_spanish_emp_1_1579_1581_v2          863                 472         472 ✅
clio:es_spanish_emp_1_1582_1587_v2          800                 631         631 ✅
```

✅ **No collateral damage from the whole-array rewrite** — the risk the
re-census structurally cannot see, since a truncated `_mget` source would have
silently gutted all 5,268. Each geometry still carries **9 fields**
(`bounds`, `geom_class`, `geom_ref`, `geometry_index`, `h3_centroid`,
`h3_cover`, `has_geom`, `repr_point`, `timespans`); S9's 400-doc spot-check
against the rollback found **0 length changes, 0 lost fields, 0 non-`h3_cover`
changes.**

ℹ️ **The re-census covered the whole 18,248 frame, not only the patched 5,268**
— so it also proves **no regression among the 12,980 that were already
correct.** That is the denominator rule used offensively rather than
defensively.

⚠️ **`geom_class` remains OUT OF SCOPE and that row does not close.** The 248
`point`-class geometries now have correct covers *for their stored polygons*;
if the class is wrong it is still wrong. **5,268 fixed is not 248 explained.**

### ⚠️ NEW ROW — a TOP-LEVEL `h3_cover` exists, is diverged, and is read by nothing

**Found by S9 during its integrity check, investigated rather than reported, and
verified here. It is PRE-EXISTING — not created by 2.11.**

```
id                                   top-level   old nested   new nested
clio:es_spanish_emp_1_1572_1578_v1      838          832         469
clio:es_spanish_emp_1_1579_1581_v2      845          863         472
clio:es_spanish_emp_1_1582_1587_v2      868          800         631
```

**It matches neither the old cover nor the new one** — so it was **already
diverged before the write**, and S9's payload only ever touched `geometries`.
S9 also found it on 200/200 *untouched* documents.

✅ **Nothing reads it — verified.** `clustering_payload.py:226` does
`g.get("h3_cover")` while iterating `geometries`; `spatial.py:576`,
`es_helpers.py:1187/1215` and `clustering_payload.py:95` all name
`geometries.h3_cover`. **So the containment path is genuinely fixed.**

🛑 **But it is a top-level field that looks authoritative, is stale, and is read
by nothing — and it is exactly the shape of the docstring error corrected on 31
Aug**, which told readers `h3_cover` was a top-level field. **Someone will
eventually assume it means something.** Its own row, unresolved.

### 2.11 spec (as dispatched, retained verbatim) — **S9 (`indexing-98`)** — SG RULING, 2 Sep

ℹ️ **Body left in its dispatched present tense deliberately** — it records *why*
SG ruled to remediate now rather than defer, and that reasoning outlives the tense.

**SG's decision, overruling S8's recommendation to defer: do it now.** This is
the corpus-correctness class SG's standing ruling already places ahead of the
beta-gated retile, and it is a live user-facing wrong answer.

**Scope — exactly these, nothing else:**

```
clio   3,522 of 15,683 defective geometries
whg    1,746 of  2,565
       5,268 total, in places_h3ccode-20260805t120000z (behind the `places` alias)
```

🛑 **DO NOT TOUCH `un`, `nl`, `osm`, `ohm`** — all measured **live-correct**.
A write to them is a regression, not a fix.

**Method.** For each defective geometry: read the polygon from the geom store by
`geom_ref`, recompute `h3_cover` + `h3_centroid` with `compute_h3_fields`, and
patch the live index. S9 already holds this machinery from the censuses.

**Hard requirements — each exists because of a fault this campaign recorded:**

1. **Use `_bulk` with `update` actions. NOT `_update_by_query`**, which re-runs
   the ingest pipeline (the `geom_store_way_gap` lesson).
2. **Rewrite the WHOLE `geometries` array per document.** `h3_cover` is nested
   *inside* `geometries[]`; a partial-doc update cannot patch one array element,
   and a naive attempt will silently write a top-level field instead — which is
   exactly the `compute_h3_fields` docstring error corrected on 31 Aug.
3. **Capture the pre-change `geometries` arrays to a rollback file BEFORE the
   first write.** 5,268 documents is cheap insurance and the only reversal path.
4. **Dry-run first**, reporting counts, and **report the denominator** —
   *"N patched of M attempted of 5,268 identified"*, not "done".
5. **Verify by RE-CENSUS, not by exit code.** Re-run the same census over both
   namespaces afterwards and expect **0 defective**, plus unchanged document
   counts (`clio` 15,683 / `whg` 2,565). ⚠️ **A census that reports 0 because it
   examined 0 is this campaign's signature failure — report what was examined.**
6. **Idempotent and re-runnable.** A second run must be a no-op.

⚠️ **Recomputing fixes the COVER, not the CLASS.** The 248 `point`-class
geometries carrying covers up to 1,230 cells will get correct covers for their
*stored polygons* — but if `geom_class` is itself wrong for them, **that remains
wrong afterwards**. Do not close that row on the strength of this fix.

✅ **Preconditions verified 2 Sep before dispatch:**
`places_h3ccode-20260805t120000z` still carries `default_pipeline =
extract_namespace`, and the pipeline exists (HTTP 200) — so the
snapshot-restore silent-400 trap is **not** present. Cluster yellow (single
node, expected).

✅ **SAFE TO RUN CONCURRENTLY WITH S8's `wd` PASS — the targets are disjoint.**
S8 writes **only** `staged/wd/` trees and does **not** write to the live index;
this writes **only** `clio`/`whg` documents in the live index and no staged
tree. Both **read** the geom store, and concurrent reads are safe — neither
writes it. Namespaces are disjoint (`wd` vs `clio`/`whg`). Disk is not a
constraint: the patch is a few MB against S8's ~20–25 GB, with 235 GB free.
**The real risks are ES-side, not S8-side** — and `h3_cover` updates touch no
`dense_vector`, so the HNSW-merge heap failure mode does not apply.

### ✅ 2.7 COMPLETE (2 Sep) — all four namespaces have a real `final/`; S5 and S3 UNBLOCKED

```
ns    rows          ccodes      vs live    resolver
gn    13,454,817    99.950%     ~99.94%    final/places.parquet
wd    11,459,393    97.425%     ~97.3%     final/places.parquet
nl         4,363   100.000%     100%       final/places.parquet
un           247    pass-thru   n/a        final/places.parquet
```

✅ **Verified here, not accepted on report — and the row counts were counted
independently, by a different method (`wc -l` over `final/places.jsonl`), and
match S8's to the unit:**

```
un 247   nl 4,363   gn 13,454,817   wd 11,459,393
```

All four carry a **complete JSONL+parquet pair** in `final/` (`gn` 8.75 GB + 1.04 GB; `wd` 13.16 GB +
1.71 GB; `nl` 23.3 MB + 6.8 MB; `un` 1.95 MB + 0.68 MB). Since
`_STAGED_SOURCE_PRIORITY` walks `final → h3_merged → … → extract` on
`.exists()`, **a present `final/places.parquet` is exactly what makes the
resolver return it** — Check 1 is structurally satisfied for all four. `/vast`
at **204 GB**, matching S8's figure.

**Check 3 is two-sided — each residual EXPLAINED, not tolerated**, which is the
half that matters:

* `gn` **6,663 uncoded**, dominated by GeoNames **undersea** codes — SMU 1,371,
  CNYU 680, BSNU 523, BNKU 486, RFU 404, RDGU 365 … seamounts, canyons, basins,
  banks, reefs, ridges. **100% would have been a FAILURE** — it would mean
  countries assigned to open ocean.
* `wd` **295,049 uncoded**, top types Q23442 island 63,060, Q39594 cape 15,150,
  Q34763 peninsula 9,619, Q11446/Q852190 ship/shipwreck 8,955 — coastal,
  maritime and **vessels**, legitimately outside any country. Came in **slightly
  above** live's 97.3%, consistent with the repaired tier-1 prefilter resolving
  a few *more*.
* `nl` **100%**, up from 99.66% — all 15 island territories resolved.

**`update_merge`, the stage that had never run**, and two independent counts
agreeing to the unit — S8's and mine, by different methods:

```
gn  docs 13,454,817 -> 13,454,817   toponyms 13,454,817 -> 26,460,645   relations 0 -> 1,831,130
```

**Relations are an exact equality** (1,831,130 offered, 1,831,130 present, zero
loss — `extract` carried none). Toponyms +13,005,828 of 16,907,445 offered,
**3,901,617 absorbed (23.1%)** by `_dedupe_toponyms`, because GeoNames repeats
each record's primary name against `extract`'s clean 1:1.

**Cover integrity, per namespace:** `gn` **census** (13,454,817 docs, `has_geom`
False and `geom_class` point for **every one** — the hull mechanism is
inapplicable *by measurement*); `wd` fresh-from-store n=400, **391 MATCH / 9
DIFFER / 0 unresolvable**, and **152 of 152 informative matches (≥100 cells)
agree**; `nl` three-way test; `un` 376 cells set-equal to prod at **both**
stages.

✅ **S8 did NOT use production as a reference for `wd`**, per the corroborator/
reference distinction — its live provenance is unestablished, and agreement
would have been as consistent with *both hull-derived* as with *both correct*.

**Costs:** `update_merge` gn 7:30 / 22.5 GiB, wd 17:11 / 40.96 GiB · h3 gn
19:18, wd 1:43:42 · ccode gn 1:03:20, wd 1:24:03. `/vast` 274 → 204 GB, **~70 GB
against a projected 95–110**, stop-line never approached.

### ⚠️ 2.7's three residuals — none blocking

1. **Sub-cell polygons under-cover.** 9 of 400 `wd` area features, **all in the
   1–9 cell band**: stored = 1 cell (centroid fallback), fresh = 2–3. E.g.
   `wd:Q21515377`, a MultiPolygon of 0.0000021 deg² — ~200 m × 165 m, **smaller
   than one r7 cell**. Same *direction* as the corpus-wide under-covering
   finding, negligible in magnitude (~2.25% of `wd`'s area features).
2. **`submit_ccode_slurm` plants a false `FAILED` row for `un`** — marks it
   skipped, runs the pass-through inline, then submits a task
   `ccode_enrichment:859` correctly refuses, leaving `11103929 FAILED exit 1` in
   `sacct` **for work that succeeded.** A guard expected to fire belongs
   *before* submission.
3. **`reconcile_stage_status`'s default sweep silently skips `update_merge`.**
   It is in `STAGE_ARTEFACTS` and the artefact existed with 13,454,817 records,
   but **only `--stage update_merge` promotes it** — so a default reconcile
   leaves `gn`/`wd` deferred at the h3 barrier **with complete artefacts on
   disk.** This is the campaign's signature class in the reconciler itself.

**➡️ S5 may now retile all 27 buckets. S3's re-harvest should find `gn:
attempted=1,111,147` rather than 0. The overlay publish gate remains SG's.**

### ⚠️ `/ix1` vs `/vast` for tile output — MEASURED, 2 Sep

**SG's reasoning — "entire mbtiles files rather than many small ones" — is sound
and true of the artefacts.** Verified: `_build_feature` → one `.geojsonl` per
bucket → **tippecanoe** → one `.mbtiles` per bucket. **No
`--temporary-directory` is passed**, so tippecanoe's heavy scratch goes to
`$TMPDIR`, **not** the output volume. The existing store is 15 files, all
`.mbtiles`, including a **737 GB** (sparse) `terrarium.mbtiles` — large-file
I/O on `/ix1` is proven in production.

🛑 **But sequential write throughput differs by 23×, measured on pitt:**

```
/vast/ishi   2 GB seq write   3.38 s   636 MB/s
/ix1/ishi    2 GB seq write  78.69 s    27.3 MB/s     <- 23x slower
```

**And the CRC copy is NOT in the serving path** — the mbtiles are **rsync'd to
the DigitalOcean tileserver** (`TILESERVER_HOST` / `TILESERVER_USER` /
`remote_dir`), which serves them. **So `/ix1`'s slowness costs BUILD time only,
never query latency.**

⚠️ **Two measurement traps, both hit today:**

* **`du` path matters.** `/ix1/ishi/tiles` = **658 G** (the sparse terrarium
  file); `/ix1/ishi/data/tiles` = **28 G** (the WHG tiles). *Both figures are
  real and they are different directories* — neither reading was wrong, and
  the working figure for this decision is **28 G**.
* **`df` on the bare mount point lies, differently per host.** On **pitt**,
  `df -h /vast` returns the **VM's local root** (61 G); on a **compute node** it
  returns the **cluster-wide** VAST filesystem (petabytes). Only
  `df -h /vast/ishi` gives the **project quota** — 1.0 T, 204 G free. **Anyone
  sizing this from the bare mount point concludes there is unlimited space on
  the volume that has already caused a production outage by filling.** (S5)
* **`/vast` headroom goes stale within hours during a campaign** — 275 G → 204 G
  in a few hours today, ~71 G consumed, consistent with 2.7's `gn`+`wd` `final/`
  outputs. **Re-measure at submit time.**

### 🛑 REVERT PATH FOR THE RETILE — record this before any session ends

**S5 built to a FRESH directory, which is the entire revert story and was
unrecorded until now:**

```
NEW build   /ix1/ishi/data/tiles-20260902-retile
OLD (7 Aug) /ix1/ishi/data/tiles              <- INTACT, untouched
revert      python -m processing.generate_tiles --redeploy-only --bucket <ns> \
                --output-dir /ix1/ishi/data/tiles
```

⚠️ **One command per bucket restores the 7 August generation.** This is the only
rollback for a deployed tileset, and it exists solely because S5 chose a fresh
output directory rather than overwriting in place. **If S5's session ends, this
path is how anyone else reverts** — it was in no document until now, which is
precisely the failure this campaign has been dismantling.

**Canary verified by the coordinator, independently of S5's report:**
`ukhc.mbtiles` **1,740,800 B**, md5 **65b9a9d00734a812ae7f05f2271159ba** —
**byte-identical** on CRC (`tiles-20260902-retile`) and on the tileserver
(`/srv/tileserver/tiles/`) — and **serving: `ukhc.json` HTTP 200.**

### ⬜ OPEN, UNOWNED — carry these past any session shutdown

1. **45 of 309 `clio` features wrong by an undetermined mechanism.** Four
   hypotheses eliminated (dateline polygon, `enrich_geometry` repair path,
   simplification, staleness-by-shard-interleaving). All remediated regardless.
2. **248 `whg` `point`-class geometries with covers up to 1,230 cells**, 100%
   defective — **a second defect, not a hull leftover** (the convex hull of a
   Point is a Point). Their covers are now correct *for their stored polygons*;
   if `geom_class` is wrong it is still wrong.
3. **§2.10 questions 2 and 3** — open, gating nothing.
4. ~~**`clio` +2,986**~~ — 🛑 **REFUTED 3 Sep, not merely unsourced. Strike it
   wherever it is quoted.** Direct census of the live index:

   ```
   clio docs                       15,690
   clio geometries (nested)        15,690    exactly one per doc
     geom_class = area             15,690    100% — no point or line bucket exists
     has_geom = true               15,690    100% retrievable
     geom_ref present              15,690    100%
   ```

   **Every `clio` document has exactly one areal, store-backed geometry, so the
   polygon count a retile should produce is 15,690** — reproducing the geom-store
   key count from two prior independent measurements (this week's, and the 9 Aug
   handover's *"clio verified at 15,690 entries / 15,690 distinct keys"*). Three
   sources, one number. The nested agg partitioned into a single `area` bucket
   with no remainder; a missing nested wrapper would have returned 0 for
   everything, including the control.

   ⚠️ **The other circulating figure, `13,907`, differs from 15,690 by 1,783 —
   11.4% of the layer — and is NOT reconciled.** The two may be measuring
   different things: 15,690 is the **index/store input**, `13,907` is presumably
   a **built-tileset output**, and tippecanoe legitimately drops and merges
   features. But that is exactly the input-versus-output difference this
   campaign insists must be **asserted rather than assumed**. Whoever next
   touches the `clio` tileset should reconcile them explicitly — it is the same
   shape as the `poly=` assertion already on S5's row, and 11.4% is too large to
   wave through as simplification.

### ✅ 2.3 COMPLETE — overlay PUBLISHED 2 Sep, gateway restarted, serving verified

**SG authorised the publish knowing the gate said FAIL. S3 held for SG's own
words rather than acting on a relay, and did NOT relax the gate** —
`compare_hardlink_overlays` is untouched and its FAIL stands on record as **a
verdict a human overrode with reasons, not a PASS anyone engineered.**

```
published total rows  7,572,016   (expected 7,572,016)
gn asserted rows      1,111,147   (expected 1,111,147)
new live inode 64626 · 6 Aug build preserved as hard_links.sqlite.previous (inode 59781)
top sources: wd 3,968,404 · osm 2,295,659 · gn 1,111,147 · ohm 98,569 · iv 68,935
```

✅ **THE GATE BEHAVED CORRECTLY — it was not deficient.** It cannot distinguish
an *intended* drop's cascade from an *unintended* drop, so it reports both.
**That is a guard failing closed, working as designed.** Reporting FAIL on a
correct artefact is the unavoidable price of a guard that will also FAIL on an
incorrect one — and this plan's own doctrine is that *"a guard that cannot say
PASS is as useless as one that cannot say FAIL."* **The gate was conservative in
the correct direction.**

🛑 **THE STANDARD THIS OVERRIDE MET — all four. An override that does not meet
all four is NOT this precedent.**

1. **Every deviation predicted to the unit, IN WRITING, BEFORE publication** —
   not reconciled afterwards. (`bnf` 120, `gnd` 145, `loc` 138, `viaf` 193,
   `tgn` 85, each predicted then measured.)
2. **The gate left untouched.** No flag added, no threshold relaxed, no
   `--allow-shrink` widened to manufacture a PASS.
3. **A named human took the decision** — not the session holding the row — and
   S3 held for SG's own words rather than acting on a relayed authorisation.
4. **Rollback verified real before publishing**, not assumed: `.previous` at
   inode 59781, restorable by one `mv` plus a restart.

⚠️ **The negative condition, which is what makes this a standard rather than a
story: had ANY deviation been unexplained, the correct action was to STOP.
The override was licensed by the arithmetic, not by the confidence.**

✅ **Verified live by the coordinator AFTER the gateway restart** (SG restarted
directly): gateway PID 1865149, health `ok`, and a search returned **6
hard-link edges spanning `wd`/`gn`/`osm`/`iv`**. **`gn` appearing in served
edges is the meaningful signal** — its 1,111,147 assertions exist only because
`update_merge` ran for the first time that morning, so the gateway is
demonstrably serving links from the new build rather than the 6 Aug file.

**Rollback is real rather than hoped-for:** one `mv` of
`hard_links.sqlite.previous` back, then a restart. The file was never modified
in place.

### ✅ CLOSED 2 Sep — live-delta prune permission gap (was: prune cannot write)

> ✅ **FIXED.** SG ran `chmod g+w /vast/ishi/hardlinks/hard_links_live.sqlite`
> as `gazetteer`. Verified by the coordinator **against the failure mode, not
> the permission bits**: `BEGIN IMMEDIATE` — the genuine write lock that
> produced *"attempt to write a readonly database"* — is now **GRANTED** (rolled
> straight back; the delta is unchanged at 0 rows / 6 schema objects).
> `test -w` as `stg135` passes. SQLite can raise that error from an unwritable
> journal directory as well as the file, so testing the write path rather than
> the mode bits was the discriminating check.
> ⚠️ Still true: if the gateway ever **recreates** the file it returns to `644`
> under the default umask. The durable form is a `002` umask on the gateway
> service — worth doing only if recreation starts happening (it has not since
> 11 July).

```
WARNING: live-delta prune failed: attempt to write a readonly database
/vast/ishi/hardlinks/hard_links_live.sqlite   -rw-r--r--  gazetteer:ishi
prune runs as stg135; both users are in group ishi; the DIRECTORY is drwxrwsr-x (setgid)
```

**Non-fatal by design** — the code swallows it so a prune failure cannot block a
completed publish. ✅ **And S3 checked rather than assumed whether it mattered:
the live delta holds 0 rows**, so there was nothing to prune and **no
duplication risk** (consistent with `attestation_input: 0` — the live-forwarding
flow has never written).

⚠️ **It becomes real the moment that flow activates:** a future publish would
leave already-folded rows in the delta for the gateway to **double-count**.

**Fix — one command as `gazetteer` or root:**
`chmod g+w /vast/ishi/hardlinks/hard_links_live.sqlite`
Only the file lacks group write; the directory already has it.
🛑 **Do NOT add `chmod` to `gaz_relay`'s allowlist** — that allowlist is a
token→fixed-command security boundary whose own header notes *anyone in group
`ishi` can drop a request*. Widening it for a permissions nit is the wrong
trade. ⚠️ If the gateway ever recreates the file it may revert to `644`; the
durable form is a `002` umask on the gateway service.

## Phase 3 — publication (Atlas, Beta-gated)

### 🛑 3.1 PRE-RETILE GATE — a geom-store miss renders the HULL, not a point, and every planned check passes

**Found by the Auditor, 2 Sep; verified here.** `generate_tiles._build_feature`
resolves in four tiers: `geom_ref` → store; `has_geom` → synthesized key →
store; **`hull` → rendered as Polygon/MultiPolygon** (`:1130`); `repr_point` →
Point (`:1139`). Tiers 3 and 4 are both guarded by `not require_boundary`, and
`require_boundary=False` is exactly the **per-namespace and per-WHG-dataset**
buckets (`:1094`). **So for `clio` and every other per-namespace bucket, a
geom-store miss degrades to the hull — up to 350.34° wide for the 588.**

The tier is deliberate and documented for a real case (WHG-computed
approximation polygons such as ottgaz admin hulls, deliberately kept out of the
store). **The hazard is that it cannot distinguish that case from a store miss.**

🛑 **S5's planned verification CANNOT SEE THIS.** A smeared feature **is** a
polygon: `poly=` is non-zero, the tileset's polygon count is right, and `clio`
+2,986 still lands. **Both named deltas pass in the broken world** — a
decorative check, and the exact failure this campaign exists to stop.

❌ **WITHDRAWN 2 Sep — DO NOT REINSTATE. The span assertion fails in BOTH
directions, which is worse than no check because it manufactures confidence.**

~~assert the maximum longitude span of each rendered feature — expect zero
features above 180°~~

* **False positives, measured:** a naive `max(lon) − min(lon) > 180` flags **six
  legitimate `un` countries** — `un:ata` 360.00, `un:rus` 360.00, `un:fji`
  360.00, `un:usa` 358.93, `un:nzl` 355.47, `un:kir` 348.57 — all circumpolar or
  genuinely antimeridian-crossing. **Verified independently here against
  `un/final`: exactly those six, to the hundredth.**
* **False negatives, and it cannot be rescued:** normalising the wrap **tightens
  the Spanish Empire hull from 232.63° to ~140°**, letting the real smear
  through.

🛑 **My own error, and it is the sharper half.** I championed this as *"the only
check on S5's row with a known-correct answer"* and told S5 to prove it **fails**
on `clio:es_spanish_emp_1_1572_1578`. It does. **I never asked whether it fires
on good input.** S5 did, and that is what killed it. **Proving a check fails on
known-bad is only HALF the validation — the other half is proving it passes on
known-good, and I enforced the first half all day while omitting the second.**

✅ **Replaced by a STRUCTURAL gate — SG's own commit `007a870`,
"refuse to publish a tileset the build didn't read geometry for".**
`_build_staged_feature` now records **which tier** produced each feature, and
`publish_gate` refuses the push when (1) the store was asked and returned
nothing for every lookup — **7 August exactly**, (2) anything renders from the
inline `hull` outside `whg-*`, or (3) the `.mbtiles` is empty or unreadable. A
refusal **leaves the file on disk, leaves the deployed tileset serving, marks
the namespace failed not completed, and exits non-zero so the `afterok` config
rewrite cannot fire.** **The tier is unambiguous where the geometry is not** —
which is why it succeeds where a geometric heuristic could not.

💡 **This also explains the nine point-only layers better than we had.** *Which
stage the tiler resolves to changes the failure mode:* `final/` carries **no
hull**, so a store miss there yields **points** — which is what 7 August
produced. `extract/` and `h3_merged/` JSONL **do** carry hull, so the same miss
there yields **smears**. One fault, two signatures, selected by stage.

⚠️ **"Crosses the antimeridian" names two different predicates — always say
which.** The **588** were measured as **hull longitude SPAN > 180°**. The
codebase's own test, `helpers._crosses_antimeridian` (`:836`), is **per-ring
edge jumps > 180°** — *"the signature of a polygon straddling ±180 (e.g. US via
the Aleutians)"*, whose docstring notes such geometries get a degenerate ~360°
bbox and mis-fill. **A MultiPolygon with parts either side of the Pacific is
True on the first and False on the second** — exactly what the plan measured for
`un:usa` at L2553. **Different populations by construction.** S9 has been asked
to run `_crosses_antimeridian` over the 309 store polygons, which discriminates
its two candidate faults directly.

### ✅ S5's TWO CHECKS — full specifications, filed 2 Sep before S5 runs them

**Filed here deliberately.** These converged across three sessions' messages
while S5 is blocked on 2.7; if its session ends first the reasoning goes with
it. **A specification that exists only in a message is the failure this audit
exists to catch.** All three load-bearing claims verified here against the code.

**CHECK 1 — parsed, pre-retile, no tileset needed.**

> Count `whg` docs whose geometry entry has a **`hull` of type `Polygon` or
> `MultiPolygon`** *and* where neither `geom_ref` nor `has_geom` resolves
> against the geom store. **Expect 0.** Count distinct `place_id`s in the same
> pass.

⚠️ **The type filter is load-bearing, not a refinement.**
`helpers.enrich_geometry` computes `hull = geom.convex_hull` with **no type
gate** (`:1200`) and assigns `entry['hull']` unconditionally (`:1267`) — **so a
bare Point hulls to a Point and a 2-point MultiPoint to a LineString.** ~57% of
`whg` docs carry a `"hull":` key (16,427 measured in 20 MB, all genuine keys).
**Specified without the type filter the check returns ~135k and means nothing**,
because `generate_tiles:1136` renders **only** a hull typed
`Polygon`/`MultiPolygon`.

**0 is derived, not assumed:** a Polygon-typed hull needs ≥3 non-collinear
vertices, so within `whg` it can arise only from the 770 Polygons, 478
MultiPolygons, and the ~690-member MultiPoint tail (2.3: 7,529 MultiPoints
holding 8,219 points, so all but ~690 are single-member and hull to a Point).
**All are in the store — 2.3 wrote all 9,849.**

The **distinct-`place_id`** count settles whether the extract holds more than
one run's docs (`write_staged_place_doc` **appends**) without extrapolating.
⚠️ **Do not substitute a bytes-per-line estimate** — staged `places.jsonl` is
ordered and geometry-bearing docs are longer, which is the `gn`
100.00%/65.83%/99.94% trap.

**CHECK 2 — span assertion, per polygon bucket, post-build pre-deploy.**

> After building each polygon bucket, count features whose bounding box exceeds
> **180° of longitude. Expect 0.** **Prove it FAILS first** by building a
> feature from `clio:es_spanish_emp_1_1572_1578` (232.63°) before trusting a
> pass.

**Keep this even though the submitter path cannot reach tier 3.**
`_eligible_buckets` requires `final/`, and `final/` carries no hull, so a store
miss there degrades to points — **but that protection is a side effect, not a
design.** Nothing in `_build_feature` knows the gate exists, and the `whg-*`
loop (`submit_tiles_slurm:229-231`) gates on **`_stage_completed(manifest, …)`
— manifest status only, with no filesystem check** (verified), so it bypasses
the protection entirely for the larger half of S5's run.

🛑 **The real reason to keep it: it is the ONLY assertion on S5's row with a
known-correct answer.** `poly=` non-zero, the tileset polygon count, `clio`
+2,986 and `wd` +58,657 are **all satisfied by a broken world**. *"0 features
with span > 180°"* is not.

⚠️ **The bypass route costs more than leaving staged trees wrong.** Running
`generate_tiles` directly on named buckets, or via `TILE_ES_DOC_NAMESPACES`,
skips the `final/` gate and can resolve to `h3_merged/` or `extract/` **where
hull is abundant — moving the failure mode from points to smears.** Worse, and
more visible. (S5's argument, and the strongest case against the shortcut yet.)

ℹ️ **Specimen worth keeping:** the Auditor hypothesised the 16,427 were
substring matches — the *correct* rule (parse, don't grep) invoked against a
measurement that had **already satisfied it**. S5 refuted it by re-measuring
with a sharper anchor (`grep -o '"hull"[^:]'` → **0** non-key uses). The real
cause was the ungated `convex_hull` above. **A rule can be right and still be
misapplied; the refutation came from better anchoring, not from the rule.**

### ⚠️ 3.1 — S5 CANNOT derive an expected polygon count from `geom_class` (2 Sep)

**Consequence of the `field-notes` correction, landing on S5's own row.** With
`geom_class = point` carrying areal covers measured **248 of 248 defective in
`whg`**, and `MultiPoint`→`point` invisible on the other side, **that predicate
undercounts in exactly the namespace that is half the retile** (the 47 `whg-*`
buckets). ✅ **So the expected polygon count must come from STORE KEYS — the
9,849 counted independently by S5 and by me — decomposed by stored geometry
type, not from the class field. Counted rather than classified**, which is the
same property that made the hull bound sound.

ℹ️ **But `geom_class` plays no part in tile RENDERING** — `_build_feature`
resolves `geom_ref`/`has_geom` → store, then inline `hull`, then `repr_point`,
and **never consults the class** (verified). So the correction changes what S5
is entitled to **predict**, not what gets **drawn**. **Easy to conflate, and
the distinction is the useful half.**

### 🛑 3.1's COMMAND DEPLOYS. IT DOES NOT BUILD. (found by S5 in a dry-run, 2 Sep — nothing submitted)

**Verified here, all three parts:**

```
generate_tiles.py:2229   --no-deploy   dest='deploy', action='store_false', DEFAULT=True
                         comment: "Deploy is ON by default — per-bucket auto-push
                         to the tileserver as each .mbtiles completes"
pushes fire at :2019 and :2115   `if deploy and not push_mbtiles_to_tileserver(mbtiles)`
submit_tiles_slurm.py:318-321    writes: generate_tiles --run-id .. --manifest-path .. --bucket "$BUCKET"
                                 NO --no-deploy
grep -c deploy submit_tiles_slurm.py  ->  0     (zero deploy awareness, no passthrough)
```

**So §3.1's literal command — `python -m processing.submit_tiles_slurm --run-id
h3ccode-20260805T120000Z` — pushes each `.mbtiles` to the LIVE tileserver as its
bucket completes**, then the trailing job runs `update_tileserver_config
--execute` (config rewrite + restart + verify).

🛑 **The buckets go live ONE AT A TIME, as each finishes — so there is no moment
at which the tilesets exist built-but-undeployed to be checked.** Therefore
**both** of the following are **unachievable with the command the plan gives**:

* §3.1's own precondition 3 — *"assert a non-zero polygon count per bucket
  **before deploying**"*;
* SG's standing constraint that **deploy is a separate decision from build**.

`--no-restart` does **not** help: it suppresses only the trailing config rewrite;
**the per-bucket pushes have already happened.**

⚠️ **And it would overwrite the nine poly-less tilesets — the live counterparts
of the preserved fixtures — before either check could run.** Those fixtures exist
*because* that deploy destroys the evidence.

**This is the campaign's signature fault in the plan itself: the prose says
check-then-deploy, the command does deploy-while-building, and the gap survived
because everyone read the command as doing what the paragraph around it said.**
S5 found it in a `--dry-run` **before submitting**, which is the only reason it
was found at all.

**Options — SG's, not ours:**

* **(a) Add a `--no-deploy` passthrough to `submit_tiles_slurm`** — one argparse
  flag plus one conditional fragment on the `:321` line. Small, reusable, and it
  makes §3.1's documented sequence actually performable. **S5's recommendation
  and mine.**
* **(b) Hand-roll an sbatch** calling `generate_tiles --no-deploy` per bucket —
  touches no shared code, but **reimplements the tiering/array logic** and risks
  divergence from the submitter. *(This campaign's own history is against
  hand-rolled sbatch: it is how `un`'s cover was broken.)*
* **(c) Proceed with auto-deploy** — contrary to SG's instruction and §3.1's own
  gate.

**S5 is HOLDING and has submitted nothing.** Its readiness is otherwise
complete: **both FAIL demonstrations pass** — preserved fixtures decode to **0
polygons** on 282 and 191 point-only features (proving the polygon check can
fail *and* the fixtures are readable), and the span assertion **rejects
`clio:es_spanish_emp_1_1572_1578` at exactly 232.63°**. Eligibility resolves to
**75 buckets — 27 non-`whg` plus 48 `whg-*`** — with `gn` and `wd` both present,
so **2.7 genuinely unblocked it.**

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

**⚠️ FIRST — prove the verifier can fail AND that it passes on known-good, and
do it before you deploy, because deploying destroys the evidence.** 🛑 **Both
halves are mandatory: the span assertion proposed here was withdrawn precisely
because only the fail-half was demonstrated** — it rejected the known-bad hull
correctly *and* flagged six legitimate `un` countries. **A check demonstrated
only to fail is half-validated.** S4's point (31 Aug) is that Phase 3 looks like
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

✅ **3.1 IS NOW COMPLETE AND INDEPENDENTLY VERIFIED (3 Sep)** — 73 tilesets
pushed, 0 gate refusals, the nine formerly poly-less layers confirmed serving
real MVT geometry from tile coordinates computed independently of S5's own
samples. So this step's stated precondition is met.

🛑 **BUT S7 MUST STILL WAIT. Do not start. Two new hazards post-date this
step's original scope, and one of them is a live job.**

**HAZARD 1 — `/ix1` has an active writer.** `indexing-db` is building the OSM
water import in `/ix1/ishi/water233-20260902T220703Z`, with an ocean rebuild
in flight and an expected peak around **250 GB**. Nothing in `/ix1` is safe to
inventory-and-delete while that runs.

**HAZARD 2 — there are now TWO tile generations and BOTH must survive.**
`/ix1/ishi/data/tiles-20260902-retile` is the generation deployed 3 Sep;
`/ix1/ishi/data/tiles` is the August rollback and is the **only** way back if
the new tilesets prove unstable. ⚠️ The original scope was written when only
one generation existed. **Neither directory's `.mbtiles` may be deleted** —
only the scratch *inside* them.

**Revised targets, split by volume — the `/vast` half is the valuable half:**

| target | vol | size | status / notes |
|---|---|---|---|
| `places_temporal-20260731t160000z` | `/vast` | 23 GB | ✅ **ALREADY DELETED 2 Sep** by S7 on SG's approval — `{"acknowledged":true}`, target 404, both aliases re-verified intact, data dir gone. **Do not re-run its precondition; a DELETE here now issues against a 404.** |
| `/vast/ishi/tiles-verify` | `/vast` | 17 GB | 🛑 **NOT a free deletion — see below. A minimal known-bad reproducer must be extracted FIRST.** |
| `/ix1/ishi/data/tiles/_step0` | `/ix1` | 2.7 GB | scratch only — **not** the sibling `.mbtiles` |
| stale `*.geojsonl` in `/ix1/ishi/data/tiles` | `/ix1` | ~20 GB | incl. `osm_admin.*` from the May 2025 pre-rename era |

🛑 **`tiles-verify` IS THE ONLY KNOWN-BAD ARTEFACT SET IN EXISTENCE.** Those
17 GB are the poly-less tilesets from the 7 August run, preserved deliberately
so that a verifier can be **proved to fail** on them. This campaign's own
doctrine is that every gate must be shown to FAIL on known-bad as well as pass
on known-good — and **deleting these fixtures makes that permanently unprovable
for every future verifier**, to reclaim 17 GB on a volume with 226 GB free and
a low watermark at 153.6 GB. That is not urgent space.

✅ **Required before any deletion here: extract a minimal reproducer** — likely
a few MB, one bucket's poly-less tileset plus its manifest — and delete only
the bulk. If that reproducer does not exist, `tiles-verify` stays.

**The agreed protocol (S7, 3 Sep), recorded here so it does not close with the
session that devised it:**

1. Extract one poly-less bucket's tileset plus its manifest from `tiles-verify`.
2. Run the verifier against it and **require it to FAIL**.
3. Run the same verifier against a **known-good tileset from the 3 Sep
   generation** and **require it to PASS**.
4. Report both results to the coordinator. **Only then** propose deleting the
   bulk — the deletion is not S7's to make on its own reading.

⚠️ **Step 3 is not optional padding.** A check that fails on everything is as
useless as one that passes on everything, and a reproducer validated only by
step 2 cannot tell those apart. This is the campaign's both-directions rule
applied to the fixture that exists to enforce it. **A reproducer nobody has
tested in both directions is a comment.**

**Remaining reclaim is therefore ~17 GB on `/vast` (gated as above) and ~23 GB
on `/ix1`** — the 23 GB row is already freed and is *inside* the 226 GB figure,
not additional to it. `/vast` is the constrained volume: 226 GB free of 1 TB,
**shared with production Elasticsearch**, low watermark at 153.6 GB free,
**flood stage at 51.2 GB free turning every index read-only**. `/ix1` has
1,824 GB free of 5 TB and no ES on it, so its half is housekeeping.

✅ **Reading `/vast` headroom — use ES, not `df`.** `_nodes/stats/fs` reports
`path /vast/ishi/es/data  mount /vast/ishi  total 1099.5 GB  free 242.2 GB`,
and `_cat/allocation` gives `disk.avail 225.5gb` (same measurement, GB vs GiB).
This is the cheapest correct reading: it names the mount, needs no filesystem
walk and no login-node ssh, and **it is the figure ES is actually watermarking
on**, which is what decides flood stage. ⚠️ Bare `df /vast` reports the 3.9 PB
shared pool and will show petabytes free while our 1 TB quota is nearly full —
stat `/vast/ishi` if using `df` at all (that bug was live in
`build_geom_index_sqlite.sbatch` until `d506837`).

**Release condition:** `indexing-db` reports its `/ix1` footprint settled, and
SG is satisfied the 3 Sep tilesets are stable enough to release the August
rollback. Until both, S7 waits.

---

### ✅ HOW THE FIRST-EVER PUBLISH ACTUALLY WENT — and S3's closing knowledge

**It ran CLEAN first time. No debugging, no retries, no flag-hunting, no path
surprises.** ⚠️ **S3's own warning — *"expect to debug the publish path, not run
it"* — is RETIRED as wrong**, on its own report. It was well-founded when made
(the step had never run) and the outcome should not leave the next person
over-cautious: `publish_hardlinks` **dry-runs by default** and names all four
targets explicitly (`would_publish`, `to`, `would_prune`, `marker`), and
`--execute` did exactly what the dry-run said. **Approach it as a normal
command.** The only friction was a harness permission classifier, not the code.

🛑 **RESIDUAL 1 — `publish_hardlinks` never checks `--db-path` was built by
`--run-id`.** Verified in code: `:71` validates **only** `db_path.exists()`;
`:91` stamps the marker with whatever `--run-id` says; `:127-128` falls back to
`hard_links_{run_id}.sqlite`. **A mismatched pair publishes one corpus while
recording another's provenance — silently, with a plausible marker.** S3 had to
pass `--db-path` (to reach `…-postmerge.sqlite`), and the default would have
published **an abandoned 248 MB partial from the wedged run** under a clean run
id. It escaped only because that file had already been deleted, so the fallback
raised `FileNotFoundError`. **This is the campaign's signature defect sitting in
the last step of the last row.** Fix: stamp `run_id` inside the database and
check it at publish.

🛑 **RESIDUAL 2 — `--cutoff` has the same shape.** It must come from the log of
the run that produced *that* database (`Live-delta prune cutoff (harvest start)`
at the top of the harvest log) and **nothing stops you using another**. S3 was
carrying a void one (`2026-08-31T21:36:34`) from the abandoned build and had to
consciously discard it. **Two independent arguments that must agree, with
nothing checking they do.**

**Operational knowledge that would otherwise have closed with S3:**

* ⚠️ **The harvest sbatch runs FOUR sequential Python processes**
  (`hard_links_staged` → `loc_links` → `contributor_replay` → a `finalise_local`
  heredoc), **not one.** So *"modules are already loaded, a pull is safe
  mid-run"* is **FALSE** for this job — the later three import fresh. The real
  test is `git diff --name-only <clone HEAD>..origin/main` over the modules the
  **remaining** phases import.
* **`py-spy dump` hangs against a `D`-state process** (it needs ptrace) — so the
  recommended progress tool is unavailable in **exactly** the wedge case you
  most want it. Use `/proc/<pid>/io` + `wchan` + a `timeout 8 ls` on the mount.
* **`/ix1` read throughput fell to ~290 KB/s** without being wedged. A 1.33 GB
  comparison that showed no progress in an hour finished in **2:21** after one
  `cp` of the incumbent to `/vast` and comparing locally. **Staging a
  read-heavy input to `/vast` is cheap and reversible** — not the same as
  *writing* output there.
* ✅ **`compare_hardlink_overlays` is committed and validated** against
  known-good, known-bad, sub-tolerance noise and the `--allow-shrink`
  exemption. **Reusable for any future overlay publish, not a one-off.**

## Phase 4 — tracked, not scheduled

### 4.15 Split tile `work_dir` from `output_dir` — intermediates to node-local scratch

**SG's refinement, 2 Sep. Sound, contained, and deliberately NOT sequenced ahead
of S5's retile.**

**The defect it fixes:** `generate_tiles` writes the `.geojsonl` intermediates
**and** the finished `.mbtiles` to the same `out_dir` (`:1594`, `:1603`, `:1913`,
`:1929` vs `:1776`). On `/ix1` at a measured **27.3 MB/s** every intermediate
byte is written slowly and then read back slowly by tippecanoe — and on `/vast`
they would instead consume headroom that is already at 204 G and falling.
**Node-local scratch avoids both.**

**The change:** thread a `work_dir` beside the existing `output_dir`, **defaulting
to `output_dir`** so behaviour is unchanged unless set; point the four
`.geojsonl` paths at it; leave `.mbtiles` on `output_dir`.
`submit_tiles_slurm` already threads `--output-dir` (`:281`, `:507`), so
`--work-dir` follows the identical path.

✅ **The safe scratch idiom already exists in-repo and must be used:**
`es_staging.sbatch:56` uses **`${SLURM_SCRATCH}`**, and `:89` carries the
warning about reconstructing `/scratch/slurm-$SLURM_JOB_ID` — **that is Fault 9**
(the staging info file exported the *staging job's* `SLURM_JOB_ID`, so a
consuming job addressed another job's scratch). **Do not rebuild the path from
a job id.**

🛑 **Why it is NOT scheduled before the retile:**

1. It is **a code change to a script about to be run**, which is this campaign's
   most-repeated lesson.
2. **Node-local scratch capacity is unverified.** A bucket whose `.geojsonl`
   exceeds it fails in a **new** way, on the first retile since the geom-store
   loss — trading a known slow path for an unknown failure mode.
3. The benefit is **build wall-time on a Beta-gated deliverable with no
   deadline**, against SG's standing ruling that correctness outranks it.
4. The current path **demonstrably works** — it built the existing 28 G.

✅ **Better sequencing: run the retile as-is, and let it produce the numbers this
change needs.** Nobody currently knows the total `.geojsonl` volume for a
74-bucket run (`alc.geojsonl` is 4.8 MB; `gn.mbtiles`/`wd.mbtiles` are 1.6/1.9
GB, and GeoJSON typically runs several times its mbtiles). **Measure it, measure
the scratch, then implement with real figures** — and per doctrine, the test
must **FAIL on the pre-change code** before it is trusted.

⚠️ **If S5's build turns out painfully slow, that is the evidence that promotes
this from tracked to scheduled.**



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
| 4.14 | ⚠️ **761 `clio` features whose stored `h3_cover` does not contain their own `repr_point`** (S9's probe; framing by S8, and the framing is the contribution). ~~This is **not** the antimeridian defect — `hullX = 0` for all of them.~~
❌ **STRUCK 2 Sep — THE EXCLUSION IS VOID, CONFIRMED BY S9 ITSELF.** `hull` is an *ingestion
intermediate*: `staged_parquet.strip_hull` drops it from every parquet sidecar,
so a probe reading anything downstream of `extract/` sees **no `hull` at all**
and must report `hullX = 0` whether or not the defect is present. Measured 2 Sep: `hull` occurs **0 times in
`schemas/places.json`** — it is not in the ES mapping at all — and `strip_hull`
runs on the **index path too**, in `index_namespace.py:134,140` and
`index_from_stage.py:106,113`, for parquet rows *and* JSONL lines. So `hull`
survives in exactly one place:

| source | can `hull` be present? |
|---|---|
| live index | **no** — stripped on the index path; 0 occurrences in the mapping |
| any `places.parquet`, any stage | **no** — `strip_hull_for_parquet` |
| `extract` / `update_merged` / `boundary_merged` / `h3_merged` **JSONL** | **yes** |
| **`final/places.jsonl`** | **NO — measured 2 Sep** |

⚠️ **`final/places.jsonl` carries no `hull` either, which narrows the surviving
carrier further than first stated.** `ccode_merge._iter_source_docs` prefers
`h3_merged/places.parquet` over the JSONL, and the parquet is already
hull-stripped — so `final/` inherits hull-less documents no matter that
`ccode_merge` applies only `normalize_for_parquet` (empty-list → `None`) on the
way out. Measured, both namespaces that have a `final/`:

```
un/h3_merged  hull=247    un/final  hull=0
nl/h3_merged  hull=4,363  nl/final  hull=0
```

**This makes the §4.14 exclusion *more* likely void, not less**, because the
761 are described by their **stored** fields and `final/` is the most likely
staged source for that word. Revised outcome list: *field, from `extract` /
`update_merged` / `boundary_merged` / `h3_merged` JSONL* → **stands**;
*recomputed from the geom store* → **stands**; *field, from ES, any parquet,
**or `final/places.jsonl`*** → **VOID**.

✅ **ANSWERED BY S9 (`indexing-98`), 2 Sep — job 11105892, 7 s, read-only.**
**Its probe read `clio/final/places.jsonl`, which carries no `hull` field at
all** (0 of 15,690 — measured independently here before the question was put).
**So `hullX = 0` could never have fired.** S9 confirms it had retracted that
column at the time — *"ignore the hullX column entirely… it reads 0 everywhere
including `un`, whose hull we have proved crosses"* — and that **its caveat was
recorded in 4.14 as its opposite.** S9 asked for the correction rather than
defending the row.

**S9 regenerated the 761 and intersected them against the 588:**

```
in both              309     (41 per cent of the 761)
selfEXCL only        452
antimeridian only    279
ids: /vast/ishi/diagnostics/clio_selfexcl_s9.txt
     /vast/ishi/diagnostics/clio_antimeridian_hulls_20260902.txt
```

Overlap examples are `clio:es_spanish_emp_1_1572_1578_v1` and siblings — the
Spanish Empire, Philippines to the Americas, crossing ±180 exactly as `un:usa`
does. **The mechanism is present, not excluded.**

✅ **MECHANISM SETTLED, 2 Sep — job 11105893, all 309, 0 no-verdict, read-only.
`clio` IS `un`'s fault, and the recompute set is ONE FAULT IN FOUR NAMESPACES.**

```
A. _crosses_antimeridian(POLYGON)  [what compute_h3_fields keys off]:     0   (0%)
B. hull lon-span > 180             [the 588 predicate]              :   309 (100%)
   both A and B                                                     :     0
C. stored != fresh_from_polygon    [the cover is WRONG]             :   309 (100%)
D. stored == cover_from_HULL       [hull-derived — the un signature] :   264  (85%)
```

1. **`clio`'s covers are hull-derived** — 85% **byte-identical** to the cover
   computed from the convex hull, the same test that established it for
   `un:usa`. Same fault, same silent geom-store fallback, same three stacked
   `except Exception` blocks.
2. **"Antimeridian handling applied to a genuinely-crossing polygon" is RULED
   OUT** — **0 of 309** polygons trip the codebase's predicate.
3. **The two predicates are perfectly disjoint here** — 309/309 on hull-span,
   0/309 on ring-jumps, zero overlap. This run is the demonstration that they
   are not interchangeable.

**So the recompute set stops being a list and becomes a class: `un`, `nl`,
`clio`, `whg` are one fault, remedied the same way, and `6ad2640` prevents
recurrence by making the fallback raise.**

⚠️ **Two things S9 explicitly does NOT claim, preserved because the restraint is
the load-bearing part.** (i) **45 of the 309 (15%) are wrong but not
byte-identical** to a hull-derived cover — all are wrong so remediation is
unaffected, but hull-derivation is established for 85% and **unexplained for the
rest**.

> 🔬 **S9 tried to close the 45 and failed — job 11105903. Two named hypotheses
> eliminated, a third proposed. "Unexplained" is now a stronger statement than
> when first recorded, not a weaker one.**
>
> The Auditor's hypothesis was strong and predicted the exact shape of the
> result: `enrich_geometry` can substitute the **envelope** for an invalid hull,
> then `buffer(0)`, round to 6 dp and `make_valid` — so `entry['hull']` need not
> equal `geom.convex_hull`, and test D **recomputed** the hull (because
> `clio/final` carries none). The 45 might simply have been the repaired cases.
>
> ```
> h3_merged docs carrying a hull dict : 309 of 309   premise CONFIRMED
> final cover == h3_merged cover      : 309 of 309   checked, not assumed
> recomputed convex_hull reproduces   : 264
>   residual re-tested with STORED hull: 0 closed, 45 STILL unexplained
> hull vertex counts: min 12, max 35  (threshold 5,000 — simplification CANNOT have fired)
> ```
>
> **45 → 45.** Refuted, and simplification eliminated as a bonus.
>
> ❌ **STALENESS REFUTED TOO — job 11105905. FOUR hypotheses eliminated.**
> The Auditor's discriminator needs no timestamps at all: **a shard rewrite is
> not per-key selective**, so if the 45 and the 264 share shards, a store
> rewrite cannot distinguish them.
>
> ```
> shards holding the 264 hull-derived : [83, 84]
> shards holding the 45 residual      : [83, 84]
> shards with ONLY residual           : []
> 45 of 45 residual keys share a shard with a hull-derived key
> ```
>
> **Perfect interleaving.** A re-merge replacing the 45's geometry would have
> replaced the 264 in the same two shards — and those reproduce their covers
> exactly. Consistent with what CLAUDE.md already documents: `geom_store
> --merge --keep-staging` leaves existing shards untouched.
>
> **Eliminated: dateline-crossing polygon (0/309); the `enrich_geometry` repair
> path (45 → 45 against the stored hull); simplification (12–35 vertices against
> a 5,000 threshold); staleness (shard interleaving).** Code-version-at-write-time
> is the last standing candidate and **S9 explicitly declines to promote it by
> elimination — "the only hypothesis left" is not evidence**, and testing it
> would need the `clio` h3 job id and the reflog window, which is the same
> reasoning that misled this campaign once already.
>
> 🔒 **Freeze the row here: 264 hull-derived, 45 wrong by an undetermined
> mechanism, all 309 remediated identically.** C is 309/309; the recompute fixes
> every one regardless of cause. **Nothing in the decision moved — the third
> time that has been true in this thread, which is why it was safe to keep
> digging.** (ii) This measures the **309-feature intersection, not all 15,690**;
C = 100% here does **not** license "100% of `clio` is wrong" — the sampled
figure was 22%.

ℹ️ **An epistemics note worth keeping.** I originally wrote that `clio` is in
the recompute set *because its covers are hull-derived*; S9 challenged it on the
ground that `clio/final` carries `geom_ref`, and I retracted. **The retraction
was right and the claim was also true.** Field presence tells you what
`cover_geometry_for` *would* do **with a working reader** — not what it did; `un`
had `geom_ref` too. So the original claim was **unsupported when made** and is
**now supported by measurement**. Both steps were correct: asserting it without
evidence was wrong, and doubting it was right, and the measurement settled it.

~~🛑 **THE INTERSECTION REFUTES THE EXCLUSION; IT DOES NOT ESTABLISH THE
MECHANISM**~~ — superseded by the above; retained for the reasoning — S9's caveat, and it corrects an overreach of mine.** I wrote that
`clio` is in the recompute set *"precisely because its covers are hull-derived"*.
**Not established.** `clio/final` carries `geom_ref`, `has_geom` and `geom_class`
per geometry (verified here on `clio:sumerian_city-states_-3400_-3201`), so
`cover_geometry_for` would have resolved the **geom-store polygon**, not the
hull. **Two different faults share one signature:** a hull-derived cover, or
correct polygon retrieval with **antimeridian handling applied to a polygon that
genuinely crosses ±180** — and the Spanish Empire's does.

**The decisive test is stored-vs-fresh on the 309**, which S9 has offered to run
and which is cheap. Until it runs the row records *the exclusion is void*, and
**not** *the hull defect explains `clio`*.

ℹ️ **The 279 antimeridian-only ids are expected, not anomalous.** `selfEXCL`
caught 8 of 247 on `un` — ~3 per cent against a namespace 96 per cent materially
wrong. **It under-reports by design: it can confirm, never clear.**

🛑 **THE ANTIMERIDIAN MECHANISM IS PRESENT IN `clio` AT SCALE — 588 FEATURES.
MEASURED 2 Sep, ALL 15,690, NOT A SAMPLE.** This does not by itself overturn
§4.14's exclusion, but it **removes the reading on which the exclusion would be
safe regardless of provenance**, and it is a data-correctness finding in its own
right.

```
clio docs examined         15,690
geometries examined        15,690
with a hull key            15,690     (every one — the source carries hull)
hull lon-span > 180°          588     <- SPAN predicate (see 3.1 note)
widest hull lon span       350.34°
examples: clio:es_spanish_emp_1_1572_1578  232.63°   (and _v1, 1579_1581, 1582_1587 …)
```

The examples are self-corroborating: **the Spanish Empire spans the Pacific**, so
its convex hull crosses the antimeridian by construction. This resembles the `un` defect —
but ⚠️ **it is NOT established that `clio`'s covers are hull-derived** (S9's
caveat, below): `clio/final` carries `geom_ref` per geometry, so
`cover_geometry_for` could resolve the store polygon. A hull-derived cover on a
232°-span hull would cover the wrong half of the globe; so, independently, would
mishandled antimeridian wrapping of a polygon that genuinely crosses.

⚠️ **Stated as hypothesis, not finding, and it needs one Slurm job to settle.**
588 crossing hulls and 761 `repr_point`-outside-`h3_cover` counter-examples are
**the same order of magnitude**, in the same namespace, with a mechanism that
would produce exactly that symptom. **They may be largely the same features.**
Nothing here proves it — the intersection has not been computed, because `h3` is
not importable on the VM (`shapely` 2.0.7 is; `h3` is not) and the test needs a
compute node.

**The 588 ids are written out** —
`/vast/ishi/diagnostics/clio_antimeridian_hulls_20260902.txt` — so the
intersection needs no recomputation by whoever holds the other list.

**The prediction is DIRECTIONAL, which makes the test quantitative rather than a
vague comparison.** A hull-derived cover over a >180° hull covers the *wrong half
of the globe*, so a feature's own `repr_point` will **almost never** fall inside
it. The mechanism therefore predicts **near-total containment in one direction**,
not mere overlap:

```
of the 588 crossers, how many fail repr_point ∈ h3_cover?
  ~588 of 588 fail   -> mechanism CONFIRMED as a cause; the exclusion is wrong
  far fewer          -> mechanism present but NOT the cause; the exclusion survives
residual = 761 - |588 ∩ failures|   <- the counter-examples still unexplained
```

If all 588 fail, **173 remain unexplained** — and 173, not 761, is the number
that should carry forward as the open correctness question.

**Cheapest route first, and it needs no compute node: ask S9 whether it still
has the 761 ids.** If so this is a set intersection against the file above and
nothing needs recomputing. The `h3` recompute is the fallback if that list is
gone. *(Note the question has moved twice: "which file did the probe read?" →
"field or recomputed?" → "do you still have the ids?" — each cheaper and more
directly on the point than the last.)*

**⭐ The best test is FREE, because `clio` is already in the recompute set.** Its
covers are hull-derived, which is why it is queued — so **the fix for the 588 is
already scheduled work**. After `clio`'s cover recompute, re-run the
`repr_point ∈ h3_cover` census over all 15,690 and compare against 761:

* **drops to ~173** → the 588 were the cause; the exclusion was wrong; the
  residual is the real 4.14
* **drops to ~0** → the whole row was the hull defect
* **stays ~761** → the mechanism is present but is not the cause; the exclusion
  stands and 4.14 is untouched

Every outcome is informative, it adds no job, and it runs where `h3` imports
anyway. **If it holds, 4.14 is not a separate residual at all — it is an
instance of Fault 14 in a namespace already queued for the fix.**

⚠️ **Keep the row open until that census actually runs.** "Already scheduled" is
not "already verified" — which is this campaign's own lesson.

**Either way §4.14's row stays open.** Even a surviving exclusion narrows the
cause without closing it: the `repr_point`-within-geometry invariant that
`gateway/spatial.py:11`/`:900` and `ccode_enrichment.py:518` document themselves
as relying on is still contradicted 761 times.

✅ **`clio` MEASURED DIRECTLY, 2 Sep — the question is now answerable, and the
prediction that `clio` would be hull-less throughout is REFUTED:**

```
clio/extract      parquet=no    jsonl hull = 15,690
clio/h3_merged    parquet=YES   jsonl hull = 15,690   <- hull SURVIVED
clio/final        parquet=YES   jsonl hull = 0
```

`clio`'s `extract/` has **no parquet**, so `h3_merge` read the JSONL and `hull`
carried through to `h3_merged/`. (15,690 = exactly the `clio` geom-store key
count measured the same day — one hull per geometry, a clean cross-check.) It
dies only at `final/`, where `ccode_merge` read the stripped parquet.

**So the exclusion is genuinely live, and the outcome list collapses to a
single answerable question for S9 — which file did the probe open?**

* `clio/extract/places.jsonl` or `clio/h3_merged/places.jsonl` → `hullX` **could**
  have been non-zero → **the measurement is real and the exclusion STANDS**;
* `clio/final/`, any parquet, or the live index → **VOID, and the antimeridian
  hypothesis is not excluded**;
* recomputed from the geom store → **stands**, on any source.

The general mechanism, worth carrying: **all four merge readers prefer the
parquet** (`ccode_merge:68`, `h3_merge:100`, `boundary_merge:63`,
`update_merge:94`), so **`hull` dies at the first stage whose source directory
holds a parquet, and hull survival is namespace- and history-dependent rather
than a property of the stage.** `clio` kept it to `h3_merged` only because its
extract happened to be JSONL-only.

⚠️ **`ccode_merge`'s own comment is wrong** — it states "the canonical JSONL
keeps hull and explicit nulls intact for downstream consumers". The nulls, yes;
the hull, no, because the *source preference* silently defeats it. Harmless in
function (`hull` is an ingestion intermediate consumed before `final/`) and
misleading in fact — which is how this thread started.

**The question to put to S9 is not "which file" but "field read, or
recomputed?"** — the probe may have recomputed the hull from the geom-store
polygon, which is available on every path:

* **recomputed from the store** → exclusion **stands**, on any source;
* **field, from staged JSONL** → **stands**;
* **field, from ES or any parquet** → **VOID — the antimeridian hypothesis is
  not excluded.**

⚠️ **Two of the four outcomes reinstate a live correctness hypothesis** about
the `repr_point`-within-geometry invariant. Note also that 4.14 describes the
761 by their **stored** `h3_cover` and `repr_point` — fields that live in ES and
in the parquet, i.e. precisely the two sources where the exclusion would be void
by construction. **That is an inference from the wording, not a finding**; only
S9 can say which file it opened. It matters because it is not a data oddity but **761 counter-examples to an invariant two subsystems are documented as relying on**: `gateway/spatial.py:11` and `:900` ("Cheap, exact fast-reject: `repr_point` is guaranteed within the…"), `ccode_enrichment.py:518` ("`repr_point` is guaranteed to lie within the geometry"), and CLAUDE.md at `:359` and `:664`. If the guarantee does not hold, `containment=exact` can return a wrong answer for those features and the ccode refine can discard a correct candidate — **and neither would look like an error**. Triage as a correctness question, not a curiosity. No explanation offered by anyone yet; `clio` being one of the nine point-only boundary layers may or may not bear on it. |
| ~~4.15~~ | ❌ **REFUTED — do not record. `nl` CAN serve as a `contained_in` scope.** S8 reported that all 4,363 `nl` docs carry `has_geom=None` and `geom_class=None` in both its `final/` and the live index, so no Native Land territory could be used as a scope region. **Checked on both sides and it does not hold.** Its own `staged/nl/final/places.jsonl` reads `geom_class='area'`, `has_geom=True`; and in the live index `exists` returns **4,363 of 4,363** for each of `geometries.geom_class` and `geometries.has_geom` (ES `exists` is false for nulls, so they are set). Kept as a struck row rather than deleted, because a plausible user-facing defect that does not exist is worse in an audit than no row at all — and because whatever S8 read to get `None` is worth knowing (a parquet sidecar, or nested `_source` filtering, both of which can present set fields as absent). |
| 4.5 | AAT coverage 4,436 / 15,448 = 28.7% (place#142). |
| 4.6 | ⚠️ **PROMOTED 31 Aug from housekeeping to REQUIRED — and CORRECTED: this is S5's work, not a separate migration.** My first write-up filed the `whg-*` tilesets with the genuinely legacy `datasets-*` / `collections-*` family. They are not: `generate_tiles` builds them natively as **per-WHG-dataset buckets** (`_whg_dataset_sub_ids`, `whg-<dataset_sub_id>.mbtiles`, "one per contributor dataset discovered at submit time"), and the current 47 were produced by that same pipeline on 22–23 July. So S5 rebuilds them by naming those buckets — no separate project, no migration. The `datasets-*` / `collections-*` tilesets are the actual legacy family and stay in `plan-outstanding-2026-07.md` §8. S3 flagged that the `whg` tiles now carry dead place ids after 2.3's re-mint, and expected §3.1's 27-bucket retile to cover it. It does not: **there is no `whg` bucket**. `whg` is served as **47 legacy per-dataset tilesets** (`whg-<dataset_id>.mbtiles`, 22–23 July), which sit outside the 27 and are untouched by 3.1. Verified by decoding `whg-1052.mbtiles`: it carries `whg:1052:6954924`, `whg:1052:6954927` … — the old place-key form, which after 2.3 returns `found:false`. So **every click-through from those 47 layers is now dead**, and regenerating them is the completion of 2.3. **Add the 47 `whg-*` buckets to S5's run.** |
| 4.7 | Merge stages still hold whole patches in memory; the allocations are tiered, the profile is unchanged. |
| 4.8 | **41 of the 89 datasets referenced by contributor links are not in the index** (48 are). `contributor_replay` accepts `ds_status ∈ {indexed, accessioning, wd-complete}`; ingestion requires `Dataset.authority=True AND public`. 2.3's id map makes the mismatch harmless and visible, but the underlying question — publish them, or narrow the replay filter to match? — is a Django-side call for SG. |

---

## ⭐ 4.17 The mapping-versus-schema diff — 30 undeclared fields (Auditor, 3 Sep)

**Question nobody had asked:** which fields does the `places` index actually
*hold*, versus which does `schemas/places.json` *declare*?

```
live mapping fields              101
declared in schemas/places.json   71
UNDECLARED (accepted by dynamic)  30      5,891,998 field-instances
declared but ABSENT from live      0      <- every declared field exists IN THE MAPPING
```

⚠️ **That last line proves mapping-completeness ONLY — it says nothing about
whether any document carries the field.** Tested separately: **`depictions` is
declared, mapped, and holds 0 documents** — a genuine dead declaration the diff
could not see. (`descriptions` holds 26,867 and is fine; `exists` does not match
nested containers, which produced seven false zeros before the queries were
corrected against `types.identifier` 51,014,923 and `toponyms.label` 49,848,837
as controls.)

`dynamic` defaults to **true** on this index, so any authority can add a field
and nothing reports it. **Every undeclared field's doc count is exactly one
authority's full document total** — `source` 2,991,143 = `tgn`; the six-field
Ottoman block 16,296 each = `ofs`; `historical_county` 24,000 = `iv`;
`display_color` 4,363 = `nl`; the 247s = `un`. No field is shared between
authorities.

🛑 **THIS IMMEDIATELY REFUTED THE RECOMMENDATION IT WAS TESTING.** Thread 2,
reasoning from the single instance of top-level `h3_cover`, recommended
`dynamic: strict` so re-introduction would fail loudly. **That would have broken
production.** `geometries.boundary_source` is undeclared *and read by*
`ccode_enrichment.py:208-210`:

```python
sources = {(g or {}).get("boundary_source") for g in (doc.get("geometries") or [])}
(fallback if sources == {"bnda"} else primary).append(doc)
```

— the **primary/fallback tier split for `un` country polygons**, on the namespace
the whole corpus prefilters through. A second real-reader group
(`wikidata_qid`, `admin_unit`, `kaza`, `kaza_1848`, `liva_1848`) is consumed by
`processing/interlink_ottgaz.py`. **A recommendation derived from one member of
a class was wrong about the class**, and only the enumeration could show it.

**Serving cost, measured:** no undeclared field reaches an API consumer.
`/api/search` uses an explicit allow-list (`es_helpers.py:1219`); `/api/places`
fetches `_source: True` so all 30 cross ES→gateway on every call and are then
**discarded**, having no slot in `PlaceDetail`. Cost is storage plus per-request
transfer and parse of fields thrown away — sharpest in `h3_cover`/`h3_centroid`,
dynamically mapped as **`text`**, so H3 cell ids are analysed as prose in 1.31 M
documents and never queried.

### The remedy — declare first, tighten second

1. **Declare and keep:** `geometries.boundary_source` and the ottgaz interlink
   set. These are legitimate; the defect is the schema omission, not the field.
2. **Delete:** root `h3_cover` / `h3_centroid` (§thread 2), and root `timespans`
   (82,508 docs) — a duplicate of the nested path that nothing reads.
3. **Decide:** ~18 authority-local fields (`source`, `area_km2`,
   `historical_county`, `display_color`, `time_period`, `language_family`,
   `description`, `continent`, `subregion`, `region`, `admin_level`, `divan`,
   `nahiye`, `register_*`, `source_project`) — each one authority's own metadata,
   read by nothing. Declare as intentional per-source extras, or drop at the
   next re-extract.
4. **Only then** consider strict mapping, and **per branch** — `geometries` and
   the root have different populations and different readers.

### Triage of the 30 (Auditor, 3 Sep) — ordered by value and risk

**A · DECLARE AND KEEP — real consumers; the defect is the omission**
`geometries.boundary_source` 247 (`ccode_enrichment:208`, production `un` tier
split) · `kaza` / `liva_1848` / `kaza_1848` / `admin_unit` / `wikidata_qid`
(`interlink_ottgaz:89/:106/:155/:218`).

**B · DUPLICATES → DELETE**
⭐ **`source` — 2,991,143 docs and a PURE DUPLICATE of declared `namespace`.**
The value is literally the namespace string in the same document:
`{"namespace": "tgn", "source": "tgn", "place_id": "tgn:8330053"}`. Written by
~24 authority scripts. **The largest undeclared field in the index carries no
information at all** — biggest win, zero risk, no migration.
Root `h3_cover` / `h3_centroid` 1,310,192 each — unchanged from thread 2.
⚠️ **Root `timespans` 82,508 is NOT a clean delete.** `_collapse_timespans`
(`places.py:250`) reads only nested paths and those are populated — but **202 of
the 82,508 have root timespans and NO nested timespans anywhere** (e.g.
`dgsd:10020`). Deleting the field destroys the only temporal data those places
have. **99.75% duplicate, 0.25% sole-source: migrate the 202 first.**

**C · MISFILED CONTENT** — `description` 2,057 (`nl`, `whg`) holds a **URL**
(`https://native-land.ca/listings/territories/...`), not prose, and does **not**
overlap declared `descriptions` (0 docs carry both). It belongs in declared
`links`. A content fix, not a schema one.

**D · AUTHORITY-LOCAL, NO CONSUMER FOUND** — 12 fields, ~78 k docs:
`area_km2` 30,729 · `historical_county` 24,000 · `register_no`/`register_type`/
`register_year`/`source_project` 16,296 each · `nahiye` 5,606 · `display_color`
4,363 · `divan` 2,994 · `region` 2,599 · `language_family` 2,490 · `time_period`
1,830 · `continent` / `admin_level` 247 · `subregion` 240. Only `area_km2` is
derivable from data already present (geometry), so it alone has a positive
argument for deletion rather than declaration.

🛑 **SCOPE OF EVERY "NO CONSUMER" ABOVE.** Searched: `gateway/`, `processing/`,
`clustering/`, `authorities/`, `tests/`, `testing/`, all `*.js` in this repo, and
the Django registry payload (which carries only *dataset*-level fields — its
`description` is the dataset's, not the place field's). **NOT searched: the
whg3 frontend**, which is not checked out beside this repo. **Grep whg3 before
deleting anything in B or D** — especially `display_color`, named exactly like
something a map style would read.

**Suggested order:** 1 `source` (2.99 M, zero risk) · 2 declare Group A (closes
the strict-mapping hazard) · 3 root `timespans` (migrate 202, then delete) ·
4 `description` → `links` · 5 Group D after the whg3 grep · 6 `depictions` —
populate it or drop it from the schema.

✅ **The durable fix is the check, not the cleanup: put a mapping-versus-schema
diff in `audit_rebuild.py`.** One comparison, a denominator, and it converts a
class currently found *by accident* — twice, sideways, while investigating
something else — into a bounded standing check. Same argument as 4.9's store
cross-check, applied to the schema.

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
  reached. **Expect to debug the publish path, not run it.** ⚠️ **Largely superseded (Auditor F19):** all but the final publish have since run — LOC 1,132 attempted / 1,129 inserted, contributor replay reached DO Postgres unconfigured, `finalise_local` ran, and `1f5aa50`'s renamed drop-ledger fields ran on CRC for the first time. ~~**Only `publish_hardlinks --execute` remains untried.**~~ ✅ **It has since run** — 2 Sep, published and verified live. *(Second-order staleness: this correction was accurate when written and was overtaken by the event it described.)* Asking about
  in-flight state also turned up a stale 16 MB SQLite WAL beside the deleted
  partial database, which a rerun under the same run id would have adopted.
* **S4:** 2.5 and 2.6 are both finishable cold — *"the residual risk is not
  knowledge, it is discipline: both overrides are things you must do, not things
  the tooling will do for you."* Explicitly did **not** clear the 872-document
  gap or `index_namespace`'s stage chain (4.11).
