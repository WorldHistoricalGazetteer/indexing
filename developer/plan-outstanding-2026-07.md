# WHG Re-Indexing — Refreshed Plan of Outstanding Work

> **Compiled:** 10 July 2026
> **Purpose:** A single, current picture of what remains after ~6 weeks of
> isolated fixes, replacing the now-stale/partial plan docs in this folder.
> Supersedes the "outstanding" sections of `plan-ingestionRebuild.execution.md`
> and consolidates the DEPRECATED clustering plan, the handoffs, and a live
> audit of the production indices + tileserver (10 July 2026).

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
- [ ] **Per-hit payload assembly** — `h3`, `h3_cover`, `temporal_range` (derive from
      `timespans`), `aat_ids` + `aat_ancestors` (from `types.aat_paths`),
      `query_match{name,score}`; optional per-toponym `phon_emb` gated on
      `include_embeddings`.
- [ ] **Hard-link expansion + ship** — query the **union of the batch overlay + the
      live-delta** (`hard_links_live.sqlite`) for result-set assertions (+ bounded
      1-hop) and emit them as edges `{a,b,relation_type,source}` with `via_hard_link`
      provenance. Reading the live-delta here is what gives a `POST /api/links`
      real-time reconcile effect — this **is** "Ticket B" from
      `developer/handoff-hardlink-live-delta-followups.md`; there is no reconcile-time
      hard-link expansion today, so Ticket B is a requirement *on this item*, not a
      standalone task. (Pending contributor assertions are merged separately at
      Django from DO Postgres, scope-filtered — Master Plan Part VII.)
- [ ] **Discovery scope filter** — accept pending `dataset_id` scope tokens; filter
      `dataset_status:published OR dataset_id ∈ scope`; Django merges DO pending
      assertions (Master Plan Part VII).
- [x] **`POST /api/links` + `DELETE /api/links`** internal endpoints (arch plan
      §13c/d) — **implemented 2026-07-11** (`gateway/links.py` + tests, branch
      `feat/api-links-receiver`; writes a separate live-delta SQLite, contract
      reused from `sqlite_overlay`/`staging_contract`). Follow-ups (separate
      tickets): batch harvest of fresh `ContributorAttestation` rows +
      live-delta pruning in `contributor_replay.py`; optional live reconcile-time
      union(batch, live-delta) lookup; deploy on Pitt. See
      `developer/handoff-api-links-receiver.md`.
- [ ] **Params** — add `include_embeddings`, `facet_weights` (pass-through),
      `phase_2`, `result_limit`; **remove** `group_by_cluster` **and**
      `cluster_threshold` (no server clustering). Retire `build_cluster_lookup` /
      the `clusters`-index join.
- [ ] **Prominence ranking** — the initial (pre-cluster) ranking used the `clusters`
      index's `cluster_size` as a tiebreaker; with the index gone, replace it
      (baseline component size if precomputed, else attestation count / population,
      or drop the tiebreaker).

**Offline (indexing pipeline):**
- [ ] **Calibration** — produce `clustering_params` (θ_bridge/θ_query/θ_synth/
      θ_synth_structural/τ_name/τ_link + default weights) + `toponym_stoplist`
      (small offline job; salvage the deleted `calibration.py` math).
- [ ] **AAT ancestors** — ensure `aat_ancestors` are emittable per hit (derive from
      `types.aat_paths` or add a field). `temporal_range` is gateway-derived — no
      schema change.
- [ ] *(Optional / deferred)* **`baseline_cluster_id` precompute** — connected
      components over overlay `sameAs`/`exactMatch` (+ toponym `s.n ≥ 0.95`) →
      patch onto place docs (`apply_links_patch` pattern). Add the schema field
      only if built.

**Browser (whg3 — gated on §6 main↔atlas):**
- [ ] `clustering.js` — the full scorer (all facets) + Union-Find + synthetic-edge
      passes + θ/weight sliders + cluster cards (Master Plan §3–4), with an
      embedding-source abstraction (payload-decode for Atlas, worker-inference for
      the Workbench).

**Cleanup:**
- [ ] **Delete the stale `clusters` index** (`clusters_20260325`, dead `cluster_v1.0`
      HDBSCAN) once the gateway no longer reads it — DO-side grep confirmed nothing
      else does.
