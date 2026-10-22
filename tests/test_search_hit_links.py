"""``/api/search`` hits carry the place's ``links`` — only when asked (include_links).

The WHG website's Locus page wants the Wikipedia articles a wd place carries
(Wikidata sitelinks, stored as ``seeAlso`` links). ``/api/places`` returned them;
``/api/search`` fetched ``links`` from Elasticsearch on every request and then
dropped them, because ``SearchHit`` did not declare the field and a response
model silently drops what it does not declare.

Pinned here:

* flag OFF — the serialised response is the flag-ON response minus exactly the
  two new keys, through the real FastAPI response-model path (dump, re-validate,
  serialise), and the places query does not even select ``links``;
* flag ON — ``links`` shaped like ``PlaceDetail.links``, Wikipedia first,
  ``links_total`` before the per-hit cap, ``[]`` for a place with none;
* ``build_places_filter`` still selects ``links`` by default, which
  ``/api/reconcile`` depends on.

Stubbed Elasticsearch only (no network, nothing written).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gateway import search as search_mod
    from gateway.es_helpers import (
        MAX_HIT_LINKS, build_places_filter, format_hit_links,
    )
    from gateway.search import SearchHit
except ModuleNotFoundError as exc:  # pragma: no cover - dev machines without the API deps
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


WP = "https://{}.wikipedia.org/wiki/Greece"
# Stored order deliberately puts an authority link FIRST, so "Wikipedia first"
# is something the formatter has to do rather than something the fixture gives.
WD_LINKS = [
    {"type": "closeMatch", "identifier": "gn:390903"},
    {"type": "seeAlso", "identifier": WP.format("en")},
    {"type": "closeMatch", "identifier": "tgn:1000074"},
    {"type": "seeAlso", "identifier": WP.format("fr")},
]


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeES:
    """httpx.AsyncClient stand-in; records each (url, body) it is sent."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, *a, **kw):  # constructor
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kw):
        self.calls.append((url, json))
        if "/places/" in url:
            src = [
                {"place_id": "wd:Q41", "namespace": "wd", "title": "Greece",
                 "ccodes": ["GR"], "types": [],
                 "geometries": [{"repr_point": {"lon": 22.0, "lat": 39.0}}]},
                {"place_id": "gn:390903", "namespace": "gn", "title": "Greece",
                 "ccodes": ["GR"], "types": [],
                 "geometries": [{"repr_point": {"lon": 22.0, "lat": 39.0}}]},
            ]
            # Like ES, return `links` only if the query's _source selected it.
            if "links" in (json or {}).get("_source", []):
                src[0]["links"] = WD_LINKS
            return _Resp({"hits": {"total": {"value": 2},
                                   "hits": [{"_source": s} for s in src]},
                          "aggregations": {}})
        return _Resp({"hits": {"hits": [
            {"_score": 9.0, "_source": {"name": "Greece", "lang": "en",
                                        "attestations": ["wd:Q41", "gn:390903"]}},
        ]}})

    def places_source(self) -> list[str]:
        bodies = [b for u, b in self.calls if "/places/" in u]
        assert len(bodies) == 1, bodies
        return bodies[0]["_source"]


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(search_mod.router)
    return TestClient(app)


def _post(body: dict) -> tuple[dict, str, _FakeES]:
    fake = _FakeES()
    with mock.patch("gateway.search.httpx.AsyncClient", fake), \
            mock.patch("gateway.search.es_auth", return_value=None):
        r = _app().post("/api/search", json=body)
    assert r.status_code == 200, r.text
    return r.json(), r.text, fake


