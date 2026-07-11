"""Tests for the gateway ``POST`` / ``DELETE /api/links`` receiver
(``gateway/links.py``).

Mirrors the wire contract in the handoff (``developer/handoff-api-links-receiver.md``)
and ``whg3/api/crc_client.py``: idempotent INSERT OR IGNORE into a *live-delta*
SQLite, idempotent DELETE by the UNIQUE key, and 422 on rows that violate the
canonical hard-link contract. The live-delta path is monkeypatched to a temp file
so the real overlay directory is never touched.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import links


def _row(**over) -> dict:
    row = {
        "place_a": "gn:123",
        "place_b": "wd:Q90",       # "gn:123" < "wd:Q90" — canonical order
        "relation_type": "sameAs",
        "source_category": "contributor",
        "source_id": "contributor:42",
        "asserted_at": "2026-07-11T00:00:00Z",
        "justification": "test",
    }
    row.update(over)
    return row


_KEY = {
    "place_a": "gn:123", "place_b": "wd:Q90",
    "relation_type": "sameAs", "source_id": "contributor:42",
}


class TestLinksReceiver(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self._orig_path = links.LIVE_DB_PATH
        # Point the live-delta DB at a temp file; _connect() reads this module
        # global at call time, so patching it here is sufficient.
        links.LIVE_DB_PATH = Path(self.tmp.name) / "sub" / "live.sqlite"
        app = FastAPI()
        app.include_router(links.router)
        self.client = TestClient(app)

    def tearDown(self):
        links.LIVE_DB_PATH = self._orig_path
        self.tmp.cleanup()

    def test_post_inserts_and_creates_db(self):
        r = self.client.post("/api/links", json=_row())
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.json()["inserted"])
        # Creates the file + parent dir on first use.
        self.assertTrue(links.LIVE_DB_PATH.exists())

    def test_post_duplicate_is_noop(self):
        self.client.post("/api/links", json=_row())
        r = self.client.post("/api/links", json=_row())
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.json()["inserted"])  # INSERT OR IGNORE

    def test_source_category_is_forced_contributor(self):
        # Even if a caller sent something else, the row is stored as contributor.
        self.client.post("/api/links", json=_row(source_category="authority"))
        import sqlite3
        conn = sqlite3.connect(str(links.LIVE_DB_PATH))
        got = conn.execute(
            "SELECT source_category FROM hard_link_assertions").fetchone()[0]
        conn.close()
        self.assertEqual(got, "contributor")

    def test_delete_removes_and_is_idempotent(self):
        self.client.post("/api/links", json=_row())
        r = self.client.request("DELETE", "/api/links", json=_KEY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], 1)
        # Deleting again (or a row that never existed) is a 2xx no-op.
        r2 = self.client.request("DELETE", "/api/links", json=_KEY)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["deleted"], 0)

    def test_bad_ordering_422(self):
        # place_a >= place_b violates the canonical-order CHECK.
        r = self.client.post("/api/links", json=_row(place_a="wd:Q90", place_b="gn:123"))
        self.assertEqual(r.status_code, 422)

    def test_bad_relation_type_422(self):
        r = self.client.post("/api/links", json=_row(relation_type="bogus"))
        self.assertEqual(r.status_code, 422)

    def test_missing_field_422(self):
        body = _row()
        del body["place_b"]
        r = self.client.post("/api/links", json=body)
        self.assertEqual(r.status_code, 422)  # pydantic

    def test_distinct_relation_accepted(self):
        r = self.client.post("/api/links", json=_row(relation_type="distinct"))
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.json()["inserted"])


if __name__ == "__main__":
    unittest.main()
