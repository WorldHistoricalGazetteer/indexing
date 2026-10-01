# Trismegistos Geo in PLATO

**A PLATO conformance test of an authority, not a submission.** Trismegistos Geo (TM Geo) is
indexed by WHG as the authority `tm` (`authorities/trismegistos/`). This folder converts the
whole of it to PLATO JSON, and back, to find what PLATO cannot say about it:
[place-attestation-ontology#16](https://github.com/pelagios/place-attestation-ontology/issues/16).
Nothing here is staged or submitted to WHG.

The first pass raised five PLATO issues (#18 to #22), ruled on by Stephen on 1 October 2026 and
carried into PLATO at commit 7720890 (towards 0.8.x). This second pass uses what they added, and
is written against PLATO 7720890 as vendored in PLATO tools 3b10811.

## Source

| | |
|---|---|
| What | Trismegistos Geo, the geographical file of [Trismegistos](https://www.trismegistos.org/) (KU Leuven) |
| Where | `authorities/trismegistos/tm_geo.db`, read in place and opened `immutable`, so no side files are made. Built by `build_database.py` from `TM_geo.sql`, with links to other gazetteers from TM's GeoRelations service |
| Version | phpMyAdmin dump of the `tm` database's `geo` table, generated 8 April 2026, 09:52 (MySQL 8.0.25). Sent to Stephen Gadd by Tom Gheldof of Trismegistos in April 2026, as "the most recent data dump … containing all current 64,857 place names and the fields available via TM Data Services API"; committed 14 April 2026 (ce6d838). The concordances came from TM's GeoRelations Matcher API, as Tom advised |
| Checksums | `TM_geo.sql` sha256 `9743cd5873bb89c3e18cba19c2a6ee1d167564a35c87495563da9490fa65a6d5`; `tm_geo.db` sha256 `a92c4d00de7159012e0cea8579a5c12b06b7e3f9e15b134665e8c0ac5b5fc360` |
| Licence | CC BY-SA 4.0, as recorded in `processing/settings.py` (verified at trismegistos.org/dataservices, 6 June 2026). Rights: Trismegistos / KU Leuven |
| Not here | The links between texts and places, which hold TM's actual evidence. TM has them; whether it publishes them is a question for TM |

## Running it

```bash
python3 contributions/trismegistos/tm2plato.py --out /tmp/tm/tm-geo.jsonl         # all 64,857 records, about 7 s
PLATO_TOOLS=~/PycharmProjects/plato-tools node contributions/plato_run.mjs /tmp/tm/tm-geo.jsonl check
# through RDF and back, then to TM's own fields:
node ~/PycharmProjects/plato-tools/bin/plato-tools.mjs convert --to ntriples --out /tmp/tm /tmp/tm/tm-geo.jsonl
node ~/PycharmProjects/plato-tools/bin/plato-tools.mjs convert --to plato-jsonl --out /tmp/tm/back /tmp/tm/tm-geo.nt
node ~/PycharmProjects/plato-tools/bin/plato-tools.mjs compare /tmp/tm/tm-geo.jsonl /tmp/tm/back/tm-geo.jsonl
python3 contributions/trismegistos/plato2tm.py /tmp/tm/back/tm-geo.jsonl            # exit 1 if any carried field differs
# negative controls, on a sample of every feature (--sample 3: 60 records):
python3 contributions/trismegistos/tm2plato.py --sample 3 --out /tmp/tm/ctl/sample.jsonl
PLATO_TOOLS=... node contributions/plato_run.mjs /tmp/tm/ctl/sample.jsonl convert ntriples /tmp/tm/ctl/sample.nt
PLATO_TOOLS=... python3 contributions/controls.py /tmp/tm/ctl/sample.nt /tmp/tm/ctl/work -- bash -c \
  "node plato_run.mjs {nt} convert plato-jsonl {out}.jsonl >/dev/null 2>&1 && python3 trismegistos/plato2tm.py --only-present {out}.jsonl"
```

The full-scale steps (the check, the two conversions and the compare) each hold the whole
dataset in memory: run them one at a time. Every count `tm2plato.py` reports gives its
denominator: records (64,857) or links (71,835).

## Mapping

One SpatialEntity per TM Geo record, at `https://www.trismegistos.org/place/<id>`, with TM's id as
its `entityIdentifier`. Every attestation cites TM Geo as a dataset (`citesAsDataSource`, locator
`geo <id>`). The gazetteer is a draft at an `example.org` address: this is a test, not a
publication.

| TM field | PLATO | Notes |
|---|---|---|
| `standard_name` | a name, `formStatus` Headword | TM's own filing form, so no date |
| `latin_name`, `greek_unicode`, `coptic_unicode` | names, `la`; `grc`, `Grek`; `cop`, `Copt` | variants split on ` - `, brackets and `var.`/`fem.` |
| `egyptian_unicode` | names, language `egy-Latn-t-egy-egyd`, script `Latn`, transliteration system "Egyptological transliteration" | Demotic, known only in transliteration: a toponym in its own right (#21) |
| `ethnicon` | names with `nameType` demonym | the inhabitants, not the place: not a toponym |
| `full_name` | its bracketed modern name, as a name with no date | the rest repeats country, region and name |
| `?` on a name or a status class | `certaintyLevel` LessCertain | the form as written stays in `sourceLabel` |
| Leiden brackets (`A.[ ]`, `Abat[ ]`, `Ab( )`) | `transcriptionCompleteness` NonReconstructable, or Reconstructable where letters are supplied | |
| `country` 'ghost name' (661) | kept; every name `transcriptionAccuracy` TranscriptionFalse | PLATO's own definition: "a ghost form, kept because it circulates" |
| `status` | one type per `;` segment: the class as label, the segment in `sourceLabel` | 2,041 distinct values; no AAT mapping attempted |
| `status` "people" (1,815 records) | the record is the land the people lived in (#22): the type's label is "land of a people" with "people" in `sourceLabel`, and every name but the demonyms has `nameType` toponym and ethnonym | a people is not a SpatialEntity; TM has no separate record of the people to point at, so `HomelandOf` is not used |
| `coordinates` (lat,lon) | a GeoJSON Point (lon, lat), the text in `sourceLabel` | the inverse requires the two to agree |
| `location` | one attestation per position the words give (#19), each carrying the words in full: "near X" a geometry Near X, one per TM place named; "in X" a ContainedIn relation to X, or by `relatedLabel` alone where X is no TM place (#18); a bearing or a distance a geometry with one anchor; "between X and Y" a geometry BetweenXAndY with `relativeTo` both (#19); other words naming one TM place a geometry anchored on it; a clause ending `?` LessCertain | clauses are split on `;`, `, ` and, where two or more TM places are named, ` and `; a clause of bare place names continues the one before it. Words that give no position stay a geometry of words alone |
| `begin_date`, `end_date`, `*_fmt` | one attestation with only a timespan, `timespanRole` EvidenceSpan (#20): `-0399` for BC 399, the written form in `sourceLabel`; 0 is no date | the span of the documents, not the dates of the place or of any name |
| `province`, `nomos_code` | ContainedIn relations to a place minted for each unit in the test's own namespace (190 units), each unit with a name attestation of the name or code as TM gives it, citing the first record that names it (#18) | TM gives these as names and codes; they recur, so they are minted rather than named alone |
| `nomos_code` with `?` (1,264) | the relation's attestation is LessCertain | doubt about the code is doubt about the containment (#16, finding 6) |
| `country` | `ccodes`, by WHG's own `COUNTRY_TO_CCODE` | |
| `country` with `?` (1,674) | a ContainedIn relation by `relatedLabel` alone, LessCertain, and no `ccodes` | a bare code would drop the doubt (#16, finding 6) |
| `georelations` | identity relations, `unspecified`, for Pleiades, GeoNames, Wikidata (Wikipedia slugs resolved to Wikidata) and Syriaca | the rest have no address to point at |
| `region` | not mapped | a code in Egypt, the province elsewhere |

## Results (8 April 2026 dump, PLATO 7720890)

- **Valid:** all 64,857 records, with 190 minted units: 65,047 places and 304,195 attestations
  (266,911 in the first pass: the windows are now attestations of their own, and a location
  gives one per position). PLATO tools (3b10811) finds no problems.
- **Through RDF and back** (3,975,499 triples): `plato-tools compare` finds all 304,195
  attestations unchanged.
- **Back to TM's fields** (`plato2tm.py`): every carried field matches in every record that has it:
  - standard name 64,857;
  - country 64,196 (62,260 as `ccodes`, 262 with no code to give, 1,674 as a doubted relation);
  - province 64,141;
  - location 33,783;
  - dates 26,522;
  - status 25,294;
  - coordinates 24,538;
  - identity links 19,943;
  - Greek 14,705;
  - Latin 13,496;
  - nome 10,884;
  - ethnicon 4,966;
  - Egyptian 2,026;
  - Coptic 658;
  - minted units 190 of 190, each with the right name, first record and count.
- **What the rulings added, on real data** (records of 64,857 unless said otherwise):
  - #18: 9,377 records contain a region named with nothing to point at ("in the Delta"), as
    9,524 ContainedIn relations by `relatedLabel` alone; 190 of 190 minted units carry a name
    attestation.
  - #19: 502 records are "between" two TM places, as 503 geometries with two anchors; 7,545
    records give several positions, as several attestations (8,362 records have ContainedIn
    relations to TM places, 12,928 Near geometries, 4,765 a bearing).
  - #20: 26,522 records have an attestation window, each one attestation with EvidenceSpan and
    nothing else dated.
  - #21: 2,026 records have Demotic names, 3,056 names tagged `egy-Latn-t-egy-egyd`.
  - #22: 1,815 records are peoples: 1,815 types "land of a people", 4,123 names typed toponym
    and ethnonym.
- **Negative controls:** `controls.py` on the sample corrupts one value of each of the 43
  predicates in its RDF (41 in the first pass; `timespan_role` and `notes` are new). The inverse
  catches all 43. It also requires every value carried twice, or derived from another, to agree
  with it: a location's positions must be exactly what its words give, with the words on each;
  a window must be the one attestation with EvidenceSpan; a people's names must be typed; a
  unit must be named as TM names it. A first run of this pass caught 42: an identity link's
  basis was exempted for Wikipedia-resolved links, which it never needed to be, and the hole
  was closed.

## What PLATO could not say, or said only by stretching

The first pass's ten findings, with what became of each. Counts are records, of 64,857, unless
marked as links, of 71,835.

1. **Resolved by #18 (PLATO 0.8.x, 7720890): a relation to something known only by name.** The
   units stay minted, by the ruling, since they recur: 190 places (64,141 records in a province,
   10,884 in a nome), each now with a name attestation citing the first record that names it.
   TM's own nome records exist (statuses such as "district: nomos"), but the code-to-record
   mapping is not in the data.
2. **Resolved by #19 (7720890): "between X and Y" has two anchors.** 502 records say it with
   both. 232 more begin "between" without exactly two TM places ("between the 5th cataract and
   Meroe (3460)") and stay in words. Locations naming several TM places are several attestations:
   7,545 records.
3. **Resolved by #18 (7720890): a location relative to a region that is not a place** ("in the
   Delta", "in the Aegean Sea") is a ContainedIn relation by name alone: 9,377 records. "Near" a
   region that is not a TM place (630) still has nowhere to go but words, since `relativeTo`
   takes an address and a name for an anchor was deferred.
4. **Resolved by #20 (7720890): the dates are an attestation window.** One attestation per record
   with `timespanRole` EvidenceSpan (26,522), and no name is dated.
5. **Resolved by #21 (7720890): Egyptian names known only in transliteration** (2,026) are
   toponyms, tagged `egy-Latn-t-egy-egyd`.
6. **Addressed in the converter, as #16 said (not a PLATO change): doubt about a country or a
   nome code.** A doubted country ("Israel?", 1,674) is a ContainedIn relation by name with the
   doubt on the attestation, and no `ccodes`; a doubted nome code (1,264) puts the doubt on its
   relation's attestation.
7. **Resolved by #22 (7720890): peoples** (1,815) are not SpatialEntities; the records are the
   land each lived in, typed "land of a people" with TM's word kept, their names toponym and
   ethnonym. `HomelandOf`, the new relation type, needs a separate record of the people, which
   TM does not have.
8. **Links to databases with no stable address** (edh 29,483, talbert_peutinger 3,269, wikipedia
   1,363 unresolved, dasi 389, rib 384, and others; links) cannot be identity relations. #16
   says the fix is the converter's, by keeping the addresses TM's GeoRelations service returns;
   WHG's build of `tm_geo.db` kept only the identifiers, so that waits on a rebuild of the
   database, outside this folder.
9. **The sources disagree, in words** ("on the western Nile bank (but according to Plinius / Iuba
   on the eastern Nile bank)", 5). That needs two attestations with AlternativeTo, which needs the
   sources to be named. Not PLATO's to fix.
10. **What the sources are.** Every statement here cites TM Geo itself. The texts that are TM's
    real evidence are not in this dump. #20 says the window is to be computed from them once
    TM's GeoRef table can be had.

Still in words, and not raised: 97 locations offer alternatives ("in L00 Alexandria (100)? or in
U08 Ptolemais Hermeiou (2023)?"); 312 bearings and 226 distances have no single TM place to
measure from; 126 name several TM places in words that are not a position ("the harbour of
Kyrene (1201) at the Mediterranean (47793)").

Found in passing, not about PLATO (checked with the indexing session):
- **Countries with no code.** WHG's `COUNTRY_TO_CCODE` (`authorities/trismegistos/places.py:38`)
  has no entry for 82 records, of the 272 without a code; the other 190 are 'unknown' or 'N/A'.
  The 82, in full:
  - Luxemburg 50, Congo? 7, Vatican 5;
  - South Sudan, Liechtenstein, Estonia 3 each;
  - Kenya, Mozambique?, Bangladesh 2 each;
  - Mozambique, Monaco, Iceland, Mauritania, Guinea? 1 each.

  (In this pass the ten doubted ones are relations by name, so the converter reports 262.)
- **Odd values in `greek_unicode`.**
  - 'a' (tm 10) is a placeholder, which `places.py:296` already skips.
  - 'templum Mercurii' (66146) and 'mons Arueri' (66147) are Latin in the Greek field. This
    conversion leaves all three out.
  - '[ ]' (8783) and '.[ ]' (11654) are pure lacunae. They are kept here as Greek names that
    cannot be reconstructed; WHG indexes them as toponyms with no letters.
