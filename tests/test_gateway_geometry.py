"""``GET /api/geometry/{place_id}`` (gateway/geometry.py) — plan §5.3.

Run package-qualified, never with ``discover -s tests`` (tests/_sandbox.py):

    .venv/bin/python -m unittest tests.test_gateway_geometry

The real handler is driven against a REAL ``GeomStoreReader`` over a temp
store of genuine WKB polygons (``index.sqlite`` written by the production
builder) and a faked Elasticsearch, so the path that matters — key
construction, store read, union, size bound, response model — is the one
exercised. Each refusal (404 / 451 / 503) is paired with a 200 on the same
fixture, so a handler that refused everything fails here.
"""

from __future__ import annotations

import asyncio
import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

try:
    from fastapi import HTTPException
    from shapely import wkb as shapely_wkb
    from shapely.geometry import Polygon, shape
    from gateway import geometry, spatial
    from gateway.geometry import GeometryResponse, bound_geometry, place_geometry
except ModuleNotFoundError as exc:  # pragma: no cover - dev machines without the API deps
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")

from processing.geom_store import (
    INDEX_JSON_NAME, GeomStoreReader, shard_filename, write_sqlite_index,
)


def run(coro):
    return asyncio.run(coro)


def _square(x0, y0, x1, y1):
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])


def _circle(cx, cy, r, n):
    return Polygon([(cx + r * math.cos(2 * math.pi * i / n),
                     cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)])


# What the store holds. kain_par is a withheld authority; its polygon is IN the
# store so the 451 is shown to be a refusal, not a miss.
STORE = {
    "osm:r1_0": _square(0, 0, 1, 1),
    "osm:r1_1": _square(2, 2, 3, 3),
    "osm:r2_0": _circle(10, 10, 1.0, 5000),
    "ohm:r9_0": _square(5, 5, 6, 6),
    "kain_par:7_0": _square(-1, 50, 0, 51),
}

# What the index says. osm:r3 is a point-only place (has_geom False).
ES = {
    "osm:r1": {"place_id": "osm:r1", "namespace": "osm", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_class": "area"},
        {"geometry_index": 1, "has_geom": True, "geom_class": "area"}]},
    "osm:r2": {"place_id": "osm:r2", "namespace": "osm", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_class": "area"}]},
    "osm:r3": {"place_id": "osm:r3", "namespace": "osm", "geometries": [
        {"geometry_index": 0, "has_geom": False, "geom_class": "point"}]},
    "ohm:r9": {"place_id": "ohm:r9", "namespace": "ohm", "geometries": [
        {"geometry_index": 0, "has_geom": True}]},
    "kain_par:7": {"place_id": "kain_par:7", "namespace": "kain_par", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_class": "area"}]},
}


def _build_store(store_dir: Path):
    index = {}
    name = shard_filename(1)
    offset = 0
    with open(store_dir / name, "wb") as fh:
        for key, geom in STORE.items():
            blob = shapely_wkb.dumps(geom)
            fh.write(blob)
            index[key] = {"file": name, "offset": offset, "length": len(blob)}
            offset += len(blob)
    with open(store_dir / INDEX_JSON_NAME, "w") as f:
        json.dump(index, f)
    write_sqlite_index(((k, e["file"], e["offset"], e["length"]) for k, e in index.items()), store_dir)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeES:
    """Stand-in for httpx.AsyncClient answering the places term query."""

    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kw):
        self.calls.append(json)
        pid = json["query"]["term"]["place_id"]
        src = ES.get(pid)
        hits = [{"_source": src}] if src else []
        return _Resp({"hits": {"hits": hits, "total": {"value": len(hits)}}})


class GeometryEndpointBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.store = Path(cls._tmp.name)
        _build_store(cls.store)
        cls.reader = GeomStoreReader(cls.store)
        assert cls.reader.backend == "sqlite"

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()
        cls._tmp.cleanup()

    def setUp(self):
        # Establish, don't inherit: the handler sees THIS store, whatever an
        # earlier test (or the sandboxed real settings) left in spatial.
        self._saved = (spatial._reader, spatial._reader_init)
        spatial._reader, spatial._reader_init = self.reader, True
        self.es = _FakeES()
        self._patches = [
            mock.patch("gateway.geometry.httpx.AsyncClient", self.es),
            mock.patch("gateway.geometry.es_auth", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        spatial._reader, spatial._reader_init = self._saved

    def get(self, pid, **params):
        return run(place_geometry(pid, tolerance=params.get("tolerance"),
                                  max_bytes=params.get("max_bytes", geometry.GEOMETRY_MAX_BYTES_DEFAULT)))

    def refused(self, pid, **params) -> HTTPException:
        with self.assertRaises(HTTPException) as cm:
            self.get(pid, **params)
        return cm.exception


class TestServesTheStoredGeometry(GeometryEndpointBase):
    def test_union_of_every_stored_geometry_byte_for_byte(self):
        resp = self.get("osm:r1")
        self.assertEqual(resp.geometry_count, 2)
        self.assertEqual(resp.geometry["type"], "MultiPolygon")
        got = shape(resp.geometry)
        want = STORE["osm:r1_0"].union(STORE["osm:r1_1"])
        self.assertTrue(got.equals(want), "served geometry differs from the store")
        self.assertFalse(resp.simplified)
        self.assertIsNone(resp.tolerance)
        self.assertEqual(resp.vertex_count, 10)
        self.assertEqual(resp.bounds, [0.0, 0.0, 3.0, 3.0])
        self.assertEqual(resp.namespace, "osm")
        self.assertEqual(self.es.calls[0]["query"]["term"]["place_id"], "osm:r1")

    def test_place_prefix_is_stripped(self):
        self.assertEqual(self.get("place:ohm:r9").place_id, "ohm:r9")

    def test_fields_survive_the_response_model(self):
        # An undeclared field is silently dropped on the way out
        # (reference_gateway_response_models): validate the dump round-trips.
        resp = self.get("ohm:r9")
        dumped = GeometryResponse.model_validate(resp.model_dump()).model_dump()
        for field in ("geometry", "geometry_count", "bounds", "vertex_count", "bytes",
                      "max_bytes", "simplified", "tolerance", "source"):
            self.assertIn(field, dumped)
        self.assertEqual(dumped["geometry"]["type"], "Polygon")
        self.assertEqual(dumped["source"], "geom-store")

    def test_coordinates_are_rounded_to_six_decimals(self):
        resp = self.get("osm:r2", max_bytes=geometry.GEOMETRY_MAX_BYTES_CEILING)
        ring = resp.geometry["coordinates"][0]
        self.assertGreater(len(ring), 4000, "fixture circle should be dense")
        for x, y in ring:
            self.assertEqual(x, round(x, 6))
            self.assertEqual(y, round(y, 6))


class TestRefusalsArePairedWithSuccess(GeometryEndpointBase):
    def test_unknown_place_is_404_not_found(self):
        exc = self.refused("osm:r999")
        self.assertEqual(exc.status_code, 404)
        self.assertEqual(exc.detail["error"], "not found")
        self.assertEqual(len(self.es.calls), 1, "the index was never asked")
        self.assertEqual(self.get("osm:r1").place_id, "osm:r1")

    def test_point_only_place_is_404_no_geometry(self):
        exc = self.refused("osm:r3")
        self.assertEqual(exc.status_code, 404)
        self.assertEqual(exc.detail["error"], "no geometry")
        self.assertEqual(self.get("osm:r1").geometry_count, 2)

    def test_unnamespaced_id_is_422(self):
        self.assertEqual(self.refused("r1").status_code, 422)
        self.assertEqual(self.es.calls, [], "a malformed id must not reach the index")

    def test_withheld_authority_is_451_before_the_index_is_asked(self):
        withheld = geometry.non_redistributable_namespaces()
        # The determination is read from processing.settings, not hard-coded
        # here: assert what it must contain (audited 2026-07-22) and must not.
        self.assertTrue({"kain_par", "nl", "chgis"} <= withheld, withheld)
        self.assertFalse({"osm", "ohm", "wd", "gn", "clio", "po"} & withheld, withheld)

        exc = self.refused("kain_par:7")
        self.assertEqual(exc.status_code, 451)
        self.assertEqual(exc.detail["namespace"], "kain_par")
        self.assertIn("Kain", exc.detail["source"]["rights_holder"])
        self.assertEqual(self.es.calls, [], "a withheld source must not reach the index")
        self.assertNotIn("coordinates", json.dumps(exc.detail))
        # Same fixture, permitted authority: served.
        self.assertEqual(self.get("ohm:r9").geometry["type"], "Polygon")

    def test_store_unavailable_is_503_and_recovers(self):
        spatial._reader = None
        exc = self.refused("osm:r1")
        self.assertEqual(exc.status_code, 503)
        self.assertEqual(exc.detail["error"], "geometry store unavailable")
        spatial._reader = self.reader
        self.assertEqual(self.get("osm:r1").geometry_count, 2)

    def test_index_error_is_502(self):
        async def boom(*a, **kw):
            import httpx
            raise httpx.ConnectError("refused")
        with mock.patch.object(self.es, "post", boom):
            exc = self.refused("osm:r1")
        self.assertEqual(exc.status_code, 502)


class TestSizeBound(GeometryEndpointBase):
    def test_under_the_cap_is_served_whole(self):
        resp = self.get("osm:r2", max_bytes=geometry.GEOMETRY_MAX_BYTES_CEILING)
        self.assertFalse(resp.simplified)
        self.assertEqual(resp.vertex_count, 5001)
        self.assertLessEqual(resp.bytes, geometry.GEOMETRY_MAX_BYTES_CEILING)

    def test_over_the_cap_is_simplified_under_it(self):
        whole = self.get("osm:r2", max_bytes=geometry.GEOMETRY_MAX_BYTES_CEILING)
        cap = 20_000
        self.assertGreater(whole.bytes, cap, "fixture must exceed the cap for this to test anything")
        resp = self.get("osm:r2", max_bytes=cap)
        self.assertTrue(resp.simplified)
        self.assertIsNotNone(resp.tolerance)
        self.assertLessEqual(resp.bytes, cap)
        self.assertLess(resp.vertex_count, whole.vertex_count)
        self.assertGreater(resp.vertex_count, 3)
        self.assertEqual(resp.max_bytes, cap)
        # Still the same shape, roughly: a circle of radius 1 around (10, 10).
        got = shape(resp.geometry)
        self.assertAlmostEqual(got.area, math.pi, delta=0.05)
        # Bounds describe the FULL geometry, not the simplified one.
        self.assertEqual(resp.bounds, whole.bounds)

    def test_explicit_tolerance_is_applied_and_reported(self):
        resp = self.get("osm:r2", tolerance=0.05, max_bytes=geometry.GEOMETRY_MAX_BYTES_CEILING)
        self.assertTrue(resp.simplified)
        self.assertEqual(resp.tolerance, 0.05)
        self.assertLess(resp.vertex_count, 200)

    def test_bound_raises_413_when_nothing_fits(self):
        # Nothing can serialise a polygon in 50 bytes; the bound must say so
        # rather than loop or return something over the cap.
        with self.assertRaises(HTTPException) as cm:
            bound_geometry(STORE["osm:r2_0"], None, 50)
        self.assertEqual(cm.exception.status_code, 413)
        self.assertEqual(len(cm.exception.detail["bounds"]), 4)

    def test_bound_leaves_a_small_geometry_alone(self):
        gj, simplified, tol, size = bound_geometry(STORE["osm:r1_0"], None, 10_000)
        self.assertFalse(simplified)
        self.assertIsNone(tol)
        self.assertEqual(gj["type"], "Polygon")
        self.assertEqual(size, len(json.dumps(gj, separators=(",", ":"))))


class TestKeyConstruction(unittest.TestCase):
    def test_keys_follow_geometry_index_and_skip_inline_points(self):
        src = {"place_id": "wd:Q1", "geometries": [
            {"geometry_index": 3, "has_geom": True},
            {"geometry_index": 0, "has_geom": False},
            {"has_geom": True},            # no index: positional
            "not a dict",
        ]}
        self.assertEqual(geometry.geom_keys_for(src), ["wd:Q1_3", "wd:Q1_2"])

    def test_no_place_id_means_no_keys(self):
        self.assertEqual(geometry.geom_keys_for({"geometries": [{"has_geom": True}]}), [])


class TestRouteIsMounted(unittest.TestCase):
    def test_app_registers_the_route_before_the_catch_all(self):
        from gateway.app import app
        paths = [getattr(r, "path", None) for r in app.routes]
        self.assertIn("/api/geometry/{place_id}", paths)
        # Routers registered first match first; the proxy catch-all must come after.
        self.assertLess(paths.index("/api/geometry/{place_id}"), paths.index("/{path:path}"))


if __name__ == "__main__":
    unittest.main()
