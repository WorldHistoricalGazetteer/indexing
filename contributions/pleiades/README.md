# Pleiades: the ancient world gazetteer

**Status (27 September 2026): converted and round-tripped as a PLATO conformance test. Not
ingested, and not an ingestion route.** Pleiades is an *authority* in WHG's sense, staged by
`authorities/`, not a contribution. The converter is here beside the other PLATO assessments
because that is where the method and the driver live; nothing in this folder writes to
Elasticsearch, the geom store, or whg3.

## Source

| | |
|---|---|
| What | Pleiades daily places dump, `@graph` of place records |
| Where | https://atlantides.org/downloads/pleiades/json/pleiades-places-latest.json.gz |
| Version | fetched 2026-09-27, `Last-Modified: Sun, 27 Sep 2026 11:10:05 GMT`, 135,950,549 bytes |
| sha256 | `58856c1f5a52ec9980bbe2c972fbdb6cea2e3337a243eac9c4726a79f6a995a8` |
| Licence | CC BY 3.0 |

Measured from that file: 42,361 places, 44,079 names, 45,309 locations, 14,995 connections,
218,324 references, 68,464 name-level period attestations.

## Converting

```
python3 pleiades2plato.py forward pleiades-places-latest.json.gz pl-plato.json
python3 pleiades2plato.py inverse <roundtripped .json|.jsonl> pl-back.json
python3 pleiades2plato.py compare pleiades-places-latest.json.gz pl-back.json
```

Forward → `plato_run.mjs convert ntriples` → `convert plato-jsonl` → inverse → compare.

**Convert in chunks.** The whole corpus as one document does not complete: it was still running
after 61 minutes at 101% CPU and 3 GB resident with zero disk IO, because `plato_run.mjs` opens
SQLite with `memory: true` while the browser app uses OPFS. Split into 15 chunks of 3,000 places
and the same work takes 8 minutes. Chunking is safe here because the profile is place-centric and
identity relations are nested, so nothing crosses a record boundary.

## Result

42,361 entities, 227,176 attestations, 4,676,894 triples. Schema-clean.

**1 difference across 363,703 assertions**, and it is a tooling bug rather than a conversion
error: place 585129's attested name begins with U+FEFF and loses it in JSON to N-Triples, while
place 462204's trailing U+FEFF survives. A minimal document with leading, trailing and middle BOMs
loses all three, which does not match the corpus behaviour, so the loss is reproduced but the rule
is not isolated.

Negative controls, each planted in the N-Triples and each caught: a toponym, a citation function,
a geometry.

## What the conversion could not say

Three things Pleiades records that PLATO has no slot for. Each is parked in a PropertyValue, which
carries it through a round trip and asserts it of the *place* when it is really about the reading
or about why a source was cited. A lossless round trip is not the same as a faithful model.

* **Citation function.** 218,324 references, every one typed: `seeFurther` 136,758,
  `citesAsRelated` 33,144, `citesAsDataSource` 30,312, `citesAsEvidence` 12,483, `seeAlso` 2,913,
  `cites` 2,714. 176,502 also carry a locator, which PLATO does hold. `plato:Citation` has a
  locator and no function, so "this is the evidence" and "see this for more" become one edge.
* **Transcription quality.** 44,079 names carry `transcriptionAccuracy` (inaccurate 357, false 10)
  and `transcriptionCompleteness` (reconstructable 989, non-reconstructable 6).
* **Inferred confidence.** 68,464 name attestations carry one of four confidences, two of which say
  the confidence was itself inferred: `confident-inferred` 282, `less-confident-inferred` 76.

Two further findings are layers out of step rather than absences:

* **IRIs.** 1,410 citations carry a non-ASCII source identifier, 797 distinct (`#André-1980`,
  `#Sillières-1990`, `#Waşowicz-1975`). The ontology ranges these `xsd:anyURI`, whose value space is
  IRI references; the JSON Schema types them `format: uri`, which is ASCII-only. `safe_uri()`
  percent-encodes them, which validates and changes the identifier.
* **Deep time.** `startEarliest`/`endLatest` are `^-?\d{4}`, so no year before 10000 BCE fits.
  Pleiades has 199 such endpoints across 169 places, down to 2,600,000 BCE, including Neolithic
  Knossos. They go in `edtfString`, which keeps them and hides them from anything filtering the
  structured fields.

## Pleiades data quality met on the way

Not PLATO's problem, but a converter has to handle them, and `safe_uri()` does:

* 450 place-type URIs contain a raw space (`.../place-types/numbered feature`).
* 29 references carry a URI with no scheme or a leading space; 14 more contain a quote, tab or
  bracket; 2 Google Books URLs are truncated mid-escape and end in a lone `%`.
* Language values that are not BCP-47: the literal string `'None'` (16), `asyr` (15), `cana` (10),
  `etruscan-in-latin-characters` (9), `osar` (6), `arbd` (2). Well-formed tags go in `language`;
  the rest are kept as a PropertyValue so nothing is dropped silently.
* One `citationDetail` is an integer where a string is required.

## Open questions

* Whether the citation function belongs on `plato:Citation` as a typed relation, which would also
  house the markets corpus's `evidence_kind`.
* Whether `plato:Preferred` should be named in the JSON Schema's `formStatus` description, which
  currently lists only Headword, Normalised and Reconstructed.
* Pleiades does not mark exclusivity, so it cannot say whether its 3,775 places with more than one
  located location, or 9,166 with more than one place type, are alternatives or complements. Its
  model treats them as complements.