- [ ] Refresh `search-system-architecture.md` + `CLAUDE.md` for the client-side
      scoring/clustering model.

### Consequence for sequencing

Because **all** scoring + clustering is now client-side, the indexing side is pure
*fuel* — none of it produces visible clustering on its own. The visible payoff is
entirely in whg3's `clustering.js`, which is gated on the **§6 main↔atlas
reconciliation**. So that decision is the true gate; the gateway/offline work above
can proceed in parallel but only lights up once the browser side lands.

### Open questions
- **`distinct` semantics** — hard split (arch plan §6c) vs strong negative weight
  (place#25). It's an *explicit* assertion, so hard-split is likely compatible with
  #25's objection to *inferred* prevention — but confirm before the Union-Find is
  written.
- **Architectural-plan doc status** — filed `.DEPRECATED.md` but is the backend
  spec; un-deprecate and record these decisions so we build to a matching spec.
- **Payload optimisation (deferred)** — a hybrid (gateway pre-scores public↔public
  edges, browser embeds only private records) would cut the Atlas payload but forks
  the scorer; skip until payload proves painful.

## 2. AAT / type system + per-namespace coverage backfill  ★ major

The unified AAT type system (`type-mapping-plan.md`) is still largely a **design
doc** — the ingestion-time file-based `aat_ids`/`aat_paths` enrichment shipped,
but the `type_mappings` index, derivation passes, and post-retrieval
consanguinity engine are unbuilt. A **live audit (10 Jul 2026)** of AAT coverage
per namespace shows real gaps:

| Coverage | Namespaces |
|----------|-----------|
| **Good (70–100%)** | chgis, hgis, og, ofs, un (100%); alc 99%; ohm 93%; gn 85%; osm 84%; wd 75%; pl 70% |
| **ZERO AAT** | **tgn (3.0M!)**, **gb (1.17M!)**, iv (24K), clio (15.7K), whg (14.2K), po (9K), tm (64K), nl (4.4K), dgsd (3.8K), dp (2.6K), ukhc (92) |

Some zero-coverage namespaces legitimately lack meaningful native types
(gb1900, nl territories, whg mixed LPF, dp language points), but **tgn (3M) and
gb (1.2M)** are large and worth mapping — tgn currently emits a generic `place`
type and should carry real AAT IDs.

**Outstanding:**
- [ ] One-time `aat_enrich` backfill of every namespace whose `final/` snapshot
      pre-dates the enrich stage (Batch 2 TODO) — but note several namespaces
      have **no mapping table at all yet**, not merely un-enriched:
  - [ ] TGN → AAT (biggest opportunity; 3M docs, currently generic `place`).
  - [ ] GB1900, Index Villaris, Cliopatria (`polity`), PeriodO (`period`),
        Trismegistos, DGSD, ukhc — decide which get AAT mappings.
- [ ] Build the derivation passes still unstarted (`type-mapping-plan.md` §Passes
      0a–4): Pleiades direct, TGN-bridged GN/WD, OSM static (+Tier-2), Wikidata
      P1014/P279, label matching, hierarchy propagation.
- [ ] Build the `type_mappings` ES index + sync/alias-swap; post-retrieval
      consanguinity search engine; type-facet UI (replaces raw `identifier`
      facets — see §7).
- [ ] **OSM Tier-2 tag-key expansion** (~3–5M new features): ingest the 11
      additional tag keys inventoried in `osm-types-inventory.md` /
      `ohm-types-inventory.md`.

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
- [ ] **Decide which contributed datasets should be published** and flip
      `authority=True`/`public` on them in the WHG Django DB (the real gate).
      Alternatively run `whg-places.py --include-pending` to stage pending ones
      (isolated via `dataset_status="pending"`).
- [ ] Re-run `whg-places.py` → staged follow-through (h3/final → `index_namespace`
      → toponym rebuild → Symphonym embeddings → clustering) — or the
      incremental single-namespace path for a small batch.
- [ ] Generate + deploy `whg-<id>` tiles for the new datasets;
      `update_tileserver_config`.
- [ ] `push_gazetteer_inventory` to fan out per-dataset registry rows.
- [ ] Handle genuinely **pending/unpublished** submissions — `whg-places.py`
      flags them out of scope pending a new Django endpoint (documented gap).

---

## 4. Ingestion-rebuild tail (Batches 13b / 14 / 14a)

The rebuild is done; three tail items remain (all "not blocking"):

- [ ] **Batch 13b** — bring the legacy v3.2 reconciliation links into the
      canonical attestation store. Note (2026-07-10 audit): **no import code exists
      yet** in either repo — only the `legacy_v3_2` flag + docstrings anticipate it.
      See §1's "Legacy migration" note: recommend a governed ETL alongside the
      review workflow, not a bulk dump.
- [ ] **Batch 14** — formal integration/test harness: end-to-end staged-first
      run, multi-gazetteer fan-out→barrier, index-load-from-stage w/ ES,
      deselection + artefact cleanup, inventory push, hard-link harvest + atomic
      swap on a Pitt-mock, **scope-leakage test** (no `dataset_status:'pending'`
      leaks to off-scope users), OSM/OHM perf baseline. Plus the deferred
      validation gates scattered across Batches 4d/7/9/10/11/12 (row-count /
      toponym-count / tile-reproducibility / referential-integrity checks that
      need a real end-to-end run).
- [ ] **Batch 14a** — the retention-sweep logic (`processing/retention_sweep.py`)
      is built; only **scheduling** it (cron / Slurm / Django command) is open.
- [ ] Batch 12 loose ends: gateway-side periodic re-open / SIGHUP for the SQLite
      hard-link overlay; periodic DO↔Pitt drift job (cadence undecided);
      atomic-swap verified against a live Pitt gateway.
- [ ] Retire `authority-selection.md` as the selection source in favour of the
      Django gazetteer registry (Batch 3 deferred fallback path).

---

## 5. Activate already-built features (low effort, high value)

- [ ] **Deploy `query_vector` in the gateway** (`handoff-reconcile-query-vector.md`).
      The code is present on prod (`/ix1` HEAD is current), but the handoff
      recorded it as "not yet deployed" on 2026-07-06 — **confirm the gateway
      *process* was restarted** (`es gateway-restart`) so `/api/reconcile`
      actually honours a client-supplied 128-int8 vector. Safe any time; whg3
      already sends it and the gateway ignores it until active.
- [ ] **Prod re-push of citation/licence metadata** (`handoff-citation-metadata.md`).
      Done on dev; prod stores none of the new attribution fields because prod's
      website code predates Phase-4. **Gated on the whg3 `atlas → main`
      promotion** landing the Phase-4 code + `licensing/0003` migration; then
      re-run the identical inventory push incl. the `for ns in ofs og ukhc`
      single-namespace pushes. Four custom/ND licence keys resolve on dev only
      until prod parity.

---

## 6. whg3 `main` vs `atlas` divergence (the website side)  ★ major, website-side

> **CORRECTED 2026-07-10.** Earlier this section said the `atlas` branch removes
> the `workbench/` app. That misread the diff direction. The branches diverged at
> merge-base **2026-05-04**; **`main` is the active line** and has since built the
> **Collaborative Workbench (beta)** — 15+ commits through 2026-07-10 (Place
> Collection editor, record/dataset check-out, publish-back, community
> "Suggestions", in-editor geometry drawing). **`atlas` development is paused**
> (tip 2026-06-22) and simply predates the Workbench — it does *not* delete a
> feature main still has; main added it after atlas branched.

Two divergent website lines exist and the relationship needs your call — this is
**not** a clean "promote atlas → main":

- **`main`** — active; the **Collaborative Workbench** authoring suite lives here
  (`workbench/` + `api/` `ContributorAttestation`/`RecordSuggestion` models, CRC
  gateway client `api/crc_client.py` → `/api/reconcile`,`/api/places`,`/api/extend`,
  `/api/links`), plus the citations/licensing overhaul.
- **`atlas`** — paused; the Atlas-map default UI + "Gazetteers" reframe + region/
  type-tree/period/polity widgets + client-side dynamic-clustering scaffolding.

**RESOLVED 2026-07-11 (SG, in progress on whg3):** `main` is the single line.
The **Atlas UI is being folded into `main`** (which already carries the BETA
Workbench), behind a **staff-only BETA gate** so it ships incrementally without
public exposure while the client-side scoring/clustering is built. This collapses
the divergence and gives `clustering.js` one home. Consequences for this plan:
- §1's browser work (`clustering.js`) is no longer gated on a separate atlas
  promotion — it targets `main` behind the BETA gate.
- §5's citation re-push "gated on atlas→main" becomes "gated on the Phase-4 /
  licensing code reaching prod on `main`" (same code, single line now).
- SG will do the whg3 merge + BETA gate, then return here to reassess §1 next
  steps against the unified line.

---

## 7. Search UX parity gaps (`search-system-architecture.md` §8.3)

- [ ] **Pagination** — `size` defaults to 100; no `search_after`/scroll.
- [ ] **`undated` handling** — the temporal filter in `build_places_filter()`
      doesn't wrap the range in a `should` that also matches docs with no
      timespans (legacy parity).
- [ ] **`fclasses` → type facets** — replace legacy A/P/S/R/L/T/H checkboxes with
      a faceted type filter driven by server-side aggregations (ties to §2).
- [ ] **Type-facet labels** — server facets currently return raw `identifier`
      values; unfriendly until AAT labels are wired (ties to §2).
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

The tileserver serves **116 tilesets**. All 22 authority namespaces have tiles
(+ context overlays: `gn_capitals`, `osm_misc`, basemap layers), but **~79
legacy `datasets-NNN` / `collections-NNN` tilesets** from the old v3 portal are
still served alongside only 7 new `whg-<id>` buckets.

- [ ] Migrate the remaining legacy contributed datasets onto `whg-*` buckets
      (couples to §3's dataset publication decisions), then retire the stale
      `datasets-*`/`collections-*` entries.

---

## 9. Docs refresh (cheap, prevents future confusion)

- [ ] `search-system-architecture.md` — last updated 4 Apr; still documents the
      live `clusters` index as an enrichment source. Rewrite for query-time /
      browser clustering + SQLite hard-link overlay.
- [ ] `CLAUDE.md` — namespace table omits alc, chgis, dgsd, hgis, tm, clio, og,
      ofs, po, whg (all now live); still describes the ES `clusters` model.
- [ ] Any `CLUSTERS.md` / clustering docs — reflect retired static clustering.

---

## 10. Known-harmless / deferred (track, don't rush)

- [ ] **TGN temporal extent** placeholder `[2025, 2025]` (TGN emits no
      inception/abolition) — open domain decision, harmless.
- [ ] Dynamic-clustering design threads deferred by their own text: discovery-
      completeness empirical validation, Options B/C (edge/embedding shipping).

---

## Suggested sequencing

1. **Quick wins first:** confirm/restart gateway for `query_vector` (§5);
   docs refresh (§9).
2. **Unblock the website release:** drive the whg3 `atlas → main` promotion (§6),
   which then lets you do the citation prod re-push (§5).
3. **Contributed gazetteers (§3):** decide the publication set, ingest, tile,
   register — this is self-contained and visibly grows the corpus.
4. **AAT/type system (§2):** start with TGN→AAT (3M docs, biggest single win)
   and the `type_mappings` index; feed the type-facet UI (§7).
5. **Clustering re-architecture (§1):** the largest effort, but now pure *fuel*
   for the browser — nothing here shows clustering on its own. Gateway/offline in
   parallel: (a) offline calibration (`clustering_params` + `toponym_stoplist`) +
   AAT ancestors; (b) gateway payload assembly + hard-link expansion + scope
   filter + `/api/links` receiver; (c) drop the `clusters`-index join and delete
   the stale index. The visible payoff — `clustering.js` (all scoring + Union-Find)
   — lives in whg3 and is **gated on the §6 main↔atlas decision**.
6. **Rebuild tail (§4)** and **tile cleanup (§8)** as ongoing hygiene.

---

## Appendix A — (removed)

The earlier "concrete sketch" here (the `co_references` schema, the
`coreferences_patch` scripts, "kill the overlay") was the WRONG design and has
been removed. The correct, confirmed work breakdown now lives in §1 above; the
schema change and scripts were reverted and the branch deleted.
