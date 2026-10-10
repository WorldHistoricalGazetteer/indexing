"""Per-geometry tile channels (place#166).

One ``polygon > point`` vote used to decide four things about a bucket's
tiles at once — extent footprint, z8 pin, point clustering, no-drop — and
every hybrid gazetteer got at least one of them wrong. These tests pin the
replacement: each feature routes itself by geometry type into one of four
channels, and the tippecanoe plan follows from what was streamed, never from
a majority.

Three layers of test, cheapest first:

* routing, dissolve, extent and anchor geometry — pure shapely, no I/O;
* the channel plan and the build driver — ``generate_tileset`` and
  ``tile_join`` are mocked and their arguments asserted, so the zoom ranges
  and flags each channel is tiled with are checked without tippecanoe;
* an end-to-end build of a hybrid fixture with the real ``tippecanoe`` /
  ``tile-join`` when they are on PATH, decoded tile by tile, and then held
  to its ledger by ``verify_tileset_channels`` — including two deliberately
  broken builds the verifier must REFUSE, so that a green run is evidence of
  something.

Run package-qualified (``python -m unittest tests.test_tile_channels``) so the
sandbox in ``tests/__init__`` is installed; see ``tests/_sandbox.py``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from shapely.geometry import LineString, Point, Polygon, shape

from processing import generate_tiles as gt
from processing.generate_tiles import (
    CHANNEL_EXTENT,
    CHANNEL_LABELS,
    CHANNEL_POINTS,
    CHANNEL_SHAPES,
    BucketStream,
)
from tests._sandbox import assert_sandboxed


class _FakeReader:
    def __init__(self, store=None):
        self._store = store or {}

    def get(self, key):
        return self._store.get(key)

    def close(self):
        pass


# ─── fixture geometry (near 10°E 50°N so z8+ tiles exist) ──────────────────
_SQ_A = Polygon([(10.0, 50.0), (10.1, 50.0), (10.1, 50.1), (10.0, 50.1)])
_SQ_B = Polygon([(10.1, 50.0), (10.2, 50.0), (10.2, 50.1), (10.1, 50.1)])
_FAR = Polygon([(12.0, 52.0), (12.1, 52.0), (12.1, 52.1), (12.0, 52.1)])
_ROUTE = LineString([(10.0, 50.3), (10.3, 50.3), (10.3, 50.5)])


def _gj(g):
    return g.__geo_interface__


# ═══════════════════════════════════════════════════════════════════════════
# 1. Routing
# ═══════════════════════════════════════════════════════════════════════════

class GeometryRouting(unittest.TestCase):

    def test_each_type_has_a_kind_and_a_channel(self):
        cases = {
            "Point": ("point", CHANNEL_POINTS),
            "MultiPoint": ("point", CHANNEL_POINTS),
            "Polygon": ("polygon", CHANNEL_SHAPES),
            "MultiPolygon": ("polygon", CHANNEL_SHAPES),
            "LineString": ("line", CHANNEL_SHAPES),
            "MultiLineString": ("line", CHANNEL_SHAPES),
        }
        for t, (kind, channel) in cases.items():
            with self.subTest(t):
                self.assertEqual(gt._geometry_kind({"type": t, "coordinates": []}), kind)
                self.assertEqual(gt._channel_for(kind), channel)

    def test_lines_are_not_points(self):
        """The defect in one line: the old test counted everything that was
        not polygonal as a point, so pl's routes were 'point=18,250'."""
        self.assertNotEqual(gt._geometry_kind(_gj(_ROUTE)), "point")
        self.assertEqual(gt._geometry_kind(_gj(_ROUTE)), "line")

    def test_geometry_collection_classified_by_most_areal_member(self):
        gc = {"type": "GeometryCollection",
              "geometries": [_gj(Point(0, 0)), _gj(_ROUTE), _gj(_SQ_A)]}
        self.assertEqual(gt._geometry_kind(gc), "polygon")
        gc = {"type": "GeometryCollection", "geometries": [_gj(Point(0, 0)), _gj(_ROUTE)]}
        self.assertEqual(gt._geometry_kind(gc), "line")

    def test_unknown_geometry_routes_nowhere(self):
        self.assertIsNone(gt._geometry_kind({"type": "Circle"}))
        self.assertIsNone(gt._geometry_kind(None))
        self.assertIsNone(gt._channel_for(None))


# ═══════════════════════════════════════════════════════════════════════════
# 2. Per-feature dissolve
# ═══════════════════════════════════════════════════════════════════════════

