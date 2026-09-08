"""The precompute stage's Charsiu tags must come from the routing table.

THE DEFECT THIS PINS. `phonetics/ipa/routes.py` and
`phonetics/extraction/precompute_neural_phonetics.py` each carried their own
CharsiuG2P language mapping. `c37d927` corrected the first — `zh`/`gan`/`wuu`
from the bogus `cmn` to `zho-s` — and did not reach the second.

⚠ `cmn` is not a tag CharsiuG2P knows, and it does not say so. A byte-level ByT5
does not error on an unrecognised tag; it generates anyway, and for Han input it
generates Japanese on'yomi:

    北京   <cmn>   -> hokːjoɯ         ("hokkyō", Japanese)
           <zho-s> -> peɪ˨˩˦tɕɪŋ˥˥    ("běijīng", correct Mandarin)

🛑 WHY THIS MATTERED MORE THAN AN ORDINARY DRIFT. The precompute stage produces
`neural_phonetics.parquet`, which the corpus rebuild consumes as Priority 2 for
exactly these languages. So the re-extract commissioned to FIX the Japanese-IPA
defect would have re-imported it — either by reusing the existing parquet
(built 2026-02-19, seven months before the fix) or by regenerating it from this
unfixed map. Neither route would have errored, and the output is well-formed
IPA, so residue, parseability and drop-rate checks all pass it. Only asking
whether it is the RIGHT LANGUAGE finds it.

The map is now derived from NEURAL_ROUTES rather than restated, and this test
holds the two in step.
"""
import unittest


class PrecomputeLangMapTest(unittest.TestCase):

    def test_map_is_derived_from_the_routing_table(self):
        from phonetics.extraction.precompute_neural_phonetics import CHARSIU_LANG_MAP
        from phonetics.ipa.routes import NEURAL_ROUTES, BACKEND_CHARSIU
        expected = {}
        for (lang, _script), (backend, tag) in NEURAL_ROUTES.items():
            if backend == BACKEND_CHARSIU:
                expected.setdefault(lang, tag)
        self.assertEqual(
            CHARSIU_LANG_MAP, expected,
            "precompute's Charsiu tags must equal the routing table's — two "
            "copies is how `cmn` survived in one of them")

    def test_the_bogus_tag_is_gone(self):
        """`cmn` specifically: the tag the model silently reads as Japanese."""
        from phonetics.extraction.precompute_neural_phonetics import CHARSIU_LANG_MAP
        offenders = {k: v for k, v in CHARSIU_LANG_MAP.items() if v == "cmn"}
        self.assertEqual(
            offenders, {},
            "`cmn` is not a CharsiuG2P tag; Han input under it yields Japanese "
            "on'yomi. Use `zho-s`.")

    def test_chinese_topolects_route_to_mandarin_orthography(self):
        from phonetics.extraction.precompute_neural_phonetics import CHARSIU_LANG_MAP
        for lang in ("zh", "gan", "wuu"):
            self.assertEqual(CHARSIU_LANG_MAP.get(lang), "zho-s",
                             f"{lang}+CJK must use zho-s")
        self.assertEqual(CHARSIU_LANG_MAP.get("yue"), "yue",
                         "Cantonese has its own tag and must not be folded in")


if __name__ == "__main__":
    unittest.main()
