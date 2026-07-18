# Plan — GB1900 place typing (map-typography → AAT)

> **Status (2026-07-18):** FULL-COVERAGE NATIONAL RUN IN PROGRESS. The VLM now runs on
> **ALL 2.67M labels** (not just the Tier-0 residual — see §0a), reading text + typography
> **and recording a bounding box per label**. As-built pipeline: **parallel S3 tile fetch
> (COMPLETE, 0 failures) → 12-way sharded Slurm cropper → VLM workers (bbox) → reconcile →
> dated edition**. Historic-county attribution (`hc_county` HCS code) built. Details + the
> evolutions since the pilot: **§0a**. This document is the concrete staged plan (off
> `plan-outstanding-2026-07.md` §2 and `developer/aat-typing-status.md`).
> **Author aid:** Claude (2026-07-17/18).
>
> **RELATED SUB-PROJECT — admin/parish BOUNDARY extraction from the same OS raster:**
> A distinct but sibling effort mines the **admin boundaries** (parish/district/borough/
> county) off the same NLS six-inch z17 tiles via a two-stage CV/ML pipeline
> (self-labelled synthetic data → RF component classifier → U-Net line-enforcer),
> seeded by the 25.9k georeferenced GB1900 boundary labels. Fully documented in
> **`developer/plan-gb1900-parish-extraction.md`** + code in
> **`developer/gb1900-boundary-probe/`**. It shares this project's tile cache, VLM infra
> and CRC a100 env.
> **Scope:** derive a coarse **place type** for each `gb:` record so they can be
> AAT-mapped like every other authority. GB1900 is the **only WHG source with 0%
> AAT coverage.**
>
> **SOURCE DECISION (SG, 2026-07-17): use the CC0 "Final Raw Dump"** (not the
> CC-BY-SA gazetteers) — CC0 avoids share-alike contaminating WHG's whole
> redistribution surface. Working source =
> `gb1900_locations.csv` (**2,666,341 pins**) inside
> `GB1900_final_raw_dump_july_2018.zip`. Trade-offs vs the curated gazetteer: it
> carries only `first_transcription` (the *unreconciled* initial text, not the
> majority-agreed `final_text`) and EWKB-hex coords — both handled (§2.4). We forgo
> the curated set's ~30k manual corrections, but **make our own via VLM readings**
> (§4.2/§4.3) and **record the VLM/OCR-detected bounding boxes** as durable data
> (§4.3, §10) since the source records none.
>
> **ULTIMATE AIM (SG, 2026-07-17): publish WHG's own place-typed EDITION** — a
> standalone, openly-published derived dataset in which **every edit is recorded and
> traceable**, **versioned**, and **re-runnable as user feedback accrues**. Built on
> the **CC0** raw dump, so WHG may license/publish it freely — but it **must NOT be
> named "GB1900" / "Great Britain 1900"**; it needs its own WHG name. Provenance,
> versioning, feedback loop, naming, and edition mechanics: **§11**.
>
> **BUILT SO FAR (2026-07-17, autonomous):**
> - **P0.5 DONE** — OS lettering scheme transcribed from OS 404 →
>   `typesystem/data/gb1900_os_lettering.json`.
> - **P0 DONE (Tier-0 text typing)** — `processing/gb1900/text_types.py` +
>   `typesystem/data/gb1900_os_abbrev.json`; run on the full **2.67M** pins:
>   **1,663,981 typed (62.4%)** zero-GPU (after numeric + keyword lift), 1.5%
>   illegible dropped, 4.5% ALLCAPS + 31.6% residual routed to Tier-1. Residual is
>   dominated by proper settlement names (Welsh/Gaelic) — exactly Tier-1's target.
> - **Tier-1 infra staged/scaffolded** — raw dump staged to
>   `/vast/ishi/gb1900/raw/`; `processing/gb1900/tiles.py` (NLS fetch + crop,
>   deg2num validated) + `processing/gb1900/vlm.sbatch` (GOTW-adapted GPU array).
>   **Feasibility confirmed (2026-07-17):** CRC *can* reach the NLS tile hosts
>   (`mapseries-tilesets.s3` HTTP 200) though not Vision of Britain; vLLM env
>   (`/vast/ishi/envs/vllm`) + HF cache present. NLS template confirmed
>   (`os/6inchsecond`, §5.2).
> - **Hampshire VLM PILOT RUN (2026-07-17)** — 500 crops → Qwen2.5-VL-72B-AWQ on
>   2×A100 (7.5 min, 500/500 legible; `gb1900_vlm.sbatch` job 3254599). **Verdict:
>   typography recovery is REAL** — `Tumuli`→OldEnglish (blackletter antiquity) ✓,
>   `Spring`→IC (water) ✓, named houses/places→RP ✓. **But two fixes found:** (1)
>   the VLM mis-reads *tiny abbreviation* labels (`P`→"ROAD", `W`→"WATERLOO") by
>   latching onto larger neighbours — so **run the VLM on the Tier-0 RESIDUAL only**
>   *(SUPERSEDED 2026-07-18 → FULL COVERAGE, §0a: the neighbour-latching was fixed by the
>   marker-crop + verbatim hint, and bboxes require reading every label)*; (2) crops tightened
>   (was 170px min width → neighbour bleed). Both implemented; residual-only re-run
>   is the confirming step. Pipeline proven end-to-end on real data.
> - **PILOT VALIDATED (2026-07-17, after iteration).** VLM reliability solved by
>   matching GOTW's recipe (h200 `gpu:1` TP=1, per-job cache isolation, `--host
>   127.0.0.1`; a poisoned shared `~/.cache/vllm` was the root cause). Crop redesign:
>   **marker-in-context** (red ring on the anchor; VLM reads the ring-marked label in
>   ANY direction — handles sloped/curved labels + neighbour-disambiguation) sized as
>   an **orientation-agnostic radius square** (caps-aware length estimate). Reading
>   fixed by passing the **crowd transcription as an error-flagged HINT** (locate +
>   correct) and demanding **VERBATIM** output (preserve OS abbreviations — `Ws`→`Ws`,
>   not "Wells"). Final residual run (127 crops): ~110/127 confirm the crowd text,
>   real corrections happen (`Little Dark Hat` clean; `broad lane`→`Broad Lane`),
>   `os_style`→type sensible (`RYDE`→RC→settlement, named→RP, pits→Stump). Residual
>   tail = a few early-stops / dropped-leading-char cases — handled by §11.5, not more
>   prompt-tuning. Pipeline files: `gb1900_tiles.py` (fetch+marker crops),
>   `gb1900_vlm.sbatch` + `gb1900_vlm_infer.py` (h200 VLM), `gb1900_os_lettering.json`
>   (`style_to_type_token` crosswalk).
>
> **DECISIONS (SG, 2026-07-17/18):**
> - **NEWER decisions (2026-07-18) — see §0a:** VLM on **ALL 2.67M labels** (full coverage,
>   not residual-only); **record a bbox per label**; **historic-county** attribution
>   (HCS codes); **admin tags (nation/district/parish) via a GB1900-gazetteer join** (CC-BY-SA,
>   Voronoi-containment fallback) — no restricted boundaries; parallel S3 fetch + **sharded
>   Slurm cropper**; everything as **idempotent per-`pin_id` patches (never re-run)**.
> - **DATA MODEL — include everything; TYPING is the guard (SG 2026-07-18):** keep all label
>   kinds — named places, **unnamed point-features** (`W` well, `P` pump, unnamed P.H. — a place
>   with `title`=type and **empty `toponyms[]`**), and **non-place annotations** (`F.P.` footpath,
>   `B.M.` bench-mark, spot-heights). Justification: each is an **attestation** that clustering
>   can later name/link across sources (a nameless `W` clusters spatially+by-type with a named
>   well elsewhere). Guard for UIs = a cheap derived **`is_named`** flag (`toponyms[]` non-empty):
>   default views / typeahead / name-search gate on `is_named`; nameless features + annotations
>   are reachable only via **type or spatial queries**. "GB-STAMP" stays (gloss: *typed map
>   labels*) — the type + `is_named` make the non-place minority explicit, not hidden.
> - **Ingest scope = COMPLETE / everything** — all ~2.67M pins ingested (incl.
>   footpaths/wells/pumps); downstream users customise their own filtering. (So the
>   §2.4 "default-exclude generic features" option is dropped — index it all, typed.)
> - **PRESERVE `gb:<pin_id>` identifiers** — they are already interlinked to other
>   gazetteers (e.g. `IV-GB1900-OSM-WD.lp.json`; prod `gb:` ids are `gb:<24hex>` =
>   raw-dump `pin_id`, verified). The raw dump is a **superset** of the current
>   abridged pin_ids, so existing links stay valid and coverage roughly doubles.
> - **No human cluster→type gate (old Tier-2 removed).** Because the OS
>   style→feature scheme is *documented* (`gb1900_os_lettering.json`), the VLM emits
>   the `os_style` code and the type is a **table lookup** — no upfront human font
>   labelling. Human effort = QA sampling + the downstream user-feedback loop (§11.2).
> - **Curated ~30k "manual fixes" investigated:** only **170** are `notes`-flagged
>   (the "1.5%" is mostly *silent* reconciliation). The broad `first_transcription`
>   vs curated `final_text` divergence (24.6%) is **overwhelmingly cosmetic**
>   abbreviation spacing (`F.P.`→`F. P.`) which Tier-0 normalisation already
>   neutralises. The genuine errors (garbles/misreadings/accents/truncations) are
>   exactly what a **VLM map-read corrects** — and the curated `final_text` for
>   noted rows is itself often garbled (`GEORGE`→`GEOR`), so **raw + our VLM
>   corrections may beat the curated CC-BY-SA set.** Design validated.
> - **Edition name (pick one; must NOT be "GB1900"):** e.g. **"WHG Historical
>   Feature Layer of Britain, c.1900"**, **"WHG British Map-Label Gazetteer,
>   1888–1914"**, **"Cartonym: WHG Typed Map Labels of Britain (c.1900)"**,
>   **"WHG Ordnance Label Gazetteer (Britain, 1888–1914)"**. Namespace stays `gb`.

---

## 0a. AS-BUILT production pipeline (2026-07-18) — supersedes the pilot narrative above

The pilot-era text above ("run the VLM on the Tier-0 residual only", single-VM fetch/crop) is
kept for history but **superseded** by the decisions and infra below. Current reality:

**Full-coverage decision (SG, 2026-07-17/18).** The VLM runs on **all 2.67M labels**, not just
the ~1M Tier-0 residual. Rationale: uniform map-grounded typing/text for *every* label, a
recorded **bbox for every label** (→ precise county + the durable bbox dataset, §12.1), and
"do it once, never re-run". Cost is the fetch (see below), not GPU. Tier-0 typing still ships
as the cheap deterministic layer; the VLM read supersedes it where processed.

**The pipeline, end to end (all state on `/vast`, keyed on `gb:<pin_id>`):**
1. **Parallel S3 tile fetch** — `gb1900_tiles fetch --workers N` (ThreadPoolExecutor, chunked,
   per-tile retry/backoff). S3 (`mapseries-tilesets.s3`) is concurrency-robust, so this is
   safe and fast: **the full-GB z16 set — 1,725,842 tiles — fetched in ~2–3 h with 0 failures**
   (882 absent sea/edge 404s). The old single-thread fetch (~days, and it *died* on a transient
   `Server disconnected`) is retired. Host note: the `nls-N.tileserver.com` shards are dead
   placeholders now — S3 is the sole free host (§5.2).
2. **Sharded Slurm cropper** — `processing/gb1900/crop_shard.py` on **htc** (`gbcrop`, 12-way
   array). Once tiles are cached, cropping is embarrassingly parallel: shard k crops pins with
   `int(pin_id,16)%nshards==k` whose crop doesn't already exist, writing `batch_s{k}_NNNN.jsonl`.
   Replaced the single-VM cropper (`gb1900_pipeline.py`), which had become the throughput
   bottleneck (the 6 VLM workers were out-pacing it and idling).
3. **VLM workers** — `gb1900_vlm_worker.sbatch` (autonomous pool; `TP` env → a100 uses TP=2).
   Scaled to the account cap: **4 a100 (8 GPUs) + 2 h200 = 6 workers**. `gb1900_vlm_infer.py`
   now emits **`bbox`** (tight box, fractional crop coords) alongside `vlm_text`/`os_style`. Each
   worker pulls `batch_*.jsonl` (glob matches both `batch_NNNN` and `batch_s{k}_NNNN`), infers,
   writes `vlm/<batch>/shard-0.jsonl`; the last worker out runs the inline reconcile → edition.
4. **Reconcile + dating** — `gb1900_reconcile.py` (hint↔VLM policy, §11.5) → `gb1900_dating.py`
   (sheet-precise, §10b) → the published edition (§11).

**Admin tags.** Two open sources, no restricted boundaries needed:
- **Historic county** — `processing/gb1900/county_attribution.py`: point-in-polygon of each
  label's **centre** (bbox-centre where detected, else best-guess offset from the pin anchor)
  against the OPEN **HCT/`ukhc`** polygons → `hc_county` = HCS 3-char code (e.g. `CRN`). Near-border
  labels get `hc_county_uncertain` + a work-list for VLM true-bbox refinement.
- **Nation / district / parish — via a `pin_id` JOIN to the GB1900 gazetteer** (2026-07-18
  decision). The complete/abridged GB1900 gazetteer carries `nation, local_authority (district),
  parish` per pin — VoB's own point-in-polygon result, **published CC-BY-SA** (fill: nation 100%,
  district 100%, parish 95%). Our `gb:<pin_id>` = the gazetteer pin_id, so the join is direct and
  gives openly-usable parish/district tags **without** any boundary geometry. This is why we
  DON'T need CAMPOP/GBHGIS (both restricted — see `plan-gb1900-parish-extraction.md` Licensing).
  Licence note: CC-BY-SA (attribution + share-alike, not CC0) → **segment the edition** (CC0 core
  + CC-BY-SA admin fields) or accept CC-BY-SA on those fields. Covers the ~2.55M complete-gazetteer
  subset (~95% of our 2.67M); fetch the *complete* gazetteer for full coverage.

Both are **incremental patches** (no re-run), like the Wikipedia-links / ccode backfills. CSV
export: `gb1900_export_csv.py`.

**Never re-run.** Every improvement lands as an **idempotent patch keyed on `gb:<pin_id>`**
(county, bbox top-up of pre-bbox records, future feedback re-typing) — no full rebuild. The
workers re-invoke the infer script per batch, so a schema change (e.g. adding `bbox`) flows to
the remaining batches in place.

**Follow-ups (post-run):** §12 untranscribed-text discovery via **bbox-overlap masking** of a
map text-spotter (MapReader/MapTextPipeline **tested & works** on OS six-inch, 2026-07-18 — mask
by bbox overlap, NOT string, and it *augments* GB1900); §12.1 bootstrap a **native OS-six-inch
text detector** from our ~2.67M-box dataset (positive-unlabelled + self-training). Both densify
the boundary-label seeds for `plan-gb1900-parish-extraction.md`.

## 0b. STOP & TUNE (2026-07-18) — VLM run paused; typography signal not yet good enough

The full-coverage VLM run was **stopped** (all gpu + htc jobs cancelled) after inspection showed
the *identification/classification* isn't reliable enough to ship:
- **VLM bbox = unreliable** (visual-grounding weakness): format is fine but placement is
  mis-located — boxes on blank ground / clipping labels / disagreeing with the anchor
  (`scratchpad/bbox_overlay.png`). **Deprecated. The text-SPOTTER (MapReader/MapTextPipeline,
  tested) is the box authority.** New split: **spotter → boxes; VLM → reading + `os_style`**.
- **`os_style` = suspect** (e.g. `B.M.`→`EC`); coherence unjudgeable while crops were bad.
- **Cropper = OK** (14/14 sampled marker-crops contained the target label at the ring anchor)
  but crops are loose + have the **red ring baked in** → VLM-marker-specific, not neutral inputs.

**Keep / discard (don't pollute the new pipeline):**
- KEEP (clean, independent, reusable): **tiles** (full GB, 0-fail fetch), **`national_typed.jsonl`**
  (Tier-0 text+coords), **`gb_admin.jsonl`** (gazetteer nation/district/parish), gazetteer CSV.
- QUARANTINE / clear before re-run: **`vlm/` shards** (bad bbox + suspect `os_style`; text
  re-derivable) and the **marker-crops** (ring baked in). **DELETED 2026-07-18** — the ~40 G of
  marker-crop PNGs on `/vast/ishi/gb1900/crops/` had filled the shared `/vast` volume to 100%
  and tripped ES's flood-stage read-only block on prod indices (see below).
- New pipeline starts from **tiles + national_typed + gb_admin** only.

**Storage principles (SG 2026-07-18, learned the hard way — see `vast_capacity_and_crop_fragments`):**
- `/vast` is a **1 TB volume shared with production Elasticsearch** → capacity-critical. Never
  persist millions of throwaway image fragments there.
- **Store crop COORDINATES, not image fragments.** Crops are cheap to re-extract from the cached
  tiles and discard. The re-worked cropper (`crop_shard.py`) must emit a manifest of
  `{pin_id, tile refs, window/bbox}`; VLM/spotter/HITL extract crops **in-memory on demand** and
  discard. Tiles become the one durable input; everything downstream is ephemeral.
- **Tile lifecycle:** durable backup = a single tar on `/ix1` (`/ix1/ishi/gb1900_tiles.tar`, 20 G;
  no small-file NFS penalty at rest). For a crop campaign, keep/untar the loose tiles on **`/vast`**
  (shared flash — all Slurm-array shards read one copy; fast). Never crop from loose tiles on
  `/ix1` (that random small-file NFS read pattern is exactly what drove the /ix1→/vast migration).
- **HITL UIs live IN THE REPO** (`font_hitl_review.html` + `hitl_build.py`; build locally, open via
  `file://`), **not** as claude.ai artifacts.