class PerFeatureDissolve(unittest.TestCase):

    def test_touching_parts_become_one_polygon(self):
        mp = {"type": "MultiPolygon",
              "coordinates": [_gj(_SQ_A)["coordinates"], _gj(_SQ_B)["coordinates"]]}
        out = gt._dissolve_parts(mp)
        self.assertEqual(out["type"], "Polygon")
        self.assertAlmostEqual(shape(out).area, 0.02, places=9)

    def test_disjoint_parts_are_left_alone_and_never_unioned(self):
        mp = {"type": "MultiPolygon",
              "coordinates": [_gj(_SQ_A)["coordinates"], _gj(_FAR)["coordinates"]]}
        with mock.patch("shapely.ops.unary_union",
                        side_effect=AssertionError("union called for disjoint parts")):
            out = gt._dissolve_parts(mp)
        self.assertIs(out, mp)

    def test_many_parts_use_the_tree_path(self):
        """Above the threshold the precheck goes through an STRtree; the
        answer must not change with the path taken."""
        n = gt._DISSOLVE_TREE_THRESHOLD + 5
        islands = [Polygon([(i, 0), (i + 0.4, 0), (i + 0.4, 0.4), (i, 0.4)]) for i in range(n)]
        mp = {"type": "MultiPolygon", "coordinates": [_gj(p)["coordinates"] for p in islands]}
        self.assertIs(gt._dissolve_parts(mp), mp)   # all disjoint
        islands[1] = Polygon([(0.2, 0), (2.2, 0), (2.2, 0.4), (0.2, 0.4)])  # overlaps 0 and 2
        mp = {"type": "MultiPolygon", "coordinates": [_gj(p)["coordinates"] for p in islands]}
        out = gt._dissolve_parts(mp)
        self.assertEqual(out["type"], "MultiPolygon")
        self.assertEqual(len(out["coordinates"]), n - 2)

    def test_single_polygon_untouched(self):
        g = _gj(_SQ_A)
        self.assertIs(gt._dissolve_parts(g), g)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Extent — lines contribute, points do not
# ═══════════════════════════════════════════════════════════════════════════

class ExtentFootprint(unittest.TestCase):

    def test_lines_contribute_a_buffer(self):
        sink = []
        gt._accumulate_coverage(_gj(_ROUTE), sink)
        self.assertEqual(len(sink), 1)
        self.assertGreater(sink[0].area, 0)
        self.assertTrue(sink[0].contains(Point(10.15, 50.3)))
        # roughly line length × 2 × buffer: not a hairline, not the world
        self.assertLess(sink[0].area, _ROUTE.length * 4 * gt._LINE_BUFFER_DEG)

    def test_points_contribute_nothing(self):
        sink = []
        gt._accumulate_coverage(_gj(Point(1, 1)), sink)
        self.assertEqual(sink, [])

    def test_a_line_only_bucket_gets_an_extent(self):
        """pl: 230 routes and no polygons had no extent anywhere before."""
        sink = []
        gt._accumulate_coverage(_gj(_ROUTE), sink)
        feat = gt._coverage_feature(sink, "pl")
        self.assertIsNotNone(feat)
        self.assertEqual(feat["properties"]["coverage"], 1)
        self.assertGreater(shape(feat["geometry"]).area, 0)

    def test_small_holes_are_removed_large_ones_kept(self):
        outer = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        tiny = Polygon([(0.5, 0.5), (0.501, 0.5), (0.501, 0.501), (0.5, 0.501)])
        big = Polygon([(0.1, 0.1), (0.4, 0.1), (0.4, 0.4), (0.1, 0.4)])
        g = Polygon(outer.exterior.coords, [tiny.exterior.coords, big.exterior.coords])
        out = gt._drop_small_holes(g)
        self.assertEqual(len(out.interiors), 1)
        self.assertAlmostEqual(Polygon(out.interiors[0]).area, 0.09, places=9)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Streaming: the vote is gone
# ═══════════════════════════════════════════════════════════════════════════

def _write_docs(tmp: Path, ns: str, docs: list[dict]) -> None:
    src = tmp / ns / "final"
    src.mkdir(parents=True, exist_ok=True)
    with (src / "places.jsonl").open("w") as fh:
        for d in docs:
            fh.write(json.dumps(d) + "\n")


