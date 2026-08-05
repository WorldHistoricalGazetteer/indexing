"""The ``covers`` fast path must change speed, not answers.

The bug, 5 August 2026: moving `un` from BNDA to geoBoundaries HPSC raised the
average country outline from 232 to 73,663 vertices (Australia: 1,655,696).
``_filter_by_containment`` measures overlap with ``un_geom.intersection(
place_geom)``, and overlay cost scales with the COUNTRY's vertex count, not the
place's — so every areal place got ~300x dearer.

Measured on the live corpus-wide run (array 10745399):

    osm  points   ~206,000 docs/min
    osm  areal         4,223 docs/min   <-- 50x collapse, ~36 h projection
    clio                 198 docs/min   <-- continent-scale polities

The fix: a place that a country wholly covers has intersection == place, so its
measure is the place's own — no overlay needed. ``covers`` is answered from the
same prepared STRtree that ``intersects`` already built.

This is an exactness claim, not an approximation, so the test that matters is
equivalence: for a spread of geometries the fast path must return EXACTLY what
the unconditional-overlay implementation returned, ccodes and ordering alike.
"""

from __future__ import annotations

import unittest

from shapely.geometry import (
    GeometryCollection, LineString, MultiPolygon, Point, Polygon,
)


def _reference_filter(place_geom, candidate_ccodes, un_cache):
    """The pre-fast-path implementation: always overlay. Ground truth."""
    is_point = place_geom.geom_type in ("Point", "MultiPoint")
    is_linear = place_geom.geom_type in (
        "LineString", "MultiLineString", "LinearRing")
    matches: list[tuple[str, float]] = []

    for ccode in candidate_ccodes:
        for prepared, un_geom in un_cache.prepared_for(ccode):
            try:
                if not prepared.intersects(place_geom):
                    continue
                if is_point:
                    matches.append((ccode, 1.0))
                    break
                inter = un_geom.intersection(place_geom)
                if inter.is_empty:
                    continue
                measure = inter.length if is_linear else inter.area
                if measure <= 0:
                    continue
                matches.append((ccode, measure))
                break
            except Exception:
                continue

    if not matches:
        return []
    if is_point:
        return sorted({c for c, _ in matches})
    matches.sort(key=lambda t: t[1], reverse=True)
    return [c for c, _ in matches]


