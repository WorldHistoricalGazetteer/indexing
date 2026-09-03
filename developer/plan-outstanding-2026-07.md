# WHG Re-Indexing — Refreshed Plan of Outstanding Work

> **Compiled:** 10 July 2026
> **Purpose:** A single, current picture of what remains after ~6 weeks of
> isolated fixes, replacing the now-stale/partial plan docs in this folder.
> Supersedes the "outstanding" sections of `plan-ingestionRebuild.execution.md`
> and consolidates the DEPRECATED clustering plan, the handoffs, and a live
> audit of the production indices + tileserver (10 July 2026).

---

## ★ SESSION HANDOFF — pick up here (last worked: 17 Jul 2026 — gateway ops + unchecked-item audit)

**★ Done 17 Jul (gateway ops + audit session) — committed + PUSHED to `main`:**

- **Gateway `/api/places` inherits Wikipedia links from matched wd records** (commit
  `c17439f`). A non-wd place with a `sameAs`/`exactMatch`/`closeMatch` relation to a
  `wd:Q…` now gets that wd doc's Wikipedia `seeAlso` links merged into its own `links[]`
  at detail-fetch time (batched local ES join, no reindex, always in sync). Verified
  live (`osm:m49_central_asia` → 20 Wikipedia links from `wd:Q27275`). Detail-endpoint
  only; not in `/api/search`/`/api/reconcile`.
- **`gateway_ctl.sh` hardened** — (a) `do_start` now self-activates gazetteer's local
  conda (`bb3d056`): the cron-driven `gaz_relay` reached it with a bare env, so
  `python -m gateway` ran system python3.9 and died on import → "FAILED to start". (b)
  Logs now go to a **rotating** `/vast/ishi/elastic/logs/gateway.log` (`28709a8`, keep 5)
  instead of `/dev/null`, which had left startup crashes untraceable. ⚠ The log change is
  pushed + pulled to the /vast clone but **needs a `gw gateway-restart` to load** (the
  relay cron stalled again mid-session — see [[gaz_relay_service_ops]]).

