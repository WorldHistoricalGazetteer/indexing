"""The OSM/OHM ingest handlers must build closed area-tagged ways as POLYGONS.

place#256. Both handlers built *every* way with ``create_linestring``, area-tagged
or not, so a lake, park or landuse block was ingested as an open line:
``has_geom`` true with no usable polygon, ``h3_cover`` collapsed to a single
centroid hex, and ``containment=exact`` silently degrading to a ``repr_point``
test. The 10.5M polygons restored in July (place#145) were an in-place
augmentation pass, NOT a handler fix — so the next re-ingest undid them.

THE FIXTURE IS BUILT TO REACH THE DISTINCTION, not to pass. The interesting case
is the third one: a way that is CLOSED but carries ``area=no``. A naive fix
("closed ⇒ polygon") turns a loop canal or ring road into a lake, which is a
different corruption of the same field and would pass a fixture containing only
the first two cases. We delegate the judgement to osmium's area assembler rather
than re-deriving it, and this fixture is what proves the delegation holds.

    closed + natural=water       -> AREA   (the defect: was line)
    open   + waterway=river      -> LINE   (must not regress)
    closed + waterway=canal      -> LINE   (area=no; must NOT become a polygon)
     + area=no
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import osmium  # noqa: F401
    import shapely  # noqa: F401
    _DEPS = hasattr(osmium, "FileProcessor") and hasattr(
        osmium.FileProcessor, "with_areas")
except Exception:  # pragma: no cover
    _DEPS = False

REPO = Path(__file__).resolve().parent.parent

FIXTURE_OSM = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="whg-test">
  <node id="1" lat="50.0" lon="0.0" version="1"/>
  <node id="2" lat="50.0" lon="0.1" version="1"/>
  <node id="3" lat="50.1" lon="0.1" version="1"/>
  <node id="4" lat="50.1" lon="0.0" version="1"/>
  <node id="5" lat="51.0" lon="1.0" version="1"/>
  <node id="6" lat="51.0" lon="1.1" version="1"/>
  <way id="100" version="1">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="name" v="Test Lake"/><tag k="natural" v="water"/>
  </way>
  <way id="200" version="1">
    <nd ref="5"/><nd ref="6"/>
    <tag k="name" v="Test River"/><tag k="waterway" v="river"/>
  </way>
  <way id="300" version="1">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="1"/>
    <tag k="name" v="Loop Way"/><tag k="waterway" v="canal"/><tag k="area" v="no"/>
  </way>
</osm>
"""


class _Tracker:
    """Minimal stand-in for ProgressTracker; never skips."""

    def __init__(self):
        self.counts = {"node": 0, "way": 0, "relation": 0, "area": 0}
        self.targets = dict(self.counts)

    def should_skip(self, type_):
        return False

    def increment(self, type_):
        self.counts[type_] += 1


def _load(script_name, module_name):
    path = REPO / "authorities" / script_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(handler_cls, module, fixture_path):
    module._ATTESTATION_YEAR = 2025
    docs = []
    handler = handler_cls(_Tracker(), docs.append)
    handler.apply_file(str(fixture_path))
    return handler, {d["place_id"]: d for d in docs}


