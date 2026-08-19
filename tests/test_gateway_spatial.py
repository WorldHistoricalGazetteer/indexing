"""Unit tests for the gateway spatial-containment engine (gateway/spatial.py).

Mirrors the style of tests/test_ccode_enrichment.py: pure-function tests with
synthetic square/point fixtures, skipped when h3 or shapely are unavailable.
No live Elasticsearch — resolve_region is exercised against a stub client.
"""

import asyncio
import unittest

try:
    import h3  # noqa: F401
    import shapely  # noqa: F401
    _DEPS = True
except Exception:  # pragma: no cover
    _DEPS = False

if _DEPS:
    from gateway import spatial


def _square(x0, y0, x1, y1):
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }


def _hit(geoms=None, rp=None, cover=None):
    """Build a fake ES hit ``{"_source": {...}}``."""
    src = {"geometries": []}
    for g in (geoms or []):
        src["geometries"].append({"geom": g})
    if rp is not None:
        src["repr_point"] = {"lon": rp[0], "lat": rp[1]}
    if cover is not None:
        if not src["geometries"]:
            src["geometries"].append({})
        src["geometries"][0]["h3_cover"] = cover
    return {"_source": src}


def _cover_for(geom_geojson, res=6):
    """Compute an h3_cover (compacted) for a polygon, as ingestion would."""
    shape_obj = h3.geo_to_h3shape(geom_geojson)
    cells = h3.h3shape_to_cells(shape_obj, res)
    return list(h3.compact_cells(list(cells)))


@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestRegionBuild(unittest.TestCase):
    def test_cover_built_and_bounded(self):
        region = spatial.region_from_geojson(_square(0, 0, 2, 2))
        self.assertIsNotNone(region)
        self.assertTrue(region.has_cover)
        self.assertIsNotNone(region.prepared)  # bounds-built region has geometry
        self.assertTrue(region.resolutions)
        total = sum(len(c) for c in region.cover_by_res.values())
        self.assertGreater(total, 0)
        self.assertLessEqual(total, spatial.H3_POLYFILL_MAX_CELLS)
        self.assertTrue(region.h3_terms)

    def test_continent_scale_stays_bounded(self):
        region = spatial.region_from_geojson(_square(-60, -40, 60, 40))
        self.assertIsNotNone(region)
        total = sum(len(c) for c in region.cover_by_res.values())
        self.assertLessEqual(total, spatial.H3_POLYFILL_MAX_CELLS)

    def test_cache_hit_returns_same_object(self):
        a = spatial.region_from_geojson(_square(10, 10, 11, 11))
        b = spatial.region_from_geojson(_square(10, 10, 11, 11))
        self.assertIs(a, b)


@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestFuzzyContains(unittest.TestCase):
    def setUp(self):
        self.region = spatial.region_from_geojson(_square(0, 0, 2, 2))

    def test_point_inside_passes(self):
        h = _hit(geoms=[{"type": "Point", "coordinates": [1, 1]}], rp=[1, 1])
        self.assertTrue(spatial.hit_matches(h["_source"], self.region, "fuzzy", "intersects"))

    def test_point_far_outside_fails(self):
        h = _hit(geoms=[{"type": "Point", "coordinates": [20, 20]}], rp=[20, 20])
        self.assertFalse(spatial.hit_matches(h["_source"], self.region, "fuzzy", "intersects"))

    def test_areal_overlap_via_cover(self):
        # Polygon straddling the region border; its repr_point lies OUTSIDE the
        # region, so fuzzy must rely on the stored h3_cover to detect overlap.
        straddle = _square(1.5, 1.5, 3.0, 3.0)
        h = _hit(rp=[2.25, 2.25], cover=_cover_for(straddle))
        self.assertTrue(spatial.hit_matches(h["_source"], self.region, "fuzzy", "intersects"))


