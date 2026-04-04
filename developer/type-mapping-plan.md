# WHG Unified Type System: Architecture, Mapping, and Search

> **Audience:** Human developers and coding agents working on the WHG v3.5
> codebase.  The search model described here is designed to migrate
> cleanly to the ArangoDB graph architecture planned for v4.
>
> **Status:** Design document and implementation plan.

---

## 1. Why AAT as the Primary Type Vocabulary

WHG v3.5 adopts the Getty Art & Architecture Thesaurus (AAT) as its
canonical place-typing vocabulary, replacing the coarse GeoNames
feature-class system used in earlier versions.

**What AAT provides that GeoNames feature codes do not:**

- Dereferenceable URIs (`http://vocab.getty.edu/aat/{id}`).
- A maintained hierarchical thesaurus with explicit broader/narrower
  relationships (SKOS).
- Poly-hierarchy: a concept may have multiple broader terms through
  different facet paths.
- Wide adoption across the cultural heritage and digital humanities
  linked-data ecosystem.

GeoNames feature codes are a flat two-tier code list (9 classes, ~680
codes) without inferential structure or URI-based identity.

**The attestation-centric v4 data model will permit polyvocal typing:**
a GeoNames-sourced attestation carries its feature code, a
Wikidata-sourced attestation carries its Q-class, and contributed
datasets carry whatever vocabulary the scholar assigned.  AAT serves as
the preferred harmonisation vocabulary, not the sole permitted one.
The mapping infrastructure built here for v3.5 will feed directly into
that model.

### 1.1 AAT Bulk Data

Full N-Triples dumps are available from the Getty:

| File | URL | Contents |
|------|-----|----------|
| `full.zip` | `http://aatdownloads.getty.edu/VocabData/full.zip` | All statements including inferred triples |
| `explicit.zip` | `http://aatdownloads.getty.edu/VocabData/explicit.zip` | Only explicitly asserted triples |

Consult the "Export Files" section of the semantic representation
documentation at `http://vocab.getty.edu/doc` before use.  Data is
released under the Open Data Commons Attribution License (ODC-By) 1.0.

For WHG purposes, `full.zip` is preferred unless a local reasoner is
available to materialise inferred triples from `explicit.zip`.

---

## 2. The Mapping Problem

WHG indexes place records from multiple authority sources and from
user-contributed datasets.  These sources type places using three
different vocabularies:

| Source | Typing vocabulary | Granularity |
|--------|-------------------|-------------|
| **AAT** | ~4 000 place-type concepts (hierarchical) | Fine: the canonical WHG vocabulary |
| **GeoNames** | 9 feature classes, ~680 feature codes | Medium (codes) / coarse (classes) |
| **Wikidata** | Open-ended Q-items as `P31` values | Extremely fine but noisy |
| **OSM** | 6 tag keys × open-ended tag values | Medium; structured but no hierarchy |
| **Pleiades** | 229 curated place types (220 active) | Fine; 151 already map to AAT |

The existing WHG `places` index on the Pitt CRC staging ES instance
already contains ingested records from **TGN**, **GeoNames**,
**Wikidata**, **OSM**, and **Pleiades**, together with their assertions
of mutual identity (coreferences).  TGN records carry editorially
assigned AAT place-type identifiers, and many are coreferenced with
GeoNames and Wikidata entities.  The `places` index therefore already
holds all the raw material needed to derive cross-vocabulary mappings:
if a TGN record typed as AAT `300008389` ("cities") is coreferenced
with a GeoNames entity whose feature code is `PPLA`, that pairing
constitutes a curator-verified mapping from `PPLA` to
`aat:300008389`.

### 2.1 Coverage Expectations

- **Strong coverage:** GeoNames classes P (populated places) and A
  (administrative divisions) map naturally to AAT.  Parts of S
  (spots/buildings/farms) also find counterparts, since AAT is strong
  on building and structure types.
- **Weak coverage:** Natural-feature classes H (hydrographic), T
  (landforms), U (undersea), and V (vegetation) contain many codes for
  which AAT has no corresponding concept or only very coarse-grained
  matches.
- **Pleiades — strong coverage:** The Pleiades vocabulary is
  explicitly curated against AAT.  Of 220 active types, **151 already
  carry a `same_as` link to an AAT concept** via the Pleiades
  vocabulary endpoint (`https://pleiades.stoa.org/vocabularies/place-types`).
  A further 9 link to Wikidata Q-items and 1 to GeoNames.  Only ~60
  types (mostly specialist ancient categories like `ekklesiasterion`,
  `pagus`, `vicus`, `centuriation`) lack any external mapping.  These
  can be handled by manual assignment or label matching against AAT.
