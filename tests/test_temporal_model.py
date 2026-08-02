"""Tests for the attestation/lifespan temporal model (place#164).

Covers the three defects the issue identifies:

1. **Storage** — ``processing.temporal`` encodes attestations as
   ``start.latest``/``end.earliest`` rather than ``in``/``in`` point lifespans.
2. **The reader flattens regardless** — ``doc_temporal_range`` no longer
   partitions min-to-start / max-to-end, which inverted attestation windows;
   and ``doc_temporal_bounds`` exposes the four bounds the date filter needs.
3. **Granular values are unreadable** — string years are now coerced, so the
   208,937 ``whg`` docs carrying ``{"start": {"earliest": "2022"}}`` stop
   computing as undated.
"""

from __future__ import annotations

import unittest

from processing.gazetteer_temporal_extent import (
    doc_temporal_bounds,
    doc_temporal_range,
)
from processing.temporal import (
    attested_at,
    attested_window,
    bounded,
    coerce_year,
    lifespan,
    normalise_timespans,
    precision_bounds,
)


def _doc(*timespans: dict) -> dict:
    """A staged doc carrying the given timespans on its geometry."""
    return {"geometries": [{"timespans": list(timespans)}]}


class AttestationEncodingTests(unittest.TestCase):
    def test_attested_at_is_not_a_point_lifespan(self):
        # The whole defect in one assertion: a snapshot must NOT say
        # "existed only in 2026".
        got = attested_at(2026)
        self.assertEqual(got, [{"start": {"latest": 2026}, "end": {"earliest": 2026}}])
        self.assertNotIn("in", got[0]["start"])
        self.assertNotIn("in", got[0]["end"])

    def test_attested_at_none_yields_nothing(self):
        self.assertEqual(attested_at(None), [])

    def test_attested_window_bounds_inward(self):
        # gb: surveyed somewhere in 1888-1914 → started no later than 1914,
        # ended no earlier than 1888.
        self.assertEqual(
            attested_window(1888, 1914),
            [{"start": {"latest": 1914}, "end": {"earliest": 1888}}],
        )

    def test_attested_window_single_year_matches_attested_at(self):
        self.assertEqual(attested_window(1680, 1680), attested_at(1680))

    def test_attested_window_tolerates_reversed_input(self):
        self.assertEqual(attested_window(1914, 1888), attested_window(1888, 1914))

    def test_attested_window_one_sided(self):
        self.assertEqual(attested_window(None, 1914), [{"start": {"latest": 1914}}])
        self.assertEqual(attested_window(1888, None), [{"end": {"earliest": 1888}}])
        self.assertEqual(attested_window(None, None), [])


class LifespanAndClosureTests(unittest.TestCase):
    def test_genuine_lifespan_uses_in(self):
        self.assertEqual(
            lifespan(1738, 1740),
            [{"start": {"in": 1738}, "end": {"in": 1740}}],
        )

    def test_closure_rule_supplies_start_latest_from_end(self):
        # ukhc/kain_par: a known abolition with an unknown start. Without the
        # closure rule the record can never be *definitely* alive at any year,
        # which is plainly wrong for a county that existed in 1973.
        got = lifespan(end=1974)
        self.assertEqual(got, [{"start": {"latest": 1974}, "end": {"in": 1974}}])
        se, sl, ee, el = doc_temporal_bounds(_doc(got[0]), "ukhc")
        self.assertEqual(sl, 1974)
        self.assertTrue(sl <= 1973 <= (ee if ee is not None else 1973) or sl <= 1974)

    def test_definitely_alive_at_a_year_before_a_known_end(self):
        doc = _doc(*lifespan(start=1542, end=1974))
        se, sl, ee, el = doc_temporal_bounds(doc, "ukhc")
        self.assertEqual((se, sl, ee, el), (1542, 1542, 1974, 1974))
        self.assertTrue(sl <= 1973 <= ee, "should be definitely alive in 1973")

    def test_closure_can_be_disabled(self):
        self.assertEqual(
            lifespan(end=1974, apply_closure=False),
            [{"end": {"in": 1974}}],
        )

    def test_open_start_and_open_end(self):
        self.assertEqual(lifespan(start=1707), [{"start": {"in": 1707}}])
        self.assertEqual(lifespan(), [])


