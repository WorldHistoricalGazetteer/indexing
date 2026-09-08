# TGN contributes no co-reference edges, and the cause is upstream of the harvest

**Measured 8 September 2026, read-only against the live overlay and staged tree.**
Raised by a beta tester's Map-your-Data failures (`No usable geometry for:
tgn:7012189`), diagnosed with `whg3-67` and `markets-95`, whose sessions have
since ended — hence this file.

## The symptom

A `contained_in` scope on a TGN place fails, and the linked-polygon fallback
(place#144) does not rescue it. Confirmed end-to-end against the live gateway:

```
tgn:7011479   applied=False  mode=none            hits=0
wd:Q487413    applied=True   mode=polygon         hits=10
osm:r178025   applied=True   mode=polygon         hits=10
gn:1528260    applied=True   mode=linked-polygon  hits=10   (borrows wd:Q487413)
```

**Everything except TGN works**, and `gn` borrowing `wd`'s polygon shows the
fallback is correct and live.

## Why: `tgn` has no usable edges at all

`tgn:7012189` ("Ysyk-Köl") carries one geometry: `geom_class="point"`,
`has_geom=false`, `h3_cover` of one cell. A point cannot scope, so
`resolve_region` returning None is right. The fallback then needs a co-referent —
and there is none.

```
hard_link_assertions total                    7,572,016
  with tgn:7012189 at either endpoint                 0
  with ANY tgn: endpoint                             85
  of those 85: source_category=contributor           85   (3 legacy_v3_2 datasets, all 2026-04-06)
               relation_type=closeMatch              85
```

🛑 **The usable figure is 0, not 85.** `spatial.py:542`
`_GEOM_LENDING_RELATIONS = ("sameAs", "exactMatch")`, and all 85 are
`closeMatch` — contributor annotations, not harvest output. Store-wide for
scale: `sameAs` 7,500,821 · `closeMatch` 70,673 · `exactMatch` 522.

## The cause is the TGN EXTRACT, not the harvest configuration

The harvest ran and covered the corpus. **Positive controls, so the query is not
what is failing:**

```
authority-sourced edges by source_id
  wd 3,968,404 · osm 2,295,659 · gn 1,111,147 · ohm 98,569 · pl 0 · tgn 0 · whg 0
```

`clustering.harvest.hard_links_staged` reads `related_place_id` /
`relation_type` from each namespace's `staged/{ns}/final/places.parquet`:

```
tgn   related_place_id  ABSENT      (parquet present, 200 MB)
gn    related_place_id  PRESENT
pl    related_place_id  PRESENT
osm   related_place_id  PRESENT
wd    related_place_id  PRESENT
```

✅ **So tgn WAS read and there was nothing in it to harvest.** The fix belongs in
`authorities/tgn-places.py`: Getty publishes `skos:exactMatch` to Wikidata and it
is simply not carried into the staged extract. A TGN re-extract would then
populate the overlay as a side effect, with no separate co-reference pass.

⚠ **Method caveat:** the column test is `grep` for the literal string in the
parquet footer, not a schema parse — pitt's system python has no pyarrow and the
conda env is on the home filesystem pitt does not mount. Strong (present for
four namespaces, absent for the fifth, with real column names visible in both)
but a `pq.ParquetFile(...).schema_arrow` check would settle it beyond argument.

## 🛑 Two further items, neither investigated

* **`pl` is a DIFFERENT defect with the same symptom.** Pleiades *has* the
  column and still produced 0 edges — so either the values are empty or every
  target is in a namespace the harvester drops as unknown. Found only because
  the positive controls included it.
* **The honest relation type is the one the fallback ignores.** Getty TGN
  concepts and Wikidata items are genuinely not always the same thing, so
  `closeMatch` is often the correct assertion — and it will be filed and never
  used. Someone will eventually "fix" this by asserting `sameAs` where it is not
  quite true. **A decision, not a bug:** harvest as `exactMatch` only where Getty
  and Wikidata genuinely agree; or let the spatial path borrow across
  `closeMatch`; or leave TGN unusable as a scope and say so in the client.

## Unrelated defect found in the same area, confirmed in deployed code

`gateway/hard_link_expansion.py` — the inner SQLite waits **exceed** the outer
guard deadline, so a contended read can never succeed inside the budget:

```
:87   _IO_TIMEOUT_S  = 3.0     outer IoGuard deadline
:88   _IO_COOLDOWN_S = 60.0    breaker duration
:145  sqlite3.connect(..., timeout=5.0)
:146  PRAGMA busy_timeout=5000
```

A read that would have completed at 3.5 s instead opens a **60 s** breaker that
suppresses the linked-polygon fallback. Observed tripping at low volume against
a store benchmarking at 4–57 ms. **Fix: derive the inner waits from
`_IO_TIMEOUT_S`** so the relationship is expressed rather than two constants that
must be remembered to agree. Not applied — `origin/main` auto-deploys to the
live gateway.
