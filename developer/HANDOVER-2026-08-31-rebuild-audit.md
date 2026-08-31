# Audit — the July/August re-ingestion, what landed and what is left

**Written 2026-08-31.** Everything below was measured today against the live
indices, the live geom store, the live tileserver and the deployed tilesets —
never against a manifest status or a plan's own claim. Sources audited:
`plan-temporal-model.md` §10, `plan-atlas-data-architecture.md` §8,
`HANDOVER-2026-08-09-geom-store.md`.

**One-line summary:** the re-ingestion itself is complete and correct in
production. The *publication* half is not: a partial retile on 7 August ran
against the geom store while it was destroyed, and **nine gazetteer boundary
layers are on the live map today as points with no polygons**. A second,
unrelated defect turned up while verifying that one: the gateway answers an
*unresolvable* containment scope globally instead of failing closed (§2b).

---

## 1. Completed — verified today

| area | evidence |
|---|---|
| `places` rebuilt and promoted | alias `places` → `places_h3ccode-20260805t120000z`, 51,187,900 docs — exactly the 6 Aug figure |
| `toponyms` rebuilt and promoted | alias `toponyms` → `toponyms_temporal-20260731t160000z`, 72,703,741 docs |
| **Symphonym embeddings** | **72,703,741 / 72,703,741 = 100.0%** — no backfill outstanding. Sampled `ukhc` names added on 20 Aug: real 128-d int8 vectors (112/128 non-zero), so the names-only refresh path embeds correctly too |
| ccodes | 49,731,096 (97.15%, was 73.90%) |
| AAT types on places | 51,014,923 (99.7%) |
| `gn` alternate names (the `update_merge` loss) | recovered — `gn:2643743` (London) carries 185 toponyms; 17,210,516 toponyms attest `gn` |
| `wd` links / geoshapes | 3,093,094 docs with links; 51,094 areal geometries |
| toponyms infix-ngram mapping (place#127) | retained through the rebuild — `name.{keyword,ngram,prefix,raw}` |
| hard-link overlay | `/ix1/ishi/hardlinks/hard_links.sqlite`, 1.33 GB, 6 Aug — the 7.6 M-row rebuild is published |
| registry inventory + licences | pushed 6 Aug (67/67 batches prod + dev, 26 licences verified) |
| old index generations | `places_postbarrier-20260502…` and `toponyms_ngram-20260722` **deleted** — the ~75 GB reclaim happened |
| geom store | live at `/vast/ishi/geom`, **11,758,768** rows in `index.sqlite`, two backups on `/ix1`. Holds every polygon the affected tilesets need (see §2) |
| retile #1 + `tileboss` style | 27 tilesets deployed 7 Aug; deployed `style.json` carries the `["has","label"]` filter on 10/10 label layers, matching `tileboss` `origin/production` |

The user's two specific worries are therefore both closed: **the Symphonym
backfill is done**, and **tilesets were regenerated** — but see §2 for how.

---

## 2. ⚠️ THE MAIN DEFECT — nine boundary layers are points on the live map

The 7 August retile happened in **two waves**, and the second ran after the geom
store had been destroyed:

* **Wave 1** (deployed 11:02–11:09) — `osm`, `osm_misc`, `ohm`, `gn`, `wd`,
  `tgn`, `nl`, `un`. Healthy store, polygons intact, label anchors present.
  Built *before* `c5c209c`, so they carry the **old collapsed temporal stamps**
  and no `start_def` / `end_def`.
* **Wave 2** (deployed 16:35–16:37) — the 19 per-namespace gazetteer buckets,
  rebuilt to pick up `c5c209c`. Their job logs read
  `geom-store: opened /vast/ishi/geom/index.sqlite (2 entries, sqlite backend)`
  and every bucket streamed `poly=0`. `ohm` failed outright
  (`ERROR: no tileset produced`), `osm` / `osm_misc` / `tgn` were cancelled at
  12:37:55 — but the gazetteer buckets "succeeded" and were deployed.

Decoded from the deployed `.mbtiles` today (first 60 tiles each):

| tileset | polygons before | polygons now |
|---|---:|---:|
| `clio` | 12,704 | **0** |
| `kain_par` | ~23,177 | **0** |
| `po` | 7,815 | **0** |
| `vob_lgd` | 9,765 | **0** |
| `vob_rd` | 4,418 | **0** |
| `hgis` | 892 | **0** |
| `vob_rc` | 385 | **0** |
| `vob_cty` | 369 | **0** |
| `ukhc` | 92 | **0** |

(`un` and `nl` still render polygons — wave 1. `alc`, `chgis`, `dgsd`, `dp`,
`gb`, `iv`, `ofs`, `og`, `pl`, `tm`, `gn`, `tgn` are point gazetteers and lost
nothing.)

The same emptiness cost wave 2 its label anchors, so **place#159 phase 2 is not
actually deployed** either, despite the plan recording all 27 rebuilt with
anchors — the verification only sampled `ohm`.

**The geom store now holds all of it** (`clio` 15,690, `kain_par` 23,177,
`po` 7,815, `vob_lgd` 9,765, `vob_rd` 4,418, `hgis` 892, `vob_rc` 385,
`vob_cty` 369, `ukhc` 92), so a retile fixes it with no re-extraction.

---

## 2b. Second defect, found in passing — an unresolvable scope answers unscoped

Not a rebuild fault; a gateway one, and a silent-wrong-answer class. `CLAUDE.md`
and place#144 both state that a scope which cannot be applied **fails closed**,
and that the response's `scope` object records what was applied. Neither holds:

```
POST /api/search {"query":"Paris","contained_in":["un:not_a_real_place"],
                  "containment":"fuzzy","relation":"intersects"}
  → scope: null | 3 hits — Paris (TR), Paris (GA), Paris (GA)
```

A client with a typo'd or stale place id gets a confident *global* answer that
looks scoped. The containment engine itself is fine — the same request with the
real id returns correctly:

```
POST /api/search {"query":"Paris","contained_in":["un:fra"], …}
  → 1 hit, tgn:8723013, ccodes ["FR"]
```

Note `scope` is `null` in **both** responses, so even the successful case fails
to report what it applied. Two things to fix: fail closed when
`resolve_region` returns nothing for every requested id, and populate `scope`.

(Checked while testing whether the store's missing `un` polygons had broken
country-scoped search. They have not — `resolve_region` borrows a `sameAs`
co-referent's polygon, so `un:fra` scopes correctly today. That is the only
reason `un`'s absence from the store is invisible to search.)

> ⚠️ **That parenthesis is wrong, and was corrected by measurement on 31 August
> (S2, step 2.1).** It is true of `containment=fuzzy`, which works off the
> `h3_cover` in ES and never opens the store. `containment=exact` is a different
> path, and `apply_containment`'s own docstring says what it does: *"if the
> region geometry could not be loaded, exact silently degrades to the fuzzy
> test."* So for as long as the store lacked `un`, **every country-scoped exact
> query was answering fuzzily** — live and wrong, not latent.
>
> The same request before the merge and after the gateway restart:
>
> ```
> contained_in:["un:fra"], containment=fuzzy -> 15 hits   (both times)
> contained_in:["un:fra"], containment=exact -> 15 hits   BEFORE  <- identical to fuzzy
>                                            ->  4 hits   AFTER
> ```
>
> The 11 that dropped are Swiss-only (`wd:Q71 Geneva ['CH']`, `gn:2660646`,
> `tgn:7007279` …); the 4 that survive are exactly the `['CH','FR']` cross-border
> features that really do intersect France — so the post-merge answer is the
> correct one, not merely a different one. Both responses reported
> `scope: {applied: true, mode: "polygon"}` throughout: nothing in the API ever
> said the constraint had been weakened.
>
> This is the same shape of fault as §2's retile — a component reporting success
> for work it had not done — and it generalises past `un`. **A namespace whose
> geometries are missing from the store does not fail exact containment; it
> quietly downgrades it.** The `whg` case S3 found on the same day is the same
> class: 2,320 areal and linear shapes that never reached the store, so any
> `contained_in` scoped to a contributed dataset degraded identically. So "which
> namespaces claim `has_geom` but hold no store keys" is a **search-correctness**
> question, not only a tile-generation one.

---

## 3. What remains — by area (see §4 for the sequenced plan)

### 3.1 Retile all 27 buckets — one operation, fixes §2 and the deferred work

```bash
python -m processing.submit_tiles_slurm --run-id h3ccode-20260805T120000Z
```

It settles four things at once: restores the nine lost boundary layers; gives
the eight wave-1 buckets the `start_def`/`end_def` props they lack (the reason
the Atlas date filter is still switched off); publishes the label anchors; and
puts `clio`'s 2,986 newly-addressable polygons on the map for the first time
(§3 of the 9 Aug handover — a visible change to that layer, worth a look).

Three preconditions, all of them traps:

1. **⚠️ EXCLUDE `un`, or restore its geometries first.** The store holds
   **0** `un` geometries (SG's open decision, §3.4). The deployed `un` tileset
   still has real polygons from wave 1 — retiling it today would replace the
   country boundaries with points, repeating exactly the §2 failure.
2. **Set `TILE_ES_DOC_NAMESPACES=gn,wd`** — their staged trees are still test
   stubs (6.5 KB / 14 KB); `71bcc39` makes the builder read those two from the
   places index instead.
3. **Assert a non-zero polygon count per bucket before deploying**, and restart
   the tileserver promptly afterwards (it holds descriptors on the old inodes;
   last push hit 99% disk. It is at 83% / 8.5 GB free now).

### 3.2 whg3 — the two-mode `setFilter` on the map layers

The last client-side piece of place#176; a few lines, spelled out in the issue.
Pointless before 3.1, since the props it filters on only exist afterwards.

### 3.3 Re-ingest the `whg` namespace — its place ids disagree with the website

`f835b26` (18 Aug) mints `whg:<dataset>:<src_id>`; production was indexed on
6 Aug and still carries `whg:<dataset>:<WHG place key>`. Confirmed against the
v3 clone: prod holds `whg:1052:6954931`, whose `src_id` is `8`. So WHG's own
reconciliation service and the index name the same place differently — which is
what the commit was written to stop. ~229 k docs, one namespace.

Measured while planning the re-ingest: the id is also a join key in the hard-link
overlay, where **10,732 of 13,466 distinct `whg:` endpoints (79.7%) already
dangle** — `clustering/harvest/contributor_replay.py` mints ids for datasets that
ingestion never accepted (89 referenced, 48 in the index). Both problems are
handled by the emitted id map in `plan-completion-2026-08-31.md` §2.3.

### 3.4 `un` — still awaiting SG's decision

Unchanged from the 9 Aug handover §2: 247 geometries absent from the store,
untouched because of the geoBoundaries-vs-BNDA question. Now blocks a clean
retile (§3.1 precondition 1), so it is no longer cost-free to defer.

### 3.5 Housekeeping — ~145 GB, no risk

| item | size | note |
|---|---:|---|
| `/ix1/ishi/DELETABLE-AFTER-2026-08-31--geom-broken` | **57 GB** | SG asked for this "as soon as practical"; the name's deadline is **today** |
| `/vast/ishi/geom_rebuild/staging` + `staging_pending` | 22 GB | redundant since the merge |
| `/vast/ishi/tiles-verify` | 17 GB | 7 Aug scratch incl. the ohm band `.geojsonl` |
| `/ix1/ishi/data/tiles/_step0` | 2.7 GB | place#160 diagnostic; keep until 3.1 verifies, then delete |
| stale `*.geojsonl` in `/ix1/ishi/data/tiles` | ~20 GB | incl. `osm_admin.*` from the May 2025 pre-rename era |
| `places_temporal-20260731t160000z` | 23 GB | the rollback index; retire once 3.1 is verified |

### 3.6 Pipeline debt — before the *next* rebuild, not before the retile

* **Fault 12 is still unfixed in code.** `submit_ccode_slurm._mark_un_skipped`
  still only marks `un`'s ccode stages `skipped`; nothing regenerates `final/`
  from `h3_merged`, so `un` will again index a stale `h3_cover`. It was fixed by
  hand for this run only. (Fault 13, the wall-time floors, **is** committed.)
* **`gn` / `wd` staged trees are stubs** (6.5 KB / 14 KB — in fact **one row
  each**), collateral from the `unittest discover` accident, **and `nl` is missing
  entirely** — no directory at all, against 4,363 `nl` places in prod (S4's
  census, 31 Aug). `nl` **belongs with this accident after all** — I first
  recorded it as older, on the 9 August handover's "already missing before any of
  this", then found the tile log `tiles-ns-10756173_*.out` of **2026-08-07T01:21**
  reading `nl → nl: 4,363 features` from a staged tree that has since vanished.
  The builder reads staging, and `TILE_ES_DOC_NAMESPACES` did not exist until
  8 August, so `staged/nl` was intact on 7 August. Together they mean a staged-corpus read today sees **26,269,329 of
  51,188,772 places, 51.3%**, and reports success. `wd`'s re-run extract exists at
  `/vast/ishi/staged_geomrebuild/wd` (9.7 GB) and can be promoted rather than
  re-run; `gn` needs a re-extract. Staging is the pipeline's canonical input, so
  the next rebuild regresses both without this.
* **`toponyms-temporal-20260731T160000Z.db` has no `ipa` / `panphon_features`** —
  ⚠️ **not because stage 1 timed out.** This audit inherited that from
  `plan-temporal-model.md` §10 without checking it, and S4 measured it on 31 Aug:
  the columns are skipped **by design**. The preserved sbatch ends
  `--training-namespaces _none_`, an unmatched sentinel `submit_batch9_slurm`
  passes by default unless `--for-retrain` is given (`ef31016`, 2 May: *"IPA +
  PanPhon are training-only artefacts"*). Both stage-1 jobs COMPLETED inside a
  3:41 wall; the 12 h TIMEOUT was a *separate* job, `whg-toponyms-rerun` on
  4 August, a backfill that reached 87.7% before being killed. Nothing in the
  search stack reads these columns; the next **Symphonym training** run does.
  Allow ~9 h and raise the wall — for the rerun's reason, not stage 1's.
* `geom_store --merge` still has no prune step for keys absent from the current
  corpus, and `authorities/backfill_admin_levels.py` still has its broken
  `BOUNDARIES_INDEX` import.

### 3.7 Data residuals — recorded, none blocking

* **`og`'s 251 of 6,260 geometries is the sources' ceiling, not a fault**
  (measured after the first draft of this audit said otherwise). The extract log
  reads `ofs point index: 16,296 places → 1,123 admin keys`, so the ofs input was
  complete; og simply has 6,260 admin units and ofs attests only 1,123 of them,
  and a hull can only be computed where settlements attest the unit. The `wd`
  fallback contributed **0** because **no og doc carries a wd link at all**
  (0 / 6,260 in the staged extract) — so raising og's coverage is a *reconciliation*
  task (establish og↔wd links), not a pipeline fix. Its 3.9% ccode coverage is a
  consequence, not an independent problem.
* **The 2,569 `has_geom=false` docs are now diagnosed** (31 Aug, by S3 and
  confirmed here) — they are not a diffuse "incomplete ingestion" tail but two
  named authority bugs, and the geometry was **never written, not lost**:

  | | area | line | cause |
  |---|---:|---:|---|
  | `whg` | 1,248 | 1,072 | `whg-places.py` passes `geom_key` to `enrich_geometry` but never calls `configure_module_writer`, and `enrich_geometry` writes only when a module writer is configured. **Fixed inside 2.3** |
  | `og` | 249 | 0 | `ottgaz-places.py` calls `enrich_geometry(geo, timespans=ts)` with **no `geom_key` at all**, so its computed hulls are never keyed for the store. Not yet fixed |
  | corpus | 1,497 | 1,072 | those two are the whole of it |

  Consequence beyond the count: a polygon in neither the store nor the index is
  unservable and untileable, so `og`'s 249 hulls cannot render even where they
  exist. Plus 1 areal doc with no `h3_cover`, unrelated.

  The check that generalises: `grep -L configure_module_writer` across the
  authorities that compute areal geometry. 14 configure a writer; `ottgaz` is the
  one that computes geometry and does not.
* Out of this campaign's scope but still the standing tail: AAT coverage
  4,436/15,448 = 28.7% (place#142), and the legacy per-dataset contributed
  tilesets (`whg-*.mbtiles`, 23 July) awaiting migration
  (`plan-outstanding-2026-07.md` §8).

---

## 4. Ordered plan

See **[`plan-completion-2026-08-31.md`](plan-completion-2026-08-31.md)** — the
sequenced version of §3, ordered on SG's steer of 31 August: *the tilesets are
consumed only by the Beta-gated Atlas UI, so getting everything updated properly
and stable comes first, and the retile comes last.*