- **OSM — medium coverage:** OSM types use a `key=value` tag model
  across 6 keys (`place`, `natural`, `water`, `waterway`, `historic`,
  `landuse`).  The `place` key values (`city`, `town`, `village`,
  `hamlet`, etc.) map straightforwardly to AAT inhabited-place
  concepts.  `historic` values (`castle`, `monument`, `ruins`, etc.)
  also find good AAT counterparts.  `natural` and `water`/`waterway`
  values are weaker, mirroring the GeoNames H/T coverage gap.
- **Unmapped places:** Many places will lack any type assignment.
  Where possible, assign a default broad AAT type during the
  post-processing augmentation step (e.g. "inhabited places" for
  GeoNames P-class sources, "administrative divisions" for A-class).

### 2.2 Pleiades Vocabulary and AAT

Pleiades uses its own curated place-type vocabulary, published as a
JSON endpoint at `https://pleiades.stoa.org/vocabularies/place-types`.
This vocabulary is **explicitly aligned to AAT**: most entries carry a
`same_as` URI pointing to a Getty AAT concept.

**Summary (as of April 2026):**

| Category | Count |
|----------|-------|
| Total types | 229 |
| Active (not deprecated) | 220 |
| With AAT `same_as` | 151 (69%) |
| With Wikidata `same_as` | 9 |
| With GeoNames `same_as` | 1 |
| No external mapping | ~60 |

**Mapping strategy for Pleiades:**

1. **Direct AAT links (151 types):** Extract the AAT ID from the
   `same_as` URI (e.g. `http://vocab.getty.edu/aat/300008347` →
   `300008347`).  These are curator-verified `confidence=exact,
   source=pleiades_vocabulary` mappings requiring no review.

2. **Wikidata links (9 types):** Where the `same_as` points to a
   Wikidata Q-item (e.g. `building` → `Q41176`), bridge through the
   Wikidata→AAT mapping (Pass 1 in §3.2) if available.  Assign
   `confidence=exact, source=pleiades_via_wikidata`.

3. **Unmapped types (~60):** These are mostly specialist ancient-world
   categories.  Handle via:
   - Label matching against AAT (Pass 3 in §3.2).
   - Manual assignment for domain-specific terms
     (`ekklesiasterion`, `prytaneion`, `lesche`, `pagus`, `vicus`,
     `nuraghe`, etc.) that have no AAT equivalent — assign the
     nearest broader AAT type.

**Note:** The Pleiades vocabulary is a **static lookup table**, not a
coreference-derived mapping.  It should be fetched once, converted to
a mapping file, and loaded into the `type_mappings` index alongside
the GeoNames and Wikidata mappings.

### 2.3 OSM Tag Vocabulary and AAT

> **Full inventory:** See `developer/osm-types-inventory.md` for the
> comprehensive catalogue of all OSM tag keys and values relevant to
> WHG, with per-value AAT mapping assessments, frequency estimates,
> and implementation priority tiers.

OSM types in the WHG `places` index are stored as:
```json
{"identifier": "<tag_value>", "label": "osm", "sourceLabel": "<tag_key>=<tag_value>"}
```

The ingestion script (`authorities/osm-places.py`) currently extracts
types from **6 OSM tag keys**: `place`, `natural`, `water`,
`waterway`, `historic`, and `landuse`.  The `sourceLabel` field
preserves the full `key=value` pair, enabling unambiguous reverse
lookups.

**This is incomplete.** A comprehensive survey (documented in
`osm-types-inventory.md`) identifies **11 additional tag keys** that
carry gazetteer-relevant named features currently being skipped:

| Additional key | Tier | Est. new named features | AAT coverage |
|----------------|------|------------------------|--------------|
| `amenity` (selected) | 2 | ~2M | 78% |
| `tourism` | 2 | ~300K | 40% |
| `leisure` | 2 | ~800K | 30% |
| `man_made` | 2 | ~200K | 52% |
| `boundary` (excl. admin) | 2 | ~210K | 43% |
| `military` | 2 | ~30K | 30% |
| `building` (allowlist) | 2 | ~200K | 52% |
| `aeroway` | 3 | ~40K | 25% |
| `railway` | 3 | ~250K | 22% |
| `geological` | 3 | ~5K | 0% |
| `power` | 3 | ~15K | 0% |

The Tier 2 keys alone would add an estimated **3–5M named features**
to the index.  `boundary=administrative` is excluded from this table
because it is planned for a separate administrative index (see
`search-system-architecture.md`).

**OSM `place` tag values** (~18M records, high-frequency):

