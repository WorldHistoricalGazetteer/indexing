"""Unit tests for gateway /api/reconcile scope signalling + name variants.

Covers issue place#144:

  * Gap 1 — a ``contained_in`` container with no polygon must not silently
    become an unconstrained query. Either a co-referent's real boundary is used
    (``scope.mode = "linked-polygon"``) or the request fails closed with
    ``scope.applied = False``. No geometry is ever invented.
  * Gap 2 — optional ``variants`` are tried alongside the primary query in
    discovery, unioned and deduped, best score per place.

Also covers place#197 — supplying ``variants`` made reconciliation *worse*:

  * KNN passes were unioned on raw ``_score``, which are cosines to different
    query vectors and not comparable, so a variant's junk neighbours outscored
    the primary's genuine hits. Each pass is now normalised by its own top.
  * The Step-2 candidate pool was an arbitrary doc-order page of the discovery
    set, so the extra ids a variant injects evicted the correct match before it
    was ever ranked. It is now the top-K by discovery score.

Pure-function tests: no live Elasticsearch (the ES round trips are stubbed).
"""

import unittest

from gateway.es_helpers import (
    build_toponym_query,
    collect_place_ids,
    rank_candidate_ids,
)
from gateway.reconcile import (
    MAX_VARIANTS,
    VARIANT_SCORE_WEIGHT,
    ReconcileRequest,
    _build_scope_info,
    _normalise_variants,
)


class _FakeRegion:
    """Stand-in for spatial.ResolvedRegion (only the provenance fields matter)."""

    def __init__(self, source="polygon", area_ids=(), linked_ids=(),
                 point_ids=(), unresolved_ids=()):
        self.source = source
        self.area_ids = area_ids
        self.linked_ids = linked_ids
        self.point_ids = point_ids
        self.unresolved_ids = unresolved_ids


# ---------------------------------------------------------------------------
# Gap 1 — scope signalling
# ---------------------------------------------------------------------------

class TestScopeInfo(unittest.TestCase):
    def test_no_scope_requested_is_silent(self):
        # Responses stay byte-identical for requests that never asked for scope.
        req = ReconcileRequest(query="Sarum")
        self.assertIsNone(_build_scope_info(req, None))

    def test_polygon_container_reports_applied_and_exact(self):
        req = ReconcileRequest(query="Sarum", contained_in=["ukhc:WIL"])
        scope = _build_scope_info(req, _FakeRegion(area_ids=("ukhc:WIL",)))
        self.assertTrue(scope.requested)
        self.assertTrue(scope.applied)
        self.assertEqual(scope.mode, "polygon")
        self.assertFalse(scope.approximate)   # the container's own boundary
        self.assertEqual(scope.containers_polygon, ["ukhc:WIL"])
        self.assertIsNone(scope.message)

    def test_ignored_point_container_is_reported_alongside_polygon(self):
        req = ReconcileRequest(query="Sarum",
                               contained_in=["ukhc:WIL", "wd:Q23154"])
        region = _FakeRegion(area_ids=("ukhc:WIL",), point_ids=("wd:Q23154",))
        scope = _build_scope_info(req, region)
        self.assertTrue(scope.applied)
        self.assertFalse(scope.approximate)
        self.assertEqual(scope.containers_approximated, ["wd:Q23154"])
        self.assertIn("ignored", scope.message)

    def test_linked_polygon_is_reported_as_borrowed(self):
        # gn:3017382 (France, point-only) sameAs wd:Q142 (France, polygon):
        # the scope is exact, but sourced from the co-referent's boundary.
        req = ReconcileRequest(query="Paris", contained_in=["gn:3017382"])
        region = _FakeRegion(source="linked-polygon", linked_ids=("wd:Q142",),
                             point_ids=("gn:3017382",))
        scope = _build_scope_info(req, region)
        self.assertTrue(scope.applied)
        self.assertFalse(scope.approximate)   # a real boundary, not a buffer
        self.assertEqual(scope.mode, "linked-polygon")
        self.assertEqual(scope.containers_linked, ["wd:Q142"])
        self.assertIn("wd:Q142", scope.message)

    def test_unresolvable_container_fails_closed(self):
        # The bug this issue is about: scope requested, nothing resolvable.
        # applied=False is the contract that stops the caller returning the
        # unconstrained result set.
        req = ReconcileRequest(query="Sarum", contained_in=["place:nope:1"])
        scope = _build_scope_info(req, None)
        self.assertTrue(scope.requested)
        self.assertFalse(scope.applied)
        self.assertEqual(scope.mode, "none")
        # the canonical "place:" prefix is stripped, as in resolve_region
        self.assertEqual(scope.containers_unresolved, ["nope:1"])
        self.assertIn("could not be applied", scope.message)

    def test_bounds_degrade_to_coarse_filter_but_stay_applied(self):
        # region_from_geojson failed, yet build_places_filter still applies a
        # repr_point-in-bounds gate — so the query IS constrained.
        req = ReconcileRequest(query="Sarum",
                               bounds={"type": "Polygon", "coordinates": [[]]})
        scope = _build_scope_info(req, None)
        self.assertTrue(scope.applied)
        self.assertEqual(scope.mode, "bbox")
        self.assertTrue(scope.approximate)

    def test_empty_bounds_fail_closed(self):
        req = ReconcileRequest(query="Sarum",
                               bounds={"type": "GeometryCollection", "geometries": []})
        scope = _build_scope_info(req, None)
        self.assertFalse(scope.applied)