**Working store: DuckDB (DECISION SG 2026-07-18).** Retire the loose-JSONL-per-stage +
`reconcile.py` join model. The consolidated dataset is **one DuckDB file** keyed on `pin_id`:
```
pins(pin_id PK, lon, lat, spotter_text, os_style, size_band, os_case, type_aat,
     admin_nation, admin_district, admin_parish, hc_county,
     date_start, date_end, is_named, tile_id, ...)  -- resolved per-pin state
spotter(pin_id, box_idx, x0,y0,x1,y1, text, conf, cap_height_m)  -- 0..N MapReader boxes/pin (box authority)
tiles(tile_id PK, tile_x, tile_y, paper_level, grad_x, grad_y,
      is_colour, sheet_id, edition, ...)            -- per-tile/sheet imagery metadata
```
- **OS type = STYLE × SIZE × CASE — three orthogonal, independently-measured axes (SG 2026-07-18).**
  *style* ← the font embedding (size-blind by height-normalisation; label style only, size needs no
  human labelling); *size* ← `spotter.cap_height_m`, the box height in **ground metres** (z16 px ×
  ~1.45 m/px), banded into `size_band` (small/med/large — natural gaps in the data, e.g. serif_italic
  splits ~21–28 m standard vs 58–102 m large); *case* ← `allcaps`/rendering. Verified on the first
  HITL batch: within one style, cap-height is clearly multi-modal (the "###ath" large-italic that has
  the same style but a bigger size). Size also aids disambiguation (large caps → road/parish).
- **`tiles` = imagery-metadata / calibration layer (different grain from `pins`).** Holds the
  per-sheet/tile **paper-tone flat-field parameters** (compact: paper level + linear/low-order gradient,
  NOT rasters — *params-not-fragments*), computed once in a preprocessing pass and read-many by the
  font-embedding stage / HITL crop builder / any re-cropping. `pins`/`spotter` carry `tile_id` and
  **join** to it at extraction time to divide out illumination. Same table later carries colour-vs-mono,
  sheet edition/date, and the sheet-overlap extents for the §12.2C edge-duplicate merge.
- **Why (over Parquet-join-on-read):** the pipeline is idempotent-per-`pin_id`-patch / never-re-run,
  so *resume/residual* ("which pins still lack a spotter box / a read?") and *HITL corrections* must
  be one-line SQL against a materialized table, not an anti-join across accumulating per-stage
  Parquet + dedup-by-latest. Also cleanest ES hand-off (`SELECT * FROM pins` → bulk `gb:` docs) and
  a one-table substrate for the HITL viewer.
- **Parallel-write-safe flow (matches Slurm arrays):** workers write per-shard **Parquet** (no lock
  contention) → a **single serial merge** does `INSERT … ON CONFLICT(pin_id) DO UPDATE` → exports
  the next stage's inputs as **shard-partitioned Parquet** so VLM/spotter workers **never open the
  DB** (they read their Parquet partition, write result shards). The serial upsert (few M rows from
  columnar Parquet, seconds–minutes) runs *between* stages, off the workers' critical path — not a
  bottleneck at 2.7 M pins. DuckDB is already a project dependency.
- **Crop windows = `spotter` box coordinates** (no image fragments) → realises coords-not-fragments.

**Typing-signal policy (SG 2026-07-18):** typography is the primary type signal; **do NOT lean
more on text-based typing** — restrict text-based typing to cases where a *checked* transcription
is an unambiguous OS abbreviation (`F.P.`, `B.M.`, `W`, `P`, `Ch.`…). Proper names carry no type,
so the font classification must be made reliable (the tuning goal).

**Tuning agenda:** (1) spotter → clean tight crops (no ring) from tiles; (2) re-assess `os_style`
coherence on clean crops → salvage via prompt-tuning, or pivot to **font-embedding clustering**;
(3) rebuild the HITL on clean crops. §12.0/§12.1 already updated (spotter is the box authority).

**Font-embedding feasibility probe — RESULT (2026-07-18, gpu job 3275512, `developer/gb1900-font-probe/`).**
The VLM `os_style` is confirmed incoherent (montages: italic/upright lumped in RP; Stump≈EC; outline
"COTON" + mixed-case "Redhill" both RC), so we pivoted to a learned style embedding. A synthetic
supervised-contrastive encoder (8 OS-axis classes; ink composited on real tiles + degradation)
scored **knn 0.99 / silhouette 0.78 on synthetic** — the axes are strongly learnable — but on the
2500 real spotter crops it **collapsed**: HDBSCAN gave 1 coherent cluster (66 clean serif place-names)
+ 1 heterogeneous blob (2145, italic+numerals+abbrev+serif mixed) + 289 noise; no VLM-label alignment.
**Verdict: DOMAIN GAP, not a weak signal** (opposite of the boundary probe) — capacity is proven, the
work is synthetic→real transfer. Partial transfer already occurred (the serif-placename cluster).
**Iteration-2 levers, prioritised:** (a) **paper-tone alignment** — per-sheet/tile cached flat-field
(illumination) correction applied to both synthetic + real so paper is canonical and only ink varies
(SG idea; now the lead lever) — preserve ink weight, don't binarize; (b) degradation realism — real
backgrounds with linework crossing text, measured ink/paper profiles, curved baselines; (c) a little
real supervision — anchor/fine-tune on the HITL-labelled crops (few-shot metric learning) and/or an
unsupervised photometric-consistency term on real crops (doubles as domain adaptation); (d) stronger
backbone / higher input res / glyph-level embedding. Gate iteration-2 on the same synth+real montages.

**Iteration-2 RESULT (2026-07-18, gpu job 3276528; +road_caps → 9 classes).** Synthetic gate held
(**knn 0.925** @ 1/9 chance). Real clustering **broke out of the iter-1 collapse**: HDBSCAN found 4
coherent clusters (+1880 noise) — 00 (136) **road/path context** (spaced caps STREET/ROAD/WATLING/ROMAN
+ F.P. + busy bg — validates the road_caps/casing signal), 01 (298) serif place-names + numerals, 02
(121) italic single-letter marks, 03 (65) pure italic "M.". **Verdict: domain gap PARTIALLY closed** —
structure emerged where iter-1 had none, but (a) 75% still noise, (b) clusters partly content-driven
(03=all "M.", 02=single letters), (c) the upright-vs-italic serif place-name axis still not cleanly
split. **Iteration-3 lead lever: a little REAL supervision** — few-shot metric learning anchored on the
HITL-labelled real crops (classify by nearest labelled exemplar per OS style), which should sharpen the
fine place-name axes clustering blurred; plus glyph-level/content-hardening. Short marks (M./P/B.)
cluster by content — fine, they're typed via the checked-abbreviation route, not style.

**Iteration-3 + FUSION RESULT (2026-07-18) — CONCLUSION of the font-typing exploration.**
275 HITL anchors (2 batches; +`slab_italic` = 10 classes). Coverage reality: the rare treatments are
GENUINELY SCARCE in the test region — serif_italic 44 / serif_upright 24 / road_caps 10 solid, rest ≤3
(sans/engraved 0). iter-3 few-shot anchor-kNN: synth held (knn 0.877) but real style classification
weak; serif_italic montage decent, serif_upright a real upright/italic mix. **Fusion head** (SG idea —
embedding + size + case + text, since tier-0 `os_style` is EMPTY): lifts *every* style class over
embedding-only (serif_italic .16→.25, serif_upright .08→.17, numeral .45→.53; overall .41→.45) —
architecture validated — **but the absolute ceiling on the fine upright/italic serif axis stays ~0.17–0.25.**
**Verdict:** across VLM→embedding(iter1-3)→few-shot→fusion, fine font-style typing on real OS crops does
NOT reach usable accuracy. What DOES work reliably: **tier-0 TEXT rules** (`tier0_rule`: abbrev+keyword+
numeric already type ~59%; `residual` 33% is the target tail), the **SIZE axis** (`cap_height_m`, clean +
free), **CASE** (`allcaps`), and **short-mark/word separation**. Recommendation: bank those for typing;
treat fine font-style as a **low-confidence enrichment** (apply only where confident, e.g. strong
italic-serif→water); reopen deep embedding R&D only for a big lever (pretrained backbone + glyph-level +
wider multi-region anchors). See [[gbstamp_font_typing_pivot]].