| OSM tag | Description | Suggested AAT | AAT ID |
|---------|-------------|---------------|--------|
| `place=city` | Large urban settlement | cities | 300008389 |
| `place=town` | Medium urban settlement | towns | 300008375 |
| `place=village` | Small rural settlement | villages | 300008372 |
| `place=hamlet` | Very small settlement | hamlets | 300008197 |
| `place=suburb` | District of a city | suburbs | 300000745 |
| `place=neighbourhood` | Sub-area of a city | neighborhoods | 300000745 |
| `place=isolated_dwelling` | Single dwelling | dwellings | 300005433 |
| `place=farm` | Agricultural holding | farms | 300000206 |
| `place=island` | Island | islands | 300008791 |
| `place=islet` | Very small island | islets | 300008792 |
| `place=locality` | Named locality | inhabited places (broad) | 300008347 |
| `place=county` | County-level admin | counties | 300000771 |
| `place=municipality` | Municipality | municipalities | 300265612 |
| `place=region` | Named region | regions | 300387178 |
| `place=state` | State/province | states | 300000776 |
| `place=country` | Country | nations | 300128207 |
| `place=continent` | Continent | continents | 300128176 |
| `place=archipelago` | Island group | archipelagos | 300386854 |
| `place=quarter` | City quarter | quarters | 300000745 |
| `place=borough` | Borough | boroughs | 300000778 |
| `place=allotments` | Allotment gardens | allotment gardens | — |
| `place=plot` | Named land plot | — | — |
| `place=square` | Town square | plazas | 300008066 |

**OSM `natural` tag values:**

| OSM tag | Suggested AAT | AAT ID |
|---------|---------------|--------|
| `natural=peak` | peaks | 300008816 |
| `natural=volcano` | volcanoes | 300132325 |
| `natural=bay` | bays | 300132316 |
| `natural=beach` | beaches | 300008816 |
| `natural=cape` | capes | 300008850 |
| `natural=cliff` | cliffs | 300008749 |
| `natural=cave_entrance` | caves | 300008746 |
| `natural=glacier` | glaciers | 300008771 |
| `natural=spring` | springs | 300008697 |
| `natural=hot_spring` | hot springs | 300008700 |
| `natural=geyser` | geysers | — |
| `natural=ridge` | ridges | 300266640 |
| `natural=valley` | valleys | 300008830 |
| `natural=peninsula` | peninsulas | 300008804 |
| `natural=isthmus` | isthmuses | — |
| `natural=strait` | straits | 300266559 |
| `natural=reef` | reefs | 300008808 |
| `natural=wetland` | wetlands | 300008899 |
| `natural=wood` | forests (broad) | — |
| `natural=heath` | heaths | 300008877 |
| `natural=grassland` | grasslands | — |
| `natural=scrub` | — | — |

**OSM `water` tag values:**

| OSM tag | Suggested AAT | AAT ID |
|---------|---------------|--------|
| `water=lake` | lakes | 300008680 |
| `water=river` | rivers | 300008707 |
| `water=reservoir` | reservoirs | 300006191 |
| `water=pond` | ponds | 300008688 |
| `water=lagoon` | lagoons | — |
| `water=canal` | canals | 300006075 |
| `water=harbour` | harbors | 300008678 |
| `water=bay` | bays | 300132316 |
| `water=oxbow` | oxbow lakes | — |

**OSM `waterway` tag values:**

| OSM tag | Suggested AAT | AAT ID |
|---------|---------------|--------|
| `waterway=river` | rivers | 300008707 |
| `waterway=stream` | streams | 300008699 |
| `waterway=canal` | canals | 300006075 |
| `waterway=drain` | drainage channels | — |
| `waterway=waterfall` | waterfalls | 300008736 |
| `waterway=dam` | dams | 300006072 |

**OSM `historic` tag values:**

| OSM tag | Suggested AAT | AAT ID |
|---------|---------------|--------|
| `historic=castle` | castles | 300006891 |
| `historic=monument` | monuments | 300006958 |
| `historic=ruins` | ruins | 300008057 |
| `historic=archaeological_site` | archaeological sites | 300000810 |
| `historic=fort` | forts | 300006909 |
| `historic=church` | churches | 300007466 |
| `historic=monastery` | monasteries | 300000641 |
| `historic=manor` | manor houses | 300005366 |
| `historic=battlefield` | battlefields | 300000835 |
| `historic=memorial` | monuments (broad) | 300006958 |
| `historic=tomb` | tombs | 300005926 |
| `historic=tower` | towers | 300004847 |
| `historic=city_gate` | city gates | 300002837 |
| `historic=mine` | mines | 300000390 |
| `historic=lighthouse` | lighthouses | 300007741 |
| `historic=bridge` | bridges | 300007836 |
| `historic=aqueduct` | aqueducts | 300006165 |
| `historic=milestone` | milestones | 300006973 |

**OSM `landuse` tag values:**

