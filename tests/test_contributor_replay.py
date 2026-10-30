"""Tests for the legacy v3.2 contributor attestation replay.

The replay reads from two DO PG tables (``place_link`` + ``close_matches``)
and converts each row into the canonical hard_link_assertions shape. These
tests cover the row→row mapping logic without touching PG: we feed
synthetic rows that match the SELECT projections in
``_PLACE_LINK_QUERY`` / ``_CLOSE_MATCH_QUERY``.

Since 31 August 2026 those projections hand over the raw ``(dataset id,
places.id)`` pair rather than a ``whg:`` id assembled in SQL, and the whg
endpoint is resolved through the extract's id map. The fixtures below therefore
carry ``dataset_key`` / ``place_key``, and every mapping call takes a map and a
drop ledger. See ``developer/plan-completion-2026-08-31.md`` §2.3.
"""

from __future__ import annotations

import re
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from clustering.harvest import contributor_replay as cr
from clustering.sqlite_overlay import builder, insert_rows
from processing.staging_contract import (
    HARD_LINK_SOURCE_CATEGORIES, HARD_LINK_SQLITE_SCHEMA)
from processing.whg_id_map import SCHEMA, WhgIdMap


def _map(extra: dict[tuple[str, str], str] | None = None) -> WhgIdMap:
    """A stand-in for the extract's id map covering the fixture places."""
    by_key = {
        ("42", "101"): "whg:42:src-a",
        ("1", "200"): "whg:1:src-b",
        ("5", "50"): "whg:5:src-c",
    }
    if extra:
        by_key.update(extra)
    return WhgIdMap([{"schema": SCHEMA, "run_id": "test-run"}], by_key)


def _ledger() -> cr.DropLedger:
    return cr.DropLedger()


