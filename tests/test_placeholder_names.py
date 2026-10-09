"""Placeholder toponyms in name-match discovery (place#216).

``Oknoname 037068 Dam`` is a GNIS cataloguing placeholder ("OK, no name"), not a
toponym, and once ranked first for ``Barony Lanach, Scotland``. The census on
place#216 found ~123,000 such records across osm/gn/wd, and that the matching
happens on the ``toponyms`` index — so the remedy is to take the decision on the
toponym NAME that matched, in the three discovery helpers, and leave the record
itself alone (it is still a real feature with real coordinates).

What is proven here:

  * the committed rule file is EMPTY, so with it the helpers are byte-identical
    to before — nothing changes until the pattern list is approved;
  * an excluded name never puts its attestations into the pool, in all three
    tiers (KNN collect, near-miss, exact), while an ordinary name in the same
    hit list still does;
  * a demoted name's contribution is scaled, and a genuine exact match still
    outranks it;
  * matching is whole-name and case-insensitive, so ``no ?name`` catches
    ``Noname`` and not ``No Name Creek``;
  * the loader never raises: a missing file, a non-object file and a pattern
    that does not compile all degrade to "no rules" and say so in ``rejected``.

Pure-function tests; no ES round trips. The default-file assertions read the
real committed file, so they fail the day someone populates it without also
updating this expectation — which is the point.
"""

import json
import tempfile
import unittest
from pathlib import Path

from gateway import placeholders
from gateway.es_helpers import (
    LEXICAL_EXACT_BOOST,
    LEXICAL_FUZZY_BOOST,
    apply_lexical_boost,
    apply_lexical_near_miss,
    collect_place_ids,
)
from gateway.placeholders import (
    DEFAULT_DEMOTE_WEIGHT,
    EMPTY_RULES,
    PlaceholderRules,
    load_placeholder_rules,
    load_rules_from,
    rules_from_dict,
)

PLACEHOLDER = "Oknoname 037068 Dam"
REAL = "Lannach"


def _hits(pairs):
    """[(name, [place_ids]), ...] — a lexical pass's hit list."""
    return [{"_source": {"name": n, "attestations": pids}} for n, pids in pairs]


def _knn(pairs):
    """[(name, _score, [place_ids]), ...] — a KNN pass as ES scores it."""
    return [{"_score": s, "_source": {"name": n, "attestations": pids}}
            for n, s, pids in pairs]


def _rules(exclude=(), demote=(), weight=DEFAULT_DEMOTE_WEIGHT):
    return rules_from_dict({"exclude": list(exclude), "demote": list(demote),
                            "demote_weight": weight}, source="test")


OKNONAME = _rules(exclude=[r"oknoname.*"])


# ---------------------------------------------------------------------------
# The committed rule file — the tiers approved on place#216 (2026-10-09)
# ---------------------------------------------------------------------------

COMMITTED = Path(__file__).resolve().parents[1] / "gateway" / "data" / "placeholder_names.json"

