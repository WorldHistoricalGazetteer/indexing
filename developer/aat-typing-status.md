# AAT place-type coverage — status & the road ahead

> **Updated:** 12 July 2026 (after the TGN → AAT backfill). Companion to
> `plan-outstanding-2026-07.md` §2. Purpose: a single view of which sources are
> typed against the Getty **AAT** hierarchy, what remains, and the note for the
> public `/development` list.

## Why this matters (the §7 search overlap — explicit)

AAT place types are what make **type filtering** and **type facets** in search
work. Two §7 items depend directly on this coverage:

- **`fclasses` → type facets** — replacing the legacy A/P/S/R/L/T/H checkboxes with
  a real, *hierarchical* type filter (pick "inhabited places" and also match its
  subtypes) needs records carrying `aat_ids` / `aat_paths`.
- **Type-facet labels** — friendly facet labels (instead of raw source codes) come
  from the AAT `term` for each id.

So **until a source is typed, its records can't be filtered or faceted by place
type.** Getting everything typed is a multi-month, source-by-source effort.

## Coverage (re-audited 12 Jul 2026, prod `places`)

| Band | Namespaces (records · AAT coverage) |
|------|-------------------------------------|
| **Good (70–100%)** | chgis, hgis, og, ofs, un — 100% · **tgn ~99% (NEW)** · alc 99% · ohm 93% · gn 85% · osm 85% · wd 75% · pl 71% |
| **Zero — has a mappable native vocab** | **tm** (64K, ancient-world types) · **iv** (24K) · **clio** (15.7K, `polity`) · **dgsd** (3.8K, Song-dynasty) · **ukhc** (92, `historic-county` — trivial) |
| **Zero — little/no native place type** | **gb** (1.17M, transcribed OS map text) · **whg** (14.2K, mixed contributed LPF) · **po** (9K, time periods, not places) · **nl** (4.4K, territories) · **dp** (2.6K, language points) |

**TGN (~3M)** was the biggest single win and is now typed (see §2 /
`processing/tgn_aat_backfill.py`). **gb (1.2M)** is the largest remaining zero but
the hardest — GB1900 is transcribed map labels with little reliable type signal.

## Suggested order for the rest

1. **Cheap, high-confidence:** `ukhc` (one type), `clio` (`polity` → one AAT), `tm`
   (ancient-world type vocabulary), `dgsd`.
2. **Medium:** `iv` (Index Villaris settlement types), a native-type pass for `whg`
   datasets that carry types.
3. **Judgement calls:** `gb` (may not be worth it), `po`/`dp`/`nl` (arguably not
   place-typed — map to a single representative AAT concept, or leave untyped).

Then the cross-cutting §2 build-out: the `type_mappings` index + derivation passes
+ the post-retrieval consanguinity engine, feeding the §7 type-facet UI.

---

## Ready-to-paste note for the whg3 `/development` list

Add to `main/views.py` → `BETA_STATUS_SECTIONS` → "In development & beta preview"
`items` (marked `staff: True` so it shows to staff and is hidden from the public
until ready). Matches the existing item shape:

```python
{"name": "Standardised place types across every source (Getty AAT)",
 "stage": "dev", "version": "3.5", "staff": True,
 "body": "We're giving every place in WHG a standardised place type from the Getty "
         "Art & Architecture Thesaurus (AAT) — a shared, hierarchical vocabulary — so "
         "you can filter and browse by the *kind* of place (city, river, temple, "
         "administrative area…) consistently, whichever gazetteer a record came from. "
         "This powers the search experience directly: a hierarchical type filter (choose "
         "'inhabited places' and also get its subtypes) and friendly, human-readable "
         "type labels, replacing today's raw source codes. It's a large, source-by-source "
         "effort spanning several months: the biggest sources — GeoNames, OpenStreetMap, "
         "Wikidata, OpenHistoricalMap, and now the Getty Thesaurus of Geographic Names "
         "(~3M records) — are typed, but a number of smaller and specialist gazetteers "
         "still need their type vocabularies mapped to AAT before their records become "
         "fully filterable by type. Coverage is growing steadily."},
```
