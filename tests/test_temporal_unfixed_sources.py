"""Temporal encoding for the five sources place#164's §3 table missed.

The issue's per-source table classified eighteen namespaces and the step-2
commit fixed fifteen scripts. These five still emitted the defect, and two of
them sit *inside* step 5's re-ingest chains — so a full rebuild would have
written the wrong encoding into the new index for `wd` and `gn` while the plan
recorded them as fixed.

============================  =====================================================
script                        was
============================  =====================================================
``wikidata-geoshapes.py``     hardcoded ``{"start": {"in": 2025}, "end": {"in": 2025}}``
``trismegistos/places.py``    ``start.in``/``end.in`` over the *document* dates (class B)
``dplace-places.py``          hardcoded 2025, plus the survey year as a point lifespan
``ottgaz-places.py``          ``in 1300``/``in 1922`` for every *undated* unit
``geonames-toponyms.py``      ``end.in`` with no closure when only ``to`` is given
============================  =====================================================
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from processing.gazetteer_temporal_extent import doc_temporal_bounds

_AUTHORITIES = Path(__file__).resolve().parent.parent / "authorities"


def _load(module_name: str, relative_path: str):
    """Load a hyphenated authority module by path."""
    spec = importlib.util.spec_from_file_location(
        module_name, str(_AUTHORITIES / relative_path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(timespans) -> dict:
    return {"geometries": [{"timespans": list(timespans)}]}


class OttgazUndatedUnitTests(unittest.TestCase):
    """`og` — the empire span is a bound on an unknown date, not a lifespan."""

    @classmethod
    def setUpClass(cls):
        cls.og = _load("ottgaz_places", "ottgaz-places.py")

    def test_undated_unit_is_not_claimed_to_have_lived_622_years(self):
        ts = self.og.unit_timespans(None, None)
        self.assertEqual(
            ts, [{"start": {"earliest": 1300}, "end": {"latest": 1922}}]
        )
        se, sl, ee, el = doc_temporal_bounds(_doc(ts), "og")
        self.assertEqual((se, el), (1300, 1922), "possibly alive across the empire")
        self.assertIsNone(sl, "but definitely alive at no year")
        self.assertIsNone(ee)

    def test_fully_dated_unit_is_a_real_lifespan(self):
        self.assertEqual(
            self.og.unit_timespans(1867, 1922),
            [{"start": {"in": 1867}, "end": {"in": 1922}}],
        )

    def test_known_start_cannot_outlive_the_empire(self):
        self.assertEqual(
            self.og.unit_timespans(1867, None),
            [{"start": {"in": 1867}, "end": {"latest": 1922}}],
        )

    def test_known_end_gets_closure_and_cannot_predate_the_empire(self):
        ts = self.og.unit_timespans(None, 1864)
        self.assertEqual(
            ts,
            [{"start": {"latest": 1864, "earliest": 1300}, "end": {"in": 1864}}],
        )
        _, sl, ee, _ = doc_temporal_bounds(_doc(ts), "og")
        self.assertTrue(sl <= 1864 <= ee, "closure makes 1864 definitely alive")


class TrismegistosAttestationTests(unittest.TestCase):
    """`tm` — TM dates bound the *documents*, not the place's existence."""

    def test_document_range_is_an_attestation_window(self):
        from processing.temporal import attested_window

        ts = attested_window(246, 249)
        self.assertEqual(
            ts, [{"start": {"latest": 249}, "end": {"earliest": 246}}]
        )
        se, sl, ee, el = doc_temporal_bounds(_doc(ts), "tm")
        self.assertFalse(sl <= 247 <= ee, "a 4-year window names no definite year")
        self.assertIsNone(se, "and claims no outer bound in either direction")
        self.assertIsNone(el)

    def test_bce_dates_survive(self):
        from processing.temporal import attested_window

        self.assertEqual(
            attested_window(-330, -320),
            [{"start": {"latest": -320}, "end": {"earliest": -330}}],
        )


class GeonamesToponymClosureTests(unittest.TestCase):
    """`gn` — an alternate name with only a `to` needs the closure rule."""

    def test_to_only_gets_a_start_upper_bound(self):
        from processing.temporal import lifespan

        ts = lifespan(None, 1974)
        self.assertEqual(ts, [{"end": {"in": 1974}, "start": {"latest": 1974}}])
        _, sl, ee, _ = doc_temporal_bounds(_doc(ts), "gn")
        self.assertTrue(
            sl <= 1974 <= ee,
            "without closure this name is definitely alive at NO year",
        )

    def test_from_and_to_stay_a_lifespan(self):
        from processing.temporal import lifespan

        self.assertEqual(
            lifespan(1889, 1974),
            [{"start": {"in": 1889}, "end": {"in": 1974}}],
        )


class SnapshotSourcesDeriveTheirYearTests(unittest.TestCase):
    """`wd` geoshapes and `dp` — derived, never hardcoded.

    A literal year is wrong twice over: it is the attestation-as-lifespan
    defect, and it goes stale the moment a newer dump lands. Both modules now
    read the year off the release on disk.
    """

    def test_dplace_release_timespans_are_an_attestation(self):
        dp = _load("dplace_places", "dplace-places.py")
        self.assertEqual(len(dp.RELEASE_TIMESPANS), 1)
        ts = dp.RELEASE_TIMESPANS[0]
        self.assertIn("latest", ts["start"])
        self.assertIn("earliest", ts["end"])
        self.assertNotIn("in", ts["start"])
        self.assertEqual(ts["start"]["latest"], ts["end"]["earliest"])

    def test_no_authority_script_hardcodes_a_point_lifespan_year(self):
        """No authority *writes* ``{"start": {"in": <literal>}, "end": ...}``.

        Matched on the AST, not the text, so the several docstrings that quote
        the old encoding in order to explain the fix don't trip it.
        """
        import ast

        def _endpoint_literal_year(node):
            """The literal year in ``{"in": 1234}``, else None."""
            if not isinstance(node, ast.Dict):
                return None
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "in"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, int)
                ):
                    return value.value
            return None

        offenders = []
        for path in sorted(_AUTHORITIES.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                endpoints = {}
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value in ("start", "end"):
                        endpoints[key.value] = _endpoint_literal_year(value)
                if endpoints.get("start") is not None and endpoints["start"] == endpoints.get("end"):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], f"hardcoded snapshot lifespan in {offenders}")


if __name__ == "__main__":
    unittest.main()
