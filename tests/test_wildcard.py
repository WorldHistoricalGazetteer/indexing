import unittest

from gateway import wildcard as w


class PatternRulesTests(unittest.TestCase):
    def test_plain_text_and_good_patterns_are_allowed(self):
        for p in ("Paris", "par*", "Par?s", "Lond?n", "Newcas*le", "San Fran*", "abc*def*", "caf\\?e", "par\\*"):
            self.assertIsNone(w.check(p), p)

    def test_leading_wildcard_refused_with_advice(self):
        for p in ("*aris", "?aris", "*"):
            msg = w.check(p)
            self.assertIsNotNone(msg, p)
        self.assertIn("par*", w.check("*aris"))

    def test_too_little_literal_before_first_wildcard(self):
        for p in ("p*", "pa*", "a?b", "pa?is"):
            self.assertIn("at least 3", w.check(p), p)
        self.assertIsNone(w.check("par?s"))                 # control: exactly three letters before it

    def test_too_many_wildcards_and_too_long(self):
        self.assertIn("Too many", w.check("abc*d*e*f*g*h"))
        self.assertIsNone(w.check("abc*d*e*f*"))             # control: exactly four
        self.assertIn("too long", w.check("a" * 101))

    def test_escaped_wildcards_are_literal(self):
        self.assertFalse(w.has_wildcard("caf\\*"))
        self.assertTrue(w.has_wildcard("caf*"))
        self.assertTrue(w.has_wildcard("a\\\\*"))            # an escaped backslash, then a real star
        self.assertIsNone(w.check("what\\?"))                # no real wildcard: ordinary text
        self.assertEqual(w.literal_text("par\\*is*"), "par*is")

    def test_has_wildcard_ignores_ordinary_names(self):
        for p in ("Paris", "St. John's", "Saint-Denis", "Aix-en-Provence", ""):
            self.assertFalse(w.has_wildcard(p), p)


class RegexAndScoreTests(unittest.TestCase):
    def test_regex_matches_like_the_documented_syntax(self):
        r = w.to_regex("par*")
        self.assertTrue(r.match("Paris")); self.assertTrue(r.match("PARIS")); self.assertTrue(r.match("par"))
        self.assertFalse(r.match("Spar"))
        q = w.to_regex("Par?s")
        self.assertTrue(q.match("Paris")); self.assertFalse(q.match("Pars")); self.assertFalse(q.match("Parris"))
        self.assertTrue(w.to_regex("caf\\?").match("caf?")); self.assertFalse(w.to_regex("caf\\?").match("cafe"))

    def test_shorter_closer_names_rank_higher(self):
        self.assertEqual(w.score("par*", "Paris"), w.score("par*", "Parma"))      # same length: a tie, by design
        self.assertGreater(w.score("par*", "Paris"), w.score("par*", "Parisian Gardens"))
        self.assertGreater(w.score("par*", "Paris-Plage"), w.score("par*", "Parisian Gardens"))

    def test_question_mark_counts_as_a_matched_letter(self):
        self.assertEqual(w.score("par?s", "Paris"), 1.0)
        self.assertEqual(w.score("par*", ""), 0.0)
        self.assertLessEqual(w.score("paris*", "Paris"), 1.0)


if __name__ == "__main__":
    unittest.main()


class ImportanceTests(unittest.TestCase):
    def test_importance_is_damped_and_monotonic(self):
        f = w.importance_factor
        self.assertEqual(f(None), 1.0); self.assertEqual(f(0), 1.0); self.assertEqual(f("x"), 1.0)
        self.assertLess(f(1_000), f(1_000_000))
        self.assertLess(f(2_000_000), 8.5)

    def test_a_famous_longer_name_can_beat_an_obscure_closer_one_but_not_always(self):
        paris = w.score("par*", "Paris") * w.importance_factor(2_100_000)
        paro = w.score("par*", "Paro") * w.importance_factor(0)
        self.assertGreater(paris, paro)                                   # the point of the tie-break
        self.assertLess(w.TITLE_FIT_BONUS * w.importance_factor(10 ** 8), 20)            # the multipliers stay bounded
