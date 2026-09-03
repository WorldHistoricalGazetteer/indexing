# Post-mortem — why full re-ingestion keeps going wrong, and the code changes that would stop it

> **Status: living document, opened 2 September 2026, mid-campaign.**
> The completion campaign is still running (`gn` in flight). Entries are added
> as faults are *closed*, not as they are found, so that each one records a
> measured cause rather than a hypothesis. Nothing here is speculative: every
> claim cites the artefact or measurement that established it.
>
> **Companion documents.** [`plan-completion-2026-08-31.md`](plan-completion-2026-08-31.md)
> is the running record of *this* campaign — what was done, in order, with
> numbers. [`plan-temporal-model.md`](plan-temporal-model.md) §10 holds the
> original register, **Faults 1–13**. This document does the thing neither does:
> it groups them into **classes** and names the **permanent code fix** for each
> class, because the register has been growing for two campaigns and the same
> shapes keep recurring under new names.

---

## Why a taxonomy and not just a longer list

Faults 1–13 were recorded individually, each with its own fix. The fixes were
correct and the faults kept coming, because they were being treated as thirteen
unrelated bugs. They are not. **Nine of the thirteen are the same fault**
(1, 2, 3, 4, 6, 7, 9, 11, 12), and **that class is still recruiting: two more
instances in this campaign alone**, for eleven of sixteen. The recurrence, not
any individual fault, is the finding.

> **Faults 14, 15 and 16 are assigned *here*, in this document.** They had no
> numbers before it: this campaign's faults were described in detail across the
> plan and never entered the register. Anyone grepping `Fault 15` will find only
> this file until [`plan-temporal-model.md`](plan-temporal-model.md) §10 adopts
> them. Saying so explicitly, because a document that diagnoses unresolvable
> references should not quietly create three more.

The register is also, as of today, **not enumerated anywhere in one place**.
Faults 1–7 appear as an unnumbered list under one heading; 8–11 have their own
sections; 12–13 sit under "two faults worth carrying forward"; and this
campaign's faults are described in detail but **never numbered into the
register at all**. `grep -rn "Fault [0-9]"` across the repo returns citations
in four documents and three source files with no canonical definition behind
them. That is itself an instance of Class C below — a reference that cannot be
resolved is indistinguishable from one that can, until someone tries.

---

## Class A — absence read as presence

**The single most expensive defect class in this codebase.** A required input
is missing, unreachable, or inapplicable; the code substitutes something
plausible, or does nothing, and **reports success**. Every instance is silent by
construction: the substitute is a valid value of the right type, so no
downstream check can see it.

| # | Instance | The substitution |
|---|----------|------------------|
| 1 | `run_ingestion` asked the **live index** whether a namespace already had docs, before every script, on a compute node where `es is None` | With ES reachable it would have printed "Skipping wd: 11,455,754 places already exist" for every namespace and made the whole rebuild a **silent no-op**. The `AttributeError` was the *lucky* outcome. |
| 2 | The same call in the **success** path (`es.indices.refresh`), swallowed by a bare `except Exception` | `run_ingestion` returned False: **eleven namespaces staged every document correctly and were recorded FAILED.** |
| 3 | A **third** live-index call in the same function, in the closing summary | Same substitution, third site. That one function contained three instances is why the class is worth naming rather than fixing case by case. |
| 4 | `whg` has no local dump (it comes from the DO Django reconcile API) and was absent from `SELF_FETCHING` | "No data files found", **exit 0**. 228,918 documents would have gone missing behind a log line. |
| 6 | `_is_namespace_snapshot_trigger` fell back to `script_id.endswith("-places")`; `un`'s script is `un-countries` | `un` staged its 247 BNDA polygons and **never had `extract` marked completed** — permanently short of the global barrier, absent from the index, and it is the authority the entire corpus prefilters against. |
| 7 | `ukhc-places.py` raised `ImportError` on its own import line since `6fba141` | Nothing catches it: the module is only ever `python -m`'d, never imported, so no test ever loaded it. |
| 9 | `/ix1/ishi/esinfo/es-staging.env` exported `SLURM_JOB_ID` — the **staging job's** id — and is sourced by any job needing staging ES | The consuming job's own id was **silently replaced by a valid-looking one**, so `/scratch/slurm-$SLURM_JOB_ID` addressed another job's scratch. A wrong value of the right type, which is the class's signature. |
| 11 | Every Symphonym cache hit was written with a **null `doc_id`** | ~93% of embeddings **unjoinable, while every stage logged success**. Long-standing committed code, not an operational slip. |
| 12 | `un` is marked `skipped` for ccode because it *supplies* country codes | `final/` is written **only** by `ccode_merge`, so skipping ccode skipped `un`'s `final/` regeneration. Its corrected `h3_cover` sat in `h3_merged` for three days while the index served a stale copy. |
| 14 | **This campaign.** A hand-written sbatch omitted `export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:…"` → `sqlite3` `ImportError` → swallowed by `h3_stage`'s `except Exception: _GeomStoreReader = None` | `cover_geometry_for` fell to `select_h3_cover_geometry(geom, geom.get("hull"))` — **the convex hull instead of the real polygon.** `un`'s cover degraded 376 → 278 cells. Because `un` supplies `contained_in` regions, this **nullified the place#144 fix on its own**, and made the tier-1 ccode prefilter inert for every namespace downstream. |
| 14b | **Fault 14 is not one namespace's accident — measured 2 Sep across the recompute set.** `un`, `nl`, `clio` and `whg` are **one fault in four namespaces**: `clio`'s stored covers are **85% byte-identical to a cover computed from the convex hull** (264 of 309), the same signature that established it for `un:usa`. The competing explanation — antimeridian handling applied to a genuinely-crossing polygon — was **ruled out at 0 of 309** on the codebase's own predicate | The recompute set is a **class, not a list**. One fix, one prevention (`6ad2640` makes the fallback raise), four namespaces |
| 17 | **`publish_hardlinks` never checks that `--db-path` was built by `--run-id`** — verified: `:71` validates **only** `db_path.exists()`, `:91` stamps the marker with whatever `--run-id` says, and `:127-128` silently falls back to `hard_links_{run_id}.sqlite` when `--db-path` is omitted | **Publishes one corpus while recording the provenance of another**, silently, with a plausible marker. Found 2 Sep by S3 *after* using it — it had to pass `--db-path` to reach a `…-postmerge.sqlite`, and the default would have published **an abandoned 248 MB partial from a wedged run** under a clean run id. It escaped only because that file had already been deleted, so the fallback raised `FileNotFoundError` — loudly, which is the right failure, and entirely by luck. **Fix: stamp `run_id` inside the database and check it at publish.** |
| 15 | Every consumer walks `final → h3_merged → … → extract` testing **`.exists()` only** | `open("w")` creates a **zero-byte** file that is *immediately preferred* over the complete earlier stage — no rows at all, silently, for as long as the merge runs. Hours, for `gn` and `wd`. |

### The permanent fixes

1. **Guard the import if you must; the *use site* must raise.** This is the
   fix that landed (`6ad2640`), and the distinction matters. `h3_stage` **still
   carries** `except Exception: _GeomStoreReader = None` — deliberately, since
   the store is genuinely optional at import time. What changed is that
   `cover_geometry_for` now **raises `RuntimeError`** instead of silently
   substituting the hull. **Import-time optionality is fine; use-time
   substitution is the fault.** Audit every `except Exception: x = None` for
   whether its *consumers* degrade silently — that is where the damage is, not
   at the import.