class CoversFastPathIsEquivalent(unittest.TestCase):

    def setUp(self):
        from processing.ccode_enrichment import _UnGeometryCache

        # Two adjacent "countries" sharing the x=10 border, plus a detached one.
        self.west = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        self.east = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
        self.far = Polygon([(50, 50), (60, 50), (60, 60), (50, 60)])

        class _Cache(_UnGeometryCache):
            def __init__(self, geoms):
                super().__init__({})
                self._geoms = geoms

            def geoms_for(self, ccode):
                return self._geoms.get(ccode, [])

        self.cache = _Cache({
            "WW": [self.west],
            "EE": [self.east],
            "FF": [self.far],
        })
        self.ccodes = ["WW", "EE", "FF"]

    def _both(self, geom):
        from processing.ccode_enrichment import _filter_by_containment
        fast = _filter_by_containment(geom, self.ccodes, self.cache)
        ref = _reference_filter(geom, self.ccodes, self.cache)
        return fast, ref

    def _assert_equivalent(self, geom, label):
        fast, ref = self._both(geom)
        self.assertEqual(fast, ref, f"{label}: fast={fast} reference={ref}")
        return fast

    # -- the fast path itself ------------------------------------------------

    def test_area_wholly_inside_matches_reference(self):
        """The case the fast path exists for: covered, so no overlay."""
        got = self._assert_equivalent(
            Polygon([(2, 2), (4, 2), (4, 4), (2, 4)]), "area inside")
        self.assertEqual(got, ["WW"])

    def test_line_wholly_inside_matches_reference(self):
        got = self._assert_equivalent(
            LineString([(2, 2), (8, 8)]), "line inside")
        self.assertEqual(got, ["WW"])

    def test_point_inside_matches_reference(self):
        self._assert_equivalent(Point(5, 5), "point inside")

    # -- the cases the fast path must NOT swallow ----------------------------

    def test_straddling_area_still_overlays_and_orders_by_overlap(self):
        """Genuine border-straddler: covered by neither, so both are measured.

        7 units of width in WW, 3 in EE, so WW must rank first. This is the
        ordering the fast path could silently destroy.
        """
        got = self._assert_equivalent(
            Polygon([(3, 2), (13, 2), (13, 4), (3, 4)]), "straddler")
        self.assertEqual(got, ["WW", "EE"])

    def test_straddling_area_leaning_east_orders_east_first(self):
        got = self._assert_equivalent(
            Polygon([(8, 2), (18, 2), (18, 4), (8, 4)]), "straddler east")
        self.assertEqual(got, ["EE", "WW"])

    def test_border_touch_is_still_rejected(self):
        """An area east of the border only TOUCHES WW — zero overlap area."""
        got = self._assert_equivalent(
            Polygon([(10, 2), (12, 2), (12, 4), (10, 4)]), "border touch")
        self.assertEqual(got, ["EE"])

    def test_area_outside_everything(self):
        got = self._assert_equivalent(
            Polygon([(30, 30), (32, 30), (32, 32), (30, 32)]), "outside")
        self.assertEqual(got, [])

    def test_multipolygon_split_across_two_countries(self):
        """Covered by neither individually — must report both."""
        geom = MultiPolygon([
            Polygon([(2, 2), (4, 2), (4, 4), (2, 4)]),
            Polygon([(12, 2), (14, 2), (14, 4), (12, 4)]),
        ])
        got = self._assert_equivalent(geom, "split multipolygon")
        self.assertEqual(sorted(got), ["EE", "WW"])

    def test_line_crossing_the_border(self):
        got = self._assert_equivalent(
            LineString([(5, 5), (15, 5)]), "crossing line")
        self.assertEqual(sorted(got), ["EE", "WW"])

    def test_geometry_collection_is_handled(self):
        """clio polities arrive as GeometryCollections (see py-spy locals)."""
        geom = GeometryCollection([
            Polygon([(2, 2), (4, 2), (4, 4), (2, 4)]),
            Point(6, 6),
        ])
        self._assert_equivalent(geom, "geometry collection")

    def test_zero_area_degenerate_polygon_is_rejected_as_before(self):
        """A collapsed polygon has no areal overlap under either path."""
        self._assert_equivalent(
            Polygon([(2, 2), (4, 2), (4, 2), (2, 2)]), "degenerate")


class CoversFastPathAvoidsTheOverlay(unittest.TestCase):
    """Equivalence alone would be satisfied by doing nothing — prove the
    overlay is actually skipped, or the fix is cosmetic."""

    def test_no_intersection_call_for_a_covered_place(self):
        from processing.ccode_enrichment import (
            _UnGeometryCache, _filter_by_containment,
        )

        from shapely.prepared import prep

        calls: list[str] = []

        class _CountingGeom:
            """Delegating proxy, not a Polygon subclass.

            Shapely 2 constructs geometries in C: ``Polygon(...)`` returns a
            plain Polygon even when subclassed, so an overridden method is
            never bound and would silently count nothing. ``_filter_by_
            containment`` only ever calls ``.intersection`` on the raw
            geometry, so a proxy over that one method is a faithful instrument.
            """

            def __init__(self, geom):
                self._geom = geom

            def intersection(self, other, **kw):
                calls.append("intersection")
                return self._geom.intersection(other, **kw)

        country = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])

        class _Cache(_UnGeometryCache):
            def __init__(self, geoms):
                super().__init__({})
                self._geoms = geoms

            def geoms_for(self, ccode):
                return self._geoms.get(ccode, [])

            def prepared_for(self, ccode):
                return [(prep(g), _CountingGeom(g))
                        for g in self.geoms_for(ccode)]

        cache = _Cache({"XX": [country]})

        inside = Polygon([(2, 2), (4, 2), (4, 4), (2, 4)])
        self.assertEqual(_filter_by_containment(inside, ["XX"], cache), ["XX"])
        self.assertEqual(calls, [], "covered place must not trigger an overlay")

        straddler = Polygon([(5, 2), (15, 2), (15, 4), (5, 4)])
        _filter_by_containment(straddler, ["XX"], cache)
        self.assertEqual(calls, ["intersection"],
                         "a straddler must still be measured by overlay")


if __name__ == "__main__":
    unittest.main()