# ---------------------------------------------------------------------------
# Gap 2 — name variants
# ---------------------------------------------------------------------------

class TestVariantNormalisation(unittest.TestCase):
    def test_absent_variants_are_a_no_op(self):
        self.assertEqual(_normalise_variants(ReconcileRequest(query="Sarum")), ([], []))

    def test_blank_and_duplicate_forms_dropped(self):
        req = ReconcileRequest(
            query="Sarum",
            variants=["  Old Sarum ", "", "  ", "sarum", "Old sarum", "Searoburh"],
        )
        variants, vectors = _normalise_variants(req)
        self.assertEqual(variants, ["Old Sarum", "Searoburh"])
        self.assertEqual(vectors, [None, None])

    def test_variants_are_capped(self):
        req = ReconcileRequest(query="Sarum",
                               variants=[f"form{i}" for i in range(MAX_VARIANTS + 5)])
        variants, _ = _normalise_variants(req)
        self.assertEqual(len(variants), MAX_VARIANTS)

    def test_client_vectors_stay_aligned_after_dedup(self):
        # A dropped variant must not shift the remaining variants' vectors.
        req = ReconcileRequest(
            query="Sarum",
            variants=["Sarum", "Old Sarum", "Searoburh"],
            variant_vectors=[[0] * 128, [1] * 128, [2] * 128],
        )
        variants, vectors = _normalise_variants(req)
        self.assertEqual(variants, ["Old Sarum", "Searoburh"])
        self.assertEqual([v[0] for v in vectors], [1, 2])


class TestVariantQueryBuilding(unittest.TestCase):
    def test_no_variants_leaves_query_unchanged(self):
        base = build_toponym_query("Sarum", "exact")
        self.assertEqual(build_toponym_query("Sarum", "exact", variants=None), base)
        self.assertEqual(build_toponym_query("Sarum", "exact", variants=[]), base)

    def test_variants_are_dis_max_with_discounted_boost(self):
        body = build_toponym_query("Sarum", "exact",
                                   variants=["Old Sarum", "Searoburh"],
                                   variant_weight=VARIANT_SCORE_WEIGHT)
        dis_max = body["query"]["dis_max"]
        # tie_breaker 0 → a toponym scores its BEST single form, never a sum
        self.assertEqual(dis_max["tie_breaker"], 0.0)
        self.assertEqual(len(dis_max["queries"]), 3)
        # primary clause is unwrapped and unboosted
        self.assertEqual(dis_max["queries"][0], {"term": {"name.keyword": "Sarum"}})
        for clause, form in zip(dis_max["queries"][1:], ["Old Sarum", "Searoburh"]):
            self.assertEqual(clause["bool"]["boost"], VARIANT_SCORE_WEIGHT)
            self.assertEqual(clause["bool"]["must"],
                             [{"term": {"name.keyword": form}}])

    def test_namespace_filter_still_wraps_the_variant_query(self):
        body = build_toponym_query("Sarum", "exact", namespaces=["gn"],
                                   variants=["Old Sarum"])
        self.assertEqual(body["query"]["bool"]["filter"],
                         [{"terms": {"namespaces": ["gn"]}}])
        self.assertIn("dis_max", body["query"]["bool"]["must"][0])

    def test_variants_work_in_every_text_mode(self):
        for mode in ("exact", "starts", "in", "fuzzy"):
            body = build_toponym_query("Sarum", mode, variants=["Old Sarum"])
            self.assertEqual(len(body["query"]["dis_max"]["queries"]), 2, mode)