| OSM tag | Suggested AAT | AAT ID |
|---------|---------------|--------|
| `landuse=forest` | forests | 300008838 |
| `landuse=farmland` | agricultural land | 300265699 |
| `landuse=residential` | residential areas | — |
| `landuse=commercial` | commercial areas | — |
| `landuse=industrial` | industrial areas | — |
| `landuse=military` | military bases | 300000455 |
| `landuse=cemetery` | cemeteries | 300000632 |
| `landuse=quarry` | quarries | 300000402 |
| `landuse=vineyard` | vineyards | — |
| `landuse=orchard` | orchards | — |
| `landuse=meadow` | meadows | 300008876 |
| `landuse=reservoir` | reservoirs | 300006191 |
| `landuse=port` | ports | 300120580 |
| `landuse=salt_pond` | salt pans | — |

**Mapping strategy for OSM:**

1. **Static lookup table.** Unlike GeoNames and Wikidata, OSM types
   are not derivable from TGN coreferences (TGN rarely links to OSM).
   Instead, build the mapping table manually from the reference tables
   above and from `osm-types-inventory.md`, plus any additional values
   discovered by querying the `places` index for distinct OSM type
   identifiers.

2. **Discovery query.** Run an ES aggregation on the `places` index
   to enumerate all distinct `(identifier, sourceLabel)` pairs where
   `label=osm`.  This reveals the actual tag values present in the
   data, including long-tail values not listed above.

3. **Assign confidence levels:**
   - `place` key values with clear AAT equivalents: `confidence=exact`.
   - `historic` key values: `confidence=exact` (AAT is strong here).
   - `amenity` values (worship, cultural, civic): mostly
     `confidence=exact` (AAT has good coverage of institutional types).
   - `tourism` values (museums, castles): `confidence=exact` where
     they overlap with `historic`; `confidence=broad` otherwise.
   - `leisure` values (parks, reserves): `confidence=exact` for major
     types; coverage thins for modern recreational facilities.
   - `man_made` values (lighthouses, bridges, towers):
     `confidence=exact` for landmark types; weaker for infrastructure.
   - `natural`/`water`/`waterway` values with AAT matches:
     `confidence=exact` or `confidence=broad` depending on fit.
   - Values with no AAT equivalent: assign nearest broader AAT type
     with `confidence=broad, source=osm_manual`.

4. **Key disambiguation.** Because the `sourceLabel` preserves the
   full `key=value` pair, ambiguous values like `river` (which could
   come from `water=river` or `waterway=river`) are resolvable.  The
   mapping table should key on `sourceLabel`, not just `identifier`.

5. **Volume note.** OSM is the largest authority.  With the current 6
   tag keys it contributes ~18M places.  Expanding to all Tier 2 keys
   would add an estimated 3–5M more, bringing the total to ~21–23M.
   Even a coarse mapping covers a significant portion of the index.

6. **Deduplication.** Many OSM features carry multiple primary tags
   (e.g. `historic=castle` + `tourism=castle`).  The ingestion emits
   one type entry per matching tag key.  The AAT mapping layer
   deduplicates — both map to the same AAT concept.

7. **`building` allowlist.** The `building` key is only extracted when
   the value is in a curated allowlist of significant building types
   (cathedral, castle, palace, mosque, etc.).  `building=yes`,
   `building=residential`, and similar generic values are not extracted.
   See `osm-types-inventory.md` §5.5 for the full allowlist.

---

## 3. Deriving Cross-Vocabulary Mappings

All mapping derivation is performed on the **Pitt CRC cluster**,
orchestrated with bash, Python, and Slurm.  The input is the existing
`places` index on the CRC staging ES instance.  The outputs are the
`aat_types` and `type_mappings` ES indices described in section 5, plus
a set of review-ready mapping files (JSON or TSV) for manual
inspection.

> **Coding-agent note:** Specifics of CRC job submission, ES
> connection configuration, and Slurm resource allocation should be
> inferred from existing examples of working with the staging ES in
> the WHG CRC environment.

### 3.1 Exploiting Coreferences in the `places` Index

The `places` index contains records from TGN, GeoNames, and Wikidata,
linked by identity assertions.  The mapping strategy is:

1. **Identify coreferenced clusters.** Query the `places` index for
   groups of records (one TGN, one or more GeoNames, one or more
   Wikidata) that assert identity with one another.
2. **Extract type tuples.** For each cluster, extract the TGN record's
   AAT type(s), the GeoNames record's feature code, and the Wikidata
   record's `P31` Q-id(s).  Each cluster yields one or more
   `(aat_id, gn_fcode)` and/or `(aat_id, wd_qid)` pairings.
3. **Aggregate across all clusters.** Collect all pairings, counting
   the number of independent clusters attesting each.  High-frequency
   pairings (attested by many independent places) are high-confidence
   mappings.

### 3.2 Bootstrap Passes