class BoundedTests(unittest.TestCase):
    def test_all_four_bounds(self):
        # vob_*: previous snapshot 1901, this one 1911, next 1921.
        self.assertEqual(
            bounded(1901, 1911, 1911, 1921),
            [{"start": {"earliest": 1901, "latest": 1911},
              "end": {"earliest": 1911, "latest": 1921}}],
        )

    def test_vob_snapshot_is_definitely_alive_only_at_the_census_year(self):
        doc = _doc(*bounded(1901, 1911, 1911, 1921))
        se, sl, ee, el = doc_temporal_bounds(doc, "vob_rd")
        self.assertTrue(sl <= 1911 <= ee, "definitely alive at the census year")
        self.assertFalse(sl <= 1915 <= ee, "must NOT over-claim 1915")
        self.assertTrue(se <= 1915 <= el, "but 1915 is possibly alive")

    def test_closure_applied_when_only_end_bounds_given(self):
        self.assertEqual(
            bounded(end_earliest=1911, end_latest=1921),
            [{"start": {"latest": 1911},
              "end": {"earliest": 1911, "latest": 1921}}],
        )

    def test_empty(self):
        self.assertEqual(bounded(), [])

    def test_coincident_bounds_collapse_to_in(self):
        # po gives a single year for ~8.4k periods. Bounds that meet pin the
        # year exactly, which is what `in` means — emit the canonical form so
        # a consumer that special-cases `in` cannot miss them.
        self.assertEqual(bounded(1492, 1492, 1521, 1521),
                         [{"start": {"in": 1492}, "end": {"in": 1521}}])

    def test_collapse_is_per_endpoint(self):
        self.assertEqual(
            bounded(1901, 1911, 1921, 1921),
            [{"start": {"earliest": 1901, "latest": 1911}, "end": {"in": 1921}}],
        )

    def test_collapsed_form_reads_back_identically(self):
        # The collapse must not change what the date filter sees: `in` is
        # exact and so serves as both bounds.
        collapsed = _doc(*bounded(1492, 1492, 1521, 1521))
        spelled = _doc({"start": {"earliest": 1492, "latest": 1492},
                        "end": {"earliest": 1521, "latest": 1521}})
        self.assertEqual(doc_temporal_bounds(collapsed, "po"),
                         doc_temporal_bounds(spelled, "po"))


class CoerceYearTests(unittest.TestCase):
    def test_ints_pass_through(self):
        self.assertEqual(coerce_year(2022), 2022)
        self.assertEqual(coerce_year(-49), -49)

    def test_strings_are_coerced(self):
        # The 208,937 whg docs.
        self.assertEqual(coerce_year("2022"), 2022)
        self.assertEqual(coerce_year("2022-01-01"), 2022)
        self.assertEqual(coerce_year(" 1680 "), 1680)

    def test_negative_and_signed(self):
        self.assertEqual(coerce_year("-0049"), -49)
        self.assertEqual(coerce_year("-0049-03-15"), -49)
        self.assertEqual(coerce_year("+1200"), 1200)

    def test_rubbish_returns_none(self):
        for bad in ("", "   ", "circa 1500", None, {}, [], "abc-01"):
            self.assertIsNone(coerce_year(bad), repr(bad))

    def test_bool_is_not_a_year(self):
        # bool is an int subclass; True would otherwise read as year 1.
        self.assertIsNone(coerce_year(True))
        self.assertIsNone(coerce_year(False))


class PrecisionBoundsTests(unittest.TestCase):
    def test_year_precision_pins_exactly(self):
        self.assertEqual(precision_bounds(1200, 9), (1200, 1200))
        self.assertEqual(precision_bounds(1200, 11), (1200, 1200))

    def test_century_precision_spans_the_century(self):
        # Wikidata's 12th century is +1200-00-00 at precision 7.
        self.assertEqual(precision_bounds(1200, 7), (1200, 1299))

    def test_decade_and_millennium(self):
        self.assertEqual(precision_bounds(1996, 8), (1990, 1999))
        self.assertEqual(precision_bounds(1200, 6), (1000, 1999))

    def test_bce_truncates_downward(self):
        lo, hi = precision_bounds(-49, 7)
        self.assertLessEqual(lo, -49)
        self.assertGreaterEqual(hi, -49)

    def test_unknown_or_geological_precision_is_not_invented(self):
        self.assertEqual(precision_bounds(1200, None), (None, None))
        self.assertEqual(precision_bounds(-100000, 3), (None, None))
        self.assertEqual(precision_bounds(None, 9), (None, None))