def _hybrid_docs(ns: str) -> tuple[list[dict], dict]:
    """2 adjacent polygons (as ONE MultiPolygon place + one Polygon), a line,
    and 3 points — a miniature wd/hgis."""
    docs = [
        {"place_id": f"{ns}:1", "title": "Two-part shire",
         "geometries": [{"geom_ref": f"{ns}:1_0"}]},
        {"place_id": f"{ns}:2", "title": "Far county",
         "geometries": [{"geom_ref": f"{ns}:2_0"}]},
        {"place_id": f"{ns}:3", "title": "Old road",
         "geometries": [{"geom_ref": f"{ns}:3_0"}]},
        {"place_id": f"{ns}:4", "title": "Town A",
         "geometries": [{"repr_point": {"lon": 10.05, "lat": 50.05}}]},
        {"place_id": f"{ns}:5", "title": "Town B",
         "geometries": [{"repr_point": {"lon": 10.5, "lat": 50.5}}]},
        {"place_id": f"{ns}:6", "title": "Town C",
         "geometries": [{"repr_point": {"lon": 11.0, "lat": 51.0}}]},
    ]
    # A dense knot of hamlets so tippecanoe has something to cluster at low
    # zoom (three scattered towns never fall within 10 px of each other).
    docs += [{"place_id": f"{ns}:{100 + i}", "title": f"Hamlet {i}",
              "geometries": [{"repr_point": {"lon": 10.5 + (i % 8) * 0.0005,
                                             "lat": 50.5 + (i // 8) * 0.0005}}]}
             for i in range(40)]
    store = {
        f"{ns}:1_0": {"type": "MultiPolygon",
                      "coordinates": [_gj(_SQ_A)["coordinates"], _gj(_SQ_B)["coordinates"]]},
        f"{ns}:2_0": _gj(_FAR),
        f"{ns}:3_0": _gj(_ROUTE),
    }
    return docs, store


class StreamingWithoutAVote(unittest.TestCase):

    def test_point_majority_still_yields_extent_and_labels(self):
        """wd: 11.4M points, 51k polygons — point-dominant, and its polygons
        had no extent. Now 3 points vs 3 shapes still gets every channel."""
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            docs, store = _hybrid_docs("wd")
            _write_docs(tmp, "wd", docs)
            out = tmp / "tiles"; out.mkdir()
            with mock.patch.object(gt, "STAGED_BASE_DIR", str(tmp)):
                s = gt._stream_bucket("wd", _FakeReader(store), out_dir=out)
            self.assertGreater(s.counts["point"], s.shapes, "fixture is not point-dominant")
            self.assertEqual(set(s.paths), {CHANNEL_POINTS, CHANNEL_SHAPES,
                                            CHANNEL_EXTENT, CHANNEL_LABELS})
            self.assertTrue(s.extent)
            self.assertEqual(s.labels, 3)       # two polygons + one line

    def test_polygon_majority_still_writes_points_to_their_own_channel(self):
        """hgis: polygon-dominant, so its points were pinned to z8 and never
        clustered. Points must land in the points channel regardless."""
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            docs, store = _hybrid_docs("hgis")
            docs = [d for d in docs if d["place_id"] in ("hgis:1", "hgis:2", "hgis:3", "hgis:4")]
            _write_docs(tmp, "hgis", docs)
            out = tmp / "tiles"; out.mkdir()
            with mock.patch.object(gt, "STAGED_BASE_DIR", str(tmp)):
                s = gt._stream_bucket("hgis", _FakeReader(store), out_dir=out)
            self.assertGreater(s.shapes, s.counts["point"], "fixture is not polygon-dominant")
            self.assertIn(CHANNEL_POINTS, s.paths)
            pts = [json.loads(l) for l in s.paths[CHANNEL_POINTS].read_text().splitlines()]
            self.assertEqual(len(pts), 1)
            self.assertEqual(pts[0]["geometry"]["type"], "Point")

    def test_counts_name_lines_separately(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            docs, store = _hybrid_docs("pl")
            _write_docs(tmp, "pl", docs)
            out = tmp / "tiles"; out.mkdir()
            with mock.patch.object(gt, "STAGED_BASE_DIR", str(tmp)):
                s = gt._stream_bucket("pl", _FakeReader(store), out_dir=out)
            self.assertEqual(s.counts, {"point": 43, "polygon": 2, "line": 1})

    def test_the_multipart_place_is_written_dissolved(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            docs, store = _hybrid_docs("wd")
            _write_docs(tmp, "wd", docs)
            out = tmp / "tiles"; out.mkdir()
            with mock.patch.object(gt, "STAGED_BASE_DIR", str(tmp)):
                s = gt._stream_bucket("wd", _FakeReader(store), out_dir=out)
            shapes = {json.loads(l)["properties"]["place_id"]: json.loads(l)
                      for l in s.paths[CHANNEL_SHAPES].read_text().splitlines()}
            self.assertEqual(shapes["wd:1"]["geometry"]["type"], "Polygon")   # was MultiPolygon
            self.assertEqual(shapes["wd:3"]["geometry"]["type"], "LineString")

    def test_empty_channels_leave_no_file(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_docs(tmp, "gn", [{"place_id": "gn:1", "title": "X",
                                     "geometries": [{"repr_point": {"lon": 1, "lat": 2}}]}])
            out = tmp / "tiles"; out.mkdir()
            with mock.patch.object(gt, "STAGED_BASE_DIR", str(tmp)):
                s = gt._stream_bucket("gn", _FakeReader(), out_dir=out)
            self.assertEqual(set(s.paths), {CHANNEL_POINTS})
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["gn.points.geojsonl"])
            self.assertFalse(s.extent)
            self.assertEqual(s.labels, 0)


# ═══════════════════════════════════════════════════════════════════════════
# 5. The tippecanoe plan, without tippecanoe
# ═══════════════════════════════════════════════════════════════════════════

def _stream_with(channels: set[str], *, extent: bool | None = None) -> BucketStream:
    s = BucketStream()
    for ch in channels:
        s.paths[ch] = Path(f"/nonexistent/x.{ch}.geojsonl")
    s.extent = CHANNEL_EXTENT in channels if extent is None else extent
    return s


class ChannelPlan(unittest.TestCase):

    def _by(self, plan):
        return {p["channel"]: p for p in plan}

    def test_hybrid_bucket_gets_four_passes_with_the_right_flags(self):
        plan = self._by(gt._channel_plan(_stream_with(set(gt.CHANNELS))))
        self.assertEqual(set(plan), set(gt.CHANNELS))
        pts, shp, ext, lbl = (plan[CHANNEL_POINTS], plan[CHANNEL_SHAPES],
                              plan[CHANNEL_EXTENT], plan[CHANNEL_LABELS])
        self.assertEqual((pts["minzoom"], pts["maxzoom"]), (0, 10))
        self.assertTrue(pts["cluster_points"]); self.assertFalse(pts["preserve_all"])
        self.assertEqual((shp["minzoom"], shp["maxzoom"]), (gt._BOUNDARY_MINZOOM, 10))
        self.assertTrue(shp["preserve_all"]); self.assertFalse(shp["cluster_points"])
        self.assertEqual((ext["minzoom"], ext["maxzoom"]), (0, gt._COVERAGE_MAXZOOM))
        # labels share the shapes' pin: no anchor below the zoom its shape draws at
        self.assertEqual((lbl["minzoom"], lbl["maxzoom"]), (gt._BOUNDARY_MINZOOM, 10))
        self.assertTrue(lbl["preserve_all"]); self.assertFalse(lbl["cluster_points"])

    def test_points_are_clustered_even_when_shapes_dominate(self):
        """The hgis failure: clustering was a per-bucket switch."""
        plan = self._by(gt._channel_plan(_stream_with({CHANNEL_POINTS, CHANNEL_SHAPES,
                                                       CHANNEL_EXTENT, CHANNEL_LABELS})))
        self.assertTrue(plan[CHANNEL_POINTS]["cluster_points"])

    def test_shapes_without_an_extent_are_not_pinned(self):
        plan = self._by(gt._channel_plan(_stream_with({CHANNEL_SHAPES, CHANNEL_LABELS})))
        self.assertEqual(plan[CHANNEL_SHAPES]["minzoom"], 0)
        self.assertEqual(plan[CHANNEL_LABELS]["minzoom"], 0)

    def test_points_only_is_one_clustered_pass(self):
        plan = gt._channel_plan(_stream_with({CHANNEL_POINTS}))
        self.assertEqual([p["channel"] for p in plan], [CHANNEL_POINTS])
        self.assertTrue(plan[0]["cluster_points"])

    def test_context_overlay_keeps_its_own_config(self):
        cfg = {"minzoom": 3, "maxzoom": 9, "cluster_points": False}
        plan = self._by(gt._channel_plan(_stream_with({CHANNEL_POINTS}), ctx_cfg=cfg))
        self.assertEqual((plan[CHANNEL_POINTS]["minzoom"], plan[CHANNEL_POINTS]["maxzoom"]), (3, 9))
        self.assertFalse(plan[CHANNEL_POINTS]["cluster_points"])

    def test_only_the_real_feature_passes_are_required(self):
        plan = self._by(gt._channel_plan(_stream_with(set(gt.CHANNELS))))
        self.assertTrue(plan[CHANNEL_POINTS]["required"])
        self.assertTrue(plan[CHANNEL_SHAPES]["required"])
        self.assertFalse(plan[CHANNEL_EXTENT]["required"])
        self.assertFalse(plan[CHANNEL_LABELS]["required"])


class BuildDriver(unittest.TestCase):
    """``_build_channels`` with tippecanoe and tile-join mocked out."""

    def _run(self, stream, *, fail=()):
        calls = []

        def fake_generate(src, target, layer, desc, **kw):
            ch = target.name.split(".")[-2]
            calls.append((ch, kw))
            if ch in fail:
                return False
            target.write_bytes(b"x")
            return True

        joined = []

        def fake_join(inputs, output, *, layer_name=None):
            joined.extend(p.name for p in inputs)
            output.write_bytes(b"joined")
            return True

        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for ch in stream.paths:
                stream.paths[ch] = tmp / f"b.{ch}.geojsonl"
                stream.paths[ch].write_bytes(b"{}\n")
            with mock.patch.object(gt, "generate_tileset", side_effect=fake_generate), \
                 mock.patch.object(gt, "tile_join", side_effect=fake_join):
                built, failures = gt._build_channels(
                    "b", stream, out_dir=tmp, mbtiles=tmp / "b.mbtiles", description="d")
            ledger = tmp / ("b" + gt.CHANNEL_LEDGER_SUFFIX)
            ledger_json = json.loads(ledger.read_text()) if ledger.exists() else None
        return built, failures, dict(calls), joined, ledger_json

    def test_four_channels_are_tiled_and_joined_into_one_file(self):
        built, failures, calls, joined, ledger = self._run(_stream_with(set(gt.CHANNELS)))
        self.assertTrue(built); self.assertEqual(failures, [])
        self.assertEqual(set(calls), set(gt.CHANNELS))
        self.assertEqual(sorted(joined), sorted(f"b.{c}.mbtiles" for c in gt.CHANNELS))
        self.assertEqual(calls[CHANNEL_SHAPES]["minzoom"], gt._BOUNDARY_MINZOOM)
        self.assertTrue(calls[CHANNEL_POINTS]["cluster_points"])
        self.assertEqual(set(ledger["channels"]), set(gt.CHANNELS))
        self.assertEqual(ledger["channels"][CHANNEL_SHAPES]["minzoom"], gt._BOUNDARY_MINZOOM)

    def test_extent_failure_unpins_the_shapes(self):
        """Old code: footprint failed → 'deploying boundaries without
        footprint' with the boundaries still pinned to z8 — invisible below."""
        built, failures, calls, _, ledger = self._run(
            _stream_with(set(gt.CHANNELS)), fail={CHANNEL_EXTENT})
        self.assertTrue(built)
        self.assertEqual(failures, ["b/extent"])
        self.assertEqual(calls[CHANNEL_SHAPES]["minzoom"], 0)
        self.assertEqual(calls[CHANNEL_LABELS]["minzoom"], 0)
        self.assertNotIn(CHANNEL_EXTENT, ledger["channels"])
        self.assertEqual(ledger["channels"][CHANNEL_SHAPES]["minzoom"], 0)

    def test_label_failure_is_survivable(self):
        built, failures, calls, joined, _ = self._run(
            _stream_with(set(gt.CHANNELS)), fail={CHANNEL_LABELS})
        self.assertTrue(built)
        self.assertEqual(failures, ["b/labels"])
        self.assertNotIn("b.labels.mbtiles", joined)

    def test_points_failure_fails_the_bucket(self):
        built, failures, _, _, ledger = self._run(
            _stream_with(set(gt.CHANNELS)), fail={CHANNEL_POINTS})
        self.assertFalse(built)
        self.assertIn("b/points", failures)
        self.assertIsNone(ledger)


# ═══════════════════════════════════════════════════════════════════════════
# 6. End to end with the real tippecanoe, decoded tile by tile, then verified
# ═══════════════════════════════════════════════════════════════════════════

_HAVE_TIPPECANOE = bool(shutil.which("tippecanoe") and shutil.which("tile-join"))
try:
    import mapbox_vector_tile  # noqa: F401
    _HAVE_MVT = True
except ImportError:
    _HAVE_MVT = False


def _decode_tile(mbtiles: Path, z: int, x: int, y: int, layer: str) -> list[dict]:
    import gzip
    import mapbox_vector_tile
    con = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, (2 ** z - 1) - y)).fetchone()
    finally:
        con.close()
    if not row:
        return []
    blob = row[0]
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    return (mapbox_vector_tile.decode(blob).get(layer) or {}).get("features") or []


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    import math
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


@unittest.skipUnless(_HAVE_TIPPECANOE and _HAVE_MVT,
                     "needs tippecanoe + tile-join on PATH and mapbox_vector_tile")
class EndToEndHybridBucket(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        assert_sandboxed()
        cls._tmp = TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        docs, store = _hybrid_docs("hgis")
        _write_docs(tmp, "hgis", docs)
        cls.out = tmp / "tiles"
        cls.out.mkdir()
        with mock.patch.object(gt, "STAGED_BASE_DIR", str(tmp)), \
             mock.patch.object(gt, "GeomStoreReader", lambda _dir: _FakeReader(store)), \
             mock.patch("processing.tilegen_bands.load_bands", return_value={}):
            produced, refusals, empty = gt.generate_tiles_from_staged(
                buckets=["hgis"], output_dir=cls.out, deploy=False)
        cls.produced, cls.refusals, cls.empty = produced, refusals, empty
        cls.mbtiles = cls.out / "hgis.mbtiles"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_one_tileset_one_layer_one_ledger(self):
        self.assertEqual(self.produced, [self.mbtiles])
        self.assertEqual(self.refusals, {})
        con = sqlite3.connect(f"file:{self.mbtiles}?mode=ro", uri=True)
        md = dict(con.execute("SELECT name, value FROM metadata").fetchall())
        con.close()
        layers = json.loads(md["json"])["vector_layers"]
        self.assertEqual([vl["id"] for vl in layers], ["hgis"])
        fields = set(layers[0]["fields"])
        self.assertTrue({"label", "coverage", "point_count"} <= fields, fields)
        ledger = json.loads((self.out / "hgis.channels.json").read_text())
        self.assertEqual(ledger["counts"], {"point": 43, "polygon": 2, "line": 1})
        self.assertEqual(ledger["label_anchors"], 3)
        self.assertTrue(ledger["extent"])
        self.assertEqual(set(ledger["channels"]), set(gt.CHANNELS))

    def test_low_zoom_has_extent_and_points_but_no_shapes(self):
        feats = _decode_tile(self.mbtiles, 0, 0, 0, "hgis")
        classes = [gt_verify._classify(f) for f in feats]
        self.assertIn("coverage", classes)
        self.assertNotIn("polygon", classes)
        self.assertNotIn("line", classes)
        self.assertNotIn("label", classes)   # anchors share the shapes' z8 pin

    def test_points_are_clustered_below_the_shapes_pin(self):
        """The hgis failure in miniature: a polygon-majority bucket's points
        used to be pinned to z8 and never clustered. Tippecanoe clusters
        only near its base zoom (lower zooms thin by drop rate, exactly as
        the production gn/tgn tilesets do), so look for a cluster feature
        anywhere below z8 rather than at z0."""
        con = sqlite3.connect(f"file:{self.mbtiles}?mode=ro", uri=True)
        try:
            samples = gt_verify.sample_tiles(con, 7)
        finally:
            con.close()
        per_zoom = gt_verify.count_classes(samples, "hgis")
        clusters = sum(c["cluster"] for c in per_zoom.values())
        points = sum(c["point"] for c in per_zoom.values())
        self.assertGreater(clusters, 0, {z: dict(c) for z, c in per_zoom.items()})
        self.assertGreater(points + clusters, 0)

    def test_high_zoom_has_shapes_labels_and_points(self):
        x, y = _tile_xy(10.05, 50.05, 9)
        feats = _decode_tile(self.mbtiles, 9, x, y, "hgis")
        classes = [gt_verify._classify(f) for f in feats]
        self.assertIn("polygon", classes)
        self.assertIn("label", classes)
        self.assertIn("point", classes)
        self.assertNotIn("coverage", classes)
        # the two-part shire arrived as ONE polygon (dissolved), not two
        polys = [f for f in feats if gt_verify._classify(f) == "polygon"
                 and f["properties"].get("place_id") == "hgis:1"]
        self.assertEqual(len(polys), 1)

    def test_line_has_an_anchor_at_high_zoom(self):
        x, y = _tile_xy(10.3, 50.4, 9)   # on the route's longest segment
        feats = _decode_tile(self.mbtiles, 9, x, y, "hgis")
        labels = [f for f in feats if gt_verify._classify(f) == "label"]
        self.assertTrue(any(f["properties"].get("place_id") == "hgis:3" for f in labels),
                        [f["properties"] for f in labels])

    def test_verifier_passes_the_real_build(self):
        ledger = gt_verify.read_ledger(self.mbtiles)
        failures, report = gt_verify.verify(self.mbtiles, ledger, require_shapes=True)
        self.assertEqual(failures, [], "\n".join(report + failures))

    def test_verifier_refuses_a_ledger_that_disagrees(self):
        """Same tileset, ledger edited to claim no shapes: 'label' and
        'coverage' are then unexplained and the verifier must say so."""
        ledger = gt_verify.read_ledger(self.mbtiles)
        ledger["counts"]["polygon"] = 0
        ledger["counts"]["line"] = 0
        failures, _ = gt_verify.verify(self.mbtiles, ledger)
        self.assertTrue(failures)
        self.assertTrue(any("streamed no shapes" in f for f in failures), failures)

    def test_verifier_refuses_an_old_scheme_build(self):
        """The pre-#166 pipeline: everything in one clustered pass, no
        labels, no extent, shapes from z0. Presented with a #166 ledger it
        must fail on the missing labels and the shapes below z8."""
        legacy_dir = self.out / "legacy"
        legacy_dir.mkdir()
        allf = legacy_dir / "hgis.geojsonl"
        with allf.open("wb") as fh:
            for ch in (CHANNEL_POINTS, CHANNEL_SHAPES):
                fh.write((self.out / f"hgis.{ch}.geojsonl").read_bytes())
        legacy = legacy_dir / "hgis.mbtiles"
        self.assertTrue(gt.generate_tileset(allf, legacy, "hgis", "legacy",
                                            minzoom=0, maxzoom=10, cluster_points=True))
        ledger = gt_verify.read_ledger(self.mbtiles)
        failures, _ = gt_verify.verify(legacy, ledger)
        self.assertTrue(any("label" in f for f in failures), failures)
        self.assertTrue(any("leaked below" in f or "coverage" in f for f in failures), failures)

    def test_region_source_without_shapes_is_refused(self):
        ledger = gt_verify.read_ledger(self.mbtiles)
        ledger["counts"] = {"point": 3, "polygon": 0, "line": 0}
        failures, _ = gt_verify.verify(self.mbtiles, ledger, require_shapes=True)
        self.assertTrue(any("region source" in f for f in failures), failures)


from processing import verify_tileset_channels as gt_verify  # noqa: E402


class TippecanoeFlags(unittest.TestCase):
    """The argv ``generate_tileset`` hands tippecanoe, captured without running it.

    tippecanoe's point drop rate (``-r``, default 2.5 per zoom below the base
    zoom) is untouched by ``--no-feature-limit`` and friends, so a "no-drop"
    labels pass thinned its anchors below z10 (hgis: 399 of 892 at z8, 701 at
    z9, all 892 at z10 — measured 10 Oct 2026, identical in the live build).
    """

    def _argv(self, **kw) -> list[str]:
        seen = {}

        def fake_run(cmd, **_):
            seen["cmd"] = [str(c) for c in cmd]
            return mock.Mock(returncode=1)

        with TemporaryDirectory() as tmp, mock.patch.object(gt.subprocess, "run", fake_run):
            src = Path(tmp) / "x.geojsonl"
            src.write_text(json.dumps({"type": "Feature", "properties": {},
                                       "geometry": {"type": "Point", "coordinates": [0, 0]}}) + "\n")
            gt.generate_tileset(src, Path(tmp) / "x.mbtiles", "x", "x", **kw)
        return seen["cmd"]

    def _flag(self, argv, name):
        return argv[argv.index(name) + 1] if name in argv else None

    def test_preserve_all_passes_never_rate_drop_points(self):
        argv = self._argv(preserve_all=True)
        self.assertIn("--no-feature-limit", argv)
        self.assertEqual(self._flag(argv, "--drop-rate"), "1", argv)

    def test_clustered_points_keep_tippecanoes_default_rate_unless_opted_in(self):
        with mock.patch.object(gt, "_POINTS_DROP_RATE", ""):
            argv = self._argv(cluster_points=True)
        self.assertIn("--cluster-distance", argv)
        self.assertNotIn("--drop-rate", argv, argv)
        with mock.patch.object(gt, "_POINTS_DROP_RATE", "1"):
            argv = self._argv(cluster_points=True)
        self.assertEqual(self._flag(argv, "--drop-rate"), "1", argv)

    def test_extent_pass_has_neither(self):
        argv = self._argv()
        self.assertNotIn("--drop-rate", argv)
        self.assertIn("--coalesce-densest-as-needed", argv)


@unittest.skipUnless(_HAVE_TIPPECANOE and _HAVE_MVT,
                     "needs tippecanoe + tile-join on PATH and mapbox_vector_tile")
class LabelScanFallback(unittest.TestCase):
    """A sample can miss one anchor per country-sized polygon (po: labels in
    304 of 39,300 z8 tiles); the verifier then scans the zoom before failing,
    and a tileset with no labels at all still fails after scanning it all."""

    def setUp(self):
        assert_sandboxed()
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _points(self, name, feats, **kw) -> Path:
        src = self.tmp / f"{name}.geojsonl"
        with src.open("w") as fh:
            for props, (lon, lat) in feats:
                fh.write(json.dumps({"type": "Feature", "properties": props,
                                     "geometry": {"type": "Point", "coordinates": [lon, lat]}}) + "\n")
        out = self.tmp / f"{name}.mbtiles"
        self.assertTrue(gt.generate_tileset(src, out, "t", "t", minzoom=8, maxzoom=8, **kw))
        return out

    @staticmethod
    def _tiles_at(mb: Path, z: int) -> int:
        con = sqlite3.connect(f"file:{mb}?mode=ro", uri=True)
        try:
            return con.execute("SELECT COUNT(*) FROM tiles WHERE zoom_level=?", (z,)).fetchone()[0]
        finally:
            con.close()

    def test_scan_finds_a_lone_anchor_and_reports_the_denominator(self):
        # 40 plain points spread over 40 z8 tiles and ONE anchor: a 64-tile
        # sample is all of them here, so call the scan directly.
        feats = [({"place_id": f"t:{i}"}, (i * 2.0, 10.0)) for i in range(40)]
        feats.append(({"label": 1, "place_id": "t:anchor"}, (-120.0, -40.0)))
        mb = self._points("sparse", feats, preserve_all=True)
        found, scanned, total = gt_verify.first_label_scan(mb, "t", 8)
        self.assertEqual(found, 1)
        self.assertEqual(total, self._tiles_at(mb, 8))
        self.assertGreater(total, 20)   # z8 tiles are 1.4 deg wide: 40 points 2 deg apart
        self.assertTrue(1 <= scanned <= total, (scanned, total))

    def test_scan_refuses_a_tileset_with_no_labels_after_scanning_every_tile(self):
        feats = [({"place_id": f"t:{i}"}, (i * 2.0, 10.0)) for i in range(40)]
        mb = self._points("nolabels", feats, preserve_all=True)
        n = self._tiles_at(mb, 8)
        self.assertGreater(n, 20)
        self.assertEqual(gt_verify.first_label_scan(mb, "t", 8), (0, n, n))

    def test_preserve_all_keeps_every_anchor_below_the_base_zoom(self):
        """The hgis finding, reproduced: 200 anchors tiled z8-z10; every one
        must be present at z8, which the default drop rate does not give."""
        feats = [({"label": 1, "place_id": f"t:{i}"}, (-170 + i * 1.7, (i % 60) - 30.0))
                 for i in range(200)]
        src = self.tmp / "anchors.geojsonl"
        with src.open("w") as fh:
            for props, (lon, lat) in feats:
                fh.write(json.dumps({"type": "Feature", "properties": props,
                                     "geometry": {"type": "Point", "coordinates": [lon, lat]}}) + "\n")
        out = self.tmp / "anchors.mbtiles"
        self.assertTrue(gt.generate_tileset(src, out, "t", "t", minzoom=8, maxzoom=10, preserve_all=True))
        con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        try:
            seen = set()
            for (blob,) in con.execute("SELECT tile_data FROM tiles WHERE zoom_level=8"):
                for f in (gt_verify._decode(blob).get("t") or {}).get("features") or []:
                    seen.add(f["properties"]["place_id"])
        finally:
            con.close()
        self.assertEqual(len(seen), 200, f"{len(seen)} of 200 anchors at z8")


class SubmitterNoDeploy(unittest.TestCase):
    """``submit_tiles_slurm --no-deploy`` must reach the array task's command
    line, or the rolling retile pushes every bucket in parallel after all."""

    def _script(self, **kw):
        from processing import submit_tiles_slurm as mod
        with TemporaryDirectory() as tmp:
            with mock.patch.object(mod, "_REPO", tmp):
                return mod._build_sbatch_script(
                    run_id="r", buckets=["wd"], resources={"mem_gb": 1, "wall_s": 60},
                    manifest_path=Path(tmp) / "m.json", array_map_path=Path(tmp) / "a.json",
                    depend_on=None, output_dir=Path(tmp) / "out", suffix="s", **kw)

    def test_default_deploys(self):
        self.assertNotIn("--no-deploy", self._script())

    def test_no_deploy_reaches_generate_tiles(self):
        self.assertIn("--no-deploy", self._script(deploy=False))


if __name__ == "__main__":
    unittest.main()