Execute in the order listed.  Later passes should not overwrite
higher-confidence mappings from earlier passes.

#### Pass 0a: Pleiades Vocabulary Direct Mapping

The highest-confidence and easiest source.  The Pleiades place-type
vocabulary at `https://pleiades.stoa.org/vocabularies/place-types`
already publishes AAT `same_as` URIs for 151 of 220 active types.

1. Fetch the Pleiades vocabulary JSON endpoint.
2. For each entry with a `same_as` URI containing `vocab.getty.edu/aat/`:
   extract the AAT ID and create a `(pleiades_type_id, aat_id)` mapping.
3. For entries whose `same_as` points to Wikidata: note for resolution
   in Pass 1 or manual assignment.
4. Assign `confidence=exact, source=pleiades_vocabulary`.

Output: a Pleiades-to-AAT mapping table (151+ entries, no review
needed for `same_as`-sourced entries).

#### Pass 0b: TGN-Bridged Mappings (GeoNames and Wikidata)

This is the primary and highest-confidence source for GeoNames and
Wikidata.

1. Query the `places` index for all coreferenced clusters that include
   a TGN record carrying an AAT type.
2. For each cluster, extract `(aat_id, gn_fcode)` and
   `(aat_id, wd_qid)` pairings as described in 3.1.
3. Aggregate and threshold: retain only pairings attested by at least
   *n* independent clusters (suggested: n >= 3) to filter noise from
   miscatalogued entries.
4. Assign `confidence=exact, source=tgn_bridge`.

Output: two mapping tables (AAT-to-GeoNames, AAT-to-Wikidata) with
attestation counts.

#### Pass 0c: OSM Static Mapping

OSM types require a manually curated static lookup table because OSM
records are rarely coreferenced with TGN.  The mapping tables in
§2.3 and the comprehensive inventory in `osm-types-inventory.md`
provide the reference.

1. Run an ES aggregation on the `places` index to enumerate all
   distinct `sourceLabel` values where `label=osm`:
   ```json
   {
     "size": 0,
     "query": {"nested": {"path": "types", "query": {"term": {"types.label": "osm"}}}},
     "aggs": {
       "type_values": {
         "nested": {"path": "types"},
         "aggs": {
           "osm_only": {
             "filter": {"term": {"types.label": "osm"}},
             "aggs": {
               "by_source_label": {
                 "terms": {"field": "types.sourceLabel", "size": 1000}
               }
             }
           }
         }
       }
     }
   }
   ```
2. For each discovered `sourceLabel`, look up in the static mapping
   tables (§2.3 and `osm-types-inventory.md`).  Assign AAT IDs where
   available.
3. For unmapped values, attempt label matching against AAT terms
   (Pass 3 logic) or assign the nearest broader AAT type manually.
4. Assign `confidence=exact, source=osm_manual` for clear matches;
   `confidence=broad, source=osm_manual` for broader-type assignments.
5. Handle cross-key deduplication: `historic=castle` and
   `tourism=castle` both map to AAT 300006891.  The mapping table
   carries separate entries keyed on `sourceLabel` but they resolve
   to the same AAT concept.

Output: an OSM-to-AAT mapping table keyed on `sourceLabel` (e.g.
`place=city` → `300008389`).  The `sourceLabel` key is necessary
because the same `identifier` (e.g. `river`) can appear under
different tag keys (`water=river`, `waterway=river`).

**Scope note:** The initial Pass 0c covers the 6 currently extracted
tag keys.  After the OSM ingestion is expanded to include Tier 2 keys
(see `osm-types-inventory.md` §9), re-run the discovery aggregation
and extend the mapping table to cover the new `sourceLabel` values.

#### Pass 1: Wikidata P1014 Links

~1 200 Wikidata items carry a `P1014` (Getty AAT ID) property.  For
Wikidata Q-ids occurring in the `places` index that were not already
mapped in Pass 0:

1. Query Wikidata SPARQL for items with `P1014` values.
2. Create `(aat_id, wd_qid)` mappings.
3. Assign `confidence=exact, source=wikidata_p1014`.

#### Pass 2: Wikidata P279 Walk

For high-frequency Wikidata type Q-ids still unmapped after Passes 0
and 1:

1. Walk `P279` (subclass-of) upward from the unmapped Q-id until a
   Q-id with a `P1014` link or an existing mapping is found.
2. Record the mapping with the hop count.
3. Assign `confidence=broad, source=wikidata_p279`.

#### Pass 3: Label Matching (both vocabularies)

For GeoNames fcodes and Wikidata Q-ids still unmapped:

1. **GeoNames:** Compare AAT `term` values against GeoNames
   `featureCodes_en.txt` descriptions.  Normalise plurals, strip
   parenthetical qualifiers, apply fuzzy / token-overlap scoring.
