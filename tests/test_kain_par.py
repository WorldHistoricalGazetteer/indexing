"""Unit tests for the Kain & Oliver ancient-parishes builder (place#135).

Builds a synthetic in-memory OSGB shapefile with the SN 852232 attribute schema
(ID/PAR/PLA/CAT/...) and exercises the parish/place doc construction, CAT typing,
open-start timespan, WGS84 reprojection, and the PAR-vs-PLA toponym handling.
"""

from __future__ import annotations

import importlib.util
import io
import zipfile
import unittest
from pathlib import Path

# The module name is hyphenated (authorities/kain_par-places.py) — load by path.
_spec = importlib.util.spec_from_file_location(
    "kain_par_places",
    str(Path(__file__).resolve().parent.parent / "authorities" / "kain_par-places.py"),
)
kp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kp)


def _make_zip() -> Path:
    import shapefile

    def _sq(x, y, s=1000):
        return [[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]

    # (ID, PAR, PLA, CAT, x, y) — OSGB eastings/northings
    rows = [
        (0, "STAPLEHURST", "STAPLEHURST", "P", 578000, 143000),   # parish, PAR==PLA
        (1, "WIGTON", "OULTON", "T", 324000, 549000),             # township, PAR!=PLA
        (2, "SOME PLACE", "EXTRA BIT", "EP", 400000, 400000),     # extra-parochial
        (3, "ODDCAT", "ODD PLACE", "Z9", 410000, 410000),         # unknown CAT
    ]
    shp, dbf, shx = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, dbf=dbf, shx=shx, shapeType=shapefile.POLYGON)
    w.field("ID", "N", 11)
    w.field("PAR", "C", 100)
    w.field("PLA", "C", 100)
    w.field("CAT", "C", 10)
    for (uid, par, pla, cat, x, y) in rows:
        w.poly([_sq(x, y)])
        w.record(uid, par, pla, cat)
    w.close()

    out = Path("/tmp/claude-1000/-home-stephen-PycharmProjects-indexing/"
               "f6961763-f45a-4538-8201-6354569d8280/scratchpad/_kainpar_synth.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("1851EngWalesParishandPlace.shp", shp.getvalue())
        zf.writestr("1851EngWalesParishandPlace.dbf", dbf.getvalue())
        zf.writestr("1851EngWalesParishandPlace.shx", shx.getvalue())
    out.write_bytes(buf.getvalue())
    return out


class TestCatType(unittest.TestCase):
    def test_known_codes(self):
        self.assertEqual(kp._cat_type("P"), ("parish", "parish (P)"))
        self.assertEqual(kp._cat_type("T"), ("township", "township (T)"))
        self.assertEqual(kp._cat_type("EP"),
                         ("extra-parochial-place", "extra-parochial place (EP)"))

    def test_combined_and_lowercase_normalised(self):
        self.assertEqual(kp._cat_type("P, EP")[0], "parish")
        self.assertEqual(kp._cat_type("p")[0], "parish")

    def test_unknown_falls_back(self):
        ident, src = kp._cat_type("Z9")
        self.assertEqual(ident, "ancient-parish")
        self.assertEqual(src, "Z9")


class TestBuildDocs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {d["place_id"]: d for d in kp.build_docs(_make_zip())}

    def test_one_doc_per_id(self):
        self.assertEqual(set(self.docs), {f"kain_par:{i}" for i in range(4)})

    def test_title_is_place_recased(self):
        self.assertEqual(self.docs["kain_par:0"]["title"], "Staplehurst")

    def test_open_start_timespan_ends_1851(self):
        ts = self.docs["kain_par:0"]["geometries"][0]["timespans"][0]
        self.assertEqual(ts, {"end": {"in": 1851}})
        self.assertNotIn("start", ts)

    def test_parish_added_as_second_toponym_when_distinct(self):
        tops = [t["toponym_id"] for t in self.docs["kain_par:1"]["toponyms"]]
        self.assertEqual(tops, ["Oulton@en", "Wigton@en"])

    def test_no_duplicate_toponym_when_par_equals_pla(self):
        tops = [t["toponym_id"] for t in self.docs["kain_par:0"]["toponyms"]]
        self.assertEqual(tops, ["Staplehurst@en"])

    def test_cat_typing(self):
        self.assertEqual(self.docs["kain_par:0"]["types"][0]["identifier"], "parish")
        self.assertEqual(self.docs["kain_par:1"]["types"][0]["identifier"], "township")
        self.assertEqual(self.docs["kain_par:3"]["types"][0]["identifier"], "ancient-parish")

    def test_reprojected_to_wgs84_and_gb(self):
        d = self.docs["kain_par:0"]
        rp = d["geometries"][0]["repr_point"]
        self.assertTrue(-6 < rp["lon"] < 2 and 50 < rp["lat"] < 56, rp)
        self.assertEqual(d["ccodes"], ["GB"])
        self.assertEqual(d["boundary"], "ancient-parish")


if __name__ == "__main__":
    unittest.main()
