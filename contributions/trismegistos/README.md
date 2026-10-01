# Trismegistos Geo in PLATO

**A PLATO conformance test of an authority, not a submission.** Trismegistos Geo (TM Geo) is
indexed by WHG as the authority `tm` (`authorities/trismegistos/`). This folder converts the
whole of it to PLATO JSON, and back, to find what PLATO cannot say about it:
[place-attestation-ontology#16](https://github.com/pelagios/place-attestation-ontology/issues/16).
Nothing here is staged or submitted to WHG.

## Source

| | |
|---|---|
| What | Trismegistos Geo, the geographical file of [Trismegistos](https://www.trismegistos.org/) (KU Leuven) |
| Where | `authorities/trismegistos/tm_geo.db`, read in place and opened `immutable`, so no side files are made. Built by `build_database.py` from `TM_geo.sql`, with links to other gazetteers from TM's GeoRelations service |
| Version | phpMyAdmin dump of the `tm` database, generated 8 April 2026, 09:52 (MySQL 8.0.25); committed 14 April 2026 (ce6d838) |
| Checksums | `TM_geo.sql` sha256 `9743cd5873bb89c3e18cba19c2a6ee1d167564a35c87495563da9490fa65a6d5`; `tm_geo.db` sha256 `a92c4d00de7159012e0cea8579a5c12b06b7e3f9e15b134665e8c0ac5b5fc360` |
| Licence | CC BY-SA 4.0, as recorded in `processing/settings.py` (verified at trismegistos.org/dataservices, 6 June 2026). Rights: Trismegistos / KU Leuven |
| Not here | The links between texts and places, which hold TM's actual evidence. TM has them; whether it publishes them is a question for TM |

## Running it

```bash
python3 contributions/trismegistos/tm2plato.py --out /tmp/tm/tm-geo.jsonl         # all 64,857 records, about 5 s
PLATO_TOOLS=~/PycharmProjects/plato-tools node contributions/plato_run.mjs /tmp/tm/tm-geo.jsonl check
# through RDF and back, then to TM's own fields:
node ~/PycharmProjects/plato-tools/bin/plato-tools.mjs convert --to ntriples --out /tmp/tm /tmp/tm/tm-geo.jsonl
node ~/PycharmProjects/plato-tools/bin/plato-tools.mjs convert --to plato-jsonl --out /tmp/tm/back /tmp/tm/tm-geo.nt
python3 contributions/trismegistos/plato2tm.py /tmp/tm/back/tm-geo.jsonl            # exit 1 if any carried field differs
# negative controls, on a sample of every feature (--sample 3: 46 records):
python3 contributions/trismegistos/tm2plato.py --sample 3 --out /tmp/tm/ctl/sample.jsonl
PLATO_TOOLS=... node contributions/plato_run.mjs /tmp/tm/ctl/sample.jsonl convert ntriples /tmp/tm/ctl/sample.nt
PLATO_TOOLS=... python3 contributions/controls.py /tmp/tm/ctl/sample.nt /tmp/tm/ctl/work -- bash -c \
  "node plato_run.mjs {nt} convert plato-jsonl {out}.jsonl >/dev/null 2>&1 && python3 trismegistos/plato2tm.py --only-present {out}.jsonl"
```

Every count `tm2plato.py` reports gives its denominator: records (64,857) or links (71,835).

## Mapping

One SpatialEntity per TM Geo record, at `https://www.trismegistos.org/place/<id>`, with TM's id as
its `entityIdentifier`. Every attestation cites TM Geo as a dataset (`citesAsDataSource`, locator
`geo <id>`). The gazetteer is a draft at an `example.org` address: this is a test, not a
publication.

| TM field | PLATO | Notes |
|---|---|---|
| `standard_name` | a name, `formStatus` Headword | TM's own filing form, so no date |
| `latin_name`, `greek_unicode`, `coptic_unicode` | names, `la`; `grc`, `Grek`; `cop`, `Copt` | variants split on ` - `, brackets and `var.`/`fem.` |
| `egyptian_unicode` | names, `egy`, script `Latn`, transliteration system "Egyptological transliteration" | known only in transliteration (see findings) |
| `ethnicon` | names with `nameType` demonym | the inhabitants, not the place: not a toponym |
| `full_name` | its bracketed modern name, as a name with no date | the rest repeats country, region and name |
| `?` on a name or a status class | `certaintyLevel` LessCertain | the form as written stays in `sourceLabel` |
| Leiden brackets (`A.[ ]`, `Abat[ ]`, `Ab( )`) | `transcriptionCompleteness` NonReconstructable, or Reconstructable where letters are supplied | |
| `country` 'ghost name' (661) | kept; every name `transcriptionAccuracy` TranscriptionFalse | PLATO's own definition: "a ghost form, kept because it circulates" |
| `status` | one type per `;` segment: the class as label, the segment in `sourceLabel` | 2,041 distinct values; no AAT mapping attempted |
| `coordinates` (lat,lon) | a GeoJSON Point (lon, lat), the text in `sourceLabel` | the inverse requires the two to agree |
| `location` | a geometry with no coordinates: the words in `sourceLabel`, and `relativeTo` the first TM place it names, with Near, Within, BetweenXAndY, or a bearing and distance | |
| `begin_date`, `end_date`, `*_fmt` | a timespan on each attested-name attestation: `-0399` for BC 399, the written form in `sourceLabel`; 0 is no date | the dates bound the documents, not the place |
| `province`, `nomos_code` | ContainedIn relations to a place minted for each unit in the test's own namespace (190 units) | TM gives these as names and codes |
| `country` | `ccodes`, by WHG's own `COUNTRY_TO_CCODE` | |
| `georelations` | identity relations, `unspecified`, for Pleiades, GeoNames, Wikidata (Wikipedia slugs resolved to Wikidata) and Syriaca | the rest have no address to point at |
| `region` | not mapped | a code in Egypt, the province elsewhere |

## Results (8 April 2026 dump)

- **Valid:** all 64,857 records, with 190 minted units: 65,047 places and 266,911 attestations. PLATO tools finds no problems.
- **Through RDF and back** (3,698,164 triples): `plato-tools compare` finds all 266,911 attestations unchanged.
- **Back to TM's fields** (`plato2tm.py`): every carried field matches in every record that has it:
  - standard name 64,857;
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
  - country 64,196.
- **Negative controls:** `controls.py` on the sample corrupts one value of each of the 41 predicates in its RDF. The inverse catches all 41. It also requires every value carried twice, or derived from another, to agree with it, and the dataset's header to be exactly as written. A first run caught only 23; the 18 holes it showed were closed.

## What PLATO could not say, or said only by stretching

Counts are records, of 64,857, unless marked as links, of 71,835.

1. **A relation to something known only by name.** A relation must name its target by address.
   TM gives provinces and nomes as names and codes, so the test mints 190 places for them (64,141
   records in a province, 10,884 in a nome). TM's own nome records exist (statuses such as
   "district: nomos"), but the code-to-record mapping is not in the data.
2. **"Between X and Y" has two anchors; `relativeTo` holds one** (662 records; 216 more have a
   "between" with fewer than two TM places). More generally, 4,688 locations name more than one
   TM place, and only the first is kept as an anchor; the words keep the rest.
3. **A location relative to a region that is not a place** ("in the Delta", "in the Aegean Sea":
   6,201) can be said only in words.
4. **The dates are an attestation window,** the span of the documents that mention the place
   (26,522 records). PLATO has no term for that. Carried as each attested name's timespan, which
   says a little more than TM does.
5. **Egyptian names are known only in transliteration** (2,026). `toponym` is "the name string in
   its original script", but there is none here.
6. **Doubt about a country** ("Egypt?", 1,674) has no place in `ccodes`. Doubt about a nome code
   (1,264) has no place in a relation.
7. **Peoples** (status "people", 1,686) are not places. They are SpatialEntities of type "people"
   here; whether PLATO should hold them, or point to them as related entities, is a decision.
8. **Links to databases with no stable address** (edh 29,483, talbert_peutinger 3,269, wikipedia
   1,363 unresolved, dasi 389, rib 384, and others; links) cannot be identity relations. A
   Wikipedia article is a record *about* a place, and would be a SubjectOf relation, but its
   language edition is not given.
9. **The sources disagree, in words** ("on the western Nile bank (but according to Plinius / Iuba
   on the eastern Nile bank)", 5). That needs two attestations with AlternativeTo, which needs the
   sources to be named.
10. **What the sources are.** Every statement here cites TM Geo itself. The texts that are TM's
    real evidence are not in this dump.

Found in passing, not about PLATO (checked with the indexing session):
- **Countries with no code.** WHG's `COUNTRY_TO_CCODE` (`authorities/trismegistos/places.py:38`)
  has no entry for 82 records, of the 272 without a code; the other 190 are 'unknown' or 'N/A'.
  The 82, in full:
  - Luxemburg 50, Congo? 7, Vatican 5;
  - South Sudan, Liechtenstein, Estonia 3 each;
  - Kenya, Mozambique?, Bangladesh 2 each;
  - Mozambique, Monaco, Iceland, Mauritania, Guinea? 1 each.
- **Odd values in `greek_unicode`.**
  - 'a' (tm 10) is a placeholder, which `places.py:296` already skips.
  - 'templum Mercurii' (66146) and 'mons Arueri' (66147) are Latin in the Greek field. This
    conversion leaves all three out.
  - '[ ]' (8783) and '.[ ]' (11654) are pure lacunae. They are kept here as Greek names that
    cannot be reconstructed; WHG indexes them as toponyms with no letters.
