"""Unit tests for the authority download-legality + volume gating (place#135).

Covers ``processing.push_gazetteer_inventory._download_fields`` — the combination
of the explicit ``redistributable`` legal flag with the ``DOWNLOAD_MAX_RECORDS``
volume cap into the effective ``downloadable`` flag sent to the registry.
"""

from __future__ import annotations

import unittest

from processing.push_gazetteer_inventory import _download_fields
from processing.settings import DOWNLOAD_MAX_RECORDS


class TestDownloadFields(unittest.TestCase):
    def test_open_and_small_is_downloadable(self):
        f = _download_fields({"redistributable": True}, 1000)
        self.assertTrue(f["redistributable"])
        self.assertTrue(f["downloadable"])
        self.assertNotIn("download_blocked_reason", f)

    def test_licence_restricted_blocks_download(self):
        f = _download_fields({"redistributable": False}, 1000)
        self.assertFalse(f["redistributable"])
        self.assertFalse(f["downloadable"])
        self.assertEqual(f["download_blocked_reason"], "licence-restricted")

    def test_volume_cap_blocks_open_dataset(self):
        f = _download_fields({"redistributable": True}, DOWNLOAD_MAX_RECORDS + 1)
        self.assertTrue(f["redistributable"])
        self.assertFalse(f["downloadable"])
        self.assertEqual(f["download_blocked_reason"], "volume-exceeds-cap")

    def test_at_cap_is_downloadable(self):
        f = _download_fields({"redistributable": True}, DOWNLOAD_MAX_RECORDS)
        self.assertTrue(f["downloadable"])

    def test_unset_defaults_to_open(self):
        f = _download_fields({}, 100)
        self.assertTrue(f["redistributable"])
        self.assertTrue(f["downloadable"])

    def test_licence_restriction_takes_precedence_over_volume(self):
        # Both restricted and oversized -> reason names the licence.
        f = _download_fields({"redistributable": False}, DOWNLOAD_MAX_RECORDS + 1)
        self.assertEqual(f["download_blocked_reason"], "licence-restricted")

    def test_booleans_always_present(self):
        # False booleans must be emitted (they bypass the falsy attribution filter).
        f = _download_fields({"redistributable": False}, 10)
        self.assertIn("redistributable", f)
        self.assertIn("downloadable", f)


if __name__ == "__main__":
    unittest.main()