## 1. Summary / goal / success criteria

**Goal.** Give every GB1900 label a `types[].identifier` drawn from a small
controlled vocabulary of OS feature kinds (e.g. `church`, `public-house`, `farm`,
`well`, `railway-station`, `parish-boundary`, `city`, `village`, `hill`, `river`,
`wood`…), then map that vocabulary to Getty AAT — reusing the **exact same
enrichment path every other small authority already uses**:

- Add a `"gb": { <token>: [<aat_id>, …], … }` block to
  `processing/manual_aat_maps.py` (`MANUAL_AAT_MAPS`).
- Re-index the `types[]` field on the live `gb:` docs (per-record token).
- Run `python -m processing.apply_aat_enrich --namespace gb --es-host … --execute`.
  `aat_enrich.augment_doc` reads `MANUAL_AAT_MAPS["gb"][identifier]`, injects
  `aat_ids`, and the AAT-hierarchy path-fill attaches `aat_paths`
  (`processing/aat_enrich.py:130-231`). No authority-script change is needed for
  the AAT step — the same table drives future ingestion too.

**What "typed" means here:** a record carries at least one AAT id resolvable in
the prod `types` index, so it becomes filterable/facetable by type
(`aat-typing-status.md` §"Why this matters").

**Success criteria (tiered — we do NOT need 100%):**
- **Tier 0 (text-only), target ≥ 55–70% of records typed** at a coarse level,
  zero imagery. Cheap, deterministic, shippable on its own.
- **Tier 1+2 (typography), target ≥ 90% typed** after the VLM/clustering pass +
  human cluster→type assignment on the residual.
- Every emitted token maps to a **validated** AAT id (validated against the prod
  `types` index, per the `manual_aat_maps` convention comment).
- Reversible: typing is a metadata overlay on existing docs; a bad batch can be
  re-patched (idempotent, like `wikipedia_sitelinks` was — see
  `developer/handoff-wikipedia-sitelinks.md`).

---

## 2. Data reality (VERIFIED)

### 2.1 The raw source

- **File (on CRC):**
  `/ix1/ishi/data/gb1900/GB1900_gazetteer_abridged_july_2018/GB1900_gazetteer_abridged_july_2018.zip`
  → inner CSV `GB1900_gazetteer_abridged_july_2018/gb1900_abridged.csv`,
  **UTF-16** encoded. This is the *abridged* public release
  (Vision of Britain / NLS Data Foundry, **CC-BY-SA** — *not* CC0; see §2.4
  licensing). Loaded by `authorities/gb1900-places.py` (`stage_gb1900`).

- **Exact column list (verified by opening the zip on `pitt`):**
  ```
  pin_id, final_text, nation, local_authority, parish,
  osgb_east, osgb_north, latitude, longitude, notes
  ```

- **Geometry recorded = a single anchor point only.** There is
  **`osgb_east`/`osgb_north`** (British National Grid, EPSG:27700) **and**
  `latitude`/`longitude` (WGS84) for **one point per label**. **There is NO
  bounding box: no width, no height, no other corner, no text angle, no font
  metadata.** `authorities/gb1900-places.py:76` builds a `Point` from
  `[lon, lat]` and keeps nothing else. → **The "single anchor point, no box
  extent" hypothesis is CONFIRMED by the data.**

- **Which point is the anchor — CONFIRMED and refined.** The GB1900 / NLS
  project documentation states each name was tagged with the coordinates of
  *"the **bottom-left of the first letter of its first word**."* (NLS OS1900 /
  GB1900 gazetteer docs, corroborated by the GB1900 Wikipedia entry). So it is
  **not** the box centroid and **not** even the box corner — it is the
  **baseline-left of the first glyph**. The docstring in
  `authorities/gb1900-places.py:25` ("south-west corner of the text label") is
  *approximately* right but should be corrected to "bottom-left of the first
  letter."

  **Implication for cropping (Tier 1):** we know where the label *starts*
  (baseline-left) and the reading direction is *rightward and slightly up/down*,
  but we know neither the text length in map units nor the font size nor the
  rotation. The crop window must be **estimated and over-sized**, then the label
  re-detected inside it (see §4.2).

### 2.2 Text signal already in hand

- **`final_text`** is the transcribed label string — our richest free signal.
- **ALLCAPS fraction ≈ 24.9%** (measured over a 200k-row sample of `final_text`
  on `pitt`). **ALLCAPS is NOT a standalone type prior.** On OS County Series maps
  ALLCAPS is a *case* that appears across many feature classes, and the actual
  type is carried by the **font family, size band, and letter-spacing** the caps
  are set in — e.g. large upright roman caps = town/city; small caps =
  village/parish; **wide-spaced antique/italic caps = seas, mountain ranges,
  regions, and antiquities**; boundary/administrative labels in yet another face.
  So ALLCAPS is best treated as a **routing signal into the typography pass**
  (§4.2), decisive on its own only when the text content also tells us the type.
- Abbreviations are pervasive and follow the **OS abbreviations convention**
  (sample rows: `"Parly. & Munl Boro. By."`, `"F.P."`, `"P.H."`, `"Ch."`,
  `"Sch."`, `"Sta."`, `"Well"`, `"Fm."`, `"Ho."`). These are near-deterministic
  type tells (see §4.1).
- `nation` ∈ {England, Scotland, Wales} → drives `ccodes=['GB']` today; also
  tells us which NLS coverage/edition applies.
- `parish` / `local_authority` are modern admin context (useful for sheet/era
  disambiguation, not typing).

### 2.3 Era

Survey period **1888–1914** (2nd-edition County Series 1:10,560 "six-inch"),
hard-coded in `authorities/gb1900-places.py:70`. This **matches the NLS "OS
Six-inch 2nd edition, 1888–1915" seamless layer** (see §5.1) — a clean era match,
which is the key to fetching the *right* map raster.

### 2.4 Abridged vs COMPLETE source — the abridgement dropped ~1.38M **rows**, not columns

WHG currently ingests the **abridged** GB1900 release
(`GB1900_gazetteer_abridged_july_2018.zip`, **1,174,450 rows**;
`processing/settings.py:536,549`). A **complete** release exists
(**2,552,460 rows**). **The two share the same columns** — the abridgement removed
**~1,378,010 rows**, being the commonest repeating feature labels:

| Removed label | Meaning | Count (complete) |
|---|---|---|
| `F.P.` | footpath | 306,583 |
| `W` | well | 190,979 |
| `P` | pump | 115,877 |
| … | other common OS-abbreviation features | remainder to ~1.38M |

**Why this matters *specifically for typing*:** the dropped rows are the ones Tier 0
types with **near-100% confidence** — they *are* pure OS abbreviations
(`F.P.`→footpath, `W`→well, `P`→pump). The abridgement threw away exactly the
low-hanging fruit for a *feature*-typing project (it was curated for a *place-name*
gazetteer, where a footpath crossing / well / pump isn't a "place" — a deliberate,
defensible choice for that goal, `settings.py:536` verified 2026-06-06).

**Consequences / options (a scope decision for SG):**
- **Fetch the complete set regardless.** Even if not all of it is ingested as
  searchable places, it is a **free, massive, self-labelling ground-truth** for
  Tier 0 (every `F.P.` is a labelled footpath, every `W` a well) and — with map
  crops — for validating the Tier 1 VLM typography classifier against known types.
  High value, ~zero cost, no product commitment.
- **Whether to *ingest* the extra ~1.38M as searchable places is a product call.**
  Options: (a) ingest all, with types, and **default-exclude** the generic-feature
  classes (footpath/well/pump) from search so they don't swamp GB results; (b)
  ingest them as a **separate feature layer / down-ranked**; (c) keep the abridged
  set for the gazetteer and use the complete set **only** as typing ground-truth.
- Ingestion cost is low: **same schema**, so it's a source-URL swap in
  `settings.py` + `gb1900-places.py` + a re-stage; the type work is *cheaper* per
  record than the abridged set (more of it is trivial-text-typeable).
- **Complete download URL — CONFIRMED (downloaded + inspected 2026-07-17):**
  `https://www.visionofbritain.org.uk/downloads/GB1900_gazetteer_complete_july_2018.zip`
  (140 MB zip → **703 MB** UTF-16 CSV `gb1900_gazetteer_complete_july_2018.csv`).
  Its README states **2,552,459 rows** and lists the **identical 10 columns**
  (`pin_ID, final_text, nation, local_authority, parish, osgb_east, osgb_north,
  latitude, longitude, notes`) — so the abridgement is purely row removal, as
  suspected. *(`pastplace.org` was unreachable this session; use the
  visionofbritain.org.uk host. From the CRC boxes both hosts returned HTTP 000 —
  the fetch must run from a networked host and be staged onto `/vast`/`/ix1`.)*
- **Bonus quality gain:** the complete set had **~30,000 points (c. 1.5%)
  manually checked & corrected against the historical maps** after the crowd-source
  phase — so overlapping records are also *cleaner*, not just more numerous.
- **A third release exists — the CC0 "Final Raw Dump"** (four tables, all raw
  crowd-sourcing data incl. every transcription/confirmation, minus volunteer PII).
  `.../downloads/GB1900_final_raw_dump_july_2018.zip`. **CC0** (no attribution/
  share-alike). Useful if per-transcription confidence/vote data ever helps
  (ambiguity flags), but messier than the reconciled Complete gazetteer.

### 2.5 Licensing & attribution (NOTE — surface when redistributing)

Per SG (2026-07-17), record and surface the GB1900 / Vision of Britain copyright
terms whenever relevant (esp. before any redistribution). From
`visionofbritain.org.uk/data/`:

- **Complete AND Abridged gazetteers → CC-BY-SA.** Commercial use allowed, but you
  **must acknowledge "the Great Britain Historical GIS, the GB1900 partners and
  volunteers"**, **must not imply endorsement** by the GB1900 project/partners,
  **must link the licence and note if changes were made**, and — **share-alike** —
  may **only redistribute under the same CC-BY-SA licence, without additional
  restrictions.** You **may not** call any derived work "GB1900 Gazetteer" (or
  similar); only *unmodified* files may carry that name.
- **Raw Dump → CC0 1.0** (no acknowledgement required; same name restriction).
- **Implication for WHG:** the SA clause reaches WHG's redistribution surface
  (API/tiles). This does **not** gate ingestion (cf. `feedback_defer_licensing`;
  sitewide attribution is being rebuilt separately) — but the attribution +
  share-alike obligation must be captured in that attribution system, and the CC0
  raw dump is the SA-free alternative if share-alike proves awkward. See
  memory `gb1900_visionofbritain_licensing`.

---

## 3. Approach — a three-tier pipeline

The insight: **most records can be typed from text alone; imagery is only for the
residual.** Do the cheap deterministic thing first, measure the gap, then spend
GPU only where it pays.

```
Tier 0  text-only heuristics (OS-abbreviation dict + keyword gazetteer)     → majority
   │        (deterministic, no imagery, no GPU; ALLCAPS is only a routing
   │         flag into Tier 1 — never a type on its own, see §4.1.2)
   ▼
Tier 1  typography signature via NLS map raster + VLM/CV, then CLUSTER      → residual
   │        (GPU Slurm; per-label crop → font-style descriptor → embedding)
   ▼
Tier 2  HUMAN assigns each font/typography cluster → one type token (once)  → propagate
            (a few hundred clusters, reviewed in a small notebook/UI)
```

All three tiers converge on the **same output**: a per-record type **token** from
one controlled vocabulary → `manual_aat_maps["gb"]` → AAT.

---

## 4. Tier detail

### 4.1 Tier 0 — text-only typing (do this first; ship independently)

Pure-Python, deterministic, runs on any host (no GPU, no imagery). Three signals,
highest-confidence first:

1. **OS-abbreviation dictionary.** Build `typesystem/data/gb1900_os_abbrev.json`
   mapping OS County Series abbreviations → type token. These are standard and
   published (NLS "OS map abbreviations" guide; Charles Close Society sheetlines).
   Examples:
   | abbrev / token in `final_text` | type token | AAT concept (to validate) |
   |---|---|---|
   | `Ch.`, `Chy.` | `church` | 300007466 churches |
   | `P.H.` | `public-house` | 300005141 public houses / inns |
   | `Sch.`, `Schl.` | `school` | 300005526 schools |
   | `P.O.` | `post-office` | 300005982 post offices |
   | `Sta.`, `Ry. Sta.` | `railway-station` | 300005815 railroad stations |
   | `F.P.`, `F.B.` | `footpath` / `footbridge` | 300055977 / 300007836 bridges |
   | `Well`, `Spr.` | `well` / `spring` | 300006860 / 300008698 |
   | `Fm.`, `Farm` | `farm` | 300000206 farms |
   | `Ho.`, `Hall` | `house` / `hall` | 300005425 houses |
   | `Sml.`, `Mill` | `mill` | 300004396 mills |
   | `Quy.`, `Bdy`, `Boro. By.` | `quarry` / `boundary` | 300000275 / 300387473 |
   | `Inn`, `Hotel` | `inn` | 300005141 |
   | `Br.`, `Bri.` | `bridge` | 300007836 bridges |
   Longest-match / word-boundary matching; a small ordered rule list, not ML.