class TestVariantScoreAccumulation(unittest.TestCase):
    """The KNN path runs one search per form and folds them in with score_scale."""

    @staticmethod
    def _hits(name, score, pids):
        return [{"_score": score, "_source": {"name": name, "attestations": pids}}]

    def test_best_score_per_place_across_passes(self):
        scores = {}
        names = {}
        collect_place_ids(self._hits("Sarum", 0.8, ["gn:1", "gn:2"]), scores,
                          match_names=names)
        # A variant that matches gn:2 better still wins after the discount...
        collect_place_ids(self._hits("Old Sarum", 1.0, ["gn:2", "gn:3"]), scores,
                          match_names=names, score_scale=VARIANT_SCORE_WEIGHT)
        self.assertEqual(set(scores), {"gn:1", "gn:2", "gn:3"})   # union, deduped
        self.assertAlmostEqual(scores["gn:1"], 0.8)
        self.assertAlmostEqual(scores["gn:2"], 0.9)               # 1.0 * 0.9
        self.assertAlmostEqual(scores["gn:3"], 0.9)
        self.assertEqual(names["gn:2"], "Old Sarum")

    def test_equal_match_prefers_the_primary_query(self):
        scores = {}
        names = {}
        collect_place_ids(self._hits("Sarum", 1.0, ["gn:1"]), scores, match_names=names)
        collect_place_ids(self._hits("Old Sarum", 1.0, ["gn:1"]), scores,
                          match_names=names, score_scale=VARIANT_SCORE_WEIGHT)
        self.assertAlmostEqual(scores["gn:1"], 1.0)
        self.assertEqual(names["gn:1"], "Sarum")


class TestCrossPassNormalisation(unittest.TestCase):
    """place#197 — passes run against different query vectors are normalised
    per pass, so raw KNN cosines never compete across passes."""

    @staticmethod
    def _hits(pairs):
        # pairs: [(name, score, [pids]), ...] — one pass's hit list
        return [{"_score": sc, "_source": {"name": n, "attestations": pids}}
                for n, sc, pids in pairs]

    def test_variant_junk_cannot_outrank_the_primary_best(self):
        # The reported shape: the variant's neighbourhood is TIGHTER, so its junk
        # comes back at a higher raw cosine than the primary's genuine hit.
        scores, names = {}, {}
        collect_place_ids(self._hits([("Newton with Scales", 0.88, ["gn:good"])]),
                          scores, match_names=names, normalise=True)
        collect_place_ids(self._hits([("Niujiaozhai", 0.99, ["gn:junk"])]),
                          scores, match_names=names,
                          score_scale=VARIANT_SCORE_WEIGHT, normalise=True)
        self.assertAlmostEqual(scores["gn:good"], 1.0)
        self.assertAlmostEqual(scores["gn:junk"], VARIANT_SCORE_WEIGHT)
        self.assertGreater(scores["gn:good"], scores["gn:junk"])

    def test_within_pass_order_is_preserved(self):
        scores = {}
        collect_place_ids(
            self._hits([("A", 0.9, ["gn:1"]), ("B", 0.8, ["gn:2"])]),
            scores, normalise=True)
        self.assertAlmostEqual(scores["gn:1"], 1.0)
        self.assertAlmostEqual(scores["gn:2"], 0.8 / 0.9)
        self.assertGreater(scores["gn:1"], scores["gn:2"])

    def test_variant_still_promotes_a_place_the_primary_ranked_poorly(self):
        # Normalising must not neuter variants: a place the primary barely
        # reached is still lifted by a strong variant match.
        scores, names = {}, {}
        collect_place_ids(self._hits([("Melford, Long", 1.0, ["gn:other"]),
                                      ("Long Melford", 0.5, ["gn:target"])]),
                          scores, match_names=names, normalise=True)
        collect_place_ids(self._hits([("Long Melford", 0.99, ["gn:target"])]),
                          scores, match_names=names,
                          score_scale=VARIANT_SCORE_WEIGHT, normalise=True)
        self.assertAlmostEqual(scores["gn:target"], VARIANT_SCORE_WEIGHT)
        self.assertEqual(names["gn:target"], "Long Melford")

    def test_empty_and_zero_score_passes_are_safe(self):
        # A degenerate pass (no hits, or every hit at zero) contributes nothing
        # rather than dividing by zero.
        scores = {}
        collect_place_ids([], scores, normalise=True)
        collect_place_ids(self._hits([("X", 0.0, ["gn:1"])]), scores, normalise=True)
        self.assertEqual(scores, {})


