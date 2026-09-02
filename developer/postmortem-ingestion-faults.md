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
unrelated bugs. They are not. **Nine of them are the same fault**, and the
recurrence is the finding.

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
| 1–3 | `run_ingestion` asked the **live index** whether a namespace already had docs, on a compute node where `es is None` | With ES reachable it would have printed "Skipping wd: 11,455,754 places already exist" for every namespace and made the whole rebuild a **silent no-op**. The `AttributeError` was the *lucky* outcome. A bare `except Exception` in the success path then recorded **eleven correctly-staged namespaces as FAILED**. |
| 4 | `whg` has no local dump (it comes from the DO Django reconcile API) and was absent from `SELF_FETCHING` | "No data files found", **exit 0**. 228,918 documents would have gone missing behind a log line. |
| 6 | `_is_namespace_snapshot_trigger` fell back to `script_id.endswith("-places")`; `un`'s script is `un-countries` | `un` staged its 247 BNDA polygons and **never had `extract` marked completed** — permanently short of the global barrier, absent from the index, and it is the authority the entire corpus prefilters against. |
| 7 | `ukhc-places.py` raised `ImportError` on its own import line since `6fba141` | Nothing catches it: the module is only ever `python -m`'d, never imported, so no test ever loaded it. |
| 12 | `un` is marked `skipped` for ccode because it *supplies* country codes | `final/` is written **only** by `ccode_merge`, so skipping ccode skipped `un`'s `final/` regeneration. Its corrected `h3_cover` sat in `h3_merged` for three days while the index served a stale copy. |
| 14 | **This campaign.** A hand-written sbatch omitted `export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:…"` → `sqlite3` `ImportError` → swallowed by `h3_stage`'s `except Exception: _GeomStoreReader = None` | `cover_geometry_for` fell to `select_h3_cover_geometry(geom, geom.get("hull"))` — **the convex hull instead of the real polygon.** `un`'s cover degraded 376 → 278 cells. Because `un` supplies `contained_in` regions, this **nullified the place#144 fix on its own**, and made the tier-1 ccode prefilter inert for every namespace downstream. |
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
cost. The request looks principled and is wrong.

* **Fault 13 — inherited wall times.** `estimate_wall_time_seconds` medians past
  runs, which is predictive only while the *inputs* are unchanged. The
  BNDA→geoBoundaries move invalidated every stored ccode runtime at a stroke,
  and the stale median killed `clio` and `ohm` at a 01:20:00 wall with their
  work unfinished.
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
  at the 96 G actually requested and **disagree at 64 G, where one says fits and
  the other says OOM after hours on a 13M-doc corpus.**

### The permanent fixes

1. **Stream or spill `_load_patches`.** A whole-file in-memory dict is unbounded
   in a variable no caller reasons about. This is the concrete code change with
   the best cost/benefit in this document.
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
* **My own control failed and I disbelieved it.** A Denver probe returned the
  "wrong" answer and I inferred my test was broken; the test was correct and the
  defect was wider than my hypothesis. **A failing control means the test is
  wrong *or* the defect is bigger than you think** — and the second reading is
  the one that gets skipped.

### The permanent fixes

1. **Every gate must be shown to FAIL on a known-bad input before it is
   trusted.** This is now doctrine for the retile (prove the verifier fails on
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
* **Cite `module.symbol`; treat `:NNN` as a hint.** Of 38 line-number citations
  checked, 26 survived, and **every survivor carried a symbol name**. Line
  numbers rot within a single campaign.
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

Ordered by expected cost avoided, not by effort.

1. **Ban `except Exception` around imports in pipeline stages.** Cause of the
   most expensive fault of this campaign (14) and of Fault 7.
2. **Stream or spill `update_merge._load_patches`** — removes the unbounded
   memory term (Fault 16) that no estimator models.
3. **Resolvers must test content, not existence** — closes Faults 6 and 15 and
   the whole `.exists()` family.
4. **Regenerate `final/` from `h3_merged/` whenever ccode is skipped** — Fault
   12's real fix; currently `_mark_un_skipped` does it inline, which works but
   leaves the misleading FAILED row.
5. **Estimators record and invalidate on their inputs** — Fault 13, Fault 16.
6. **Hoist `_STAGED_SOURCE_PRIORITY`** — after 2.7.
7. **Every gate demonstrated to fail on a known-bad input** before it is relied
   on to pass.
