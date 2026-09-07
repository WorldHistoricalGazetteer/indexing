#!/usr/bin/env python3
"""Accent folding in query derivation.

Run package-qualified: python -m unittest tests.test_accent_folding

The decisive test is `test_valparaiso_finds_valparaiso`: it fails against the
pre-change tree, because `derive_name_forms` returned [] for a query with no
bracket and no comma, so an unaccented query had no path to an accented name.
"""
import unittest

from gateway.es_helpers import (
    DERIVED_LOSSY_WEIGHT, MAX_DERIVED_FORMS, VARIANT_SCORE_WEIGHT,
    derive_name_forms, derived_form_weight, fold_accents,
)


class TestFoldAccents(unittest.TestCase):
    def test_strips_diacritics_keeps_letters(self):
        self.assertEqual(fold_accents("Valparaíso"), "Valparaiso")
        self.assertEqual(fold_accents("Áspra Spítia"), "Aspra Spitia")
        self.assertEqual(fold_accents("Fāshān"), "Fashan")
        self.assertEqual(fold_accents("Zürich"), "Zurich")

    def test_leaves_unaccented_text_alone(self):
        # The negative control: if this changed anything, every query would
        # gain a spurious derived form.
        for s in ("Manchester", "Bury St. Edmunds", "", "北京", "Москва"):
            self.assertEqual(fold_accents(s), s, s)

    def test_does_not_transliterate(self):
        """The scope boundary. Folding must not romanise: full query
        romanisation is NOT authorised, and a change that quietly did both
        would ship an unmeasured widening."""
        self.assertEqual(fold_accents("北京"), "北京")
        self.assertEqual(fold_accents("Москва"), "Москва")
        self.assertEqual(fold_accents("القاهرة"), "القاهرة")


class TestDerivation(unittest.TestCase):
    def test_valparaiso_finds_valparaiso(self):
        """THE case this change exists for, and the one that will be quoted.

        Before: a query with no bracket and no comma produced NO derived forms,
        so `Valparaiso` had no path to the indexed `Valparaíso`. Fails against
        the pre-change tree."""
        forms = derive_name_forms("Valparaíso")
        self.assertIn("Valparaiso", forms)

    def test_unaccented_query_gains_nothing(self):
        # Must stay empty: a derived form per query is another concurrent KNN
        # pass, and the overwhelming majority of queries have no diacritic.
        self.assertEqual(derive_name_forms("Manchester"), [])

    def test_folded_form_comes_first_under_the_cap(self):
        """It is the only LOSSLESS derivation, so a cap that bites must not
        drop it in favour of a speculative bracket reading."""
        forms = derive_name_forms("Áspra (Spítia), Attica")
        self.assertTrue(forms)
        self.assertEqual(fold_accents("Áspra (Spítia), Attica"), forms[0])
        self.assertLessEqual(len(forms), MAX_DERIVED_FORMS)

    def test_existing_bracket_and_comma_behaviour_survives(self):
        # Regression: the accent branch must not disturb queries it does not
        # apply to.
        self.assertIn("Broxbourn", derive_name_forms("Broxbourn (St. Augustine)"))
        self.assertIn("Bury St. Edmunds",
                      derive_name_forms("Bury St. Edmunds, Suffolk"))


class TestWeighting(unittest.TestCase):
    def test_pure_fold_scores_as_a_full_variant(self):
        """An accent fold discards no token. Comparing raw casefolds would
        call it lossy and discount it as though a qualifier had been dropped."""
        self.assertEqual(derived_form_weight("Valparaíso", "Valparaiso"),
                         VARIANT_SCORE_WEIGHT)

    def test_a_genuinely_lossy_form_is_still_discounted(self):
        # The control that keeps the change honest: without it, the fold-aware
        # comparison could have made everything full-weight.
        self.assertEqual(derived_form_weight("Bury St. Edmunds, Suffolk",
                                             "Bury St. Edmunds"),
                         DERIVED_LOSSY_WEIGHT)

    def test_weight_clears_the_tier_ordering_floor(self):
        """LEXICAL_EXACT_BOOST x weight must exceed LEXICAL_FUZZY_BOOST + 1.0,
        i.e. weight > 0.7, or an exact hit on a derived form could rank below a
        near-miss on the original."""
        self.assertGreater(derived_form_weight("Valparaíso", "Valparaiso"), 0.7)


if __name__ == "__main__":
    unittest.main()
