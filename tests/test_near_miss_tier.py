"""Discovery's near-miss lexical tier + absolute match confidence (place#199).

The reported symptom: "Broxbourn (St. Augustine)" never matches. The client half
(deriving `Broxbourn` as a variant) was fixed on whg3, and made no difference —
the gateway's `fuzzy`/`phonetic` discovery had exactly two channels, phonetic KNN
and an EXACT `name.raw` lookup, so a variant could only ever help by being
spelled exactly as indexed. `Broxbourn` is not indexed (`Broxbourne` is), so the
result set came back byte-identical to the one with no variants at all, even
though `Broxbourn` finds the place perfectly when it IS the query.

Three things are tested here, matching the three asks on the issue:

  A. a near-miss tier between exact and phonetic, scored on real resemblance;
  B. a KNN pass now contributes in proportion to how good its best hit actually
     is, so a variant that found the answer beats a primary that found noise;
  C. an ABSOLUTE confidence, because the displayed score is normalised by the
     pool's best and is therefore ~100 even when nothing matched (place#198).

The tier ordering place#197 established must survive all three: an exactly
spelled candidate still outranks every inexact one. Pure-function tests; the ES
round trips are not involved.
"""

import unittest

from gateway.es_helpers import (
    KNN_SIMILARITY_FLOOR,
    LEXICAL_EXACT_BOOST,
    LEXICAL_FUZZY_BOOST,
    LEXICAL_FUZZY_FLOOR,
    MAX_DISCOVERY_SCORE,
    absolute_confidence,
    apply_lexical_boost,
    apply_lexical_near_miss,
    build_lexical_fuzzy_query,
    collect_place_ids,
    knn_pass_quality,
    name_resemblance,
    rank_candidate_ids,
)
from gateway.es_helpers import (
    DERIVED_LOSSY_WEIGHT,
    MAX_DERIVED_FORMS,
    VARIANT_SCORE_WEIGHT,
    derive_name_forms,
    derived_form_weight,
)


def _hits(pairs):
    """[(name, [place_ids]), ...] — a text/lexical pass's hit list."""
    return [{"_source": {"name": n, "attestations": pids}} for n, pids in pairs]


def _knn(pairs, cosine=True):
    """[(name, cosine, [place_ids]), ...] — a KNN pass, scored as ES scores it.

    ES scores a `similarity: cosine` dense_vector as (1 + cosine) / 2, so tests
    that care about absolute quality must speak in cosines, not _scores.
    """
    return [{"_score": ((1 + c) / 2 if cosine else c),
             "_source": {"name": n, "attestations": pids}}
            for n, c, pids in pairs]


# ---------------------------------------------------------------------------
# A — the near-miss tier
# ---------------------------------------------------------------------------

class TestNameResemblance(unittest.TestCase):
    def test_a_near_miss_scores_high(self):
        self.assertGreater(name_resemblance("Broxbourn", "Broxbourne"), 0.9)

    def test_case_is_irrelevant(self):
        self.assertEqual(name_resemblance("BROXBOURN", "broxbourn"), 1.0)

    def test_an_unrelated_name_scores_below_the_floor(self):
        self.assertLess(name_resemblance("Broxbourn", "Aït Bourzouine"),
                        LEXICAL_FUZZY_FLOOR)

    def test_a_bracketed_qualifier_still_resembles_the_bare_name(self):
        # Row 1 of the issue: even with NO variants supplied, the raw cell value
        # must reach the record it qualifies.
        self.assertGreater(
            name_resemblance("Broxbourn (St. Augustine)", "Broxbourne"),
            LEXICAL_FUZZY_FLOOR)

    def test_blank_forms_are_safe(self):
        self.assertEqual(name_resemblance("", "Broxbourne"), 0.0)
        self.assertEqual(name_resemblance("Broxbourne", "   "), 0.0)


class TestNearMissQuery(unittest.TestCase):
    def test_primary_and_variants_share_one_pass(self):
        body = build_lexical_fuzzy_query(
            ["Broxbourn (St. Augustine)", "Broxbourn"],
            variant_weight=VARIANT_SCORE_WEIGHT)
        dis_max = body["query"]["dis_max"]
        self.assertEqual(len(dis_max["queries"]), 2)
        self.assertEqual(dis_max["queries"][1]["bool"]["boost"], VARIANT_SCORE_WEIGHT)

    def test_it_uses_the_fuzzy_text_clause(self):
        body = build_lexical_fuzzy_query(["Broxbourn"])
        clause = body["query"]["bool"]["should"][0]["multi_match"]
        self.assertEqual(clause["fuzziness"], "AUTO")
        self.assertIn("name^3", clause["fields"])

    def test_namespace_scope_is_pushed_into_discovery(self):
        body = build_lexical_fuzzy_query(["Broxbourn"], namespaces=["gn"])
        self.assertEqual(body["query"]["bool"]["filter"],
                         [{"terms": {"namespaces": ["gn"]}}])

    def test_nothing_to_look_up_returns_none(self):
        self.assertIsNone(build_lexical_fuzzy_query([]))
        self.assertIsNone(build_lexical_fuzzy_query(["", "  "]))