def _pl(**overrides):
    base = {
        "dataset_key": "42",                           # datasets.id
        "place_key": "101",                            # places.id (the WHG key)
        "place_b": "wd:Q90",                           # authority concordance
        "user_id": 7,
        "asserted_at": datetime(2025, 6, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def _cm(**overrides):
    base = {
        "dataset_key_a": "1",
        "place_key_a": "200",
        "dataset_key_b": "5",
        "place_key_b": "50",
        "user_id": 9,
        "asserted_at": datetime(2025, 7, 1, tzinfo=timezone.utc),
        "basis": "reviewed",
    }
    base.update(overrides)
    return base


class TestPlaceLinkMapping(unittest.TestCase):
    def _row(self, record=None, id_map=None, ledger=None):
        return cr._place_link_to_hard_link(
            record or _pl(), id_map or _map(), ledger or _ledger())

    def test_basic_conversion(self):
        row = self._row()
        self.assertEqual(row["relation_type"], "closeMatch")
        self.assertEqual(row["source_category"], "contributor")
        self.assertEqual(row["source_id"], "contributor:7:legacy_v3_2")
        self.assertEqual(row["justification"], "place_link")
        # The whg endpoint is the id the EXTRACT minted, not one assembled from
        # the dataset and place keys — that difference is the whole point.
        self.assertEqual({row["place_a"], row["place_b"]},
                         {"whg:42:src-a", "wd:Q90"})
        self.assertLess(row["place_a"], row["place_b"])
        self.assertEqual(row["asserted_at"], "2025-06-01T00:00:00+00:00")

    def test_unmapped_place_is_dropped_and_counted(self):
        # A dataset that is late-curation but not authority=True AND public:
        # 10,732 of 13,466 overlay endpoints were exactly this (plan §2.3).
        ledger = _ledger()
        self.assertIsNone(self._row(_pl(dataset_key="777", place_key="1"), ledger=ledger))
        self.assertEqual(ledger.rows, 1)
        self.assertEqual(ledger.per_dataset, {"777": 1})

    def test_rejects_bare_identifier(self):
        # Defensive: jsonb.identifier without a namespace prefix is corrupt.
        self.assertIsNone(self._row(_pl(place_b="Q90")))
        self.assertIsNone(self._row(_pl(place_b=":Q90")))
        self.assertIsNone(self._row(_pl(place_b="wd:")))

    def test_rejects_bogus_sentinel_identifiers(self):
        # Curator tools sometimes wrote Python `None` / JS `null` as the
        # authrecord_id — observed in the live whgv3beta data 2026-05-02.
        for bad in ("wd:None", "wd:null", "wd:NONE", "tgn:undefined", "wd:nan", "wd:"):
            self.assertIsNone(
                self._row(_pl(place_b=bad)),
                f"should reject {bad}",
            )

    def test_rejects_wikidata_without_letter_prefix(self):
        # WD entity IDs are always Q/P/L/M-prefixed; bare digits are bad data.
        self.assertIsNone(self._row(_pl(place_b="wd:1628979")))
        # But other namespaces (gn, tgn) DO use bare numeric IDs and stay valid.
        ok = self._row(_pl(place_b="gn:3092472"))
        self.assertIsNotNone(ok)
        ok = self._row(_pl(place_b="wd:Q90"))
        self.assertIsNotNone(ok)
        ok = self._row(_pl(place_b="wd:P31"))
        self.assertIsNotNone(ok)

    def test_rejects_missing_user(self):
        self.assertIsNone(self._row(_pl(user_id=None)))

    def test_rejects_self_loop(self):
        # place_a now comes from the map, so a self-loop means the concordance
        # names the very place the map resolved to.
        self.assertIsNone(self._row(_pl(place_b="whg:42:src-a")))


class TestCloseMatchMapping(unittest.TestCase):
    def _row(self, record=None, id_map=None, ledger=None):
        return cr._close_match_to_hard_link(
            record or _cm(), id_map or _map(), ledger or _ledger())

    def test_reviewed_basis(self):
        row = self._row(_cm(basis="reviewed"))
        self.assertEqual(row["relation_type"], "closeMatch")
        self.assertEqual(row["source_id"], "contributor:9:legacy_v3_2")
        self.assertEqual(row["justification"], "close_match:reviewed")
        # Both endpoints resolved through the map, then lex-canonicalized.
        self.assertEqual(row["place_a"], "whg:1:src-b")
        self.assertEqual(row["place_b"], "whg:5:src-c")

    def test_authid_basis(self):
        row = self._row(_cm(basis="authid"))
        self.assertEqual(row["justification"], "close_match:authid")

    # -- ruling N1 (place#342): imported is a weaker category ---------------

    def test_reviewed_and_authid_stay_contributor(self):
        for basis in ("reviewed", "authid"):
            row = self._row(_cm(basis=basis))
            self.assertEqual(row["source_category"], "contributor", basis)
            self.assertEqual(row["source_id"], "contributor:9:legacy_v3_2", basis)

    def test_imported_basis_is_filed_under_imported(self):
        row = self._row(_cm(basis="imported"))
        self.assertEqual(row["source_category"], "imported")
        self.assertEqual(row["source_id"], "imported:9:legacy_v3_2")
        self.assertEqual(row["justification"], "close_match:imported")
        # Everything else about the row is the same shape as a reviewed one.
        self.assertEqual(row["relation_type"], "closeMatch")
        self.assertEqual((row["place_a"], row["place_b"]),
                         ("whg:1:src-b", "whg:5:src-c"))
        self.assertIn(row["source_category"], HARD_LINK_SOURCE_CATEGORIES)

    def test_unknown_basis_is_not_filed_as_either(self):
        # Unreachable from the query's ``basis IN (...)``, but a widened query
        # must not quietly file a new basis as contributor OR imported.
        self.assertIsNone(self._row(_cm(basis="something_new")))
        self.assertIsNone(self._row(_cm(basis=None)))

    def test_self_loop_rejected(self):
        # Both keys resolving to the same minted id — which is now possible in a
        # way it was not when the id was assembled from the keys themselves.
        id_map = _map({("1", "200"): "whg:1:same", ("5", "50"): "whg:1:same"})
        self.assertIsNone(self._row(id_map=id_map))

    def test_missing_user_rejected(self):
        self.assertIsNone(self._row(_cm(user_id=None)))

    def test_either_unmapped_end_drops_the_edge(self):
        # An edge with one indexed end and one unindexed end is not half-usable.
        ledger = _ledger()
        self.assertIsNone(self._row(_cm(dataset_key_b="777"), ledger=ledger))
        self.assertEqual(ledger.per_dataset, {"777": 1})
        self.assertEqual(ledger.rows, 1)

    def test_both_ends_unresolved_is_one_lost_edge_not_two(self):
        # The two counts must not be conflated: the diagnostic attributes an
        # unresolved reference to each dataset, but only one edge was lost.
        ledger = _ledger()
        self.assertIsNone(
            self._row(_cm(dataset_key_a="888", dataset_key_b="777"), ledger=ledger))
        self.assertEqual(ledger.per_dataset, {"888": 1, "777": 1})
        self.assertEqual(ledger.endpoint_refs, 2)
        self.assertEqual(ledger.rows, 1)


def _at(**overrides):
    """A synthetic ``api_contributorattestation`` row (the live flow)."""
    base = {
        "place_a": "gn:2988507",
        "place_b": "wd:Q90",
        "relation_type": "sameAs",
        "user_id": 42,
        "asserted_at": datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        "justification": "curator match",
        "legacy_v3_2": False,
    }
    base.update(overrides)
    return base


class TestAttestationMapping(unittest.TestCase):
    def _row(self, record=None, id_map=None, ledger=None, dispositions=None):
        return cr._attestation_to_hard_link(
            record if record is not None else _at(),
            id_map or _map(), ledger or _ledger(), dispositions)

    def test_basic_conversion(self):
        row = self._row(_at())
        self.assertEqual(row["relation_type"], "sameAs")
        self.assertEqual(row["source_category"], "contributor")
        # Fresh live row → NO legacy suffix (Ticket A).
        self.assertEqual(row["source_id"], "contributor:42")
        self.assertEqual(row["justification"], "curator match")
        self.assertEqual(row["asserted_at"], "2026-07-10T12:00:00+00:00")
        self.assertLess(row["place_a"], row["place_b"])
        self.assertEqual({row["place_a"], row["place_b"]}, {"gn:2988507", "wd:Q90"})

    def test_source_id_matches_django_model(self):
        # Mirrors whg3 ContributorAttestation.source_id() exactly so a row in
        # both stores dedups on the identical UNIQUE key.
        self.assertEqual(self._row(_at())["source_id"],
                         "contributor:42")
        legacy = self._row(_at(legacy_v3_2=True))
        self.assertEqual(legacy["source_id"], "contributor:42:legacy_v3_2")

    def test_all_relation_types_accepted(self):
        for rt in ("sameAs", "exactMatch", "closeMatch", "distinct"):
            self.assertIsNotNone(self._row(_at(relation_type=rt)))

    def test_bad_relation_type_rejected(self):
        self.assertIsNone(self._row(_at(relation_type="relatedTo")))
        self.assertIsNone(self._row(_at(relation_type=None)))

    def test_missing_user_rejected(self):
        self.assertIsNone(self._row(_at(user_id=None)))

    def test_self_loop_rejected(self):
        self.assertIsNone(
            self._row(_at(place_a="wd:Q90", place_b="wd:Q90")))

    def test_empty_justification_becomes_none(self):
        self.assertIsNone(self._row(_at(justification=""))["justification"])
        self.assertIsNone(self._row(_at(justification=None))["justification"])

    def test_non_canonical_input_is_reordered(self):
        # The model CHECK enforces place_a < place_b, but guard defensively.
        row = self._row(_at(place_a="wd:Q90", place_b="gn:2988507"))
        self.assertLess(row["place_a"], row["place_b"])

    # -- the id forms Django may have written -----------------------------
    #
    # These ids were minted by Django, not by the SQL here, and the two
    # mistakes are not symmetric: leaving a legacy id alone yields a dangling
    # edge, while rewriting a CURRENT id yields a wrong one. Hence the
    # already-current test runs first and unconditionally.

    def test_current_whg_id_is_left_exactly_alone(self):
        disp = {}
        row = self._row(_at(place_a="whg:42:src-a", place_b="wd:Q90"),
                        dispositions=disp)
        self.assertEqual({row["place_a"], row["place_b"]},
                         {"whg:42:src-a", "wd:Q90"})
        self.assertEqual(disp.get("already_current"), 1)
        self.assertEqual(disp.get("not_whg"), 1)
        self.assertIsNone(disp.get("remapped"))

    def test_legacy_whg_id_is_translated(self):
        disp = {}
        row = self._row(_at(place_a="whg:42:101", place_b="wd:Q90"),
                        dispositions=disp)
        self.assertEqual({row["place_a"], row["place_b"]},
                         {"whg:42:src-a", "wd:Q90"})
        self.assertEqual(disp.get("remapped"), 1)

    def test_unmapped_whg_id_drops_the_row_and_is_counted(self):
        ledger = _ledger()
        disp = {}
        self.assertIsNone(
            self._row(_at(place_a="whg:777:9", place_b="wd:Q90"),
                      ledger=ledger, dispositions=disp))
        self.assertEqual(ledger.per_dataset, {"777": 1})
        self.assertEqual(ledger.rows, 1)
        self.assertEqual(disp.get("unmatched"), 1)

    def test_non_whg_endpoints_are_untouched(self):
        disp = {}
        row = self._row(_at(), dispositions=disp)
        self.assertEqual({row["place_a"], row["place_b"]},
                         {"gn:2988507", "wd:Q90"})
        self.assertEqual(disp.get("not_whg"), 2)


class TestIterHardLinkRows(unittest.TestCase):
    def test_combines_sources_with_per_source_counts(self):
        pl_rows = [_pl(), _pl(place_b="bare-no-colon")]   # 1 valid, 1 rejected
        cm_rows = [_cm(), _cm(basis="authid"), _cm(user_id=None)]  # 2 valid, 1 rejected
        at_rows = [_at(), _at(relation_type="bad"), _at(user_id=None)]  # 1 valid, 2 rejected
        rows, counts = cr.iter_hard_link_rows(
            pl_rows, cm_rows, at_rows, id_map=_map())
        self.assertEqual(len(rows), 4)
        for k, v in {
            "place_link_input": 2,
            "place_link_converted": 1,
            "close_match_input": 3,
            "close_match_converted": 2,
            "close_match_converted_imported": 0,
            "attestation_input": 3,
            "attestation_converted": 1,
        }.items():
            self.assertEqual(counts[k], v, k)

    def test_imported_rows_are_tallied_separately(self):
        # An imported row IS converted (it is still in the overlay) but is
        # reported under its own counter so a summary never presents it as
        # contributor evidence. Note: same pair, two rows, no dedup here — the
        # list is what gets inserted; the UNIQUE-key behaviour is proven below
        # against a real sqlite.
        cm_rows = [_cm(basis="reviewed"), _cm(basis="imported"), _cm(basis="authid")]
        rows, counts = cr.iter_hard_link_rows([], cm_rows, id_map=_map())
        self.assertEqual(len(rows), 3)
        self.assertEqual(counts["close_match_converted"], 3)
        self.assertEqual(counts["close_match_converted_imported"], 1)
        self.assertEqual(
            sorted(r["source_category"] for r in rows),
            ["contributor", "contributor", "imported"])

    def test_attestations_default_empty(self):
        rows, counts = cr.iter_hard_link_rows([_pl()], [], id_map=_map())
        self.assertEqual(len(rows), 1)
        self.assertEqual(counts["attestation_input"], 0)

    def test_id_map_is_required(self):
        # Not a convenience default: a caller that forgot the map must fail at
        # the call rather than quietly harvest a second set of ids.
        with self.assertRaises(TypeError):
            cr.iter_hard_link_rows([_pl()], [])

    def test_reports_drops_and_provenance(self):
        # The drop count is the number that shows the join worked. Against the
        # live overlay it should land near 10,732 of 13,466 endpoints; a run
        # that drops far fewer matched rows it should not have.
        pl_rows = [_pl(), _pl(dataset_key="777", place_key="1"),
                   _pl(dataset_key="777", place_key="2")]
        rows, counts = cr.iter_hard_link_rows(pl_rows, [], id_map=_map())
        self.assertEqual(len(rows), 1)
        self.assertEqual(counts["dropped_unmapped"]["rows_dropped"], 2)
        self.assertEqual(counts["dropped_unmapped"]["unresolved_endpoint_refs"], 2)
        self.assertEqual(counts["dropped_unmapped"]["datasets_with_drops"], 1)
        self.assertEqual(counts["dropped_unmapped"]["top_datasets"], {"777": 2})
        self.assertEqual(counts["id_map_entries"], 3)
        self.assertEqual(counts["id_map_run_ids"], ["test-run"])


class TestSourceIdContract(unittest.TestCase):
    def test_legacy_suffix_always_present(self):
        # Every row from this harvest is v3 legacy data — the gateway needs
        # the suffix to filter it once dynamic-cluster attestations land.
        for uid in (1, 999, "alice"):
            self.assertEqual(cr._build_source_id(uid), f"contributor:{uid}:legacy_v3_2")
            self.assertEqual(cr._build_source_id(uid, "imported"),
                             f"imported:{uid}:legacy_v3_2")

    def test_reviewed_close_match_and_migrated_attestation_share_a_source_id(self):
        # The premise of the UNIQUE-key dedup proven in
        # TestImportedCategoryInOverlay: the legacy row (flow A) and the
        # attestation whg3 migrates from it (flow B, legacy_v3_2=True) must
        # produce the identical source_id for the same user.
        legacy = cr._close_match_to_hard_link(_cm(basis="reviewed"), _map(), _ledger())
        migrated = cr._attestation_to_hard_link(
            _at(place_a="whg:1:src-b", place_b="whg:5:src-c", user_id=9,
                relation_type="closeMatch", legacy_v3_2=True),
            _map(), _ledger())
        self.assertEqual(legacy["source_id"], migrated["source_id"])
        self.assertEqual(legacy["source_id"], "contributor:9:legacy_v3_2")
        # ...and the imported row for the same pair and user does NOT.
        imported = cr._close_match_to_hard_link(_cm(basis="imported"), _map(), _ledger())
        self.assertNotEqual(imported["source_id"], migrated["source_id"])


# ---------------------------------------------------------------------------
# The active-attestation query against the Phase 5 (place#342) column set
# ---------------------------------------------------------------------------

# The columns ``api_contributorattestation`` had BEFORE whg3's Phase 5
# migration. The query may name only these (plus ``status`` in its WHERE).
_PRE_PHASE5_COLUMNS = frozenset({
    "id", "place_a", "place_b", "relation_type", "user_id", "asserted_at",
    "justification", "legacy_v3_2", "status", "revoked_at",
})

# The additive Phase 5 columns. None of them may appear in the query.
_PHASE5_NEW_COLUMNS = ("certainty", "evidence", "bundle", "place_a_pk", "place_b_pk")

# A SQLite stand-in for the post-migration PG table. The harvest's SQL is plain
# enough to run verbatim here, so the ``status = 'active'`` predicate is
# EXECUTED, not merely read, against rows carrying every new status.
_PHASE5_TABLE_DDL = """
CREATE TABLE api_contributorattestation (
    id INTEGER PRIMARY KEY,
    place_a TEXT NOT NULL,
    place_b TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    user_id INTEGER,
    asserted_at TEXT,
    justification TEXT,
    legacy_v3_2 INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    revoked_at TEXT,
    certainty REAL,
    evidence TEXT,
    bundle TEXT,
    place_a_pk INTEGER,
    place_b_pk INTEGER
)
"""


def _phase5_conn() -> sqlite3.Connection:
    """An in-memory table with the new columns and one row per status.

    Each row is a DIFFERENT pair so the forwarded set identifies which status
    came through by its endpoints, not by count alone."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_PHASE5_TABLE_DDL)
    fixture = [
        # (place_a, place_b, status, certainty, evidence, bundle)
        ("gn:1", "wd:Q1", "active", 0.9, "curated", "b1"),
        ("gn:2", "wd:Q2", "pending", 0.5, "awaiting review", "b1"),
        ("gn:3", "wd:Q3", "withdrawn", None, None, None),
        ("gn:4", "wd:Q4", "revoked", 0.9, "curated", "b2"),
    ]
    for i, (a, b, status, cert, ev, bundle) in enumerate(fixture, start=1):
        conn.execute(
            "INSERT INTO api_contributorattestation (id, place_a, place_b, "
            "relation_type, user_id, asserted_at, justification, legacy_v3_2, "
            "status, certainty, evidence, bundle, place_a_pk, place_b_pk) "
            "VALUES (?, ?, ?, 'sameAs', 42, '2026-10-01T00:00:00+00:00', "
            "'j', 0, ?, ?, ?, ?, ?, ?)",
            (i, a, b, status, cert, ev, bundle, i * 10, i * 10 + 1),
        )
    return conn


class TestActiveAttestationQuery(unittest.TestCase):
    """``_ACTIVE_ATTESTATION_QUERY`` after whg3 added Phase 5 columns.

    How the fetch is (not) mocked: nothing in this file patches asyncpg. The
    mapping tests above call the row→row functions with synthetic dicts and
    never exercise the SQL at all, which is why the ``status`` predicate had no
    test until now. Here the query text is executed verbatim in SQLite against
    the new column set, and the one end-to-end test patches ``cr.fetch_all``
    (the synchronous façade ``replay`` calls) with the rows that execution
    returned."""

    def test_query_names_no_phase5_column(self):
        referenced = set(re.findall(r"\bca\.(\w+)", cr._ACTIVE_ATTESTATION_QUERY))
        self.assertTrue(referenced, "regex found no ca.<column> references")
        self.assertLessEqual(referenced, _PRE_PHASE5_COLUMNS,
                             f"query names post-migration columns: "
                             f"{sorted(referenced - _PRE_PHASE5_COLUMNS)}")
        for col in _PHASE5_NEW_COLUMNS:
            self.assertNotIn(col, cr._ACTIVE_ATTESTATION_QUERY, col)
        # And the predicate is the one Django's live forwarder uses.
        self.assertRegex(cr._ACTIVE_ATTESTATION_QUERY,
                         r"WHERE\s+ca\.status\s*=\s*'active'")

    def test_executed_verbatim_forwards_active_and_only_active(self):
        conn = _phase5_conn()
        # Prove the fixture holds every status before testing what is dropped.
        statuses = {r[0] for r in conn.execute(
            "SELECT status FROM api_contributorattestation")}
        self.assertEqual(statuses, {"active", "pending", "withdrawn", "revoked"})

        rows = [dict(r) for r in conn.execute(cr._ACTIVE_ATTESTATION_QUERY)]

        # Presence first: the active row IS forwarded, with the projection the
        # mapper expects...
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["place_a"], rows[0]["place_b"]), ("gn:1", "wd:Q1"))
        self.assertEqual(
            set(rows[0]),
            {"place_a", "place_b", "relation_type", "user_id", "asserted_at",
             "justification", "legacy_v3_2"})
        # ...and none of the new columns ride along in the projection.
        for col in _PHASE5_NEW_COLUMNS:
            self.assertNotIn(col, rows[0])
        # Then absence, in the same call: pending, withdrawn and revoked
        # endpoints do not appear.
        forwarded = {(r["place_a"], r["place_b"]) for r in rows}
        for absent in (("gn:2", "wd:Q2"), ("gn:3", "wd:Q3"), ("gn:4", "wd:Q4")):
            self.assertNotIn(absent, forwarded)

    def test_replay_end_to_end_with_fetch_patched(self):
        # ``replay`` → ``fetch_all`` → mapping → ``insert_rows``. Patch the
        # façade with exactly what the verbatim query returned, so the overlay
        # content is a consequence of the SQL predicate, not of a hand-built
        # fixture that already omitted the pending row.
        conn = _phase5_conn()
        fetched = [dict(r) for r in conn.execute(cr._ACTIVE_ATTESTATION_QUERY)]
        with TemporaryDirectory() as tmp, \
                mock.patch.object(cr, "fetch_all", return_value=([], [], fetched)), \
                mock.patch.object(cr, "load_id_map", return_value=_map()):
            db = Path(tmp) / "overlay.sqlite"
            stats = cr.replay(db_path=db, id_map_path=Path(tmp) / "unused.jsonl")
            self.assertEqual(stats["attestation_input"], 1)
            self.assertEqual(stats["attestation_converted"], 1)
            self.assertEqual(stats["inserted"], 1)
            got = sqlite3.connect(str(db)).execute(
                "SELECT place_a, place_b, source_category, source_id "
                "FROM hard_link_assertions").fetchall()
        self.assertEqual(got, [("gn:1", "wd:Q1", "contributor", "contributor:42")])


# ---------------------------------------------------------------------------
# The ``imported`` category in a real overlay built from the DDL
# ---------------------------------------------------------------------------

# The DDL as it stood BEFORE 30 Oct 2026 — two categories. Kept here verbatim
# because the point of the test is that a file built from it still exists on
# disk somewhere and cannot take an ``imported`` row.
_PRE_N1_DDL = HARD_LINK_SQLITE_SCHEMA.replace(
    "CHECK (source_category IN ('authority', 'contributor', 'imported'))",
    "CHECK (source_category IN ('authority', 'contributor'))")
# ``str.replace`` no-ops silently if the DDL text drifts; the tests below would
# then be comparing the current DDL with itself.
assert "'imported'" in HARD_LINK_SQLITE_SCHEMA and "'imported'" not in _PRE_N1_DDL


def _overlay_rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT place_a, place_b, relation_type, source_category, source_id "
        "FROM hard_link_assertions ORDER BY source_category, source_id, relation_type"
    ).fetchall()


class TestImportedCategoryInOverlay(unittest.TestCase):
    """UNIQUE-key behaviour proven against a sqlite built from the DDL, via the
    same ``insert_rows`` (``INSERT OR IGNORE``) the harvest uses."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "overlay.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_imported_and_reviewed_for_same_pair_are_two_rows(self):
        rows, _ = cr.iter_hard_link_rows(
            [], [_cm(basis="reviewed"), _cm(basis="imported")], id_map=_map())
        with builder(self.db) as conn:
            stats = insert_rows(conn, rows)
            got = _overlay_rows(conn)
            by_cat = dict(conn.execute(
                "SELECT source_category, COUNT(*) FROM hard_link_assertions "
                "GROUP BY source_category").fetchall())
        self.assertEqual(stats, {"attempted": 2, "inserted": 2, "rejected": 0})
        self.assertEqual(got, [
            ("whg:1:src-b", "whg:5:src-c", "closeMatch", "contributor",
             "contributor:9:legacy_v3_2"),
            ("whg:1:src-b", "whg:5:src-c", "closeMatch", "imported",
             "imported:9:legacy_v3_2"),
        ])
        # Counted separately: the imported row never inflates the contributor count.
        self.assertEqual(by_cat, {"contributor": 1, "imported": 1})

    def test_migrated_attestation_dedups_against_its_legacy_reviewed_row(self):
        # Flow A (legacy reviewed close_match, user 9) + flow B (the attestation
        # whg3's migrate_reviewed_links creates from it: same user, closeMatch,
        # legacy_v3_2=True). Same source_id → same UNIQUE key → one row.
        migrated = _at(place_a="whg:1:src-b", place_b="whg:5:src-c", user_id=9,
                       relation_type="closeMatch", legacy_v3_2=True)
        rows, counts = cr.iter_hard_link_rows(
            [], [_cm(basis="reviewed")], [migrated], id_map=_map())
        self.assertEqual(len(rows), 2)   # both mapped — dedup is the DB's job
        with builder(self.db) as conn:
            stats = insert_rows(conn, rows)
            got = _overlay_rows(conn)
        self.assertEqual(stats, {"attempted": 2, "inserted": 1, "rejected": 0})
        self.assertEqual(got, [
            ("whg:1:src-b", "whg:5:src-c", "closeMatch", "contributor",
             "contributor:9:legacy_v3_2"),
        ])

    def test_dedup_does_not_hold_if_the_migration_changes_relation_type(self):
        # Honest limit of the previous test: relation_type is part of the
        # UNIQUE key. If migrate_reviewed_links writes the attestation as
        # ``sameAs`` rather than ``closeMatch``, both rows land.
        migrated = _at(place_a="whg:1:src-b", place_b="whg:5:src-c", user_id=9,
                       relation_type="sameAs", legacy_v3_2=True)
        rows, _ = cr.iter_hard_link_rows(
            [], [_cm(basis="reviewed")], [migrated], id_map=_map())
        with builder(self.db) as conn:
            stats = insert_rows(conn, rows)
            got = _overlay_rows(conn)
        self.assertEqual(stats["inserted"], 2)
        self.assertEqual([r[2] for r in got], ["closeMatch", "sameAs"])

    def test_imported_row_never_dedups_against_a_migrated_attestation(self):
        migrated = _at(place_a="whg:1:src-b", place_b="whg:5:src-c", user_id=9,
                       relation_type="closeMatch", legacy_v3_2=True)
        rows, _ = cr.iter_hard_link_rows(
            [], [_cm(basis="imported")], [migrated], id_map=_map())
        with builder(self.db) as conn:
            stats = insert_rows(conn, rows)
            got = _overlay_rows(conn)
        self.assertEqual(stats["inserted"], 2)
        self.assertEqual([(r[3], r[4]) for r in got], [
            ("contributor", "contributor:9:legacy_v3_2"),
            ("imported", "imported:9:legacy_v3_2"),
        ])

    def _pre_n1_overlay(self) -> Path:
        old = sqlite3.connect(str(self.db))
        old.executescript(_PRE_N1_DDL)
        old.close()
        return self.db

    def test_pre_n1_overlay_silently_drops_imported_rows(self):
        # What an overlay built before the DDL change actually does with an
        # imported row, measured through the harvest's own INSERT OR IGNORE:
        # NO IntegrityError. SQLite's OR IGNORE skips a CHECK violation the
        # same way it skips a UNIQUE collision, so the row vanishes and the
        # only trace is ``inserted`` one short of ``attempted`` — the exact
        # signature of a legitimate dedup. (The first draft of this test
        # asserted a raise, and failed.) The builder's schema guard exists
        # because of this; here it is bypassed to show the hazard it covers.
        self._pre_n1_overlay()
        rows, _ = cr.iter_hard_link_rows(
            [], [_cm(basis="reviewed"), _cm(basis="imported")], id_map=_map())
        conn = sqlite3.connect(str(self.db), isolation_level=None)
        try:
            stats = insert_rows(conn, rows)
            got = _overlay_rows(conn)
        finally:
            conn.close()
        self.assertEqual(stats, {"attempted": 2, "inserted": 1, "rejected": 0})
        self.assertEqual([r[3] for r in got], ["contributor"])

    def test_builder_refuses_a_pre_n1_overlay(self):
        # ...hence the guard: opening an old-DDL file through the builder is a
        # hard error at OPEN time, before any row can be dropped. A rebuilt
        # overlay is required; harvesting into the old file is not an option.
        from clustering.sqlite_overlay import OverlaySchemaMismatch
        self._pre_n1_overlay()
        with self.assertRaises(OverlaySchemaMismatch) as ctx:
            with builder(self.db):
                pass
        self.assertIn("imported", str(ctx.exception))
        # And the file was not upgraded in passing: the two-category CHECK is
        # still what the file says.
        ddl = sqlite3.connect(str(self.db)).execute(
            "SELECT sql FROM sqlite_master WHERE name='hard_link_assertions'"
        ).fetchone()[0]
        self.assertIn("'authority', 'contributor'))", ddl)
        self.assertNotIn("'imported'", ddl)
        # The same rows go straight into a file built from the CURRENT DDL.
        rows, _ = cr.iter_hard_link_rows(
            [], [_cm(basis="reviewed"), _cm(basis="imported")], id_map=_map())
        fresh = Path(self.tmp.name) / "rebuilt.sqlite"
        with builder(fresh) as conn:
            self.assertEqual(insert_rows(conn, rows)["inserted"], 2)

    def test_builder_reopens_a_current_overlay(self):
        # The guard must not fire on a resume of a file built from the current
        # DDL (the "already exists" path it sits behind).
        with builder(self.db) as conn:
            insert_rows(conn, [cr._close_match_to_hard_link(
                _cm(basis="imported"), _map(), _ledger())])
        with builder(self.db) as conn:
            self.assertEqual([r[3] for r in _overlay_rows(conn)], ["imported"])


class TestPublishableStatusOverride(unittest.TestCase):
    """The default `ds_status` filter is overridable via env var so the
    same code can target whgv2 (`accessioned`) or whgv3beta
    (`indexed`/`accessioning`/`wd-complete`) without code change."""

    def test_default_when_env_unset(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                cr._publishable_ds_statuses(),
                cr._DEFAULT_PUBLISHABLE_DS_STATUSES,
            )

    def test_env_override(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"WHG_PUBLISHABLE_DS_STATUSES": "accessioned, indexed"}):
            self.assertEqual(cr._publishable_ds_statuses(), ("accessioned", "indexed"))

    def test_empty_env_falls_back_to_default(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"WHG_PUBLISHABLE_DS_STATUSES": "  "}):
            self.assertEqual(
                cr._publishable_ds_statuses(),
                cr._DEFAULT_PUBLISHABLE_DS_STATUSES,
            )


if __name__ == "__main__":
    unittest.main()