2. **ALLCAPS — a router, not a type.** ≈25% of rows are all-caps, but caps alone
   is **ambiguous**: OS uses it for towns, villages, parishes, boundaries, seas,
   ranges, regions and antiquities, distinguished only by **font family / size /
   letter-spacing** (see §2.2, §4.2). So Tier 0 must **not** collapse ALLCAPS to a
   single "prominent place" token. Handle it in three ways, in order:
   - **Decisive text content wins:** an ALLCAPS label whose words are a type tell
     still types from §4.1.1/§4.1.3 (e.g. `... PARISH`, `... BORO`, `... DIVISION`,
     `CO. ...` → administrative; a caps river/range name → physical). Case doesn't
     override an explicit textual tell — it just co-occurs.
   - **Otherwise defer:** an ALLCAPS label with no textual tell is **routed to the
     Tier 1 typography pass** (family + size band + tracking resolve it), NOT
     assigned a coarse type here. Emit it as `residual` with an `allcaps=true`
     feature so Tier 1 prioritises it.
   - Record the ALLCAPS flag as a **feature**, never as a final answer, so the
     downstream clustering can combine it with the typographic descriptor.

3. **Gazetteer / keyword heuristics.** Suffix/keyword table on the descriptive
   words that OS uses in full: `Wood`, `Plantation`, `Common`, `Moor`, `Hill`,
   `Down`, `Fell`, `Point`, `Head`, `Bay`, `River`, `Brook`, `Burn`, `Lough`,
   `Reservoir`, `Colliery`, `Works`, `Quarry`, `Cottage`, `Cottages`, `Bridge`,
   `Wharf`, `Pier`, `Chapel`, `Cemetery`, `Castle` (antiquity), `Tumulus`,
   `Camp`, `Fort` (antiquities). Each → a token.

**Deliverable of Tier 0:** a script `processing/gb1900/text_types.py` that reads
`final_text` and emits `(place_id, token, confidence, rule)`; a coverage report
(what % typed, histogram over tokens, residual list). **This alone likely types
the majority** — the biggest single win, and it de-risks the whole project before
any GPU spend.

**Also cross-check against GOTW's `build_aat_shortlist.py` / `aat_resolve.py`**
(`/home/stephen/PycharmProjects/GOTW/process/`) — they already do string→AAT
shortlisting and may donate an abbreviation/label→AAT seed list.

### 4.2 Tier 1 — typography signature from the map raster

For records Tier 0 can't confidently type (and as a *cross-check* on a sample of
those it can): read the label off the georeferenced OS raster and characterize its
**typography**, because OS rendered feature classes in distinct, **documented**
type styles.

#### 4.2.0 This is grounded in the OS lettering specification — NOT guesswork

The core premise (type style ⇒ feature class) is **not** an assumption; OS
published formal lettering specifications and the style→feature scheme is
documented in primary sources (verified 2026-07-17):

- **"Character of Writing for Ordnance Survey Plans" (OS 404), 1881 & 1914
  editions** — the OS's *dedicated* lettering spec (digitised at NLS). The 1914
  edition is contemporaneous with GB1900's survey window.
- **"Conventional Signs and Writing Used on the Six Inch Maps of the Ordnance
  Survey" (Plate IV)**, in *A description of the large scale maps of Great Britain*
  (1920) — the six-inch writing plate (NLS `maps.nls.uk/view/128076894`).
- **"Notes on Archaeology for Guidance in the Field" (1921, O.G.S. Crawford)** —
  antiquities are lettered by font **by period**: pre-Roman, Roman, post-Roman
  (Saxon/medieval) each get a distinct style.
- **"Notes on Boundaries" (1914)** — boundary/mereing labelling.
- **Richard Oliver, "A few notes on map lettering", *Sheetlines* 95, pp. 33-36**
  (Charles Close Society; PDF read 2026-07-17) — secondary synthesis, with the
  OS's own style **names** (below), citing *Ordnance Survey alphabets* (OS, 1934)
  + internal type-specimen manuals.

**What the source actually says — TRANSCRIBED FROM OS 404 (June 1914), the
per-feature writing table (read 2026-07-17).** OS 404 lists the writing character
for *every* feature class, in two scale columns; **for GB1900 we read the
"1/10560 and 6-inch scales" column.** Its style abbreviations:

| Abbrev | Style | | Abbrev | Style |
|---|---|---|---|---|
| **R.P.** | Roman Print (serif, mixed-case) | | **E.C.** | Egyptian Capitals (slab sans) |
| **R.C.** | Roman Capitals | | **O.E.C.** | Open Egyptian Capitals |
| **I.C.** | Italic Capitals | | **O.R.C./O.I.C.** | Open Roman/Italic Capitals |
| **Stump** | the standard stamped hand (the *default*) | | **Old English / German Text** | black-letter (antiquities) |
| **Ornamental** | decorative (counties, county boroughs) | | | |

**Documented six-inch feature → style (selected, from OS 404 pp. 9–11):**

| Style (6-inch column) | Feature classes OS assigns it |
|---|---|
| **R.C.** (Roman Capitals) | cathedrals, county boroughs, barracks (large), forts, forests, cattle markets, cemeteries, colleges, courts of law (principal), harbours, havens, headlands (large), **hill ranges**, hospitals, hotels, large bays/beaches |
| **R.P.** (Roman Print) | churches, chapels, town halls, dispensaries, drill halls, dock buildings |
| **I.C.** (Italic Capitals) | **canals, canal basins, docks, "cuts" in navigable rivers, public gardens, deer parks** — i.e. **water & designed-water features** |
| **E.C.** (Egyptian Capitals) | **Roman** antiquities; parliamentary county divisions; courts & alleys |
| **Old English** | **pre-historic / Saxon** antiquities (+ ancient almshouses) |
| **German Text** | **Norman or subsequent (medieval)** antiquities |
| **Ornamental** | counties, county boroughs |
| **Stump** (default) | farms, brooks, fords, ferries, foot bridges/paths, single dwelling houses, collieries, coal pits, filter beds, cattle pens, caves, drying grounds, guide posts, recreation grounds, grave yards … (the **bulk** of minor features) |

Antiquities are thus lettered **by period** (Roman = E.C.; pre-historic/Saxon =
Old English; Norman+ = German Text), confirming the antiquity-font premise
precisely. **Process note (dating):** from **1882** the six-inch was
photo-lithographed with **stamped** lettering, standardised across the map by the
**1890s** — so GB1900's 1888–1914 sheets use a *consistent, stamped* style set,
which is what makes cluster-by-typography viable.

**⚠ CRITICAL CAVEAT — the six-inch differentiates FEWER classes than 1/2500.**
Note how many feature classes fall to **"Stump"** in the 6-inch column that carry
a *distinct* style at 1/2500 (e.g. banks: 1/2500 R.P. → 6-inch **Stump**; churches
stay R.P. but farms/ferries/fords are Stump at both). **So on the six-inch,
typography reliably separates only a *subset* of types:** prominent
settlements/administrative units (R.C. / Ornamental / caps size-hierarchy), **water
features (I.C.)**, and **antiquities by period (E.C. / Old English / German Text)** —
while the large mass of minor rural features is **undifferentiated Stump** and
**cannot** be told apart by font. Those must be typed from **text** (Tier 0
abbreviations), which is exactly Tier 0's strength. **This is the key finding: Tier
0 (text) and Tier 1 (typography) are complementary, not redundant — typography adds
water/antiquity/prominence that text often can't, and text resolves the Stump mass
that typography can't.**

**Consequence for the plan:** the scheme above is transcribed from the primary
source (P0.5 substantially DONE), so the VLM label set = the OS style names
(R.P./R.C./I.C./E.C./Old-English/German-Text/Stump/Ornamental) and clusters grade
directly against documented feature classes. What remains of P0.5 is transcribing
the full ~300-row OS 404 table into `typesystem/data/gb1900_os_lettering.json`
(feature → 6-inch style) so Tier 1's cluster→type step is a table lookup.

**Note the CAPS rows:** ALLCAPS spans at least five of these classes — the
discriminator is **family (roman vs antique/italic) + size band + letter-spacing**,
which is exactly why ALLCAPS must be resolved *here*, not pre-judged in Tier 0.

**Per-label steps:**
1. **lat/lon → tile + pixel.** Standard slippy-map math (Web Mercator, EPSG:3857):
   `n = 2^z; xtile = (lon+180)/360·n; ytile = (1 - ln(tan φ + sec φ)/π)/2·n`;
   pixel offset = fractional part × 256. Reuse a tiny `deg2num` helper (no such
   helper exists in `processing/helpers.py` today — add one; `h3` is imported
   there but that's a different grid). Choose **z ≈ 16–17** on the NLS six-inch
   layer so glyphs are legible.
2. **Crop an over-sized window** anchored at the label's baseline-left pixel,
   extending **rightward** (reading direction) and a generous margin up/down —
   sized from `len(final_text) × assumed_glyph_width` with a comfortable safety
   factor (we have no true extent, so **over-crop deliberately**). Fetch the 1–4
   covering tiles, stitch, crop.
3. **Re-detect the actual text inside the crop** (so we're not characterizing
   whitespace): a light OCR/text-detector (e.g. Surya — already in the `whg`
   conda env per GOTW `run_pipeline.sh`, or PaddleOCR) gives the tight glyph
   bbox + angle. This *also* yields a confidence that the transcribed
   `final_text` is what's actually there (era/sheet-mismatch guard).
4. **Characterize typography.** Two interchangeable back-ends:
   - **VLM (primary):** send the tight crop to a self-hosted Qwen2.5-VL with a
     strict JSON schema whose **primary field is the documented OS style code**
     (§4.2.0) — `{os_style: RP|RC|IC|EC|OldEnglish|GermanText|Stump|Ornamental,
     case: lower|title|caps|smallcaps, size_band: small|medium|large|extra_large,
     tracking: tight|normal|wide, legible: bool}`. `os_style` maps *directly* to a
     feature class via the OS 404 table (RC/Ornamental→prominent settlement/admin,
     IC→water, EC/OldEnglish/GermanText→antiquity by period, RP→named building),
     so clusters resolve to types by table lookup, not hope. `case`/`size_band`/
     `tracking` corroborate `os_style` and split the size hierarchy *within* caps
     (large RC = town/city vs small = village). **Because ~half of six-inch classes
     are `Stump` (§4.2.0 caveat), a `Stump` verdict means "typography can't tell —
     defer to Tier 0 text"**, not a type; the VLM's value is concentrated on the
     RC/IC/EC/OldEnglish/GermanText minority it *can* separate.
     **`case`, `size_band` and `tracking` are separate fields precisely so an
     ALLCAPS label is decomposed** (caps + family + size + spacing jointly pick the
     type) rather than flattened to one label. `tracking` (letter-spacing) is the
     tell for extended-area features — OS sets seas/ranges/regions in **wide-spaced
     caps**. This mirrors the GOTW schema'd-JSON pattern (§6), with fields chosen
     for OS typography.
   - **Classical CV (cheap alt / ensemble):** slant angle (skew), stroke
     contrast, x-height, cap-ratio, serif detection → a fixed-length feature
     vector. Much cheaper than a VLM at 1.2M scale; can pre-filter so the VLM
     only sees ambiguous crops.
5. **Embed & cluster.** Turn each label's typographic descriptor (VLM fields
   one-hot + CV features, or a small image-encoder embedding of the crop) into a
   vector; **cluster** (HDBSCAN / k-means) into a few hundred groups. The claim
   is that *type style is nearly discrete* on OS maps, so clusters should be
   tight and few.

### 4.3 Tier 2 — type = documented lookup (no human cluster gate)

**SUPERSEDED (SG, 2026-07-17):** the human cluster→type assignment below is **no
longer a gate.** Since the OS style→feature scheme is *documented*
(`gb1900_os_lettering.json`), the VLM emits an `os_style` code and the type is a
**direct table lookup** — plus `vlm_text` as a recorded correction (§11.1). Human
effort collapses to **QA sampling** (spot-check a sample per `os_style`) + the
**downstream user-feedback loop** (§11.2). Clustering is now optional QA (outlier
detection), not a labelling step. The original cluster-review design is retained
below only as the fallback if the VLM can't reliably recover the documented styles
(the P3 pilot decides).

<details><summary>Original Tier-2 (human cluster review) — fallback only</summary>

- Surface each cluster to a human as **a contact sheet of ~25 example crops +
  their `final_text` values + the modal VLM descriptor**.