@unittest.skipUnless(_DEPS, "pyosmium with FileProcessor.with_areas not installed")
class WayAreaHandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("WHG_STAGING_MODE", "1")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls._tmp.name) / "fixture.osm"
        cls.fixture.write_text(FIXTURE_OSM, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _geom_class(self, doc):
        geoms = doc.get("geometries") or []
        self.assertTrue(geoms, f"{doc['place_id']} carries no geometry at all")
        return geoms[0].get("geom_class")

    # ---- osm -------------------------------------------------------------

    def test_osm_closed_area_tagged_way_becomes_an_area(self):
        mod = _load("osm-places.py", "osm_places_areatest")
        _, docs = _run(mod.OSMHandler, mod, self.fixture)
        self.assertIn("osm:w100", docs, "the lake was not ingested at all")
        self.assertEqual(self._geom_class(docs["osm:w100"]), "area",
                         "a closed natural=water way must be a POLYGON — this is "
                         "the place#256 defect: it was built as a LineString")

    def test_osm_open_way_stays_a_line(self):
        mod = _load("osm-places.py", "osm_places_areatest")
        _, docs = _run(mod.OSMHandler, mod, self.fixture)
        self.assertEqual(self._geom_class(docs["osm:w200"]), "line")

    def test_osm_closed_way_tagged_area_no_stays_a_line(self):
        """The case a naive 'closed ⇒ polygon' fix would corrupt."""
        mod = _load("osm-places.py", "osm_places_areatest")
        _, docs = _run(mod.OSMHandler, mod, self.fixture)
        self.assertEqual(self._geom_class(docs["osm:w300"]), "line",
                         "area=no means NOT an area, however closed the way is")

    def test_osm_emits_each_way_exactly_once(self):
        """No way may be written as both a polygon and a line.

        ``write_staged_place_doc`` APPENDS and does not dedupe, so a second row
        for the same place_id would leave the surviving geometry dependent on
        row ordering through four downstream stages.
        """
        mod = _load("osm-places.py", "osm_places_areatest")
        module_docs = []
        mod._ATTESTATION_YEAR = 2025
        handler = mod.OSMHandler(_Tracker(), module_docs.append)
        handler.apply_file(str(self.fixture))
        ids = [d["place_id"] for d in module_docs]
        self.assertEqual(len(ids), len(set(ids)),
                         f"duplicate place_ids emitted: {ids}")
        self.assertEqual(handler.area_way_ids, {100},
                         "only the area-tagged closed way should be polygonised")

    # ---- ohm -------------------------------------------------------------

    def test_ohm_closed_area_tagged_way_becomes_an_area(self):
        mod = _load("ohm-places.py", "ohm_places_areatest")
        _, docs = _run(mod.OHMHandler, mod, self.fixture)
        self.assertIn("ohm:w100", docs)
        self.assertEqual(self._geom_class(docs["ohm:w100"]), "area")

    def test_ohm_closed_way_tagged_area_no_stays_a_line(self):
        mod = _load("ohm-places.py", "ohm_places_areatest")
        _, docs = _run(mod.OHMHandler, mod, self.fixture)
        self.assertEqual(self._geom_class(docs["ohm:w300"]), "line")

    def test_ohm_emits_each_way_exactly_once(self):
        mod = _load("ohm-places.py", "ohm_places_areatest")
        handler, docs = _run(mod.OHMHandler, mod, self.fixture)
        self.assertEqual(handler.area_way_ids, {100})


class SourceSpecificGateTest(unittest.TestCase):
    """The tag gates are source-specific and must not be unified.

    ``osm`` accepts 7 keys, ``ohm`` 13. Using the OSM set for OHM silently drops
    ~57k indexed ways (place#145 OHM canary). The area handler calls each
    script's OWN ``process_tags``, so this asserts the two really do differ —
    if they ever converge, the reason this mattered has gone and the comment
    pointing at it is misleading.
    """

    def test_ohm_accepts_strictly_more_tag_keys_than_osm(self):
        import re
        ohm_src = (REPO / "authorities" / "ohm-places.py").read_text()
        m = re.search(r"TYPE_TAG_KEYS\s*=\s*(\[[^\]]*\])", ohm_src, re.S)
        self.assertIsNotNone(m, "TYPE_TAG_KEYS not found in ohm-places.py")
        ohm_keys = set(eval(m.group(1)))

        osm_src = (REPO / "authorities" / "osm-places.py").read_text()
        m2 = re.search(r"elif any\(k in tags for k in (\[[^\]]*\])\)", osm_src, re.S)
        self.assertIsNotNone(m2, "osm-places.py tag gate not found")
        osm_keys = set(eval(m2.group(1))) | {"place"}

        self.assertEqual(len(osm_keys), 7, f"osm gate changed: {sorted(osm_keys)}")
        self.assertEqual(len(ohm_keys), 13, f"ohm gate changed: {sorted(ohm_keys)}")
        self.assertTrue(osm_keys < ohm_keys,
                        "ohm must be a strict superset of osm")


class WithLocationsCompatibilityTest(unittest.TestCase):
    """``with_locations`` takes ``storage``, not ``idx`` (pyosmium 4.x).

    Both handlers called ``.with_locations(idx='flex_mem')``, which raises
    TypeError on the pyosmium in the CRC ``whg`` env (4.2.0) and locally
    (4.3.1) — so ``apply_file`` could not run at all. The other call sites in
    the repo pass it positionally and were unaffected. Guard the fix site: the
    default storage IS ``flex_mem``, so the no-argument form is equivalent.
    """

    def test_no_handler_uses_the_idx_keyword(self):
        for name in ("osm-places.py", "ohm-places.py"):
            src = (REPO / "authorities" / name).read_text()
            self.assertNotIn("with_locations(idx", src,
                             f"{name}: with_locations(idx=...) raises TypeError "
                             f"on pyosmium 4.x — use the positional or default form")


if __name__ == "__main__":
    unittest.main()
