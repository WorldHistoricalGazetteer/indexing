"""``GET /api/geometry/{place_id}`` (gateway/geometry.py) — plan §5.3.

Run package-qualified, never with ``discover -s tests`` (tests/_sandbox.py):

    .venv/bin/python -m unittest tests.test_gateway_geometry

The real handler is driven against a REAL ``GeomStoreReader`` over a temp
store of genuine WKB polygons (``index.sqlite`` written by the production
builder) and a faked Elasticsearch, so the path that matters — key
construction, store read, union, size bound, response model — is the one
exercised. Each refusal (404 / 413 / 451 / 503) is paired with a 200 on the
same fixture, so a handler that refused everything fails here.

The cost class is in the suite on purpose: ``TestLargeNoisyGeometry`` builds
200k- and 1M-vertex NOISY rings at test time and asserts wall-clock and
resident memory, because the first version of the bound re-simplified the
original geometry topology-preservingly from a fine tolerance and took
221 s / 559 MB on the 1M ring (review of e5f2c15). Those two tests fail
against that loop; see the commit that introduced them.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import resource
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

try:
    from fastapi import HTTPException
    from shapely import wkb as shapely_wkb
    from shapely.geometry import Polygon, shape
    from gateway import geometry, spatial
    from gateway.geometry import Deadline, GeometryResponse, bound_geometry, place_geometry
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


def noisy_ring(n: int, span: float = 4.0, seed: int = 1) -> Polygon:
    """A ring with per-vertex radial noise, so Douglas–Peucker has real work to
    do at every tolerance (a smooth circle collapses at any tolerance)."""
    rnd = random.Random(seed)
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        r = (span / 2) * (1 + 0.02 * rnd.uniform(-1, 1) + 0.05 * math.sin(37 * t))
        pts.append((10 + r * math.cos(t), 50 + r * math.sin(t)))
    return Polygon(pts)


# What the store holds. kain_par is a withheld authority; its polygon is IN the
# store so the 451 is shown to be a refusal, not a miss. wd:Q5_0 is lent to
# og:5; nl:9_0 is lent to og:6 (and must not leak through the loan).
STORE = {
    "osm:r1_0": _square(0, 0, 1, 1),
    "osm:r1_1": _square(2, 2, 3, 3),
    "osm:r2_0": _circle(10, 10, 1.0, 5000),
    "ohm:r9_0": _square(5, 5, 6, 6),
    "kain_par:7_0": _square(-1, 50, 0, 51),
    "wd:Q5_0": _square(20, 20, 21, 21),
    "nl:9_0": _square(30, 30, 31, 31),
}

# What the index says. osm:r3 is point-only (has_geom False); osm:r4 promises
# two stored geometries of which the store holds one.
ES = {
    "osm:r1": {"place_id": "osm:r1", "namespace": "osm", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_class": "area", "geom_ref": "osm:r1_0"},
        {"geometry_index": 1, "has_geom": True, "geom_class": "area", "geom_ref": "osm:r1_1"}]},
    "osm:r2": {"place_id": "osm:r2", "namespace": "osm", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_class": "area"}]},
    "osm:r3": {"place_id": "osm:r3", "namespace": "osm", "geometries": [
        {"geometry_index": 0, "has_geom": False, "geom_class": "point"}]},
    "osm:r4": {"place_id": "osm:r4", "namespace": "osm", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_ref": "osm:r1_0"},
        {"geometry_index": 1, "has_geom": True, "geom_ref": "osm:r4_1"}]},
    "ohm:r9": {"place_id": "ohm:r9", "namespace": "ohm", "geometries": [
        {"geometry_index": 0, "has_geom": True}]},
    "kain_par:7": {"place_id": "kain_par:7", "namespace": "kain_par", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_class": "area"}]},
    "og:5": {"place_id": "og:5", "namespace": "og", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_class": "area",
         "geom_ref": "wd:Q5_0", "source": "wd"}]},
    "og:6": {"place_id": "og:6", "namespace": "og", "geometries": [
        {"geometry_index": 0, "has_geom": True, "geom_class": "area",
         "geom_ref": "nl:9_0", "source": "nl"}]},
}


def _build_store(store_dir: Path, extra: dict | None = None):
    index = {}
    name = shard_filename(1)
    offset = 0
    with open(store_dir / name, "wb") as fh:
        for key, geom in {**STORE, **(extra or {})}.items():
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

    def __init__(self, docs=None):
        self.calls = []
        self.docs = docs if docs is not None else ES
        self.delay = 0.0          # seconds to sleep before answering

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kw):
        self.calls.append(json)
        if self.delay:
            await asyncio.sleep(self.delay)
        pid = json["query"]["term"]["place_id"]
        src = self.docs.get(pid)
        hits = [{"_source": src}] if src else []
        return _Resp({"hits": {"hits": hits, "total": {"value": len(hits)}}})


class GeometryEndpointBase(unittest.TestCase):
    extra_store: dict = {}

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.store = Path(cls._tmp.name)
        _build_store(cls.store, cls.extra_store)
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
        self.assertEqual((resp.geometry_count, resp.geometry_expected), (2, 2))
        self.assertEqual(resp.geometry["type"], "MultiPolygon")
        got = shape(resp.geometry)
        want = STORE["osm:r1_0"].union(STORE["osm:r1_1"])
        self.assertTrue(got.equals(want), "served geometry differs from the store")
        self.assertFalse(resp.simplified)
        self.assertIsNone(resp.tolerance)
        self.assertEqual((resp.vertex_count, resp.raw_vertex_count), (10, 10))
        self.assertEqual(resp.bounds, [0.0, 0.0, 3.0, 3.0])
        self.assertEqual(resp.namespace, "osm")
        self.assertGreaterEqual(resp.elapsed_ms, 0)
        self.assertEqual(self.es.calls[0]["query"]["term"]["place_id"], "osm:r1")
        self.assertIn("geometries.geom_ref", self.es.calls[0]["_source"])

    def test_place_prefix_is_stripped(self):
        self.assertEqual(self.get("place:ohm:r9").place_id, "ohm:r9")

    def test_fields_survive_the_response_model(self):
        # An undeclared field is silently dropped on the way out
        # (reference_gateway_response_models): validate the dump round-trips.
        resp = self.get("ohm:r9")
        dumped = GeometryResponse.model_validate(resp.model_dump()).model_dump()
        for field in ("geometry", "geometry_count", "geometry_expected", "bounds", "vertex_count",
                      "raw_vertex_count", "bytes", "max_bytes", "simplified", "tolerance",
                      "elapsed_ms", "source"):
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

    def test_borrowed_geometry_is_read_by_its_geom_ref(self):
        # og:5 carries wd's polygon under wd's key; the positional key og:5_0
        # does not exist, so a handler reconstructing {pid}_{idx} says "no
        # geometry" here instead.
        resp = self.get("og:5")
        self.assertTrue(shape(resp.geometry).equals(STORE["wd:Q5_0"]))
        self.assertEqual(resp.geometry_count, 1)

    def test_store_read_is_uncached(self):
        with mock.patch.object(self.reader, "get_wkb", wraps=self.reader.get_wkb) as get_wkb:
            self.get("osm:r1")
        self.assertEqual(get_wkb.call_count, 2)
        for call in get_wkb.call_args_list:
            self.assertFalse(call.kwargs.get("cached", True), "the entry-count LRU must be bypassed")


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

    def test_store_short_of_the_index_is_404_incomplete_not_a_partial_union(self):
        exc = self.refused("osm:r4")
        self.assertEqual(exc.status_code, 404)
        self.assertEqual(exc.detail["error"], "geometry incomplete")
        self.assertEqual((exc.detail["expected"], exc.detail["found"]), (2, 1))
        self.assertNotIn("coordinates", json.dumps(exc.detail))
        self.assertEqual(self.get("osm:r1").geometry_count, 2)

    def test_unnamespaced_id_is_422(self):
        self.assertEqual(self.refused("r1").status_code, 422)
        self.assertEqual(self.es.calls, [], "a malformed id must not reach the index")

    def test_withheld_authority_is_451_before_the_index_is_asked(self):
        withheld = geometry.non_redistributable_namespaces()
        # The determination is read from processing.settings, not hard-coded
        # here: assert what it must contain (audited 2026-07-22) and must not.
        self.assertTrue({"kain_par", "nl", "chgis"} <= withheld, withheld)
        self.assertFalse({"osm", "ohm", "wd", "gn", "clio", "po", "og"} & withheld, withheld)

        exc = self.refused("kain_par:7")
        self.assertEqual(exc.status_code, 451)
        self.assertEqual(exc.detail["namespace"], "kain_par")
        self.assertIn("Kain", exc.detail["source"]["rights_holder"])
        self.assertEqual(self.es.calls, [], "a withheld source must not reach the index")
        self.assertNotIn("coordinates", json.dumps(exc.detail))
        # Same fixture, permitted authority: served.
        self.assertEqual(self.get("ohm:r9").geometry["type"], "Polygon")

    def test_withheld_lender_is_451_through_a_permitted_borrower(self):
        # og is redistributable; its geometry here is nl's. Provenance is per
        # geometry, so the loan must not launder the polygon.
        exc = self.refused("og:6")
        self.assertEqual(exc.status_code, 451)
        self.assertEqual(exc.detail["namespace"], "nl")
        self.assertEqual(exc.detail["id"], "og:6")
        self.assertIn("borrowed", exc.detail["detail"])
        self.assertNotIn("coordinates", json.dumps(exc.detail))
        # Same borrower namespace with a permitted lender: served.
        self.assertEqual(self.get("og:5").geometry["type"], "Polygon")

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


class TestCostBounds(GeometryEndpointBase):
    def test_raw_vertex_cap_refuses_before_parsing(self):
        with mock.patch.object(geometry, "GEOMETRY_MAX_RAW_VERTICES", 1000):
            exc = self.refused("osm:r2")           # 5001 vertices, 80 KB of WKB
            self.assertEqual(exc.status_code, 413)
            self.assertEqual(exc.detail["error"], "geometry too large")
            self.assertEqual(exc.detail["max_vertices"], 1000)
            self.assertEqual(self.get("osm:r1").geometry_count, 2)   # 10 vertices: fine
        self.assertEqual(self.get("osm:r2", max_bytes=geometry.GEOMETRY_MAX_BYTES_CEILING).raw_vertex_count, 5001)

    def test_deadline_aborts_between_stages(self):
        # A slow index read eats the whole budget: the request must stop at
        # the next stage boundary instead of going on to read the store.
        self.es.delay = 0.1
        with mock.patch.object(geometry, "GEOMETRY_TIME_BUDGET_S", 0.05), \
                mock.patch.object(self.reader, "get_wkb", wraps=self.reader.get_wkb) as get_wkb:
            exc = self.refused("osm:r1")
        self.assertEqual(exc.status_code, 413)
        self.assertEqual(exc.detail["error"], "geometry budget exceeded")
        self.assertEqual(exc.detail["stage"], "index read")
        get_wkb.assert_not_called()
        self.es.delay = 0.0
        self.assertEqual(self.get("osm:r1").geometry_count, 2)

    def test_busy_endpoint_is_503_with_retry_after(self):
        async def scenario():
            sem = geometry._semaphore()
            for _ in range(geometry.GEOMETRY_CONCURRENCY):
                await sem.acquire()                  # every slot held by "other" requests
            try:
                with mock.patch.object(geometry, "GEOMETRY_TIME_BUDGET_S", 0.05):
                    return await place_geometry("osm:r1", tolerance=None,
                                                max_bytes=geometry.GEOMETRY_MAX_BYTES_DEFAULT)
            finally:
                for _ in range(geometry.GEOMETRY_CONCURRENCY):
                    sem.release()
        t0 = time.perf_counter()
        resp = run(scenario())
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers["retry-after"], "5")
        self.assertIn("busy", json.loads(resp.body)["detail"]["error"])
        self.assertEqual(self.es.calls, [], "a refused request must not have cost an index read")
        self.assertEqual(self.get("osm:r1").geometry_count, 2)   # slots released: served


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
        self.assertTrue(got.is_valid)
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


def _maxrss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class TestLargeNoisyGeometry(unittest.TestCase):
    """The cost class, visible to the suite (module docstring).

    Budgets are generous multiples of the measured figures (200k: 0.6 s;
    1M: 1.8 s; no RSS growth beyond the fixture on either) so a slow CI box
    passes, while the previous loop (14 s / 221 s, +170 MB / +559 MB) fails
    both tests on time and the 1M one on memory as well.
    """

    def _bound(self, n, wall_s, rss_mb):
        geom = noisy_ring(n)
        before = _maxrss_mb()
        t0 = time.perf_counter()
        gj, simplified, tol, size = bound_geometry(geom, None, geometry.GEOMETRY_MAX_BYTES_DEFAULT,
                                                   Deadline(geometry.GEOMETRY_TIME_BUDGET_S))
        elapsed = time.perf_counter() - t0
        grown = _maxrss_mb() - before
        self.assertLess(elapsed, wall_s, f"{n} noisy vertices took {elapsed:.1f}s")
        self.assertLess(grown, rss_mb, f"{n} noisy vertices grew RSS by {grown:.0f} MB")
        self.assertTrue(simplified)
        self.assertLessEqual(size, geometry.GEOMETRY_MAX_BYTES_DEFAULT)
        # Not over-simplified either: the least-simplified fit sits near the cap.
        self.assertGreater(size, geometry.GEOMETRY_MAX_BYTES_DEFAULT // 4, f"over-simplified to {size} bytes")
        out = shape(gj)
        self.assertTrue(out.is_valid)
        self.assertAlmostEqual(out.area, geom.area, delta=0.02 * geom.area)

    def test_200k_vertex_noisy_ring(self):
        self._bound(200_000, wall_s=geometry.GEOMETRY_TIME_BUDGET_S, rss_mb=100)

    def test_1m_vertex_noisy_ring(self):
        self._bound(1_000_000, wall_s=geometry.GEOMETRY_TIME_BUDGET_S, rss_mb=250)


class TestKeyConstruction(unittest.TestCase):
    def test_keys_follow_geom_ref_then_geometry_index_and_skip_inline_points(self):
        src = {"place_id": "og:1", "geometries": [
            {"geometry_index": 3, "has_geom": True},                       # positional
            {"geometry_index": 0, "has_geom": False},                      # inline point
            {"has_geom": True},                                            # no index: positional
            {"has_geom": True, "geom_ref": "wd:Q7_0", "source": "wd"},     # borrowed
            {"has_geom": True, "geom_ref": "og:1_4"},                      # own, by ref
            "not a dict",
        ]}
        self.assertEqual(geometry.geom_keys_for(src), [
            ("og:1_3", "og"), ("og:1_2", "og"), ("wd:Q7_0", "wd"), ("og:1_4", "og")])

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