@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestExactContains(unittest.TestCase):
    def setUp(self):
        self.region = spatial.region_from_geojson(_square(0, 0, 2, 2))

    def test_point_inside_outside(self):
        inside = _hit(geoms=[{"type": "Point", "coordinates": [1, 1]}], rp=[1, 1])
        outside = _hit(geoms=[{"type": "Point", "coordinates": [9, 9]}], rp=[9, 9])
        self.assertTrue(spatial.hit_matches(inside["_source"], self.region, "exact", "intersects"))
        self.assertFalse(spatial.hit_matches(outside["_source"], self.region, "exact", "intersects"))

    def test_straddle_intersects_but_not_within(self):
        straddle = _square(1.5, 1.5, 3.0, 3.0)
        h = _hit(geoms=[straddle], rp=[2.25, 2.25])
        self.assertTrue(spatial.hit_matches(h["_source"], self.region, "exact", "intersects"))
        self.assertFalse(spatial.hit_matches(h["_source"], self.region, "exact", "within"))

    def test_fully_inside_within(self):
        inner = _square(0.5, 0.5, 1.5, 1.5)
        h = _hit(geoms=[inner], rp=[1, 1])
        self.assertTrue(spatial.hit_matches(h["_source"], self.region, "exact", "within"))


@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestApplyContainment(unittest.TestCase):
    def test_filters_and_exact_subset_of_fuzzy(self):
        region = spatial.region_from_geojson(_square(0, 0, 2, 2))
        straddle = _square(1.5, 1.5, 3.0, 3.0)
        hits = [
            _hit(geoms=[{"type": "Point", "coordinates": [1, 1]}], rp=[1, 1]),       # in
            _hit(geoms=[{"type": "Point", "coordinates": [50, 50]}], rp=[50, 50]),   # out
            _hit(geoms=[straddle], rp=[2.25, 2.25], cover=_cover_for(straddle)),     # overlap
        ]
        fuzzy = spatial.apply_containment(hits, region, "fuzzy", "intersects")
        exact = spatial.apply_containment(hits, region, "exact", "intersects")
        # The far-outside point is dropped by both.
        self.assertEqual(len(fuzzy), 2)
        self.assertEqual(len(exact), 2)
        # Exact ⊆ fuzzy (fuzzy is the tolerant gate; with cover populated they agree here).
        fuzzy_ids = {id(h) for h in fuzzy}
        for h in exact:
            self.assertIn(id(h), fuzzy_ids)


@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestApplyContainmentAsync(unittest.TestCase):
    """place#165: the exact path now does real work, so it runs off the loop.

    ``apply_containment_async`` must be a drop-in for ``apply_containment`` —
    same results — while keeping the Shapely refine out of the event loop.
    """

    def setUp(self):
        self.region = spatial.region_from_geojson(_square(0, 0, 2, 2))
        straddle = _square(1.5, 1.5, 3.0, 3.0)
        self.hits = [
            _hit(geoms=[{"type": "Point", "coordinates": [1, 1]}], rp=[1, 1]),
            _hit(geoms=[{"type": "Point", "coordinates": [50, 50]}], rp=[50, 50]),
            _hit(geoms=[straddle], rp=[2.25, 2.25], cover=_cover_for(straddle)),
        ]

    def test_matches_sync_results_for_fuzzy_and_exact(self):
        for mode in ("fuzzy", "exact"):
            sync = spatial.apply_containment(self.hits, self.region, mode, "intersects")
            got = asyncio.run(
                spatial.apply_containment_async(
                    self.hits, self.region, mode, "intersects",
                    # A bounds-built region already carries its geometry, so a
                    # sentinel reader is enough to select the threaded path.
                    reader=object() if mode == "exact" else None,
                )
            )
            self.assertEqual([id(h) for h in got], [id(h) for h in sync], mode)

    def test_none_region_passes_hits_through(self):
        got = asyncio.run(
            spatial.apply_containment_async(self.hits, None, "exact", "intersects")
        )
        self.assertEqual(got, self.hits)

    def test_exact_runs_off_the_event_loop(self):
        import threading

        seen: list[int] = []
        real = spatial.hit_matches

        def _spy(*a, **kw):
            seen.append(threading.get_ident())
            return real(*a, **kw)

        spatial.hit_matches = _spy
        try:
            asyncio.run(
                spatial.apply_containment_async(
                    self.hits, self.region, "exact", "intersects", reader=object(),
                )
            )
        finally:
            spatial.hit_matches = real
        self.assertTrue(seen, "hit_matches was never called")
        self.assertNotIn(
            threading.get_ident(), seen,
            "exact containment ran on the calling (event-loop) thread",
        )


