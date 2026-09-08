"""Every CharsiuG2P tag in NEURAL_ROUTES must be one the model actually knows.

WHY THIS TEST EXISTS. `cmn` was routed for `zh`, `gan` and `wuu`, and CharsiuG2P
does not recognise it. The prompt is `<{tag}>: {text}` and a byte-level ByT5 does
not reject an unknown tag — it generates regardless, and for Han input it returns
Japanese on'yomi. 881,588 of 1,583,722 stored `zh` rows (55.7%) carry Japanese-only
phonemes as a result, every one of them recording `mode=cmn`, so the route fired
as written and the MODEL silently changed language.

⚠ THE OUTPUT IS WELL-FORMED IPA. Residue, PanPhon parseability and drop-rate all
pass it cleanly — the defect is invisible to every structural check, because
nothing about `hokːjoɯ` is malformed. It is only visible if you ask whether the
IPA is in the language that was requested.

This test is a cheap standing guard: it does not need the model, only the tag list
the model publishes, so a future edit that invents a plausible-looking code fails
here rather than 1.6 M rows later.
"""
import unittest

from phonetics.ipa.routes import BACKEND_CHARSIU, NEURAL_ROUTES, SCRIPT_FIRST

#: Tags CharsiuG2P's multilingual byT5 checkpoint publishes for the languages we
#: route. Chinese is `zho-s` / `zho-t` (simplified / traditional); there is no
#: `cmn`. Verified against the model on 8 Sep 2026 by comparing output for the
#: same strings — see the module docstring.
KNOWN_CHARSIU_TAGS = {"zho-s", "zho-t", "yue", "jpn", "kor"}

#: The one that was wrong, pinned so it cannot come back by a plausible edit.
REJECTED = {"cmn"}


class TestCharsiuTags(unittest.TestCase):

    def test_every_charsiu_route_uses_a_known_tag(self):
        bad = [(k, tag) for k, (backend, tag) in NEURAL_ROUTES.items()
               if backend == BACKEND_CHARSIU and tag not in KNOWN_CHARSIU_TAGS]
        self.assertEqual([], bad,
                         f"CharsiuG2P does not know these tags, and will NOT say so — "
                         f"it returns fluent IPA in another language: {bad}")

    def test_the_known_bad_tag_cannot_return(self):
        used = {tag for backend, tag in NEURAL_ROUTES.values()
                if backend == BACKEND_CHARSIU}
        used |= {tag for backend, tag in SCRIPT_FIRST.values()
                 if backend == BACKEND_CHARSIU}
        self.assertEqual(set(), used & REJECTED,
                         "`cmn` is not a CharsiuG2P tag; Chinese is `zho-s`/`zho-t`. "
                         "It cost 881,588 rows of Japanese IPA on Chinese toponyms.")

    def test_chinese_variants_all_route_to_a_chinese_tag(self):
        """gan and wuu are approximated by Mandarin deliberately — but they must
        land on a CHINESE tag, not on whatever an unknown code degrades to."""
        for lang in ("zh", "gan", "wuu"):
            with self.subTest(lang=lang):
                backend, tag = NEURAL_ROUTES[(lang, "CJK")]
                self.assertEqual(BACKEND_CHARSIU, backend)
                self.assertIn(tag, {"zho-s", "zho-t"})


if __name__ == "__main__":
    unittest.main()
