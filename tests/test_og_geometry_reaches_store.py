"""og's computed hulls must actually reach the geom store (§4.2).

``authorities/ottgaz-places.py`` called ``enrich_geometry(geo, timespans=ts)``
with **no ``geom_key``**, and staged without configuring a module writer.
Both are required: without a key ``enrich_geometry`` computes ``repr_point``,
``hull`` and ``bounds``, returns ``has_geom=False``, and **discards the
polygon**; without a configured writer the key is ignored.

So og's 249 computed ``ofs`` hulls existed as geometry *entries* and as no
stored geometry at all — unservable (no `containment=exact`, no geometry
retrieval) and untileable. The staging log reported "with computed geometry:
249" throughout, because that counter counts entries rather than polygons.

The assertion here is deliberately **not** "the source contains a geom_key
argument", which would pass on a key that is misspelled, shadowed, or
ignored for want of a writer. It runs the real ``enrich_geometry`` against a
real ``GeomStoreWriter`` and asserts the bytes are retrievable afterwards —
the only claim anyone downstream cares about.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

try:                            # package-qualified run (tests/__init__.py ran)
    from ._sandbox import assert_sandboxed
except ImportError:             # `discover -s tests` puts tests/ on sys.path
    from _sandbox import assert_sandboxed


HULL = {
    "type": "Polygon",
    "coordinates": [[[32.0, 39.0], [33.0, 39.0], [33.0, 40.0],
                     [32.0, 40.0], [32.0, 39.0]]],
}


class OgHullReachesTheGeomStore(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        assert_sandboxed()

    def test_enrich_geometry_without_a_key_discards_the_polygon(self):
        """The pre-change call shape — documents what was actually lost.

        This passes before and after; it is here so the defect is legible
        rather than inferred, and so a future change to enrich_geometry's
        no-key behaviour is caught.
        """
        from processing.helpers import enrich_geometry

        ge = enrich_geometry(HULL, timespans=None)
        self.assertIsNotNone(ge)
        self.assertFalse(
            ge.get("has_geom"),
            "without a geom_key the polygon is not stored — this is why og's "
            "hulls were unservable despite the staging log counting them",
        )

    @staticmethod
    def _load_og():
        """Load the authority by path — its filename has a hyphen."""
        import importlib.util

        src = (Path(__file__).resolve().parent.parent
               / "authorities" / "ottgaz-places.py")
        spec = importlib.util.spec_from_file_location("ottgaz_places", src)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_og_stages_its_hull_into_the_store(self):
        """The real claim: og's OWN call puts retrievable bytes in the store.

        Drives ``process_row`` rather than calling ``enrich_geometry``
        directly with a hand-written key — a test that supplies its own
        geom_key exercises the helper and would pass on the pre-change
        source, proving nothing about og.
        """
        import json

        from processing.geom_store import (
            GeomStoreWriter, configure_module_writer,
        )

        og = self._load_og()

        # A sancak with three ofs member points, so _match_points hits and
        # _hull_geometry produces a real polygon.
        ofs_idx = {("sancak", og._norm("Ankara")): [
            (32.8, 39.9), (33.1, 40.1), (32.5, 39.6),
        ]}
        row = {"ottgaz_id": "12345", "Placename@tr": "Ankara",
               "Unit": "sancak", "StartDate": "1600", "EndDate": "1700"}

        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "geom"
            with GeomStoreWriter(str(store), "og") as gsw:
                configure_module_writer(gsw)
                try:
                    doc = og.process_row(row, ofs_idx, {})
                finally:
                    configure_module_writer(None)

            self.assertIsNotNone(doc, "fixture row should produce a document")
            self.assertTrue(doc["geometries"],
                            "fixture should match ofs points and build a hull")
            self.assertTrue(
                doc["geometries"][0].get("has_geom"),
                "og's hull must be written to the geom store — has_geom False "
                "means the polygon was computed and thrown away, which is "
                "exactly what shipped",
            )

            # The writer stages `<ns>.bin` + `<ns>.index.json`; the shard
            # index a GeomStoreReader needs is only built later by
            # consolidate_geom_store. Assert against what staging actually
            # produces: the key is recorded and the WKB has real bytes.
            idx_path = store / "og.index.json"
            bin_path = store / "og.bin"
            self.assertTrue(idx_path.exists(), "no staging index was written")
            entries = json.loads(idx_path.read_text())
            keys = {e["k"] if "k" in e else e.get("key") for e in entries}
            self.assertIn(
                "og:12345_0", keys,
                "the hull must be recorded under its key, or exact containment "
                "and tile generation see nothing",
            )
            self.assertGreater(bin_path.stat().st_size, 0,
                               "the WKB payload must actually be written")

    def test_source_configures_a_module_writer_and_passes_a_key(self):
        """Both halves are needed; either alone stores nothing.

        A source-level check, because the runtime test above uses its own
        writer and so cannot observe whether *staging* configures one.
        """
        src = (Path(__file__).resolve().parent.parent
               / "authorities" / "ottgaz-places.py").read_text()

        # assertTrue, not assertIn: a failing assertIn dumps the whole source
        # file into the report and buries the message.
        self.assertTrue(
            'geom_key=f"{place_id}_0"' in src,
            "ottgaz-places.py must pass geom_key to enrich_geometry, or the "
            "hull is computed and thrown away",
        )
        self.assertTrue(
            "configure_module_writer" in src,
            "enrich_geometry ignores geom_key with no module writer "
            "configured, so stage_file must set one up",
        )


if __name__ == "__main__":
    unittest.main()
