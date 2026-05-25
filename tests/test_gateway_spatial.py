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
class TestResolveRegion(unittest.TestCase):
    def test_builds_from_source_h3_cover_and_caches(self):
        # Real postbarrier _source carries h3_cover/bounds/geometry_index — NOT
        # the full geom. resolve_region builds the region cover from h3_cover.
        cover = _cover_for(_square(0, 0, 3, 3))
        bounds = [0, 0, 3, 3]
        hits = [{"_source": {
            "place_id": "un:XX",
            "geometries": [{"h3_cover": cover, "bounds": bounds, "geometry_index": 0}],
        }}]
        client = _StubClient(hits)
        region = asyncio.run(spatial.resolve_region(["un:XX"], client, None))
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

    def test_no_coverage_raises(self):
        hits = [{"_source": {"place_id": "x:1", "geometries": []}}]
        client = _StubClient(hits)
        with self.assertRaises(spatial.RegionError):
            asyncio.run(spatial.resolve_region(["x:1"], client, None))

    def test_unknown_ids_raise(self):
        client = _StubClient([])
        with self.assertRaises(spatial.RegionError):
            asyncio.run(spatial.resolve_region(["nope:1"], client, None))


if __name__ == "__main__":
    unittest.main()
