"""``/api/search`` must not answer an unapplicable geographic scope globally.

HANDOVER-2026-08-31 §2b, found while checking whether the geom store's missing
``un`` polygons had broken country-scoped search (they had not):

    POST /api/search {"query": "Paris", "contained_in": ["un:not_a_real_place"],
                      "containment": "fuzzy", "relation": "intersects"}
      → 3 hits — Paris (TR), Paris (GA), Paris (GA) — and no `scope` at all

A client with a typo'd or stale place id got a confident **global** answer that
looked scoped, and could not tell the difference: the successful request
(``un:fra`` → 1 hit, ``tgn:8723013``) reported no scope either. Both halves are
the bug, and both are pinned here — this is a silent-wrong-answer class, so the
regression pin matters more than the fix.

``/api/reconcile`` already failed closed (place#144) and its own scope
behaviour is pinned in ``test_reconcile_scope_variants``. What is new is that
the model and its builder are now shared (``gateway.spatial.build_scope_info``),
so the two endpoints cannot answer the same question differently again — the
parity test below is the point of that move.

Pure-function / stubbed tests: no live Elasticsearch.
"""

from __future__ import annotations

import unittest
from unittest import mock

from gateway import spatial
from gateway.reconcile import ReconcileRequest, _build_scope_info
from gateway.search import SearchRequest, SearchResponse, search