@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestLoadGeometryConcurrency(unittest.TestCase):
    """A cached region is shared; load_geometry must be done once, atomically.

    Regression guard for the window this opened: with the load running off the
    event loop, a second thread could see ``_geom_loaded=True`` before
    ``prepared`` was assigned and silently fall back to fuzzy.
    """

    def test_concurrent_load_geometry_all_observe_the_result(self):
        import threading

        square = _square(0, 0, 2, 2)

        class _SlowReader:
            """Widens the load window so an unsynchronised race would show."""
            calls = 0

            def get(self, key):
                type(self).calls += 1
                import time
                time.sleep(0.02)
                return square

        region = spatial.ResolvedRegion(
            cover_by_res={}, resolutions=(), bbox_geojson=square, h3_terms=[],
            geom_keys=("un:x_0",),
        )
        reader = _SlowReader()
        results: list[bool] = []
        lock = threading.Lock()

        def _worker():
            ok = region.load_geometry(reader)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 8)
        self.assertTrue(all(results), "a thread saw the region as unloaded")
        self.assertIsNotNone(region.prepared)
        # Loaded exactly once despite 8 concurrent callers.
        self.assertEqual(_SlowReader.calls, 1)

    def test_no_usable_geometry_is_sticky_and_reports_false(self):
        region = spatial.ResolvedRegion(
            cover_by_res={}, resolutions=(), bbox_geojson={}, h3_terms=[],
            geom_keys=("un:missing_0",),
        )

        class _EmptyReader:
            calls = 0

            def get(self, key):
                type(self).calls += 1
                return None

        reader = _EmptyReader()
        self.assertFalse(region.load_geometry(reader))
        self.assertFalse(region.load_geometry(reader))
        # Second call must not re-read the store.
        self.assertEqual(_EmptyReader.calls, 1)


class _StubResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StubClient:
    """Minimal async stand-in for httpx.AsyncClient used by resolve_region."""
    def __init__(self, hits):
        self._hits = hits
        self.calls = 0

    async def post(self, url, json=None, auth=None, headers=None):
        self.calls += 1
        return _StubResp({"hits": {"hits": self._hits}})


@unittest.skipUnless(_DEPS, "h3 + shapely required")
@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestPreparedGeometryConcurrency(unittest.TestCase):
    """Predicates must not share one PreparedGeometry across threads.

    GEOS builds a prepared geometry's spatial index lazily on first use and mutates
    it as it goes, so concurrent predicate calls on ONE instance corrupt it. It does
    not raise — it segfaults the process. On 2026-08-18 that killed gateway workers
    four times in three minutes under a 25-query reconcile batch, and every request
    in flight on a dying worker returned empty, so rows silently recorded "no match".

    A segfault would take this test process down with it, which is the point: the
    failure is loud here and silent in production.
    """

    def test_threads_get_their_own_prepared_geometry(self):
        import threading

        square = _square(0, 0, 10, 10)
        region = spatial.ResolvedRegion(
            cover_by_res={}, resolutions=(), bbox_geojson=square, h3_terms=[],
            geom_keys=("un:x_0",),
        )

        class _Reader:
            def get(self, key):
                return square

        self.assertTrue(region.load_geometry(_Reader()))

        seen: list = []
        errors: list = []
        lock = threading.Lock()

        def _worker(i):
            try:
                p = region.prepared_local()
                # Hammer the predicates: this is what corrupted the shared index.
                inside = all(p.intersects(spatial._Point(5, 5)) for _ in range(200))
                outside = any(p.intersects(spatial._Point(50, 50)) for _ in range(200))
                with lock:
                    # Keep a REFERENCE to the prepared object, not just its id:
                    # once a thread ends its thread-local is released, and CPython
                    # happily reuses the id for the next thread's object — which
                    # makes an id-only check report sharing that never happened.
                    seen.append((p, inside, outside))
            except Exception as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"predicate calls raised: {errors}")
        self.assertEqual(len(seen), 16)
        # Every thread must have got a DISTINCT prepared geometry …
        self.assertEqual(len({id(p) for p, _, _ in seen}), 16,
                         "threads shared a PreparedGeometry — the crash is back")
        # … and every one must still give the right answers.
        self.assertTrue(all(inside for _, inside, _ in seen))
        self.assertFalse(any(outside for _, _, outside in seen))

    def test_same_thread_reuses_its_prepared_geometry(self):
        square = _square(0, 0, 4, 4)
        region = spatial.ResolvedRegion(
            cover_by_res={}, resolutions=(), bbox_geojson=square, h3_terms=[],
            geom_keys=("un:x_0",),
        )

        class _Reader:
            def get(self, key):
                return square

        region.load_geometry(_Reader())
        self.assertIs(region.prepared_local(), region.prepared_local(),
                      "a thread should prepare once, not per call")