# (name, expected classification) — census 2 on place#216. T1 = exclude, T2 = demote,
# T3 = None. The T3 rows are the real names each pattern must NOT reach.
CENSUS_PROBES = [
    ("Oknoname 037068 Dam", "exclude"), ("Oknoname Dam", "exclude"),
    ("Unnamed", "exclude"), ("unnamed shipwreck", "exclude"), ("Unnamed Mineshaft", "exclude"),
    ("Unknown", "exclude"), ("?", "exclude"), ("...", "exclude"), ("-", "exclude"),
    ("X", "exclude"), ("xx", "exclude"), ("Fixme", "exclude"), ("TBA", "exclude"),
    ("Sin nombre", "exclude"), ("名称不明", "exclude"), ("без названия", "exclude"),
    ("Desconocida", "exclude"), ("Unbekannt", "exclude"), ("Senza nome", "exclude"),
    ("Ohne Namen", "exclude"), ("Sans nom", "exclude"), ("Sem nome", "exclude"),
    ("Onbekend", "exclude"), ("Naamloos", "exclude"), ("Bezimienny", "exclude"),
    ("No Name", "demote"), ("Noname", "demote"), ("Nimetön", "demote"), ("Q1", "demote"),
    ("Q7", "demote"), ("286", "demote"), ("NA", "demote"), ("N/A", "demote"),
    ("Na", "demote"), ("NN", "demote"), ("N.N.", "demote"),
    ("No Name Creek", None), ("Nameless Point", None), ("Untitled", None),
    ("Unknown Pond", None), ("Unknown Soldier", None), ("Sin Nombre, Cerro", None),
    ("Desconocida Reef", None), ("Nimetönlampi", None), ("Namnlösen", None),
    ("无名岛", None), ("Lac Inconnu", None), ("M25", None), ("manzana 349", None),
    ("Nil", None), ("None", None), ("Todo", None), ("[ROMAN ROAD]", None),
    ("(Holy Roman Empire)", None), ("Halton Holegate", None), ("London", None),
    ("Xanten", None), ("Q-Park", None),
]


class TestCommittedRules(unittest.TestCase):

    def test_the_file_loads_whole(self):
        data = json.loads(COMMITTED.read_text(encoding="utf-8"))
        rules = load_rules_from(COMMITTED)
        self.assertTrue(rules.active)
        self.assertEqual(rules.rejected, ())
        self.assertEqual(len(rules.exclude), len(data["exclude"]))
        self.assertEqual(len(rules.demote), len(data["demote"]))
        self.assertEqual(rules.demote_weight, 0.25)

    def test_census_probes(self):
        rules = load_rules_from(COMMITTED)
        for name, expected in CENSUS_PROBES:
            with self.subTest(name=name):
                self.assertEqual(rules.classify(name), expected)

    def test_the_configured_rules_are_the_committed_ones(self):
        load_placeholder_rules.cache_clear()
        try:
            self.assertEqual(load_placeholder_rules().classify("Oknoname Dam"), "exclude")
        finally:
            load_placeholder_rules.cache_clear()

    def test_empty_rules_leave_the_helpers_unchanged(self):
        # The mechanism with EMPTY_RULES is the pre-#216 behaviour: placeholder kept.
        knn = _knn([(PLACEHOLDER, 0.99, ["gn:1"]), (REAL, 0.98, ["wd:2"])])
        pool = {}
        collect_place_ids(knn, pool, normalise=True, placeholder_rules=EMPTY_RULES)
        self.assertEqual(pool["gn:1"], 1.0)   # the placeholder sets the scale
        self.assertIn("wd:2", pool)


# ---------------------------------------------------------------------------
# Exclusion — all three tiers
# ---------------------------------------------------------------------------