2. **Wikidata:** Fuzzy-match the English label of unmapped Q-ids
   against AAT terms.
3. Assign `confidence=inferred, source=label_match`.  Flag for manual
   review.

#### Pass 4: Hierarchy Propagation

For AAT leaf types that have no direct GeoNames mapping after earlier
passes:

1. Inherit the most-specific fcode(s) of the nearest mapped ancestor
   in the AAT hierarchy.
2. Assign `confidence=broad, source=hierarchy_propagation`.

### 3.3 Review and Promotion

Each pass produces a JSON or TSV mapping file with attestation counts,
confidence levels, and source labels.  These files are reviewed
manually before being loaded into the `aat_types` and `type_mappings`
ES indices.  The review priority is:

1. `confidence=inferred` mappings from label matching (Pass 3).
2. Low-attestation-count mappings from TGN bridging (Pass 0).
3. `confidence=broad` mappings from hierarchy propagation (Pass 4).

High-confidence, high-attestation mappings from Passes 0 and 1 can
be promoted without individual review.

### 3.4 Reporting

A reporting script scans the `places` index and the current mapping
tables to identify:

- Wikidata Q-ids and GeoNames fcodes present in the data but not yet
  mapped to any AAT type.
- AAT types with no GeoNames or Wikidata mappings (coverage gaps).
- Distribution of confidence levels across the mapping tables.

---

## 4. Post-Processing Type Augmentation

The authority sources (TGN, GeoNames, Wikidata) have already been
ingested into the `places` index.  AAT type assignment is therefore a
post-processing step, run on the CRC staging instance and promoted to
production after verification.

### 4.1 Authority-Source Records

A bulk-update job:

1. Query the `places` index for records sourced from GeoNames,
   Wikidata, OSM, or Pleiades that lack AAT type annotations.
2. For each, look up the record's native type (fcode, `P31` Q-id,
   OSM `sourceLabel`, or Pleiades type identifier) in the mapping
   tables.
3. Write the mapped AAT type identifier(s) into the record's `types`
   array in ES.

### 4.2 Contributed Datasets

For contributed datasets already in the index whose `types` field
contains Wikidata Q-ids or GeoNames fcodes but no AAT identifiers:

1. Look up the Q-id / fcode in the mapping tables.
2. Assign the highest-confidence AAT type(s) found.
3. Log cases where no mapping exists (feeds the reporting script).

For future ingestions, the same lookup should be applied at ingest
time so that new records arrive with AAT types already assigned.

> **Coding-agent note:** The `places` index stores only the directly
> assigned AAT type identifier(s) for each record.  Do *not* add
> ancestor chains, related types, or any other hierarchy-derived data
> to the `places` index.  All hierarchical reasoning is performed
> post-retrieval (see section 6).

---

## 5. ES Index Structures

### 5.1 The `places` Index (existing, minimal changes)

Each place record in the `places` index retains its existing schema.
The only change is that the `types` array is progressively populated
with mapped AAT identifiers via the post-processing augmentation in
section 4.  No ancestor chains, depth values, or hierarchy-derived
fields are added to `places`.

### 5.2 The `aat_types` Index (new)

A dedicated index holding the AAT type hierarchy and cross-vocabulary
mappings.  This is the lookup structure used for query expansion and
post-retrieval consanguinity computation.

```json
{
  "aat_id": 300008389,
  "term": "cities",
  "parent_id": 300008347,
  "path": "300264550.300008346.300008347.300008389",
  "depth": 3,
  "ancestors": [300264550, 300008346, 300008347],
  "gn_fcodes": ["PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC"],
  "wd_qids": ["Q515", "Q1549591"],
  "osm_tags": ["place=city"],
  "pleiades_types": ["polis"]
}
```

The `osm_tags` field stores the full `sourceLabel` (i.e. `key=value`)
because the same value can appear under different keys.  Multiple
OSM tags may map to the same AAT concept (e.g. both `historic=castle`
and `tourism=castle` map to 300006891).

The `path` field (dot-delimited ancestor chain from root to leaf)
enables prefix queries to retrieve all descendants of a given type.
The `ancestors` array enables fast lookup of all ancestors for a given
type.  Together they support the consanguinity computation described
in section 6.

### 5.3 `type_mappings` Index (optional, for reverse lookups)

```json
{ "gn_fcode": "PPLA", "aat_ids": [300008389, 300008347] }
{ "wd_qid": "Q515",   "aat_ids": [300008389] }
{ "osm_tag": "place=city", "aat_ids": [300008389] }
{ "pleiades_type": "settlement", "aat_ids": [300008347] }
```

### 5.4 Sync and Cache Invalidation

A sync script bulk-indexes the AAT hierarchy plus the derived mapping
tables into the CRC staging ES instance.  Run after each mapping
derivation cycle.