2. **Never resolve a stage by `.exists()`.** A resolver must test *content* —
   a completion marker, a row count, or a rename-on-complete. See Class D.
3. **Existence-not-content resolvers are banned generally.** Fault 6's
   `endswith("-places")` and the `.exists()` chain are the same mistake at
   different layers: a cheap proxy standing in for the real property. Derive
   from the authoritative list (`INGESTION_ORDER`) or measure the real thing.
4. **`exit 0` must mean "the work was done".** Faults 4 and 14 both exited 0.
   A stage that finds nothing to do must say so in a machine-readable way that
   the barrier can distinguish from success.

> **Three instruments failed this way in a single session — 2 Sep — and all
> three would have read as findings:**
>
> | Instrument | Returned | Actually meant |
> |---|---|---|
> | `sacct -M htc -S 2026-09-01T20:00` | empty | the window was parsed in the **host's local time** against UTC-named run ids — off by four hours |
> | `grep -o "\"toponym_id\"" places.jsonl` | `0` on **both** sides | quoting mangled through an ssh heredoc; **matched nothing**, and a zero on both sides reads as *"no change"* |
> | `cardinality` agg on `dataset` over `whg:` | `0` | **there is no `dataset` field** — the schema has `dataset_id`, and a well-formed agg on an absent field is a legitimate, silent zero |
> | `geo_distance` on `geometries.repr_point`, **not wrapped in `nested`** | `0` hits, **no error** | `geometries` is a **nested** field. The identical query wrapped in `nested` returns **1,149**. Verified 2 Sep against the live index |
>
> None errored. Each produced a well-formed, plausible, **wrong** answer of the
> right type — which is the definition of this class, arrived at from the
> observer's side rather than the pipeline's. **A silent filter cannot report an
> empty result as absence**, and the discipline that catches it is the same one
> the code needs: ask what the *broken* world would produce, and if it is the
> same output, the check is decorative.
>
> **Seven instances are now recorded across two campaigns days, in two
> sub-shapes** (the four below from 1 Sep, via the Auditor):
>
> * **A predicate that could never match** — `hullX = 0` for every namespace and
>   `area_ft = 0` across 31, both reading keys absent from the source; `has_geom
>   = None` for all 4,363 `nl` records, read at the document root when the field
>   is nested per-geometry; `FRESH == STORED` at 110,802 cells, where the decoder
>   `continue`d past every document so the identity held between a set and
>   itself.
> * **Addressing that could never resolve** — the three above. **This is the
>   nastier half and it is newer to the record: a well-formed query against a
>   non-existent field is not an error in ES, Slurm or bash. It is a valid empty
>   result, shaped exactly like a true negative.** The predicate cases at least
>   fail over data you control; these fail in the *addressing* layer, where
>   scrutinising the logic does not help.
>
> **One fix covers all seven: every measurement reports what it EXAMINED, not
> only what it found.** `cardinality(dataset) = 0` is a plausible answer; `0`
> beside a matched `doc_count` of 228,918 is a visible contradiction. The gate's
> `0/0` had no denominator on either side; the `sacct` empty set never echoed
> the window it searched.
>
> ⚠️ **The denominator rule alone does NOT catch the seventh instance, and that
> is why it needs a companion clause.** `hullX = 0 of 761 examined` is a
> perfectly well-formed report, **correct in both halves, and still void** —
> because `hull` cannot exist in the source. No denominator reveals that; only
> knowing the field is absent from what you read does. So the rule is two
> clauses: **report the denominator, AND confirm the field can exist in the
> source you read.** The first catches a predicate that never matched; only the
> second catches a field that was never there.
>
> **The measurement analogue of Class A, in S9's formulation — the class's
> second face rather than a process note.** The pipeline fault is *a required
> input is absent, something plausible is substituted, the stage reports
> success.* The measurement fault is exactly: **an absent field, a false
> predicate, a reported zero.** Four instances in one day from S9 alone —
> `hull` read at the wrong stage, `geom_class` at the wrong nesting, `area_ft`
> from both, and `hullX` recorded in §4.14 **as a finding when its author had
> already retracted it.** That last is the most expensive form: a null
> measurement that entered a document as a conclusion and steered triage away
> from a live hypothesis for a day.
>
> **The denominator rule is TWO-SIDED, and we had been applying one side.**
> "0 defective" is meaningless without "of N examined" — that is the numerator's
> N, and it was already doctrine. **The other side is the extrapolation's N:**
> *"~1,565 affected"* is equally meaningless without the frame it multiplies.
> Measured 2 Sep: a `whg` defect rate of 61% sampled from a frame of
> `geom_ref` + multi-cell cover (2,565) was extrapolated against an area-shaped
> denominator (1,248) — **a 2× error in a figure headed for a production
> remediation decision.** The rate was sound; the multiplication was not.
> **A rate needs its denominator, and so does anything you multiply it by.**
>
> ⚠️ **The nested-field case is the most dangerous of the set and it is
> LATENT, not historical.** Querying a `nested` field without a `nested`
> wrapper is **not an error in Elasticsearch** — it matches nothing and returns
> a clean `0`. Measured 2 Sep: `geo_distance` on `geometries.repr_point`
> returned **0 hits with no error**, where the same query wrapped returned
> **1,149**. ✅ **The gateway is clean** — `grep -rn "geo_distance" gateway/
> processing/` returns **nothing**, so this is not a live defect. **It is a trap
> for the next person to write an ad-hoc query or a new spatial feature**, and
> it is the same shape as `geom_class` read at the document root when it is
> nested per-geometry (S9, 4,363 `nl` records) and `h3_cover` assigned to the
> document root by a docstring that was wrong for four months. **Three
> instances, one cause: `geometries` is nested and it does not announce
> itself.** Found incidentally by a session analysing an unrelated dataset,
> which is how latent traps usually surface.
>
> **Third clause: parse, don't grep, when the question is structural.** An
> eighth instance failed in the *opposite* direction from the other seven:
> `grep -o hull` over `gn` returned **2,950**, which reads as "hull survived"
> and would have **refuted a correct structural prediction**; the precise
> pattern `"hull":` returns **0**, the 2,950 being substring matches inside
> place names. **A match count is not a key count.** This is the better
> specimen of the class than any false zero, because a false zero makes you
> miss a defect while a false non-zero makes you abandon a sound argument —
> and **no denominator would have caught it**: `2,950 of 13,454,817 examined`
> is well-formed and entirely misleading.
>
> **The rule now has three clauses, and it was corrected twice in one day, each
> time by the first case the previous version failed on** — report the
> denominator; confirm the field can exist in the source you read; parse rather
> than grep for structure. That progression is worth noticing in itself: it is
> the same discipline this document demands of gates, applied to the rule about
> gates. **A rule adopted is not a rule verified.**
>
> **The tell, when no denominator is available: absence of variance where the
> world has variance.** Thirty-one namespaces of differing character cannot all
> score exactly 0; 4,363 records cannot all be `None`; two independently
> computed cell sets do not agree to the digit. **The signal is not the value,
> it is the missing noise.**
>
> ⚠️ **This has already invalidated a live inference.** Plan §4.14 excludes the
> antimeridian defect for 761 `clio` counter-examples on the strength of
> `hullX = 0` — but `staged_parquet.strip_hull` drops `hull` from every parquet
> sidecar, so any probe downstream of `extract/` reports 0 regardless. The
> exclusion is flagged pending confirmation of which source the probe read.
>
> **This class has a mirror in the operators, including me.** Diagnosing the
> campaign, I twice reported "nothing happened" from an instrument that could
> not support it: `squeue`-empty (a statement about an *instant*) read as
> not-started when a job had already completed, and `sacct -S 2026-09-01T20:00`
> (parsed in the **host's local time**, against UTC-named run ids) read as
> "nothing ran" when it meant "nothing ran inside a window I mis-specified by
> four hours". **A tool that filters silently cannot report an empty result as
> absence.** Same fault, different substrate.

---

## Class B — estimators keyed to the wrong variable

A resource request is derived from a model whose input no longer predicts the
cost. The request looks principled and is wrong. **It misleads in the safe
direction as readily as the dangerous one** — Fault 13 under-provisioned and
killed two jobs; Fault 16 over-provisioned by 3× from two independent routes
that shared one bad anchor. A class defined only by under-provisioning would
have caught one and missed the other.

* **Fault 13 — inherited wall times.** `estimate_wall_time_seconds` medians past
  runs, which is predictive only while the *inputs* are unchanged. The
  BNDA→geoBoundaries move invalidated every stored ccode runtime at a stroke,
  and the stale median killed `clio` and `ohm` at a 01:20:00 wall with their
  work unfinished ([`plan-temporal-model.md:899`](plan-temporal-model.md)).
* **Fault 16 — `update_merge`'s memory request.** `_load_patches` holds the
  **entire** patch file in memory as `dict[place_id → merged_patch]`, lists
  included, before a single document streams. That is a term keyed to **patch
  size**, not corpus size, and nothing in the estimator knows it exists. `wd`'s
  patch is 89 MB / 58,657 rows; **`gn`'s is 1.4 GB / 8,125,650 rows** — the
  GeoNames alternate names the corpus went without until `update_merge` was
  fixed. Measured at **10.2 GiB** for `gn` (marginal slope over 400k→800k real
  rows). Scaling `wd`'s 40.96 GiB peak by corpus size — the published
  derivation — put `gn` at 62.5 GiB, "98% of 64 G, it fits"; the decomposition
  puts it at a 10.2 GiB floor **plus** a peak reaching 48 GiB. Both routes agree
  at the 96 G actually requested and disagree at 64 G — ~~where one says fits and
  the other says OOM after hours on a 13M-doc corpus~~. **❌ Both were ~3× high:
  measured 22.5 GiB (see below).**

> **MEASURED, 2 Sep — and it refutes both estimates, including the one in the
> paragraph above.** `gn`'s `update_merge` completed in **00:07:30** with a peak
> RSS of **23,617,000 K = 22.5 GiB** against the 96 G requested. The ratio route
> predicted 62.5 GiB; the decomposition predicted ~58 GiB. **Both were ~3x
> high.** 64 G — the request the decomposition argued would OOM — would have
> been ample, and so would 32 G.
>
> The 10.2 GiB patch-dict measurement was sound; what neither route modelled is
> that **`wd`'s 40.96 GiB was not dominated by any term that transfers to
> `gn` at all.** `gn` has 139x the patch rows, and finished in **less than half**
> `wd`'s 17:11 at **half** the peak. `wd`'s cost lived in something specific to
> `wd` — most likely the Wikidata geoshapes in `geometries_to_replace`, which
> are large polygon blobs.
>
> **The lesson is sharper than the original entry and cuts both ways:** a peak
> measured on one namespace does not transfer to another without knowing *which
> term produced it*, and this class misleads in the safe direction as readily as
> the dangerous one. Over-asking cost only backfill priority here; the same
> reasoning applied to a wall time would have killed the job.

### The permanent fixes

1. **Stream or spill `_load_patches`.** A whole-file in-memory dict is unbounded
   in a variable no caller reasons about, and **the next namespace's patch is
   not bounded by `gn`'s**. *(The superlative this entry used to carry — "the
   best cost/benefit in this document" — was earned by an OOM prediction that
   measurement refuted. The fix is still right; it is justified by
   unboundedness alone, and it has been moved below the resolver fix in the
   priority list accordingly.)*