class TestCandidatePoolOrdering(unittest.TestCase):
    """place#197 — the Step-2 terms list is the top-K BY DISCOVERY SCORE, not an
    arbitrary slice; ES scores a pure `filter` bool uniformly, so whatever falls
    outside the window is lost before ranking."""

    def test_top_k_by_score(self):
        scores = {"gn:junk1": 0.5, "gn:good": 0.99, "gn:junk2": 0.4}
        self.assertEqual(rank_candidate_ids(scores, 2), ["gn:good", "gn:junk1"])

    def test_correct_match_survives_a_pool_flooded_by_variant_hits(self):
        scores = {f"gn:junk{i}": 0.80 for i in range(400)}
        scores["gn:good"] = 1.0
        self.assertIn("gn:good", rank_candidate_ids(scores, 40))

    def test_tiebreak_is_stable(self):
        scores = {"gn:2": 0.9, "gn:1": 0.9, "gn:10": 0.9}
        self.assertEqual(rank_candidate_ids(scores, 3),
                         rank_candidate_ids(dict(reversed(scores.items())), 3))

    def test_pool_larger_than_the_set_returns_everything(self):
        scores = {"gn:1": 0.2, "gn:2": 0.3}
        self.assertEqual(set(rank_candidate_ids(scores, 100)), {"gn:1", "gn:2"})


class TestCandidateIsArea(unittest.TestCase):
    """_format_candidate marks candidates areal by geom_class, not has_geom."""

    def _geoms(self, src):
        from gateway.reconcile import _format_candidate
        return _format_candidate(src, 50.0).geometries

    def test_polygon_is_area(self):
        g = self._geoms({"place_id": "osm:w1", "geometries": [
            {"repr_point": {"lon": 1, "lat": 2}, "has_geom": True, "geom_class": "area"}]})
        self.assertEqual([(x.is_area, x.has_geom) for x in g], [(True, True)])

    def test_line_is_not_area_despite_has_geom(self):
        # the place#145 case: LineString is has_geom=true but NOT areal
        g = self._geoms({"place_id": "osm:w2", "geometries": [
            {"repr_point": {"lon": 1, "lat": 2}, "has_geom": True, "geom_class": "line"}]})
        self.assertEqual([(x.is_area, x.has_geom) for x in g], [(False, True)])

    def test_point_is_not_area(self):
        g = self._geoms({"place_id": "gn:1", "geometries": [
            {"repr_point": {"lon": 1, "lat": 2}, "has_geom": False, "geom_class": "point"}]})
        self.assertEqual([(x.is_area, x.has_geom) for x in g], [(False, False)])

    def test_legacy_polygon_without_geom_class_is_area(self):
        g = self._geoms({"place_id": "un:FR", "geometries": [
            {"repr_point": {"lon": 1, "lat": 2}, "has_geom": True}]})
        self.assertEqual([(x.is_area, x.has_geom) for x in g], [(True, True)])


if __name__ == "__main__":
    unittest.main()
