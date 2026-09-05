"""The negative sampler must produce a set the discrimination guard ACCEPTS,
and must refuse rather than relax when it cannot.

Three mutations, each a way a plausible-looking negative sampler goes wrong:

  1. It relaxes the match when a bucket is thin — quietly reintroducing the easy
     negative for exactly the rare script pairs the benchmark exists to measure.
  2. It draws a co-referent of the query place, so the model is penalised for
     being right.
  3. It leaves positives whose negative could not be drawn, making the class
     balance vary per cell so per-cell AUCs are not comparable.

Pure Python: no ES, no sqlite file, no /vast.
"""
import random
import unittest

from evaluation.corpus import Positive
from evaluation.discrimination import (
    check_negative_matching, length_band)
from evaluation.negatives import (
    HaystackIndex, build_negatives, drop_unpaired)


def _haystack():
    docs = []
    for i in range(400):
        docs.append({"name": f"кандидат{i:03d}", "lang": "ru", "script": "CYRILLIC"})
        docs.append({"name": f"cand{i:03d}", "lang": "en", "script": "LATIN"})
    return docs


def _positives(n=100):
    return [Positive(place_id=f"gn:{i}", namespace="gn",
                     query=f"query{i:03d}", query_lang="en", query_script="LATIN",
                     partner=f"партнёр{i:03d}", partner_lang="ru",
                     partner_script="CYRILLIC")
            for i in range(n)]


class NegativeSamplerTest(unittest.TestCase):

    def test_produced_set_passes_the_matching_guard(self):
        """The whole point: the output must survive the check that refuses easy
        negatives. If it does not, the sampler and the guard disagree and one of
        them is wrong."""
        hay = HaystackIndex(_haystack())
        pairs, census = build_negatives(_positives(), hay, lambda p: set(),
                                        random.Random(0))
        pairs, dropped = drop_unpaired(pairs)
        self.assertEqual(dropped, 0)
        check_negative_matching(pairs)          # must not raise
        self.assertEqual(census["negatives"], 100)

    def test_refuses_to_relax_when_the_bucket_is_empty(self):
        """MUTATION: a script/length bucket with nothing in it.

        The wrong behaviour is to fall back to another script and report a full
        set. The right behaviour is to draw nothing and SAY SO — the census must
        carry the unmatched count per script pair, because a cell with no
        negatives has no AUC and must not look like a smaller sample.
        """
        hay = HaystackIndex([d for d in _haystack() if d["script"] == "LATIN"])
        pairs, census = build_negatives(_positives(20), hay, lambda p: set(),
                                        random.Random(0))
        self.assertEqual(census["negatives"], 0)
        self.assertEqual(census["unmatched_by_script_pair"]["LATIN→CYRILLIC"], 20)
        self.assertFalse(any(p.label == 0 for p in pairs))

    def test_a_co_referent_name_is_never_drawn_as_a_negative(self):
        """MUTATION: the negative is a real name of the query's place.

        Forbid every candidate but one and check the survivor is the one drawn —
        an implementation that ignored `forbidden` would pick another.
        """
        docs = [{"name": f"кандидат{i:03d}", "lang": "ru", "script": "CYRILLIC"}
                for i in range(50)]
        allowed = "кандидат007"
        forbidden = {d["name"] for d in docs} - {allowed}
        hay = HaystackIndex(docs)
        pairs, census = build_negatives(_positives(30), hay,
                                        lambda p: forbidden, random.Random(1))
        negs = [p.candidate for p in pairs if p.label == 0]
        self.assertTrue(negs, "no negative drawn at all")
        self.assertEqual(set(negs), {allowed})

    def test_unpaired_positives_are_dropped_and_counted(self):
        """MUTATION: leaving a positive whose negative failed, so the cell's
        class balance silently differs from every other cell's."""
        docs = [{"name": "кандидат000", "lang": "ru", "script": "CYRILLIC"}]
        hay = HaystackIndex(docs)
        # Forbid the only candidate: every draw fails.
        pairs, census = build_negatives(_positives(10), hay,
                                        lambda p: {"кандидат000"}, random.Random(2))
        kept, dropped = drop_unpaired(pairs)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 10)
        self.assertEqual(census["unmatched_positives"], 10)

    def test_negatives_share_the_positives_length_band(self):
        hay = HaystackIndex(_haystack())
        pairs, _ = build_negatives(_positives(), hay, lambda p: set(),
                                   random.Random(3))
        by_q = {}
        for p in pairs:
            by_q.setdefault((p.query, p.label), p)
        for (q, label), p in by_q.items():
            if label == 0:
                pos = by_q[(q, 1)]
                self.assertEqual(length_band(p.candidate),
                                 length_band(pos.candidate))


if __name__ == "__main__":
    unittest.main()
