# This directory intentionally leads `zenodo/epitran_extensions/`

**Do not "re-sync" the two directories.** They are not two copies of one thing.

| directory | role | changed by |
|---|---|---|
| `phonetics/epitran_extensions/` | **what the pipeline installs.** `scripts/install_epitran_extensions.sh:11` reads this path and copies the CSVs into Epitran's map directory. | ordinary defect repair |
| `zenodo/epitran_extensions/` | **what WHG publishes and cites.** Carries `LICENCE.md`, no tooling. | a deliberate deposit |

"In use" and "endorsed" are different claims, and they have separate homes so that
fixing an operational defect does not silently assert that a value has been
reviewed. ⚠ **A script that copied one directory over the other would collapse that
distinction and publish unreviewed values.** Nothing does so today — checked
7 Sep 2026: no file outside `developer/` references `zenodo/epitran_extensions`
at all — and anything that starts to must decide this question explicitly first.

## Files that currently lead the deposit

| file | why | measured |
|---|---|---|
| `pan-Guru.csv` | the deposited map has **0 of 10 Gurmukhi independent vowels**, so a Punjabi name beginning with a vowel loses it | 44 → 68 rules, **24 added, 0 removed** |
| `sin-Sinh.csv` | the deposited map has **0 of 18 Sinhala independent vowels**, same defect | 52 → 81 rules, **29 added, 0 removed** |

Both promotions are **purely additive** — no existing rule is changed or removed —
so they cannot regress a row that transcribes correctly today. The added values are
**drafted and not yet reviewed by a speaker**; they are here because a missing vowel
is a defect rather than a stylistic preference, and a measurably better map beats a
measurably broken one *in the operational path*. The endorsement question stays open
in `developer/epitran-drafts/REVIEW.md`, and the deposit does not move until it is
answered.

⚠ **The mechanism behind both, which predicts where else to look:** a rule-writer
working from a **consonant chart** never meets the independent vowels, because the
chart does not show them. That predicts the defect in **abugidas** specifically and
not in alphabets — and the measured pattern matches. Shipped sets still missing all
of theirs: `bpy-Beng` 0/12, `guj-Gujr` 0/14, `khm-Khmr` 0/17, `nep-Deva` 0/17,
`new-Deva` 0/17.
