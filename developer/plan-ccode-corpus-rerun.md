# Plan — Full-Corpus `ccodes` Re-Derivation from UN BNDA

> **Compiled:** 21 July 2026
> **Trigger:** GitHub issue [`place#126`](https://github.com/WorldHistoricalGazetteer/place/issues/126)
> — "IV tagged as both GB and NL". Root-caused to systemic **stale ccode
> contamination**, of which the reported `iv` case (310 docs) is a tiny slice.
> **Goal:** Re-derive `places.ccodes` for the entire corpus from the clean UN
> BNDA boundaries, correcting several million stale errors, **in place, with
> zero ES downtime, within Slurm memory limits.**

---

## 1. Problem statement & root cause

The Atlas *Gazetteers → Explore* facet groups places by `ccodes`. `iv:`
(Index Villaris, 1680 — an authoritatively **all-GB** English gazetteer whose
authority script hardcodes `['GB']`) shows some places under **NL**
(Netherlands, ISO 3166 — *not* Native Land).

**Mechanism (confirmed):**

1. The staged **ccode-enrichment** stage is *authoritative and overwrites*
   source-asserted ccodes via point-in-polygon against the `un` country
   geometries (`processing/ccode_enrichment.py`, docstring §"authoritative").
2. Under the **retired Natural-Earth `un` source**, multi-part countries with
   overseas territories bundled those parts into one feature. The **Netherlands
   feature included its Caribbean territories** (Bonaire/Saba/St-Eustatius), so
   its **convex-hull / envelope spanned the Atlantic** and swallowed points
   across the Channel. The resolver's hull-fallback path
   (`_UnGeometryCache._load` → `entry["hull"]`) then passed point-in-polygon for
   SE-England points.
3. The source has since migrated to **UN BNDA** (`processing/data/
   un_bnda_countries.geojson`). Verified: current prod `un:nld` is
   European-only (bounds `3.36–7.21°E`); its hull does **not** contain the Kent
   points. **The source is fixed — but the already-written ccodes were never
   re-resolved.** `processing/backfill_ccodes.py` only *fills empty* ccodes
   (`must_not exists ccodes`); it never overwrites, so the contamination
   persists untouched in prod (`places_postbarrier-20260502t130000z`).

### 1a. Is "do BNDA polygons overlap?" the right test?

Partially. It detects *one* failure mode — two different countries covering the
same ground → double-tagging. **BNDA passes it** (audit below). But it is **not**
the mechanism here: our errors came from **stale data + hull/envelope
over-assignment**, which happens regardless of whether any polygons overlap. The
*direct* correctness test is **re-resolve every doc's `repr_point` against the
clean BNDA index (exact per-part `intersects`, never hulls) and compare** — which
is exactly what this plan operationalises.

### 2a. BNDA coastline generalisation — snap calibration (measured 21 Jul)

BNDA's coastline is generalised by ~2–3 km, so genuinely-coastal `repr_point`s
can fall just offshore of the polygon and resolve to *no* country. Measured on
the all-GB `iv` gazetteer (24,000 pts) — clears vs snap tolerance, **0 non-GB at
every tolerance** (island nation → no mis-assignment):

| snap | ~km | cleared | GB |
|---|---|---|---|
| 0.01° | 1.1 | 640 (2.7 %) | 23,360 |
| 0.05° | 5.6 | 300 | 23,700 |
| **0.075°** | **8.3** | **~200** | **~23,800** |
| 0.10° | 11 | 140 | 23,860 |

→ **snap fixed at 0.075°.** (The residual ~140–200 clears are IV's genuinely-bad
1680 coordinates / far-offshore points.)

### 1b. BNDA topology audit (21 Jul 2026) — PASS

`processing/data/un_bnda_countries.geojson` (2.4 MB):

| Check | Result |
|---|---|
| Features / distinct ISO2 | 262 / 250 |
| Invalid geometries | **0** |
| Area-overlapping different-country pairs (>1e-6 deg²) | **2** — `IT–SM`, `IT–VA` (legitimate enclaves fully inside Italy) |
| Antimeridian / wide-envelope countries (~360° lon span) | 6 — `AQ, RU, FJ, US, NZ, KI` (+ moderate `CA, GL, CN, ID, SJ, KZ`) |

**Conclusion:** BNDA is a trustworthy authority. The only overlaps are the two
Italian enclaves (points there legitimately resolve to `IT`+`SM`/`VA`). The
wide-envelope countries are exactly why the resolver **must never use hulls** —
`UnCountryIndex` already decomposes MultiPolygons into per-part local envelopes
and tests exact `intersects`, so they are handled correctly.

### 1c. Blast radius (prod `places_postbarrier-20260502t130000z`, 21 Jul)

| Metric | Count |
|---|---|
| `iv` docs tagged NL (the reported issue) | 310 / 24,000 |
| Docs tagged **NL** with `repr_point` **outside** real NL bbox (definite errors) | **740,152** |
| Total docs tagged NL | 1,384,710 → **>53 % of NL tags are wrong** |
| Docs multi-tagged (≥2 ccodes) | 6,308,619 |
| Docs with any ccode | 49,435,665 |
| Docs with `repr_point` but **no** ccode (also in scope — get filled) | 474,642 |

NL is one of ~8 territory-owning countries that produced ocean-spanning hulls in
the old run (structurally also `US, FR, ES, PT, DK, NO, GB, FJ …`), so the true
error count is plausibly **several million**. → **A full-corpus re-run is
merited**; an `iv`-only patch would leave millions of identical errors.

---

## 2. Design decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| **Authority** | UN BNDA via `UnCountryIndex.from_bnda_geojson` | Clean (§1b), self-contained (no geom store / ES), native ISO2, antimeridian-safe, exact per-part `intersects` — never hulls. |
| **Empty-result policy** | **Snap ~8.3 km, then blank** (`snap_tol_deg=0.075`, no fallback to existing) | *SG decision 21 Jul (revised).* Spatial is authoritative; coastal points recovered by unambiguous nearest-country snap; genuinely-offshore points **cleared**. Snap raised from 0.01°→0.075° after measuring BNDA coastline generalisation (§2a): 0.01° would blank ~2.7 % of the all-GB `iv` gazetteer (640/24k real coastal places); 0.075° blanks only genuine bad coordinates. Zero mis-assignment risk at any tolerance — snap **refuses** when ≥2 countries are within tolerance (straits blank, never wrong-side). |
| **Overwrite scope** | Overwrite ccodes for every processed doc, **but emit a patch only when the result differs from current** | Minimises writes → protects ES (see §4). Clearing is expressed as `ccodes: []`. |
| **Resolution basis** | `repr_point` for all docs | `repr_point` is guaranteed *within* the geometry, so it yields the country the feature primarily sits in. Fast, memory-light, exact for the single-country majority. |
| **Cross-border areas** | Accept single (primary) ccode; documented limitation | Re-resolving full area geometry needs a geom-store read per doc (~300/s ceiling). Deferred to an optional refinement pass (§6) keyed on bbox-straddles-border. |
| **`un` namespace** | **Skip** | Self-asserts its own ccode; re-resolving is circular. |
| **Docs without `repr_point`** | Untouched | Cannot be spatially resolved; never blanked. |
| **Cutover** | **In-place scripted `_bulk` updates on the live index** — no new index, no alias swap | Zero downtime; identical to the proven `backfill_ccodes` / `recompute_h3_index` pattern. |

---

## 3. Memory footprint (directly addresses the "memory limits" constraint)

The old `ccode_enrichment.run_ccode_enrichment` needed **24 GiB** (full UN
`h3_cover` index + country polygons resident). **This plan does not use that
path.** `UnCountryIndex.from_bnda_geojson`:

- Input is the **2.4 MB** BNDA GeoJSON — no geom store, no ES, no h3 index.
- Resident set = STRtree over ~few-hundred country *parts* + one `prep()` per
  part ≈ **a few hundred MB**.
- **Slurm `--mem=8G` is generous** (vs the old 24 GiB). Resolve is CPU-bound and
  embarrassingly parallel by input slice.

**ES-side:** `places` carries **no `dense_vector`** (verified: 0 in
`schemas/places.json`) — so the recurring **HNSW-merge OOM risk documented for
the *toponyms* index does not apply here.** Segment churn from 49 M scripted
updates is ordinary geo/keyword merging. Prod heap is already **28 g** (of
62 GB RAM) with ~11 % used — ample headroom.

---

## 4. Keeping ES serving throughout (the other locked constraint)

1. **In-place, no swap.** Updates land on the live index behind the `places`
   alias; the gateway keeps serving the whole time.
2. **Only-changed writes.** The resolve phase compares against each doc's
   *current* ccodes (carried in the export) and emits a patch **only on
   difference** — cutting the apply set from ~49.9 M to (expected) a few
   million, proportionally reducing merge load.
3. **Throttled apply.** Reuse `backfill_ccodes.apply`'s token-bucket
   (`--rps`, default 1500; start at 1000, ramp while watching `_cat/nodes`
   heap % and CPU). `refresh=false`; let background merges proceed gradually.
4. **Off-peak waves.** Apply in slices during low-traffic windows; the job is
   fully resumable/idempotent, so it can be paused between waves.
5. **Watchdog stays armed.** Leave the `es_watchdog` cron enabled (auto-recovers
   a downed prod ES). Do **not** `touch .es_watchdog.disabled`.
6. **No forced forcemerge during hours.** Optionally one `es -forcemerge places`
   in a quiet window *after* apply to reclaim deleted-doc space — not required
   for correctness.
7. **Rollback.** Take an ES snapshot of `places` before apply (`staging_repo`);
   the operation is also trivially re-runnable to a clean state.

---

## 5. Execution phases

All three phases extend the existing, battle-tested
`processing/backfill_ccodes.py` (PIT pagination + throttled `_bulk` already
proven at ~9.87 M docs). New work = an **overwrite/re-resolve mode**.

### Phase 0 — Prep & safety (pitt)
- Confirm BNDA is the committed clean file; re-run the §1b audit script.
- Snapshot `places` (`es -...` / snapshot API → `staging_repo`).
- Confirm heap 28 g, watchdog armed.

### Phase 1 — `export` (pitt, read-only)
- New flag `--all` (or subcommand `export-all`): drop the `must_not exists
  ccodes` filter; target = **all docs with a `geometries.repr_point`**.
- Emit per doc: `{place_id, ccodes (current), geometries:[{geometry_index,
  repr_point}]}`. **Carry current `ccodes`** so resolve can diff.
- Exclude `un:` at the query (prefix `must_not`) — or filter in resolve.
- Sliceable via existing `--slice/--of` PIT scroll. Output to `/vast/ishi/
  ccodefix2/targets.*.jsonl`.

### Phase 2 — `resolve` (Slurm `htc` array, `--mem=8G`)
- Build `UnCountryIndex.from_bnda_geojson(UN_BNDA_COUNTRIES_FILE)` once.
- Per doc: `resolve_ccodes_for_doc_exact(rec, country_index, None,
  snap_tol_deg=0.075)` → `(ccodes, outcome)`.
- **Diff:** compare sorted `ccodes` vs sorted current. Emit
  `{place_id, ccodes}` (possibly `[]` = clear) **only when changed**.
- Array e.g. `--of 64` (~780 k docs/slice); QOS `htc-htc-s` (1 day) is plenty.
  No outbound net needed (self-contained file).

### Phase 2a — **Measure-first gate** (mandatory before any apply)
- Run resolve on **one slice** (~1/64). Report: change rate, clear rate,
  snap-recovery count, and a **sample of 30 diffs** (esp. NL→GB, coastal snaps,
  clears). Sanity: `iv` slice → all `['GB']`; NL-outside-bbox → resolves away.
- **Only proceed to full apply on approval of that sample.**

### Phase 3 — `apply` (pitt, throttled, overwrite mode)
- New `--overwrite` mode. Painless:
  ```
  if (params.ccodes == null) { }                     // no-op safety
  else if (params.ccodes.length == 0) {              // blank
    ctx._source.remove('ccodes');
  } else { ctx._source.ccodes = params.ccodes; }
  ```
- `--rps 1000→1500`, `--batch 500`, `refresh=false`. Waves; monitor heap/CPU.

### Phase 4 — Verify
- Re-run §1c queries: NL-outside-bbox → ~0; NL total ≈ real-NL docs; `iv` →
  24,000 all `['GB']`, 0 NL; multi-tag count drops to legitimate residue
  (borders + IT enclaves).
- Spot-check ~20 known-bad samples (Beachy Point → GB; St Albans(Kent) → GB).
- Confirm Atlas Explore facet corrects (reads live index; no reindex needed).

### Phase 5 — Cleanup (optional, off-peak)
- One `es -forcemerge places` to reclaim deleted-doc space.
- Snapshot the corrected index.

---

## 6. Known limitation & optional follow-up

**Cross-border *areas*** (rivers, mountain ranges, large admin units, some
`osm`/`ohm`/`ukhc` polygons) resolve to their `repr_point`'s single country and
lose legitimate secondary ccodes. This is an acceptable simplification for v1
(the vast majority of docs are points or single-country areas). Optional
refinement: a second pass over docs whose `bounds` straddle a BNDA border,
resolving the **full geometry** via `UnCountryIndex.ccodes_for(area_geom)` (loads
geom-store polygon per doc — slower, but a small subset).

---

## 7. Downstream / no-ops
- **toponyms / clusters:** unaffected — ccodes live only on `places`.
- **gateway / Atlas:** read the live index; the facet self-corrects. No reindex.
- **Idempotency:** the whole pipeline is deterministic and re-runnable; a second
  run over corrected data emits ~0 patches.

---

## 8. Resolved (SG, 21 Jul)
1. **Cross-border areas (§6):** **full-geometry refinement is essential** —
   areas resolve against their full geom-store polygon from the start (built into
   `resolve --overwrite`).
2. **Snap tolerance:** **0.075° (~8.3 km)** (see §2a).
3. **Timing:** no specific window — proceed.
