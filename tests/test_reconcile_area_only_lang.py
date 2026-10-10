"""Reconcile ``area_only`` (place#323) and ``lang`` (place#324).

``area_only`` is an ES-side filter (``area_only_clause``), so ``size`` still
returns up to N AREAL candidates. The clause is checked by a small interpreter
over the real clause (nested/bool/term/exists) against geometries that
``spatial.is_areal`` also judges: the two must agree on every shape, which is
what keeps the ES filter, ``is_area`` and containment on one definition.

The endpoint tests drive ``reconcile_search`` against a fake ES and assert on
what was POSTED, and on the response, so a call site that stops forwarding the
parameter fails them (see the mutation notes in the commit message).
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from gateway.es_helpers import area_only_clause, build_places_filter

try:
    from gateway import reconcile as rec
    from gateway import spatial
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


# ---- a tiny evaluator for the clause shapes area_only_clause uses ----------
def _ev(q: dict, g: dict) -> bool:
    (kind, body), = q.items()
    if kind == "term":
        (f, v), = body.items()
        return g.get(f.split(".", 1)[1]) == v
    if kind == "exists":
        return g.get(body["field"].split(".", 1)[1]) is not None
    if kind == "bool":
        ok = all(_ev(c, g) for c in body.get("filter", []))
        ok = ok and not any(_ev(c, g) for c in body.get("must_not", []))
        sh = body.get("should")
        if sh:
            ok = ok and any(_ev(c, g) for c in sh)
        return ok
    raise AssertionError(f"unsupported clause {kind}")


def clause_keeps(place_geoms: list[dict]) -> bool:
    c = area_only_clause()["nested"]
    return any(_ev(c["query"], g) for g in place_geoms)


POINT = {"geom_class": "point", "has_geom": False}
LINE = {"geom_class": "line", "has_geom": True}   # has_geom true yet NOT areal
AREA = {"geom_class": "area", "has_geom": True}
LEGACY_POLY = {"has_geom": True}                  # no geom_class yet
LEGACY_PT = {"has_geom": False}


class AreaClauseAgreesWithIsAreal(unittest.TestCase):
    def test_each_shape(self):
        for name, g, want in [("point", POINT, False), ("line", LINE, False),
                              ("area", AREA, True), ("legacy polygon", LEGACY_POLY, True),
                              ("legacy point", LEGACY_PT, False)]:
            with self.subTest(name):
                self.assertEqual(spatial.is_areal(g), want)      # positive control
                self.assertEqual(clause_keeps([g]), want)

    def test_any_areal_geometry_keeps_the_place(self):
        self.assertTrue(clause_keeps([POINT, AREA]))
        self.assertFalse(clause_keeps([POINT, LINE]))

    def test_default_body_has_no_area_clause_and_is_unchanged(self):
        base = build_places_filter(["gn:1"], None, None, None, None)
        self.assertEqual(base, build_places_filter(["gn:1"], None, None, None, None,
                                                   area_only=False))
        self.assertNotIn(area_only_clause(), base["query"]["bool"]["filter"])
        on = build_places_filter(["gn:1"], None, None, None, None, area_only=True)
        self.assertIn(area_only_clause(), on["query"]["bool"]["filter"])


# ---- endpoint, against a fake ES -------------------------------------------
class _Resp:
    def __init__(self, hits): self._h = hits
    def raise_for_status(self): pass
    def json(self): return {"hits": {"hits": self._h, "total": {"value": len(self._h)}}}


def _place(pid, geoms):
    return {"_source": {"place_id": pid, "namespace": pid.split(":")[0],
                        "title": pid, "geometries": geoms}}


PLACES = {
    "gn:pt": [dict(POINT, repr_point={"lat": 1, "lon": 1})],
    "gn:ln": [dict(LINE, repr_point={"lat": 1, "lon": 1})],
    "gn:pg": [dict(AREA, repr_point={"lat": 1, "lon": 1})],
}


class FakeClient:
    posts: list = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, **kw):
        FakeClient.posts.append((url, json))
        if "toponyms" in url or rec.TOPONYMS_INDEX in url:
            if "attestations" in str(json) and "terms" in str(json) and "knn" not in json:
                # enrichment lookup (and lexical queries) -> nothing needed
                pass
            return _Resp([{"_score": 1.0, "_source": {"name": "Blackburn", "lang": "en",
                          "attestations": list(PLACES)}}])
        # places: apply the posted area_only clause the way ES would
        filt = json["query"]["bool"]["filter"]
        want = area_only_clause() in filt
        out = [_place(p, g) for p, g in PLACES.items()
               if not want or clause_keeps(g)]
        return _Resp(out)


def run(**kw):
    FakeClient.posts = []
    req = rec.ReconcileRequest(query="Blackburn", mode="phonetic", **kw)
    with mock.patch("httpx.AsyncClient", FakeClient), \
         mock.patch.object(rec, "_build_phonetic_knn",
                           side_effect=lambda form, **k: {"knn": {}, "_k": k}) as knn:
        resp = asyncio.run(rec.reconcile_search(req))
    return resp, knn


class AreaOnlyEndpoint(unittest.TestCase):
    def test_default_returns_everything(self):
        resp, _ = run()
        self.assertEqual({h.place_id for h in resp.hits}, set(PLACES))

    def test_area_only_keeps_only_the_polygon(self):
        resp, _ = run(area_only=True)
        self.assertEqual([h.place_id for h in resp.hits], ["gn:pg"])
        self.assertTrue(resp.hits[0].geometries[0].is_area)

    def test_scores_of_survivors_are_unchanged(self):
        # Same place, same discovery, only the filter differs -> same score/match.
        a, _ = run()
        b, _ = run(area_only=True)
        da = {h.place_id: h for h in a.hits}["gn:pg"]
        self.assertGreater(da.score, 0)
        self.assertEqual(da.model_dump(), b.hits[0].model_dump())   # whole candidate
        self.assertEqual(a.max_score, b.max_score)

    def test_places_body_carries_the_clause_only_when_asked(self):
        run(area_only=True)
        places = [j for u, j in FakeClient.posts if rec.PLACES_INDEX in u]
        self.assertTrue(places and all(area_only_clause() in j["query"]["bool"]["filter"]
                                       for j in places))
        run()
        places = [j for u, j in FakeClient.posts if rec.PLACES_INDEX in u]
        self.assertTrue(places and not any(area_only_clause() in j["query"]["bool"]["filter"]
                                           for j in places))


class LangNormalisation(unittest.TestCase):
    def test_table(self):
        for raw, want in [(None, "und"), ("en", "en"), ("EN", "en"), (" Fr ", "fr"),
                          ("grc", "grc"), ("e", "und"), ("english", "und"),
                          ("e1", "und"), ("", "und"), ("en-GB", "und"), (5, "und")]:
            with self.subTest(raw=raw):
                self.assertEqual(rec._normalise_lang(raw), want)


class LangEndpoint(unittest.TestCase):
    def test_default_is_und_for_every_pass(self):
        _, knn = run(variants=["Blakeburn"])
        langs = [c.kwargs["lang"] for c in knn.call_args_list]
        self.assertGreaterEqual(len(langs), 2)
        self.assertEqual(set(langs), {"und"})

    def test_lang_reaches_primary_variant_and_derived_passes(self):
        _, knn = run(variants=["Blakeburn"], lang="EN")
        forms = [c.args[0] for c in knn.call_args_list]
        self.assertIn("Blackburn", forms); self.assertIn("Blakeburn", forms)
        self.assertEqual({c.kwargs["lang"] for c in knn.call_args_list}, {"en"})

    def test_derived_form_pass_gets_lang(self):
        req = rec.ReconcileRequest(query="Blackburn (Lancashire)", mode="phonetic", lang="de")
        FakeClient.posts = []
        with mock.patch("httpx.AsyncClient", FakeClient), \
             mock.patch.object(rec, "_build_phonetic_knn",
                               side_effect=lambda f, **k: {"knn": {}}) as knn:
            resp = asyncio.run(rec.reconcile_search(req))
        self.assertTrue(resp.derived_forms)                       # positive control
        self.assertEqual({c.kwargs["lang"] for c in knn.call_args_list}, {"de"})
        self.assertGreater(len(knn.call_args_list), 1)

    def test_malformed_falls_back_to_und_without_error(self):
        _, knn = run(lang="english!!")
        self.assertEqual({c.kwargs["lang"] for c in knn.call_args_list}, {"und"})

    def test_client_vector_still_wins_and_lang_reaches_embed_otherwise(self):
        import numpy as np
        from gateway import symphonym, es_helpers
        vec = [3] * 128
        with mock.patch.object(symphonym, "model_version", return_value="v8"), \
             mock.patch.object(symphonym, "embed", return_value=np.zeros(128)) as emb:
            b = es_helpers.build_phonetic_knn("Blackburn", lang="en", query_vector=vec,
                                              query_vector_model="v8")
            emb.assert_not_called()                       # client vector won
            self.assertEqual(b["knn"]["query_vector"], vec)
            # stale-model vector is discarded -> server embed, WITH the lang
            es_helpers.build_phonetic_knn("Blackburn", lang="en", query_vector=vec,
                                          query_vector_model="v7")
            emb.assert_called_once()
            self.assertEqual(emb.call_args.kwargs.get("lang"), "en")


if __name__ == "__main__":
    unittest.main()