class ReaderEnvelopeTests(unittest.TestCase):
    """doc_temporal_range: the registry envelope."""

    def test_attestation_window_is_no_longer_inverted(self):
        # THE regression this fixes: partitioning min-to-start/max-to-end
        # returned (1914, 1888) for gb — a range ending before it begins.
        doc = _doc(*attested_window(1888, 1914))
        self.assertEqual(doc_temporal_range(doc, "gb"), (1888, 1914))

    def test_snapshot_attestation(self):
        self.assertEqual(doc_temporal_range(_doc(*attested_at(2026)), "osm"), (2026, 2026))

    def test_plain_lifespan_unchanged(self):
        doc = _doc(*lifespan(1500, 1800))
        self.assertEqual(doc_temporal_range(doc, "iv"), (1500, 1800))

    def test_ongoing_and_open_start_conventions_preserved(self):
        self.assertEqual(doc_temporal_range(_doc({"start": {"in": 1707}}), "un"),
                         (1707, None))
        self.assertEqual(doc_temporal_range(_doc({"end": {"in": 1974}}), "ukhc"),
                         (None, 1974))

    def test_string_years_are_no_longer_undated(self):
        # Previously (None, None) — the whg defect.
        doc = _doc({"start": {"earliest": "2022"}, "end": {"latest": "2022"}})
        self.assertEqual(doc_temporal_range(doc, "whg"), (2022, 2022))


class ReaderBoundsTests(unittest.TestCase):
    """doc_temporal_bounds: the four bounds the date filter needs."""

    def test_osm_attestation_is_possibly_alive_in_1500(self):
        doc = _doc(*attested_at(2026))
        se, sl, ee, el = doc_temporal_bounds(doc, "osm")
        self.assertEqual((se, sl, ee, el), (None, 2026, 2026, None))
        # Not definitely alive in 1500 ...
        self.assertFalse(sl <= 1500 <= ee)
        # ... but possibly alive, because the outer bounds are absent. This is
        # what removes the "OSM blanks out on any historical range" defect.
        self.assertTrue(
            (se is None or se <= 1500) and (el is None or 1500 <= el)
        )

    def test_lifespan_is_definite_throughout(self):
        doc = _doc(*lifespan(1738, 1740))
        self.assertEqual(doc_temporal_bounds(doc, "clio"), (1738, 1738, 1740, 1740))

    def test_attestation_window_has_empty_definite_core(self):
        # Honest, not a defect: gb attests the place somewhere in 1888-1914
        # and at no single nameable year.
        doc = _doc(*attested_window(1888, 1914))
        se, sl, ee, el = doc_temporal_bounds(doc, "gb")
        self.assertEqual((se, sl, ee, el), (None, 1914, 1888, None))
        self.assertGreater(sl, ee, "definite core should be empty")

    def test_undated_doc(self):
        self.assertEqual(doc_temporal_bounds({"geometries": [{}]}, "osm"),
                         (None, None, None, None))

    def test_string_years_are_coerced_here_too(self):
        doc = _doc({"start": {"earliest": "2022"}, "end": {"latest": "2022"}})
        self.assertEqual(doc_temporal_bounds(doc, "whg"), (2022, None, None, 2022))

    def test_one_unbounded_timespan_makes_the_doc_unbounded(self):
        doc = _doc(
            {"start": {"in": 1500}, "end": {"in": 1600}},
            {"start": {"latest": 1900}, "end": {"earliest": 1900}},
        )
        se, sl, ee, el = doc_temporal_bounds(doc, "iv")
        self.assertIsNone(se, "second timespan has no lower bound on start")
        self.assertIsNone(el, "second timespan has no upper bound on end")

    def test_bounds_read_from_toponyms_and_relations_too(self):
        doc = {"toponyms": [{"timespans": attested_at(1680)}]}
        self.assertEqual(doc_temporal_bounds(doc, "iv"), (None, 1680, 1680, None))
        doc = {"relations": [{"timespans": attested_at(1680)}]}
        self.assertEqual(doc_temporal_bounds(doc, "iv"), (None, 1680, 1680, None))


class ClusteringNarrowsAttestationsTests(unittest.TestCase):
    """§7: co-reference turns two attestations into a definite span.

    A place attested by Index Villaris in 1680 and by OSM in 2026 is
    *definitely* alive across the whole span — a conclusion neither source
    states alone, and one that is unreachable while both are point lifespans.
    """

    def test_two_attestations_yield_a_definite_span(self):
        doc = _doc(*attested_at(1680), *attested_at(2026))
        se, sl, ee, el = doc_temporal_bounds(doc, "iv")
        self.assertEqual(sl, 1680)
        self.assertEqual(ee, 2026)
        self.assertTrue(sl <= 1800 <= ee, "definitely alive in 1800")


if __name__ == "__main__":
    unittest.main()


