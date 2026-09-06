"""A source-declared romanisation must survive the script-mismatch filter (#250).

`is_script_mismatch` exists to catch an UNDECLARED inconsistency — a bare `zh`
name that is accidentally Latin. It could never catch a DECLARED one, because
the base-split destroyed the declaration before the test:
`zh-Latn-pinyin-x-notone` became `zh`, LATIN was absent from that language's
expected scripts, and the row was discarded for being exactly what its tag said.

Measured consequence before the fix: ~1.16M Getty-declared romanisations in tgn
alone never reached the store, and `tgn lang=zh script=LATIN` was exactly 0.

⚠ The `bo` case is the control and is kept as a test: Tibetan is ABSENT from
`LANG_EXPECTED_SCRIPTS`, so its romanisations always survived — which is what
made this the toponym build's filter rather than something upstream. If a future
change adds `bo` to that dict, the second test here starts exercising the fix
instead of the bypass, and the first still holds the line.
"""
import unittest

from phonetics.extraction.rebuild_toponyms_index import (
    LANG_EXPECTED_SCRIPTS, _has_script_subtag, is_script_mismatch)
from phonetics.utils.script_detection import Script


class DeclaredScriptSubtagTest(unittest.TestCase):

    def test_declared_romanisations_are_kept(self):
        """The population that was lost: 1.16M rows across six languages."""
        for lang in ("zh-Latn", "zh-Latn-pinyin-x-notone", "ja-Latn",
                     "fa-Latn", "el-Latn", "ru-Latn", "ar-Latn"):
            self.assertFalse(is_script_mismatch(lang, Script.LATIN),
                             f"{lang} declares Latin script and was discarded")

    def test_undeclared_mismatch_is_still_filtered(self):
        """MUTATION: the rule must not be neutered.

        A bare `zh` name that is accidentally Latin is the genuine tagging error
        the filter was written for, and it must still be caught — otherwise this
        fix trades one silent loss for one silent admission.
        """
        for lang in ("zh", "ja", "ko", "ru", "el", "ar", "fa"):
            self.assertTrue(is_script_mismatch(lang, Script.LATIN),
                            f"bare {lang} + LATIN should still be a mismatch")

    def test_native_script_is_never_a_mismatch(self):
        self.assertFalse(is_script_mismatch("zh", Script.CJK))
        self.assertFalse(is_script_mismatch("zh-Hans", Script.CJK))

    def test_bo_control_unaffected(self):
        """Tibetan is absent from the dict, so it survived before AND after."""
        self.assertNotIn("bo", LANG_EXPECTED_SCRIPTS)
        self.assertFalse(is_script_mismatch("bo", Script.LATIN))
        self.assertFalse(is_script_mismatch("bo-Latn", Script.LATIN))

    def test_region_and_extension_subtags_are_not_mistaken_for_script(self):
        """`en-GB` is a region, `x-notone` an extension. Neither declares a
        script, so neither may bypass the rule on its own."""
        self.assertFalse(_has_script_subtag("en-GB"))
        self.assertFalse(_has_script_subtag("zh-CN"))
        self.assertFalse(_has_script_subtag("de"))
        self.assertTrue(_has_script_subtag("zh-Latn"))
        self.assertTrue(_has_script_subtag("sr-Cyrl"))
        self.assertTrue(_has_script_subtag("ug-Arab"))

    def test_a_region_tagged_name_still_gets_the_rule(self):
        """`zh-CN` declares a region, not a script — the mismatch rule applies."""
        self.assertTrue(is_script_mismatch("zh-CN", Script.LATIN))


if __name__ == "__main__":
    unittest.main()