class TestNearMissTier(unittest.TestCase):
    def test_a_variant_that_is_not_indexed_still_finds_the_place(self):
        """Row 2 of the issue — the whole point.

        Primary is a bracketed form that matches nothing; the variant
        `Broxbourn` is NOT indexed, so the exact tier cannot see it either.
        """
        scores, names = {}, {}
        collect_place_ids(_knn([("Aït Bourzouine", 0.72, ["gn:junk"])]),
                          scores, match_names=names,
                          score_scale=knn_pass_quality(
                              _knn([("Aït Bourzouine", 0.72, ["gn:junk"])])),
                          normalise=True)
        apply_lexical_near_miss(
            _hits([("Broxbourne", ["gn:good"])]), scores,
            {"Broxbourn (St. Augustine)": 1.0, "Broxbourn": VARIANT_SCORE_WEIGHT},
            match_names=names)
        self.assertEqual(rank_candidate_ids(scores, 1), ["gn:good"])
        self.assertEqual(names["gn:good"], "Broxbourne")

    def test_junk_below_the_floor_contributes_nothing(self):
        # A long query retrieves anything sharing one common token ("St").
        scores = {}
        apply_lexical_near_miss(
            _hits([("St Kilda", ["gn:junk"])]), scores,
            {"Minster-in-Sheppy (St. Mary and St. Sexburgh)": 1.0})
        self.assertEqual(scores, {})

    def test_the_tier_is_additive_not_a_max(self):
        # Found phonetically AND lexically beats found only one way.
        scores = {}
        collect_place_ids(_knn([("Broxbourne", 0.95, ["gn:both"])]),
                          scores, normalise=True)
        apply_lexical_near_miss(_hits([("Broxbourne", ["gn:both", "gn:lex"])]),
                                scores, {"Broxbourn": 1.0})
        self.assertGreater(scores["gn:both"], scores["gn:lex"])

    def test_an_exact_match_still_outranks_near_miss_plus_phonetic(self):
        """place#197's invariant, which the new tier must not breach."""
        scores = {}
        # gn:near gets EVERYTHING the inexact tiers can give: a perfect phonetic
        # pass and a perfect resemblance.
        collect_place_ids(_knn([("Broxbourne", 1.0, ["gn:near"])]),
                          scores, normalise=True)
        apply_lexical_near_miss(_hits([("Broxbourne", ["gn:near"])]), scores,
                                {"Broxbourne": 1.0})
        apply_lexical_boost(_hits([("Long Melford", ["gn:exact"])]), scores,
                            {"long melford": LEXICAL_EXACT_BOOST})
        self.assertLessEqual(scores["gn:near"], 1.0 + LEXICAL_FUZZY_BOOST)
        self.assertGreater(scores["gn:exact"], scores["gn:near"])

    def test_a_variant_near_miss_is_worth_less_than_the_same_primary_one(self):
        primary, variant = {}, {}
        apply_lexical_near_miss(_hits([("Broxbourne", ["gn:1"])]), primary,
                                {"Broxbourn": 1.0})
        apply_lexical_near_miss(_hits([("Broxbourne", ["gn:1"])]), variant,
                                {"Broxbourn": VARIANT_SCORE_WEIGHT})
        self.assertGreater(primary["gn:1"], variant["gn:1"])

    def test_match_names_keeps_the_dominant_evidence(self):
        # A strong phonetic hit must not be relabelled by a weaker near-miss.
        scores, names = {}, {}
        collect_place_ids(_knn([("Broxbourne", 1.0, ["gn:1"])]), scores,
                          match_names=names, normalise=True)
        apply_lexical_near_miss(_hits([("Broxbourn Hall", ["gn:1"])]), scores,
                                {"Broxbourn": 1.0}, match_names=names)
        self.assertEqual(names["gn:1"], "Broxbourne")

    def test_namespace_filters_are_honoured(self):
        scores = {}
        apply_lexical_near_miss(
            _hits([("Broxbourne", ["gn:1", "gb:2"])]), scores,
            {"Broxbourn": 1.0}, exclude_prefixes=("gb:",))
        self.assertEqual(list(scores), ["gn:1"])

    def test_empty_inputs_are_safe(self):
        scores = {}
        self.assertEqual(apply_lexical_near_miss([], scores, {"X": 1.0}), 0)
        self.assertEqual(apply_lexical_near_miss(_hits([("X", ["gn:1"])]),
                                                 scores, {}), 0)
        self.assertEqual(scores, {})