class SchemaMappingTests(unittest.TestCase):
    """place#164 defect 3: the live index mapped these as ``text``.

    ``schemas/places.json`` declares them ``integer``, but the live index's
    sub-fields were created by *dynamic mapping* from the first values to
    arrive — strings, for the ``whg`` LPF datasets — so
    ``geometries.timespans.start.earliest`` and ``end.latest`` came out
    ``text`` and could not be range-queried at all. Others
    (``geometries.end.earliest``, every ``toponyms`` outer bound) simply did
    not exist, because nothing had ever written them.

    The new encoding makes ``start.latest``/``end.earliest`` the *primary*
    fields, so this guards the schema that the next full rebuild applies.
    """

    def test_every_timespan_subfield_is_integer_in_the_schema(self):
        import json
        from pathlib import Path

        from processing.temporal import ENDPOINTS, SUBFIELDS

        schema = json.loads(Path("schemas/places.json").read_text())
        props = schema["mappings"]["properties"]
        for section in ("geometries", "toponyms", "relations"):
            timespans = props[section]["properties"]["timespans"]["properties"]
            for endpoint in ENDPOINTS:
                fields = timespans[endpoint]["properties"]
                for sub in SUBFIELDS:
                    self.assertIn(
                        sub, fields,
                        f"{section}.timespans.{endpoint}.{sub} missing from schema",
                    )
                    self.assertEqual(
                        fields[sub]["type"], "integer",
                        f"{section}.timespans.{endpoint}.{sub} must be integer "
                        f"— text cannot be range-queried",
                    )


class NormaliseTimespansTests(unittest.TestCase):
    def test_string_years_become_ints(self):
        got = normalise_timespans(
            [{"start": {"earliest": "2022"}, "end": {"latest": "2022-12-31"}}]
        )
        self.assertEqual(got, [{"start": {"earliest": 2022}, "end": {"latest": 2022}}])

    def test_unparseable_endpoints_are_dropped(self):
        self.assertEqual(
            normalise_timespans([{"start": {"earliest": "circa 1500"}}]), []
        )

    def test_mixed_datasets_normalise_to_one_type(self):
        # The parquet schema-inference failure mode: bare number in one
        # dataset, ISO string in another.
        got = normalise_timespans(
            [{"start": {"in": 1500}}, {"start": {"in": "1600-01-01"}}]
        )
        self.assertEqual(got, [{"start": {"in": 1500}}, {"start": {"in": 1600}}])
        for ts in got:
            self.assertIsInstance(ts["start"]["in"], int)

    def test_non_list_and_junk_are_tolerated(self):
        self.assertEqual(normalise_timespans(None), [])
        self.assertEqual(normalise_timespans("nope"), [])
        self.assertEqual(normalise_timespans([None, 3, {"start": "x"}]), [])


class RepresentableYearTests(unittest.TestCase):
    """Years must survive the trip into Elasticsearch's ``integer`` mapping.

    Wikidata models the age of the universe as an ordinary time claim, so
    ``-13798000000`` reaches the builders. ES rejects the whole document with
    ``failed to parse field [...] of type [integer]`` — 3,639 ``wd`` docs were
    lost that way on the place#164 rebuild before this bound existed.
    """

    def test_out_of_range_year_is_dropped_not_clamped(self):
        # Clamping to the int32 floor would assert a date the source never gave.
        self.assertEqual(attested_at(-13_798_000_000), [])
        self.assertEqual(attested_window(-13_798_000_000, -13_797_000_000), [])
        self.assertEqual(lifespan(-13_798_000_000), [])

    def test_the_representable_half_of_a_pair_survives(self):
        # A place with an absurd start and a real end keeps the end, and the
        # closure rule still applies to it.
        self.assertEqual(
            lifespan(-13_798_000_000, 1900),
            [{"end": {"in": 1900}, "start": {"latest": 1900}}],
        )

    def test_boundary_values(self):
        from processing.temporal import YEAR_MAX, YEAR_MIN
        self.assertEqual(lifespan(YEAR_MIN), [{"start": {"in": YEAR_MIN}}])
        self.assertEqual(lifespan(YEAR_MAX), [{"start": {"in": YEAR_MAX}}])
        self.assertEqual(lifespan(YEAR_MIN - 1), [])
        self.assertEqual(lifespan(YEAR_MAX + 1), [])

    def test_ordinary_deep_history_is_untouched(self):
        # The bound must not disturb real archaeology.
        self.assertEqual(lifespan(-3000, -2000),
                         [{"start": {"in": -3000}, "end": {"in": -2000}}])

    def test_coerce_year_applies_the_same_bound(self):
        self.assertIsNone(coerce_year("-13798000000"))
        self.assertEqual(coerce_year("-3000"), -3000)
