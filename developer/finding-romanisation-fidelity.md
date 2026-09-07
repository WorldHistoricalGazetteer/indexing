# Should 722,044 recovered romanisations get English-phonology IPA?

> **Measured 7 September 2026.** Code: `evaluation/romanisation_fidelity.py`
> plus the `kk` ceiling probe. Raw:
> `/vast/ishi/ipa-v8/logs/romanisation_fidelity.json`, `kk_ceiling.json`.
> **Recommendation: NO.**

## The question

`extract_namespace`'s #250 recovery restored ~775k romanised forms. Seven
languages have **no same-language Latin Epitran mode** (`zho-Latn`, `fas-Latn`,
`jpn-Latn`, `ell-Latn`, `rus-Latn`, `kor-Latn`, `ara-Latn` are all absent from
the 218 installed), so the only way to give them IPA is to impose a *different*
language's Latin phonology — in practice `eng-Latn`. `kk` is the exception:
`kaz-Latn` **is** installed, so its 47,877 rows route correctly and need no
decision.

This is not a search question — Symphonym embeds the name string, so these
forms are already KNN-searchable without IPA. It is a **labelling** question:
IPA feeds training-pair selection and the PanPhon space, so a wrong choice puts
a systematically wrong label inside the corpus the next model is measured with.

## 🛑 The ceiling, from the one language where both readings exist

`kk` has a Cyrillic native script **and** an installed `kaz-Latn`, so the same
romanised strings can be read two ways and the only variable is which phonology
reads them. 3,721 triples, every one scored under all three readings:

| reading | mean PanPhon cosine distance to the native form |
|---|---:|
| **CEILING** — `kaz-Latn`, the correct mode | **0.1205** |
| **TREATMENT** — `eng-Latn`, imposed | **0.2756** |
| **FLOOR** — a random other romanisation | **0.5062** |

**Imposing English gets 59.8% of the way from random to correct, and more than
doubles the distance** (0.1205 → 0.2756, a penalty of +0.155).

So the substitution is not worthless — it is comfortably better than noise —
but it discards about 40% of the available signal on the language where it
should do *best*: Kazakh's Latin orthography is broadly phonemic and closer to
English conventions than Persian, Arabic or Chinese romanisation.

## The seven languages, against the floor only

No ceiling is available for these (no correct Latin mode exists — that is the
whole problem), so only the distance from random can be measured.

| lang | n | treatment | floor | signal recovered vs floor |
|---|---:|---:|---:|---:|
| el | 206 | 0.2205 | 0.3967 | **0.444** |
| ja | 427 | 0.2910 | 0.5123 | **0.432** |
| ru | 191 | 0.2873 | 0.4965 | **0.421** |
| ko | 220 | 0.3768 | 0.5175 | 0.272 |
| ar | 143 | 0.3335 | 0.4395 | 0.241 |
| fa | 1,636 | 0.4291 | 0.5385 | 0.203 |
| **zh** | 3,000 | 0.4354 | 0.5225 | **0.167** |
| *(kk, for scale)* | *3,721* | *0.2756* | *0.5062* | *0.456* |

⚠ **Reported per language and never pooled** — a pooled mean would hide a
2.7× spread between `el` and `zh`, which is the split the recommendation turns
on.

**`kk` at 0.456 is the best of the set, and even it recovers only ~60% of what
its correct mode achieves (0.762 vs floor).** Every one of the seven scores
*below* `kk`, and the two largest populations score worst: `zh` (467,161 rows)
at 0.167 and `fa` (97,045) at 0.203. The languages that need the substitution
most are the ones it serves worst.

## Recommendation: do not impose `eng-Latn`

- On the only language with ground truth, it loses ~40% of achievable signal.
- The seven candidates all score below that language, `zh` by nearly 3×.
- The cost of being wrong is a wrong *label*, not a wrong search result — the
  hardest defect class here to detect, and one this campaign has already been
  caught by (an Epitran-derived labeller evaluating an Epitran-taught model).
- ⚠ It would also manufacture more of the romanisation shortcut that already
  distorts evaluation: ~35% of CJK↔Latin positives romanise to identical
  strings, so feeding English-phonology IPA for romanised forms risks scoring
  the model well for exploiting an artefact.

✅ **Ship `kk` (47,877 rows)** — a same-language mode exists, nothing is
imposed, and the derived router already routes it. The hand-written map lost
these purely by having no `(kk, LATIN)` entry.

**Leave the 722,044 as `no_route`.** They are searchable now and they are
honestly labelled as having no reading, which is recoverable. A wrong reading
is not.

⚠ The decision to alter corpus semantics is SG's; this is the measurement and a
recommendation, not a change. 722,044 rows is ~1% of the corpus and 100% of the
recovered romanisations for those seven languages.

## Method notes

- The `kk` probe **cannot go through `IPAConverter.to_ipa`**: the shipped
  45-entry map has no `kk` entry in *either* script, so the first attempt
  scored **0 of 3,757 triples**. The language chosen because it has a correct
  Latin mode is one the shipped converter cannot read at all. Modes are
  resolved through the derived router and Epitran called directly, and the
  resolved modes (`kaz-Cyrl`, `kaz-Latn`) are printed so the reading each
  vector represents is on the record.
- **The join was bounded, not merely lucky.** Reported per run: `nat` 8,961
  rows, `lat` 51,257 rows, **0 bytes of DuckDB temp in use**. The ~200M-row
  `toponym_attestations` table is never a probe side. This mattered after three
  successive spill failures, each fixed correctly for its own variant and each
  moving the trap rather than removing it.