# ---------------------------------------------------------------------------
# B — a KNN pass contributes in proportion to how good it really is
# ---------------------------------------------------------------------------

class TestKnnPassQuality(unittest.TestCase):
    def test_a_perfect_neighbour_scores_one(self):
        self.assertAlmostEqual(knn_pass_quality(_knn([("A", 1.0, ["gn:1"])])), 1.0)

    def test_a_floor_scraping_neighbour_scores_zero(self):
        self.assertAlmostEqual(
            knn_pass_quality(_knn([("A", KNN_SIMILARITY_FLOOR, ["gn:1"])])), 0.0)

    def test_an_empty_pass_scores_zero(self):
        self.assertEqual(knn_pass_quality([]), 0.0)

    def test_an_unexpected_score_scale_leaves_the_pass_unscaled(self):
        # Defensive: a dot_product index (or a mapping change) must not silently
        # mangle every score — better unscaled than wrong.
        self.assertEqual(knn_pass_quality([{"_score": 7.4, "_source": {}}]), 1.0)

    def test_a_good_variant_pass_beats_a_hopeless_primary_pass(self):
        """Row 2 vs row 4 of the issue, in scores.

        The primary matched noise (barely above the floor); the variant found a
        near-identical neighbour. Before place#199 both passes were crowned at
        their own top and the variant's 0.9 discount decided it.
        """
        primary = _knn([("Aït Bourzouine", 0.71, ["gn:junk"])])
        variant = _knn([("Broxbourne", 0.99, ["gn:good"])])
        scores = {}
        collect_place_ids(primary, scores, score_scale=knn_pass_quality(primary),
                          normalise=True)
        collect_place_ids(
            variant, scores,
            score_scale=VARIANT_SCORE_WEIGHT * knn_pass_quality(variant),
            normalise=True)
        self.assertEqual(rank_candidate_ids(scores, 1), ["gn:good"])

    def test_within_pass_ordering_is_untouched(self):
        # place#197: the scaling is a constant per pass, so it re-ranks nothing
        # inside one.
        hits = _knn([("A", 0.95, ["gn:1"]), ("B", 0.90, ["gn:2"]),
                     ("C", 0.85, ["gn:3"])])
        plain, scaled = {}, {}
        collect_place_ids(hits, plain, normalise=True)
        collect_place_ids(hits, scaled, score_scale=knn_pass_quality(hits),
                          normalise=True)
        self.assertEqual(rank_candidate_ids(plain, 10), rank_candidate_ids(scaled, 10))


# ---------------------------------------------------------------------------
# C — an absolute confidence, because the score is always ~100
# ---------------------------------------------------------------------------

class TestAbsoluteConfidence(unittest.TestCase):
    def test_an_exact_match_found_every_way_is_full_confidence(self):
        raw = LEXICAL_EXACT_BOOST + LEXICAL_FUZZY_BOOST + 1.0
        self.assertEqual(absolute_confidence(raw), 100.0)

    def test_phonetic_noise_is_near_zero_however_the_pool_ranks_it(self):
        # The place#198 shape: this candidate is TOP of its pool, so its
        # displayed score is 100. Its confidence must not be.
        scores = {}
        hits = _knn([("Aït Bourzouine", 0.71, ["gn:junk"])])
        collect_place_ids(hits, scores, score_scale=knn_pass_quality(hits),
                          normalise=True)
        best = max(scores.values())
        self.assertEqual(round(best / best * 100), 100)      # the displayed score
        self.assertLess(absolute_confidence(best), 5)        # the honest one

    def test_confidence_is_monotonic_in_the_raw_score(self):
        values = [absolute_confidence(r) for r in (0.0, 0.4, 1.0, 1.75, 2.0, 3.75)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 0.0)

    def test_it_never_exceeds_one_hundred(self):
        self.assertEqual(absolute_confidence(MAX_DISCOVERY_SCORE * 10), 100.0)

    def test_a_negative_or_zero_score_is_zero(self):
        self.assertEqual(absolute_confidence(0.0), 0.0)
        self.assertEqual(absolute_confidence(-1.0), 0.0)


# ---------------------------------------------------------------------------
# Server-side de-bracketing — the no-variant path
# ---------------------------------------------------------------------------

