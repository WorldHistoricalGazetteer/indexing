# Plan — temporal model: attestations, uncertainty bounds, and per-source encoding

> **Status:** proposed, not started.
> **Written:** 30 July 2026 — split out of `plan-atlas-data-architecture.md` §1.6, which had grown
> to a third of that document while being a different programme with a different owner.
> **Issue:** [place#164](https://github.com/WorldHistoricalGazetteer/place/issues/164)
> **Blocks:** `plan-atlas-data-architecture.md` §5.1 (temporal filter on Atlas map layers) — and
> nothing else in that plan.
> **Repos:** `indexing` (ingestion, ES mapping, reader), `whg3` (Date Range UI, query modes)
> **Runs FIRST**, and hands over to `plan-atlas-data-architecture.md` §8 when re-ingestion
> completes — see **§9**. Do not schedule a retile from this plan.
>
> **In one line:** `schemas/places.json` gives every temporal endpoint `in` / `earliest` /
> `latest`; ingestion uses `in` almost exclusively, so *attestations* ("recorded alive in 2025")
> are stored as *lifespans* ("existed only in 2025"), which makes any historical date filter
> exclude them.

---

## 1. The defect: attestations are being stored as lifespans

`schemas/places.json` gives every temporal endpoint three sub-fields — `in`, `earliest`,
`latest` — under both `start` and `end`. Ingestion uses `in` almost exclusively, and that single
choice is what makes the dates unusable.

| encoding | claim |
|---|---|
| `start.in = Y`, `end.in = Y` | the place existed **only** in year Y |
| `start.latest = Y`, `end.earliest = Y` | started **no later than** Y, ended **no earlier than** Y — i.e. **attested alive at Y** |

Same two numbers, opposite meanings. A source that records places *as they were* at some moment
— OSM's 2025 dump, Index Villaris in 1680 — is making the second claim and we are storing the
first.

The queries follow directly, with **absent** outer bounds doing the work:

```
definitely alive at Q :  start.latest <= Q <= end.earliest
possibly  alive at Q :  (start.earliest ?? -inf) <= Q <= (end.latest ?? +inf)
```

An OSM boundary encoded properly is not *definitely* alive in 1500, but **is** *possibly* alive,
because `start.earliest` is absent and therefore unbounded. **This dissolves the Atlas plan's §1.5 problem
without any convention hack**: no `end = 9999`, no "+Contemporary" toggle, no year heuristic. It
also removes the over-claim in `end = 9999`, which asserts "still alive today" — something a 2025
snapshot does not license either.

## 2. Three defects, not one

1. **Storage** — attestations written as `in`/`in` point lifespans (below).
2. **The reader flattens regardless.** `_iter_year_ints`
   (`gazetteer_temporal_extent.py:136`) yields *every* integer under an endpoint and
   `_collect_extent_for_doc` takes `min()` under `start`, `max()` under `end`. Feed it the
   **correct** attestation encoding and it returns `(2025, 2025)` — the identical wrong pair. So
   fixing storage alone changes nothing; `doc_temporal_range` must stop collapsing, or split:
   envelope semantics for the registry aggregate, four bounds for per-feature filtering.
3. **Granular values that do exist are unreadable.** 208,937 `whg` docs carry
   `{"start": {"earliest": "2022"}, "end": {"latest": "2022"}}` — **strings**, and **none** of
   them carry `in`. `_iter_year_ints` states "strings are not coerced", so `doc_temporal_range`
   returns `(None, None)`: those places are computed as **undated** — no tile temporal props, no
   range-mode filtering, absent from their datasets' registry `temporal_extent` — while the dates
   sit in ES in plain sight. Compounding this, the live index maps
   `geometries.timespans.start.earliest` and `end.latest` as **`text`** where
   `schemas/places.json` declares `integer`, so those fields cannot even be range-queried.

## 3. Per-source reading (sampled from prod ES, 30 July 2026)

| source | stored now | correct reading | class |
|---|---|---|---|
| `osm` | `start.in 2025 / end.in 2025` (8,861,802 docs) | attested 2025 → `start.latest 2025 / end.earliest 2025` | **A** |
| `tgn`, `nl` | `start.in 2025 / end.in 2025` | attested 2025 | **A** |
| `iv` | `start.in 1680 / end.in 1680` | attested 1680 | **A** |
| `gb` | `start.in 1888 / end.in 1914` | attested *somewhere in* 1888–1914 → `start.latest 1914 / end.earliest 1888` (definite core legitimately empty) | **A** |
| `ofs` | `start.in 1830 / end.in 1849` | register compilation window, as `gb` | **A** |
| `alc` | `start.in 1786 / end.in 1789` | publication window, as `gb` | **A** |
| `un` | `start.latest 2025`, no end | **already the right idiom**; add `end.earliest 2025` | **A** |
| `pl` | `start.in 300 / end.in 640` (toponyms) | attested during a named period; re-ingest preserves *which* periods, and multiple discrete ones | **A→B** |
| `tm` | `start.in 246 / end.in 249` (toponyms) | attested in documents dated 246–249 | **A→B** |
| `po` | `start.in -49 / end.in -29` (toponyms) | PeriodO's model is **natively fuzzy** — each of start and stop carries its own earliest/latest range. We kept two numbers of four. | **B** |
| `ohm` | genuine `start_date` / `end_date` tags | real lifespans — **`in` is correct** | **C** |
| `clio` | `1738–1740`, `1799–1799` | polity lifespans | **C** |
| `hgis` | `1701–1808` | admin-unit lifespans | **C** |
| `ukhc` | `start null / end.in 1974` | **add `start.latest 1974`** (or 1542 for the Welsh counties, where the real start is known) — see the closure rule below | **A** |
| `kain_par` | `end.in 1851` only | **add `start.latest 1851`** by the same rule | **A** |
| `vob_*` | `1911–1921`, `1921–1931` … | consecutive census snapshots per `G_UNIT` — encode all four bounds from the snapshot sequence (below) | **A** |
| `wd` | mixed (`start.in 2013 / end null`, often absent) | **precision is being discarded**; P585 is written as a point lifespan | **A→B** |

**Closure rule — `start.latest` defaults to `end.earliest`.** Anything that ended must have
started no later than it ended, so a known end always licenses a `start.latest`. This is not
cosmetic: the *definitely alive* test requires an upper bound on `start`, so without it a record
with only an end **can never be definitely alive at any year** — which is plainly wrong for a
historic county that existed in 1973. Apply wherever a real start is unknown; prefer the real
start where it is known (`ukhc` Welsh counties, 1542).

**`vob_*` — the snapshot sequence gives all four bounds.** A `1911–1921` pair should not assert
anything about 1915; the census attests **1911**. But the 1921 endpoint is not noise either — it
bounds when the configuration can have ended. Taking the previous and next snapshots for the same
`G_UNIT`:

```
start.earliest = previous snapshot year (where that snapshot differs)
start.latest   = this snapshot year        ┐ the attestation
end.earliest   = this snapshot year        ┘
end.latest     = next snapshot year (where that snapshot differs)
```

→ *definitely* alive in 1911, *possibly* alive 1901–1921. Strictly more informative than either a
`1911–1921` lifespan (which over-claims 1915) or a bare 1911 attestation (which discards the
bounding information). Where consecutive snapshots are identical they are two attestations and the
bounds widen accordingly. This is the four-field model earning its keep.

**Class A — backfillable in place.** The correct values are a deterministic function of what is
already stored; no source file is needed. A `_bulk` rewrite (**not** `_update_by_query`, which
re-runs the ingest pipeline — see the geom-store note in the repo's field notes).

**Class B — needs re-ingestion, and should be scheduled rather than deferred.** The source carried
structure that extraction discarded, so no in-place transform can recover it. `po` is the clear
case: PeriodO's fuzzy start/stop ranges are exactly the four numbers this model wants, and we kept
two — in a gazetteer whose entire subject matter *is* temporal extent. `pl` and `tm` are class A by
mechanical transform but gain real fidelity from re-ingestion (per-period attestations rather than
a collapsed union).

There is no cost argument for deferring these: `po` = 9,003 records, `pl` = 25,561, `tm` = 64,196
— hours of work, against `osm`'s 8.86 M docs of class-A backfill. Re-ingest them alongside the
class-A pass, not "when next touched".

`wd` is A→B: the P585 and closure fixes are mechanical, but recovering **precision** (below)
requires re-reading the dump.

**Class C — leave alone.** Genuine lifespans, correctly using `in`. Not everything is an
attestation, which is why this must be a **per-source interpretation** rather than a global rule.

**Class ? — needs the source owner's judgement** before either treatment.

## 4. Wikidata already has the convention — we are discarding it

Wikidata's time datatype carries a **`precision`** code alongside the timestamp
(`11` day · `10` month · `9` year · `8` decade · `7` century · `6` millennium, plus coarser
geological steps), and the value may also carry `before` / `after` offsets in units of that
precision. On top of that sit qualifier properties that map almost one-to-one onto our model:
**P1319** earliest date, **P1326** latest date, **P1317** floruit, **P1249** time of earliest
written record, and **P1480** sourcing circumstances = `Q5727902` ("circa").

`authorities/wikidata-places.py` reads `claim["mainsnak"]["datavalue"]["value"]["time"]`
(`:178`, `:192`) and **never reads `["precision"]`**. So a century-precision inception
(`+1200-…`, precision 7) becomes `start.in = 1200` — "began exactly in 1200" — where Wikidata
said "some time in the 12th century". And `:227` writes

```python
return [{"start": {"in": point}, "end": {"in": point}}]   # P585 point in time
```

which is the attestation-as-lifespan error stated directly in code.

Proposed mapping:

| Wikidata | our encoding |
|---|---|
| P571 inception / P580 start time, precision 9 | `start.in` |
| …precision 8 / 7 / 6 | `start.earliest` / `start.latest` = the decade / century / millennium bounds |
| P576 dissolved / P582 end time | `end.*`, same precision rules |
| **P585 point in time** | attestation → `start.latest = Y`, `end.earliest = Y` |
| P1249 earliest written record | attestation → `start.latest = Y` |
| P1317 floruit | attestation window |
| P1319 earliest date / P1326 latest date | `start.earliest` / `start.latest` directly |
| P1480 = circa | widen the bounds by one precision unit |

*(Precision codes recalled from the Wikibase datamodel — worth confirming against the dump before
implementing; 9/10/11 and 7/8 are the ones that matter in practice.)*

## 5. Refresh the source dumps before any re-ingestion — and the sequencing that implies

Ages on CRC as at 30 July 2026:

| source | dump | age | size |
|---|---|---|---|
| `wd` | `wd/latest-all.json.gz` | **8.2 months** (2025-11-23) | 150 GB |
| `gn` | `gn/allCountries.zip` + `alternateNamesV2.zip` | **8.2 months** (2025-11-22) | 415 MB + 198 MB |
| `tgn` | `tgn/explicit.zip` | 7.4 months (2025-12-18) | 1.16 GB |
| `osm` | `osm/planet-latest.osm.pbf` | 3.8 months (2026-04-06) | 92 GB |
| `ohm` | `ohm/planet-latest.osm.pbf` | 3.8 months (2026-04-06) | 1.08 GB |
| `pl` | `pl/pleiades-places-latest.json.gz` | 2.8 months (2026-05-05) | 132 MB |

`wd` is the joint-oldest *and* by far the fastest-moving. (Corrected 30 July 2026: an earlier draft
of this table read `wd` as a 2025-07-28 / 12-month dump and inferred that it was "only now falling
through" `fetch_authorities`'s 365-day gate. The file's mtime is **2025-11-23** — 250 days, still
inside the default gate. Nothing here rests on that: every source below is fetched with `--age 0`,
which bypasses the gate entirely.)

**Sequencing consequence — this changes the class-A/class-B split.** For any source that is going
to be re-ingested from a fresh dump anyway, the class-A backfill is **wasted work**, provided the
ingest fix (§2 defect 1) lands first. So:

```
1. fix the ingest code            ← required regardless; do not skip
2. refresh dumps (wd, osm, ohm, gn, pl)
3. re-ingest those five           ← they arrive correctly encoded; no backfill needed
4. backfill ONLY the sources not being refreshed:
   tgn*, nl, iv, gb, ofs, alc, un, ukhc, kain_par, vob_*
```

*(`tgn` is 7.4 months old — refresh it too and it moves to the re-ingest set.)*

Two further points:

- **A fresh OSM dump changes the attestation year from 2025 to 2026** — which is correct, and
  another reason not to backfill `osm` to `start.latest 2025` first and then immediately overwrite
  it. `osm` is 8.86 M docs; that is the single largest piece of avoidable work in the whole plan.
- **The long pole is the download**: 148 GB (`wd`) and 92 GB (`osm`). Start those early; everything
  else in this section is hours.

**Interaction with the Atlas plan's §8.** Re-ingesting `osm` / `ohm` forces a retile of exactly the three buckets
the place#159/#160 retile covers. Running the tiling fix standalone first means paying that 24 h
retile twice. That is defensible if the missing-tiles fix is wanted sooner than the dump refresh —
but it should be a deliberate choice, not a surprise.

## 6. Calendar models — no data-model change needed

Wikidata time values carry a `calendarmodel` URI, and our extractor ignores it. Measured over the
same 600 k-entity dump sample (33,194 dated claims on entities with P625):

| calendar model | claims | share |
|---|---:|---:|
| `Q1985727` proleptic Gregorian | 30,796 | 92.78 % |
| **`Q1985786` proleptic Julian** | **2,398** | **7.22 %** |

7.2 % sounds alarming for a historical gazetteer. It is not, because **we store years and the
Julian/Gregorian divergence is measured in days** (10 days at 1582, ~3 days per 400 years, zero
around the 3rd century).

Julian claims by precision: **72.06 % are year-precision** (`+1500-00-00`, month and day zeroed —
nothing to convert), 12.47 % century, 10.84 % day, 1.50 % month. Only the month-or-finer subset —
**296 claims** — has a real date that conversion could move, and of those only ones sitting within
~13 days of a year boundary could change the *year* we store:

```
EXPOSURE: 21 / 33,194 dated claims = 0.0633%
```

And 21 is an **upper bound**, because the test is direction-agnostic while the conversion is not.
Normalising Julian → Gregorian *adds* days, so an early-January Julian date moves forward within
the same year (`+1492-01-02` → 1492-01-11); only a **late-December** Julian date crosses into the
next year. Every sampled example was January — i.e. non-flipping:

```
Q79791  P582 +1492-01-02  prec 11        Q260775 P585 +1117-01-03  prec 11
Q123559 P576 +1492-01-02  prec 11        Q315609 P571 +0968-01-02  prec 11
```

**Recommendation: change nothing in the data model.** Normalise to proleptic Gregorian *years* at
ingest and let `calendarmodel` stop there — it is a property of the source statement, not of the
place, and at year granularity it is very nearly information-free. Converting day-precision Julian
dates properly is a deterministic ~5-line offset and worth doing for tidiness; it is not worth a
schema field, a sub-object, or a migration.

Two residual notes:

- **Year-precision Julian is mildly ambiguous by nature**: "Julian year 1500" spans roughly
  Gregorian 1500-01-11 → 1501-01-10, so it overlaps Gregorian 1500 for ~97 % of its extent.
  Mapping Y → Y is right to within days. Where that is not good enough, the **existing**
  `earliest`/`latest` pair absorbs it (`start.earliest = Y`, `start.latest = Y+1`) — the §1
  uncertainty machinery already covers the case, with no new field.
- **Non-solar calendars are a different problem and are not solved by ignoring them.** A Hijri
  year is ~354 days, so one Hijri year genuinely straddles two Gregorian years and the drift is
  ~11 days per year. That is relevant to `ofs` and `og` (Ottoman registers, Hijri/Rumi dating) and
  possibly `chgis` (reign-era dating). If the upstream source converted, we inherit its choice; if
  not, the same `earliest`/`latest` widening applies — but with a routine one-year spread rather
  than a 0.06 % edge case. Worth confirming per source before those are re-ingested.

## 7. Attestations narrow under clustering

A place attested by Index Villaris in 1680 and by OSM in 2025 yields `start.latest = 1680` and
`end.earliest = 2025` — **definitely alive across the whole span**, a conclusion neither source
states alone. Aggregating attestations is `min()` over `start.latest` and `max()` over
`end.earliest`, which is *not* the current envelope rule and produces a genuinely different
(and defensible) result. This is a real payoff from the co-reference work, and it is unreachable
while both sources are stored as point lifespans.

## 8. Ownership — this is not a tiling change

The tiling side of it is trivial (carry four ints instead of two; the place#160 byte census shows
attributes are ~1/2400 of tile bytes, so it is free). The work is in ingestion, the ES mapping,
the reader, and the query semantics — and **the ingest fix is required regardless of any
backfill**, so that future re-runs emit the right encoding rather than re-introducing the defect.

This work is **place#164**. The Atlas plan's §5.1 (temporal filter on map layers) is blocked on
it; nothing else in that plan is.

---

## 9. Order of work, and the explicit handover to the Atlas plan

**This plan runs first**, because it is the one that commissions re-ingestion, and re-ingesting
`osm`/`ohm` forces a retile that `plan-atlas-data-architecture.md` §8 also needs. Running them the
other way round pays the 24 h-per-bucket retile twice and publishes the wrong encoding for another
cycle.

### Start immediately — no dependencies, nothing blocked by them

1. **Kick off the dump downloads.** ✅ **RUNNING — submitted 30 July 2026, 17:12 EDT.** 148 GB
   (`wd`) + 92 GB (`osm`), plus `gn`, `ohm`, `pl`, `tgn` (§5). This is the long pole and it blocks
   nothing, so it runs while the code work happens.

   Three Slurm jobs on `htc` (never on a login node), via the now-parameterised
   `processing/refresh_authorities.slurm` — `NS`/`AGE` come from the submitting environment:

   ```bash
   cd /vast/ishi/elastic
   NS=wd  AGE=0 sbatch -M htc --account=ishi -J fetch_wd  --time=3-00:00:00 --qos=htc-htc-n --export=ALL processing/refresh_authorities.slurm
   NS=osm AGE=0 sbatch -M htc --account=ishi -J fetch_osm --time=3-00:00:00 --qos=htc-htc-n --export=ALL processing/refresh_authorities.slurm
   NS=gn,pl,ohm,tgn AGE=0 sbatch -M htc --account=ishi -J fetch_small --time=12:00:00 --qos=htc-htc-s --export=ALL processing/refresh_authorities.slurm
   ```

   (Split three ways so a slow `wd` does not hold up the rest. Jobs 10692795 / 10692796 / 10692797;
   logs at `/ix1/ishi/logs/fetch_*_<jobid>.log`.)

   **Storage — no manual deletion needed.** Every one of the six writes to a *fixed* filename
   (`ohm` has an explicit `name:` in its config precisely because upstream uses dated ones), and
   `fetch_authorities` downloads to `<name>.part`, then `temp.replace(dest)` only on success. So
   the old dump stays readable until the new one is complete and is then replaced in place. Budget
   peak disk = **old + new** for whatever is in flight (~250 GB here); `/ix1/ishi` had 2.1 TB free
   at submission, so this is comfortable. The job logs `df` before and after and flags any leftover
   `.part` (= the fetch did not finish; resumable, do not delete).

   **Stale derivative — ✅ deleted 30 July 2026.** `/ix1/ishi/data/authorities/tgn/tgn_side_index.sqlite`
   (1.86 GB, Dec 2025, plus its `-shm`/`-wal`). `authorities/tgn-places.py:115 build_side_index`
   builds its indexes **in memory** from `explicit.zip` and contains no sqlite references at all —
   the file was a leftover from a retired implementation, and stale against the refreshed
   `explicit.zip` regardless. (The other sqlite-backed authorities — `trismegistos`, `dgsd`,
   `chgis`, `wikidata-geoshapes` — keep their own databases in their own directories and were
   unaffected.) `tgn/` now contains only `explicit.zip`.
2. **Fix the ingest code** (§2 defect 1, §3 per-source table). **Required regardless of any
   backfill** — the backfill is one-off, the encoding is permanent, and a future re-run with
   unfixed code silently re-introduces the defect.
3. **Fix the reader** — `_iter_year_ints` / `_collect_extent_for_doc` / `doc_temporal_range` (§2
   defect 2), including string coercion (§2 defect 3). Without this, correct storage still reads
   back as `(2025, 2025)`.
4. **Fix the schema/live-mapping divergence** — `text` vs `integer` on `start.earliest` /
   `end.latest` (§2 defect 3); takes effect at the next full rebuild, so it must be in place before
   step 6.

### Then

5. **Re-ingest** `wd`, `osm`, `ohm`, `gn`, `pl`, `tgn` from the refreshed dumps — they arrive
   correctly encoded, so **no class-A backfill is needed for any of them** (§5).
6. **Backfill only what was not refreshed** — `nl`, `iv`, `gb`, `ofs`, `alc`, `un`, `ukhc`,
   `kain_par`, `vob_*`. `_bulk`, **not** `_update_by_query` (which re-runs the ingest pipeline).
7. **Re-ingest class B** alongside — `po` (9,003), `pl` (25,561), `tm` (64,196). Hours, not a
   campaign; do not defer these to "when next touched".

### ⇨ HANDOVER — hand to `plan-atlas-data-architecture.md` §8 here

**Trigger: the moment re-ingestion (step 5) completes.**

At that point this plan owns nothing further that touches tiles. The Atlas plan owns the **single**
retile that publishes all of it, and that retile is gated on four things, only one of which is
ours — see its §8 entry condition:

| gate | owner |
|---|---|
| temporal encoding fixed **and** re-ingested | **this plan, steps 2–5** |
| `tile-join -pk` + skip-message failure + verifier (place#160) | Atlas plan §8 item 3 |
| labels channel `label:1` (place#159) | Atlas plan §8 item 4 |
| containment-hierarchy test decided (Atlas §9.4) | Atlas plan §5.4 |

**Do not schedule a retile from this plan.** If steps 2–5 finish before the other three gates,
stop and say so — the retile waits.

### Afterwards (Atlas plan owns these, listed so the dependency is visible)

8. **Temporal filter on map layers** — Atlas plan §5.1 / §8 item 2. This is what the whole exercise
   unblocks: it collapses OHM's low-zoom overlap by 88–99 %. Two client-side consequences decided
   30 July 2026: a historical range **does** hide contemporary sources (the "+Contemporary" toggle
   is abandoned), and the control becomes **two modes** — *definitely* vs *possibly* alive — rather
   than one range test plus an "+Undated" escape hatch.

