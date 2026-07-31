"""Unit tests for the Vision of Britain / GB Historical GIS boundary builder
(``authorities.vob_common``, place#135).

Builds a synthetic in-memory UKDS-style shapefile zip (OSGB EPSG:27700, the
standard GBHGIS attribute schema, two units across two census years) and
exercises the multi-snapshot grouping, per-decade timespans, WGS84
reprojection, ALL-CAPS recasing, and the helper functions.
"""

from __future__ import annotations

import io
import zipfile
import unittest
from pathlib import Path

from authorities import vob_common as vc


# ── name recasing ────────────────────────────────────────────────────────────
class TestTitlecase(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(vc._titlecase_name("LLANDILO FAWR"), "Llandilo Fawr")

    def test_connectives_lowercased(self):
        self.assertEqual(vc._titlecase_name("NEWCASTLE UPON TYNE"), "Newcastle upon Tyne")
        self.assertEqual(vc._titlecase_name("ISLE OF WIGHT"), "Isle of Wight")
        self.assertEqual(vc._titlecase_name("ASHBY DE LA ZOUCH"), "Ashby de la Zouch")
        self.assertEqual(vc._titlecase_name("ALSTON WITH GARRIGILL"), "Alston with Garrigill")

    def test_apostrophe_not_broken(self):
        self.assertEqual(vc._titlecase_name("KING'S NORTON"), "King's Norton")

    def test_hyphen_segments(self):
        self.assertEqual(vc._titlecase_name("STOKE-UPON-TRENT"), "Stoke-upon-Trent")
        self.assertEqual(vc._titlecase_name("HENLEY-ON-THAMES"), "Henley-on-Thames")

    def test_leading_connective_still_capitalised(self):
        self.assertEqual(vc._titlecase_name("LE"), "Le")

    def test_mixed_case_untouched(self):
        self.assertEqual(vc._titlecase_name("Already Cased"), "Already Cased")


# ── helpers ──────────────────────────────────────────────────────────────────
class TestHelpers(unittest.TestCase):
    def test_successor_map_regular(self):
        self.assertEqual(vc._successor_map([1851, 1861, 1871]),
                         {1851: 1861, 1861: 1871, 1871: 1881})

    def test_successor_map_closes_wwii_gap(self):
        # 20thC levels skip 1941 (WWII); 1931 must span to 1951, not 1941.
        succ = vc._successor_map([1911, 1921, 1931, 1951, 1961, 1971])
        self.assertEqual(succ[1931], 1951)
        self.assertEqual(succ[1971], 1981)  # final -> +DECADE

    def test_year_from_filename(self):
        self.assertEqual(vc._year_from_filename("ew1851_regcounties"), 1851)
        self.assertEqual(vc._year_from_filename("ew1911_admcounties"), 1911)
        self.assertIsNone(vc._year_from_filename("no_year_here"))

    def test_lang_map(self):
        self.assertEqual(vc._lang_for("eng"), "en")
        self.assertEqual(vc._lang_for("CYM"), "cy")
        self.assertEqual(vc._lang_for("xyz"), "und")

    def test_reproject_osgb_to_wgs84(self):
        # OSGB easting/northing near Carmarthen, Wales -> ~(-4.06, 51.91).
        geom = {"type": "Polygon", "coordinates": [[
            [248060, 209121], [271382, 209121],
            [271382, 240454], [248060, 240454], [248060, 209121],
        ]]}
        wgs = vc._reproject_to_wgs84(geom)
        xs = [c[0] for c in wgs["coordinates"][0]]
        ys = [c[1] for c in wgs["coordinates"][0]]
        self.assertTrue(-5 < min(xs) and max(xs) < -3, xs)   # west Wales lon
        self.assertTrue(51 < min(ys) and max(ys) < 53, ys)   # ~52 lat


# ── end-to-end build over a synthetic shapefile zip ──────────────────────────
def _make_shapefile_zip() -> Path:
    """Two G_UNITs, each present in 1851 and 1861; unit 200 only in 1851."""
    import shapefile

    def _sq(x, y, s=1000):
        return [[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]

    rows = [
        # (year, g_unit, g_name, x, y)
        (1851, 100, "LLANDILO FAWR", 248060, 209121),
        (1861, 100, "LLANDILO FAWR", 248060, 209121),
        (1851, 200, "KING'S NORTON", 250000, 210000),
        (1861, 300, "NEWCASTLE UPON TYNE", 420000, 560000),
    ]
    by_year: dict[int, list] = {}
    for r in rows:
        by_year.setdefault(r[0], []).append(r)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for year, recs in by_year.items():
            shp, dbf, shx = io.BytesIO(), io.BytesIO(), io.BytesIO()
            w = shapefile.Writer(shp=shp, dbf=dbf, shx=shx, shapeType=shapefile.POLYGON)
            w.field("G_UNIT", "N", 11)
            w.field("G_NAME", "C", 100)
            w.field("G_YEAR", "N", 11)
            w.field("UNITTYPE", "C", 40)
            w.field("NATION", "C", 20)
            w.field("G_LANGUAGE", "C", 4)
            for (yr, gu, gn, x, y) in recs:
                w.poly([_sq(x, y)])
                w.record(gu, gn, yr, "PR_DIST", "ENGLAND", "eng")
            w.close()
            base = f"UKDA-9032-xml/xml/ukds_ew{year}_x/ew{year}_x"
            zf.writestr(base + ".shp", shp.getvalue())
            zf.writestr(base + ".dbf", dbf.getvalue())
            zf.writestr(base + ".shx", shx.getvalue())

    out = Path("/tmp/claude-1000/-home-stephen-PycharmProjects-indexing/"
               "f6961763-f45a-4538-8201-6354569d8280/scratchpad/_vob_synth.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(buf.getvalue())
    return out


class TestBuildDocs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zip = _make_shapefile_zip()
        cls.docs = {d["place_id"]: d
                    for d in vc.build_docs(vc.LEVELS["vob_rd"], cls.zip)}

    def test_one_doc_per_unit(self):
        self.assertEqual(set(self.docs), {"vob_rd:100", "vob_rd:200", "vob_rd:300"})

    def test_multi_snapshot_grouping(self):
        # Unit 100 appears in both census years -> two geometry snapshots.
        self.assertEqual(len(self.docs["vob_rd:100"]["geometries"]), 2)
        self.assertEqual(len(self.docs["vob_rd:200"]["geometries"]), 1)

    def test_per_snapshot_timespans(self):
        """Each census snapshot carries all four bounds (place#164).

        A ``1861`` census attests the unit **at 1861** and says nothing about
        1865 — but the neighbouring snapshots do bound when the configuration
        can have begun and ended. The old ``{"start": {"in": 1861}, "end":
        {"in": 1871}}`` over-claimed every intervening year as definite.
        """
        ts = [g["timespans"][0] for g in self.docs["vob_rd:100"]["geometries"]]
        # 1851 is the first snapshot in the series, so nothing bounds how long
        # before it the unit had existed: start.earliest stays absent.
        self.assertIn(
            {"start": {"latest": 1851},
             "end": {"earliest": 1851, "latest": 1861}}, ts)
        self.assertIn(
            {"start": {"earliest": 1851, "latest": 1861},
             "end": {"earliest": 1861, "latest": 1871}}, ts)

    def test_snapshot_is_definite_only_at_the_census_year(self):
        from processing.gazetteer_temporal_extent import doc_temporal_bounds

        doc = self.docs["vob_rd:100"]
        se, sl, ee, el = doc_temporal_bounds(doc, "vob_rd")
        self.assertTrue(sl <= 1851 <= ee, "definitely alive at a census year")
        self.assertTrue(
            (se is None or se <= 1856) and (el is None or 1856 <= el),
            "possibly alive between censuses",
        )

    def test_polygons_reprojected_to_wgs84(self):
        g = self.docs["vob_rd:100"]["geometries"][0]
        lon, lat = g["repr_point"]["lon"], g["repr_point"]["lat"]
        self.assertTrue(-6 < lon < 2 and 50 < lat < 56, (lon, lat))

    def test_names_recased(self):
        self.assertEqual(self.docs["vob_rd:200"]["title"], "King's Norton")
        self.assertEqual(self.docs["vob_rd:300"]["title"], "Newcastle upon Tyne")

    def test_doc_shape(self):
        d = self.docs["vob_rd:100"]
        self.assertEqual(d["namespace"], "vob_rd")
        self.assertEqual(d["ccodes"], ["GB"])
        self.assertEqual(d["boundary"], "registration-district")
        self.assertEqual(d["types"][0]["identifier"], "registration-district")
        self.assertEqual(d["toponyms"][0]["toponym_id"], "Llandilo Fawr@en")


if __name__ == "__main__":
    unittest.main()
