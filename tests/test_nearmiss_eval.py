import random
import unittest

from evaluation.nearmiss import errors as E
from evaluation.nearmiss import metrics as M


class ErrorModelTests(unittest.TestCase):
    def test_edit_distance_basics(self):
        self.assertEqual(E.edit_distance("Paris", "Paris"), 0)
        self.assertEqual(E.edit_distance("Paris", "Pairs"), 1)      # transposition counts once
        self.assertEqual(E.edit_distance("Köln", "Koln"), 1)
        self.assertEqual(E.edit_distance("abc", "xyz"), 3)

    def test_each_class_changes_a_suitable_name(self):
        rng = random.Random(1)
        cases = {"phonetic": "Philadelphia", "vowel": "Manchester", "double": "Bellingham", "diacritic": "Besançon",
                 "dropped": "Hamburgh", "extra": "Leeds", "transpose": "Glasgow", "ocr": "Camden", "typo": "Dublin",
                 "abbrev": "Saint Louis"}
        for cls, name in cases.items():
            out = E.apply_class(name, cls, rng)
            self.assertIsNotNone(out, cls)
            self.assertNotEqual(out, name, cls)

    def test_class_that_cannot_apply_returns_none(self):
        self.assertIsNone(E.apply_class("Rome", "abbrev", random.Random(1)))
        self.assertIsNone(E.apply_class("Rome", "diacritic", random.Random(1)))

    def test_corrupt_is_deterministic_and_always_differs(self):
        names = ["Ballymena", "Lauderdale", "Newcastle upon Tyne", "Kraków", "Saint Petersburg", "York"]
        a = [E.corrupt(n, random.Random(7)) for n in names]
        b = [E.corrupt(n, random.Random(7)) for n in names]
        self.assertEqual(a, b)
        for n, r in zip(names, a):
            if r:
                self.assertNotEqual(r[0], n)
                self.assertTrue(r[1])

    def test_corrupt_stays_near_the_original(self):
        rng = random.Random(3)
        for _ in range(300):
            r = E.corrupt("Wolverhampton", rng)
            if r:
                self.assertLessEqual(E.edit_distance("Wolverhampton", r[0]), 8)   # three errors, some multi-letter

    def test_force_puts_the_requested_class_first_or_gives_up(self):
        r = E.corrupt("Köln", random.Random(1), force="diacritic")
        self.assertEqual(r[0], "Koln"); self.assertEqual(r[1][0], "diacritic")
        self.assertIsNone(E.corrupt("Rome", random.Random(1), force="diacritic"))   # nothing to strip

    def test_nonsense_is_not_empty_and_is_seeded(self):
        self.assertEqual(E.nonsense(random.Random(5)), E.nonsense(random.Random(5)))
        self.assertGreaterEqual(len(E.nonsense(random.Random(5))), 5)


def _item(pid="p1", truth=("p1",), name="Paris", **kw):
    return {"kind": "misspelling", "place_id": pid, "truth_pids": list(truth), "original": name,
            "classes": ["vowel"], "edit_distance": 1, **kw}


class MetricsTests(unittest.TestCase):
    def test_wilson_interval_contains_rate_and_widens_with_small_n(self):
        lo, hi = M.wilson(8, 10)
        self.assertLess(lo, 0.8); self.assertGreater(hi, 0.8)
        lo2, hi2 = M.wilson(800, 1000)
        self.assertLess(hi2 - lo2, hi - lo)
        self.assertEqual(M.wilson(0, 0), (0.0, 0.0))

    def test_rank_and_any_vs_specific(self):
        it = _item(pid="p1", truth=("p1", "p2"))
        o = M.score_item(it, [("x", 90), ("p2", 80), ("p1", 70)])
        self.assertEqual(o["rank_any"], 1)          # a namesake counts for 'any'
        self.assertEqual(o["rank_specific"], 2)     # but not for the strict reading
        self.assertFalse(o["top1_right"])

    def test_country_reading_is_the_headline(self):
        it = _item(pid="p1", truth=("p1", "far"), truth_cc=["p1"])
        o = M.score_item(it, [("far", 90), ("p1", 80)])          # a namesake abroad ranks first
        self.assertEqual(o["rank_any"], 0)
        self.assertEqual(o["rank_cc"], 1)
        self.assertFalse(o["top1_right"])                         # not right: it is the wrong country
        self.assertEqual(M.aggregate([o])["hit@1"]["k"], 0)
        self.assertEqual(M.aggregate([o])["any_namesake@10"]["k"], 1)

    def test_empty_result_is_a_miss_not_a_crash(self):
        o = M.score_item(_item(), [])
        self.assertIsNone(o["rank_any"]); self.assertFalse(o["answered"]); self.assertEqual(o["top_conf"], 0.0)

    def test_precision_is_reported_with_coverage(self):
        items = [_item(), _item(), _item(), _item()]
        outs = [M.score_item(items[0], [("p1", 95)]),            # right, high
                M.score_item(items[1], [("p1", 60)]),            # right, mid
                M.score_item(items[2], [("zz", 90)]),            # WRONG, high
                M.score_item(items[3], [("zz", 20)])]            # wrong, low
        a = M.aggregate(outs)
        self.assertEqual(a["accept"]["85"]["coverage"]["k"], 2)
        self.assertEqual(a["accept"]["85"]["precision"]["k"], 1)  # 1 of the 2 that reach 85
        self.assertEqual(a["accept"]["30"]["coverage"]["k"], 3)
        self.assertEqual(a["hit@1"]["k"], 2)

    def test_nonsense_measured_separately_and_never_counted_as_misses(self):
        junk = {"kind": "nonsense", "original": "Zibo", "classes": []}
        outs = [M.score_item(_item(), [("p1", 90)]), M.score_item(junk, [("q", 88)]), M.score_item(junk, [])]
        a = M.aggregate(outs)
        self.assertEqual(a["n"], 1)                               # nonsense is not in the denominator
        self.assertEqual(a["hit@1"]["rate"], 1.0)
        self.assertEqual(a["false_accept"]["85"]["k"], 1)
        self.assertEqual(a["false_accept"]["85"]["n"], 2)

    def test_stratify_overlapping_families_are_not_summed(self):
        items = [_item(classes=["phonetic", "vowel"]), _item(classes=["vowel"])]
        outs = [M.score_item(i, [("p1", 90)]) for i in items]
        s = M.stratify(items, outs, {"error_class": lambda it: it["classes"]})
        self.assertEqual(s["error_class"]["vowel"]["n"], 2)
        self.assertEqual(s["error_class"]["phonetic"]["n"], 1)    # 3 label-memberships from 2 items: by design
        self.assertEqual(s["ALL"]["n"], 2)

    def test_length_buckets(self):
        self.assertEqual([M.length_bucket(x) for x in ("Rome", "Ghent", "Lisbon", "Birmingham", "Wolverhampton!", "Newcastle upon Tyne")],
                         ["<=5", "<=5", "6-8", "9-12", "13+", "13+"])


if __name__ == "__main__":
    unittest.main()