class TestDeriveNameForms(unittest.TestCase):
    """A bracketed qualifier is the asker's apparatus, never part of an indexed
    toponym, so the raw string is the one form guaranteed not to match."""

    def test_both_readings_of_a_qualifier_are_tried(self):
        self.assertEqual(derive_name_forms("Broxbourn (St. Augustine)"),
                         ["Broxbourn", "Broxbourn St. Augustine"])

    def test_a_qualifier_that_is_part_of_the_name_survives(self):
        # Newcastle upon Tyne is one place; Broxbourn St Augustine is a parish of
        # another. Which reading is right is not knowable here, so try both.
        self.assertIn("Newcastle upon Tyne", derive_name_forms("Newcastle (upon Tyne)"))

    def test_an_unbracketed_query_derives_nothing(self):
        # The overwhelming majority of queries — responses stay byte-identical.
        self.assertEqual(derive_name_forms("Broxbourne"), [])
        self.assertEqual(derive_name_forms(""), [])
        self.assertEqual(derive_name_forms("Minster-in-Sheppy"), [])

    def test_unbalanced_brackets_degrade_gracefully(self):
        # The pair form finds nothing; stripping the characters still works.
        self.assertEqual(derive_name_forms("Broxbourn (St. Augustine"),
                         ["Broxbourn St. Augustine"])

    def test_a_wholly_bracketed_query_yields_its_content(self):
        # Removing the pair would leave nothing, so only one form survives.
        self.assertEqual(derive_name_forms("(St. Augustine)"), ["St. Augustine"])

    def test_dangling_punctuation_is_tidied(self):
        self.assertEqual(derive_name_forms("Melford, (Long)")[0], "Melford")

    def test_square_and_curly_brackets_count(self):
        self.assertEqual(derive_name_forms("Sarum [Old]"), ["Sarum", "Sarum Old"])

    def test_derived_forms_never_repeat_the_query(self):
        for q in ("Broxbourn (St. Augustine)", "(Sarum)", "A [B] (C)"):
            self.assertNotIn(q.casefold(), [f.casefold() for f in derive_name_forms(q)])

    def test_the_reported_row_one_case_now_reaches_the_record(self):
        """place#199 row 1 — the bracketed query with NO variants supplied.

        The derived form gets a full KNN pass of its own, which is what lifts it
        past the primary's phonetic noise; the near-miss tier alone could not
        (0.75 ceiling vs a saturated ~0.93 noise floor).
        """
        derived = derive_name_forms("Broxbourn (St. Augustine)")
        scores = {}
        # primary pass: noise, but noise sitting high in a saturated space
        primary = _knn([("Ait Bourzouine", 0.979, ["gn:junk"])])
        collect_place_ids(primary, scores, score_scale=knn_pass_quality(primary),
                          normalise=True)
        # derived pass: the record itself
        dpass = _knn([("Broxbourne", 0.998, ["gn:good"])])
        collect_place_ids(dpass, scores,
                          score_scale=VARIANT_SCORE_WEIGHT * knn_pass_quality(dpass),
                          normalise=True)
        apply_lexical_near_miss(
            _hits([("Broxbourne", ["gn:good"])]), scores,
            {"Broxbourn (St. Augustine)": 1.0,
             **{f: VARIANT_SCORE_WEIGHT for f in derived}})
        self.assertEqual(rank_candidate_ids(scores, 1), ["gn:good"])


