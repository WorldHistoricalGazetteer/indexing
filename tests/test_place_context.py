"""Pure parsing of genealogy place strings (gateway/place_context.py)."""
from __future__ import annotations

import unittest

from gateway import place_context as pc

G = pc.PROFILES["gedcom"]


def norms(parsed):
    return [c.norm for c in parsed.components]


class Normalisation(unittest.TestCase):
    def test_clean_drops_bracketed_qualifiers_and_whitespace(self):
        self.assertEqual(pc.clean("  Broxbourne  (St. Augustine) "), "Broxbourne")
        self.assertEqual(pc.clean("Dun   Laoghaire;"), "Dun Laoghaire")

    def test_fold_is_case_and_punctuation_insensitive(self):
        self.assertEqual(pc.fold("U.S.A."), "usa")
        self.assertEqual(pc.fold("St. Mary’s"), "st marys")

    def test_admin_words_stripped_from_either_end(self):
        for t in ("County Antrim", "Antrim County", "Co. Antrim", "Antrim Shire"):
            self.assertEqual(pc.strip_admin_words(t), "Antrim", t)
        self.assertEqual(pc.strip_admin_words("County"), "County", "never strip to nothing")

    def test_country_aliases(self):
        self.assertEqual(pc.country_of("U.S.A."), ("US", False))
        self.assertEqual(pc.country_of("Deutschland"), ("DE", False))
        self.assertEqual(pc.country_of("Scotland"), ("GB", True), "constituent country flagged")
        self.assertIsNone(pc.country_of("Antrim"))


class Form(unittest.TestCase):
    def test_parse_form_levels(self):
        self.assertEqual(pc.parse_form("City, County, State, Country"),
                         ["locality", "admin2", "admin1", "country"])
        self.assertEqual(pc.parse_form("Town, Parish, Whatever"), ["locality", "locality", "other"])
        self.assertIsNone(pc.parse_form(""))
        self.assertIsNone(pc.parse_form(None))


class Parse(unittest.TestCase):
    def test_gedcom_default_is_small_to_large_narrowest_first(self):
        p = pc.parse_place("Ballymena, County Antrim, Ireland", profile=G)
        self.assertEqual(norms(p), ["Ballymena", "County Antrim", "Ireland"])
        self.assertEqual((p.order_applied, p.order_source), (pc.SMALL_TO_LARGE, "profile"))
        self.assertEqual(p.components[-1].country, "IE")
        self.assertEqual(p.components[-1].level, "country")

    def test_explicit_large_to_small_is_reversed_so_the_narrowest_is_first(self):
        p = pc.parse_place("Hungary, Pest, Budapest", profile=G, order=pc.LARGE_TO_SMALL)
        self.assertEqual(norms(p), ["Budapest", "Pest", "Hungary"])
        self.assertEqual(p.order_source, "explicit")

    def test_auto_order_follows_the_country(self):
        a = pc.parse_place("Ireland, Antrim, Ballymena", profile=pc.PROFILES["generic"])
        self.assertEqual((a.order_applied, a.order_source), (pc.LARGE_TO_SMALL, "auto"))
        self.assertEqual(norms(a)[0], "Ballymena")
        b = pc.parse_place("Ballymena, Ireland", profile=pc.PROFILES["generic"])
        self.assertEqual(b.order_applied, pc.SMALL_TO_LARGE)

    def test_auto_with_no_country_says_it_guessed(self):
        p = pc.parse_place("Ballymena, Antrim", profile=pc.PROFILES["generic"])
        self.assertEqual(p.order_applied, pc.SMALL_TO_LARGE)
        self.assertTrue(any("could not be inferred" in n for n in p.notes))

    def test_blank_slots_are_kept_for_positions_but_excluded_from_filled(self):
        p = pc.parse_place("Holy Land, , Ontario, Canada", profile=G)
        self.assertEqual(len(p.components), 4)
        self.assertEqual([c.norm for c in p.filled], ["Holy Land", "Ontario", "Canada"])
        self.assertTrue(p.components[1].blank)

    def test_form_labels_each_component_in_order(self):
        p = pc.parse_place("Provo, Utah County, Utah, USA", profile=G, form="City, County, State, Country")
        self.assertEqual([c.level for c in p.components], ["locality", "admin2", "admin1", "country"])
        self.assertEqual(p.order_source, "form")

    def test_form_longer_than_the_string_anchors_both_ends(self):
        p = pc.parse_place("Provo, Utah, USA", profile=G, form="City, County, State, Country")
        self.assertEqual([c.level for c in p.components], ["locality", "admin1", "country"])
        self.assertTrue(any("anchored" in n for n in p.notes))

    def test_custom_separator(self):
        p = pc.parse_place("Ballymena; Antrim; Ireland", profile=G, separator=";")
        self.assertEqual(norms(p), ["Ballymena", "Antrim", "Ireland"])

    def test_a_single_component_with_no_country(self):
        p = pc.parse_place("Newport", profile=G)
        self.assertEqual(norms(p), ["Newport"])
        self.assertIsNone(p.components[0].country)

    def test_service_profiles_say_what_is_documented_and_what_is_assumed(self):
        self.assertEqual(pc.PROFILES["gedcom"].basis, "gedcom-spec")
        self.assertEqual(pc.PROFILES["treealive"].basis, "gedcom-spec")
        for name in ("familysearch", "findmypast", "wikitree", "generic"):
            self.assertEqual(pc.PROFILES[name].basis, "assumed", name)
        for name in ("ancestry", "myheritage"):      # seen in public sample exports, not vendor docs
            self.assertEqual(pc.PROFILES[name].basis, "observed", name)

    # --- strings taken from public Ancestry / MyHeritage sample GEDCOMs --------------------------
    def _names(self, text, service="ancestry"):
        return [c.norm for c in pc.parse_place(text, profile=pc.PROFILES[service]).filled]

    def test_ancestry_blank_slots_are_skipped(self):
        self.assertEqual(self._names("Lauderdale, , Alabama, USA"), ["Lauderdale", "Alabama", "USA"])
        self.assertEqual(self._names(", , Texas, USA"), ["Texas", "USA"])
        self.assertEqual(self._names(",Wayne,New York,USA"), ["Wayne", "New York", "USA"])

    def test_street_address_prefix_is_skipped(self):
        self.assertEqual(self._names("57, Caledon Road, East Ham, Essex, England", "myheritage"),
                         ["East Ham", "Essex", "England"])
        self.assertEqual(self._names("12 High Street, Leeds, England"), ["Leeds", "England"])

    def test_address_skipping_does_not_eat_real_places(self):
        """Controls: a numeral or a saint in a place NAME, or a lone street, stays a place."""
        self.assertEqual(self._names("3 Rivers, Michigan, USA"), ["3 Rivers", "Michigan", "USA"])
        self.assertEqual(self._names("St Louis, Missouri, USA"), ["St Louis", "Missouri", "USA"])
        self.assertEqual(self._names("Park Lane"), ["Park Lane"])
        self.assertEqual(self._names("Leeds, 57, England"), ["Leeds", "57", "England"])   # only LEADING parts


if __name__ == "__main__":
    unittest.main()
