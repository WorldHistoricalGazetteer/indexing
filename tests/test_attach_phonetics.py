"""Contract for `attach_phonetics` — the single writer of `panphon_embedding`.

The guarantee is not "it sets two fields". It is that a document is either
fully attributed or WHOLLY untouched, because a half-written document is the
shape that survives every count and fails at index time:
`panphon_embedding` is an indexed dense_vector, and ES rejects `[]`/`null`
with "Cannot update parameter [dims]" — after the run has already counted the
document as transcribed.
"""
import unittest

from phonetics.extraction.rebuild_toponyms_index import attach_phonetics


class AttachPhonetics(unittest.TestCase):

    def _doc(self):
        return {"toponym_id": "London@en", "name": "London",
                "lang": "en", "script": "LATIN"}

    def test_writes_both_and_reports_true(self):
        doc = self._doc()
        self.assertTrue(attach_phonetics(doc, "ˈlʌndən", [0.5] * 192))
        self.assertEqual(doc["ipa"], "ˈlʌndən")
        self.assertEqual(len(doc["panphon_embedding"]), 192)

    def test_refuses_an_empty_embedding_and_touches_nothing(self):
        # The whole point: no field may be set when the pair is incomplete.
        for empty in ([], None):
            with self.subTest(embedding=empty):
                doc = self._doc()
                before = dict(doc)
                self.assertFalse(attach_phonetics(doc, "ˈlʌndən", empty))
                self.assertEqual(doc, before)
                self.assertNotIn("ipa", doc)
                self.assertNotIn("panphon_embedding", doc)

    def test_refuses_a_missing_ipa_and_touches_nothing(self):
        # An embedding with no transcription is a vector nothing can explain.
        for missing in ("", None):
            with self.subTest(ipa=missing):
                doc = self._doc()
                before = dict(doc)
                self.assertFalse(attach_phonetics(doc, missing, [0.5] * 192))
                self.assertEqual(doc, before)

    def test_an_all_zero_embedding_is_still_a_value(self):
        # A list of zeros is truthy as a list and is NOT this function's job to
        # reject — the pooling already returns None for an all-zero vector
        # (ES rejects zero-magnitude vectors for cosine). Pinning the division
        # of labour so a future reader does not add a second, divergent check.
        doc = self._doc()
        self.assertTrue(attach_phonetics(doc, "x", [0.0] * 192))

    def test_a_refusal_leaves_the_doc_reusable(self):
        # After refusing, the SAME doc must still accept a later good value —
        # the fall-through routes depend on it.
        doc = self._doc()
        self.assertFalse(attach_phonetics(doc, "ˈlʌndən", []))
        self.assertTrue(attach_phonetics(doc, "ˈlʌndən", [0.25] * 192))
        self.assertEqual(len(doc["panphon_embedding"]), 192)


if __name__ == "__main__":
    unittest.main()