class TestResolveRegion(unittest.TestCase):
    def test_builds_from_source_h3_cover_and_caches(self):
        # Real postbarrier _source carries h3_cover/bounds/geometry_index — NOT
        # the full geom. resolve_region builds the region cover from h3_cover.
        # Only area geometries (has_geom=True) define a usable container.
        cover = _cover_for(_square(0, 0, 3, 3))
        bounds = [0, 0, 3, 3]
        hits = [{"_source": {
            "place_id": "un:XX",
            "geometries": [{"h3_cover": cover, "bounds": bounds,
                            "geometry_index": 0, "has_geom": True}],
        }}]
        client = _StubClient(hits)
        region = asyncio.run(spatial.resolve_region(["un:XX"], client, None))
        self.assertIsNotNone(region)
        self.assertTrue(region.has_cover)
        self.assertEqual(region.geom_keys, ("un:XX_0",))
        self.assertEqual(client.calls, 1)
        # A point inside the region passes the fuzzy test.
        h = _hit(rp=[1.5, 1.5])
        self.assertTrue(spatial.hit_matches(h["_source"], region, "fuzzy", "intersects"))
        # Second call for the same id is a cache hit — no further ES fetch.
        region2 = asyncio.run(spatial.resolve_region(["un:XX"], client, None))
        self.assertIs(region, region2)
        self.assertEqual(client.calls, 1)

    def test_point_only_container_without_a_co_referent_returns_none(self):
        # place#144: a point-only container (has_geom=False) has an h3_cover of a
        # single centroid cell — useless as a region. No geometry is invented, so
        # resolve_region returns None and the ENDPOINT must fail closed rather
        # than run the query unconstrained.
        hits = [{"_source": {
            "place_id": "wd:Q60576135",
            "geometries": [{"h3_cover": [h3.latlng_to_cell(51.0, -1.3, 7)],
                            "h3_centroid": h3.latlng_to_cell(51.0, -1.3, 7),
                            "repr_point": {"lon": -1.3, "lat": 51.0},
                            "bounds": [-1.3, 51.0, -1.3, 51.0],
                            "geometry_index": 0, "has_geom": False}],
        }}]
        region = asyncio.run(spatial.resolve_region(
            ["wd:Q60576135"], _StubClient(hits), None, link_fallback=False))
        self.assertIsNone(region)

    def test_point_only_container_borrows_a_co_referent_polygon(self):
        # The real shape of the gap: gn:3017382 (France) is point-only, its
        # sameAs twin wd:Q142 carries the boundary. Following the identity edge
        # beats buffering the point.
        cover = _cover_for(_square(0, 0, 3, 3))
        by_id = {
            "gn:3017382": {"_source": {"place_id": "gn:3017382", "geometries": [
                {"repr_point": {"lon": 2.0, "lat": 47.0},
                 "geometry_index": 0, "has_geom": False}]}},
            "wd:Q142": {"_source": {"place_id": "wd:Q142", "geometries": [
                {"h3_cover": cover, "bounds": [0, 0, 3, 3],
                 "geometry_index": 0, "has_geom": True}]}},
        }

        class _ByIdClient:
            def __init__(self):
                self.calls = 0

            async def post(self, url, json=None, auth=None, headers=None):
                self.calls += 1
                ids = json["query"]["terms"]["place_id"]
                return _StubResp({"hits": {"hits": [by_id[i] for i in ids if i in by_id]}})

        class _Edge:
            def __init__(self, a, b, rel):
                self.a, self.b, self.relation_type = a, b, rel

        edges = [_Edge("gn:3017382", "wd:Q142", "sameAs"),
                 _Edge("gn:3017382", "whg:1:2", "closeMatch")]  # weaker — must be ignored
        real_expand = spatial._linked_container_ids

        async def _fake_linked(ids):
            return [e.b for e in edges
                    if e.a in ids and e.relation_type in spatial._GEOM_LENDING_RELATIONS]

        spatial._linked_container_ids = _fake_linked
        try:
            region = asyncio.run(
                spatial.resolve_region(["gn:3017382"], _ByIdClient(), None))
        finally:
            spatial._linked_container_ids = real_expand

        self.assertIsNotNone(region)
        self.assertEqual(region.source, "linked-polygon")
        self.assertEqual(region.linked_ids, ("wd:Q142",))
        self.assertEqual(region.point_ids, ("gn:3017382",))
        # The borrowed boundary now scopes candidates for real.
        self.assertTrue(spatial.hit_matches(
            _hit(rp=[1.5, 1.5])["_source"], region, "fuzzy", "within"))
        self.assertFalse(spatial.hit_matches(
            _hit(rp=[40, 40])["_source"], region, "fuzzy", "within"))

    def test_no_geometry_at_all_returns_none(self):
        # Nothing to seed a buffer from → still None, and the endpoint must then
        # refuse to answer with an unscoped result set.
        hits = [{"_source": {"place_id": "x:1", "geometries": [{}]}}]
        client = _StubClient(hits)
        self.assertIsNone(asyncio.run(spatial.resolve_region(["x:1"], client, None)))

    def test_polygon_plus_point_only_uses_only_polygon(self):
        # One polygon container + one point-only container → region built from
        # the polygon alone (same as passing the polygon id by itself).
        cover = _cover_for(_square(0, 0, 3, 3))
        hits = [
            {"_source": {"place_id": "ukhc:CMB", "geometries": [
                {"h3_cover": cover, "bounds": [0, 0, 3, 3],
                 "geometry_index": 0, "has_geom": True}]}},
            {"_source": {"place_id": "wd:Q60576135", "geometries": [
                {"h3_cover": [h3.latlng_to_cell(51.0, -1.3, 7)],
                 "bounds": [-1.3, 51.0, -1.3, 51.0],
                 "geometry_index": 0, "has_geom": False}]}},
        ]
        client = _StubClient(hits)
        region = asyncio.run(
            spatial.resolve_region(["ukhc:CMB", "wd:Q60576135"], client, None))
        self.assertIsNotNone(region)
        self.assertEqual(region.geom_keys, ("ukhc:CMB_0",))
        self.assertEqual(region.area_ids, ("ukhc:CMB",))
        # ...but the ignored point-only container is reported, so the endpoint
        # can tell the client its scope was only partly honoured.
        self.assertEqual(region.point_ids, ("wd:Q60576135",))

    def test_no_coverage_returns_none(self):
        # Resolved place with no geometries → no usable container → unconstrained.
        hits = [{"_source": {"place_id": "x:1", "geometries": []}}]
        client = _StubClient(hits)
        self.assertIsNone(asyncio.run(spatial.resolve_region(["x:1"], client, None)))

    def test_unknown_ids_return_none(self):
        # Nonexistent id resolves to nothing → unconstrained (no error).
        client = _StubClient([])
        self.assertIsNone(asyncio.run(spatial.resolve_region(["nope:1"], client, None)))

    def test_place_prefix_is_stripped(self):
        # A client passing the canonical candidate id form (place:ukhc:CMB) must
        # resolve to the bare namespaced id rather than hitting the no-resolve path.
        cover = _cover_for(_square(0, 0, 3, 3))
        captured = {}

        class _CapturingClient(_StubClient):
            async def post(self, url, json=None, auth=None, headers=None):
                captured["ids"] = json["query"]["terms"]["place_id"]
                return await super().post(url, json=json, auth=auth, headers=headers)

        hits = [{"_source": {"place_id": "ukhc:CMB", "geometries": [
            {"h3_cover": cover, "bounds": [0, 0, 3, 3],
             "geometry_index": 0, "has_geom": True}]}}]
        client = _CapturingClient(hits)
        region = asyncio.run(spatial.resolve_region(["place:ukhc:CMB"], client, None))
        self.assertIsNotNone(region)
        self.assertEqual(captured["ids"], ["ukhc:CMB"])