class TestExclusion(unittest.TestCase):

    def test_knn_collect_drops_the_placeholder_and_keeps_the_real_name(self):
        pool, names = {}, {}
        collect_place_ids(
            _knn([(PLACEHOLDER, 0.99, ["gn:1", "osm:n1"]), (REAL, 0.98, ["wd:2"])]),
            pool, match_names=names, normalise=True, placeholder_rules=OKNONAME)
        self.assertNotIn("gn:1", pool)
        self.assertNotIn("osm:n1", pool)
        self.assertIn("wd:2", pool)
        self.assertEqual(names, {"wd:2": REAL})

    def test_an_excluded_top_hit_does_not_set_the_normalisation_scale(self):
        # The placeholder was the KNN's nearest neighbour. Once excluded, the
        # best SURVIVING hit must be the pass's 1.0 — otherwise a dropped name
        # still shapes the scores of everything beneath it.
        pool = {}
        collect_place_ids(
            _knn([(PLACEHOLDER, 0.99, ["gn:1"]), (REAL, 0.90, ["wd:2"])]),
            pool, normalise=True, placeholder_rules=OKNONAME)
        self.assertEqual(pool, {"wd:2": 1.0})

    def test_text_mode_collect_drops_it_too(self):
        pool = {}
        collect_place_ids(
            _knn([(PLACEHOLDER, 12.0, ["gn:1"]), (REAL, 9.0, ["wd:2"])]),
            pool, placeholder_rules=OKNONAME)
        self.assertEqual(pool, {"wd:2": 9.0})

    def test_exact_tier_gives_an_excluded_name_no_boost(self):
        pool = {}
        n = apply_lexical_boost(
            _hits([(PLACEHOLDER, ["gn:1"]), (REAL, ["wd:2"])]), pool,
            {PLACEHOLDER.lower(): LEXICAL_EXACT_BOOST, REAL.lower(): LEXICAL_EXACT_BOOST},
            placeholder_rules=OKNONAME)
        self.assertEqual(n, 1)
        self.assertEqual(pool, {"wd:2": LEXICAL_EXACT_BOOST})

    def test_near_miss_tier_gives_an_excluded_name_no_boost(self):
        pool = {}
        n = apply_lexical_near_miss(
            _hits([(PLACEHOLDER, ["gn:1"]), ("Oknoname 037068 Dams", ["gn:3"])]),
            pool, {"Oknoname 037068 Dam": 1.0}, placeholder_rules=OKNONAME)
        self.assertEqual(n, 0)
        self.assertEqual(pool, {})

    def test_a_place_with_a_real_name_is_still_found_by_that_name(self):
        # kain_par:11771 is titled "Unknown" and carries the toponym
        # "Halton Holegate". Excluding "unknown" must not hide the place.
        rules = _rules(exclude=["unknown"])
        pool = {}
        apply_lexical_boost(
            _hits([("Unknown", ["kain_par:11771"]), ("Halton Holegate", ["kain_par:11771"])]),
            pool, {"halton holegate": LEXICAL_EXACT_BOOST, "unknown": LEXICAL_EXACT_BOOST},
            placeholder_rules=rules)
        self.assertEqual(pool, {"kain_par:11771": LEXICAL_EXACT_BOOST})


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------

class TestDemotion(unittest.TestCase):

    def test_a_demoted_knn_hit_is_scaled_and_stays_in_the_pool(self):
        rules = _rules(demote=["unknown"], weight=0.25)
        pool = {}
        collect_place_ids(
            _knn([("Unknown", 0.99, ["osm:n1"]), (REAL, 0.99, ["wd:2"])]),
            pool, normalise=True, placeholder_rules=rules)
        self.assertEqual(pool["wd:2"], 1.0)
        self.assertAlmostEqual(pool["osm:n1"], 0.25)

    def test_a_demoted_exact_match_still_loses_to_a_genuine_one(self):
        rules = _rules(demote=["unknown"], weight=0.25)
        pool = {}
        apply_lexical_boost(
            _hits([("Unknown", ["osm:n1"]), ("Unknown Pond", ["gn:9"])]), pool,
            {"unknown": LEXICAL_EXACT_BOOST, "unknown pond": LEXICAL_EXACT_BOOST},
            placeholder_rules=rules)
        self.assertAlmostEqual(pool["osm:n1"], LEXICAL_EXACT_BOOST * 0.25)
        self.assertEqual(pool["gn:9"], LEXICAL_EXACT_BOOST)
        self.assertGreater(pool["gn:9"], pool["osm:n1"])

    def test_near_miss_contribution_is_scaled(self):
        rules = _rules(demote=["unnamed.*"], weight=0.5)
        pool = {}
        apply_lexical_near_miss(
            _hits([("Unnamed Lake", ["wd:1"])]), pool, {"Unnamed Lakes": 1.0},
            placeholder_rules=rules)
        self.assertGreater(pool["wd:1"], 0)
        self.assertLess(pool["wd:1"], LEXICAL_FUZZY_BOOST * 0.5 + 1e-9)

    def test_exclude_wins_over_demote(self):
        rules = _rules(exclude=["oknoname.*"], demote=["oknoname.*"])
        self.assertEqual(rules.classify(PLACEHOLDER), "exclude")
        self.assertEqual(rules.weight(PLACEHOLDER), 0.0)


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------

