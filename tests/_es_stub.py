"""A stubbed Elasticsearch for exercising the REAL ``reconcile_search`` offline.

Not a test module (leading underscore): shared by the reconcile end-to-end tests.
It patches ``httpx.AsyncClient`` so the handler's ES calls hit an in-memory
``httpx.MockTransport`` that answers the three kinds of search the endpoint makes:

* discovery on ``toponyms`` (knn / near-miss / exact) — ``n_places`` places with
  descending scores, each carried by one toponym hit via ``attestations``;
* the places fetch — the requested ids, in the order asked;
* the enrichment fetch on ``toponyms`` (``terms`` on ``attestations``, size >= 500)
  — two toponyms per requested place.

Every request is recorded in ``calls`` so tests can assert what was ASKED, not just
what came back.
"""

from __future__ import annotations

import contextlib
import json
from unittest import mock

import httpx

_ORIG = httpx.AsyncClient


def _find_lists(obj, key):
    """Every list found under ``key`` anywhere in a JSON-ish structure."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, list):
                out.append(v)
            out.extend(_find_lists(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_find_lists(v, key))
    return out


class FakeES:
    def __init__(self, n_places: int = 60, took: dict | None = None, ccode: str = "GB"):
        self.n_places = n_places
        self.took = took or {"discovery": 11, "places": 5, "enrich": 7}
        self.ccode = ccode
        self.calls: list[dict] = []          # {"kind", "path", "body"}

    def pid(self, i: int) -> str:
        return f"gn:{1000 + i}"

    def _discovery(self, body):
        hits = [{"_score": 0.99 - i * 0.005, "_source": {
                    "name": f"Name{i}", "lang": "en", "attestations": [self.pid(i)]}}
                for i in range(self.n_places)]
        return {"took": self.took["discovery"], "hits": {"total": {"value": len(hits)}, "hits": hits}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        path = request.url.path
        if "/toponyms/_search" in path:
            ids = [i for l in _find_lists(body, "attestations") for i in l]
            if ids and body.get("size", 0) >= 500 and "knn" not in body:
                self.calls.append({"kind": "enrich", "path": path, "body": body, "ids": ids})
                hits = [{"_score": 1, "_source": {"name": f"{pid}-a", "lang": "en", "attestations": [pid]}}
                        for pid in ids]
                hits += [{"_score": 1, "_source": {"name": f"{pid}-b", "lang": "fr", "attestations": [pid]}}
                         for pid in ids]
                return httpx.Response(200, json={"took": self.took["enrich"],
                                                 "hits": {"total": {"value": len(hits)}, "hits": hits}})
            kind = "knn" if "knn" in body else "other"
            self.calls.append({"kind": "discovery_" + kind, "path": path, "body": body})
            return httpx.Response(200, json=self._discovery(body))
        ids = [i for l in _find_lists(body, "place_id") for i in l] or \
              [i for l in _find_lists(body, "_id") for i in l] or \
              [i for l in _find_lists(body, "values") for i in l]
        self.calls.append({"kind": "places", "path": path, "body": body, "ids": ids})
        hits = [{"_score": 1, "_source": {"place_id": pid, "title": pid.upper(),
                                          "ccodes": [self.ccode], "geometries": []}} for pid in ids]
        return httpx.Response(200, json={"took": self.took["places"],
                                         "hits": {"total": {"value": len(hits)}, "hits": hits}})

    @contextlib.contextmanager
    def installed(self):
        factory = lambda *a, **k: _ORIG(transport=httpx.MockTransport(self.handler), timeout=5)
        with mock.patch("httpx.AsyncClient", factory):
            yield self

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c["kind"] == kind)
