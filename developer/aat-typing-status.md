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

## Coverage (re-audited 12 Jul 2026, prod `places`, after the backfills)

| Band | Namespaces (records · AAT coverage) |
|------|-------------------------------------|
| **100%** | tgn (NEW, ~3M) · chgis · hgis · og · ofs · un · **iv · clio · po · nl · dgsd · dp · ukhc (all NEW)** |
| **~92–99%** | alc 99% · **whg 99% (NEW)** · ohm 93% · **tm 92% (NEW; rest = `people`/`kleros`, left untyped)** |
| **70–85%** | gn 85% · osm 85% · wd 75% · pl 72% |
| **ZERO** | **gb (1.17M)** — the only remaining zero |

**What got done 2026-07-12:** TGN (`processing/tgn_aat_backfill.py`) + a curated
`processing/manual_aat_maps.py` (namespace+identifier maps, an `aat:<id>` extractor,
and a whg free-text `sourceLabel` map) wired into `aat_enrich.augment_doc` — driving
both the live backfill (`apply_aat_enrich --namespace <ns>`) and future ingestion.

## What remains

- **gb (1.17M)** — no native feature type; genuinely hard. Idea (unbuilt): a VLM/CV
  reading OS map typography — see `authorities/gb1900-places.py`.
- **wd residual 25%** — a long tail of specific Wikidata Q-items. The right tool is
  the aat_mapper **P1014/P279 derivation pass** (bridges Q-item→AAT via Wikidata's
  Getty-AAT property). The Wikidata API is firewalled from pitt → run it from a
  net-connected host and rebuild `typesystem/data/wikidata.json`.
- **pl residual** — mostly non-place metadata (`unlocated`/`label`/`unknown`/`false`),
  legitimately untypeable.
- **Cross-cutting (§2/§7):** the `type_mappings` index + post-retrieval consanguinity
  engine, and the AAT-based type-facet UI (server facets on `aat_ids` with friendly
  labels + a hierarchical filter) — the visible §7 payoff now that coverage is high.

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