Use the ES index-alias swap pattern: push to a timestamped index, then
atomically swap the alias, so search always reads a consistent
snapshot.  Once verified on the staging instance, replicate to
production.

---

## 6. Type Search Architecture

### 6.1 Design Principle: Lean Places, Smart Post-Processing

The `places` index is not bloated with hierarchy-derived fields.  Each
place stores only its directly assigned AAT type identifier(s).  All
hierarchical reasoning (narrower-term expansion, broader-term
inclusion, sibling detection) is performed in two stages:

1. **Broad retrieval** from the `places` index, casting a wide net.
2. **Post-retrieval banding and ranking** by computing consanguinity
   between each result's assigned type and the user's query type,
   using the `aat_types` index as the hierarchy lookup.

This keeps the `places` index lean and schema-stable, concentrates
type-hierarchy logic in a single post-processing layer, and avoids
re-indexing millions of place records whenever the AAT hierarchy or
mappings change.

### 6.2 The Two Retrieval Problems

#### The narrower-term problem

A search for "villages" should find "nucleated villages", "fishing
villages", and all other subtypes.  At query time, look up the query
type in the `aat_types` index and retrieve all descendant `aat_id`s
(via a `path` prefix query).  Also retrieve their mapped `gn_fcodes`
and `wd_qids`.  Use the resulting set of identifiers to query the
`places` index.

#### The broader-term problem

A place typed only as "inhabited places" *might* be a village but
might equally be a city.  The subsumption relationship runs the wrong
way, and no hierarchical traversal can resolve ambiguity that reflects
genuine ignorance in the source data.  This is handled by
post-retrieval consanguinity banding (section 6.3).

### 6.3 Post-Retrieval Consanguinity Model

After retrieving candidate places from the `places` index (using a
broad query that encompasses exact, narrower, broader, and sibling
types), each result is assigned to a tier by comparing its assigned
AAT type against the user's query type using the `aat_types` index.

**Tier 1 -- Exact and narrower matches.**  The result's assigned type
is equal to the query type, or the query type appears in the result
type's `ancestors` array (i.e. the result type is subsumed by the
query type).  These are certain hits.  Consanguinity distance = 0.
*Example: searching for "villages" returns everything typed as
villages, nucleated villages, fishing villages, etc.*

