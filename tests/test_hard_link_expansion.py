"""Tests for the gateway hard-link expansion reader
(``gateway/hard_link_expansion.py``).

Covers the union of the two stores (batch overlay + live-delta), dedup by the
overlay UNIQUE key, in-set vs bounded 1-hop classification, and best-effort
resilience when a store is absent. Both DB paths are monkeypatched to temp files
so the real overlay directory is never touched.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gateway import hard_link_expansion as hle
from clustering.sqlite_overlay import initialise_schema, _INSERT_SQL, _row_tuple


def _write(path: Path, rows: list[dict]) -> None:
    """Create a hard-link SQLite at ``path`` with the canonical schema + rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        initialise_schema(conn)
        conn.executemany(_INSERT_SQL, [_row_tuple(r) for r in rows])
        conn.commit()
    finally:
        conn.close()


def _row(a, b, rel="sameAs", cat="authority", sid="wikidata", **over) -> dict:
    row = {
        "place_a": a, "place_b": b, "relation_type": rel,
        "source_category": cat, "source_id": sid,
        "asserted_at": None, "justification": None,
    }
    row.update(over)
    return row


class TestHardLinkExpansion(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        base = Path(self.tmp.name)
        self.batch = base / "batch" / "hard_links.sqlite"
        self.live = base / "live" / "hard_links_live.sqlite"
        self._orig_batch = hle.BATCH_DB_PATH
        self._orig_live = hle.LIVE_DB_PATH
        hle.BATCH_DB_PATH = self.batch
        hle.LIVE_DB_PATH = self.live

    def tearDown(self):
        hle.BATCH_DB_PATH = self._orig_batch
        hle.LIVE_DB_PATH = self._orig_live
        self.tmp.cleanup()

    # -- basics -----------------------------------------------------------

    def test_empty_input_returns_empty(self):
        _write(self.batch, [_row("gn:1", "wd:Q1")])
        self.assertEqual(hle.expand_hard_links([]), [])
        self.assertEqual(hle.expand_hard_links([""]), [])

    def test_missing_stores_are_non_fatal(self):
        # Neither file exists → best-effort empty, no exception.
        self.assertEqual(hle.expand_hard_links(["gn:1", "wd:Q1"]), [])

    def test_in_set_edge_returned(self):
        _write(self.batch, [_row("gn:1", "wd:Q1")])
        edges = hle.expand_hard_links(["gn:1", "wd:Q1"])
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertEqual((e.a, e.b, e.relation_type, e.source), ("gn:1", "wd:Q1", "sameAs", "wikidata"))
        self.assertTrue(e.via_hard_link)

    # -- 1-hop bounding ---------------------------------------------------

    def test_one_hop_included_by_default(self):
        # gn:1 in the result set; wd:Q1 is a 1-hop neighbour (not in the set).
        _write(self.batch, [_row("gn:1", "wd:Q1")])
        edges = hle.expand_hard_links(["gn:1"])  # only one endpoint in set
        self.assertEqual(len(edges), 1)

    def test_one_hop_excluded_when_disabled(self):
        _write(self.batch, [_row("gn:1", "wd:Q1")])
        edges = hle.expand_hard_links(["gn:1"], one_hop=False)
        self.assertEqual(edges, [])

    def test_one_hop_is_bounded_but_in_set_is_not(self):
        # 3 in-set edges (both endpoints in the set) + 5 one-hop edges.
        in_set_rows = [_row(f"a:{i}", "z:in", sid=f"s{i}") for i in range(3)]
        one_hop_rows = [_row("a:hub", f"z:out{i}", sid=f"h{i}") for i in range(5)]
        _write(self.batch, in_set_rows + one_hop_rows)
        result_set = [f"a:{i}" for i in range(3)] + ["z:in", "a:hub"]
        edges = hle.expand_hard_links(result_set, max_one_hop=2)
        in_set = [e for e in edges if e.a in result_set and e.b in result_set]
        one_hop = [e for e in edges if not (e.a in result_set and e.b in result_set)]
        self.assertEqual(len(in_set), 3)   # never capped
        self.assertEqual(len(one_hop), 2)  # capped at max_one_hop

    # -- union + dedup ----------------------------------------------------

    def test_union_of_batch_and_live(self):
        _write(self.batch, [_row("gn:1", "wd:Q1", sid="wikidata")])
        _write(self.live, [_row("gn:1", "wd:Q1", rel="closeMatch",
                                cat="contributor", sid="contributor:42")])
        edges = hle.expand_hard_links(["gn:1", "wd:Q1"])
        self.assertEqual(len(edges), 2)
        sources = {e.source for e in edges}
        self.assertEqual(sources, {"wikidata", "contributor:42"})

    def test_dedup_by_unique_key_batch_wins(self):
        # Same UNIQUE key (a,b,relation_type,source_id) in both stores → one edge.
        _write(self.batch, [_row("gn:1", "wd:Q1", sid="wikidata")])
        _write(self.live, [_row("gn:1", "wd:Q1", sid="wikidata")])
        edges = hle.expand_hard_links(["gn:1", "wd:Q1"])
        self.assertEqual(len(edges), 1)

    def test_live_only_still_read(self):
        # Batch overlay absent, live-delta present → live edge still returned.
        _write(self.live, [_row("gn:1", "wd:Q1", cat="contributor", sid="contributor:7")])
        edges = hle.expand_hard_links(["gn:1", "wd:Q1"])
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].source, "contributor:7")

    # -- determinism ------------------------------------------------------

    def test_output_is_sorted_deterministically(self):
        rows = [
            _row("a:2", "b:2", sid="s2"),
            _row("a:1", "b:1", sid="s1"),
            _row("a:1", "b:1", rel="closeMatch", sid="s1"),
        ]
        _write(self.batch, rows)
        ids = ["a:1", "b:1", "a:2", "b:2"]
        first = [(e.a, e.b, e.relation_type, e.source) for e in hle.expand_hard_links(ids)]
        self.assertEqual(first, sorted(first))


if __name__ == "__main__":
    unittest.main()