- Human picks **one type token** for the cluster (or "mixed → split / send back").
- Propagate the token to **every** record in the cluster. This is the whole
  point: **one human decision types thousands of records.**
- Realistically **a few hundred clusters** → a day or two of review, not 1.2M
  decisions.
- Map each chosen token → AAT id(s) in `manual_aat_maps["gb"]` (validate each id
  against the prod `types` index first).

**Review surface:** a static HTML contact-sheet generator (mirrors GOTW's
`process/review_ui.py` / `export_reader.py` approach — GOTW already builds static
review pages), or a Jupyter notebook. No live service needed.

</details>

---

## 5. NLS map tiles (research findings)

### 5.1 The right layer (era match)

- **Use: OS Six-inch to the mile, 2nd edition, 1888–1915** (1:10,560), seamless,
  GB-wide (England & Wales page: `maps.nls.uk/os/6inch-england-and-wales/`;
  Scotland has its own six-inch coverage). This is *the* GB1900 source era.
- MapTiler exposes it as layer id **`uk-osgb10k1888`** (and a seamless "~1900"
  composite `uk-osgb1888`). Projection **EPSG:3857 Web Mercator**, 256px tiles.
- The more legible **25-inch (1:2,500)** exists but is **not seamless GB-wide**
  (NLS: "not published for all areas" / not offered as a seamless MapTiler layer)
  → use it only opportunistically where available; six-inch is the workhorse.

### 5.2 Tile URL scheme — CONFIRMED (2026-07-17)

**NLS six-inch 2nd edition (1888–1915) direct XYZ:**
`https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/{z}/{x}/{y}.png`
(EPSG:3857, 256px PNG; verified serving real tiles over a GB1900 pin at **z15 &
z16**; **max zoom ≥ 16**, z16 legible for typography). CRC reaches this S3 host
directly (no key, unlike MapTiler). Wired as the default in `gb1900_tiles.py`.
Historical detail below retained.

**Host note (tested 2026-07-17):** the `nls-N.tileserver.com` shards mentioned below are
**DEPRECATED** — they now serve a *"maps upgraded → maptiler.com"* placeholder image
(~7 KB) instead of real tiles, so they are useless for bulk fetch. **S3
(`mapseries-tilesets.s3.amazonaws.com`) is the sole free real host** (MapTiler is the paid
alternative, needs a key). S3 is **concurrency-robust** (Amazon-scale, thousands req/s), so
the fetch is **parallelised** — `gb1900_tiles fetch --workers N` runs N concurrent fetchers
with per-tile retry+backoff; the intermittent `Server disconnected` resets are transient and
handled per-tile (they previously killed the single-threaded fetch → stalled the run). This
turns the national fetch from ~days to ~hours and lets full-label VLM coverage be fed.



- **MapTiler (verified template):**
  `https://api.maptiler.com/tiles/uk-osgb10k1888/{z}/{x}/{y}.jpg?key=YOUR_KEY`
  (requires a MapTiler key; Web Mercator; JPG).
