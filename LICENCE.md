# Licensing

**This repository is not one licensable thing**, and a single grant at the root would
misdescribe most of it. What follows states what is settled, what is not, and one
exclusion that must never become untrue.

## Component map

| component | status |
|---|---|
| **`zenodo/epitran_extensions/`** — grapheme→IPA rule sets | ✅ **Contributions under CC0 1.0.** WHG proceeds on the basis that such rows attract no rights, and the CC0 dedication exists so nothing turns on it. ⚠ The v7 Zenodo record states CC BY 4.0 at the record level; that stands for v7 and is to be revisited for v8. See [`zenodo/epitran_extensions/LICENCE.md`](zenodo/epitran_extensions/LICENCE.md). |
| **Model weights** (`hf/model.safetensors`) | ⚠ **Published CC BY 4.0** within the Zenodo deposit (DOI [10.5281/zenodo.18682017](https://doi.org/10.5281/zenodo.18682017), 2026-02-22). The HuggingFace licence field must be checked to match; if it does not, that is an inconsistency to resolve, not an open choice. |
| **WHG's own code** | 🛑 **NOT YET DECIDED.** No licence is asserted here. `WorldHistoricalGazetteer/whg3` uses BSD 3-Clause scoped to "WHG's own code", which is offered as precedent only. |
| **Third-party dependencies** | Unchanged, under their own terms. Nothing here alters them. |
| **Ingested authority data** | Not licensed by WHG; see below. |

## Ingested authority data is not ours to license

`processing/settings.py` records a `license_spdx` per authority. Twelve distinct terms
are currently declared, including `CC0-1.0`, `CC-BY-4.0`, `CC-BY-SA-4.0`,
`ODbL-1.0`, `ODC-By-1.0` and four bespoke `custom-*` terms — **and `CC-BY-NC-4.0` and
`CC-BY-ND-4.0`.**

⚠ **The last two are named rather than smoothed over.** This project's outputs are
derivatives by construction: toponyms are extracted, transcribed and embedded. An
**ND** source is therefore a genuine unresolved question, not a labelling detail, and
a **NC** source constrains commercial reuse of anything derived from it. **This file
does not resolve either**, and no statement here should be read as a grant over data
WHG did not create.

Per-source terms are surfaced to consumers through the gazetteer registry and the
`namespaces[]` field of API responses. **Ingestion is deliberately not gated on
licence** — attribution is handled at the point of use.

## 🛑 EXCLUDED CONTENT — must never enter this repository

Some material used by this project is cleared for **acquisition and research use
only**, and **not for publication or redistribution**. It is held outside the
repository and must stay there.

**Currently excluded:** the RCAHMW Long-Held Place Names (LHPN) Welsh/English
name-variant data, held at `/vast/ishi/lhpn` with its own licence notice. Cleared
verbally for acquisition and model training; **publication and redistribution are NOT
cleared.**

⚠ **Nothing in this file grants any licence over excluded content, and no permissive
statement here should be read as a reason to commit it.**

✅ **This is enforced mechanically, not by prose.** `tests/test_no_restricted_content.py`
fails on any path or file content matching the restricted set. **An exclusion that
only exists in a document is honoured until the first person who has not read it** —
if you are adding restricted material, that test is what should stop you, and if it
does not, the test is the defect.

## Attribution

> World Historical Gazetteer, *Symphonym: Universal Phonetic Embeddings for
> Cross-Script Name Matching — Models and Evaluation Data*. Zenodo.
> https://doi.org/10.5281/zenodo.18682017

## Open questions for the project owner

1. **The code licence** for this repository. Not decided; BSD 3-Clause is `whg3`'s precedent.
2. **Which NEH awards funded which components.** ✅ The *terms* are settled: NEH General Terms §XI.B–C (2 CFR §200.315–316) leave intangible property with the recipient while the Federal Government retains a non-exclusive, royalty-free, irrevocable licence to reproduce, prepare derivative works from, publish and use it. **Open licensing is a superset of what the Government reserves, so this constrains nothing above.** Which awards funded which parts is not known here and is not inferred.
3. **The deposit-level statement for v8.** The v7 Zenodo record is CC BY 4.0 at the record level while the rule sets within it are dedicated CC0. CC0 is the more permissive, so a recipient faces no conflict — but the relationship should be **stated at the deposit for v8** rather than left to be inferred.

✅ **Resolved and removed:** `whg3`'s `NOTICE.md` previously read as applying **CC BY-NC 4.0** to contributed datasets. ⚠ **The error was scope, not licence** — NC is WHG's *curation overlay*, asserted alongside each source's own terms, and applying it to other people's material was self-defeating besides, since CC BY 4.0 §2(a)(5)(B) forbids imposing downstream restrictions on material WHG received under it. Corrected in `whg3` (`080bc72c4`).
