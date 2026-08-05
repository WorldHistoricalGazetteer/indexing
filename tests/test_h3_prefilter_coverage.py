"""An h3_cover must never under-cover its polygon.

``_polyfill_adaptive`` simplifies polygons above 5,000 vertices before polyfill,
because ``h3shape_to_cells`` scales with vertex count and a 73,663-vertex
country would otherwise be slow. Douglas–Peucker moves the boundary **inward**
by up to the tolerance — 0.10° (~11 km) at res 4, 0.25° (~28 km) at res 3 — so
coastal cells were being deleted.

The asymmetry is the whole point: h3_cover is a *prefilter*. A missing cell
loses its candidate permanently, because the precise Shapely refine never sees
the place. A spurious cell costs only that refine, which rejects it. So the
error must point outward.

Measured cost of the old behaviour: the UN prefilter held 77,279 res-4 cells
against roughly 84,000 needed for global land, and 外高村 (118.690158,
24.67126) sat *inside* China's polygon yet was never offered China as a
candidate. About 615 k osm places were lost this way — recorded as
``docs_no_candidate``.
"""

from __future__ import annotations

import unittest


def _wobbly_coast(n=6000, span=20.0):
    """A polygon with enough vertices to trip the simplify threshold, whose
    edge has sub-tolerance detail — i.e. exactly what gets eaten."""
    import math
    pts = []
    for i in range(n):
        t = i / n
        x = -span / 2 + span * t
        # ~0.02 deg wobble: below the res-4 tolerance of 0.10, so DP removes it
        y = 10.0 + 0.02 * math.sin(t * 120.0)
        pts.append((x, y))
    pts += [(span / 2, -10.0), (-span / 2, -10.0), pts[0]]
    return {"type": "Polygon", "coordinates": [[list(p) for p in pts]]}


class CoverDoesNotShrink(unittest.TestCase):

    def test_simplified_cover_contains_unsimplified_cover(self):
        """The guarantee: cover(simplified ⊕ tol) ⊇ cover(original)."""
        from processing import helpers

        geom = _wobbly_coast()
        self.assertGreater(helpers._count_vertices(geom),
                           helpers.H3_SIMPLIFY_VERTEX_THRESHOLD,
                           "fixture must actually trip the simplify path")

        with_simplify = helpers._polyfill_adaptive(geom)

        # Same geometry, simplification disabled, as ground truth.
        original = helpers.H3_SIMPLIFY_VERTEX_THRESHOLD
        try:
            helpers.H3_SIMPLIFY_VERTEX_THRESHOLD = 10 ** 9
            without_simplify = helpers._polyfill_adaptive(geom)
        finally:
            helpers.H3_SIMPLIFY_VERTEX_THRESHOLD = original

        if not without_simplify:
            self.skipTest("h3 unavailable or polyfill returned nothing")

        missing = without_simplify - with_simplify
        self.assertEqual(
            missing, set(),
            f"{len(missing)} cells present without simplification are absent "
            f"with it — the cover shrank, which loses candidates permanently")

    def test_dilation_is_applied_in_source(self):
        from pathlib import Path
        src = Path("processing/helpers.py").read_text()
        i = src.index("H3_SIMPLIFY_VERTEX_THRESHOLD:")
        window = src[i:i + 2000]
        self.assertIn("simplified.buffer(tol)", window,
                      "the simplified shape must be dilated by the same "
                      "tolerance, or the boundary moves inward")

    def test_tolerances_are_documented_in_distance_terms(self):
        """0.10 deg reads as harmless until written as 11 km."""
        from pathlib import Path
        src = Path("processing/helpers.py").read_text()
        self.assertIn("11 km", src)


class OverlapContainment(unittest.TestCase):
    """A cell the polygon intersects must be covered, even if its centre is not.

    ``h3shape_to_cells`` emits a cell only when the cell's CENTRE lies inside
    the polygon. At res 3 (~120 km hexagons) a coastal city routinely sits in a
    cell whose centre is offshore, so the cell was never emitted. Measured on
    Australia with simplification disabled: Sydney had no covering cell at any
    resolution while Cronulla, 20 km away, did. Rio, Buenos Aires, Honolulu and
    the Fujian coast behaved identically.

    This is not only a ccode prefilter concern — ``gateway/spatial.py`` runs
    fuzzy containment, the DEFAULT search mode, off ``h3_cover`` for both the
    region and each hit (place#174).
    """

    def test_cell_is_covered_when_its_centre_lies_outside_the_polygon(self):
        try:
            import h3
        except ImportError:
            self.skipTest("h3 unavailable")
        from processing import helpers

        # A point, and the res-5 cell containing it.
        lon, lat = 10.0, 50.0
        res = 5
        cell = h3.latlng_to_cell(lat, lon, res)
        c_lat, c_lon = h3.cell_to_latlng(cell)

        # A small polygon around the point that deliberately EXCLUDES the
        # cell's centre — exactly the coastal-city geometry.
        d = 0.004
        poly = {"type": "Polygon", "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d],
            [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]]}

        from shapely.geometry import Point, shape
        self.assertFalse(
            shape(poly).contains(Point(c_lon, c_lat)),
            "fixture invalid: the polygon must not contain the cell centre")

        cells = helpers._polyfill_adaptive(poly)
        covered = any(h3.latlng_to_cell(lat, lon, r) in cells
                      for r in range(0, 12))
        self.assertTrue(
            covered,
            "the polygon intersects this cell but the cover omits it — "
            "centre containment, the defect this guards against")

    def test_source_requests_overlap_containment(self):
        from pathlib import Path
        src = Path("processing/helpers.py").read_text()
        self.assertIn('contain="overlap"', src)
        self.assertIn("h3shape_to_cells_experimental", src)

    def test_falls_back_when_the_experimental_api_is_absent(self):
        """The API is flagged experimental; a narrow cover beats none."""
        from pathlib import Path
        src = Path("processing/helpers.py").read_text()
        i = src.index("def _cells_for_shape(")
        body = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("getattr(_h3", body)
        self.assertIn("h3shape_to_cells(h3_poly, res)", body)

if __name__ == "__main__":
    unittest.main()