class _FakeRegion:
    """Stand-in for spatial.ResolvedRegion (only the provenance fields matter)."""

    def __init__(self, source="polygon", area_ids=(), linked_ids=(),
                 point_ids=(), unresolved_ids=()):
        self.source = source
        self.area_ids = area_ids
        self.linked_ids = linked_ids
        self.point_ids = point_ids
        self.unresolved_ids = unresolved_ids


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient covering the search round trips.

    Records every URL posted to, so a test can assert that a failed-closed
    request queried Elasticsearch **not at all** — returning zero hits after
    running the unscoped query would still be wrong.
    """

    def __init__(self, *_args, **_kwargs):
        self.posted: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None, auth=None, headers=None):
        self.posted.append(url)
        if "/toponyms" in url:
            # Step 1 discovery, then (after the places round trip) step 3
            # enrichment — told apart by order, not by body shape.
            if "/places" in " ".join(self.posted):
                return _FakeResponse({"hits": {"hits": [
                    {"_source": {"name": "Paris", "lang": "en",
                                 "attestations": ["tgn:8723013"]}},
                ]}})
            return _FakeResponse({"hits": {"hits": [
                {"_score": 12.0, "_source": {"name": "Paris",
                                             "attestations": ["tgn:8723013"]}},
            ]}})
        # Step 2 places
        return _FakeResponse({
            "hits": {"total": {"value": 1}, "hits": [
                {"_source": {"place_id": "tgn:8723013", "title": "Paris",
                             "namespace": "tgn", "ccodes": ["FR"],
                             "types": [], "geometries": []}},
            ]},
            "aggregations": {},
        })


def _real_region(**provenance) -> spatial.ResolvedRegion:
    """A genuine ResolvedRegion — the endpoint path passes it to
    ``build_places_filter``, which reads fields a provenance-only stub lacks."""
    return spatial.ResolvedRegion(
        cover_by_res={7: {"871f9a1ffffffff"}},
        resolutions=(7,),
        bbox_geojson={"type": "Polygon", "coordinates": [
            [[2.0, 48.0], [3.0, 48.0], [3.0, 49.0], [2.0, 49.0], [2.0, 48.0]]]},
        h3_terms=["871f9a1ffffffff"],
        **provenance,
    )


# ---------------------------------------------------------------------------
# The endpoint — fail closed, and say why
# ---------------------------------------------------------------------------

class TestSearchFailsClosed(unittest.IsolatedAsyncioTestCase):

    async def test_unresolvable_container_returns_nothing_and_explains(self):
        """The §2b reproduction. No hits, and a scope saying what happened."""
        client = _FakeClient()
        with mock.patch.object(spatial, "resolve_region",
                               new=mock.AsyncMock(return_value=None)), \
             mock.patch("gateway.search.httpx.AsyncClient",
                        side_effect=lambda *a, **k: client):
            resp = await search(SearchRequest(
                query="Paris", contained_in=["un:not_a_real_place"],
                containment="fuzzy", relation="intersects"))

        self.assertEqual([], resp.hits)
        self.assertEqual(0, resp.total)
        self.assertIsNotNone(resp.scope)
        self.assertTrue(resp.scope.requested)
        self.assertFalse(resp.scope.applied)
        self.assertEqual("none", resp.scope.mode)
        self.assertEqual(["un:not_a_real_place"], resp.scope.containers_unresolved)
        self.assertIn("could not be applied", resp.scope.message)
        # ...and it did not fall back to answering the UNSCOPED query. Zero hits
        # from a global search would satisfy every assertion above while still
        # being the bug (the real index has three Parises to return).
        self.assertEqual([], client.posted)

    async def test_applied_scope_is_reported_on_the_successful_path(self):
        """The other half of §2b: `scope` was null even when scoping worked."""
        client = _FakeClient()
        region = _real_region(area_ids=("un:fra",))
        with mock.patch.object(spatial, "resolve_region",
                               new=mock.AsyncMock(return_value=region)), \
             mock.patch.object(spatial, "apply_containment_async",
                               new=mock.AsyncMock(side_effect=lambda hits, *a, **k: hits)), \
             mock.patch("gateway.search.httpx.AsyncClient",
                        side_effect=lambda *a, **k: client):
            resp = await search(SearchRequest(
                query="Paris", mode="exact", contained_in=["un:fra"],
                containment="fuzzy", relation="intersects"))

        self.assertEqual(["tgn:8723013"], [h.place_id for h in resp.hits])
        self.assertIsNotNone(resp.scope)
        self.assertTrue(resp.scope.applied)
        self.assertEqual("polygon", resp.scope.mode)
        self.assertEqual(["un:fra"], resp.scope.containers_polygon)

    async def test_unscoped_request_reports_no_scope(self):
        """A request that never asked for scope is answered exactly as before."""
        client = _FakeClient()
        with mock.patch("gateway.search.httpx.AsyncClient",
                        side_effect=lambda *a, **k: client):
            resp = await search(SearchRequest(query="Paris", mode="exact"))

        self.assertIsNone(resp.scope)
        self.assertEqual(["tgn:8723013"], [h.place_id for h in resp.hits])

    def test_scope_field_defaults_to_none(self):
        self.assertIsNone(SearchResponse().scope)


# ---------------------------------------------------------------------------
# The builder — one implementation, so the two endpoints cannot drift
# ---------------------------------------------------------------------------

class TestSharedScopeBuilder(unittest.TestCase):

    def test_no_scope_requested_is_silent(self):
        self.assertIsNone(spatial.build_scope_info(region=None))

    def test_search_and_reconcile_agree_on_an_unresolvable_container(self):
        ids = ["un:not_a_real_place"]
        from_search = spatial.build_scope_info(region=None, contained_in=ids)
        from_reconcile = _build_scope_info(
            ReconcileRequest(query="Paris", contained_in=ids), None)
        self.assertEqual(from_search.model_dump(), from_reconcile.model_dump())
        self.assertFalse(from_search.applied)

    def test_search_and_reconcile_agree_on_a_borrowed_polygon(self):
        # gn:3017382 (France, point-only) sameAs wd:Q142 (France, polygon).
        region = _FakeRegion(source="linked-polygon", linked_ids=("wd:Q142",),
                             point_ids=("gn:3017382",))
        ids = ["gn:3017382"]
        from_search = spatial.build_scope_info(region=region, contained_in=ids)
        from_reconcile = _build_scope_info(
            ReconcileRequest(query="Paris", contained_in=ids), region)
        self.assertEqual(from_search.model_dump(), from_reconcile.model_dump())
        self.assertTrue(from_search.applied)
        self.assertEqual("linked-polygon", from_search.mode)

    def test_bounds_degrade_to_coarse_filter_but_stay_applied(self):
        scope = spatial.build_scope_info(
            region=None, bounds={"type": "Polygon", "coordinates": [[]]})
        self.assertTrue(scope.applied)
        self.assertEqual("bbox", scope.mode)
        self.assertTrue(scope.approximate)

    def test_empty_bounds_fail_closed(self):
        scope = spatial.build_scope_info(
            region=None, bounds={"type": "GeometryCollection", "geometries": []})
        self.assertFalse(scope.applied)


if __name__ == "__main__":
    unittest.main()