@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestIsArealAndContainerShape(unittest.TestCase):
    def test_is_areal_predicate(self):
        # geom_class is authoritative when present
        self.assertTrue(spatial.is_areal({"geom_class": "area", "has_geom": True}))
        self.assertFalse(spatial.is_areal({"geom_class": "line", "has_geom": True}))
        self.assertFalse(spatial.is_areal({"geom_class": "point"}))
        # transitional fallback for legacy docs w/o geom_class: has_geom => areal
        self.assertTrue(spatial.is_areal({"has_geom": True}))
        self.assertFalse(spatial.is_areal({"has_geom": False}))
        self.assertFalse(spatial.is_areal({}))

    def test_line_container_is_not_a_region(self):
        # A restored LineString (has_geom=true, geom_class=line) carries an
        # h3_cover but must NOT define a containment region (place#145).
        cover = _cover_for(_square(0, 0, 3, 3))
        hits = [{"_source": {"place_id": "osm:w1", "geometries": [
            {"h3_cover": cover, "bounds": [0, 0, 3, 3], "geometry_index": 0,
             "has_geom": True, "geom_class": "line",
             "repr_point": {"lon": 1.5, "lat": 1.5}}]}}]
        region = asyncio.run(spatial.resolve_region(
            ["osm:w1"], _StubClient(hits), None, link_fallback=False))
        # no areal container -> no region (the line is recorded as point-only)
        self.assertIsNone(region)

    def test_polygon_container_with_geom_class_area_builds_region(self):
        cover = _cover_for(_square(0, 0, 3, 3))
        hits = [{"_source": {"place_id": "osm:w2", "geometries": [
            {"h3_cover": cover, "bounds": [0, 0, 3, 3], "geometry_index": 0,
             "has_geom": True, "geom_class": "area"}]}}]
        region = asyncio.run(spatial.resolve_region(["osm:w2"], _StubClient(hits), None))
        self.assertIsNotNone(region)
        self.assertEqual(region.area_ids, ("osm:w2",))

    def test_legacy_polygon_without_geom_class_still_builds_region(self):
        # transitional: a pre-backfill relation (has_geom, no geom_class) is
        # treated as areal so containment keeps working before the corpus
        # backfill lands.
        cover = _cover_for(_square(0, 0, 3, 3))
        hits = [{"_source": {"place_id": "osm:r9", "geometries": [
            {"h3_cover": cover, "bounds": [0, 0, 3, 3], "geometry_index": 0,
             "has_geom": True}]}}]
        region = asyncio.run(spatial.resolve_region(["osm:r9"], _StubClient(hits), None))
        self.assertIsNotNone(region)
        self.assertEqual(region.area_ids, ("osm:r9",))


