"""`mode: familysearch`: FamilySearch Read Places `q` syntax -> gateway parameters, lexical only."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException

from gateway import familysearch as fs

try:
    from gateway import reconcile as rc
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")

from tests._es_stub import FakeES


class Tokenize(unittest.TestCase):
    def test_pairs_and_operators(self):
        p = fs.tokenize('name:Paris +date:1800/1900 -parentId:32 +parentId:1~')
        self.assertEqual([(x.key, x.value, x.required, x.excluded, x.fuzzy) for x in p], [
            ("name", "Paris", False, False, False), ("date", "1800/1900", True, False, False),
            ("parentid", "32", False, True, False), ("parentid", "1", True, False, True)])

    def test_quoted_values_keep_spaces_and_commas(self):
        (p,) = fs.tokenize('name:"New York, New York"')
        self.assertEqual((p.value, p.quoted), ("New York, New York", True))

    def test_fuzzy_suffix_on_quoted_and_bare(self):
        self.assertTrue(fs.tokenize("name:Baden~")[0].fuzzy)
        self.assertEqual(fs.tokenize("name:Baden~")[0].value, "Baden")
        self.assertTrue(fs.tokenize('name:"Baden"~')[0].fuzzy)

    def test_escaped_tilde_is_literal(self):
        p = fs.tokenize(r"name:Ba\~")[0]
        self.assertFalse(p.fuzzy)

    def test_malformed_q_is_rejected(self):
        for bad in ("Paris", 'name:"unterminated', "name:ok garbage"):
            with self.assertRaises(ValueError, msg=bad):
                fs.tokenize(bad)


class Helpers(unittest.TestCase):
    def test_wildcards_and_escapes(self):
        self.assertTrue(fs.has_wildcard("par*"))
        self.assertFalse(fs.has_wildcard(r"par\*"))
        self.assertEqual(fs.unescape(r"a\*b"), "a*b")

    def test_hierarchy_splits_on_unescaped_commas_only(self):
        self.assertEqual(fs.split_hierarchy("par*,france"), ["par*", "france"])
        self.assertEqual(fs.split_hierarchy(r"a\,b, c"), [r"a\,b", "c"])

    def test_years(self):
        self.assertEqual(fs.parse_years("1823"), (1823, 1823))
        self.assertEqual(fs.parse_years("1800/1900"), (1800, 1900))
        self.assertEqual(fs.parse_years("1800/"), (1800, None))
        self.assertEqual(fs.parse_years("/1900"), (None, 1900))
        with self.assertRaises(ValueError):
            fs.parse_years("last Tuesday")

    def test_distance_units_default_to_miles(self):
        self.assertEqual(fs.parse_distance("45K"), 45.0)
        self.assertAlmostEqual(fs.parse_distance("10M"), 16.09344)
        self.assertAlmostEqual(fs.parse_distance("10"), 16.09344)


def mapped(q):
    return fs.translate(fs.tokenize(q))


class Translate(unittest.TestCase):
    def test_exact_is_the_default_and_is_not_phonetic(self):
        m = mapped("name:England")
        self.assertEqual((m.query, m.mode), ("England", "exactci"))

    def test_tilde_is_lexical_fuzzy_never_phonetic(self):
        self.assertEqual(mapped("name:Baden~").mode, "lexfuzzy")

    def test_wildcard_mode(self):
        self.assertEqual(mapped("name:par*").mode, "wildcard")
        self.assertEqual(mapped("name:p?ris").mode, "wildcard")
        self.assertEqual(mapped(r"name:par\*").mode, "exactci", "escaped wildcard is literal")

    def test_leading_wildcard_and_tilde_with_wildcard_are_errors_as_in_familysearch(self):
        with self.assertRaisesRegex(ValueError, "beginning"):
            mapped("name:*aris")
        with self.assertRaisesRegex(ValueError, "incompatible"):
            mapped("name:par*~")

    def test_partial_name_wins_and_is_prefix(self):
        m = mapped("name:Xyz partialName:Par")
        self.assertEqual((m.query, m.mode), ("Par", "starts"))
        self.assertTrue(any("partialName" in w for w in m.warnings))

    def test_hierarchy_in_name(self):
        m = mapped('name:"par*,france"')
        self.assertEqual((m.query, m.mode, m.parents), ("par*", "wildcard", ["france"]))

    def test_required_date_becomes_the_year_window(self):
        m = mapped("name:Paris +date:1800/1900")
        self.assertEqual((m.start_year, m.end_year), (1800, 1900))

    def test_optional_and_excluded_dates_are_reported_not_applied(self):
        m = mapped("name:Paris date:1800/1900 -date:1500")
        self.assertIsNone(m.start_year)
        self.assertEqual(len([w for w in m.warnings if "date" in w]), 2)

    def test_known_type_id_maps_unknown_is_reported(self):
        m = mapped("name:Paris +typeId:186 +typeId:999")
        self.assertEqual(m.fclasses, ["P"])
        self.assertTrue(any("999" in w for w in m.warnings))
        self.assertIn("typeId:999", m.ignored)

    def test_whg_type_values_are_accepted(self):
        m = mapped("name:France +typeId:A.PCLI +typeId:aat:300008347")
        self.assertEqual(m.types, ["A.PCLI", "aat:300008347"])

    def test_parent_id_maps_to_contained_in_and_notes_the_direct_parent_gap(self):
        a = mapped("name:Paris +parentId:un:fra~")
        self.assertEqual(a.contained_in, ["un:fra"])
        self.assertFalse(any("DIRECT" in w for w in a.warnings))
        b = mapped("name:Paris +parentId:place:un:fra")
        self.assertEqual(b.contained_in, ["un:fra"])
        self.assertTrue(any("DIRECT" in w for w in b.warnings))

    def test_excluded_and_optional_parents_not_applied(self):
        m = mapped("name:Paris -parentId:32 parentId:1")
        self.assertEqual(m.contained_in, [])
        self.assertEqual(len([w for w in m.warnings if "parentId" in w]), 2)

    def test_proximity_needs_all_three(self):
        m = mapped("name:Paris latitude:48.85 longitude:2.35 distance:45K")
        self.assertEqual((m.lat, m.lng, m.radius_km), (48.85, 2.35, 45.0))
        bad = mapped("name:Paris latitude:48.85 longitude:2.35")
        self.assertIsNone(bad.lat)
        self.assertTrue(any("proximity" in w for w in bad.warnings))

    def test_place_hint_and_unknown_keys_are_reported(self):
        m = mapped("name:Paris placeHint:1,2 bogus:1")
        self.assertTrue(any("placeHint" in w for w in m.warnings))
        self.assertTrue(any("bogus" in w for w in m.warnings))

    def test_q_must_say_what_to_find(self):
        with self.assertRaisesRegex(ValueError, "name or partialName"):
            mapped("+date:1800")


class FakeResolver:
    """Stands in for genealogy.resolve_country / resolve_container (no index needed)."""

    async def country(self, comp, hint=None):
        code = (comp.country if comp is not None else None)
        return {"code": code, "source": "alias"} if code else {"code": None, "source": "none"}

    async def container(self, comp, country=None, parent_id=None):
        return {"place_id": "un:ile", "name": comp.norm, "component": comp.norm, "via": "exactci"} \
            if comp.norm.lower() == "ile-de-france" else None


class TranslateRequest(unittest.TestCase):
    def _go(self, q, **kw):
        from gateway import genealogy as g
        r = FakeResolver()
        req = rc.ReconcileRequest(query=q, mode="familysearch", **kw)
        with mock.patch.object(g, "resolve_country", r.country), mock.patch.object(g, "resolve_container", r.container):
            return asyncio.run(fs.translate_request(req))

    def test_maps_to_gateway_parameters(self):
        new, info = self._go('name:"par*,france" +date:1800/1900')
        self.assertEqual((new.query, new.mode, new.ccodes), ("par*", "wildcard", ["FR"]))
        self.assertEqual((new.start_year, new.end_year), (1800, 1900))
        self.assertFalse(info["phonetic"])
        self.assertEqual(info["mode_applied"], "wildcard")
        self.assertEqual(info["resolved_parents"][0]["kind"], "country")

    def test_unresolved_parent_is_reported_not_guessed(self):
        new, info = self._go('name:"paris,nowhereland"')
        self.assertEqual(new.query, "paris")
        self.assertFalse(new.ccodes)
        self.assertTrue(any("nowhereland" in w for w in info["warnings"]))

    def test_a_resolved_admin_parent_becomes_contained_in(self):
        new, _ = self._go('name:"paris,ile-de-france,france"')
        self.assertEqual(new.contained_in, ["un:ile"])
        self.assertEqual(new.ccodes, ["FR"])

    def test_bad_q_is_a_422(self):
        with self.assertRaises(HTTPException) as cm:
            self._go("name:*bad")
        self.assertEqual(cm.exception.status_code, 422)

    def test_request_fields_are_merged_not_overwritten(self):
        new, _ = self._go("name:Paris +typeId:186", fclasses=["A"])
        self.assertEqual(sorted(new.fclasses), ["A", "P"])


class EndToEndNeverPhonetic(unittest.TestCase):
    """The real endpoint on a stubbed ES: in this mode NO KNN search may ever be sent."""

    def _run(self, q):
        from gateway.result_cache import TTLCache
        fake = FakeES(n_places=12)
        req = rc.ReconcileRequest(query=q, mode="familysearch", size=5)
        with fake.installed(), mock.patch.object(rc, "_result_cache", TTLCache(0, 0)):
            res = asyncio.run(rc.reconcile_search(req))
        return fake, res

    def test_exact_sends_a_case_insensitive_terms_query_and_no_knn(self):
        fake, res = self._run("name:England")
        topo = [c for c in fake.calls if c["kind"].startswith("discovery")]
        self.assertEqual(len(topo), 1, "one lexical discovery search, nothing else")
        self.assertEqual(topo[0]["kind"], "discovery_other", "no knn body")
        self.assertEqual(topo[0]["body"]["query"], {"terms": {"name.raw": ["england"]}})
        self.assertIsNotNone(res.familysearch)
        self.assertFalse(res.familysearch["phonetic"])

    def test_wildcard_and_fuzzy_are_lexical_too(self):
        for q, key in (("name:par*", "wildcard"), ("name:Baden~", "bool"), ("partialName:Par", "bool")):
            fake, _ = self._run(q)
            self.assertFalse([c for c in fake.calls if c["kind"] == "discovery_knn"], q)
            body = [c for c in fake.calls if c["kind"] == "discovery_other"][0]["body"]["query"]
            self.assertIn(key, body, q)

    def test_pagination_uses_offset(self):
        from gateway.result_cache import TTLCache
        fake = FakeES(n_places=12)
        with fake.installed(), mock.patch.object(rc, "_result_cache", TTLCache(0, 0)):
            first = asyncio.run(rc.reconcile_search(rc.ReconcileRequest(query="name:x", mode="familysearch", size=5)))
            second = asyncio.run(rc.reconcile_search(rc.ReconcileRequest(query="name:x", mode="familysearch", size=5, offset=5)))
        ids1, ids2 = [h.place_id for h in first.hits], [h.place_id for h in second.hits]
        self.assertEqual(len(ids1), 5)
        self.assertFalse(set(ids1) & set(ids2), "pages must not overlap")


if __name__ == "__main__":
    unittest.main()
