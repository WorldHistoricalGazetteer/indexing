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

| `nep-Deva.csv` | 0 of 17 Devanagari independent vowels | 46 → 63, **17 added, 0 removed** |
| `new-Deva.csv` | same | 46 → 63, **17 added, 0 removed** |
| `guj-Gujr.csv` | 0 of 14 Gujarati independent vowels | 46 → 60, **14 added, 0 removed** |
| `bpy-Beng.csv` | 0 of 12 Bengali independent vowels | 49 → 61, **12 added, 0 removed** |
| `bod-Tibt.csv` | 0 of 45 subjoined consonants — ordinary Tibetan orthography | 34 → 84, **50 added, 0 removed** |

**Verified against the affected rows, not against the rule count:**

| file | rows affected | before | after |
|---|---|---|---|
| `nep-Deva` | 3,799 | 0% letter-clean | **100%** |
| `new-Deva` | 7,292 | 0% | **100%** |
| `guj-Gujr` | 6,103 | 0% | **100%** |
| `bpy-Beng` | 11,004 | 0% | **94%** |
| `bod-Tibt` | 9,590 | 16,730 subjoined characters surviving | **0** |

⚠ **A correction to an earlier claim about `bod-Tibt`**: its 9,866 rows with no IPA
at all were a ROUTING failure already counted in Project A's 172,210 — they are
stored as `script='OTHER'` from before the enum split and route the moment the
store is rebuilt. The rule work here addresses QUALITY only, and describing it as
the worst routing case double-counted it across two ledgers.

**Provenance of the added values, because it differs by file and changes how much
weight each carries:**

* **Devanagari** — ten of the seventeen are TRANSPLANTED from `bho-Deva`, which
  already had them for the same script and characters (`अ→ə`, `आ→aː`, `ए→eː`,
  `ऐ→ɛː`, `ओ→oː`, `औ→ɔː`…). Those are not drafts. The remaining seven (vocalic
  `ऋ ऌ`, candra `ऍ ऑ`, short `ऄ ऎ ऒ`) are drafted. ⚠ `अ` is `ə` following the
  sibling; **Nepali is closer to /ʌ/** and that is a reviewer question.
* **Tibetan** — DERIVED, not drafted. A subjoined letter sits exactly `+0x50`
  above its base, so each of the 30 takes its own base row's value. ⚠ **This is
  correct only because the file is GRAPHEMIC** — measured: `ཀ་མདོ་` → `k་mdo་`,
  letter-for-letter. In a *phonemic* Tibetan map it would be wrong, since
  subjoined ra retroflexes the root and subjoined ya palatalises it rather than
  adding a segment. Same mechanical rule, right in one register and a defect in
  the other.
* **Gujarati and Bengali** — drafted; no sibling map exists for either script.
  Bengali deliberately has NO length contrast (`ই` and `ঈ` both `i`, `উ` and `ঊ`
  both `u`), unlike Devanagari where the source register marks it.

⚠ **ONE DERIVED ROW WAS CORRECTED RATHER THAN COPIED FAITHFULLY.** The `+0x50`
rule reproduced `ག → g` (ASCII U+0067) as `ྒ → g`. PanPhon rejects ASCII `g`, so
that value silently drops the /g/ — the derivation was faithful and wrong. The
derived row takes `ɡ` (U+0261); **its base still carries the defect**, so the two
disagree until the ASCII-`g` sweep lands. Each row is individually correct, which
is the better inconsistency. ⚠ The same pre-existing defect remains in
`new-Deva`, `guj-Gujr`, `bpy-Beng` (`ग`/`ગ`/`গ → g`) and every file here still
carries `्ANY → ∅` (U+2205); **neither was touched, because mixing a known sweep
into an additive change would cost the property that makes this safe.**

⚠ **The mechanism behind both, which predicts where else to look:** a rule-writer
working from a **consonant chart** never meets the independent vowels, because the
chart does not show them. That predicts the defect in **abugidas** specifically and
not in alphabets — and the measured pattern matches. Shipped sets still missing all
of theirs: `bpy-Beng` 0/12, `guj-Gujr` 0/14, `khm-Khmr` 0/17, `nep-Deva` 0/17,
`new-Deva` 0/17.