class TestRegionFromCircle(unittest.TestCase):
    """A radial filter resolves to an H3 disc — at the FINEST resolution that
    fits the cell budget, not the coarsest.

    The resolution search walks ``sorted(_H3_EDGE_KM)``, which is ascending by
    resolution *number* = descending by cell size = coarse → fine, keeping the
    last one that fits. Reversing that order broke it both ways: it bailed on the
    first (finest) resolution for anything over ~11 km, so `region_from_circle`
    returned None and every such query failed closed with zero hits; and below
    that it ran to the coarsest resolution, answering a 1 km request with a
    res-4 two-ring disc ~68 km across.
    """

    def setUp(self):
        if not spatial._H3_AVAILABLE:
            self.skipTest("h3 not installed")

    def _disc(self, radius_km):
        return spatial.region_from_circle(51.09, -1.80, radius_km)

    def test_large_radius_still_resolves(self):
        for km in (25, 100, 400):
            with self.subTest(km=km):
                region = self._disc(km)
                self.assertIsNotNone(region, f"{km} km must resolve, not fail closed")
                self.assertEqual(region.source, "h3-disc")
                self.assertTrue(region.h3_terms)

    def test_resolution_is_the_finest_that_fits_the_budget(self):
        # Finer radius ⇒ finer (higher-numbered) resolution, never coarser.
        resolutions = [self._disc(km).resolutions[0] for km in (400, 100, 25, 5, 1)]
        self.assertEqual(resolutions, sorted(resolutions),
                         f"resolution must not coarsen as the radius shrinks: {resolutions}")
        self.assertEqual(self._disc(1).resolutions[0], max(spatial._H3_EDGE_KM))

    def test_disc_stays_within_the_cell_budget(self):
        for km in (1, 5, 11, 25, 100, 400):
            with self.subTest(km=km):
                region = self._disc(km)
                cells = sum(len(c) for c in region.cover_by_res.values())
                self.assertLessEqual(cells, spatial._DISC_MAX_CELLS)
                self.assertGreater(cells, 0)

    def test_bbox_brackets_the_centre(self):
        region = self._disc(25)
        ring = region.bbox_geojson["coordinates"][0]
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        self.assertLess(min(xs), -1.80)
        self.assertGreater(max(xs), -1.80)
        self.assertLess(min(ys), 51.09)
        self.assertGreater(max(ys), 51.09)

    def test_degenerate_input_returns_none(self):
        self.assertIsNone(spatial.region_from_circle(51.09, -1.80, 0))
        self.assertIsNone(spatial.region_from_circle(51.09, -1.80, -5))
        self.assertIsNone(spatial.region_from_circle(None, -1.80, 25))