**Tier 2 -- Broader-type matches.**  The result's assigned type is an
ancestor of the query type (i.e. the result type appears in the query
type's `ancestors` array).  These are places that *could* be what the
user wants but are typed at insufficient granularity.  Consanguinity
distance = number of edges from the query type up to the result's
assigned type.
*Example: a place typed only as "inhabited places" appears here when
searching for "villages", with distance proportional to how many
hierarchy levels separate the two.*

**Tier 3 -- Sibling and lateral matches.**  The result's type shares a
common ancestor with the query type but is neither broader nor
narrower (co-hyponyms).  Consanguinity distance = sum of edges from
each type up to their nearest common ancestor.
*Example: searching for "villages" surfaces "hamlets" and "towns" as
sibling categories under "inhabited places".*

#### Computing consanguinity

Given a query type Q and a result's assigned type R:

1. Fetch Q's `ancestors` array and R's `ancestors` array from the
   `aat_types` index.
2. If Q == R, or Q appears in R's `ancestors`: **Tier 1**, distance 0.
3. If R appears in Q's `ancestors`: **Tier 2**, distance = position of
   R in Q's `ancestors` array (counting from the leaf).
4. Otherwise, find the nearest common ancestor (the first shared
   element, scanning both `ancestors` arrays from the leaf end).
   **Tier 3**, distance = (steps from Q to NCA) + (steps from R to
   NCA).
5. If no common ancestor is found within the distance threshold:
   **unranked / excluded**.

> **Coding-agent note:** The consanguinity computation runs in the
> application layer (Django view or Gateway service), not inside ES.
> For each search, pre-fetch the query type's ancestor chain once from
> `aat_types`, then iterate over the result set.  For result types
> encountered more than once, cache their ancestor lookups for the
> duration of the request.  The `aat_types` index is small (~4 000
> documents); aggressive caching (or loading the full hierarchy into
> memory at startup) is feasible and recommended.

#### Exploiting poly-hierarchy

AAT is poly-hierarchical: some concepts have multiple broader terms
through different facet paths.  Where the `aat_types` index records
multiple paths (multiple entries in `ancestors`), compute shortest-path
distance through *any* path for richer lateral connections.

#### Distance threshold

A maximum consanguinity distance (e.g. 4 or 5 edges) prevents absurd
lateral matches.  This is more principled than hand-curated root
families and less likely to create blind spots.  Results beyond the
threshold are excluded from Tier 3 entirely.

### 6.4 User-Facing Search Modes

Type-based search within a spatial region offers two modes:

#### "Only show" (hard constraint)

Excludes everything outside the specified type.  Post-retrieval
banding discards results outside the selected tiers.  The result set
shrinks.

*"Only show villages in Lincolnshire."*

Under this mode, offer a secondary choice:

- **Strict:** Tier 1 only (exact and narrower matches).
- **Inclusive:** Tiers 1 and 2 (also include broader-typed places that
  *could* be the requested type).

#### "Prioritise" (soft ranking)

Retains the full result set.  Post-retrieval banding assigns each
result a tier and consanguinity distance; the UI groups or sorts
results by tier, with distance as a secondary sort within each tier.

*"Show me places in Lincolnshire, with villages ranked first."*

### 6.5 Query Construction

When a search request arrives with a type parameter:

1. **Expand the query type.** Look up the query type in the
   `aat_types` index.  Retrieve all descendant `aat_id`s (via `path`
   prefix query) and their associated `gn_fcodes`, `wd_qids`,
   `osm_tags`, and `pleiades_types`.  Also retrieve ancestor `aat_id`s
   and sibling `aat_id`s (children of ancestors) up to the distance
   threshold, plus *their* mapped identifiers from all vocabularies.
2. **Broad retrieval.** Build an ES `bool` query matching records in
   `places` by *any* of:
   - AAT identifier in the record's `types` array, **or**
   - GeoNames fcode in the record's `types` array, **or**
   - Wikidata Q-id in the record's `types` array, **or**
   - OSM `sourceLabel` in the record's `types` array, **or**
   - Pleiades type identifier in the record's `types` array.

   Combine with any spatial, temporal, or textual filters the user has
   specified.  In "Only show / Strict" mode, the expansion set can be
   limited to Tier 1 types only, avoiding unnecessary retrieval.
3. **Post-retrieval banding.** For each result, look up its assigned
   type in the `aat_types` index and compute consanguinity against the
   query type (section 6.3).  Assign tier and distance.
4. **Filter or rank.** In "Only show" mode, discard results outside
   the selected tiers.  In "Prioritise" mode, sort by tier then by
   distance within tier.

> **Coding-agent note:** Step 1 (expansion) and step 3 (banding) both
> read from the `aat_types` index but serve different purposes.
> Expansion determines *what to retrieve* from `places`; banding
> determines *how to present* what was retrieved.  Keep them as
> separate, clearly named functions.

---

## 7. UI Enhancements

1. Offer "Only show" / "Prioritise" toggle in the search interface,
   with "Strict" / "Inclusive" sub-option under "Only show".
2. Display tier labels alongside results so users understand why each
   result appeared (e.g. "exact match", "broader type", "related
   type").
3. Allow search-results faceting by AAT type.
4. Show mapping coverage stats in the admin dashboard.

---

## 8. Migration Path to v4

The v4 graph model (ArangoDB) replaces the ES-based type indices with
native graph traversal.  The mapping tables and bootstrap heuristics
built here carry over directly; the main architectural changes are:

- **Consanguinity becomes a graph query.** Shortest-path and
  common-ancestor calculations that run in the application layer for
  v3.5 become native AQL traversal operations in ArangoDB, likely
  faster and certainly more elegant.
- **Polyvocal typing at the attestation level.** Each attestation
  carries its own type in its original vocabulary.  The AAT mapping
  becomes a property of the attestation rather than a synthetic
  annotation on the canonical place.
- **The `aat_types` ES index is replaced by the AAT subgraph** within
  the ArangoDB `indexing` database, traversable in real time.

The post-retrieval banding model translates directly: retrieve
candidate places, then traverse the AAT subgraph to compute tier and
distance for each result.

---

## 9. Estimated Effort (v3.5 Implementation)

| Phase | Scope | Effort |
|-------|-------|--------|
| 3.1--3.2 Pass 0a | Pleiades vocabulary direct mapping | 0.5 days |
| 3.1--3.2 Pass 0b | Coreference extraction and mapping derivation (CRC) | 3--4 days |
| 3.1--3.2 Pass 0c | OSM static mapping table (current 6 keys) | 1--2 days |
| 3.1--3.2 Pass 0c+ | OSM expanded ingestion (Tier 2 keys) + mapping | 2--3 days |
| 3.2 Passes 1--4 | Wikidata P1014, P279, label matching, hierarchy | 2--3 days |
| 3.3 | Manual review of inferred/low-confidence mappings | 1--2 days |
| 3.4 | Reporting scripts | 1 day |
| 4 | Post-processing type augmentation (all authorities) | 2--3 days |
| 5 | ES `aat_types` index and sync | 2--3 days |
| 6 | Post-retrieval consanguinity engine | 3--4 days |
| 7 | UI enhancements | 1--2 days |