2. **An estimator must record the inputs its estimate was keyed to**, and
   invalidate itself when they change — the BNDA→geoBoundaries move should have
   dropped the stored medians automatically.
3. **Floors, not just medians.** Already added to `submit_ccode_slurm` and
   `submit_hardlinks_slurm`. Slurm wall time is a **ceiling, not a
   reservation**: over-asking costs only backfill priority, so the asymmetry
   should be exploited deliberately everywhere, not case by case.

---

## Class C — checks that cannot discriminate

A verification passes, and would have passed had the thing it verifies been
broken. These are worse than no check, because they are cited as evidence.

* **The freshness gate could not see Fault 12.** `un`'s stale `final/` was
  **internally self-consistent**, so a gate testing self-consistency was blind
  to it by construction.
* **`SOURCE_LABEL` is not evidence of which tier ran.** `"un-h3-overlap"` is a
  module-level constant stamped on every output unconditionally. It was read as
  proof that tier 1 had engaged when tier 1 was inert. (Tier 2 is a separate
  program with its own label — the distinction that made the misreading
  possible.)
* **Cardinality is not identity.** `nl`'s `limuw` had **55 cover cells before
  and after** the fix — the hull set and the polygon set happened to be the same
  size while being different sets. **No count test could have caught it**; only
  a set comparison against the store did.
* **Document counts cannot see a name patch.** `update_merge` adds names to
  existing documents, so `count(before) == count(after)` whether or not the
  patch landed. The name count is the only check that discriminates — which is
  why it is mandatory in 2.7 and why "counts match" was nearly accepted as
  sufficient.
* **Fault 8 — an index built from a since-superseded artefact.** The register
  calls it *"the worst fault of the campaign, because the obvious verification
  cannot see it"*: `update_merge` re-ran for `gn`, `wd` and `nl` **after** those
  namespaces had been indexed, so the index was serving pre-patch documents
  while every artefact on disk was correct and every count agreed. **A check
  comparing the index to the staged tree passes; a check comparing *versions*
  is the only one that fails.** This is also the campaign's clearest argument
  for Class D's incumbent-comparison rule.
* **My own control failed and I disbelieved it.** A Denver probe returned the
  "wrong" answer and I inferred my test was broken; the test was correct and the
  defect was wider than my hypothesis. **A failing control means the test is
  wrong *or* the defect is bigger than you think** — and the second reading is
  the one that gets skipped.
