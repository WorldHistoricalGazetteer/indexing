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


class ExternalPositiveSourceTest(unittest.TestCase):
    """A positive that is NOT co-attested must not silently lose its exclusion.

    v8's scope now includes historic orthography, whose pairs come from external
    packs (LHPN Welsh<->English, a GOTW transcription set) rather than from
    shared `place_id`. Those pairs have no place to exclude around, and
    `forbidden.get(place_id, set())` would return empty and read exactly like
    "this place has no co-referents". A negative drawn without an exclusion can
    be a genuine name of the query's place, which penalises the model for being
    RIGHT and shows up only as a lower AUC.
    """

    @staticmethod
    def _external(n=10):
        return [Positive(place_id="", namespace="lhpn",
                         query=f"Kaerdyf{i:02d}", query_lang="cy",
                         query_script="LATIN",
                         partner=f"Cardiff{i:02d}", partner_lang="en",
                         partner_script="LATIN", source="lhpn-historic")
                for i in range(n)]

    def _hay(self):
        return HaystackIndex([{"name": f"Placename{i:02d}", "lang": "en",
                               "script": "LATIN"} for i in range(300)])

    def test_unanchored_positives_are_refused_by_default(self):
        from evaluation.negatives import ExclusionImpossible
        with self.assertRaises(ExclusionImpossible) as ctx:
            build_negatives(self._external(), self._hay(), lambda p: set(),
                            random.Random(0))
        self.assertIn("lhpn-historic", str(ctx.exception))

    def test_unanchored_positives_are_allowed_deliberately_and_counted(self):
        pairs, census = build_negatives(self._external(), self._hay(),
                                        lambda p: set(), random.Random(0),
                                        allow_unanchored=True)
        self.assertEqual(census["unanchored_no_exclusion"], 10)
        self.assertEqual(census["negatives"], 10)

    def test_a_co_attested_set_is_still_accepted_without_the_flag(self):
        """The control: the guard must not fire on the ordinary case."""
        pairs, census = build_negatives(_positives(20), HaystackIndex(_haystack()),
                                        lambda p: set(), random.Random(0))
        self.assertEqual(census["unanchored_no_exclusion"], 0)

    def test_same_script_pairs_are_generable_when_asked_for(self):
        """Historic orthography is usually SAME script — Welsh and English are
        both Latin — so the cross-script filter must be a parameter, not a
        hard-coded assumption."""
        from evaluation.corpus import cross_script_pairs
        place = {"place_id": "gn:1", "toponyms": [
            {"toponym_id": "Cardiff@en"}, {"toponym_id": "Caerdydd@cy"},
            {"toponym_id": "Кардифф@ru"}]}
        cross, _ = cross_script_pairs(place, max_per_place=99, rng=random.Random(0))
        allp, _ = cross_script_pairs(place, max_per_place=99, rng=random.Random(0),
                                     require_cross_script=False)
        self.assertEqual(len(cross), 2)      # both Cyrillic pairings only
        self.assertEqual(len(allp), 3)       # plus Cardiff ~ Caerdydd
        self.assertTrue(all(p.script_pair[0] != p.script_pair[1] for p in cross))
        self.assertTrue(any(p.script_pair == ("LATIN", "LATIN") for p in allp))

    def test_the_unanchored_census_separates_its_sources(self):
        """MUTATION: two unanchored populations of very different quality.

        Under the GOTW ingest spec the unanchored set is no longer one thing. A
        row that never had an anchor and a row whose anchor a specialist
        REJECTED — whose typed correction then failed re-resolution — are not
        the same evidence: the second is a correction, i.e. one of the better
        answers in the pack. A single scalar averages them, which is the
        "two populations readable as one" failure the census exists to prevent.
        """
        never = [Positive(place_id="", namespace="lhpn", query=f"Q{i}",
                          query_lang="cy", query_script="LATIN",
                          partner=f"P{i}", partner_lang="en",
                          partner_script="LATIN", source="lhpn-historic")
                 for i in range(7)]
        rejected = [Positive(place_id="", namespace="gotw", query=f"R{i}",
                             query_lang="zh", query_script="LATIN",
                             partner=f"S{i}", partner_lang="en",
                             partner_script="LATIN",
                             source="gotw-override-unresolved")
                    for i in range(3)]
        hay = HaystackIndex([{"name": f"Name{i:03d}", "lang": "en",
                              "script": "LATIN"} for i in range(300)])
        _, census = build_negatives(never + rejected, hay, lambda p: set(),
                                    random.Random(0), allow_unanchored=True)
        self.assertEqual(census["unanchored_no_exclusion"], 10)
        self.assertEqual(census["unanchored_by_source"],
                         {"gotw-override-unresolved": 3, "lhpn-historic": 7})

    def test_source_defaults_so_old_corpora_still_load(self):
        """positives.jsonl written before `source` existed must still parse."""
        old = {"place_id": "gn:1", "namespace": "gn", "query": "a",
               "query_lang": "en", "query_script": "LATIN", "partner": "b",
               "partner_lang": "ru", "partner_script": "CYRILLIC"}
        p = Positive(**old)
        self.assertEqual(p.source, "co-attestation")
        self.assertTrue(p.has_place)


if __name__ == "__main__":
    unittest.main()
