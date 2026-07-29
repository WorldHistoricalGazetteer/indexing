"""Authority licence metadata + registry read-back audit (place#157).

The registry endpoint silently DROPS a ``license_spdx`` it doesn't recognise,
so a gazetteer can end up with no terms at all without anything failing. These
tests pin the two halves of the guard:

* every authority declares a licence, and every ``custom-*`` id it declares is
  defined in ``settings.CUSTOM_LICENCES`` (so whg3 can seed it);
* ``verify_licences.audit`` names the right defect when the live registry's
  answer and ``AUTHORITIES`` disagree.
"""

from __future__ import annotations

import unittest

from processing.settings import AUTHORITIES, CUSTOM_LICENCES
from processing.verify_licences import (
    _NO_LICENCE_EXPECTED,
    audit,
    declared_licences,
    seed_rows,
)

# Compliance flags a consumer acts on. `permits_commercial` / `no_derivatives`
# may be None where the source makes no grant either way (e.g. UN geodata);
# the others must be a definite boolean.
_TRISTATE_FLAGS = ("permits_commercial", "no_derivatives")
_BOOL_FLAGS = ("share_alike", "attribution_required", "custom")


class TestDeclaredLicences(unittest.TestCase):
    def test_every_authority_declares_a_licence(self):
        """Bespoke terms are still terms — record them under a custom-* id.

        ``kain_par`` used to carry ``license_spdx: ''``, which the push filters
        out as falsy, so the most restricted authority WHG holds reached the
        registry with no terms at all.
        """
        declared = declared_licences()
        missing = [
            a["namespace"] for a in AUTHORITIES
            if a.get("namespace")
            and a["namespace"] not in _NO_LICENCE_EXPECTED
            and a["namespace"] not in declared
        ]
        self.assertEqual([], missing)

    def test_custom_ids_are_all_defined(self):
        undefined = sorted(
            spdx for spdx in declared_licences().values()
            if spdx.startswith("custom-") and spdx not in CUSTOM_LICENCES
        )
        self.assertEqual([], undefined)

    def test_custom_definitions_are_complete(self):
        for spdx, defn in CUSTOM_LICENCES.items():
            with self.subTest(spdx=spdx):
                self.assertTrue(defn.get("label"))
                self.assertTrue(defn.get("url"))
                for flag in _TRISTATE_FLAGS:
                    self.assertIn(flag, defn)
                    self.assertIn(defn[flag], (True, False, None))
                for flag in _BOOL_FLAGS:
                    self.assertIsInstance(defn.get(flag), bool)
                self.assertTrue(defn["custom"], "custom-* rows must set custom=True")

    def test_seed_rows_cover_every_custom_id_in_use(self):
        used = {s for s in declared_licences().values() if s.startswith("custom-")}
        self.assertEqual(used, {r["spdx_id"] for r in seed_rows()})

    def test_seed_rows_are_not_contributor_selectable(self):
        """A custom-* id records ONE named source's terms — never something a
        WHG contributor should be offered for their own dataset (place#157).

        Pinned on the emitted rows, not the definitions, so a future id that
        simply omits the key still ships as non-selectable rather than
        defaulting to selectable registry-side.
        """
        for row in seed_rows():
            with self.subTest(spdx=row["spdx_id"]):
                self.assertIs(False, row["contributor_selectable"])

    def test_no_derivatives_is_explicit_on_every_custom_row(self):
        """whg3 stores `no_derivatives` rather than deriving it from the id, so
        our value is authoritative — an omission would silently become
        "see terms" for a licence that has a definite answer."""
        for row in seed_rows():
            with self.subTest(spdx=row["spdx_id"]):
                self.assertIn("no_derivatives", row)

    def test_ohm_is_cc0_not_odbl(self):
        """OHM publishes under CC0; only OSM is ODbL.

        Re-verified 2026-07-29 against openhistoricalmap.org/copyright. The
        free-text ``citation`` blob (which the registry stores as the gazetteer
        description) had been copied from the OSM row and claimed ODbL —
        contradicting the licence field and prompting a "correction" request.
        """
        ohm = next(a for a in AUTHORITIES if a.get("namespace") == "ohm")
        self.assertEqual("CC0-1.0", ohm["license_spdx"])
        self.assertNotIn("ODbL", ohm["citation"])
        self.assertNotIn("Open Database License", ohm["citation"])


class TestAudit(unittest.TestCase):
    """``audit`` compares the live resolver's answer with what we declare."""

    def _resolved(self, **per_ns):
        return {"sources": {ns: v for ns, v in per_ns.items()}}

    def _clean_sources(self):
        """A resolver payload where every authority matches what we declare."""
        return self._resolved(**{
            ns: {"license": {"spdx_id": spdx}}
            for ns, spdx in declared_licences().items()
        })

    def test_clean_registry_reports_nothing(self):
        self.assertEqual([], audit(self._clean_sources()))

    def test_null_licence_is_reported_missing(self):
        payload = self._clean_sources()
        payload["sources"]["chgis"] = {"license": None}
        problems = audit(payload)
        self.assertEqual(
            [("chgis", "missing")],
            [(p["namespace"], p["kind"]) for p in problems],
        )

    def test_stale_row_is_reported_mismatch(self):
        """The `un` case: the push skipped our id, so a Natural-Earth-era
        ``custom-public-domain`` row survived and asserts a grant the UN never made."""
        payload = self._clean_sources()
        payload["sources"]["un"] = {"license": {"spdx_id": "custom-public-domain"}}
        problems = audit(payload)
        self.assertEqual(1, len(problems))
        self.assertEqual("un", problems[0]["namespace"])
        self.assertEqual("mismatch", problems[0]["kind"])
        self.assertIn("custom-public-domain", problems[0]["detail"])

    def test_absent_namespace_is_reported(self):
        payload = self._clean_sources()
        payload["sources"].pop("ukhc")
        problems = audit(payload)
        self.assertEqual(
            [("ukhc", "absent")],
            [(p["namespace"], p["kind"]) for p in problems],
        )

    def test_relations_only_namespace_is_exempt(self):
        """``loc`` contributes relations, not records — nothing to licence."""
        payload = self._clean_sources()
        payload["sources"].pop("loc", None)
        self.assertEqual([], [p for p in audit(payload) if p["namespace"] == "loc"])


if __name__ == "__main__":
    unittest.main()