* **An instrument that measures the wrong object, and returns a real number
  about it.** 2 Sep, found by indexing-db while building the `/vast` pre-flight
  guard this campaign asked for: `df /vast` reports the **3.9 PB shared VAST
  pool**; `df /vast/ishi` reports our **1 TB project quota**. Both print
  `Mounted on /vast`, same filesystem string, same column layout — the only
  difference is magnitude. A guard pointed at bare `/vast` compared
  **3,364,958 G against a 160 GB floor**, passed cheerfully, and reported a
  healthy free figure while protecting nothing. Audited the repo on that hint:
  `build_geom_index_sqlite.sbatch` logged headroom from bare `/vast` at both
  ends of the job (`d506837`) — informational rather than a guard, so nothing
  failed silently, but anyone reading those lines to judge whether there was
  room saw petabytes on a volume that shares 1 TB with production ES and has
  hit flood-stage read-only before.
  ⚠️ **This is distinct from the zero-returning instrument above and worth
  separating.** That one is *broken* and returns nothing; this one *works
  perfectly* and answers a different question than the one asked. No amount of
  checking that the instrument "ran" or "returned data" can catch it — the
  number is real, well-formed, and about the wrong object. Only knowing the
  expected magnitude catches it, which is why indexing-db caught it: it printed
  the value, saw petabytes, and compared against the 226 GB both of us had been
  quoting all day.
  **Third instance the same day, same shape, different tool:**
  `queryRenderedFeatures()` returned **506 `osm-line-*` features at z10.00 while
  nothing was being rendered** — it reports what is present in the loaded tiles
  and does **not** respect layer zoom ranges. indexing-db nearly reported "lines
  present at z10" on its word; only a screenshot showed the truth. Three
  instruments in one day (`df`, `tile-join --version`, `queryRenderedFeatures`)
  each returning a well-formed answer to a question that had not been asked.
  **The common tell is that the answer was never checked against an expected
  magnitude or an independent modality** — the escape in every case was a second
  observation of a different kind (a remembered figure, an exit code read
  properly, a screenshot).
  ⚠️ **And a DEPENDENCY can be aimed at the wrong object: `afterok:<job>` gates
  on the job's exit status, not on the job's finding.** 2 Sep, #233: the
  planet-scale ocean and inland jobs were chained `afterok` to the job that
  verifies the water filter — correct in intent. That job's verification pass
  completed cleanly (28 min, 145 M+ objects, results written to disk) and the
  job then died at its *reporting* line on a `KeyError` from an unrelated
  in-flight schema change. Exit non-zero, so the gate held — on the wrong
  signal. Had the failure fallen the other way, a job that exits 0 while its
  verification found nothing would have *released* the expensive stages.
  **Gate on the artefact or the assertion, not on the process that was supposed
  to produce it** — indexing-db's own phrasing, which is the entry: *a
  dependency on a job is not a dependency on that job's finding.* Its second
  guard, a runtime refusal reading the recorded relation count, is the correct
  shape precisely because it reads the finding.

  ⚠️ **The class is not confined to instruments — a REMEDY can be aimed at the
  wrong object too, and that form is harder to catch because nothing measures
  it until it is used.** #233's size-pressure fallback said *"drop
  `waterway=riverbank` first — pure saving"*. Measured across the filtered
  planet: **3 ways, planet-wide.** The tag was deprecated in favour of
  `natural=water` + `water=river` and the migration is essentially complete, so
  river areas still ship — they arrive under `natural=water` — and the lever
  grabs nothing. It would have executed cleanly, saved nothing, and left the
  operator concluding their build was broken. **A well-formed remedy answering
  a question nobody asked**, and it survived review by three participants
  because all of us checked whether the *reasoning* was sound and none checked
  whether the *object* still existed. For any remedy phrased as "drop X to save
  space", the reasoning is not the thing to review: **measure X.**

  ⚠️ **Fourth form, and the one that propagates furthest: the SPECIFICATION.**
  2 Sep, all three authored by the coordinator in #233 and all three caught by
  someone else's measurement: `boost-cpp` named as a dependency **that cannot
  install** (deprecated name; hard-fails the solve); `waterway=riverbank` named
  as a size-pressure fallback **that saves nothing** (3 ways planet-wide);
  `w/natural=coastline` naming a population **the control expects to be
  complete** while the `w/` prefix silently excludes 187 relations. Same
  signature every time: **the reasoning is sound and the object reference is
  wrong.** A spec is the worst host for this because everything downstream
  inherits it, and each inheritor reviews the reasoning — which is correct — and
  not the object. **Operational rule: in a specification, every tag, package
  name, path and filter expression is an UNVERIFIED CLAIM until something
  measures it, and the ones that read as background detail are exactly the ones
  nobody checks.**

  ⚠️ **Conditionals are where unverified remedies hide.** indexing-db's own
  diagnosis, ninety minutes after it recorded the remedy form above and called
  it the hard one: it proposed re-running the filter with `r/natural=coastline`
  to close the coastline-relation gap — which would have run cleanly, cost 23
  minutes, produced a larger file and closed nothing, because osmcoastline keys
  on the tag being **on the way**. It knew that; it wrote the ocean job. The
  reason it slipped is the transferable part: **"if outcome 2, then re-filter"
  arrived as contingency planning rather than as a claim, so it got the scrutiny
  of an aside instead of the scrutiny of a decision.** A conditional remedy is
  still a remedy; it is simply not being looked at yet.

  🛑 **Re-reading a specification does NOT catch this, and proposing that as the
  fix is the same error again.** The coordinator's response to the three spec
  defects was to re-read every object reference in the issue. indexing-db's
  correction: of the three, only **one** was caught by a control — `boost-cpp`
  failed at a conda solve, `riverbank` fell out of a census run for another
  purpose, and only the coastline row came from control 1. **Two of three were
  visible only on contact with the system**, and re-reading cannot reach them.
  **The remedy is to EXERCISE each object reference at spec-writing time with
  the cheapest operation that would touch it** — a package name gets a dry-run
  solve, a tag used as a lever gets counted, a filter expression gets run on a
  small extract and reconciled. Not "check the names look right"; "make the
  system answer for each name".
  ⚠️ And doing so is not immune either: exercising the style respecification's
  layer names an hour later, the coordinator probed for a layer `rivers`, got
  ABSENT, and nearly reported a defect — the layer id is `river`, the
  *source-layer* is `rivers`. **The verification instrument used the wrong
  object while checking for wrong objects.**

  ⚠️ **A LIST is the easiest host of all, because the items validate each other
  by association.** #233's respecification named five style layers to repoint at
  the new water source. Four exist in it; the fifth, `ice`, is Antarctic ice
  shelves, whose OSM equivalent (`natural=glacier`) is deliberately outside a
  water-scoped filter. **Executing that list literally would delete a working
  layer under the description of an improvement — and would look like a
  successful edit, because the other four would render correctly.** Every item
  passed the plausible test (`ice` *is* a frozen-at-z7 water-ish layer on the
  `natural_earth` source — true, and the wrong question); none was checked
  against what the replacement actually contains.

  🛑 **The dual of this class: a CORRECT reference to an EMPTY CONTAINER.** Not
  a name that fails to denote — a name that denotes exactly what it says, which
  turns out to hold nothing. 3 Sep, #233, and it invalidated the same decision
  for the second time by a different mechanism. The size-pressure fallback,
  already corrected once from `waterway=riverbank` (dead tag) to "drop the
  `rivers` layer", was verified by checking that `classify()` routes
  `water=river` to a `rivers` layer — **true, and the wrong question.** The
  branch `natural=water → lakes` fired first, so every river polygon on the
  planet landed in `lakes`: **23,140,155 features vs 4,856**, and the `rivers`
  layer the lever targets holds 0.02% of inland water. **Existence of the
  mechanism was checked; occupancy of it was not.** Both the implementer and the
  coordinator verified the reference and neither asked what arrives there.
  ⚠️ Note the common root: the same OSM migration (`waterway=riverbank` →
  `natural=water` + `water=river`) invalidated the fallback **twice by different
  routes**, and the two facts were established in the same session by the same
  person without being connected. **When a fact invalidates one decision, search
  for the others it invalidates** — a migration does not stop at the first thing
  it breaks.
  ✅ **The check that would have caught it is one line: for any lever of the
  form "drop X", measure what is currently in X.** Not that X exists, not that
  the routing to X is correct — the occupancy.

  ✅ **A fourth detection method, empirically the most productive here: when you
  touch something, look at what is next to it.** indexing-db's own accounting —
  two of its four catches came this way rather than from systematic
  verification. `riverbank` fell out of a census run for a different purpose;
  `ice` fell out because it was adjacent in a list it was checking for another
  reason. Adjacency finds what a checklist cannot, because a checklist only
  contains the references someone already thought to write down.

  ⚠️ **In one day this class appeared in an instrument, a remedy, a dependency
  and a specification.** It is not a property of measurement — it is a property
  of *reference*. Anywhere a name stands for a thing, the name can be checked
  for sense and still not denote.

  ✅ **A third detection method, and the cheapest: write the claim out in full
  before believing it.** indexing-db caught the `check-refs` substitution not by
  habit or process but by drafting the sentence "the filter is verified" and
  finding it could not defend the word *verified* — `check-refs` proves a file
  is not missing what it references, which is silent on whether the filter kept
  what the planet holds. **The prose was the instrument.** A conclusion held as
  a fragment ("check-refs clean → good") survives scrutiny that the same
  conclusion written as a complete sentence does not.
  ✅ **The remedy it built is the runtime form of the both-directions rule, and
  should be copied.** Before trusting the guard, it invoked the guard with an
  impossible floor (999999 GB) and **required it to abort**, failing the job if
  it did not:
  `[selftest] PASS: guard aborted on known-bad input, so it can fire.`
  That is a check that proves it can fail, executed *at run time on the real
  instrument*, not argued for in review. Every guard protecting something
  expensive should carry one.

