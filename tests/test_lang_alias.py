"""`LANG_ALIAS` — that it fires, that it is script-scoped, and that it never shadows.

WHY AN ALIAS IS DANGEROUS AND A MODE IS NOT. A native `iso3-Tag` mode is a claim
about a language. An alias is a claim that ANOTHER language's map is close enough,
and it is only defensible where the borrowed map is graphemic: `shn` borrows
`mya-Mymr` because that file marks no tone though Burmese is tonal and carries a
full voiced-aspirate series though Burmese has none — it is the Indic letter
series letter-for-letter, so Shan gets a transliteration of the shared graphemes
rather than Burmese phonology. The identical borrowing in Latin script would be
nonsense, which is the whole reason the table is keyed on (lang, script).

THE ORDERING IS THE SAFETY PROPERTY. `resolve` consults the alias table only after
a direct lookup fails, so installing a native map RETIRES the alias rather than
being masked by it. Test that explicitly: it is the one property whose failure is
silent, because a shadowed native mode produces a plausible wrong transcription
rather than an error.
"""
import unittest

from phonetics.ipa.routes import LANG_ALIAS, SCRIPT_TAG, RouteTable


class TestLangAlias(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.t = RouteTable()
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("epitran not installed")

    def test_alias_fires_and_is_labelled_as_one(self):
        """An aliased row must be distinguishable from a native one in an audit."""
        for lang, script, mode in (("ory", "ORIYA", "ori-Orya"),
                                   ("shn", "MYANMAR", "mya-Mymr"),
                                   ("dz", "TIBETAN", "bod-Tibt"),
                                   ("syc", "SYRIAC", "aii-Syrc")):
            with self.subTest(lang=lang):
                route, status = self.t.resolve(lang, script)
                if mode not in self.t.modes:
                    self.skipTest(f"{mode} not installed")
                self.assertEqual("ok", status)
                self.assertEqual(mode, route.mode)
                self.assertTrue(route.reason.startswith("alias:"),
                                f"aliased route reported reason={route.reason!r}; an "
                                f"audit could not tell it from a native mode")

    def test_alias_never_shadows_a_native_mode(self):
        """The safety property. `or` has a native `ori-Orya`; it must NOT be
        reported as an alias even though `ory` -> `ori` is in the table."""
        route, status = self.t.resolve("or", "ORIYA")
        if "ori-Orya" not in self.t.modes:
            self.skipTest("ori-Orya not installed")
        self.assertEqual("ok", status)
        self.assertEqual("installed-mode", route.reason)

    def test_aliases_are_script_scoped(self):
        """`shn` borrows a Myanmar-script map. In Latin script that borrowing is
        meaningless and must not happen."""
        route, status = self.t.resolve("shn", "LATIN")
        self.assertEqual("no_route", status)
        self.assertIsNone(route)

    def test_refused_alias_stays_refused(self):
        """`arc` (Official Aramaic, 700-300 BCE) was considered and declined:
        2,500 years is not an accent. A later reader should not quietly add it."""
        self.assertNotIn(("arc", "SYRIAC"), LANG_ALIAS)
        route, status = self.t.resolve("arc", "SYRIAC")
        self.assertEqual("no_route", status)

    def test_every_alias_target_could_exist(self):
        """A typo in a target is invisible — it simply never fires. Check each
        target names a mode that is installed OR a script whose map we are
        drafting (`ber`->`zgh` is inert until zgh-Tfng ships, deliberately)."""
        pending = {("ber", "TIFINAGH")}
        for (lang, script), target in LANG_ALIAS.items():
            if script == "*" or (lang, script) in pending:
                continue
            tag = SCRIPT_TAG.get(script)
            self.assertIsNotNone(tag, f"alias for unknown script {script!r}")
            self.assertIn(f"{target}-{tag}", self.t.modes,
                          f"alias {lang}->{target} targets {target}-{tag}, which is "
                          f"not installed; the alias can never fire")


if __name__ == "__main__":
    unittest.main()