- **NLS direct (to confirm from the georeferenced viewer's "XYZ" box):** NLS
  serves georeferenced seamless layers as XYZ/TMS from S3
  (`https://mapseries-tilesets.s3.amazonaws.com/<layer>/{z}/{x}/{y}.png`, and
  older `nls-N.tileserver.com` hosts). **Action for the pilot:** open the
  1888–1915 six-inch seamless layer in `maps.nls.uk/geo/explore/`, click **XYZ**,
  and record the exact template + max zoom. (Prefer the direct NLS XYZ over
  MapTiler for bulk if terms allow — one fewer dependency/key.)
- **Max zoom:** NLS six-inch seamless typically maxes around z16; the 25-inch
  around z18. Confirm per layer at pilot time — typography legibility drives the
  zoom choice.

### 5.3 Licensing & rate constraints (IMPORTANT)

- **Non-commercial / research reuse is permitted with attribution**: *"you must
  display an attribution to the National Library of Scotland, together with a
  link to our website."* WHG is NEH-funded, non-commercial → fits.
- **The six-inch and 25-inch layers are "restricted for commercial purposes"**
  (third-party digitisation contracts). **We must not redistribute the tiles** —
  we fetch, run inference, and use the imagery for our own research processing
  only, not re-serve it. Per the project convention this is a *processing* input,
  not a published asset (cf. `feedback_defer_licensing` — don't gate on licence,
  but **do** respect no-redistribution).
- **Cache policy (SG, 2026-07-17): keep EVERY fetched tile permanently on `/vast`**
  — not just the transient per-label crops. Once a tile is fetched it is retained
  in a durable tile cache (`${IX3_BASE}/gb1900/tiles/`, i.e. `/vast/ishi/gb1900/…`)
  for **potential future research re-use**, so no NLS tile is ever fetched twice
  across this or any later project. This is an **internal research cache**, fully
  compatible with the no-redistribution term (the tiles are never re-served, only
  used for WHG research). Crops derived from tiles are cached alongside. Practical
  upside: the second pass (25-inch opportunistic reads, re-runs, other analyses)
  costs zero NLS traffic.
- **Rate limits / bulk fetch:** no published hard rate limit, but bulk-scraping
  ~1.2M label crops = many tile GETs. **Mitigations:** (a) Tier 0 first slashes
  how many labels need imagery; (b) **tile-level caching** — many labels share a
  tile (six-inch, z16), so dedupe requests to the covering-tile set and cache to
  `/vast`; (c) polite concurrency + backoff; (d) contact NLS for a bulk
  arrangement or, ideally, **fetch tiles once to a local cache** for the counties
  in scope. **Do NOT hammer from many Slurm array tasks in parallel** — stage the
  tile cache first (network fetch), then run GPU inference against the local
  cache (GPU nodes shouldn't be the ones fetching from NLS anyway).

---

## 6. Compute plan (adapting the GOTW VLM pattern)

The GOTW clone at `/home/stephen/PycharmProjects/GOTW` has a **working,
battle-tested CRC VLM pattern** we should copy rather than reinvent.

### 6.1 The reusable GOTW pattern (cited)

- **Model:** `Qwen/Qwen2.5-VL-72B-Instruct-AWQ`, served by **vLLM**
  (`process/submit_vlm_slurm.py:17`, `process/run_pipeline.sh` `stage_vlm`).
- **Serving pattern (`submit_vlm_slurm.py`):** a **GPU Slurm array**; each array
  task starts its **own** `vllm serve` on a unique port
  (`PORT=$((18900 + JOB%700 + T))`), waits for `"Application startup complete"`,
  then runs inference over its **image-index shard** → a **per-shard JSONL**
  (resumable — re-runs skip done work). Header:
  ```
  #SBATCH -M gpu  -p h200  --gres=gpu:1  --cpus-per-task=8  --mem=80G
  #SBATCH --time=04:00:00  --requeue  --array=0-<n_shards-1>
  source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
  conda activate /vast/ishi/envs/vllm      # vLLM env (NOT the whg env)
  module load cuda/12.8.0
  export HF_HOME=/vast/ishi/hf_cache
  ```
- **Inference call (`triage_pages.py`):** OpenAI-compatible
  `POST {VL_BASE}/chat/completions` with a **strict JSON schema**
  (`response_format: json_schema, strict:true` from a pydantic model),
  `temperature:0`, small `max_tokens`, image as base64 `data:image/jpeg` URL,
  **`ThreadPoolExecutor(concurrency≈32)`** (vLLM batches in-flight requests),
  low-res thumbnails (`maxpx≈1024`) for speed, plus a **regex `_salvage()`** for
  Qwen's truncated/whitespace-loop outputs. DB/JSONL is the **resume state**.
- **Orchestration (`run_pipeline.sh`):** submit-and-poll from a login node/tmux
  (`sacct` polling only — no heavy compute on login nodes); each stage is its own
  Slurm job.

### 6.2 Adaptation to THIS repo's conventions

- **GPU submission.** GOTW uses `-M gpu -p h200`. This repo's memory notes say
  htc/a100 needs `sbatch -M htc --account=ishi` and GPU work goes to a100/gpu —
  **use `--account=ishi`** and pick the partition that's actually schedulable
  (`gpu`/`h200` or `htc`/a100). **Never run inference on a CRC login node**
  (`feedback_no_jobs_on_login_nodes`); submit-and-poll only, from `crc0`
  (fall back `crc1/2/3`). Activate conda **before** any `set -u`
  (`reference_crc_slurm_htc_submit`).
- **Paths.** Repo root `/vast/ishi/elastic`; put the tile cache, crops, VLM env,
  and HF cache on **`/vast`** (small-file I/O — the whole reason `/ix1` is
  avoided; see CLAUDE.md). Reuse `/vast/ishi/envs/vllm` and `/vast/ishi/hf_cache`
  that GOTW already provisioned.
- **New scripts (mirror GOTW names):**
  - `processing/gb1900/text_types.py` — Tier 0 (CPU, any host).
  - `processing/gb1900/fetch_tiles.py` — **network** tile fetch → `/vast` cache
    (run on `pitt`/CRC login is OK for *network-bound* fetch per
    `feedback_long_running_hosts`, but throttle for NLS; better as an `htc` CPU
    job). Dedupe to covering-tile set.
  - `processing/gb1900/make_crops.py` — lat/lon→tile/pixel, stitch, over-crop,
    OCR-refine → per-label crop PNG on `/vast` (CPU array; `whg` env has Surya/PIL).
  - `processing/submit_gb1900_vlm_slurm.py` — **copy `submit_vlm_slurm.py`**;
    swap the schema/prompt to the *typography descriptor*; input = crop shards →
    per-shard JSONL.
  - `processing/gb1900/cluster_types.py` — embed descriptors + HDBSCAN → clusters
    + contact sheets.
  - `processing/gb1900/apply_types.py` — write per-record `types[].identifier`
    to the live `gb:` docs (idempotent update-by-query patch, following
    `wikipedia_links_patch.py` / `apply_links_patch.py` — see
    `handoff-wikipedia-sitelinks.md`), then hand off to `apply_aat_enrich`.

### 6.3 Rough scale & cost (order-of-magnitude)

- **1.17M labels.** After Tier 0 (say ~60% typed) → **~470k need imagery.**
- **Tiles:** at six-inch z16, one 256px tile covers a sizeable ground area;
  labels cluster densely per sheet → the covering-tile set is **far** smaller
  than the label count (plausibly low hundreds of thousands of unique tiles,
  many shared). Tile fetch is the network bottleneck, not the GPU.
- **VLM:** GOTW ran Qwen2.5-VL-72B-AWQ on single-GPU array tasks at ~hundreds of
  images/task/hour with concurrency 32. ~470k crops across, say, 8–16 concurrent
  GPU array tasks is on the order of **a few GPU-days** — tractable, and the
  classical-CV pre-filter (§4.2) can cut the VLM share sharply.
- **De-risk with a pilot** before committing (see §8).

---

## 7. Human-in-the-loop

- **Only Tier 2 needs a human**, and only **once per cluster** (~hundreds of
  decisions, not millions).
- Surface: static contact-sheet HTML (per cluster: sample crops + `final_text` +
  modal descriptor) generated on CRC, viewed locally — reuse GOTW's static-review
  approach (`process/review_ui.py`, `export_reader.py`). No server/DB service.
- Output of review: a `gb1900_cluster_types.json` mapping `cluster_id → token`,
  and each `token → [aat_id…]` folded into `manual_aat_maps["gb"]`.
- **AAT mapping** is the same curated step every small authority used
  (`manual_aat_maps.py`): validate each id against the prod `types` index, then
  `apply_aat_enrich --namespace gb --execute`.

---

## 8. Phased milestones

| Phase | Deliverable | Gate |
|---|---|---|
| **P0 — Tier 0 ship** | `gb1900_text_types.py` + OS-abbreviation dict + coverage report on the full 1.17M. Fold high-confidence tokens into `manual_aat_maps["gb"]`; patch live `types[]`; `apply_aat_enrich`. | GB moves from **0% → majority** typed with **zero GPU**. Biggest bang; do first. |
| **P0.5 — Typography ground-truth** *(substantially DONE 2026-07-17)* | The OS scheme is transcribed from the primary source into §4.2.0 (OS 404 1914 per-feature table + antiquity-by-period + the six-inch Stump caveat). Remaining: capture the full ~300-row OS 404 "1/10560 & 6-inch" column into `typesystem/data/gb1900_os_lettering.json` as `{feature → os_style}` and its inverse `{os_style → [feature classes]}`. | The VLM label set (`os_style`) + cluster→type lookup are fixed to the **documented** scheme before any GPU spend; Tier 1 isn't reverse-engineering published conventions. |
| **P1 — NLS tile recon** | Confirm the exact 1888–1915 six-inch **XYZ template + max zoom** from the NLS georef viewer; verify licensing note; fetch a **one-county** tile cache to `/vast`. | Tiles fetchable + legible typography at chosen zoom. |
| **P2 — Crop pilot (one county, ~N=2–5k labels)** | `gb1900_make_crops.py` end-to-end on one county: lat/lon→pixel, over-crop, OCR-refine. Manual eyeball of crop accuracy. | Crops reliably contain the right label despite no box extent. |
| **P3 — VLM + cluster pilot (same county)** | `submit_gb1900_vlm_slurm.py` (GOTW copy) → typography descriptors → cluster → contact sheets → human assigns the county's clusters. | Clusters are tight & few; human assignment is fast; typing agrees with Tier 0 where they overlap. |
| **P4 — Scale-out** | Run tile-fetch → crop → VLM → cluster across all counties (residual only). Global human review of clusters. | ≥90% typed. |
| **P5 — Land** | `gb1900_apply_types.py` patches live `gb:` docs; `manual_aat_maps["gb"]` complete; `apply_aat_enrich --namespace gb --execute`; update `aat-typing-status.md` (drop "the only remaining zero"). | GB has AAT coverage; type facets/filter work for GB. |

**Pilot county suggestion:** somewhere with dense, varied features and good NLS
six-inch coverage (e.g. a Welsh or Scottish county — the project's origin data is
richest there) so both settlement and physical-feature type styles are exercised.

---

## 9. Risks / open questions

- **Crop accuracy without box extent (biggest risk).** We only have the
  baseline-left of the first glyph and no length/size/angle. Mitigation:
  deliberate over-crop + OCR re-detection inside the crop. Curved/rotated labels
  (rivers, coastlines) and very long labels are the hard cases; the OCR-refine
  step must handle rotation.
- **Era / sheet matching.** GB1900 pins came from a specific edition/sheet; the
  NLS seamless "1888–1915" layer is a mosaic of sheets of *slightly* different
  revision dates. Usually fine (both are 2nd-edition six-inch), but a label near
  a sheet seam or from a re-revised area could land on the wrong-vintage raster.
  The OCR-vs-`final_text` agreement check flags these.
- **NLS rate limits / licensing.** No hard published limit but bulk fetch is
  heavy and tiles are **non-redistributable** (commercial restriction). Cache
  crops for processing only; consider contacting NLS for a bulk arrangement;
  throttle. Prefer a one-time county tile cache over live fetching in inference
  jobs.
- **VLM reliability on faint historical type.** 1888–1915 engraving is fine and
  sometimes faint/overprinted; low contrast italic vs roman is exactly the
  distinction we lean on. Mitigations: pick a high-enough zoom; ensemble the VLM
  with classical CV slant/serif features; the human cluster review is the safety
  net (a bad-signal cluster gets caught and split/dropped).
- **Multi-line / overlapping / shared labels.** OS maps stack labels; a crop may
  contain a neighbour's text. OCR-refine + "which glyphs start at the anchor
  pixel" disambiguation.
- **Type granularity vs AAT.** Some tokens map cleanly (church→300007466);
  others are coarse. **Do not shortcut ALLCAPS to one broad token** — as noted in
  §2.2/§4.1.2/§4.2, caps resolves to town / village / parish / boundary / sea /
  range / region / antiquity depending on family+size+tracking, so an
  ALLCAPS-without-a-text-tell label is a Tier-1 case, not a coarse `inhabited
  places` guess. Where a residual genuinely can't be resolved beyond "prominent
  place", coarse-but-correct is acceptable (the AAT hierarchy path-fill still makes
  it facetable) — but that is the fallback, not the ALLCAPS rule.
- **Does typography actually separate types cleanly?** No longer an open
  hypothesis in principle — OS *documented* the style→feature scheme (§4.2.0: OS
  404, the six-inch Conventional Signs plate, Notes on Archaeology 1921), and from
  the 1890s the six-inch used a **standardised stamped** style set, so the signal
  is real and consistent for GB1900's era. The residual risks are **legibility**
  (faint/overprinted engraving) and **the VLM/CV reliably recovering** the
  documented style — both empirical, tested at P3. If style recovery proves too
  noisy, fall back to a VLM *content* classifier (what is this thing?) rather than
  a typography classifier — but the ground truth (P0.5) exists to grade against
  either way.

---

## 10. Output contract (how GB typing lands, precisely)

1. Each `gb:` doc's `types[]` becomes
   `[{identifier: <token>, label: "gb1900", sourceLabel: <rule/cluster tag>}]`
   (replacing today's single generic `{identifier:"named-place", …}` at
   `authorities/gb1900-places.py:101`). Update both the **authority script**
   (future ingests) and the **live index** (a `gb1900_apply_types.py` patch,
   idempotent, mirroring the Wikipedia-sitelinks patch flow).
2. `MANUAL_AAT_MAPS["gb"] = { <token>: [<aat_id>, …], … }` added to
   `processing/manual_aat_maps.py` (all ids validated against the prod `types`
   index).
3. `python -m processing.apply_aat_enrich --namespace gb --es-host <URL> --execute`
   injects `aat_ids`/`aat_paths` on the live docs (`processing/aat_enrich.py`).
4. Re-run after any future `gb` rebuild — the same table drives ingestion's
   `aat_enrich` stage automatically.

---

## 10a. FOLLOW-UP — cost of running the VLM (font + transcription) on ALL entries

**Question (SG, 2026-07-17):** rather than VLM-on-residual, run the VLM over
**every** label — independent typography (`os_style`) **and** transcription
(`vlm_text`) for all ~2.67M pins — giving a fully VLM-verified edition (catches
transcription errors in the 62% Tier-0-typed too, not just the residual). Cost:

**Measured throughput (pilot):** ~**2 crops/sec on 2×A100** (concurrency 16,
Qwen2.5-VL-72B-AWQ). Realistically ~4–5/sec with concurrency 32 + batching (GOTW).

**Estimated cost for ALL 2.67M pins:**
- **VLM compute: ~300–600 A100-GPU-hours** (2×A100 @ 2/sec = ~590 A100-hr worst
  case; concurrency-32 @ ~5/sec ≈ ~300). Wall time ~**10–37 h** across a 16–32-way
  GPU array. (Residual-only ≈ 38% of pins ≈ ~110–220 A100-hr.)
- **Tile fetch (one-time, cached forever on `/vast`): ~1–2M unique z16 tiles**
  (pilot: 500 spread pins → 1,671 tiles; dense areas share heavily) → **~40–80 GB**,
  **~30–40 h** throttled at a polite ~10–15 rps to NLS (resumable). This is the same
  fetch residual-only needs — it doesn't scale with VLM scope.
- **Crops:** CPU, cheap (~a few CPU-hours). **Storage:** tiles + ~2.67M crops
  (~15 KB each ≈ 40 GB) + edition records — all on `/vast`, fine.
- **$**: CRC is the `ishi` academic allocation (fair-share, not $-billed); ~300–600
  A100-hr is a sizeable but feasible ask (GOTW ran comparable).

**Assessment / recommendation:**
- **Feasible.** The dominant cost is ~300–600 A100-hr; the tile fetch is one-time.
- **Marginal value over residual-only is modest for the abbreviation majority:**
  Tier-0 already types those with reliable text (`F.P.`/`W`/`P`), and the VLM
  *neighbour-hijacks* tiny abbreviations (pilot) — so full-ALL both costs ~2.6× more
  and is where the VLM is weakest, **unless** the tight-crop fix reliably isolates
  short labels (needs validation).
- **Recommended path:** (1) VLM on the **residual** first (proper names, its strength);
  (2) run a **stratified QA sample** of Tier-0-typed pins through the VLM to *measure*
  the Tier-0 transcription/type error rate; (3) escalate to **full-ALL only if** that
  error rate justifies the 2.6× cost — full-ALL is the ideal end-state for a fully
  provenance-complete, independently-verified edition (§11), cost-justified, but gate
  it on (a) multi-GPU vLLM reliability solved (TP=1 or requeue), (b) short-label crop
  isolation validated. **Note this as the scale-up decision.**

---

## 10b. Per-label SHEET-PRECISE dating — BUILT + TESTED + WIRED (2026-07-17)

**DONE.** `processing/gb1900/dating.py` joins each label to its OS six-inch 2nd-ed
sheet (point-in-polygon, STRtree) and emits a per-label `timespan`
(survey-start..publication-end, year precision) + full sheet provenance
(surveyed{start,end}, published{start,end}, sheet id, `ambiguous` seam flag). NLS
sheet index staged: **`/vast/ishi/gb1900/sheets/os_6inch_2nd_GB_4326.geojson`**
(16,450 GB ed-2 sheets, WGS84, per-sheet dates from the `nls:OS_6inch_all_find`
WFS; fields `SHEET`/`SUR_STA`-`SUR_END`/`PUB_STA`-`PUB_END`; CC-BY, attribute NLS).
**Validated on the real Hampshire pilot edition: 500/500 dated, 77 seam-ambiguous**
(e.g. `Parkhill` → Hants LXXII.NW surveyed 1895-96 published 1898). Runs
autonomously via a **date-watcher on pitt** that waits for `gb-stamp_edition.jsonl`
and produces `gb-stamp_edition.dated.jsonl`. Original approach + rationale below.



Instead of the dataset-level 1888–1914 span, date each label by the **publication
date of the OS six-inch 2nd-ed sheet it falls on** (the County Series was published
sheet-by-sheet, each with its own survey/revision/publication date). **Confirmed
feasible:** NLS publishes **downloadable GeoJSON metadata for its georeferenced
layers** — sheet polygons + per-sheet dates — (`maps.nls.uk/guides/datasets/`,
Historic Maps API `maps.nls.uk/projects/api/`, `github.com/NationalLibraryOfScotland`).

Path:
1. Fetch the OS six-inch 2nd-ed **sheet index GeoJSON + dates** from NLS.
2. **Point-in-polygon** each label's coord → its sheet (`shapely`/`pyshp`).
3. Emit a per-label `timespan` at year precision — capture **both survey and
   publication** dates where available (survey = when the features were current;
   publication = the map's imprint), with provenance.

**Caveat:** the sheet date describes the **NLS raster** we read (`os/6inchsecond`);
for the small fraction of labels near a re-surveyed sheet seam, GB1900's exact
source sheet could differ slightly — flag those (the same era/sheet-match caveat as
§9). Layers cleanly onto the edition: it upgrades each record's `timespan` from
dataset-level to sheet-precise without touching the type/text pipeline.

---

## 11. Published WHG edition — provenance, versioning, feedback & naming

The end goal is not just to enrich the live `gb:` docs, but to **publish a
standalone, openly-licensed, fully-traceable WHG place-typed edition** of the
British ~1900 map labels, re-derivable and improvable over time. Requirements
(SG, 2026-07-17):

### 11.1 Full provenance — every edit recorded & traceable
Each record is an **append-only, provenance-carrying** object. Never overwrite a
source value; layer derivations on top with their evidence:

```jsonc
{
  "place_id": "gb:<pin_id>",
  "pin_id": "...",
  "source": {"dataset": "gb1900_final_raw_dump_2018", "licence": "CC0",
             "first_transcription": "F.P.", "g_point_wgs": "<ewkb>",
             "classification_count": 3},
  "lon": .., "lat": ..,                     // decoded from source EWKB
  "text": {"value": "F.P.", "source": "raw|vlm|user", "confidence": .., "version": ".."},
  "type": {"token": "footpath", "aat": [300008337],
           "method": "tier0-abbrev|tier1-vlm|user", "rule": "abbrev:F.P.",
           "confidence": .., "version": "gbtype-v1"},
  "bbox": {"px": [...], "geo": [...], "method": "surya-ocr|vlm", "version": ".."},
  "os_style": {"value": "Stump", "method": "tier1-vlm", "confidence": .., "version": ".."},
  "edits": [ {"field": "type", "from": null, "to": "footpath",
              "method": "tier0-abbrev", "version": "gbtype-v1", "ts": "<stamped>"} ]
}
```
- `text` keeps the **original** raw transcription untouched under `source`; any VLM
  or user correction is a *new* layer with its method, so every change is auditable.
- Every derived field (`type`, `bbox`, `os_style`, corrected `text`) records
  **method + confidence + version**; the `edits[]` log is the human-readable trail.
- **Corrections we make (VLM/OCR text reads, §4.2/§4.3) are first-class recorded
  edits**, standing in for the curated set's ~30k manual fixes we forwent.

### 11.2 Versioning — re-runnable as feedback accrues
- **Classification version** (`gbtype-vN`): every full re-derivation bumps it.
  Records carry the version that produced each field, so an edition is a snapshot
  and successive editions are **diffable** (what changed, why).
- **Reproducibility:** Tier-0 is deterministic (dict + rules, both versioned in
  `typesystem/data/`); Tier-1 pins the **VLM model + prompt/schema hash** so a
  re-run is repeatable and a model change is a visible version bump.
- **Feedback loop:** user corrections from the WHG UI (wrong type / wrong text)
  are captured as **high-priority ground truth** with their own provenance
  (anonymised who/when), stored durably, and **override lower-confidence auto-types**
  on the next re-run — never silently lost. Periodic re-runs fold in accumulated
  feedback + dict improvements + better tiles/models.

### 11.3 Publication & naming
- **Licence:** built on **CC0** source, so WHG may publish the derived edition
  under its own choice (recommend an open licence, e.g. CC-BY or CC0). **Not**
  encumbered by CC-BY-SA share-alike (the reason we chose the raw dump).
- **Naming — MUST differ from "GB1900" / "Great Britain 1900"** (both licences
  forbid naming a derivative "GB1900…"). The CC0 README also *welcomes*
  acknowledgment of the "GB1900 project" — so **credit them in docs** (goodwill,
  not required). **CHOSEN NAME (SG, 2026-07-17): `GB-STAMP`** — GB **S**ix-inch
  **T**yped **A**nd **M**apped **P**lacenames (the "Typed" is the distinctive
  place-typing; nods to OS *stamped* lettering). The internal WHG namespace stays
  `gb` (an identifier, not the published product name).

### 11.6 Concurrent pipeline (overlap crop+VLM with the tile fetch)

The national tile fetch is a ~day-long polite single fetch (on pitt). To avoid the
VLM waiting for it, the crop stage overlaps it — **no persistent process runs on a
login node** (SG rule):
- **`processing/gb1900/pipeline.py`** — a **cropper loop on pitt** (long processes
  OK there): finds residual pins whose covering tiles are already cached, crops
  them, and emits fixed-size **batch manifests** (`batches/batch_NNNN.jsonl`) +
  a `processed.txt` state file. Grows with the fetch; resumable.
- **`scripts/gb1900_submit_vlm_batches.sh`** — **one-shot** `sbatch` dispatcher run
  from a login node at checkpoints (submit-and-poll, not a standing driver):
  submits an h200 VLM job per un-dispatched batch (`--array=0-0`, per-batch
  resumable). Idempotent via `.submitted` markers.
- Reconcile (§11.5) folds the per-batch VLM outputs into the edition at the end.
This keeps the network (fetch) and GPU (VLM) busy at once, honours the login-node
rule, and only ever hits NLS through the single throttled fetch.

### 11.4 Storage
The provenance records + detected bboxes live in the durable research cache on
`/vast` alongside the tile cache (§5.3) — `${IX3_BASE}/gb1900/edition/` — so every
edition, edit log, and detected bbox is retained for re-use and audit.

### 11.5 Hint ↔ VLM text reconciliation (the residual-tail policy)

The pilot showed the VLM read is faithful in the large majority but has a small
tail (early-stops, dropped leading chars, occasional over/under-read). The fix is
**not** more prompt-tuning (whack-a-mole) but a reconciliation rule that keeps
**both** readings with provenance and picks a final text conservatively —
defaulting to the **crowd transcription** (verified, often 3+ agreement) and
accepting the VLM only when it is a confident, plausible correction:

`processing/gb1900/reconcile.py :: reconcile(hint, vlm_text)` →
`(final_text, source, rule)`:
- **empty VLM** → hint (`vlm-empty`).
- **letters agree** (case-insensitive) → **hint** text (crowd casing is steadier
  than the VLM's; the VLM inconsistently upper-cases) (`agree`).
- **VLM ⊂ hint** (VLM dropped part) → hint, more complete (`vlm-truncated`).
- **hint ⊂ VLM** (VLM read more) → hint, avoid neighbour over-read (`vlm-overread`).
- **small edit distance** (≤2 and length diff ≤2) → **VLM** — a confident fix
  (`correction`).
- **otherwise** (large divergence) → hint, and **flag for QA** (`divergent`).

Both `hint` and `vlm_text` are always retained under `text` provenance (§11.1) with
the chosen `source` + `rule`, so nothing is lost and a later human/QA pass (or a
better model) can revisit. `os_style`/type always come from the VLM regardless of
which text wins. A cheap code tweak (nudge the anchor ring so it never obscures the
first glyph) should further cut the dropped-leading-char cases at scale.

---

## 12. FOLLOW-UP — untranscribed-text discovery via bbox masking (proposed, SG 2026-07-17)

Once the full-coverage VLM run records a **bounding box per transcribed label** (§4.3 bbox),
we can find the map text GB1900 volunteers **never transcribed** (GB1900 is crowd-sourced and
patchy): for each tile, run a **text DETECTOR**, subtract every known-label bbox, and whatever
text remains is an **untranscribed label** → VLM-read it → a new gazetteer record.

**Why it matters twice:**
1. **Coverage** — extends the gazetteer beyond what volunteers happened to key in.
2. **Boundary-seed densification** — the boundary extraction (`plan-gb1900-parish-extraction.md`)
   stalled because the `Union & R.D. By.`/`C.P.` annotations were *never transcribed* (0 boundary
   labels in the test region). This pass would surface exactly those, giving the dense boundary
   label-seeds that were missing — partly rescuing that dead-end.

**Pipeline:** text-detector over tiles → project known VLM bboxes (fractional *crop* coords →
tile/geo) → subtract → residual text regions → VLM-read → new labels (namespaced distinctly
from `gb:` since they are *our* additions, not GB1900 transcriptions).

**TESTED 2026-07-18** (`/vast/ishi/gb1900/probe/mapreader_text/`): MapReader text-spotting
(MapTextPipeline + Rumsey-finetuned ViTAEv2-S weights) **installs + works on OS six-inch, CPU-only**,
reading real labels at high fidelity (incl. Welsh/diacritics) on 4 disparate tiles (town/rural/
coast/moor). Two findings baked in above: (1) **mask by BBOX OVERLAP, not string-match** — the
spotter emits per-*word* tokens while GB1900 stores whole labels, so a naive text diff hugely
inflates "untranscribed" with fragments (`Bridge`←"Greyfriars Bridge", `Severn`←"Severn Hill
House", adjudicated visually); spatial overlap with a GB1900 bbox = already transcribed. (2) The
spotter and GB1900 are **complementary** — spotter wins on named places, GB1900 wins on tiny
abbreviations (`B.M.`, `F.P.`) and letter-spaced titles (`KINGSLAND`) the spotter misses — so the
detector *augments* GB1900, it doesn't replace it. Genuine untranscribed residual ≈ spot-heights +
some feature labels + boundary annotations.

**Hard part = detection, not reading — but it's largely solved for OUR maps.** We already have a
strong reader (the VLM); the piece to add is *localising* text on a dense sheet (text vs
linework/hachures/contours). This is the historical-map **text-spotting** problem, and there is
proven prior art *on this exact series*: **MapReader** (Living with Machines / maps-as-data) has
integrated a text-spotting pipeline and was applied to **OS six-inch 2nd edition (1887–1949)**,
and **mapKurator** (USC-ISI → UMN) ran text-spotting over **60k+ David Rumsey maps → 100M+ text
labels**. So the detector is *leverage-existing*, not build-from-scratch — and we need only the
**detection** half (recognition is the VLM's job). Caveat: OS six-inch is dense and its text is
abbreviated/curved, so an off-the-shelf spotter likely needs light fine-tuning; the LwM lineage
(models + the OS-six-inch training data) is the place to start. Full-GB pass, incremental on the
infra now in place (all tiles cached, VLM workers, crops). Prereq: the full-coverage bbox run lands first.

### 12.1 Bootstrap a NATIVE OS-six-inch text detector from our bbox dataset (SG 2026-07-17)
The full-coverage run is, as a byproduct, producing a large **GB-wide OS-six-inch text-DETECTION dataset** (boxes from the SPOTTER,
NOT the unreliable VLM bbox — see §12.0) — almost certainly the largest for this series (ICDAR-MapText
training sets are hundreds–thousands of maps, on Rumsey/French maps, *not* OS six-inch). Fine-tuning
a detector on it should beat any off-the-shelf spotter **on our maps** and gives us the §12 detector
natively.

**The make-or-break subtlety — partial labels.** Our boxes cover only *transcribed* labels; the
untranscribed text we want to find is **unlabelled**, i.e. a **false negative** if treated as
background. Naive supervised training would therefore teach the detector to *reproduce GB1900's
blind spots* — the opposite of the goal. Handle it as **positive-unlabelled learning** (boxes =
positives; unlabelled regions = *ignore*, not negative) + an **iterative self-training loop**:
detector proposes candidate text → **VLM confirms/reads** → confirmed boxes join the training set →
retrain → detector improves and surfaces more of what GB1900 missed. Each round also **densifies the
boundary-label seeds** (`plan-gb1900-parish-extraction.md`), so the loop compounds across both projects.

Caveats: VLM bboxes are approximate (fractional *crop* coords → project to tile-pixel + quality-filter
before training); it's a genuine training project (data prep + detector arch + GPU), but the
tiles/GPU/VLM infra is already stood up. Net: a **self-improving OS-six-inch text finder**, seeded
for free by the typing run.

### 12.2 Spotter ↔ crowd transcript reconciliation (SG 2026-07-18)
Two gaps between MapReader-spotter transcripts and GB1900-crowd transcripts, each a distinct fix.
Diagnostic first (measure before building): `processing/gb1900/spotter/gap_diagnostic.py` on the
region set → `gap_report.json` (recall-vs-radius, multiword recovery, text-agreement, spotter-only).

**A. NUMBER gap.**
- *Crowd-only omissions* (~30% of crowd pins had no matching spotter box under strict
  point-in-polygon): partly a **detection miss**, partly a **match-radius artifact** — the crowd pin
  sits at the label *anchor*, offset from the glyphs. Fix: match **nearest box within a radius**
  (recall-vs-radius quantifies artifact vs genuine miss). Genuine misses keep the crowd pin (no
  box/style → typed only via checked abbreviations).
- *Broken multi-word labels*: the spotter emits **one box per word** ("Old","Hall") where the crowd
  has one label ("Old Hall"). Fix: **merge adjacent spotter boxes** by baseline alignment + inter-word
  spacing + consistent height/style, then compare the merged string. (The diagnostic estimates how
  many multiword crowd labels are recoverable by a ≥2-box merge.)
- *Spotter-only*: new labels the crowd never captured (the ~902 word-labels) → new pins, distinct
  namespace from `gb:` (our additions, not GB1900 transcriptions).

**B. TEXT-string gap.** For matched pairs, reconcile by fuzzy similarity → exact / minor-variant /
divergent. **Crowd text authoritative for matched known labels**; spotter+VLM for new labels;
divergences flagged for HITL. Feeds the DuckDB store: `pins.crowd_text`, `pins.spotter_text` (merged),
match_status, text_agreement.

**C. Sheet-edge duplicates (later tidying pass, SG 2026-07-18).** OS sheets overlap at their margins,
so a label near a sheet edge can be detected **twice** (once per adjacent sheet). Detect near-duplicates
by *matching normalised text + proximity within a sheet-overlap tolerance* and **merge into one label,
retaining BOTH coordinate sets** as provenance. Same machinery as the multiword merge. Severity depends
on whether the NLS tileset is a seamless mosaic (each ground point once) or per-sheet with overlap —
**check the tileset first**; this is a cleanup pass after A/B, not a blocker.

### 12.3 Spin-off: GB c.1900 roads GIS (SG note for follow-up, 2026-07-18)
The spotter + font + flat-field infrastructure produces, almost as byproducts, the seed assets for a
**vector road network of Britain c.1900** — a separate deliverable that reuses everything here:
- **Road-name lettering as a road-signal seed.** In built-up areas pure linework tracing fails (roads,
  plots, buildings all similar). But **road names** (the `road_caps` class: small solid caps *between
  parallel casing lines*) unambiguously mark roads and give **location + orientation** (text baseline =
  road direction) + local **width/edges** (the flanking casing). So the road-name detector we're
  building for typing doubles as a **road-seed detector** exactly where tracing is hardest.
- **Erasing lettering ink.** The spotter/detector boxes + flat-field ink/paper separation let us **mask
  and inpaint lettering** out of the sheet → a de-lettered map whose linework is far cleaner for
  centreline/edge extraction (and this also de-noises the boundary extraction, `plan-gb1900-parish-extraction.md`).
- **Casing-line tracing.** We are already modelling road casing synthetically (§ font probe); a casing
  detector + the de-lettered map + road-name seeds → traced, **named** road centrelines.
Net: font-typing, boundary extraction, and a roads GIS are three deliverables off one imagery+spotter+
flat-field stack. This is a recorded direction, not scheduled work.

### 12.4 B′ — REAL-domain recogniser for font-style (the funded next lever, SG 2026-07-18)
Every synthetic approach (VLM→embedding→few-shot→fusion, §0b) plateaued at ~0.17–0.25 on the fine
upright/italic serif axis **because of a domain gap**. B′ removes the gap at its root by training on
REAL data, whose labels are free: **every MapReader crop already carries its transcription.**

- **Data harvest (free, abundant):** `(crop, transcript)` pairs from spotter boxes — region has 8081,
  GB-wide millions. Each crop carries its box → `cap_height_m` (size) + `allcaps` (case). Optionally
  fetch **z17** crops (native ceiling; ~0.7 m/px vs z16's 1.45) so serif/stroke detail is *resolved*,
  not upscaled. **Auto-label** the text-identifiable classes for free: text ends ROAD/STREET/LANE/…→
  `road_caps`; digits→`numeral`; known OS abbreviations→`abbrev`.
- **Model:** a real word-level recogniser (CRNN + CTC) on `(crop→text)`. Real-domain, no synthetic gap.
  Height-normalised input ⇒ the encoder is size-BLIND by construction (as any OCR must be).
- **Size-aware fusion (SG):** size is NOT learned by the encoder — it stays a separate measured axis.
  `type = real-style-embedding × size_band(cap_height_m) × case`. Because every harvested example is
  auto size-tagged, build references as **letter × style × size-band**, and — key — **test style
  separability WITHIN a size band** (size has been a confound; controlling for it may itself lift the
  ceiling).
- **Falls out for free:** (a) a real-domain **font-style embedding** (the encoder) — the thing synthetic
  couldn't give; (b) a better **OS-six-inch OCR** (sharper on abbreviations); (c) the **per-letter
  alphabet** via CTC/attention alignment (SG's "assemble a full alphabet"); (d) feeds the §12.1 native
  detector and §12.3 roads-GIS lettering-erasure.
- **Eval:** same anchor-kNN / per-class on real crops, now with the real-domain encoder + size fusion;
  the test is whether upright/italic serif finally breaks past the ~0.25 ceiling (esp. within-size-band).
- **Coverage curation (SG) — for the full alphabet, once the proof is positive.** The proof trains on
  the NATURAL region distribution (uncurated), so common letters/fonts dominate and rare fonts are
  absent. Font is the unknown, so stratify only where font is known (auto-labels road/numeral/abbrev,
  HITL anchors, embedding buckets); rare fonts need the WIDER multi-region harvest to appear at all.
  Then build a **letter × font × size coverage matrix**, oversample to fill gaps, and pull per-letter
  exemplars via `crnn.per_glyph` (CTC alignment) → a curated alphabet complete across letter×font×size.
- **Cost:** data pairing is already there (cheap); CRNN training is a GPU sub-project; z17 = 4× tile
  fetch (do a region first). Decision A-vs-B′: bank the reliable signals (tier-0 rules + size + case +
  short-mark split) now regardless; B′ is the funded attempt to make fine font-style usable. See
  [[gbstamp_font_typing_pivot]].

### 12.4 RESULT — B' region proof (2026-07-18, gpu job 3277297)
Real-domain CRNN trained on 8,076 region (crop,transcript) pairs (val exact-word ~27%+ climbing — a
working OS-six-inch OCR as a byproduct). Encoder-embedding vs synthetic StyleEncoder, LOO on 275 anchors:

| variant | overall | serif_italic | serif_upright | road_caps |
|---|---|---|---|---|
| synth_only | .410 | .159 | .083 | .30 |
| synth_fusion | .451 | .273 | .167 | .30 |
| crnn_only | .451 | .273 | .167 | **.40** |
| crnn_fusion | **.459** | **.318** | .083 | **.40** |

**Real-domain DID help** — `crnn_only` matches `synth_fusion`, `crnn_fusion` is best, serif_italic
roughly doubled (.16→.32). Validates the domain-gap thesis. **But the core hard axis stays weak:**
upright-vs-italic serif WITHIN a size band (2-class, chance .5) = crnn .58 (small) / .63 (medium) /
.60 (all) — size-control helps a little, but ~.6 on a 2-class task is not usable. **Verdict:** even
the best bet (real-domain, root-cause) gives only a MODEST lift on a region-scale uncurated proof; the
fine upright/italic distinction is approaching a ceiling, not shattering it. Pushing further =
wider multi-region harvest + coverage curation + z17 = real infrastructure for incremental gain.
**Net recommendation: SHIP A** (51.5% high-confidence typing, validated); **use `crnn_fusion` as a
confidence-gated STYLE ENRICHMENT** (best style signal we have — serif_italic .32 usable for
water/feature); the **CRNN OCR is independently valuable** (feeds §12 untranscribed-discovery + §12.3
roads-GIS). Invest in the fine-axis levers only if that distinction is genuinely required.

### 12.5 THE PUSH — full-lever font-style+size drive (SG 2026-07-18, autonomous)
SG: style+size discriminate across many type classes — worth the full push. Levers, all deployed:
1. **z17** (native ceiling; ~0.7 m/px) — real serif/stroke detail, not upscaled. z17 CONFIRMED on NLS S3.
2. **Target rare-font areas by crowd transcript** — 50,099 antiquity labels (Tumulus/Cairn/Camp/
   Earthwork/Cross/Castle/"Site of" — the Gothic antiquities hand); densest 8×8 z16 blocks (top:
   4083,2619 ≈ N.Yorks Wolds) → fetch z17 there (`tiles17/`). Crowd gives coords+text for free →
   crop at those points (no spotter needed) = auto-labelled rare-font candidates.
3. **Wider multi-region harvest** across the targeted blocks + the original region.
4. **Coverage curation** — letter×font×size matrix; per-glyph exemplars (`crnn.per_glyph`).
5. **Auto-labels** — road/numeral/abbrev from text + antiquity-keyword → blackletter weak-label.
6. **Targeted HITL** for the rare fonts once surfaced (prep batch for SG's return).
7. **Stronger real-domain CRNN at z17** (higher input res) + size-fused, anchor-rebalanced, per-size-band eval.
Goal: break the upright/italic + rare-font ceiling that the region-scale z16 proof (§12.4) approached.

### 12.5 RESULT — THE PUSH cracked the rare fonts (2026-07-18, gpu job 3277299)
z17 + antiquity/urban targeting + auto-labels + real-domain CRNN, LOO on z17 anchors:
overall **0.653** (z16 was .459). Rare fonts that were impossible are now nailed:
**blackletter .963** (n=82; was 2 anchors — antiquity targeting on Salisbury Plain/Yorks Wolds +
z17 resolves the Gothic hand), **caps_spaced .980** (n=51; urban targeting), road_caps .60, abbrev
.72, numeral .49. serif_upright up to .25. **SG was right — style+size discriminate many type classes.**
Blackletter→antiquities, spaced-caps→parish/township, etc. now type cleanly. **The ONE holdout:
upright-vs-italic SERIF place-names ~0.63 within-size-band (2-class) — z17 didn't crack this fine axis.**
Auto-label haul: blackletter 227 / caps_spaced 897 / road_caps 1219 z17 crowd crops (free).
Next: anchors for slab/slab_italic/outline/engraved/sans (n≤3); the serif upright/italic axis needs the
per-glyph same-letter route (`crnn.per_glyph`) — the letter-level approach SG favoured.
**PER-GLYPH RESULT (job 3277309, SG instinct CONFIRMED):** glyph-level upright/italic = **.728** vs
word-level **.559** — comparing the SAME letter across fonts (content-controlled) lifts the holdout axis
+.17 (best letters o .64 / t .60 / r .59). Every lever paid off. Remaining work: batch-3 HITL for the
still-unanchored slab/slab_italic/outline/engraved/sans, and fold glyph-level aggregation into typing.

### 12.6 SERIF-axis push — word-semantics is the key (SG's Coppice insight, 2026-07-18)
The upright/italic serif split tracks a SEMANTIC distinction visible in the human labels: named
places/woods (Coppice 4/4, Plantation, Nursery, Wood -> **upright**) vs descriptive/water feature
labels (House, Pond, Lodge, Grange, Ford, Weir, Spring -> **italic**). So SG's "Coppice ≈ serif_upright"
generalises: a word->style LEXICON (VALIDATED on z17 — auto-labels match the real font) both
mass-produces free serif anchors AND gives a semantic feature. Result (serif_push.py, train=lexicon-auto
/ test=HUMAN): **word_semantic_only .706 > crnn_emb_only .647**; the word beats the pixels. (Naive
concat drowned the 4-d word feature in the 512-d embedding -> .647.) With glyph-level at .728 (LOO),
the production serif logic is a **routing ensemble**: word-semantic style where the word is a known
lexicon/keyword (most labels), glyph-level visual for novel proper names. Routing-ensemble LOO (serif_ensemble.py): **word-rule .857 where the word is known** (descriptive/
water feature words -> italic) but only 21% coverage; glyph word-vote .574 on novel names -> ensemble
.618. KEY residual insight: proper names ALSO split — township/village names -> upright (Betton,
Dinthill), farm/house names -> italic (Adcote, Arscott) — a settlement-RANK distinction not in the word
text but CORRELATED WITH SIZE (village labels run larger). So the serif recipe = (1) descriptive-word
lexicon -> italic (large coverage, .86), (2) SIZE for settlement-rank on proper names, (3) glyph-level
visual as tiebreaker. Expand the descriptive lexicon + more z17 -> more coverage. See [[gbstamp_font_typing_pivot]].

## Appendix — key files & commands referenced

- **Production run (as-built, §0a):**
  - Fetch: `processing/gb1900/tiles.py fetch --pins national_typed.jsonl --zoom 16 --workers 16`
    (parallel S3, chunked + retry), run in a shell retry-loop on the pitt VM.
  - Crop (Slurm): `processing/gb1900/crop_shard.py` via `gbcrop.sbatch`
    (`sbatch -M htc --array=0-11 …` `--nshards 12`) — supersedes the single-VM
    `processing/gb1900/pipeline.py`.
  - VLM: `processing/gb1900/vlm_worker.sbatch` (`-M gpu`; a100 `--gres=gpu:2 --export=ALL,TP=2`,
    h200 default TP=1) + `processing/gb1900/vlm_infer.py` (schema now includes **`bbox`**).
  - Reconcile/date: `processing/gb1900/reconcile.py`, `processing/gb1900/dating.py`.
  - County: `processing/gb1900/county_attribution.py` (HCT `hc_county` + uncertainty work-list).
  - CSV export: `processing/gb1900/export_csv.py`.
  - Data on `/vast/ishi/gb1900/`: `edition/national_typed.jsonl` (2.67M tier0), `tiles/16/…`,
    `crops/national/gb_<pin>.png`, `edition/batches/batch_*`, `edition/vlm/<batch>/shard-0.jsonl`.
  - Memory: `crc_gpu_routing_a100_l40s`, `gb1900_tile_fetch_fragility`.
- Authority: `authorities/gb1900-places.py` (docstring §12–29 has the original
  VLM idea; line 25 "SW corner" → correct to "bottom-left of first letter").
- AAT path: `processing/manual_aat_maps.py`, `processing/aat_enrich.py`
  (`augment_doc`), `processing/apply_aat_enrich.py`
  (`--namespace gb --es-host … --execute`).
- Status docs: `developer/aat-typing-status.md` ("ZERO: gb 1.17M"),
  `developer/plan-outstanding-2026-07.md` §2.
- Idempotent live-patch precedent: `developer/handoff-wikipedia-sitelinks.md`
  (`wikipedia_links_patch.py` → `apply_links_patch.py`).
- **GOTW VLM pattern (copy this):** `/home/stephen/PycharmProjects/GOTW/`
  → `process/submit_vlm_slurm.py` (GPU array + per-shard vLLM serve),
  `process/triage_pages.py` (schema'd JSON inference, concurrency, salvage),
  `process/run_pipeline.sh` (submit-and-poll orchestration; env vars
  `VLLM_ENV=/vast/ishi/envs/vllm`, `HF_CACHE=/vast/ishi/hf_cache`,
  `VLM_MODEL=Qwen/Qwen2.5-VL-72B-Instruct-AWQ`), `process/aat_resolve.py` /
  `process/build_aat_shortlist.py` (string→AAT seed).
- **OS lettering ground-truth (§4.2.0):** OS 404 "Character of Writing for
  Ordnance Survey Plans" (1881 & 1914 eds, digitised at NLS); "Conventional Signs
  and Writing Used on the Six Inch Maps" Plate IV in *A description of the large
  scale maps of Great Britain* (1920), NLS `maps.nls.uk/view/128076894`; "Notes on
  Archaeology for Guidance in the Field" (1921, O.G.S. Crawford); "Notes on
  Boundaries" (1914); Richard Oliver, "A few notes on map lettering", *Sheetlines*
  95, 33-36 (Charles Close Society); *Ordnance Survey alphabets* (OS, 1934). NLS
  Characteristic Sheets index: `maps.nls.uk/os/characteristic-sheets/info.html`.
- NLS: layer `uk-osgb10k1888` (six-inch 2nd ed. 1888–1915, EPSG:3857);
  MapTiler template `https://api.maptiler.com/tiles/uk-osgb10k1888/{z}/{x}/{y}.jpg?key=…`;
  confirm the NLS direct XYZ + max zoom from `maps.nls.uk/geo/explore/`; six-inch
  is non-commercial-only + non-redistributable, attribution required.
- CRC conventions: `sbatch -M htc --account=ishi` (or `-M gpu`), conda before
  `set -u`, no jobs on login nodes, everything on `/vast`.