* **A query that returns zero for everything, including the control.** Asked
  2 Sep whether the `osm` corpus held water polygons, I aggregated on
  `types.label` and got **0 for all seven tag keys — including `boundary`,
  which builds the admin tiles and cannot be zero**. The field was wrong
  (`label` is always the literal `"osm"`, the *vocabulary*; the tag key lives in
  `types.sourceLabel` as `natural=bay`), so the filter could match nothing at
  all. Seven uniform zeros would have read as seven findings. **Only a control
  whose answer was known in advance could expose it** — a control that merely
  *fires* would not have, since this one fired on nothing.
  ⚠️ **This is Class C in a different substrate: the instrument, not the gate.**
  A gate that passes everything and a query that returns nothing are the same
  defect — a test whose output is independent of its input — and they take the
  same remedy. Track them as one class, or the instrument case gets rediscovered
  every time, as it was three times in one day.
  ⚠️ **Documentation does not prevent this and only the control does.**
  CLAUDE.md already states, correctly, that `label` *"indicates the source
  vocabulary"*. It was read, and the trap was hit anyway. The reflex remedy —
  "document the field better" — was already in place and did not work.

### The permanent fixes

1. **Every check must be shown to FAIL on known-bad AND to PASS on known-good —
   both directions, and in both substrates: gates *and* the ad-hoc queries used
   as evidence. Proving only the first half is how a check that manufactures
   confidence gets adopted.** For a query this means: include a value whose
   answer you already know, or its zero is unfalsifiable. Measured 2 Sep: the proposed tile span assertion (`max(lon) −
   min(lon) > 180`) **correctly rejected** the known-bad hull at 232.63° — and
   **flagged six legitimate `un` countries** (`ata`/`rus`/`fji` 360.00, `usa`
   358.93, `nzl` 355.47, `kir` 348.57), while normalising the wrap to fix that
   **tightened the bad hull to ~140° and let the real smear through**. It failed
   in **both** directions, and only the good-input half revealed it. **The
   coordinator enforced the fail-half all day and omitted the pass-half**, which
   is how it survived to be recommended. ⚠️ **A heuristic over ambiguous
   geometry lost to a STRUCTURAL check — recording *which tier produced the
   feature* (`007a870`) is unambiguous where the geometry is not.** This is now doctrine for the retile (prove the verifier fails on
   the preserved fixtures first) and should be doctrine everywhere.
2. **Compare sets, not sizes**, wherever a collection is regenerated.
3. **Verify against an independent measure — never the pipeline's own status.**
   This was already the stated lesson of the temporal campaign. It recurred
   anyway, which suggests it needs to be enforced in code (a gate that reads
   `events.jsonl` is not independent of the thing that wrote it) rather than
   restated in prose.

---

## Class D — publication that is not atomic, and never compares

* **The staged merges wrote in place.** `h3_merge.run_h3_merge` and
  `ccode_merge.run_ccode_merge` both `open("w")`ed their output JSONL and
  derived the Parquet afterwards. Neither contained a single `os.replace`,
  `.tmp` or rename — the grep returned **0** for both. Combined with the
  `.exists()` resolver (Fault 15) this is the zero-byte-preferred-over-complete
  failure. **Fixed** by `staged_parquet.atomic_staged_snapshot` (2.8), which
  writes to temps and renames **parquet before jsonl**.
* **Publishes that never compare against the incumbent.** A recurring shape
  rather than a single fault: a new artefact replaces a live one with no
  measurement of the difference. It is why the overlay publish now has a
  cold-readable row-count gate, and why the `un` cover regression survived a
  chain that "succeeded" at every stage.

### The permanent fix

**Rename-on-complete, everywhere, plus a mandatory incumbent comparison at every
publish boundary.** Temp files are invisible to resolvers by construction, so
the correct earlier stage stays authoritative for the whole run and the new
stage appears only when whole — this does not shrink the window, it **removes**
it.

---

## Class F — work the pipeline performs and discards

**Fault 10.** Stage 1's steps 3–4 compute IPA via Epitran and write
`panphon_features` back for 31.9 M records — **~8 h of an ~11 h run — and
nothing in the rebuild reads them.** Not a correctness fault, which is why it
survived: every check it could fail, it passes. It is here because a taxonomy
that only catches wrong answers will never catch this, and it is one of the
largest single line-items in the run's wall-clock cost.

**Permanent fix:** delete it, or give it a consumer. A stage with no reader is
either dead code or a missing dependency, and the pipeline cannot tell you
which — only a person can.

---

## Class G — the check was performed at the wrong end of the pipe

**Distinct from Class A**, and S3's argument for separating them is right: Class
A is *a required input is absent and something plausible is substituted*. This
is *the check was made where the answer isn't*. **All three instances below
passed inspection**, and each was settled only by going and looking at the far
end.

> **A statement about what a WRITER does is not a statement about what a READER
> sees. A producer's guarantee cannot answer a consumer's question.**

