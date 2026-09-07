# Licence — WHG Epitran rule sets

These files are grapheme→IPA rule sets in Epitran's `Orth,Phon` map format, written
at the World Historical Gazetteer to cover languages and scripts the upstream Epitran
distribution does not.

## Grant

**Dual, and the two halves apply to different outlets.**

| | licence | applies to |
|---|---|---|
| **As a dataset** | **CC BY 4.0** | this directory as a whole, as published in WHG's citable Zenodo deposit |
| **As an upstream contribution** | **MIT** | any individual rule contributed by WHG to the Epitran project |

Either grant may be relied upon for its stated purpose. They are not alternatives
offered for the recipient to choose between; each names the outlet it exists for.

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

Where values agree they agree convergently: the IPA for a given letter of a public
writing system is a fact about that language, not creative expression, and two
transcribers working independently arrive at the same answer. Note `khm-Khmr` shares
no row at all, and `mya-Mymr` is a third the size of the upstream file it supposedly
copies.

## Status of the content

⚠ **These rule sets are not authoritative.** Several were measured in September 2026
to convert as little as 16.6% of real toponyms usably, and defects have been found in
shipped files at a rate of 81 rows across 38 of 115. They are under review by speakers
of the languages concerned through the WHG review interface, and **values here may be
wrong**. Corrections are welcome via that interface.

## Contributions

Contributions to these files are accepted under the dual grant above: **CC BY 4.0**
for inclusion in the Zenodo dataset, **MIT** for any row WHG contributes upstream to
Epitran. Contributors are credited by name where they wish to be.
