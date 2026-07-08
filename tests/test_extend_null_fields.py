"""Regression tests for ``gateway.extend`` resilience to present-but-null
fields (place#114).

Some records — notably PeriodO (``po``) period records — carry ES fields
explicitly set to ``null`` (e.g. ``geometries: null``, ``ccodes: null``).
``dict.get(field, [])`` returns the default only when the key is *absent*; a
key present with a ``null`` value returns ``None``, which then blew up any
extractor that iterated it (``for obj in None`` → ``TypeError`` → HTTP 500).

A single 500 blanked the whole ``/api/extend`` batch, so one such record
silently emptied unrelated ids sharing its batch. These tests pin the
per-field null handling and the per-property degrade-to-empty behaviour.
"""

from __future__ import annotations

import unittest

from gateway.extend import _extract_property, _extract_timespans, _wrap_value


# The real production record ``po:p0b6j5mgvqt`` ("Hellenistic") that triggered
# the 500: geometries / ccodes / descriptions are explicitly null.
_NULL_FIELD_RECORD = {
    "place_id": "po:p0b6j5mgvqt",
    "title": "Hellenistic",
    "toponyms": [
        {
            "toponym_id": "Hellenistic@en",
            "timespans": [{"start": {"in": -349}, "end": {"in": 70}}],
            "label": "Hellenistic",
        }
    ],
    "types": [
        {"identifier": "period", "sourceLabel": "temporal-period", "label": "periodo"}
    ],
    "descriptions": None,
    "ccodes": None,
    "geometries": None,
    "relations": [
        {
            "relation_type": "partOf",
            "label": "Chersonesos South Region Periodization",
            "related_place_id": "po:authority:p0b6j5m",
        }
    ],
}


class TestExtendNullFields(unittest.TestCase):
    def test_timespans_survive_null_geometries(self):
        """geometries: null must not crash timespan extraction."""
        spans = _extract_timespans(_NULL_FIELD_RECORD)
        self.assertEqual(spans, [{"begin": -349, "end": 70}])

    def test_temporal_objects_property(self):
        val = _extract_property("whg:temporal_objects", _NULL_FIELD_RECORD, [])
        self.assertEqual(_wrap_value(val), [{"str": '{"begin": -349, "end": 70}'}])

    def test_temporal_years_property(self):
        val = _extract_property("whg:temporal_years", _NULL_FIELD_RECORD, [])
        self.assertEqual(_wrap_value(val), [{"str": "-349-70"}])

    def test_null_ccodes_and_classes_yield_empty(self):
        for prop in ("whg:countries_codes", "whg:countries_objects",
                     "whg:classes_codes", "whg:classes_objects"):
            with self.subTest(prop=prop):
                val = _extract_property(prop, _NULL_FIELD_RECORD, [])
                self.assertEqual(_wrap_value(val), [])

    def test_no_extractor_raises_on_null_fields(self):
        """Every known property extractor must be null-safe."""
        for prop in (
            "whg:id_short", "whg:id_object",
            "whg:names_canonical", "whg:names_array", "whg:names_summary",
            "whg:geometry_geojson", "whg:geometry_wkt",
            "whg:geometry_centroid", "whg:geometry_bbox",
            "whg:countries_codes", "whg:countries_objects",
            "whg:types_objects", "whg:classes_codes", "whg:classes_objects",
            "whg:temporal_objects", "whg:temporal_years",
        ):
            with self.subTest(prop=prop):
                _wrap_value(_extract_property(prop, _NULL_FIELD_RECORD, []))


if __name__ == "__main__":
    unittest.main()