| # | checked at the producer | what the consumer actually did |
|---|---|---|
| 18 | `publish_local`'s docstring — *"the gateway's open descriptors against the previous inode stay valid until it re-opens"* | **True about POSIX, false about this consumer, and the docstring could not have known either way.** `hard_link_expansion._connect_ro` opens `file:{path}?mode=ro` **per invocation** (`:171`), closes in a `finally` (`:179`), **no cached connection** — and **no process on the host holds the file open** (every `/proc/*/fd` scanned). The publish was live the instant `os.replace` completed. Three sessions carried the wrong inference for hours and **a production gateway restart was requested on it.** |
| 19 | **permission bits** — `-rw-rw-r--`, group `ishi`, `test -w` passes | Says what the *filesystem claims*, not what a *writer gets*. SQLite raises `attempt to write a readonly database` for an unwritable **journal directory** too. Settled only by taking a real `BEGIN IMMEDIATE` lock and rolling back. |
| 20 | **document counts** before/after `update_merge` | Identical either way — the stage adds **names to existing documents**. What the consumer needs is the **name count** (`gn`: 13,454,817 → 26,460,645). A doc-count check here **cannot fail even in principle.** |

### The permanent fix

**Ask what the CONSUMER does, and go and look — it is always answerable.** In
each case above the answer was one grep or one command away: the reader's
`_connect_ro`, a write lock instead of a `stat`, a name count instead of a row
count. **The producer is the wrong place to ask, however carefully its
guarantee is written** — and a well-written producer docstring is *more*
dangerous here, because it invites the inference it cannot support.

---

## Class E — duplicated logic that drifts

`_STAGED_SOURCE_PRIORITY` is defined **five times, byte-identical**, in
`index_from_stage`, `generate_tiles`, `aat_enrich`, `gazetteer_temporal_extent`
and `hard_links_staged`. They agree today. Nothing makes them agree tomorrow,
and a resolver disagreement between the indexer and the tile generator is
precisely the shape that produces "the index and the tiles disagree and both
report success".

**Permanent fix:** hoist to one module. Deliberately **withheld until 2.7
lands** — it touches every consumer of the staged trees while those trees are
being rewritten — but it should not be dropped once the campaign closes.

**A second instance, and the fix shape worth generalising (2 Sep).** #233's
water control reported `6,123 ways in / 6,721 areas out` — **a ratio above 1,
which is structurally impossible** if a closed way either becomes one area or
fails. The cause was two definitions of "is this a water feature": the counting
script tested `natural=water`, the assembler tested every water tag it accepts.
⚠️ **The tempting fix is to relabel the column. That is the wrong fix** — a
label is a hand-maintained claim that two definitions agree, and it drifts the
moment either side changes. indexing-db instead made the counter **import the
assembler's own `classify()`**, so both sides count the same population *by
construction*. Re-run: **ways 6,721/6,721 = 1.0000** (zero assembly failures)
and **relations 911/911 multipolygon**, with the 20 non-multipolygon relations
correctly producing nothing. The conclusion strengthened as a side-effect —
from "relations are not being dropped" (true, but only decisive against a null
of zero) to a **complete accounting** in which every input corresponds to
exactly one output and nothing is left over in either direction.
**Rule: when two tables must agree, share the predicate, don't document it.**
And: *a ratio above 1 is not surprising, it is impossible* — read it as a
definition mismatch, never as noise.

**And it blocks a fix in Class D, which is the strongest argument for doing
it.** `atomic_staged_snapshot` records a residual it cannot fix by any rename
order: `write_parquet_from_jsonl` strips `hull`, so the JSONL stays canonical
for hull-consumers (`ccode_enrichment`, `generate_tiles`), and a crash between
the two renames leaves the pair disagreeing whichever order is chosen. Making
the **pair** atomic needs a directory-symlink swap — see
[`spec-atomic-stage-directory-swap.md`](spec-atomic-stage-directory-swap.md) —
**which changes an on-disk shape that five resolver copies independently
assume.** So the duplication is not merely a drift risk; it is currently
load-bearing against a known correctness gap. Hoist first, then the swap
becomes a one-site change.

---

## Process findings

These are not code faults, but they cost real time this campaign.

* **Anything that summarises or gates must be re-read whenever the thing it
  summarises is corrected.** (The Auditor's structural finding, adopted.)
  Corrections were reliably filed *adjacent to* the error, while the summary and
  gate positions that depended on them went stale — four separate instances.
* **Corrections propagate inward for free and outward only if someone carries
  them — so staleness concentrates in the most general document.** A finding
  reaches the campaign plan in minutes, because whoever found it is already
  writing there. It reaches the taxonomy one hop later, and the briefing every
  session reads *before doing anything* only if a person deliberately walks it
  out. **No session's remit covers the outer ring**, which is why the document
  with the widest blast radius gets the least scrutiny. Measured on 2 Sep:
  CLAUDE.md still documented `scripts/cluster.sh` (**not in the repository**)
  and three `es` commands with **no dispatcher branches**, its corpus figures
  were pre-rebuild in four places (`~47M` against a measured 51,187,900), and
  `whg` was recorded at `~14.2K` against **228,918** — a 16× understatement of
  the namespace this campaign had just re-ingested. The re-ingestion workflow
  ended by instructing the reader to run a command that no longer exists.
  **Standing fix: when a campaign closes a subsystem or corrects a measured
  figure, its last act is to grep the *outer* ring for it** — CLAUDE.md, then
  any handover — because the inner documents are already right. Two of the eight
  findings would have been caught by grepping one number outward, and the
  severe one by grepping `-cluster` outward when the branches were deleted.
* **Prefer a STRUCTURAL discriminator to a historical one.** To answer *"was
  this artefact rewritten?"* the obvious route is timestamps and job history —
  which is where the UTC/EDT trap, the mis-specified `sacct` window and the
  run-id/mtime mismatch all live. The Auditor's `clio` test answers it with
  **no history at all**: a geom-store shard rewrite is **not per-key
  selective**, so if the suspect keys interleave with keys that are demonstrably
  *not* stale, a rewrite cannot have produced the difference. It did — shards
  `[83, 84]` for both populations, **0 shards holding only the residual** — and
  the hypothesis died in one read. **This generalises to the whole geom store
  and well past this campaign.**
* **Stale-alarming and stale-reassuring are TWO classes needing TWO remedies,
  not one habit needing more diligence.** Both were produced on 2 Sep, on the
  same subject, hours apart — which is the evidence that they are distinct.
  * **Stale-reassuring** ("production is unaffected", held for weeks while
    `clio` served 3,522 wrong covers) is **not self-correcting**: nothing
    prompts anyone to re-check a reassurance. Its remedy is a **scheduled
    outward re-read** — the sweep that found CLAUDE.md documenting a deleted
    subsystem.
  * **Stale-alarming** ("this is a LIVE WRONG ANSWER", still standing two hours
    after the fix, in CLAUDE.md and four plan sites) **is** self-correcting the
    moment anyone checks — so it needs only that **the resolution be
    CO-LOCATED with the alarm.** All five failed on co-location alone: the fix
    existed, was correct, and sat 40–300 lines away.
  * **The remedy is therefore cheap and specific: whoever resolves an alarm
    edits the alarm, not only the log.** One edit at the moment of resolution
    would have caught all five.
  * ⚠️ **And an alarm that outlives its cause is worse than no alarm**, because
    it trains readers to discount the next one — which is expensive in a
    document whose whole purpose is to be believed.
* ⚠️ **A document's AUTHORITY and its FRESHNESS are inversely related unless
  something forces them together — because authority is what stops people
  editing it.** The propagation runs finding → plan → post-mortem → CLAUDE.md →
  **handover**, and the handover is the only one **cited as authoritative by two
  of the others while being downstream of all of them.** Measured 2 Sep: it had
  not been touched since 31 Aug, and its **one-line summary carried both
  staleness directions four lines apart** — "complete and correct in
  production" (false; 5,268 live-defective geometries measured and remediated
  that day) and a gateway defect named as outstanding that had been **fixed on
  the very day the document was last edited** (`4286a0f`). **The most trusted
  document was the least maintained**, which is the asymmetry above at its
  limit: nothing prompts a re-read of a document whose whole purpose is to be
  stable.
