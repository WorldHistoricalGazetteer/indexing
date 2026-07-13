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
    - [ ] **`--calibrate` weight fit — RAN with hard negatives; STILL NOT shipped;
      defaults retained (principled).** Even with hard negatives the fit stays
      spatial-heavy (name ~0.22 / spatial ~0.46). This is a **property of the ground
      truth, not a sampling bug:** authority `sameAs`/`exactMatch` positives are the
      *same place across gazetteers*, so their coordinates are near-duplicates →
      spatial is genuinely the strongest separator *for that positive class*. It is
      **not representative of the browser's broader task** (clustering name-variant
      places at different coordinates, user records, single-gazetteer cases), and the
      "representative embedding = first attested toponym" pick understates the name
      cosine for cross-script pairs. So the name-forward **defaults** (name 0.35 /
      spatial 0.20) are retained as the shipped slider starting-point. **A trustworthy
      empirical fit needs better positives** — e.g. contributor attestations once they
      accumulate, or toponym-cosine-based positives that include different-coordinate
      corefs — plus a best-of-N representative-embedding pick. Deferred until such
      positives exist; the machinery is ready to re-run.
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
- [ ] `clustering.js` — the full scorer (all facets) + Union-Find + θ/weight
      sliders + cluster cards (Master Plan §3–4), with an embedding-source
      abstraction (payload-decode for Atlas, worker-inference for the Workbench).
      **PARTIAL.** (NB: the Master Plan's *synthetic-edge passes* are **RETIRED** —
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
> 3. **Get the Phase-4 / licensing code (citations) to prod `main`** — this is the gate
>    on §5's citation prod re-push (indexing side is ready to re-run the moment it lands).
> 4. **(Forward, not yet actionable)** the **discovery scope filter** Django half — pass
>    the user's owned pending `dataset_id` scope tokens to `/api/search` and merge DO
>    pending assertions (Master Plan Part VII). Only matters once pending datasets flow
>    (needs a Django endpoint; the gateway half is a small add when wanted).
> 5. **Consume the richer fuel as it lands** — e.g. TGN `temporal_range` just became far
>    more accurate (real source dates, 2026-07-13), improving the browser's `s.t` signal
>    for TGN; and more `whg:` datasets are being published (§3), so expect more hits.
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
  - [ ] **wd / pl residual tail** — wd's unmapped 25% is a long tail of specific
        Wikidata Q-items (needs the aat_mapper **P1014/P279 derivation pass** — the
        Wikidata API is firewalled from pitt, so run it from a net-connected host);
        pl's remainder is non-place metadata (`unlocated`/`label`/…), untypeable.
- [ ] Build the derivation passes still unstarted (`type-mapping-plan.md` §Passes
      0a–4): Pleiades direct, TGN-bridged GN/WD, OSM static (+Tier-2), Wikidata
      P1014/P279, label matching, hierarchy propagation.
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
- [ ] **Re-run `whg-places.py` → staged follow-through** (LPF fetch per dataset →
      h3/final → `index_namespace` → toponym rebuild → Symphonym embeddings) — **the
      big ingest batch**, ~multi-hour. *(No server-side re-cluster step — clustering is
      client-side now.)* `whg-places.py --dataset <id>` targets specific datasets;
      clear `staged/whg/extract/places.jsonl` before re-staging (writes APPEND).
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
- [ ] **Prod re-push of citation/licence metadata** (`handoff-citation-metadata.md`).
      Done on dev; prod stores none of the new attribution fields because prod's
      website code predates Phase-4. **Gated on the Phase-4 code + `licensing/0003`
      migration reaching `main` (prod)** via the `staging`→`main` flow (§6); then
      re-run the identical inventory push incl. the `for ns in ofs og ukhc`
      single-namespace pushes. Four custom/ND licence keys resolve on dev only
      until prod parity.

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

- [ ] **Pagination** — `size` defaults to 100; no `search_after`/scroll.
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

The tileserver serves **116 tilesets**. All 22 authority namespaces have tiles
(+ context overlays: `gn_capitals`, `osm_misc`, basemap layers), but **~79
legacy `datasets-NNN` / `collections-NNN` tilesets** from the old v3 portal are
still served alongside only 7 new `whg-<id>` buckets.

- [ ] Migrate the remaining legacy contributed datasets onto `whg-*` buckets
      (couples to §3's dataset publication decisions), then retire the stale
      `datasets-*`/`collections-*` entries.

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
