"""Restricted content must not enter this repository.

Some material is cleared for acquisition and research use only, and NOT for
publication or redistribution. ``LICENCE.md`` says so; this test is what makes
the statement true. **An exclusion that exists only in prose is honoured until
the first person who has not read it.**

⚠ The allowlist is deliberately tiny and enumerated. Files that *discuss* the
policy must name the restricted material, so they are exempted individually and
adding to that list shows up in a diff. If you find yourself extending it, ask
whether you are exempting a discussion or smuggling the data.

⚠ And this module tests its own detector against synthetic bad input. A guard
that has never been shown to fail is not a guard -- it is a decoration, and this
project has built several of those.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Path fragments that should never name a tracked file.
RESTRICTED_PATH_PATTERNS = [
    re.compile(r"lhpn", re.I),
    re.compile(r"rcahmw", re.I),
    re.compile(r"long[-_ ]held[-_ ]place[-_ ]names", re.I),
]

# Text that would only appear inside the restricted data or its own notice.
RESTRICTED_CONTENT_MARKERS = [
    re.compile(r"not\s+cleared\s+for\s+publication", re.I),
    re.compile(r"RCAHMW.{0,80}redistribut", re.I | re.S),
]

# Files that legitimately DISCUSS the policy. Enumerated, not pattern-matched.
ALLOWLIST = {
    "LICENCE.md",
    "tests/test_no_restricted_content.py",
}

TEXT_SUFFIXES = {".md", ".py", ".txt", ".json", ".csv", ".tsv", ".yaml", ".yml",
                 ".sh", ".sbatch", ".slurm", ".cfg", ".ini", ".toml"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


class TestNoRestrictedContent(unittest.TestCase):

    def test_no_restricted_paths(self):
        offenders = [
            p for p in tracked_files()
            if p not in ALLOWLIST
            and any(rx.search(p) for rx in RESTRICTED_PATH_PATTERNS)
        ]
        self.assertEqual(
            [], offenders,
            "Restricted material must not be committed. See LICENCE.md. "
            f"Offending paths: {offenders}")

    def test_no_restricted_content(self):
        offenders = []
        for rel in tracked_files():
            if rel in ALLOWLIST:
                continue
            path = REPO / rel
            if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for rx in RESTRICTED_CONTENT_MARKERS:
                if rx.search(text):
                    offenders.append((rel, rx.pattern))
                    break
        self.assertEqual(
            [], offenders,
            "Restricted material must not be committed. See LICENCE.md. "
            f"Offending files: {offenders}")

    # -- the detector must be able to fail -------------------------------

    def test_detector_fires_on_a_restricted_path(self):
        """A guard nobody has seen fail is a decoration."""
        for candidate in ("data/lhpn-pairs.csv", "RCAHMW_export.json",
                          "developer/long-held-place-names.md"):
            self.assertTrue(
                any(rx.search(candidate) for rx in RESTRICTED_PATH_PATTERNS),
                f"detector missed a restricted path: {candidate}")

    def test_detector_fires_on_restricted_content(self):
        sample = ("This dataset is supplied by RCAHMW and is not cleared for "
                  "publication or redistribution outside the project.")
        self.assertTrue(
            any(rx.search(sample) for rx in RESTRICTED_CONTENT_MARKERS),
            "detector missed restricted content it was written to catch")

    def test_allowlist_stays_small(self):
        """Growth here is how a guard is quietly defeated."""
        self.assertLessEqual(
            len(ALLOWLIST), 4,
            "The allowlist has grown. Each entry must be a file that DISCUSSES "
            "the policy, never one that contains restricted data.")


if __name__ == "__main__":
    unittest.main()