* **Answering the questions asked is not sweeping — and a reviewer is as
  exposed to this as an author.** 2 Sep: the Auditor reviewed place#233 against
  three named questions, answered all three correctly and materially, and did
  not notice a stale *"the new z0–14 water source"* left over from before the
  maxzoom decision — a line that **contradicted the acceptance criteria sitting
  beside it**. Its own diagnosis, which generalises: *a reviewer answering
  specific questions is in the same failure mode as a session updating the log
  and not the alarm.* Both do the thing asked; neither re-reads the whole.
  **The sweep is a separate pass from the answer**, and a short list of named
  questions feels like the whole job precisely when it is not. The symmetry is
  the evidence: in the same exchange the coordinator published a fallback
  (*cap ocean at `-z9`*) that contradicted the issue's own central argument, and
  it survived the question-answering pass and was caught only by the deliberate
  sweep afterwards. **Two participants, both auditing for staleness, both
  produced it in the document they were auditing.**
* 🛑 **A correction that does not FOLLOW THE LINKS OUT of the document it
  corrects.** No other rule here covers it, because **sweeping a single document
  cannot find it** — both documents were individually correct and the defect
  lived in the **edge between them.** Two instances, 2 Sep, one signature:
  **the corrected document contained the pointer to its own uncorrected
  source.** CLAUDE.md struck *"the geom store holds 0 `un` geometries"* and in
  the same paragraph sent the reader to handover §3.1 as **required reading**,
  where the original stood unmarked as a trap — with S5 the next session due to
  act on it. And CLAUDE.md's geometry-flags entry cited `schemas/field-notes.md`
  as the authority, where the defect predicate was presented as **complete**,
  illustrated by the one example that hides its blind spot. **Standing fix,
  beside "whoever resolves an alarm edits the alarm": whoever corrects a claim
  greps for what that claim points AT, and for what points at IT — one grep in
  each direction, at the moment of correction.** Both of today's instances would
  have been caught by the outbound half alone.
