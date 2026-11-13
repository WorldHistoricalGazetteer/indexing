"""/api/genealogy/place: parse a service export string, resolve its context, climb the ladder."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

try:
    from gateway import genealogy as g
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


class H:
    """A candidate shaped like CandidateHit for the fields the resolver reads."""
    def __init__(self, pid, title, ccodes, conf=None, area=False, types=()):
        self.place_id, self.title, self.ccodes, self.confidence = pid, title, ccodes, conf
        self.geometries = [SimpleNamespace(is_area=area)]
        self.types = [{"identifier": t} for t in types]

    def model_dump(self):
        return {"place_id": self.place_id, "title": self.title, "ccodes": self.ccodes, "confidence": self.confidence}


class FakeIndex:
    """Just enough of the gazetteer for the scenarios below.

    Antrim is in Northern Ireland (GB) and has an areal OSM relation plus a point GeoNames unit; Pest has
    ONLY a GeoNames point; "Ballymena" exists in GB (real) and IE (a misspelt look-alike)."""
    def __init__(self):
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        q, mode, cc = kw["query"], kw["mode"], kw.get("ccodes")
        ql = q.lower()
        gn_admin = kw.get("fclasses") == ["A"] and kw.get("namespaces") == ["gn"]
        if kw.get("fclasses") == ["A"] and not kw.get("namespaces") and ql == "atlantis":
            return SimpleNamespace(hits=[H("gn:atl", "Atlantis", ["AT"], area=True, types=["A.PCLI"])])
        if mode in ("exactci", "lexfuzzy") and q != "Ballymena" and q != "Brussels" and q != "Newport" and q != "Budapest":
            if gn_admin:
                if ql == "pest":
                    return SimpleNamespace(hits=[H("gn:pest", "Pest", ["HU"], area=False)])
                return SimpleNamespace(hits=[])
            if ql == "county antrim" and cc in (None, ["GB"]):
                return SimpleNamespace(hits=[H("osm:r1", "County Antrim", ["GB"], area=True)])
            if ql == "antrim" and cc in (None, ["GB"]):
                return SimpleNamespace(hits=[H("ukhc:ANM", "Antrim", ["GB"], area=True)])
            return SimpleNamespace(hits=[])
        if q == "boom":
            raise RuntimeError("index fell over")
        if q == "Ballymena":
            if kw.get("contained_in") in (["osm:r1"], ["ukhc:ANM"]):
                return SimpleNamespace(hits=[H("gn:bm", "Ballymena", ["GB"], conf=100)])
            if cc == ["IE"]:
                return SimpleNamespace(hits=[H("osm:bana", "Ballymana", ["IE"], conf=38.5)])
            if cc == ["GB"]:
                return SimpleNamespace(hits=[H("gn:bm", "Ballymena", ["GB"], conf=100)])
            return SimpleNamespace(hits=[H("gn:bm", "Ballymena", ["GB"], conf=100), H("gn:bm2", "Ballymena", ["US"], conf=95)])
        if q == "Brussels":
            if cc == ["DE"]:
                return SimpleNamespace(hits=[H("gn:bruchsal", "Bruchsal", ["DE"], conf=25)])   # below every floor
            return SimpleNamespace(hits=[H("gn:brussels", "Brussels", ["BE"], conf=100)])
        if q == "Newport":
            return SimpleNamespace(hits=[H("gn:np1", "Newport", ["GB"], conf=100), H("gn:np2", "Newport", ["US"], conf=100)])
        if q == "Budapest":
            return SimpleNamespace(hits=[H("gn:bud", "Budapest", ["HU"], conf=100)])
        return SimpleNamespace(hits=[])


class Base(unittest.TestCase):
    def setUp(self):
        self.idx = FakeIndex()
        g._resolved._data.clear()
        p = mock.patch.object(g, "_lookup", self.idx)
        p.start()
        self.addCleanup(p.stop)

    def run_req(self, **kw):
        kw.setdefault("service", "gedcom")
        return asyncio.run(g.place(g.GenealogyRequest(**kw)))

    def one(self, **kw):
        return self.run_req(**kw)["results"][0]


class Resolution(Base):
    def test_full_hierarchy_resolves_and_the_most_specific_rung_answers(self):
        r = self.one(place="Ballymena, County Antrim, Northern Ireland")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["context"]["containers"][0]["place_id"], "osm:r1")
        self.assertIs(r["context"]["containers"][0]["areal"], True)
        self.assertEqual((r["scope"]["rung"], r["scope"]["relaxed"]), ("container", False))
        self.assertEqual(r["candidates"][0]["place_id"], "gn:bm")

    def test_a_wrong_country_with_a_good_county_still_finds_the_place_inside_the_county(self):
        # "Ireland" is wrong for Antrim (Northern Ireland). The county resolves outside the stated
        # country, the container rung uses it alone, and the answer is flagged as outside the country.
        r = self.one(place="Ballymena, County Antrim, Ireland")
        self.assertEqual(r["context"]["country"]["code"], "IE")
        cont = r["context"]["containers"][0]
        self.assertEqual(cont["place_id"], "osm:r1")
        self.assertTrue(cont["outside_stated_country"])
        self.assertEqual((r["scope"]["rung"], r["scope"]["relaxed"]), ("container", False))
        self.assertEqual(r["candidates"][0]["place_id"], "gn:bm")
        self.assertIs(r["candidates"][0]["country_matches"], False)

    def test_a_point_only_admin_unit_is_accepted_as_a_container_and_flagged(self):
        r = self.one(place="Budapest, Pest, Hungary")
        cont = r["context"]["containers"][0]
        self.assertEqual((cont["place_id"], cont["areal"]), ("gn:pest", False))

    def test_a_loose_match_in_the_stated_country_does_not_beat_an_exact_match_outside_it(self):
        # no container resolvable (Nowhere), country IE: the country rung finds only a 38.5 look-alike,
        # the unscoped rung finds the real thing at 100, and that one wins.
        r = self.one(place="Ballymena, County Nowhere, Ireland")
        self.assertEqual([a["rung"] for a in r["scope"]["attempts"]], ["country", "none"])
        self.assertEqual(r["candidates"][0]["place_id"], "gn:bm")
        self.assertEqual((r["scope"]["rung"], r["scope"]["relaxed"]), ("none", True))

    def test_a_wrong_country_is_relaxed_away_and_said_so(self):
        r = self.one(place="Brussels, Germany")
        self.assertEqual(r["status"], "ok")
        self.assertEqual([a["rung"] for a in r["scope"]["attempts"]], ["country", "none"])
        self.assertFalse(r["scope"]["attempts"][0]["accepted"], "25 < the confidence floor")
        self.assertEqual((r["scope"]["rung"], r["scope"]["relaxed"]), ("none", True))
        c = r["candidates"][0]
        self.assertEqual(c["place_id"], "gn:brussels")
        self.assertIs(c["country_matches"], False, "the answer is not in the country the user wrote")

    def test_a_very_weak_match_is_flagged_not_promoted(self):
        r = self.one(place="Brussels, Germany", relax=False)
        self.assertEqual(r["status"], "low_confidence")

    def test_relax_false_stops_at_the_first_rung(self):
        r = self.one(place="Brussels, Germany", relax=False)
        self.assertEqual([a["rung"] for a in r["scope"]["attempts"]], ["country"])
        self.assertEqual(r["candidates"][0]["place_id"], "gn:bruchsal")

    def test_a_bare_name_is_searched_unscoped_and_says_why(self):
        r = self.one(place="Newport")
        self.assertEqual([a["rung"] for a in r["scope"]["attempts"]], ["none"])
        self.assertIn("cannot be placed", r["scope"]["note"])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["candidates"]), 2, "both Newports offered, nothing pretended")

    def test_country_known_to_the_index_but_not_the_alias_table(self):
        r = self.one(place="Foo, Atlantis")
        self.assertEqual(r["context"]["country"]["source"], "index")
        self.assertEqual(r["context"]["country"]["code"], "AT")

    def test_country_hint_applies_when_the_string_has_none(self):
        r = self.one(place="Ballymena, County Antrim", country_hint="gb")
        self.assertEqual((r["context"]["country"]["code"], r["context"]["country"]["source"]), ("GB", "hint"))
        self.assertEqual(r["context"]["containers"][0]["place_id"], "osm:r1")
        self.assertEqual(r["scope"]["rung"], "container")

    def test_large_to_small_order_is_honoured(self):
        r = self.one(place="Hungary, Pest, Budapest", order="large_to_small")
        self.assertEqual(r["parsed"]["order_applied"], "large_to_small")
        self.assertEqual(r["context"]["country"]["code"], "HU")
        self.assertEqual(r["candidates"][0]["place_id"], "gn:bud")

    def test_parse_only_skips_the_locality_search(self):
        n = len(self.idx.calls)
        r = self.one(place="Ballymena, County Antrim, Ireland", parse_only=True)
        self.assertEqual((r["status"], r["candidates"]), ("parsed", []))
        self.assertFalse([c for c in self.idx.calls[n:] if c["query"] == "Ballymena"])

    def test_repeated_context_is_resolved_once_across_a_batch(self):
        self.run_req(places=["Ballymena, County Antrim, Ireland"] * 5)
        # one RESOLUTION of "County Antrim" = the unscoped areal lookup that finally succeeds; five
        # concurrent places must share it rather than each repeating it
        resolutions = [c for c in self.idx.calls if c["query"].lower() == "county antrim"
                       and c["mode"] == "exactci" and not c.get("ccodes") and not c.get("fclasses")]
        self.assertEqual(len(resolutions), 1, "the container is resolved once for the whole batch")

    def test_an_unresolvable_admin_level_is_reported_and_the_ladder_continues(self):
        r = self.one(place="Ballymena, County Nowhere, Ireland")
        self.assertEqual(r["context"]["unresolved"], ["County Nowhere"])
        self.assertIn(r["scope"]["rung"], ("country", "none"))

    def test_one_failing_string_does_not_sink_the_batch(self):
        out = self.run_req(places=["Brussels, Belgium", "boom, Ireland"])["results"]
        self.assertEqual(out[0]["status"], "ok")
        self.assertEqual(out[1]["status"], "error")


class Validation(Base):
    def test_unknown_service_is_a_422_that_lists_the_known_ones(self):
        with self.assertRaises(HTTPException) as cm:
            self.run_req(service="nope", place="x")
        self.assertEqual(cm.exception.status_code, 422)
        self.assertIn("generic", cm.exception.detail)

    def test_batch_size_is_bounded(self):
        with self.assertRaises(HTTPException) as cm:
            self.run_req(places=["a"] * (g.MAX_PLACES + 1))
        self.assertEqual(cm.exception.status_code, 422)

    def test_a_request_must_carry_a_place(self):
        with self.assertRaises(HTTPException):
            self.run_req()

    def test_familysearch_mode_is_refused_here(self):
        with self.assertRaises(HTTPException):
            self.run_req(place="x", mode="familysearch")

    def test_services_listing_states_what_is_documented_and_what_is_assumed(self):
        out = asyncio.run(g.services())["services"]
        basis = {s["name"]: s["basis"] for s in out}
        self.assertEqual(basis["gedcom"], "gedcom-spec")
        self.assertEqual(basis["ancestry"], "assumed")


if __name__ == "__main__":
    unittest.main()