class TestCoverOverlapsRegion(unittest.TestCase):
    """The fuzzy containment test walks parents only — never children.

    Expanding a candidate's cover to the region's resolution multiplies by 7 per
    step and materialises every child as a Python string. Against a county-sized
    container that pinned both gateway workers at 100% CPU until the watchdog
    killed them (prod, 2026-08-19). Overlap is an ancestor test, so the finer
    side can always be walked UP instead — same answer, no expansion.
    """

    def setUp(self):
        if not spatial._H3_AVAILABLE:
            self.skipTest("h3 not installed")
        import h3
        self.h3 = h3
        # A mixed-resolution region, as real containers are (ukhc:WTS is 5/6/7).
        self.inside = h3.latlng_to_cell(51.09, -1.80, 7)
        self.outside = h3.latlng_to_cell(48.85, 2.35, 7)
        self.region = spatial.ResolvedRegion(
            cover_by_res={
                5: {h3.cell_to_parent(self.inside, 5)},
                6: {h3.cell_to_parent(self.inside, 6)},
                7: {self.inside},
            },
            resolutions=(5, 6, 7), bbox_geojson={}, h3_terms=[],
        )

    def _overlaps(self, cover):
        self.region._ancestor_cache.clear()
        return spatial._cover_overlaps_region(cover, self.region)

    def test_same_resolution(self):
        self.assertTrue(self._overlaps([self.inside]))
        self.assertFalse(self._overlaps([self.outside]))

    def test_candidate_finer_than_the_region(self):
        fine_in = list(self.h3.cell_to_children(self.inside, 9))[:3]
        fine_out = list(self.h3.cell_to_children(self.outside, 9))[:3]
        self.assertTrue(self._overlaps(fine_in))
        self.assertFalse(self._overlaps(fine_out))

    def test_candidate_coarser_than_the_region(self):
        # The direction that used to expand to children.
        self.assertTrue(self._overlaps([self.h3.cell_to_parent(self.inside, 4)]))
        self.assertFalse(self._overlaps([self.h3.cell_to_parent(self.outside, 4)]))

    def test_empty_and_malformed_covers_are_safe(self):
        self.assertFalse(self._overlaps([]))
        self.assertFalse(self._overlaps(["not-a-cell", ""]))

    def test_no_child_expansion_happens(self):
        # The guarantee, pinned directly: a coarse candidate against a fine
        # region must not call cell_to_children even once.
        calls = []
        real = self.h3.cell_to_children

        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)

        self.h3.cell_to_children = spy
        try:
            self._overlaps([self.h3.cell_to_parent(self.inside, 3)])
        finally:
            self.h3.cell_to_children = real
        self.assertEqual(calls, [], "overlap test must never expand to children")

    def test_ancestor_lift_is_memoised_on_the_region(self):
        self.region._ancestor_cache.clear()
        coarse = self.h3.cell_to_parent(self.inside, 4)
        spatial._cover_overlaps_region([coarse], self.region)
        self.assertIn(4, self.region._ancestor_cache)
        self.assertEqual(self.region._ancestor_cache[4],
                         {self.h3.cell_to_parent(self.inside, 4)})


if __name__ == "__main__":
    unittest.main()