* 🛑 **A worked example chosen to be CLEAR rather than ADVERSARIAL — the text
  is true, the example is honest, and together they mislead.** This is distinct
  from a wrong claim and needs a different fix. `schemas/field-notes.md`
  documented the `geom_class`/`has_geom` defect predicate and illustrated the
  multi-variant collapse rule with **`MultiPolygon`→`area`** — **the one example
  that hides the predicate's blind spot.** Every word was correct. A reader
  checking the rule against the example comes away confident, and the
  `MultiPoint`→`point` case that defeats it is never suggested. **Fix, and it is
  a documentation habit rather than a code change: illustrate a predicate with
  the case it FAILS, not the case it handles.** (S5's formulation.)
* 🛑 **"Predicate = 0" means the predicate found nothing — NOT that there are no
  defects**, and the difference is invisible once it is written down as a
  cleared row. S5 carried *"place#145 rollout complete, predicate = 0"* as a
  verified all-clear for two days. It was an all-clear **only within a scope
  nobody had stated** — and the predicate was blind in two directions, one of
  them 248-of-248 defective. **An instrument narrower than its reputation,
  reporting success**: the same class as the rest of this document, applied to
  a result that had already been accepted and filed.
* 🛑 **VALIDATING A GATE WITH A SYNTHESISED INPUT VALIDATES YOUR ASSUMPTION, NOT
  REALITY — a ~15-minute production outage, 2 Sep, caused by the coordinator.**
  A `Referer` allowlist was added to the tileserver's nginx config to shed
  hotlinking. The access log showed **13,222 refererless requests against 1,595
  from `https://whgazetteer.org/`**, read as *abuse vs legitimate*. **It was
  backwards:** MapLibre fetches style and tile JSON **without a Referer**, so the
  refererless bucket was mostly *the map itself*. The map vanished from
  production until rollback.
  * **The pass-half was performed and still missed it.** Every allowed origin was
    tested — with **`curl -e`**, synthesising the referer expected. The one
    known-good input that mattered, **a real map page load**, was never tried.
    **"Prove it passes on known-good" means the REAL good input, not a
    reconstruction of it**; otherwise the test confirms the model rather than
    the world.
  * **The 8:1 ratio was the tell and was read as confirmation.** A number that
    large should have prompted *"is my model of this traffic wrong?"* rather than
    *"look how much abuse there is"* — **absence of variance where the world has
    variance**, the same tell recorded elsewhere in this document, missed by its
    author hours later.
  * **What was actually wrong was unrelated and real:** the tileserver bound
    `*:8080`, publicly reachable, bypassing nginx entirely. Closing that needed
    no referer logic. **The genuine finding was fixed alongside a fabricated one,
    and only the fabricated one caused an outage.**
* 🛑 **A relayed authorisation is not the authorisation reaching you — and the
  reason is not distrust.** **From the receiving end, a peer relaying an
  authorisation in good faith is indistinguishable from a peer who is mistaken
  about it** — and 2 Sep produced at least three cases of a session being
  confidently wrong about something it had read correctly. The relay is not
  suspect; it is simply **not verifiable by the recipient**, and the cost of
  waiting is almost always lower than the cost of an unauthorised write.
  **Three sessions reached this independently** — S9 before the 5,268-document
  production write, S3 before publishing the co-reference overlay, S8 in holding
  for a ruling on `wd`-first. That is convergence, not coincidence. **Each was
  right, each cost one round trip, and the coordinator was the one relaying in
  every case.** ⚠️ **Corollary for whoever coordinates: build the escape hatch
  into the instruction.** Every dispatch here carried *"if this reads as me
  expanding your remit, stop and check with SG directly"* — which is why the
  guard fired rather than being overridden by momentum.
* **Cite `module.symbol`; treat `:NNN` as a hint.** Of 38 line-number citations
  checked, 26 survived, and **every survivor carried a symbol name**. Line
  numbers rot within a single campaign.
* **A platform primitive that *declines* is not a busy signal.** Fault 5:
  `_manifest_lock` used `fcntl.flock`, which needs a lock daemon `/vast` refuses
  to provide under burst (`ENOLCK`). **Retrying harder did not help** — `ENOLCK`
  is the daemon declining to serve, not another holder saying wait. The two are
  indistinguishable if you read only "the lock call failed". Replaced with
  `O_CREAT|O_EXCL` plus stale-breaking. Listed under process rather than a
  defect class because the code reported the error correctly and the *reading*
  of it was wrong.
* **An artefact is unreliable if a known-broken run rewrote it** — provenance of
  the artefact, not its location. (Supersedes an over-general rule I had
  proposed, "use the submitter, don't hand-write": `update_merge` has no
  submitter, so the rule was unfollowable as stated.)
* **A guard expected to fire belongs *before* submission.** `submit_ccode_slurm`
  marks `un` skipped, does the pass-through inline, and *then* submits an array
  task that `ccode_enrichment` correctly refuses — leaving **`un ccode FAILED,
  exit 1`** in `sacct` for work that succeeded. A later auditor either chases it
  or, worse, distrusts `un`'s `final/` — the one artefact this campaign spent
  days establishing you *can* trust.

---

## Prioritised code changes for the next run

Ordered by expected cost avoided, not by effort. **Every item carries a SITE and
a DONE-CONDITION**, because this document outlives the sessions that wrote it and
a conclusion without an acceptance criterion is a memoir, not a fix.

**Already landed — listed so nobody re-does them:**

* ✅ **Every `INGESTION_ORDER` script is import-tested** — Fault 7's real fix,
  done by `tests/test_authority_imports.py`.
* ✅ **`atomic_staged_snapshot`** — the four staged merges write to temps and
  rename (parquet first). `554e43a` + `e37c93b`, 17/17.
* ✅ **`cover_geometry_for` raises** instead of substituting the hull —
  `6ad2640`. This is Fault 14's prevention.
* ✅ **`publish_gate`** refuses a tileset the build never read geometry for —
  `007a870`. **A heuristic over ambiguous geometry lost to a structural check:
  recording *which tier produced the feature* is unambiguous where the geometry
  is not.**

**Outstanding:**

1. **Audit the 156 `except Exception → None | pass` sites for whether their
   *consumers* degrade silently — the use site must raise.**
   *Site: 156 handlers repo-wide (AST-enumerated 2 Sep); densest are
   `processing/helpers.py` (9), `processing/generate_tiles.py` (8),
   `gateway/spatial.py` (7), `phonetics/extraction/rebuild_toponyms_index.py`
   (6). Done when each is classified raise / degrade-deliberately / unreachable,
   and every "degrade" has a comment saying what the caller sees.*
   Fault 14's actual fix — `6ad2640` deliberately **kept** the import guard, so
   "ban the guard" would not have prevented it.
   <sub>*(Correction retained: an earlier draft said "ban `except Exception`
   around imports", contradicting Class A and mis-citing both supporting faults.
   Left visible because it is this document's own process finding happening
   inside it on its first day.)*</sub>

2. **Resolvers must test content, not existence.**
   *Site: the six stage-resolver chains. Done when a stage with a zero-byte
   `places.jsonl` is NOT preferred over a complete earlier stage — test it with
   exactly that input.*
   Closes Faults 6 and 15. Promoted above the memory fix because Fault 15 is the
   zero-byte-preferred-over-complete failure this campaign actually had to fix.

3. **Stream or spill `update_merge._load_patches`.**
   *Site: `processing/update_merge.py:114`. Done when a `gn`-sized patch
   (1.4 GB / 8.1 M rows) merges with peak RSS bounded independent of patch size
   — measure it, do not assume it.*
   An unbounded term keyed to patch size that no estimator models. Justified by
   unboundedness, **not** by the refuted OOM prediction.

4. **Regenerate `final/` from `h3_merged/` whenever ccode is skipped, and fire
   the guard BEFORE submission.**
   *Site: `processing.submit_ccode_slurm._mark_un_skipped` +
   `ccode_enrichment:859`. Done when a skipped-ccode namespace ends with
   `final/` newer than `h3_merged/` **and** leaves no FAILED row in `sacct`.*
   Fault 12's real fix; the inline pass-through works but plants
   `un ccode FAILED exit 1` for work that succeeded.

5. **`reconcile_stage_status`'s default sweep must promote `update_merge`.**
   *Site: `processing/staging_orchestrator.py`, `STAGE_ARTEFACTS`. Done when a
   default reconcile promotes a namespace whose `update_merged/` artefact is
   complete on disk — today only `--stage update_merge` does.*
   Found by S8, 2 Sep. **This is the campaign's signature class inside the
   reconciler built to catch it**: complete artefacts on disk, namespace left
   deferred at the barrier.

6. **Hoist `_STAGED_SOURCE_PRIORITY` — but it MUST take the chain as a
   parameter.**
   *Site: `index_from_stage`, `generate_tiles`, `aat_enrich`,
   `gazetteer_temporal_extent`, `hard_links_staged`, `rebuild_toponyms_index`,
   `h3_stage`, `index_namespace`. Done once no session is writing staged trees
   (**not** "after 2.7" — that gate expires).*
   🛑 **The six chains are NOT the same chain.** Four are byte-identical;
   `rebuild_toponyms_index` **omits `update_merged`**; `h3_stage` tests a
   **directory**; and `index_namespace` uses `(final, ccode_merged, h3_merged,
   extract)` where **`ccode_merged` is written by nothing** and
   `boundary_merged`/`update_merged` are omitted. **Writing one shared constant
   would silently break `index_namespace` and `rebuild_toponyms_index`.** It
   also unblocks the directory-swap fix for Class D's residual.

7. **Estimators record the inputs they were keyed to, and invalidate when those
   change.**
   *Site: `estimate_wall_time_seconds` and
   `staging_orchestrator.array_memory_gb:77`. Done when the
   BNDA→geoBoundaries-class input change drops the stored medians
   automatically, rather than a human noticing.*
   Faults 13 and 16.

8. **Order-of-operations guard on re-runs.** ⚠️ **Least specified item here — it
   needs a design before it needs code.**
   *Site: undetermined. Done when an index records which artefact version it was
   built from, and a re-run of that artefact marks the index stale.*
   Fault 8: a stage re-running *after* the index was built from its output
   leaves the index stale with nothing to see.

9. 🛑 **DECISION FIRST, then code: delete or consume the toponym vocabulary
   work.** *This is SG's call, not an engineering one* — it is an either/or and
   a successor cannot execute a disjunction.
   *Site: stage 1 steps 3–4, `panphon_features` for 31.9 M records. Done when
   either the stage is removed or a consumer exists.*
   Fault 10: **~8 h of an ~11 h run that nothing reads.** ⚠️ 2.6 is deprecated
   (no retrain planned), which makes "delete" the live default.

10. **Every gate demonstrated to FAIL on known-bad AND to PASS on known-good**
    before it is relied on.
    *Site: every new check. Done when both demonstrations are recorded beside
    the check.*
    ⚠️ **The fail-half alone is how a check that manufactures confidence gets
    adopted** — the tile span assertion passed the fail-half and flagged six
    legitimate `un` countries.
    🛑 **And this rule was already in the plan, correctly, on 31 August** —
    *"a guard that cannot say PASS is as useless as one that cannot say FAIL"* —
    **2,500 lines from the doctrine it should have governed. The document did
    not lack the rule; it lacked the connection between the rule and its own
    doctrine.** That is not a lesson the span failure had to teach; it is one
    already present and never generalised.
