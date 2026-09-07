# Licence — WHG Epitran rule sets

These files are grapheme→IPA rule sets in Epitran's `Orth,Phon` map format, written
at the World Historical Gazetteer to cover languages and scripts the upstream Epitran
distribution does not.

## Grant

**Dual, and the two halves apply to different outlets.**

**Both licences are granted to everyone.** Anyone receiving this content may rely on
whichever of the two suits what they are doing.

The table records **which grant WHG relies on for which outlet** — it is not a limit
on what a recipient may rely on:

| | licence | WHG relies on it for |
|---|---|---|
| **As a dataset** | **CC BY 4.0** | this directory as a whole, as published in WHG's citable Zenodo deposit |
| **As an upstream contribution** | **MIT** | rows WHG contributes to the Epitran project |

⚠ **A public MIT grant cannot be scoped after the fact.** Once a row is in Epitran
under MIT it is MIT to everyone, and Epitran's users and packagers rely on exactly
that. An outlet-scoped grant would fail upstream for the same reason a CC-BY-only one
would.

**Why the split, and it is a property of the format rather than a preference.**
An Epitran map file is a bare two-column CSV with **no comment convention** — there
is physically nowhere in the file for an attribution or licence notice to live. CC BY
4.0's single substantive condition therefore cannot be delivered by the artefact that
travels. Separately, Epitran operates no CLA and no DCO, so the inbound licence for a
pull request is GitHub's Terms of Service §D.6 — *"you license that Content under the
same terms, and you agree that you have the right to license that Content under those
terms"* — and a contribution carrying CC-BY-only rows would make that warranty false
against an MIT project. Either finding alone settles it.

## Attribution

> World Historical Gazetteer, *Symphonym: Universal Phonetic Embeddings for
> Cross-Script Name Matching — Models and Evaluation Data*.
> Zenodo. https://doi.org/10.5281/zenodo.18682017

This directory was published in that deposit on 2026-02-22 under CC BY 4.0. **The
licence stated here matches the published record; it does not create a new one.**

## Relationship to Epitran

Epitran (David R. Mortensen et al.) is MIT-licensed, and these files use its map
format and are loaded by it.

**They are not derived from Epitran's own map files.** Five share a filename with an
upstream map, because the name is determined by the language and script rather than
chosen — and comparison against Epitran 1.35.2 shows independent authorship rather
than copying:

```
             upstream rows   ours   identical rows
kat-Geor          33          66          23
khm-Khmr          33          55           0
mya-Mymr         136          48           4
pan-Guru          62          44           5
sin-Sinh          80          52          14
```

**`khm-Khmr` shares no row at all**, and `mya-Mymr` is a third the size of the
upstream file it would supposedly be copying — neither is a copy-with-edits shape in
either direction.

Where values do agree, we regard convergence as the better explanation than copying.
There is essentially one way to express *"this letter makes this sound"*, and the
compilation's selection (all letters of the script) and arrangement (codepoint order)
are both determined rather than chosen. ⚠ **On that reasoning the percentages are
corroboration rather than the argument** — even complete agreement would not imply
copying, because there would be nothing chosen in the row to copy. Georgian is a poor
case for a percentage argument in particular: its orthography is close to phonemic, so
independent transcribers should converge heavily.

⚠ **This is reasoning, not a legal opinion.** Whether these rows attract copyright or
database rights at all is an open question, and nothing here should be read as
settling it.

## Status of the content

⚠ **These rule sets are not authoritative.** Measurement in September 2026 found
that some convert only a small minority of real toponyms into usable transcriptions,
and that defects are present across a substantial minority of the files. Figures and
methods are recorded in the project's issue tracker rather than restated here, since
they change as the review proceeds.

They are under review by speakers of the languages concerned through the WHG review
interface, and **values here may be wrong**. Corrections are welcome via that
interface.

## Contributions

Contributions to these files are accepted under the dual grant above: **CC BY 4.0**
for inclusion in the Zenodo dataset, **MIT** for any row WHG contributes upstream to
Epitran.

**Credit differs between the two outlets, and the difference is stated rather than
left to be discovered.** In the Zenodo dataset, attribution is a **condition of the
licence** and contributors are credited by name where they wish to be. Upstream, the
CSV format cannot carry a credit line at all, so contributors are **named in the pull
request as a matter of practice** — not as a licence condition, because MIT imposes
none that the file could satisfy.