class TestTrailingQualifierForms(unittest.TestCase):
    """place#205 — `Place, County` is how gazetteer columns are written, and no
    existing tier reaches it: the full string is indexed nowhere, it is ~9 edits
    from the head word, it is LONGER than its target so starts/in don't apply,
    and the KNN embeds the qualifier too, so the neighbourhood is unrelated.
    The last one is why the failure is a confident-looking wrong answer rather
    than an empty one."""

    def test_the_head_word_is_offered(self):
        self.assertIn("Bury St. Edmunds", derive_name_forms("Bury St. Edmunds, Suffolk"))

    def test_the_inversion_is_offered_too(self):
        # place#188's case. The string cannot say which reading is right, so
        # both are tried — exactly as the two bracket readings are.
        self.assertIn("Long Melford", derive_name_forms("Melford, Long"))

    def test_the_head_word_comes_first(self):
        # It is the reading that rescues the common failure, so it is the one
        # that survives if MAX_DERIVED_FORMS bites.
        self.assertEqual(derive_name_forms("Kingston, Surrey")[0], "Kingston")

    def test_a_three_part_hierarchy_is_not_inverted(self):
        # "Kingston, Surrey, England" is a hierarchy, not a qualified name;
        # inverting it would guess at which level the caller meant. The head
        # word is still offered, which is the useful part.
        forms = derive_name_forms("Kingston, Surrey, England")
        self.assertEqual(forms, ["Kingston"])

    def test_a_query_with_no_comma_or_bracket_derives_nothing(self):
        for q in ("Bury St Edmunds", "Broxbourne", "Minster in Sheppy"):
            self.assertEqual(derive_name_forms(q), [], q)

    def test_malformed_comma_forms_are_safe(self):
        for q in (", Suffolk", "Somerset,", ",", " , "):
            self.assertEqual(derive_name_forms(q), [], repr(q))

    def test_brackets_and_commas_together_stay_bounded(self):
        # Each derived form is another concurrent KNN pass.
        forms = derive_name_forms("Melford, Long (St. Catherine)")
        self.assertLessEqual(len(forms), MAX_DERIVED_FORMS)

    def test_the_cap_holds_for_every_shape(self):
        for q in ("A, B (C)", "A (B) [C], D", "Bury St. Edmunds, Suffolk"):
            self.assertLessEqual(len(derive_name_forms(q)), MAX_DERIVED_FORMS, q)

    def test_a_rearrangement_is_worth_as_much_as_a_client_variant(self):
        # Every token survives — it is the same question, asked differently.
        self.assertEqual(derived_form_weight("Melford, Long", "Long Melford"),
                         VARIANT_SCORE_WEIGHT)

    def test_a_truncation_is_worth_less(self):
        # place#205: scored equally, real places called "Melford" took rank 1
        # from Long Melford. A form that discards a token answered a narrower
        # question and must not outrank one that kept everything.
        self.assertEqual(derived_form_weight("Melford, Long", "Melford"),
                         DERIVED_LOSSY_WEIGHT)
        self.assertLess(DERIVED_LOSSY_WEIGHT, VARIANT_SCORE_WEIGHT)

    def test_the_bracket_readings_are_graded_the_same_way(self):
        q = "Broxbourn (St. Augustine)"
        self.assertEqual(derived_form_weight(q, "Broxbourn"), DERIVED_LOSSY_WEIGHT)
        self.assertEqual(derived_form_weight(q, "Broxbourn St. Augustine"),
                         VARIANT_SCORE_WEIGHT)

    def test_grading_ignores_punctuation_and_case(self):
        self.assertEqual(derived_form_weight("Bury St. Edmunds, Suffolk",
                                             "suffolk bury st edmunds"),
                         VARIANT_SCORE_WEIGHT)

    def test_a_lossy_exact_match_still_beats_every_inexact_candidate(self):
        # The whole tier ordering has to survive the discount.
        lossy_exact = LEXICAL_EXACT_BOOST * DERIVED_LOSSY_WEIGHT
        self.assertGreater(lossy_exact, LEXICAL_FUZZY_BOOST + 1.0)

    def test_a_derived_form_never_repeats_the_query(self):
        for q in ("Bury St. Edmunds, Suffolk", "Melford, Long", "Kingston, Surrey"):
            self.assertNotIn(q.casefold(),
                             [f.casefold() for f in derive_name_forms(q)], q)


class TestTierOrderingInvariant(unittest.TestCase):
    """The three tiers must stay strictly ordered, whatever the constants."""

    def test_exact_outranks_everything_inexact_can_reach(self):
        inexact_ceiling = LEXICAL_FUZZY_BOOST + 1.0        # near-miss + phonetic
        variant_exact = LEXICAL_EXACT_BOOST * VARIANT_SCORE_WEIGHT
        self.assertGreater(variant_exact, inexact_ceiling)

    def test_every_form_weight_clears_the_tier_floor(self):
        # Dropping a form weight below the tier arithmetic would let a primary's
        # near-miss outrank another form's EXACT match. Guard both constants —
        # this is what the LEXICAL_EXACT_BOOST rise to 2.5 bought room for.
        floor = (LEXICAL_FUZZY_BOOST + 1.0) / LEXICAL_EXACT_BOOST
        self.assertGreater(VARIANT_SCORE_WEIGHT, floor)
        self.assertGreater(DERIVED_LOSSY_WEIGHT, floor)

    def test_a_perfect_near_miss_outranks_a_perfect_phonetic_hit(self):
        # ...only in combination: the tier itself is deliberately below 1.0, so
        # phonetic proximity still orders WITHIN the near-miss tier.
        self.assertLess(LEXICAL_FUZZY_BOOST, 1.0)


if __name__ == "__main__":
    unittest.main()