**★ Audit 17 Jul — every remaining `[ ]` checkbox verified against code/prod/whg3.**
Hypothesis "some unchecked items are secretly done" → mostly **NO**; they are correctly
open (genuinely not done, or intentional won't-do/deferred). Verdicts:

| § / line | Item | Verdict (17 Jul) |
|---|---|---|
| §1 ~391 | Discovery scope filter (gateway) | **NOT DONE** — no `dataset_status`/`dataset_id`/scope filter in gateway; schema fields exist, query-side unwired |
| §1 ~463 | `--calibrate` weight fit | **SHIPPED** (17 Jul) — contributor-positives fit (name 0.31 / spatial 0.39, θ_query 0.22; 1,067 positives) written to tracked params, live on next gateway restart; re-run for fine-tuning |
| §1 ~492 | `clustering.js` full scorer | ⚠️ **CORRECTED 3 Sep — the abstraction was NOT un-built; it was built and shipped, and has zero consumers.** `clustering-embed.js` (the Workbench half) was committed **2026-07-12 as `2623e6ca5`** — *five days BEFORE* the 17 Jul audit line saying it "remains" — and is on `main`, i.e. in production. But its only export `attachSelfEmbeddings` **is imported by nothing**: not a webpack entry, not dynamically imported, and a sweep of every `.js`/`.html`/`.py` for the module name, the export name and plausible aliases hits only the file's own first line. `clustering.js` is imported once, by `atlas.js`, for the Atlas path only. **The remaining work is a CONSUMING UI, not the abstraction.** ✅ And it was left inert **deliberately** — `2623e6ca5`'s own message: *"Inert until a Workbench clustering view imports it (that UI is separate Workbench-roadmap work); this is the ready primitive + the abstraction."* So nothing was orphaned; the UI was explicitly scoped out. ⚠️ **The DETAIL sections of this document were right all along** — see the Phase 3 checkbox, "Workbench self-embed primitive DONE", and the note that importing it "is separate Workbench-roadmap UI work". **Only this summary row inverted it**, which is the summary-versus-detail divergence: a reader consulting the table would rebuild a module the same document records as shipped three sections later. |
| — | **whg3 items recorded 3 Sep at S6 close-out** | see the block below the table |
| §2 ~770 | `aat_enrich` backfill (parent) | effectively **DONE except GB1900** — every child done; parent open only for 789 |
| §2 ~789 | GB1900 types | **NOT DONE** — no native type; VLM idea not built |
| §2 ~822 | Hierarchy propagation Pass 4 | **WON'T DO** (struck through) |
| §3 ~918 | Per-dataset coverage res-3 coarsened | **DONE** (was a done-state note, now checked) |
| §3 ~920 | Pending/unpublished submissions | **NOT DONE** — `whg-places.py` flags pending out-of-scope; needs Django endpoint |
| §4 ~954 | Batch 14 test harness | **PARTIAL** — new `integration_harness.py` + e2e test cover the staged happy-path; 5/9 spec bullets absent (ES load, deselection, atomic-swap, scope-leakage, OSM/OHM perf) |
| §4 ~981 | Retire `authority-selection.md` | **NOT DONE** — still referenced in 4 modules |
| §7 ~1074 | PeriodO vs drawn geometry | **NOT DONE** — genuine backend gap |
| §8 ~1099 | Migrate legacy dataset tiles | **PARTIAL** — 7→**47** `whg-*` live; 53 datasets un-migrated + 24 redundant twins. **Twins MUST REMAIN until the legacy web UIs are retired** (see §8) |
| §10 ~1148 | Dynamic-clustering design threads | **DEFERRED** by own text |

> Interlock: 391 (scope filter) → 954 (scope-leakage test) → 920 (pending submissions) are
> one chain — the gateway scope filter was never built, so the dependent test can't exist
> and pending submissions stay out of scope.


### whg3 — carried forward from S6's close-out (3 Sep)

✅ **THE whg3 SIDE OF THE CAMPAIGN IS COMPLETE (3 Sep).** §3.2 done; **place#176
CLOSED**; **place#234 CLOSED** — both riders live on production and verified
there (`main` `ac9542a35`). The Regions status line makes a statement the map
previously could not ("this source has nothing in this period"), and the
"as at year Y" lock needed **no new predicate**: a locked year is the degenerate
window `[Y, Y]`, and because both modes are interval-OVERLAP tests that already
means "alive at Y". ⚠️ Had the containment reading been real, `[Y,Y]` would have
been incoherent in *definitely* and the lock would have needed a special case —
the overlap choice paid for itself.

◐ **ONE #176-ADJACENT ITEM REMAINS AND IT IS OURS, NOT whg3's: the registry
`temporal_extent` RE-PUSH — actioned 3 Sep (SG), assigned to S5.**

🛑 **place#176 §2's WARNING IS ALREADY DISCHARGED AND READS AS LIVE.** It says
*"fix `doc_temporal_range` before pushing, or it writes the collapsed extents
into the registry."* **That was resolved by SEPARATING the two functions, not by
changing the registry's.** `c5c209c`: *"`doc_temporal_range` keeps its pooled
reading and its registry consumer — for 'which period does this gazetteer
describe?' `osm` SHOULD read as contemporary rather than as unbounded."* The
tile/filter path moved to `doc_temporal_bounds`; the registry deliberately did
not. **Anyone reading #176 cold will try to "fix" a reading that is correct.**

**Measured 3 Sep — the aggregates are semantically fine and the real question is
freshness, not correctness:**

```
23 aggregates on /vast/ishi/staged/_aggregates/, most 2026-08-07 11:xx
   (c5c209c landed 15:11 the same day — and its diff against that file is
    +18/-4, ALL DOCSTRING, so a recompute changes no numbers by itself)
osm  [191, 2028]  record_count 20,622,228     <- NOT collapsed to [2026, 2026]
ohm  [-10000, 2050]                           clio [-3400, 2024]
```

⚠️ **What has moved is the CORPUS, not the code.** `gn`/`wd`/`nl` got a real
`final/` on 2 Sep (2.7), so their 7 Aug aggregates were computed from whatever
they resolved to then. **Recompute where the staged tree is newer than the
aggregate, then push.** ⚠️ `record_count` goes to the registry too — a stale one
is a user-visible wrong number on the gazetteer page.

🛑 **The real risk in item 1 is filter composition, not counting** (S6's own
flag): it touches the base-style boundary layers via `heroMap.showBoundaries`,
which keep their own `_originalFilters` registry — **a different mechanism from
the `registerTemporalLayer` registry §3.2 added** for the dynamic gazetteer
layers. The two paths compose filters differently, so a temporal clause and an
active boundary-tier `valueFilter` can overwrite each other. Not a matter of
pointing the existing filter at more layers.

The remaining items below exist on no other list. **None is urgent; none was
touched.**

**The riders, for reference — now place#234:**
* **Regions status line** — distinguish *"no boundaries at this level"* from
  *"this source has nothing in this period"*. ✅ The building block already
  exists: `heroMap.countBoundaryFeatures(source, boundaryValues)` counts via
  `querySourceFeatures` — deliberately **not** `queryRenderedFeatures`, because
  layer filters apply at tile-parse time so a count taken straight after
  `setFilter` still describes the previous tier. Counting once with and once
  without the exported `temporalFilterClause` yields exactly the two numbers the
  status line needs. This is also the honest fix for the z0–7 footprint
  limitation: `coverage:1` features carry no temporal props, and filtering them
  would assert something false in either mode.
* **"As at year Y" lock** (§5.1.3) — `temporalFilterClause(mode, Y, Y)` plus the
  UI affordance. Purely client-side, no dependency.

**Latent defects, unrecorded elsewhere:**
* ⚠️ `atlas.js:754` and `:2106` read `detail.admin_level` / `item.admin_level`
  and **both are always `undefined`** — `heroMap._emitBoundarySelection` emits
  `boundary`, and `areaSearchRouter` never sets the key. So `selectedRegions`
  entries carry `admin_level: undefined` throughout and anything later filtering
  or labelling by it silently gets nothing.
* Bundle URLs carry the cache-busting param **twice**
  (`atlas.bundle.js?v=…&v=…`): `base_webpack.html:111` re-injects CDN-fallback
  scripts and appends `v={{ asset_version }}` to a `src` that already has one
  from `atlas.html:604/608`. Same value both times, so cosmetic — but it doubles
  the cache key and reads as a bug in a stack trace.

🛑 **A cross-repo divergence that will surface as a user-visible disagreement:**
whg3 **reconciliation still reads the LEGACY v3 indices** for some LPF
properties. `PROPERTY_FIELD_MAP` (`reconcile_helpers.py:281`) maps
`whg:lpf_feature` → `[… "descriptions", "depictions"]`, and that map is consumed
against `ELASTIC_INDICES = "whg,pub,wdgn"` — **not** `places`. Everything else is
moving to the new corpus. Expect *"why does this field disagree with the
Atlas?"*. Recorded nowhere else.

⚠️ **And whg3 queries the `places` index DIRECTLY** (`placetypes/es_tree.py:71`,
`placetypes/mapping_utils.py:170,310,779,817`), not only through the gateway —
so any "no consumer" audit that reasons from the gateway's response allow-list
is unsound. Checked at the time: none of those queries touches a field on the
4.17 deletion list (`source_id` ≠ `source`).

## ★ Earlier handoff (last worked: 14 Jul 2026 — ccode / UN BNDA session)

**★ Done 14 Jul (ccode / UN BNDA session) — all committed + PUSHED to `main` (origin `6fc5a75`):**

> This work was **not** in this plan — it was surfaced by the Atlas cluster-card UI as
> data-quality issues, then grew into a country-boundary **source migration**. Recorded
> here so the handoff is current. See `memory/ccode_country_boundaries_bnda.md`.

- **Country boundaries + ccode source migrated Natural Earth → UN Geospatial BNDA.**
  Authoritative UN admin boundaries (committed: `processing/data/un_bnda_countries.geojson`):
  native ISO 3166-1 for every country + dependent territory (no NE `-99` France/Norway/
  Kosovo dropout), Antarctica included, antimeridian-correct, topological.
  `ccode_enrichment.UnCountryIndex.from_bnda_geojson` (self-contained STRtree).
- **`un` authority rebuilt from BNDA** (`authorities/un-countries.py`) — one source of
  truth for the gazetteer places AND ccode resolution. Re-ingested into prod (247 docs,
  replacing 258 NE; France=FR, Antarctica=AQ, territories separate; **ongoing temporal**
  `start.latest=2025`, no end — native BNDA only, per SG).
- **ccode backfill** (`processing/backfill_ccodes.py`, export→resolve→apply) — 9.87M docs
  that lacked ccodes resolved from BNDA → **96–100% coverage** over docs-with-geometry
  (tgn 0→96%, osm 78→99%, wd 80→98.5%); 9,522,590 applied, 0 failures.
- **Bugs fixed + applied to prod:** (a) **tgn h3 ingestion** — h3 written doc-level not
  into `geometries[]`, so all 2.97M tgn docs lacked h3 (fixed + backfilled); (b)
  **antimeridian polyfill** in `helpers._polyfill_adaptive` — dateline-crossing polygon
  interiors were dropped (US/RU/wd geoshapes); (c) NE `-99`; (d) the long-deferred
  **`wd_geoshapes` merged** into the geom store (58,611 Wikidata polygons — "prod had 0 wd
  polygons") + their h3 recomputed point→polygon.
- **Temporal query support** — `gateway/es_helpers.py` temporal filter now honors
  `latest`/`earliest` qualifiers AND treats start-with-no-end as **ongoing** (was silently
  dropping every ongoing feature); schema indexes earliest/latest. Live.
- **Symphonym backfill** for the 25 new `un` toponyms (French/qualified country names).
- **Tilesets:** `un.mbtiles` (247 BNDA features) + `wd.mbtiles` (11.46M features,
  geoshapes now polygons) regenerated + deployed + tileserver restarted — both live.
  ⚠ **Gotcha:** an incremental `index_namespace --source-stage extract` leaves STALE
  staged stages (`final/`, `h3_merged/`, `h3/`) that `generate_tiles` and the
  `gazetteer_*` aggregates silently prefer — the first un regen tiled old NE data. Retire
  the stale stages after any namespace replacement.
- **Registry:** `un` relabeled **"ISO 3166 Countries" → "UN Countries"** with UN
  provenance; aggregates regenerated (record_count 258→247, temporal `[2025, null]`);
  re-pushed prod + dev.
- **`apply_aat_enrich --namespace wd` FINISHED** — wd AAT type coverage **98.2%**
  (11.25M/11.46M typed). SG action-queue item #3 ✅.
- Gateway restarted via `gw restart` after `git reset --hard origin/main` reconciled the
  prod clone. **Do NOT scp into the `/ix1` prod clone** — it blocks `gw`'s git pull; commit
  + push + reset instead.

**📌 FOR THE whg3 AGENT — audit + mark done. ✅ AUDITED 14 Jul 2026 (whg3 agent) — all three
items confirmed shipped to whg3 prod (prod `main` @ `4ff311c3`):**
- **§1 — Atlas clustering UI real-browser visual pass ✅ DONE.** Verified live in a real
  browser (Claude-in-Chrome) across multiple whg3 sessions: cross-gazetteer cluster cards,
  θ merge-sensitivity + per-signal weight sliders, adaptive-θ auto-fit, temporal filter,
  AAT type facets, card↔marker↔zoom sync, `/atlas/place` portal. `clustering.js` client-side
  scoring + Union-Find is live (5 signals + composite mirroring the offline calibration),
  PLUS new work beyond the plan: contiguous-feature-id desync fix, and a **same-gazetteer
  repulsion constraint** (≤1 record/namespace per cluster unless the pair clears θ+margin).
- **§6 — promote whg3 `staging` → `main` ✅ DONE.** Promoted + deployed repeatedly; prod now
  well past the gate at `4ff311c3` (dozens of Atlas commits since).
- **whg3 licensing / Phase-4 promotion + prod migrations ✅ DONE.** Commit `2d4308a1b`
  (attribution_for/citation_text + seed-count-test fixes) is in `origin/main`; on prod
  `showmigrations` confirms **`licensing 0003_extend_licenses [X]`** and **`api
  0007_registry_attribution_fields [X]`** both applied. **§5's gate is cleared — the
  indexing side can now do the citation/licence prod re-push.**

**🆕 Gazetteer-level coverage filtering — INDEXING SIDE ✅ DONE (2026-07-14); whg3
exposure + wiring is the remainder.** The Atlas Gazetteers picker (Master Plan §1.4.1)
ships two "coverage" switches — *"Hide gazetteers outside **Area** filter"* (spatial) and
*"Hide gazetteers outside **Date Range** filter"* (temporal) — both currently
non-functional stubs because the client had no per-gazetteer coverage metadata. They need,
per authority: (1) **`h3_coverage`** — the H3 cell rollup of that authority's places (to
hide gazetteers whose coverage doesn't intersect the active Area polygon); (2)
**`temporal_extent`** — `[earliest, latest]` with the ongoing-null convention (to hide
gazetteers whose span doesn't overlap the active Date Range).

- **✅ Indexing side (done 14 Jul):** `gazetteer_h3_coverage` + `gazetteer_temporal_extent`
  aggregates now exist for **all 22 authorities** (regenerated the missing `po` temporal),
  and `push_gazetteer_inventory` ships **both** fields per authority. **Re-pushed to prod
  AND dev for all 22 (22/22, 0 failures).** So the registry carries current coverage for
  every gazetteer — e.g. `un` = `[2025, null]` (ongoing), undated authorities like `gn` =
  `[null, null]`.
- **▶ Temporal (Date Range) switch — DONE on whg3 dev (`2cbd1a42`, not yet prod).** The
  Atlas page context now exposes `temporal_extent` per namespace (`gazetteer_temporal`)
  and the switch hides gazetteers whose `[earliest,latest]` doesn't overlap the active Date
  Range. Tiny payload, pure client-side. (Promotion held pending in-browser verification.)
- **▶ Spatial (Area) switch — INDEXING DELIVERABLE ✅ DONE (14 Jul); whg3 wiring remains.**
  Added **`h3_coverage_coarse`** to `push_gazetteer_inventory` — the fine `h3_coverage`
  rolled up to **res-2** via `cell_to_parent` (deduped), or the `"global"` sentinel for the
  core global sources (gn/osm/wd/tgn). **Re-pushed to prod AND dev for all 22 authorities
  (22/22, 0 failures).** Every non-global authority row now carries both `h3_coverage`
  (fine) and `h3_coverage_coarse`. **Reality note (vs the "few KB" target):** the broad
  authorities don't condense to a few KB — clio **7.9 MB → 90 KB**, un **1.17 MB → 37 KB**,
  nl ~25 KB, pl ~9 KB; small/regional ones are tiny (gb 0.2 KB). **Total ≈ 200 KB across
  the ~16 regional authorities** — a ~650× reduction from the 6+ MB fine sets, and fine for
  a one-time picker load. (If even smaller is wanted, drop `_COARSE_COVERAGE_RES` to 1 in
  `push_gazetteer_inventory.py` and re-push.)
  **▶ whg3 side (small):** add `h3-js`, expose `h3_coverage_coarse` in the Gazetteers-picker
  payload, and do `h3.polygonToCells(activeArea, 2) ∩ authoritySet` on the Area switch
  (match the **res-2** the field is stored at). `"global"` authorities always pass.

---

**★ Done 14 Jul (earlier session) — all committed + PUSHED to `main` (origin at `1507f5d`):**
- **§2 wd typing → ~98% (P279 Pass 2 added).** On top of P1014, added the **P279
  subclass walk**: new `typesystem/extract_wikidata_p279.py` (htc Slurm scan → 4.24M-edge
  class graph) + `aat_mapper wikidata-p279` (BFS from an unmapped wd type to its nearest
  P1014-mapped ancestor; depth-limited, cycle-guarded, `confidence=broad`). wd type
  coverage in the `/vast` `wikidata.json` went **21.8%→98.2%** (P1014 +8, P279 +7,869).
  **`apply_aat_enrich --namespace wd` is RUNNING on `pitt`** (applying the 98% mappings to
  the ~11.5M live wd docs; coverage climbing 76%→~98%; ~1,200 upd/s, 0 err; reads the
  enriched `/vast` `wikidata.json`).
- **ES heap incident PERMANENTLY FIXED** (see the RESOLVED block below): 15g→28g via
  `ES_HEAP_SIZE` in pitt `.env.local`. **Follow-up finding:** the toponyms HNSW merges
  completed fine under 28g; post-merge a `GC.class_histogram` proved the live set is
  <1 GB — the scary high idle `heap_used%` was benign G1 garbage, **not a leak**. 28g is
  correct and sufficient; big writes (e.g. the wd apply) are safe.
- **`wdgn_20240316` RESTORED** (was deleted 13 Jul as "no code refs", but the
  **Reconciliation API used it via the `wdgn` alias** — a cross-service consumer). Restored
  from snapshot `staging_repo/production_backup_20260318`: green, 13.6M docs, `wdgn` alias
  back. See §4 handoff correction below.
- **whg3 licensing/Phase-4 VERIFIED + finalised on `staging`** (via a whg3 agent) — the
  code was already merged on `staging`; found + fixed **2 real bugs** (`attribution_for`
  ignored `citation_text`; a stale seed-count test that would break the build), commit
  **`2d4308a1b`** (not pushed). **Prod parity now = SG: promote `staging`→`main`, then on
  prod `manage.py migrate licensing` (0003) + `manage.py migrate api` (0007)** → unblocks §5.
- **`gaz_relay` privilege relay** (`scripts/gaz_relay.sh` + `gaz_request.sh`): ES/gateway
  service ops (restart/health) can now run as `gazetteer` from `stg135` **without `su`**
  (gazetteer can't be SSH'd in — sshd AllowUsers). 1-min cron + strict allowlist. Gateway
  was found STOPPED and restarted through it + verified (`/api/health` 200). See
  `memory/gaz_relay_service_ops.md`.

**SG's action queue (manual — surfaced on request):** (1) ~~run `es -update` to reconcile
`/ix1`+`/vast`~~ **✅ DONE** — both clones reconciled to `origin/main` via `git reset --hard`
this session; (2) ~~promote whg3 `staging`→`main` + migrate (unblocks §5 citation re-push)~~
**✅ DONE (whg3 side, 14 Jul) — prod `main` @ `4ff311c3`; `licensing 0003` + `api 0007`
applied on prod; §5 re-push now unblocked (indexing side)**; (3) ~~`apply_aat_enrich
--namespace wd`~~ **✅ DONE** — finished, wd AAT coverage **98.2%**. See
`memory/sg_action_queue_2026_07.md`.

---

**Done 13 Jul (previous session — all committed to `main` + deployed to pitt):**
- **TGN full re-ingest** (§10) — 2.99M places re-indexed, 709K historic toponyms
  embedded, temporal + AAT types verified live.
- **§3 whg publication** — all **48 contributed datasets** live: 228,918 places,
  embeddings backfilled, **47 tilesets serving**, 48 per-dataset registry rows on
  prod+dev, 173K docs AAT-typed. Bug fixes: `index_namespace` 512-byte `_id` guard;
  `push_gazetteer_inventory` per-dataset fan-out + 413 fix (per-dataset res-3 h3 +
  batching); tile-deploy sshd thundering-herd worked around.
- **§7 pagination** — `offset` param on `/api/search` (offset-based, not
  search_after — the gateway re-ranks in Python).

**Gateway restarted 13 Jul** (pulled 40baf95) → `undated`, `aat_types`
facet/filter, clustering-fuel params now ACTIVE + verified (faceted search returns
54 aat_type facets).

**✅ RESOLVED — ES heap incident (13 Jul) — ROOT-CAUSED + PERMANENTLY FIXED.** The
"just restart, no `-Xmx` bump needed / transient" call below was **WRONG** — it
recurred (search down/intermittent) and a second restart refilled to 15 GB in <4 h.
**Real root cause (diagnosed 13 Jul):** large concurrent Lucene **HNSW `dense_vector`
segment merges** of the ~67.5 M-doc `toponyms` index (4 merges / ~36 GB), triggered
by the day's heavy re-indexing (TGN 709 K new toponyms + embeddings, whg 88 K, …).
Rebuilding the HNSW graph during merge exhausts heap → G1 Full-GC death spiral (heap
pinned 15.3/15.4 GB, Full GC frees 0 B) → thread starvation → 429s. (The `/vast`
"28 s slow disk" FsHealth warning was a *symptom* of GC starvation, not a disk fault.)
Ruled out: fielddata (8 MB), nested `fixed_bit_set` (179 MB flat), scroll/PIT leaks
(0). **Fix:** the box was upgraded 32→62 GB RAM but heap stayed hardcoded at 15 GB.
Made heap configurable — `scripts/es.sh` exports `-Xms/-Xmx ${ES_HEAP_SIZE:-15g}`,
`_common.sh` now sources `.env.local`, and pitt `.env.local` sets **`ES_HEAP_SIZE=28g`**
(commit `ae81232`). **Verified live: heap MAX 28 GB, search+nested-agg 200 OK.** See
`memory/es_heap_hnsw_merge_root_cause.md`. Restart-after-heavy-indexing is no longer
the mitigation — 28 GB heap absorbs the vector-merge peak. Also deleted 3 stale
pre-rebuild indices (93 GB disk) — see below.

**✅ Pagination fixed + VERIFIED (13 Jul).** The 40baf95 version overlapped across
pages (pool grew with offset → re-ranked). Two fixes: (1) pool = **top-K place_ids
by discovery score** (deterministic superset; also fixes a latent ranking bug where
the old arbitrary doc-order pool dropped high-scoring candidates beyond the fetch
window); (2) **dropped the `len(names)` sort tiebreaker** — name counts come from
the bounded enrichment step so they varied with pool size (which grows with offset),
reordering equal-score places. Final sort is now `(score, place_id)` — the exact
order used to pick the pool → provably consistent. **Verified live:** page1==full[0:3],
page2==full[3:6], page1∩page2=∅. Prominence-by-name-variant is gone (weak proxy).

**✅ RESOLVED — §2 wd P1014 derivation (13 Jul, offline / WDQS-free).** WDQS was *not*
recovering (single-value probes 200'd but 200-item `VALUES` batches all 429'd at "1
req/min, active wdqs outage"), so instead of fighting the rate-limit we extracted P1014
from the **full Wikidata dump we already hold**. P1014 (Getty AAT ID) sits on
type-concept entities (`Q515`→`300008389`) that the place ingest filters out, so it was
never in our ES `wd` docs — but it *is* in the 148 GB dump. New code (commit `84fdf2b`):
- `typesystem/extract_wikidata_p1014.py` — shared extractor + standalone dump scanner.
- `authorities/wikidata-places.py` — emits the crosswalk as a **side-output of the
  existing place-ingest scan** (before the geographic filter; `"P1014"` added to the
  coarse pre-filter), so every future full Wikidata re-ingest regenerates it for free.
- `aat_mapper wikidata` reads the crosswalk (`load_p1014_crosswalk`); the **deprecated
  WDQS method (`fetch_wikidata_aat_mappings` + `sparql_label_by_id`) is removed**.

**Run (htc Slurm, `sbatch -M htc`):** scanned the dump in 39 min → **26,420-row**
`/ix1/ishi/data/wikidata/wikidata_p1014.jsonl`. **Finding — P1014 is saturated:** only
**1,895 / 10,308** wd type Q-items carry a Getty AAT ID at all, so the pass adds just
**8** net-new mappings (`wikidata.json` 2,243→2,251). The residual tail genuinely has no
P1014 — it needs the P279/label passes (§2 body) or is untypeable; a healthy WDQS would
have returned the same 8.

**SUPERSEDED 14 Jul — P279 changed the calculus.** P1014 alone (8 mappings) wasn't worth
an 11.5M-doc reindex, but the **P279 Pass-2 walk added 7,869 more → ~98% type coverage**
(see the "★ Done 14 Jul" block above), and **`apply_aat_enrich --namespace wd` is now
RUNNING on pitt** to apply it to prod. The crosswalk + P279 graph are free ingest
side-outputs, so both re-derive on any future wd rebuild.

**READY TO RUN (whg3 gate cleared 14 Jul):**
- **§5 citation/licence prod re-push** — ✅ **whg3 gate cleared:** Phase-4 promoted to
  `main` (commit `2d4308a1b` in `origin/main`) and prod migrations applied (`licensing
  0003` + `api 0007`, both `[X]` on prod `web_whgazetteer-org_main` @ `4ff311c3`). **The
  indexing side can now run the citation/licence inventory re-push** (unknown SPDX codes
  are non-fatal, so it's safe even before full vocabulary parity). See §5.

**NEEDS AN SG DECISION (do NOT do autonomously):**
- [x] **Delete stale pre-rebuild ES indices — DONE 2026-07-13 (SG-authorised).**
  Deleted `places_20260317` (45 GB/413 M), `toponyms_20260317` (42 GB),
  `wdgn_20240316` (5.6 GB; `wdgn` alias) → **~93 GB disk freed**, shard count down.
  ⚠ **Did NOT relieve heap** — see the ES incident (idle indices held no heap; the
  real cause was HNSW vector merges → fixed via 28g heap). `clusters_2026032x`
  already gone; two 8.9 kB `cluster_state_*` indices remain (negligible).
  ⚠️ **CORRECTION — `wdgn_20240316` was NOT unused: the Reconciliation API queried
  it via the `wdgn` alias** (a consumer OUTSIDE this repo, missed by the "no code
  refs" grep). Recon broke on the deletion → the API was patched to ignore missing
  indices, AND **the index was RESTORED 2026-07-14** from snapshot
  `staging_repo/production_backup_20260318` (state SUCCESS; restored *only*
  `wdgn_20240316`, NOT the 4 co-snapshotted indices incl. the deliberately-deleted
  `places_20260317`/`toponyms_20260317`): green, 13,616,287 docs, 5.69 GB, `wdgn`
  alias auto-recreated from the snapshot. **Lesson: a "no code refs" check inside
  one repo can miss cross-service consumers (recon API, Django) — grep the whole
  fleet before deleting an aliased index.**
- **§4 retire `authority-selection.md`** — needs a new Django "list enabled
  authorities" endpoint (chicken-and-egg: registry is populated post-ingest).
- **§4 Batch 14** — large end-to-end test-harness scaffolding.

**DO NOT TOUCH (in active use):**
- **Legacy `datasets-*`/`collections-*` tilesets** (~79) — the **legacy prod UI
  still serves from them** until it is swapped for Atlas (SG, 13 Jul). Keep.
- **`whg_2025_11_12` ES index** (1.2 GB) — the live `/search/` union index.

**How to direct the next agent (14 Jul):** the **indexing/gateway side is essentially
DONE** — the platform is live and every gateway contract the browser needs is on prod.
The remaining substantive work is **whg3-side** (see §1's "✅✅ WHAT REMAINS ON whg3" +
"📌 FOR THE whg3 AGENT" callouts, and §6). **The #1 actionable whg3 item is the
real-browser visual pass of the Atlas clustering UI** — code-complete (Phases 1–3) but
unconfirmed because the earlier headless agent couldn't load MapLibre WebGL; a whg3 agent
running in a real PyCharm/browser instance *can* do it. Note the wd type facets are
strengthening right now (`apply_aat_enrich --namespace wd` landing → ~98%). Indexing-side
leftovers are all SG-decisions or blocked (Batch 13b governed migration = whg3/Django;
Batch 14 harness; retention scheduling; legacy-tile retirement — all in §§2,4,8).

---

## 0. Where we actually are (the platform IS live)

The decoupled staged-Parquet → Elasticsearch rebuild **completed and cut over to
production on 2026-05-03/04**. Live aliases (audited 10 Jul 2026):

| Alias | Concrete index | Docs (incl. nested) |
|-------|----------------|---------------------|
| `places` | `places_postbarrier-20260502t130000z` | 341.5M (≈47M places) |
| `toponyms` | `toponyms_postbarrier-20260502t130000z` | 67.5M |
| `clusters` | `clusters_20260325` | 41.0M — **still the legacy static index** |
| `types` | `types_20260404_150351` | 59K (AAT hierarchy) |

**22 authority namespaces are live in `places`** (all authority scripts have
made it into the index):

```
osm 20.3M · gn 13.4M · wd 11.5M · tgn 3.0M · gb 1.17M · ohm 905K ·
chgis 81K · tm 64K · pl 25.6K · iv 24K · alc 18.2K · ofs 16.3K ·
clio 15.7K · whg 14.2K · hgis 14.1K · po 9.0K · og 6.3K · nl 4.4K ·
dgsd 3.8K · dp 2.6K · un 258 · ukhc 92
```

Recent isolated work already **DONE and verified live**: Wikipedia sitelinks →
`places.links` (10K+ wd docs carry `seeAlso` links in prod ✓); reconcile
`links` in candidates; `contained_in` resilience fixes; `is_place_type` fix;
prod gateway repo (`/ix1/ishi/elastic`) is at HEAD `8c74228` (fully current).

So nothing below is blocking — the platform serves. What remains is (1) a large
**clustering re-architecture**, (2) the **AAT/type system**, (3) **activating
already-built features**, and (4) polish/ops/docs.

---

## 1. Clustering re-architecture — client-side scoring + clustering  ★ biggest workstream

> **Design settled 2026-07-11** with SG, reconciling the whg3 Master Plan +
> the indexing architectural plan (`plan-dynamicClustering.DEPRECATED.md`) and
> going further than either: **all** pair scoring and clustering runs
> **client-side**; the gateway neither scores nor clusters nor groups. (Earlier
> drafts here proposed an ES-native `co_references` denormalisation — WRONG — then
> a query-time gateway scorer with a server-side "Option A" fallback — since
> dropped. This is the current design.)

### Confirmed architecture

- **Clustering is a browser concept.** The θ-adjustable dynamic clustering (slider,
  facet-weight sliders, live re-cluster, synthetic-edge passes, cluster cards)
  runs entirely in `clustering.js` in whg3 — for both the Atlas UI and the
  local-first "Map your Data" / Collaborative Workbench.
- **Scoring is client-side too.** The browser computes every pair signal — `s.n`
  (int8 Symphonym cosine), `s.sp` (haversine), `s.t` (interval overlap), `s.ty`
  (Wu-Palmer over shipped AAT ancestors), `s.l` (from shipped hard-link edges) —
  the composite, and the Union-Find. The gateway does **no** scoring. This is
  forced by the Workbench anyway (private records never reach the server), so
  Atlas and Workbench share **one** scorer + **one** clustering implementation.
- **No server-side grouping; Option A dropped.** OpenRefine is being superseded by
  the Workbench, so there is no non-JS interactive consumer. The **reconciliation
  API returns flat ranked candidates** (per source; the consumer chooses) — no
  clustering, no dedup. `cluster_threshold` and `group_by_cluster` are removed.
- **The gateway is a retrieve-and-ship service.** Per query it ships:
  - flat `hits[]` with `h3`, `h3_cover`, `temporal_range` (derived from
    `timespans`), `aat_ids` + `aat_ancestors`, `query_match{name,score}`;
  - the result-set **hard-link edges** (overlay expansion — see below);
  - `clustering_params` + `toponym_stoplist` (from offline calibration);
  - **optionally** per-toponym Symphonym embeddings (`include_embeddings` flag).
- **Embeddings are an optional payload — the client picks by capability:**
  - **Atlas** → `include_embeddings=true`: the gateway ships the *already-precomputed*
    toponym embeddings (they exist in the `toponyms` index from discovery). No
    client model, no client inference — right for casual searchers (~440 KB / 200
    hits).
  - **Workbench** → `include_embeddings=false`: the browser self-embeds via local
    workers (the model is already loaded to embed the user's private records the
    server never sees, so embedding the public candidates too is ~free and avoids
    the payload). `clustering.js` is agnostic — it consumes int8 vectors from
    either source.
    🛑 **THIS IS DESIGN, NOT CURRENT STATE — corrected 3 Sep.** Map-your-Data
    today does **neither** half. It embeds its own units locally
    (`reconciliation.js:8057`) and sends the vector **UP** as `query_vector` for
    phonetic KNN; `/api/reconcile` **ships no embeddings back at all**, so there
    is nothing for the client to self-embed *against*. The self-embed-candidates
    path described here **does not exist** — it is place#219's territory and is
    deferred (it needs gateway fuel). Read this bullet as the intended
    architecture, not as a description of what runs.
  - *Consistency:* the worker must run the same Symphonym build + int8 quantisation
    as ingestion (the `hf/` export) so Atlas and Workbench score alike.
    Reconciliation determinism is no longer required (recon is flat), so minor
    cross-browser float drift is tolerable.
- **Hard links come from the overlay, NOT `places.links`.** The co-reference graph
  (`sameAs`/`exactMatch`/`closeMatch`/`distinct`) lives in the Pitt SQLite overlay
  (active) + DO Postgres (pending, scope-isolated) — LOC transitive + authority +
  contributor. `places.links[]` is external *reference* links (mostly `seeAlso`)
  and is **not** the graph. The gateway does hard-link expansion over the overlay
  and ships those edges — always server-supplied, scope-sensitive, not derivable
  in the browser.
- **`baseline_cluster_id` is optional / deferred.** The browser can compute a
  *local* baseline from the shipped hard-link edges (connected components). A
  *global* precomputed field only adds the cross-query "shared baseline" structural
  synthetic-edge signal (Master Plan §3.9.2) + instant bootstrap — nice-to-haves,
  deferrable.

### Outstanding work

**Gateway (indexing `gateway/`) — retrieve + ship:**
- [x] **Per-hit payload assembly** — **IMPLEMENTED 2026-07-11**
      (`gateway/clustering_payload.py::assemble_clustering_fields` +
      `tests/test_clustering_payload.py`; wired into `/api/search` + `/api/reconcile`).
      Per hit ships `h3` (representative `h3_centroid`), `h3_cover` (bounded union),
      `temporal_range` (gateway-derived `[min_start,max_end]` from geometry
      `timespans`), `aat_ids` (leaf ids), `aat_paths` (materialised root→leaf paths —
      carries the **ancestors + depth**, so it subsumes a flat `aat_ancestors` and
      supports client-side Wu-Palmer), and `query_match{name,score}` (the matching
      toponym, captured in discovery via `collect_place_ids`). Opt-in per query via a
      new **`include_clustering_fields`** flag (default `False` → responses
      byte-identical when unset; additive; orthogonal to `include_hard_links`).
      **Remaining (deferred):** optional per-toponym `phon_emb` gated on
      `include_embeddings` — part of the "Additive param plumbing" item below, not
      built yet.
- [x] **Hard-link expansion + ship** — **IMPLEMENTED 2026-07-11**
      (`gateway/hard_link_expansion.py` + `tests/test_hard_link_expansion.py`;
      wired into `/api/search` + `/api/reconcile`). Queries the **union of the batch
      overlay + the live-delta** (`hard_links_live.sqlite`), deduped by the overlay
      UNIQUE key `(place_a,place_b,relation_type,source_id)`, for result-set
      assertions **+ bounded 1-hop**, and emits them as `edges[]`
      `{a,b,relation_type,source,via_hard_link}`. Opt-in per query via a new
      `include_hard_links` flag (default `False` → zero behaviour change; additive,
      safe ahead of the browser); both stores opened **read-only, best-effort**
      (missing/mid-swap file skipped, never fatal). Reading the live-delta here is
      what gives `POST /api/links` its real-time reconcile effect — this **was**
      "Ticket B" from `developer/handoff-hardlink-live-delta-followups.md`. Live-delta
      is self-maintaining (Ticket A, 2026-07-11). (Pending contributor assertions are
      merged separately at Django from DO Postgres, scope-filtered — Master Plan
      Part VII.) **Deployed to Pitt + restarted + smoke-tested 2026-07-11** (`edges[]`
      verified live: 6.43M-row overlay, correct shape/provenance, default byte-identical).
      The browser consumer (whg3 `clustering.js`) that reads `edges[]` is whg3-side (§6).
- [ ] **Discovery scope filter** — accept pending `dataset_id` scope tokens; filter
      `dataset_status:published OR dataset_id ∈ scope`; Django merges DO pending
      assertions (Master Plan Part VII).
- [x] **`POST /api/links` + `DELETE /api/links`** internal endpoints (arch plan
      §13c/d) — **implemented 2026-07-11** (`gateway/links.py` + tests, branch
      `feat/api-links-receiver`; writes a separate live-delta SQLite, contract
      reused from `sqlite_overlay`/`staging_contract`). **Deployed to Pitt +
      restarted 2026-07-11.** Follow-ups now resolved:
    - [x] **Batch harvest of fresh `ContributorAttestation` rows + live-delta prune**
      — shipped 2026-07-11 (Ticket A, commits `c17314b`…`94ae401`):
      `contributor_replay.py` folds active attestations into the batch overlay
      (source_id mirrors the whg3 model; no `ds_status` filter so batch ⊇ live-delta);
      `submit_hardlinks_slurm.py` prunes the live-delta after each ship (cutoff =
      pre-harvest timestamp, keeps in-flight rows); gateway creates the live-delta
      group-writable so the batch user can prune it. Live-verified against `whgv3beta`
      (table still empty until contributor links flow). The live-delta is now
      **self-maintaining / bounded**. `handoff-hardlink-live-delta-followups.md` is CLOSED.
    - [x] **Live reconcile-time union(batch, live-delta) lookup** — NOT a separate
      task; it **was** the "Hard-link expansion + ship" item above, now **IMPLEMENTED
      2026-07-11** (`gateway/hard_link_expansion.py`). See
      `developer/handoff-api-links-receiver.md`.
- [x] **Params — cluster retirement DONE 2026-07-12.** **`include_embeddings`**
      (per-name int8 `phon_emb`) done 2026-07-11. **Removed** `group_by_cluster` +
      `cluster_threshold`, retired `build_cluster_lookup` / the `clusters`-index join
      from **both** `search.py` and `reconcile.py` (+ the standalone `/api/cluster/*`
      endpoints + `ClusterGroup` + `CLUSTERS_INDEX`). Safe: browser-verified the live
      `/search/` page uses the separate legacy `whg` union index (not this gateway);
      grepped whg3 `staging` — no consumer reads `cluster_id`/`cluster_size` and
      `crc_client.py` sends neither flag; SG confirmed no external OpenRefine workflow
      sends `group_by_cluster`. Reconcile now returns flat ranked candidates.
      **`facet_weights` / `phase_2` / `result_limit` are NOT needed (dropped 2026-07-12):**
      whg3's `crc_search`/`crc_reconcile_search` send none of them (verified), and they
      have no server role — facet weights are **client-side** sliders, `phase_2` is the
      **retired** synthetic-edge passes (§16a), and `result_limit` duplicates the
      existing `size`. Not building dead params.
- [x] **Prominence ranking — DONE 2026-07-12.** The `cluster_size` tiebreaker is
      replaced by **name-variant count** (`len(hit.names)`) — a cheap, already-fetched
      prominence proxy (well-attested places carry more name forms; matches the search
      UI's documented "more name variants rank higher"). No extra ES round-trip.

**Offline (indexing pipeline):**
- [x] **Calibration** — **IMPLEMENTED 2026-07-11** (`clustering/calibrate_params.py`
      + `clustering/signal_features.py` + `tests/test_calibrate_params.py`). Produces
      `clustering_params.json` (weights name/spatial/temporal/type/link +
      θ_query/θ_bridge/θ_synth/θ_synth_structural/τ_name/τ_link) and
      `toponym_stoplist.json`. Three modes: `--defaults` (documented uncalibrated
      params, **no ES** — committed to `clustering/data/clustering_params.json` so the
      browser has params immediately); `--stoplist` (ES aggregation of high-frequency
      names); `--calibrate` (empirical logistic fit — positives = authority hard-links
      from the overlay, negatives = random cross-namespace pairs; salvaged signal math
      = haversine/interval-Jaccard/Wu-Palmer/int8-cosine). **Methodology note:** only
      the **four inferred** signals are fitted; the link weight stays fixed (hard links
      are *forced* merges at τ_link, and fitting link-presence on link-derived positives
      is circular). **Gateway now *ships* both files** per query when
      `include_clustering_fields=true` (top-level `clustering_params` + `toponym_stoplist`;
      cached loaders in `gateway/clustering_payload.py`).
    - [x] **`--stoplist` run on prod (2026-07-11)** → committed
      `clustering/data/toponym_stoplist.json` (500 names attested by ≥50 places; ranked
      by summed `attestations` size, fixing the dedup-index `doc_count` trap). Spot-check
      looks right: village-words across scripts (вёска/село/деревня), generic creek/river
      names, "San Francisco", "长城" segments.
    - [x] **Hard-negative sampling built** (`signal_features.py`): balanced mix of
      nearby (~3km, spatially-close negatives) + same-name (shared toponym) + random,
      de-duped against **transitive coreference components** of the overlay (Union-Find
      over sameAs/exactMatch/closeMatch; `distinct` not unioned). The transitive check
      caught **784** coreferent pairs vs 726 for a direct-pair check — 58 false
      negatives a direct check misses (`a≡c, c≡b ⇒ a≡b`). Pure logic unit-tested
      (`test_signal_features.py`). `θ_query` computed on the composite scale.
      **Result:** even with hard negatives + transitive de-dup the weights stay
      spatial-heavy (name ~0.22 / spatial ~0.46) — confirming the dominance is a
      property of the *positives* (coordinate-near-duplicate authority links), not
      negative noise. Defaults retained (below).
    - [x] **`--calibrate` weight fit — contributor-positives fit SHIPPED 17 Jul (SG).**
      Added a positive-source selector
      (`--positives {authority,contributor,both}`, commit `896a63b`) and ran the fit on
      the **user-reconciliation** positives (SG: treat them as reliable).
      **Result (17 Jul):** of 20,000 sampled contributor `closeMatch` links, **1,067**
      survived the in-index feature build — only **~16%** of legacy `whg:` endpoints
      resolve (most reference whg datasets never ingested; the Batch 13b dangling risk,
      now measured). Fitted weights (link fixed 0.15): **name 0.31 / spatial 0.39 /
      temporal 0.11 / type 0.04**, θ_query 0.22. **Interpretation:** vs the authority
      fit (name 0.22 / spatial 0.46) the user-reconciliation positives DO pull weight
      toward name and off spatial — but spatial still narrowly edges name (0.39 > 0.31),
      and it is *less* name-forward than the shipped **defaults** (name 0.35 / spatial
      0.20). n_positive=1,067 is modest + a biased subset (only included datasets).
      Scratch output: `/vast/ishi/elastic/tmp_calib_contrib/`. **SHIPPED — written to the
      tracked `clustering/data/clustering_params.json`** (SG go, 17 Jul); goes live to the
      browser on the **next gateway restart** (the gateway ships these params per query via
      `gateway/clustering_payload.py`). NB the operating point moved with the weights:
      θ_query 0.55→**0.22** (now the Youden-J cut on the true composite Σw·s scale).
      **▶ RE-RUN for fine-tuning** once (a) more whg datasets are ingested so more
      contributor endpoints resolve, (b) fresh contributor attestations accumulate, and
      (c) a best-of-N representative-embedding pick is added (the "first attested
      toponym" pick understates cross-script name cosine).
      *(Historical — authority-positives fit:* even with hard negatives it stayed
      spatial-heavy (name ~0.22 / spatial ~0.46), a property of the ground truth —
      authority `sameAs` positives are the *same place across gazetteers* so their
      coordinates are near-duplicates. That is why the name-forward defaults were
      retained; the contributor run above is the "better positives" follow-up.)
- [x] **AAT ancestors** — **DONE** (folded into the per-hit payload above): `aat_paths`
      (the materialised `types.aat_paths`, ancestors + depth) is emitted per hit — no
      schema change (the field already exists per-type). `temporal_range` likewise
      gateway-derived. Client-side Wu-Palmer (`s.ty`) reads `aat_paths` directly.
- [x] ~~**`baseline_cluster_id` precompute**~~ — **REJECTED (SG, 2026-07-13); will NOT
      be built.** Its two benefits have evaporated: (1) *instant bootstrap* is
      negligible (the browser computes the local baseline from the shipped hard-link
      edges in <10 ms); (2) the *cross-query structural signal* fed the synthetic-edge
      passes, which are **RETIRED** (§16a). So it would cost an offline
      connected-components job + a patch onto millions of docs (stale on every
      re-cluster) for ~zero gain. The browser derives any baseline it needs locally
      from `edges[]`.

**Browser (whg3 — `staging` dev → `main` prod; see §6):**
- [~] `clustering.js` — the full scorer (all facets) + Union-Find + θ/weight
      sliders + cluster cards (Master Plan §3–4), with an embedding-source
      abstraction (payload-decode for Atlas, worker-inference for the Workbench).
      **PARTIAL — Atlas ✅ / Workbench pending.** The Atlas path (payload-decode
      scorer + Union-Find + θ/weight sliders + cluster cards) is **live in whg3
      prod** (confirmed by the 14 Jul whg3-agent audit at the top of this doc; core
      landed whg3 `staging` `de94f176f`). What remains is the **Workbench**
      worker-inference embedding-source branch of the abstraction. (NB: the Master Plan's *synthetic-edge passes* are **RETIRED** —
      §16a of the architectural plan: "no longer needed" as discovery + hard-links +
      toponym-expansion + user-proposals cover the same recovery cases. The client
      does **not** implement them; `θ_bridge`/`θ_synth`/`θ_synth_structural` in
      `clustering_params` are therefore vestigial client-side.)
    - [x] **Phase 1 — scorer + Union-Find CORE** (whg3 `staging` `de94f176f`,
      2026-07-12). Pure UI-agnostic module `whg/webpack/js/clustering.js`. The five
      pair signals mirror this repo's `clustering/calibrate_params.py` EXACTLY
      (haversine; spatial half-life 25 km; interval-Jaccard temporal; Wu-Palmer type
      over `aat_paths`; int8 cosine name). Weighted composite from
      `clustering_params.json` defaults (name .35/spatial .20/temporal .15/type
      .15/link .15) with graceful degradation (an absent signal is dropped and the
      remaining weights renormalised). Union-Find = forced hard-link merges
      (sameAs/exactMatch/closeMatch) → `distinct` as cannot-link (**SG confirmed
      2026-07-12: Option A, hard-split**) → θ_query (.55) threshold merges,
      highest-composite-first. Embedding-source-agnostic (decode int8 `phon_emb`,
      else worker-embed). **Testing:** 17 standalone Node-ESM assertions — each
      signal vs the Python's values (spatial 25 km→0.5, temporal
      [1000,1100]∩[1050,1200]→0.25, Wu-Palmer siblings→0.667, int8 cosine), forced
      merge via a `sameAs` edge, threshold merge of near-identical hits, `distinct`
      blocking a merge, name-only graceful degradation — **all pass**. The module is
      **inert (not imported/bundled)**, so there is **no live-gateway integration
      test yet** — that lands with Phase 2.
    - [~] **Phase 2 — integration (FIRST PASS DONE; refinements open).** whg3
      `staging`.
        - [x] **2a — gateway-routed search proxy** (`a94d5b9cc`/`fa7bfe9bc`): new
          BETA-gated Django view `atlas_search` at **`/atlas/search/`** +
          `api/crc_client.crc_search()` → CRC gateway `POST /api/search` (via
          DO/Django, per the Pitt firewall) with `include_hard_links` /
          `include_clustering_fields` / `include_embeddings` + `geom=full`; returns
          the full SearchResponse (hits, edges, clustering_params, toponym_stoplist).
          **Verified live from dev:** "Jerusalem" → total **441**, 20 hits, 2 edges,
          params present, per-hit `aat_paths`/`h3`/`temporal_range`/`repr_point`.
          (Also sidesteps the empty dev-ES `/search/index/` path, which returned 0.)
        - [x] **2b — client clustering UI (first pass):** `atlas.js` routes beta
          users' search to `/atlas/search/`, feeds `clusterHits()`, renders cluster
          cards (representative title, member-count badge, namespace chips, member
          list) in the results panel, plots hits on the hero map, and a **merge-
          sensitivity (θ) slider** re-clusters the cached response live. Endpoint
          verified 200 from the browser; build compiles; **cluster-card render not
          yet visually confirmed** — the Atlas search UI is map-load-gated and the
          test browser won't complete MapLibre's WebGL load (env limitation; loads
          fine in real browsers). Needs a real-browser visual pass.
        - [x] **`s.n` name signal wired** (`8107ad3e2`): the gateway attaches the
          int8 Symphonym embedding **per-name** (`hit.names[].phon_emb`, 128-d), not
          on the hit; `clustering.js`'s embedding accessor reads it, preferring the
          **query-matched** toponym's embedding (verified: a hit titled "Fargo" that
          matched on its alt-name "Pittsburgh" contributes the "Pittsburgh" vector).
        - [x] **Map-marker ↔ panel click sync** (`8107ad3e2`): cluster cards/members
          carry `data-pids`/`data-pid`; clicking a card/member highlights + fits its
          markers (`setFeatureState({highlight})`), clicking a marker highlights +
          scrolls to its card. (Live UI still needs a real-browser pass — map-gated.)
        - [x] **`toponym_stoplist` down-weighting** (`879c6b5a6`, #1): scorePair
            scales the name signal VALUE by 0.2 when either matched toponym is on the
            gateway's stoplist (scaling the value not the weight — renormalisation
            makes weight-scaling a no-op when name is the only present signal).
            5 assertions pass.
        - [x] **Facet-weight sliders** (`879c6b5a6`, #2): collapsible per-signal
            sliders (name/spatial/temporal/type/link) seeded from `clustering_params`,
            re-cluster the cached response live.
        - [x] ~~**Synthetic-edge / bridge passes**~~ **RETIRED (§16a)** — SG-confirmed
            2026-07-12; superseded by discovery + hard-links + toponym-expansion +
            user-proposals. Scorer stays forced-merge + single-pass threshold-merge;
            `θ_bridge`/`θ_synth`/`θ_synth_structural` are vestigial client-side.
        - [x] **Cluster-member deep-links → NEW dynamic Atlas portal** (#4,
            `55fec2aad`+`361c0a2f1`): the legacy fixed-`cluster_id` portal is
            incompatible with dynamic client clusters, so — SG-agreed 2026-07-12 —
            a **new dynamic portal** resolves a place on demand: BETA-gated
            `/atlas/place/?id=<pid>` (`crc_places` → gateway `/api/places`) enriched
            with per-namespace registry attribution; an in-Atlas modal shows detail +
            **live cluster context** (the other members of its current client cluster,
            reflecting the current θ/weights — no stored id) + map highlight. Backend
            verified live; modal UI needs a real-browser pass (map-gated).
        - [x] **Phase 3 — Workbench self-embed** (#5, `2623e6ca5`):
            `clustering-embed.js::attachSelfEmbeddings()` embeds records' toponyms via
            the `recon-symphonym` worker (`embedNames` → int8, matches the gateway
            quant) and attaches `phon_emb`, so the same `clusterHits()` runs on private
            Workbench records. Inert until a Workbench clustering view imports it
            (separate Workbench-roadmap UI work).
      (Workbench self-embed primitive DONE — see #5 above; wiring it into an actual
      Workbench clustering view is separate Workbench-roadmap UI work.)

> **✅✅ WHAT REMAINS ON whg3 (definitive, 2026-07-13) — the indexing/gateway side is
> DONE.** Every gateway contract the browser needs is live on prod. The outstanding
> work is entirely whg3-side (`staging` → `main`, §6). In priority order:
>
> 1. **Real-browser visual pass of the Atlas clustering UI.** Phases 1–3 are code-
>    complete but the automated harness can't confirm the *rendered* UI (MapLibre WebGL
>    won't load in headless). A human/real-browser pass is needed for: the cluster
>    cards (representative title, member badge, namespace chips), map-marker ↔ card
>    highlight sync, the **θ (merge-sensitivity) slider** live re-cluster, the per-facet
>    **weight sliders**, and the dynamic **`/atlas/place/?id=` modal** (live cluster
>    context). Backends all verified live; only the visuals are unconfirmed.
> 2. ~~**Wire the §7 AAT type-facet UI**~~ **DONE 2026-07-13** (whg3 `staging`
>    `a5a9438ad`). `crc_search` forwards `aat_types` (list of ints) → gateway
>    hierarchical filter; the Atlas renders `facets.aat_types`
>    (`[{aat_id,label,count}]`, friendly labels) as clickable chips in the results
>    panel, toggling re-searches (query + facet selection preserved), with a clear
>    affordance; the raw `types` param is retained. **Verified live:** "Pittsburgh" →
>    35 facets (*inhabited places* 54, *villages* 7, …); filtering by aat 300008347
>    cut 146→71 results (hierarchical). Chip UI deployed; visual pass map-gated (item
>    1). *(Minor gateway-side: a few facets return the raw aat_id as label —
>    label-resolution gap in the `types` index, not whg3.)*
> 3. ~~**Get the Phase-4 / licensing code (citations) to prod `main`**~~ **whg3 side
>    DONE on `staging` (14 Jul).** A whg3 agent verified Phase-4 was already merged on
>    `staging` and fixed 2 bugs (`attribution_for` didn't prefer `citation_text`; a stale
>    `licensing` seed-count test), commit **`2d4308a1b`** (not pushed). **No further whg3
>    code work** — the gate is now purely **SG: promote `staging`→`main` + `manage.py
>    migrate licensing` (0003) + `migrate api` (0007)** on prod, which unblocks §5.
> 4. **(Forward, not yet actionable)** the **discovery scope filter** Django half — pass
>    the user's owned pending `dataset_id` scope tokens to `/api/search` and merge DO
>    pending assertions (Master Plan Part VII). Only matters once pending datasets flow
>    (needs a Django endpoint; the gateway half is a small add when wanted).
> 5. **Consume the richer fuel** — a whg3 agent confirmed (14 Jul) the currently-live fuel
>    is **already consumed by existing `staging` code** (TGN `temporal_range` → `s.t`; 48
>    `whg:` datasets auto-render, no cap; AAT type facets render) — **no code change
>    needed**. Just track the data-side upside: **wd AAT typing is landing on prod now**
>    (`apply_aat_enrich --namespace wd`, ~75%→~98%), which strengthens the wd type facets +
>    the `s.ty` clustering signal automatically as it completes. Do NOT build against wd
>    typing that isn't live yet.
>
> Nothing else on whg3 is blocked on the indexing side. The Workbench clustering *view*
> (importing the self-embed primitive #5) is separate Workbench-roadmap UI work.

> **📌 FOR THE whg3 AGENT — how to work (read first):**
> - **Branch:** do all work on **`staging`** (the dev branch) — branch off `staging`,
>   commit there, and **test on the dev server**. Do **NOT** commit to or push `main`
>   (production) directly; SG promotes `staging → main` in regular batches (§6). If
>   `staging` doesn't exist yet locally, create it from the current dev tip and confirm
>   with SG before the first push.
> - **This is the whg3 (`website`) repo**, not `indexing`. Everything below is the
>   *contract* the already-deployed CRC gateway exposes to your `clustering.js`; the
>   gateway/indexing side is done and live on prod — you consume it, you don't change
>   it. If you think a gateway change is needed, flag it for the indexing side rather
>   than working around it.
> - **Test against the live gateway** via the CRC client (`api/crc_client.py` →
>   `/api/reconcile` / `/api/search`); send the opt-in flags below to receive the fuel.
>   Guard behind the staff-only BETA gate so nothing surfaces publicly while you build.
>
> **🎯 CURRENT FOCUS (14 Jul) — read the "WHAT REMAINS ON whg3" priority list above.**
> The clustering scorer + Atlas UI (Phases 1–3) and the AAT type-facet UI are all
> **code-complete on `staging`**, and the licensing/Phase-4 work is **done** (commit
> `2d4308a1b` — only awaits SG promote+migrate). So the **#1 actionable task is the
> real-browser visual pass** of the Atlas clustering UI (cluster cards, θ / merge-
> sensitivity slider, per-facet weight sliders, the `/atlas/place/?id=` modal, map-
> marker ↔ card highlight sync) — the *backends are all verified live*; only the
> *rendered* UI is unconfirmed, because the previous (headless) agent couldn't load
> MapLibre's WebGL. A whg3 agent in a real PyCharm/browser instance **can** confirm it.
> Fix any rendering/interaction bugs you find on `staging`. (Also live: wd AAT typing is
> landing on prod now → the wd type facets strengthen automatically; don't build against
> wd typing that isn't live yet.)
>
> **The `edges[]` hard-link payload is live on the gateway (2026-07-11).** The `s.l`
> hard-link signal your scorer consumes is **already shipped** by the CRC gateway; you
> do not need any indexing-side work to start on it. To receive it:
> - Send **`"include_hard_links": true`** in the `POST /api/search` **and/or**
>   `POST /api/reconcile` body (default is `false`, so it's opt-in — nothing changes
>   until you ask for it).
> - The response gains an **`edges[]`** array; each element is
>   `{"a": "<place_id>", "b": "<place_id>", "relation_type": "sameAs|exactMatch|closeMatch|distinct", "source": "<source_id>", "via_hard_link": true}`.
>   `a < b` is canonically ordered. `source` is the assertion origin (e.g.
>   `"wikidata"`, `"loc"`, `"contributor:<user_id>"`).
> - Edges cover the **result set + a bounded 1-hop** outward expansion, so an edge's
>   `b` (or `a`) may reference a place *not* in `hits[]` — treat a hard link to an
>   off-result place as a real assertion (it can seed a synthetic edge / baseline
>   component), or ignore it; your call.
> - These are the **authoritative** co-reference edges (`via_hard_link`); feed them
>   straight into Union-Find as forced merges (`sameAs`/`exactMatch`) or forced
>   splits (`distinct` — see the open question below), distinct from the *inferred*
>   `s.n`/`s.sp`/`s.t`/`s.ty` signals your scorer computes.
> - Source of truth for the shape: `gateway/hard_link_expansion.py::HardLinkEdge`
>   and `tests/test_hard_link_expansion.py` in this repo.
>
> **✅ SCOPE — most of the fuel is now LIVE on the gateway (2026-07-11):**
> - **`s.l` (hard links)** → send `"include_hard_links": true` → `edges[]` (above).
> - **`s.sp` / `s.t` / `s.ty` + `query_match`** → send **`"include_clustering_fields":
>   true`** → every hit gains: `h3` (representative H3 cell), `h3_cover` (bounded H3
>   cell list), `temporal_range` (`[min_start, max_end]` years, or `null`), `aat_ids`
>   (leaf AAT concept ids), `aat_paths` (materialised `root.…​.leaf` dot-strings —
>   **use these for Wu-Palmer `s.ty`**: depth = path length, LCA = longest common
>   prefix), and `query_match` (`{"name","score"}` — the toponym that matched). Both
>   flags are opt-in and **orthogonal**; send both for the full non-embedding fuel.
> - **`clustering_params` + `toponym_stoplist`** → **shipped in the response** when
>   `include_clustering_fields=true` (top-level `clustering_params` = weights + θ/τ
>   thresholds; `toponym_stoplist` = 500 high-frequency generic names — down-weight
>   name matches on these). The **stoplist is empirically built**; the **weights are
>   still the domain-sensible defaults** (the empirical weight fit is deferred — it
>   needs hard negatives; see §1 Calibration). Treat the weights as tunable defaults
>   and expose them on your weight sliders; the shape is stable.
> - **`s.n` (Symphonym name cosine)** → **`include_embeddings=true`** attaches each
>   candidate name's precomputed int8 128-d `phon_emb` (Atlas path — no client model).
>   **Workbench** leaves it `false` and self-embeds in a worker with the `hf/`
>   Symphonym build + int8 quant (already loaded for private records). `clustering.js`
>   is embedding-source-agnostic — decode `phon_emb` when present, else worker-embed.
> - **Design `clustering.js` to degrade gracefully:** full multi-signal composite when
>   all fields are present; drop any signal whose field is absent (renormalise the
>   remaining weights); hard-link-only baseline as the floor. That way it lights up
>   incrementally and never hard-depends on a not-yet-shipped field.

**Cleanup:**
- [x] **Deleted the stale `clusters` index** — **DONE 2026-07-12.** `clusters_20260325`
      (41.0M docs, 3.2 GB) + its `clusters` alias dropped from prod ES after the
      `build_cluster_lookup` retirement shipped. ⚠️ **One more stale build remains:**
      `clusters_20260321` (43.7M docs, 3.5 GB, **unaliased**, older dead HDBSCAN build)
      — same dead artifact, not in the original delete request; safe to delete too
      (recommend it). The tiny `cluster_state_2026032{1,5}` markers (1 doc each) are
      left in place.
- [x] **Refreshed `search-system-architecture.md` + `CLAUDE.md`** — **DONE 2026-07-12**
      for the client-side scoring/clustering model (status banner + legacy-index /
      `build_cluster_lookup` retirement notes + the live-`/search/` union-index finding).

### Consequence for sequencing

Because **all** scoring + clustering is now client-side, the indexing side is pure
*fuel* — none of it produces visible clustering on its own. The visible payoff is
entirely in whg3's `clustering.js`. The `atlas`-vs-`main` divergence that used to
gate this is **resolved** (§6): whg3 now has a single active line — develop on
**`staging`** (dev), push regularly to **`main`** (prod) — so the browser work is
just ordinary whg3-side work with no cross-branch decision blocking it. The
gateway/offline *fuel* below and the browser scorer can now proceed **in parallel**;
the fuel lights up as `clustering.js` lands on `staging` and flows to `main`.

**Recommended start order (indexing side):** all of the below **DONE + deployed**:
- ~~Hard-link expansion + ship~~ ✅; ~~per-hit payload + AAT ancestors + calibration
  params~~ ✅; ~~`include_embeddings`~~ ✅; ~~cluster retirement + index delete~~ ✅.
  (`facet_weights`/`phase_2`/`result_limit` dropped as unnecessary — see Params above.)
  **The gateway/offline clustering fuel is complete.** Remaining §1 items are the
  Django-coupled discovery scope filter and the optional/deferred `baseline_cluster_id`.

**Hold until the whg3 browser side is ready (⚠️ these break the live contract — land
them WITH the whg3 side that stops sending/reading the retired fields, never
ahead):** remove `group_by_cluster` / `cluster_threshold`, retire
`build_cluster_lookup`, swap the prominence tiebreaker, delete the `clusters` index.
The coordination is now a simple `staging`→`main` sequencing (§6), not a branch
reconciliation: land the whg3 change on `staging`, then retire the server field in
the same push window to `main`. The browser `clustering.js` itself is whg3-side work
— not this repo.

### Open questions
- ~~**`distinct` semantics**~~ **RESOLVED 2026-07-12 (SG): hard split (Option A).**
  `distinct` is a cannot-link in the browser Union-Find — an *explicit* assertion, so
  compatible with place#25's objection to *inferred* prevention. Implemented in whg3
  `clustering.js` Phase 1.
- **Architectural-plan doc status** — filed `.DEPRECATED.md` but is the backend
  spec; un-deprecate and record these decisions so we build to a matching spec.
- **Payload optimisation (deferred)** — a hybrid (gateway pre-scores public↔public
  edges, browser embeds only private records) would cut the Atlas payload but forks
  the scorer; skip until payload proves painful.

## 2. AAT / type system + per-namespace coverage backfill  ★ major

**Largely resolved as of 12 Jul 2026.** AAT `aat_ids`/`aat_paths` enrichment is
shipped and coverage is near-complete (only `gb` at 0% — see below); the AAT
**type-facet UI backend** (facets + friendly labels + hierarchical filter) is live
(§7); and the `type_mappings` index + post-retrieval consanguinity engine are
**superseded** (delivered via the hierarchical filter + client-side Wu-Palmer — see
below). What remains: `gb`, the `wd`/`pl` residual tail, the Wikidata derivation
pass, and OSM Tier-2. The per-namespace audit that drove this:

Coverage (re-audited **12 Jul 2026**, after the TGN + small-vocab backfills):

| Coverage | Namespaces |
|----------|-----------|
| **100%** | tgn (NEW), chgis, hgis, og, ofs, un, **iv · clio · po · nl · dgsd · dp · ukhc (all NEW)** |
| **~92–99%** | alc 99%, **whg 99% (NEW)**, ohm 93%, **tm 92% (NEW — rest is `people`/`kleros`)** |
| **70–85%** | gn 85%, osm 85%, wd 75%, pl 72% |
| **ZERO AAT** | **gb (1.17M!)** — the only remaining zero |

**Almost everything is now typed** (12 Jul backfills — see below). The lone zero is
**gb** (GB1900): transcribed OS map text with no native feature type — genuinely hard
(a future VLM/CV-on-map-typography idea is noted in `authorities/gb1900-places.py`).
`wd`/`pl` have a residual long tail (specific Wikidata Q-items / non-place Pleiades
metadata). Details + the whg3 `/development` note in `developer/aat-typing-status.md`.

**Outstanding:**
- [ ] One-time `aat_enrich` backfill of every namespace whose `final/` snapshot
      pre-dates the enrich stage (Batch 2 TODO) — but note several namespaces
      have **no mapping table at all yet**, not merely un-enriched:
  - [x] **TGN → AAT — DONE 2026-07-12.** TGN place types ARE AAT concepts, encoded
        in Getty's `TGNOut_PlaceTypes.nt` (rel URI `…-placeType-<aat_id>`). New
        `processing/tgn_aat_backfill.py` (extract → resolve `path`/`term` from the live
        `types` index → scripted-update patch) typed **~2.99M** live tgn docs
        (1,045/1,050 distinct AAT ids resolved), replacing the generic `place`.
        `authorities/tgn-places.py` now reads PlaceTypes.nt at ingestion so future
        re-ingests carry real types (emits `aat_ids`; `aat_enrich` path-fill adds
        `aat_paths`, same route as og/ofs).
  - [x] **iv, clio, po, nl, dp, dgsd, ukhc, tm, whg, pl — DONE 2026-07-12.** Curated
        `processing/manual_aat_maps.py` ({namespace+identifier→AAT}, an `aat:<id>`
        identifier extractor, and a whg free-text `sourceLabel` map) wired into
        `aat_enrich.augment_doc` → drives BOTH the live backfill
        (`apply_aat_enrich --namespace <ns>`) and future ingestion, no per-script
        change. Backfilled all live docs (ukhc 92, clio 15.7K, po 9K, iv 24K, nl 4.4K,
        dp 2.6K, dgsd 3.8K, tm 59.3K, whg 14.1K, pl +top-up). All ids validated
        against the prod `types` index.
  - [ ] **GB1900** — the only remaining zero; no native type. VLM/CV map-typography
        idea documented in `authorities/gb1900-places.py` (not built).
  - [x] **wd P1014 pass — DONE 2026-07-13 (offline, WDQS-free); P1014 saturated.**
        Extracted P1014 from the 148 GB dump we already hold (new
        `typesystem/extract_wikidata_p1014.py`; htc scan → 26,420-row
        `/ix1/ishi/data/wikidata/wikidata_p1014.jsonl`), and `aat_mapper wikidata` now
        reads that crosswalk (WDQS method removed; commit `84fdf2b`). **Only 1,895 /
        10,308 wd type items carry a Getty AAT ID at all**, so the pass adds **8**
        net-new mappings — the rest of the tail genuinely has no P1014. See the handoff
        block for the full write-up + prod-fold-in note. wd's remaining tail is
        recovered by the **P279 walk** (next bullet); pl's remainder is non-place
        metadata (`unlocated`/`label`/…), untypeable.
  - [x] **wd P279 walk (Pass 2) — DONE 2026-07-13; wd type coverage 21.8%→98.2%.**
        Since P1014 is saturated, the specific unmapped wd tail is recovered by
        walking P279 (subclass-of) up to the nearest P1014-mapped ancestor. New
        `typesystem/extract_wikidata_p279.py` (htc scan → **4.24M-edge**
        `/ix1/ishi/data/wikidata/wikidata_p279.jsonl`, 53 min) + `aat_mapper
        wikidata-p279` BFS walk (commit `5a38228`). **Mapped 7,869/8,057** unmapped
        types (hops 1:5146 2:2121 3:438 4:164; `confidence=broad`, `source=wikidata_p279`)
        → `wikidata.json` **2,251→10,120 (98.2%)**; only 188 types still unmapped.
        **⭐ This flips the prod-apply calculus:** P1014 alone was 8 mappings (not worth
        an 11.5M-doc reindex), but P1014+P279 → ~98% type coverage likely types a large
        fraction of the ~2.9M currently-untyped wd docs. So an
        **`apply_aat_enrich --namespace wd`** pass is now clearly worthwhile — but it's
        an ~11.5M-doc prod reindex, **heap-sensitive** (see the ES incident 2026-07-13),
        so run it *after* heap is confirmed stable + on SG sign-off. Enriched
        `wikidata.json` is staged on `/vast` (git-untracked); the P279 mappings also
        re-derive automatically on any future wd rebuild (crosswalk + this walk).
        *Note:* the 602 deep-hop (3–4) mappings are coarser `broad` types — reviewable.
  - [x] **Label matching (Pass 3) — already built, run as cleanup.** `aat_mapper
        sparql --es-host <types>` does ES-based label matching (offline). Low recall
        after P279; run it *last* on the 188-type remainder + review the `inferred`
        hits. Not a separate build.
  - [ ] ~~Hierarchy propagation (Pass 4)~~ — **not worth building:** targets the
        SUPERSEDED reverse `type_mappings`/`aat_types` index and types *zero* new place
        docs. Skip unless a consumer reappears.
- [x] **Derivation passes (`type-mapping-plan.md` §Passes 0a–4) — accounted for:**
      0a Pleiades direct ✓ (`cmd_static`), 0b TGN-bridge → superseded by curated
      statics + `tgn_aat_backfill` ✓, 0c OSM static ✓ (Tier-2 REJECTED by SG),
      **1 P1014 ✓ (offline), 2 P279 ✓ (this session)**, 3 label matching = built (run
      as cleanup), 4 hierarchy propagation = skip (above).
- [x] **Type-facet UI backend — DONE 2026-07-12** (AAT facets + friendly labels +
      hierarchical `aat_types` filter in the gateway; see §7). Uses the existing
      `types` index for labels — **no separate `type_mappings` index needed for the
      facet UI**. *(Remaining, larger + separate:)*
- [x] **`type_mappings` index + post-retrieval consanguinity engine — SUPERSEDED
      (SG-confirmed 2026-07-12); will NOT be built.** Its value is delivered by other
      means we've since shipped: (a) **narrower-term expansion** = the server-side
      hierarchical `aat_types` filter (concept + descendants via `aat_paths`); (b)
      **type consanguinity / Wu-Palmer** = client-side in `clustering.js` (`s.ty` over
      the shipped `aat_paths`) — §1; (c) the design's **`aat_types` index (§5.2) already
      exists** as the `types` index (`aat_id`/`term`/`path`/`depth`/`gn_fcodes`/`wd_qids`/
      `osm_tags`); (d) the **`type_mappings` index (§5.3) is explicitly "optional /
      reverse-lookup"** and redundant (the `types` index carries those reverse fields).
      The only unbuilt residue — server-side broader/sibling *banding* (§6.3 Tiers 2–3)
      — partly duplicates client `s.ty` and cuts against the flat-candidates /
      client-scores architecture, so it's intentionally left out. `type-mapping-plan.md`
      §5–6 is thus historical; the type UI is served by §7's AAT facets + hierarchical
      filter + the browser scorer.
- [x] ~~**OSM Tier-2 tag-key expansion**~~ — **REJECTED (SG, 2026-07-13).** Ingesting
      the 11 additional OSM/OHM tag keys (amenity, tourism, leisure, man_made,
      boundary, military, building, aeroway, railway, geological, power; ~3–5M
      features) is **not wanted in the foreseeable future.** The current 6 tag keys
      stand. (`osm-types-inventory.md` / `ohm-types-inventory.md` retained for
      reference only.)

---

## 3. Contributed WHG gazetteers (`whg:` namespace) expansion

**Only 7 contributed datasets are live** (`whg` = 14,206 docs): Antarctica names,
Yukon, Florida, Theophanes Bulgaria, Eritrea, Congo, Gabon. Both the index and
the tileserver carry exactly these 7 (`whg-892/1052/1076/1361/1481/1485/1486`).

The ingestion **code path is complete and generic** — `authorities/whg-places.py`
ingests *every* dataset the Django API returns from
`GET /reconcile/authority-datasets`, which filters on `Dataset.authority=True`.
The bottleneck is entirely **Django-side state**: the number of datasets flagged
`authority=True` (+ `public`, `ds_status ∈ {accessioning, indexed}`). The
per-dataset checkboxes in `authority-selection.md` do **not** gate anything —
only the `whg` group flag does; the doc's dataset list is a stale bootstrap
snapshot (unchanged since 2026-04-22).

**Outstanding:**
- [x] **Publication set decided + flipped — DONE 2026-07-13 (SG).** Flipped
      `authority=True` on the **41** `public`+`indexed`+non-`core` contributed datasets
      (verified `authority` is the real gate — `/reconcile/authority-datasets` filters
      exactly `Dataset.objects.filter(authority=True)`; there is no other places/toponyms
      indexing flag). Excluded `ds:2 dplace` (core → already the `dp` namespace) and
      reverted `ds:1390 depoptest`. Now **47** contributor authority datasets
      (41 new + 6 already live). DB write via the crc0 `clustering.pg_client` tunnel to
      DO Postgres `whgv3beta`, table `datasets` (**NB: not `datasets_dataset`**). *(DO
      access also via `ssh whg` → Docker containers.)*
- [x] **Re-run `whg-places.py` → staged follow-through — DONE 2026-07-13
      (searchable-data core).** Full re-stage of all **48 authority datasets**
      (**228,918** records, 0 skipped) → h3/final chain (inline on pitt, no Slurm;
      215,401 geom docs got h3_cover; parquet sidecar skipped on a `timespans.start`
      type inconsistency, JSONL fallback used) → `index_namespace --source-stage final
      --replace` (places **228,918 indexed, 0 errors**, deleted old 14,206) → toponyms
      **207,028 augmented, 0 errors** → GPU Symphonym backfill for the **88,944** new
      toponyms (job 3030670, 5s) → `index` phase (0 errors, emb v7). **Verified live:**
      48 datasets present, Madagascar sample has ccodes+h3_cover, **0** whg toponyms
      missing embeddings. *Fix committed:* `index_namespace` now skips toponyms whose
      `_id` exceeds ES's 512-byte limit (one whg LPF `name` was a 618-byte comma-joined
      variant-spelling apparatus that aborted the augment; guarded in
      `collect_attestations`, commit `db402dd`). *(No server-side re-cluster —
      client-side now.)*
- [x] **Tiles — DONE 2026-07-13.** Generated + deployed `whg-<id>` vector
      tilesets via `submit_tiles_slurm --only-bucket` (Slurm array → per-bucket
      tippecanoe + rsync deploy → `afterok` `update_tileserver_config`). **47
      tilesets serving** (200, public); 2 datasets (`whg-1642`, `whg-1644`) have
      **zero geometry** → correctly no layer. *Gotcha fixed:* 47 parallel array
      tasks overran the tileserver sshd (`kex_exchange_identification: Connection
      reset`), failing 8 pushes → the `afterok` finalize was cancelled. Re-pushed
      the 8 **sequentially** (`generate_tiles --redeploy-only`), then ran
      `update_tileserver_config` for the present 47 (116 existing config entries
      preserved).
- [x] **Registry — DONE 2026-07-13.** `push_gazetteer_inventory --namespace whg`
      → **48 per-dataset rows upserted to prod + dev** (HTTP 200, 0 errors).
      *Fixes committed:* the incremental path now fans whg out per-dataset
      (`eee6623`); **413 fix** — per-dataset h3 footprints (not the shared 48×
      namespace aggregate) + res-3 coarsening + cell-budget batching brought the
      payload from ~430 MB → **0.4 MB** (`beee789`, coarsen commit). Geom-less
      datasets get empty coverage.
- [x] **whg AAT typing — DONE 2026-07-13.** The h3/ccode ingest chain skips
      `aat_enrich`, so a follow-up `apply_aat_enrich --namespace whg --execute`
      backfilled AAT mappings live: **173,262 / 228,918 docs** now carry
      `types.aat_ids` + `aat_paths` (0 errors). *(The ~56k without are toponym-only
      or untyped LPF records.)*
- [x] **Per-dataset registry coverage is res-3 coarsened** — DONE / done-state note,
      not outstanding work (adequate for the spatial filter). Revisit only if finer
      per-dataset footprints are ever needed.
- [ ] Handle genuinely **pending/unpublished** submissions — `whg-places.py`
      flags them out of scope pending a new Django endpoint (documented gap).

---

## 4. Ingestion-rebuild tail (Batches 13b / 14 / 14a)

The rebuild is done; three tail items remain (all "not blocking"):

- [~] **Batch 13b** — legacy v3.2 reconciliation links → canonical attestation store.
      **⚠ The 2026-07-10 "no import code exists yet" note was WRONG** (corrected
      2026-07-13): import code **exists and runs in production** —
      `clustering/harvest/contributor_replay.py` **Flow A** harvests the legacy
      `place_link` + `close_matches` tables from DO Postgres, maps them to
      `contributor:<user_id>:legacy_v3_2` hard-link rows, and inserts them into the
      **runtime overlay** as "Phase 1B" of the shipped harvest job
      (`processing/submit_hardlinks_slurm.py`). **Verified live 2026-07-13:** the
      prod overlay (`/ix1/ishi/hardlinks/hard_links.sqlite`, 6.43M rows) contains
      **26,946 `legacy_v3_2` rows** — in fact *every* contributor row is legacy
      (sameAs/closeMatch/exactMatch). **So the functional need — legacy links in the
      clustering graph — is already met.** What is genuinely unbuilt is the *governed
      migration into the canonical, reviewable Django store* `api_contributorattestation`
      (durable/editable/revocable + a moderation workflow) — that is **almost entirely
      a whg3 (Django) task, not this repo**; the plan's "governed ETL, not a bulk dump"
      is about *that* target. **Two open verification questions** (answerable only
      outside this repo): **(1) DONE** — Flow A *is* populating the overlay (above);
      **(2) OPEN — dangling-edge audit:** a sample of legacy endpoints includes the
      **retired `bnf:` namespace** (Bibliothèque nationale de France) — **not in the
      current `places` index** — alongside `whg:`, so an unknown fraction of the 26,946
      point at place_ids that no longer exist (harmless to the browser — it ignores
      off-result edges — but wasted). A full ES-existence audit was **blocked by prod
      ES 429/heap pressure on 2026-07-13** (even single `_count` queries circuit-broke;
      needs an `es es-restart` first). The sharp underlying risk (whg3 LPF `feature['id']`
      == DB `places.id`, else all `whg:` legacy edges dangle) still needs a whg3-side check.
- [ ] **Batch 14** — formal integration/test harness: end-to-end staged-first
      run, multi-gazetteer fan-out→barrier, index-load-from-stage w/ ES,
      deselection + artefact cleanup, inventory push, hard-link harvest + atomic
      swap on a Pitt-mock, **scope-leakage test** (no `dataset_status:'pending'`
      leaks to off-scope users), OSM/OHM perf baseline. Plus the deferred
      validation gates scattered across Batches 4d/7/9/10/11/12 (row-count /
      toponym-count / tile-reproducibility / referential-integrity checks that
      need a real end-to-end run).
- [~] **Batch 14a** — retention-sweep. **Tool now runnable against prod
      (2026-07-12):** fixed its ES client to authenticate (it silently couldn't reach
      the authed prod ES); a prod **dry-run** succeeds and finds **0 pending datasets**
      (correct — pending submissions aren't ingested yet, §3). **Scheduling is still
      open and is a deliberate ops decision, NOT auto-installed:** it needs (a) the
      Django `/api/retention/notify` endpoint + `WHG_API_BASE_URL`/`WHG_RETENTION_NOTIFY_ENDPOINT`
      so contributors are *warned* at 11 months before the 12-month delete; (b) a host
      that reaches **both** prod ES *and* DO PG (the `gazetteer` crontab on Pitt is the
      natural home — confirm the PG tunnel there); (c) SG sign-off on cadence + enabling
      `--execute`. **Recommended rollout:** weekly `@reboot`/cron **dry-run** first
      (`python -m processing.retention_sweep --es-host http://localhost:9201`), enable
      `--execute` only once pending datasets flow (§3) and notify is wired. (Installing
      a recurring auto-**deletion** of contributor data unilaterally would be unsafe.)
- [x] Batch 12 loose end — **gateway overlay re-open: NOT NEEDED.** The gateway holds
      **no long-lived overlay handle** — `hard_link_expansion.expand_hard_links` and
      `links._connect` open the SQLite **fresh per request** (read-only, `file:…?mode=ro`)
      and close it, so a `ship_to_pitt` atomic-swap is picked up on the very next query.
      No periodic re-open / SIGHUP required. *(Remaining Batch 12: the periodic DO↔Pitt
      drift job — cadence undecided — is separate.)*
- [ ] Retire `authority-selection.md` as the selection source in favour of the
      Django gazetteer registry (Batch 3 deferred fallback path).

---

## 5. Activate already-built features (low effort, high value)

- [x] **`query_vector` LIVE — confirmed end-to-end 2026-07-12.** Wired in
      `reconcile.py` (`build_phonetic_knn(query_vector=…)`) and active after the
      restart. Proved decisively: a reconcile with a nonsense query string
      (`"Zxqwvblark"`) + the int8 embedding of *London* returned the top-5 all
      **London** (the vector drove ranking), whereas the same string with **no**
      vector returned unrelated places (Salzburg, …). whg3 `crc_reconcile_search`
      already sends it.
- [~] **Prod re-push of citation/licence metadata** (`handoff-citation-metadata.md`).
      Done on dev; prod stores none of the new attribution fields because prod's
      website code predates Phase-4. **whg3 side verified + finalised on `staging`
      2026-07-13** (agent): Phase-4 is already merged on `staging` (License model,
      migrations `licensing/0001–0003` incl. `0003_extend_licenses` seeding the 4
      custom/ND keys `CC-BY-NC-ND-3.0/4.0`/`custom-all-rights-reserved`/
      `custom-academic-use`; registry attribution fields `api/0007`; inventory
      endpoint resolves `license_spdx`→FK, skips-and-logs unknown, stores
      `license_url`/`citation_text`). Two bugs found + fixed on `staging` (commit
      `2d4308a1b`, **not pushed**): `attribution_for()` didn't prefer `citation_text`
      (returned `description` only); and a stale `licensing` seed-count test
      (asserted 8, now 15 after 0003) that would break the build. Tests added; not
      run in-agent (no PostGIS dev DB) — SG runs
      `manage.py test licensing api.tests_attribution api.tests_inventory_licensing`.
      **Prod parity now = SG: promote `staging`→`main`, then on prod
      `manage.py migrate licensing` (0003) + `manage.py migrate api` (0007)** →
      then re-run the held inventory push incl. the `for ns in ofs og ukhc`
      single-namespace pushes (unknown SPDX codes are non-fatal, so even a partial
      vocabulary is safe).

---

## 6. whg3 branch model — `staging` (dev) → `main` (prod)  ★ resolved

> **RESOLVED 2026-07-11 (SG).** The old `main`-vs-`atlas` divergence is **closed**:
> `atlas` is **abandoned** — no further development happens on it. whg3 now runs a
> single active line with a conventional two-branch flow:
>
> - **`staging`** — the development branch (dev server). All new website work —
>   the Atlas UI (folded in behind a **staff-only BETA gate**), the Collaborative
>   Workbench, the citations/licensing overhaul, **and the `clustering.js` client-
>   side scorer/clustering** — lands here first.
> - **`main`** — the production branch. `staging` is pushed to `main` **regularly**
>   as features stabilise; prod tracks `main`.
>
> The BETA gate lets the Atlas + clustering work ship to `main` incrementally
> without public exposure, so there is no big-bang cutover to coordinate.

Consequences for this plan (the earlier "atlas → main promotion" framing is gone):
- **§1's browser work (`clustering.js`) is no longer gated on any branch
  reconciliation.** It is ordinary whg3-side work on `staging`, flowing to `main`
  behind the BETA gate. The gateway *fuel* (edges[], payload assembly, calibration)
  and the browser scorer proceed **in parallel**. The gateway `edges[]` payload it
  consumes is **already live on prod** (see §1's "📌 FOR THE whg3 AGENT" callout).
- **§5's citation re-push** is gated simply on the Phase-4 / licensing code reaching
  **`main`** (via `staging`) — same code, single line.
- The contract-breaking gateway changes in §1 (remove `group_by_cluster` /
  `cluster_threshold`, retire `build_cluster_lookup`) are now a straightforward
  `staging`→`main` sequencing: land the whg3 change that stops sending/reading the
  field on `staging`, then retire the server field in the same push window to `main`.

---

## 7. Search UX parity gaps (`search-system-architecture.md` §8.3)

- [x] **Pagination — DONE 2026-07-13** (`gateway/search.py`). Added an `offset`
      param (0-based) → returns ranked hits `[offset, offset+size)`. `search_after`
      does **not** fit: the 3-step pipeline scores + re-ranks *in the gateway* (not an
      ES sort), so pagination is offset-based on the ranked list. The candidate
      over-fetch (places filter size + enrichment bound) now covers `offset+size`
      (capped at ES's 10k), and `total` already reports the full candidate count for
      page-count math. Practical depth ≈ a few thousand (the over-fetch window).
      *(Reconcile intentionally unchanged — it returns flat top-N candidates per
      query by contract. Activates on the next gateway restart.)*
- [x] **`undated` handling — DONE 2026-07-12** (`build_places_filter`,
      `tests/test_build_places_filter.py`). When `undated=True` + a date filter is
      active, the temporal clause is a `should`-wrapper matching places whose
      timespans overlap the range **OR** that have no timespans at all (`must_not
      exists` on `toponyms.timespans.start.in`/`end.in`). Passed through from
      `search.py`; default behaviour unchanged. *(Activates on the next gateway restart.)*
- [x] **`fclasses` → type facets + labels — BACKEND DONE 2026-07-12** (`gateway/search.py`,
      `es_helpers.py`). Now that ~all namespaces carry `aat_ids`/`aat_paths` (§2):
      `facets.aat_types` aggregates on `types.aat_ids` and resolves **friendly labels**
      from the `types` index (e.g. `300008389 → "cities"`); a new **`aat_types`**
      request param gives a **hierarchical** type filter — a place matches a concept
      OR any descendant via a `types.aat_paths` wildcard (validated: AAT ids are
      distinct 9-digit segments, so the substring match is exact). Additive; the raw
      `types` facet/filter still work. *(Activates on the next gateway restart; the
      whg3 side wires the checkboxes → the `aat_types` facet/param.)*
- [ ] **PeriodO vs. drawn geometry** — period geometry is mixed with
      user-drawn geometry in `bounds`; no backend way to distinguish them.
- [x] ~~`bounds` spatial filter uses `repr_point`, not extent~~ — **investigated
      2026-07-11: not a real bug.** The `bounds` path already resolves to a
      containment region (`spatial.region_from_geojson` → `h3_cover` recall gate +
      `apply_containment` fuzzy-H3/exact-Shapely refine, search.py Step 0/2.5), so
      it is already extent-aware. The `repr_point`-only centroid filter in
      `build_places_filter` is a **degenerate fallback** (fires only when
      `region_from_geojson` returns None — Shapely unavailable / malformed bounds)
      and can't be made h3-based (h3 polyfill needs Shapely too). Fixed the
      misleading comment (es_helpers.py) that had made the fallback look like the
      primary filter and referenced the removed `hull`. No behavioural change
      warranted. (A deeper follow-up, if ever wanted: stress-test the region
      coarse-gate `h3_cover` recall for finer-than-region candidate cells — but no
      gap demonstrated.)

---

## 8. Tileserver — migrate legacy contributed-dataset tiles

The tileserver serves **155 tilesets** (live audit 17 Jul; was 116 on 14 Jul). All 22
authority namespaces have tiles (+ context overlays: `gn_capitals`, `osm_misc`, basemap
layers). Legacy portal tilesets: **77 `datasets-NNN` + 2 `collections-NNN`** still served,
now alongside **47 `whg-<id>` buckets** (up from 7 on 14 Jul). Of the 77 `datasets-*`:
**24 already have a `whg-<same-id>` twin** and **53 have no `whg-*` twin yet** (un-migrated).

**▶ EXAMINE NEXT (SG, 17 Jul):**
- [ ] **Every WHG gazetteer marked for inclusion MUST have a live tileset.** Audit the
      inclusion list against the served `whg-*` buckets and generate + deploy tiles for
      any marked-for-inclusion dataset that is still missing a `whg-*` bucket (the 53
      un-migrated `datasets-*` are the first place to look; couples to §3's publication
      decisions).
- [ ] **Do NOT retire the legacy `datasets-*`/`collections-*` twins yet.** The 24
      redundant twins (and the rest) **MUST REMAIN served until the legacy web-site UIs
      are retired** — the old v3 portal front-ends still request them by `datasets-NNN` /
      `collections-NNN` id. Retire the stale entries only *after* those UIs are
      decommissioned, not as part of the `whg-*` migration.

---

## 9. Docs refresh (cheap, prevents future confusion) — DONE 2026-07-12

- [x] `search-system-architecture.md` — added a **Status banner** reframing to the
      client-side model + the live-`/search/` union-index finding; the legacy
      `clusters` sections (3b/4/6.3) are marked historical. (A full section rewrite is
      optional; the banner prevents confusion.)
- [x] `CLAUDE.md` — namespace table now lists all 22 (added alc, chgis, dgsd, hgis,
      tm, clio, og, ofs, po, whg with sources/counts); `clusters` index +
      `build_cluster_lookup` marked retired.
- [x] `CLUSTERS.md` + `CLUSTERING_GUIDE.md` — **SUPERSEDED banners** added (retired
      static HDBSCAN model; point to client-side clustering + `calibrate_params`).

---

## 10. Known-harmless / deferred (track, don't rush)

- [x] **TGN temporal extent — DONE 2026-07-13 (SG: extract it all).** Research
      confirmed TGN subjects carry no dates, but the source holds sparse temporal data.
      Extracted **all** of it (`processing/tgn_temporal.py` parsers + `tgn_temporal_backfill.py`
      + `tgn-places.py` ingestion upgrade): **relation-level** dates (`estStart`/`estEnd`/
      `historicFlag` on broader+associative rels) → **place/geometry extent** — 1,442
      places, fully applied and verified (e.g. `tgn:7013254` Raetia → `[-15, 450]`);
      **term-level** name-in-use dates → **toponym timespans** — applied where the dated
      name is a live toponym. **Live result: 2,966 tgn docs now carry real timespans**
      (was the `[2025,2025]` placeholder). *Caveat:* the live TGN nested `toponyms` hold
      only current names, so historic dated names (Stadacona, Hochelaga…) have nothing to
      attach to — a live-index toponym-completeness gap, not an extraction flaw; a full
      TGN re-ingest via the upgraded script applies **all** term dates.
- [x] **TGN toponym-completeness gap → FULL RE-INGEST — DONE 2026-07-13.** The
      live TGN docs were missing historic toponyms (Quebec had 2, should have Stadacona
      etc.). **Root cause: the May build under-extracted — the *current* `tgn-places.py`
      is already complete** (the temporal parse proved the historic names ARE linked to
      the concept via the same `prefLabelGVP`/`altLabel`/`prefLabel` preds the script
      uses; no script change needed). Executed a **full TGN re-ingest** end-to-end:
      re-staged the (now-complete, typed, temporal) `tgn-places.py` → **2,991,044 places**
      (98 min) → `index_namespace --replace --emit-new-toponyms` (delete+reindex all
      `tgn:` places, 0 errors; augmented **3,443,731** toponyms) → **709,337 new
      toponyms** emitted → **GPU Symphonym backfill** (job 3030669, a100, 34s compute) →
      `backfill_embeddings index` (709,337 vectors, `embedding_version=7`, 0 errors).
      **Verified live:** historic Greek/Chinese names present + fuzzy-searchable;
      Raetia geom timespan `[-15,450]` live; **0** tgn toponyms missing an embedding. No
      server-side re-cluster (client-side now). *(Subsumed the term-date caveat above —
      all term + relation dates applied on re-ingest.)*
- [ ] Dynamic-clustering design threads deferred by their own text: discovery-
      completeness empirical validation, Options B/C (edge/embedding shipping).

---

## Suggested sequencing

1. **Quick wins first:** confirm/restart gateway for `query_vector` (§5);
   docs refresh (§9).
2. **Website release flow (§6):** whg3 develops on `staging` and pushes to `main`
   (prod) regularly — no branch reconciliation to drive anymore. As the Phase-4 /
   licensing code reaches `main`, do the citation prod re-push (§5).
3. **Contributed gazetteers (§3):** decide the publication set, ingest, tile,
   register — this is self-contained and visibly grows the corpus.
4. **AAT/type system (§2):** start with TGN→AAT (3M docs, biggest single win)
   and the `type_mappings` index; feed the type-facet UI (§7).
5. **Clustering re-architecture (§1):** the largest effort, but now pure *fuel*
   for the browser — nothing here shows clustering on its own. Gateway/offline in
   parallel: (a) offline calibration (`clustering_params` + `toponym_stoplist`) +
   AAT ancestors; (b) gateway payload assembly + scope filter (**hard-link
   expansion + `/api/links` receiver ✅ DONE + deployed**); (c) drop the
   `clusters`-index join and delete the stale index. The visible payoff —
   `clustering.js` (all scoring + Union-Find) — lives in whg3 on `staging`→`main`
   (the `edges[]` fuel it needs is already live on prod).
6. **Rebuild tail (§4)** and **tile cleanup (§8)** as ongoing hygiene.

---

## Appendix A — (removed)

The earlier "concrete sketch" here (the `co_references` schema, the
`coreferences_patch` scripts, "kill the overlay") was the WRONG design and has
been removed. The correct, confirmed work breakdown now lives in §1 above; the
schema change and scripts were reverted and the branch deleted.

---

**✅ DISPUTE RESOLVED (15 Jul, indexing) — whg3 was right; root cause = a STALE `/vast`
clone.** whg3's round-trip proof stands: prod DB has both columns, migration `0008` applied,
receiver stores it, dev populated with the same code. My earlier "prod model lacks the field"
claim was wrong. **The actual cause: the prod inventory push ran from `/vast/ishi/elastic`,
which sits at `94fa903` — 7 commits BEHIND the coarse commit `87fb01e` — so its
`push_gazetteer_inventory.py` had no `_coarsen_coverage` at all → the payload carried no
`h3_coverage_coarse` key → prod kept its default `[]` (dev was pushed from an up-to-date
checkout, hence populated).** `_coarsen_coverage` derives coarse INLINE from the fine list at
push time (no `{ns}.h3_coverage_coarse.json` file exists), so whg3's "missing aggregate file"
guess was a red herring. **Code fix landed 15 Jul (committed + pushed to `origin/main` @ `126fe9f`):** hardened
`_coarsen_coverage` to RAISE instead of silently returning `[]` when `h3` can't be imported
(the second latent blank-the-field mode). **`/vast` clone NOT yet reconciled:** it is still at
`94fa903`; stg135 has no GitHub key on pitt (`git@github.com` SSH → publickey denied), so only
`es -update` (run as `gazetteer`) can pull it — which requires `/ix1` up anyway. **BLOCKED on
execution — token + clone both gated on `/ix1`:** the WHG API token lives only on `/ix1`
(`/ix1/ishi/secrets/whg-api.token` + the `/ix1` clone's `.env.local`), and `/ix1` is DOWN (NFS
outage — see `place#118`). **When `/ix1` returns:** `es -update` (reconciles both clones to
`origin/main`, incl. the coarse wiring + this harden fix), then one command:
```
cd /vast/ishi/elastic && /home/gazetteer/miniconda/envs/whg/bin/python \
  -m processing.push_gazetteer_inventory            # prod (default endpoint)
# then --endpoint "$WHG_DEV_INVENTORY_ENDPOINT" for dev if needed
```
Verify: `GazetteerRegistryEntry.objects.get(namespace='gb').h3_coverage_coarse` non-empty.
(Placing the token on `/vast` — per `place#118` — would let this run without waiting for `/ix1`.)

**🔁 ORIGINAL ACTION (indexing): RE-PUSH the inventory so `h3_coverage_coarse` populates whg3.**
The `h3_coverage_coarse` (res-2) field was added to the push (commits 87fb01e/c31eeb3) BEFORE
whg3 had a column to store it, so the whg3 inventory receiver silently discarded it (whg3's
registry still shows `h3_coverage_coarse == []` for all authorities). whg3 has now shipped
the field + receiver + migration `api 0008_gazetteer_h3_coverage_coarse` (staging + main).
**Please re-run `push_gazetteer_inventory` to dev AND prod** — the Atlas Area coverage filter
(h3-js client intersection) is wired and waiting on the data. Verify with:
`GazetteerRegistryEntry.objects.get(namespace='gb').h3_coverage_coarse` (expect a small res-2
cell list, not []).

**⚠️ PROD coarse push NOT landed (verified 15 Jul 06:47 UTC).** Indexing reported the coarse
re-push reached prod, but whg3 PROD still shows `h3_coverage_coarse == []` for ALL authorities
while the FINE `h3_coverage` is populated (gb=6393) and `gb.updated_at`=06:26 UTC (recent — a
push DID touch the row, but without the coarse field). whg3 prod is READY: receiver stores
`h3_coverage_coarse` (views_indexing.py:117), migration `0008` applied, prod HEAD `fd6a4c79`;
DEV populated correctly with the SAME receiver (gb=12, un=2001, gn="global"). So the prod push
payload is missing/empty `h3_coverage_coarse` (stale push code path, or the prod-target run
read a staged-aggregate dir lacking `{ns}.h3_coverage_coarse.json`). **Re-run the prod push and
confirm `GazetteerRegistryEntry.objects.get(namespace='gb').h3_coverage_coarse` is non-empty.**

**✅ whg3 PROD field CONFIRMED present + writable (15 Jul, round-trip proof).** Re: indexing's
"the fields aren't created in the prod DB" — they ARE. `information_schema.columns` for
`api_gazetteerregistryentry` lists BOTH `h3_coverage` and `h3_coverage_coarse`; and a live ORM
round-trip on prod succeeded: `gb.h3_coverage_coarse` `[]` → wrote `['82_roundtrip_test']` →
`refresh_from_db()` read it back → reset to `[]`. Migration `0008` applied, receiver stores it
(views_indexing.py:117), prod HEAD `fd6a4c79`. DEV populated correctly with the SAME code.
**=> The prod-side blocker is the indexing PROD push not carrying `h3_coverage_coarse`** (the
prod upsert set `h3_coverage` but left coarse empty). Re-run the prod push and confirm
`GazetteerRegistryEntry.objects.get(namespace='gb').h3_coverage_coarse` is non-empty.