class TestMatching(unittest.TestCase):

    def test_whole_name_only(self):
        rules = _rules(exclude=[r"no ?name"])
        self.assertEqual(rules.classify("Noname"), "exclude")
        self.assertEqual(rules.classify("No Name"), "exclude")
        # 235 GNIS streams are really called this. Not a placeholder.
        self.assertIsNone(rules.classify("No Name Creek"))

    def test_case_insensitive_and_trimmed(self):
        self.assertEqual(OKNONAME.classify("  OKNONAME 02702 DAM "), "exclude")
        self.assertEqual(OKNONAME.classify("Oknoname Dam"), "exclude")

    def test_the_real_name_is_untouched(self):
        self.assertIsNone(OKNONAME.classify(REAL))
        self.assertEqual(OKNONAME.weight(REAL), 1.0)

    def test_blank_and_none_are_ordinary(self):
        self.assertIsNone(OKNONAME.classify(None))
        self.assertIsNone(OKNONAME.classify("   "))
        self.assertEqual(OKNONAME.weight(""), 1.0)

    def test_inactive_rules_classify_nothing(self):
        self.assertFalse(EMPTY_RULES.active)
        self.assertIsNone(EMPTY_RULES.classify(PLACEHOLDER))
        self.assertEqual(EMPTY_RULES.weight(PLACEHOLDER), 1.0)


# ---------------------------------------------------------------------------
# The loader never takes discovery down
# ---------------------------------------------------------------------------

class TestLoader(unittest.TestCase):

    def test_missing_file_means_no_rules(self):
        rules = load_rules_from(Path(tempfile.gettempdir()) / "no-such-placeholder-rules.json")
        self.assertFalse(rules.active)
        self.assertEqual(rules.rejected, ())

    def test_unparseable_file_means_no_rules_and_says_so(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
        rules = load_rules_from(fh.name)
        self.assertFalse(rules.active)
        self.assertEqual(rules.rejected[0][0], "file")

    def test_a_bad_pattern_is_skipped_not_fatal(self):
        rules = rules_from_dict({"exclude": ["oknoname.*", "(unclosed", 42, ""]})
        self.assertEqual(len(rules.exclude), 1)
        self.assertEqual(rules.classify(PLACEHOLDER), "exclude")
        self.assertEqual([r[0] for r in rules.rejected], ["exclude"] * 3)

    def test_a_non_object_file_is_ignored(self):
        rules = rules_from_dict(["oknoname.*"])
        self.assertFalse(rules.active)
        self.assertEqual(rules.rejected, (("file", "not an object"),))

    def test_demote_weight_is_bounded(self):
        for bad in (1.0, 1.5, -0.1, "x", None):
            rules = rules_from_dict({"demote": ["unknown"], "demote_weight": bad})
            self.assertEqual(rules.demote_weight, DEFAULT_DEMOTE_WEIGHT, bad)
            self.assertIn("demote_weight", [r[0] for r in rules.rejected])
        self.assertEqual(rules_from_dict({"demote_weight": 0.0}).demote_weight, 0.0)

    def test_env_override_is_honoured(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"exclude": ["oknoname.*"]}, fh)
        original = placeholders.PLACEHOLDER_NAME_PATTERNS_FILE
        placeholders.PLACEHOLDER_NAME_PATTERNS_FILE = Path(fh.name)
        load_placeholder_rules.cache_clear()
        try:
            rules = load_placeholder_rules()
            self.assertTrue(rules.active)
            self.assertEqual(rules.source, fh.name)
            self.assertEqual(rules.classify(PLACEHOLDER), "exclude")
        finally:
            placeholders.PLACEHOLDER_NAME_PATTERNS_FILE = original
            load_placeholder_rules.cache_clear()


if __name__ == "__main__":
    unittest.main()