class TestFormatHitLinks(unittest.TestCase):

    def test_shape_and_wikipedia_first(self):
        links, total = format_hit_links(WD_LINKS)
        self.assertEqual(total, 4)
        self.assertEqual([l["identifier"] for l in links], [
            WP.format("en"), WP.format("fr"), "gn:390903", "tgn:1000074"])
        # PlaceDetail.links' shape: exactly {type, identifier}.
        self.assertTrue(all(set(l) == {"type", "identifier"} for l in links))

    def test_extra_keys_and_junk_dropped(self):
        links, total = format_hit_links(
            [{"type": "seeAlso", "identifier": WP.format("de"), "x": 1}, "junk", None])
        self.assertEqual(links, [{"type": "seeAlso", "identifier": WP.format("de")}])
        self.assertEqual(total, 1)

    def test_none_is_empty(self):
        self.assertEqual(format_hit_links(None), ([], 0))

    def test_cap_keeps_wikipedia_and_reports_the_uncapped_total(self):
        raw = ([{"type": "closeMatch", "identifier": f"gn:{i}"} for i in range(5)]
               + [{"type": "seeAlso", "identifier": WP.format(l)} for l in ("en", "fr")])
        links, total = format_hit_links(raw, cap=3)
        self.assertEqual(total, 7)
        self.assertEqual(len(links), 3)
        self.assertEqual([l["identifier"] for l in links[:2]],
                         [WP.format("en"), WP.format("fr")])

    def test_default_cap(self):
        raw = [{"type": "closeMatch", "identifier": f"gn:{i}"}
               for i in range(MAX_HIT_LINKS + 5)]
        links, total = format_hit_links(raw)
        self.assertEqual((len(links), total), (MAX_HIT_LINKS, MAX_HIT_LINKS + 5))


class TestPlacesFilterSource(unittest.TestCase):

    def test_links_selected_by_default_for_reconcile(self):
        body = build_places_filter(["gn:1"], None, None, None, None)
        self.assertIn("links", body["_source"])

    def test_links_not_selected_when_off(self):
        on = build_places_filter(["gn:1"], None, None, None, None, links=True)
        off = build_places_filter(["gn:1"], None, None, None, None, links=False)
        self.assertNotIn("links", off["_source"])
        # Nothing else about the query changes.
        self.assertEqual([f for f in on["_source"] if f != "links"], off["_source"])
        self.assertEqual(on["query"], off["query"])


class TestSearchEndpointLinks(unittest.TestCase):

    BODY = {"query": "Greece", "mode": "exact"}

    def test_es_source_requests_links_only_when_flagged(self):
        _, _, off = _post(self.BODY)
        _, _, on = _post({**self.BODY, "include_links": True})
        self.assertNotIn("links", off.places_source())
        self.assertIn("links", on.places_source())

    def test_flag_on_carries_links_wikipedia_first(self):
        data, _, _ = _post({**self.BODY, "include_links": True})
        hits = {h["place_id"]: h for h in data["hits"]}
        self.assertEqual(set(hits), {"wd:Q41", "gn:390903"})
        wd = hits["wd:Q41"]
        self.assertEqual(wd["links"][0],
                         {"type": "seeAlso", "identifier": WP.format("en")})
        self.assertEqual([l["identifier"] for l in wd["links"]][:2],
                         [WP.format("en"), WP.format("fr")])
        self.assertEqual(wd["links_total"], 4)
        # Requested, and the place has none: an empty list, not an absent key.
        self.assertEqual((hits["gn:390903"]["links"], hits["gn:390903"]["links_total"]),
                         ([], 0))

    def test_flag_off_is_flag_on_minus_exactly_the_two_keys(self):
        off, off_text, _ = _post(self.BODY)
        on, _, _ = _post({**self.BODY, "include_links": True})
        # Presence and absence in the same test: the flag-on hits DO carry the
        # keys, so their absence below is not a property of a path that never ran.
        self.assertTrue(all("links" in h for h in on["hits"]))
        for h in off["hits"]:
            self.assertNotIn("links", h)
            self.assertNotIn("links_total", h)
        for h in on["hits"]:
            del h["links"], h["links_total"]
        self.assertEqual(off, on)
        # And the default response is serialised byte-for-byte as the model
        # without the fields would serialise it (no `"links": null` leak).
        self.assertNotIn('"links', off_text)

    def test_model_dump_omits_unrequested_links(self):
        base = SearchHit(place_id="gn:1", title="X").model_dump()
        self.assertNotIn("links", base)
        self.assertNotIn("links_total", base)
        with_links = SearchHit(place_id="gn:1", title="X", links=[], links_total=0).model_dump()
        self.assertEqual(with_links["links"], [])
        del with_links["links"], with_links["links_total"]
        self.assertEqual(base, with_links)
        # JSON mode too (what the response model uses).
        self.assertNotIn("links", json.loads(SearchHit(place_id="gn:1", title="X")
                                             .model_dump_json()))


if __name__ == "__main__":
    unittest.main()
