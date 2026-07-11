"""Tests for the legacy v3.2 contributor attestation replay.

The replay reads from two DO PG tables (``place_link`` + ``close_matches``)
and converts each row into the canonical hard_link_assertions shape. These
tests cover the row→row mapping logic without touching PG: we feed
synthetic rows that match the SELECT projections in
``_PLACE_LINK_QUERY`` / ``_CLOSE_MATCH_QUERY``.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from clustering.harvest import contributor_replay as cr


def _pl(**overrides):
    base = {
        "place_a": "whg:42:101",                       # contributor's WHG place
        "place_b": "wd:Q90",                           # authority concordance
        "user_id": 7,
        "asserted_at": datetime(2025, 6, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def _cm(**overrides):
    base = {
        "place_a": "whg:1:200",
        "place_b": "whg:5:50",
        "user_id": 9,
        "asserted_at": datetime(2025, 7, 1, tzinfo=timezone.utc),
        "basis": "reviewed",
    }
    base.update(overrides)
    return base


class TestPlaceLinkMapping(unittest.TestCase):
    def test_basic_conversion(self):
        row = cr._place_link_to_hard_link(_pl())
        self.assertEqual(row["relation_type"], "closeMatch")
        self.assertEqual(row["source_category"], "contributor")
        self.assertEqual(row["source_id"], "contributor:7:legacy_v3_2")
        self.assertEqual(row["justification"], "place_link")
        # Both endpoints present, lex-canonicalized
        self.assertEqual({row["place_a"], row["place_b"]}, {"whg:42:101", "wd:Q90"})
        self.assertLess(row["place_a"], row["place_b"])
        self.assertEqual(row["asserted_at"], "2025-06-01T00:00:00+00:00")

    def test_rejects_bare_identifier(self):
        # Defensive: jsonb.identifier without a namespace prefix is corrupt.
        self.assertIsNone(cr._place_link_to_hard_link(_pl(place_b="Q90")))
        self.assertIsNone(cr._place_link_to_hard_link(_pl(place_b=":Q90")))
        self.assertIsNone(cr._place_link_to_hard_link(_pl(place_b="wd:")))

    def test_rejects_bogus_sentinel_identifiers(self):
        # Curator tools sometimes wrote Python `None` / JS `null` as the
        # authrecord_id — observed in the live whgv3beta data 2026-05-02.
        for bad in ("wd:None", "wd:null", "wd:NONE", "tgn:undefined", "wd:nan", "wd:"):
            self.assertIsNone(
                cr._place_link_to_hard_link(_pl(place_b=bad)),
                f"should reject {bad}",
            )

    def test_rejects_wikidata_without_letter_prefix(self):
        # WD entity IDs are always Q/P/L/M-prefixed; bare digits are bad data.
        self.assertIsNone(cr._place_link_to_hard_link(_pl(place_b="wd:1628979")))
        # But other namespaces (gn, tgn) DO use bare numeric IDs and stay valid.
        ok = cr._place_link_to_hard_link(_pl(place_b="gn:3092472"))
        self.assertIsNotNone(ok)
        ok = cr._place_link_to_hard_link(_pl(place_b="wd:Q90"))
        self.assertIsNotNone(ok)
        ok = cr._place_link_to_hard_link(_pl(place_b="wd:P31"))
        self.assertIsNotNone(ok)

    def test_rejects_missing_user(self):
        self.assertIsNone(cr._place_link_to_hard_link(_pl(user_id=None)))

    def test_rejects_self_loop(self):
        self.assertIsNone(cr._place_link_to_hard_link(_pl(place_a="wd:Q90", place_b="wd:Q90")))


class TestCloseMatchMapping(unittest.TestCase):
    def test_reviewed_basis(self):
        row = cr._close_match_to_hard_link(_cm(basis="reviewed"))
        self.assertEqual(row["relation_type"], "closeMatch")
        self.assertEqual(row["source_id"], "contributor:9:legacy_v3_2")
        self.assertEqual(row["justification"], "close_match:reviewed")
        # Lex-canonicalized
        self.assertEqual(row["place_a"], "whg:1:200")
        self.assertEqual(row["place_b"], "whg:5:50")

    def test_authid_basis(self):
        row = cr._close_match_to_hard_link(_cm(basis="authid"))
        self.assertEqual(row["justification"], "close_match:authid")

    def test_self_loop_rejected(self):
        self.assertIsNone(
            cr._close_match_to_hard_link(_cm(place_a="whg:1:50", place_b="whg:1:50"))
        )

    def test_missing_user_rejected(self):
        self.assertIsNone(cr._close_match_to_hard_link(_cm(user_id=None)))


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
    def test_basic_conversion(self):
        row = cr._attestation_to_hard_link(_at())
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
        self.assertEqual(cr._attestation_to_hard_link(_at())["source_id"],
                         "contributor:42")
        legacy = cr._attestation_to_hard_link(_at(legacy_v3_2=True))
        self.assertEqual(legacy["source_id"], "contributor:42:legacy_v3_2")

    def test_all_relation_types_accepted(self):
        for rt in ("sameAs", "exactMatch", "closeMatch", "distinct"):
            self.assertIsNotNone(cr._attestation_to_hard_link(_at(relation_type=rt)))

    def test_bad_relation_type_rejected(self):
        self.assertIsNone(cr._attestation_to_hard_link(_at(relation_type="relatedTo")))
        self.assertIsNone(cr._attestation_to_hard_link(_at(relation_type=None)))

    def test_missing_user_rejected(self):
        self.assertIsNone(cr._attestation_to_hard_link(_at(user_id=None)))

    def test_self_loop_rejected(self):
        self.assertIsNone(
            cr._attestation_to_hard_link(_at(place_a="wd:Q90", place_b="wd:Q90")))

    def test_empty_justification_becomes_none(self):
        self.assertIsNone(cr._attestation_to_hard_link(_at(justification=""))["justification"])
        self.assertIsNone(cr._attestation_to_hard_link(_at(justification=None))["justification"])

    def test_non_canonical_input_is_reordered(self):
        # The model CHECK enforces place_a < place_b, but guard defensively.
        row = cr._attestation_to_hard_link(_at(place_a="wd:Q90", place_b="gn:2988507"))
        self.assertLess(row["place_a"], row["place_b"])


class TestIterHardLinkRows(unittest.TestCase):
    def test_combines_sources_with_per_source_counts(self):
        pl_rows = [_pl(), _pl(place_b="bare-no-colon")]   # 1 valid, 1 rejected
        cm_rows = [_cm(), _cm(basis="authid"), _cm(user_id=None)]  # 2 valid, 1 rejected
        at_rows = [_at(), _at(relation_type="bad"), _at(user_id=None)]  # 1 valid, 2 rejected
        rows, counts = cr.iter_hard_link_rows(pl_rows, cm_rows, at_rows)
        self.assertEqual(len(rows), 4)
        self.assertEqual(counts, {
            "place_link_input": 2,
            "place_link_converted": 1,
            "close_match_input": 3,
            "close_match_converted": 2,
            "attestation_input": 3,
            "attestation_converted": 1,
        })

    def test_attestations_default_empty(self):
        # Backward-compat: two-arg callers still work.
        rows, counts = cr.iter_hard_link_rows([_pl()], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(counts["attestation_input"], 0)


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
