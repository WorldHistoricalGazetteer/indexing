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

import unittest
from datetime import datetime, timezone

from clustering.harvest import contributor_replay as cr
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
            "attestation_input": 3,
            "attestation_converted": 1,
        }.items():
            self.assertEqual(counts[k], v, k)

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
