"""`authority-selection.md` must list every namespace the pipeline knows about.

Omission is not the same as an unticked box. `resolve_selected_authorities`
treats an absent entry as deselected, and `cleanup_deselected_staged_artefacts`
*deletes* the staged tree of anything deselected. So a namespace that is simply
never added to the file is dropped from the run **and** has its staged
artefacts removed, reported only as "Removed staged artefacts for deselected
authorities: …".

That is not hypothetical: the file drifted to 18 of 27 between May and July
2026 as `ofs`, `og`, `hgis`, `alc`, `kain_par` and the four `vob_*` gazetteers
were added incrementally without being registered here. A from-scratch
ingestion run — the default path, with no `-n` — would have quietly ingested 18
authorities and deleted nine staged trees.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from processing.ingest_all_authorities import INGESTION_ORDER
from processing.staging_contract import RELATIONS_ONLY_NAMESPACES
from processing.staging_orchestrator import (
    parse_authority_selection_file,
    resolve_selected_authorities,
)

_SELECTION_FILE = Path(__file__).resolve().parent.parent / "authority-selection.md"
_KNOWN = list(dict.fromkeys(ns for ns, *_ in INGESTION_ORDER))


class AuthoritySelectionCoverageTests(unittest.TestCase):
    def test_every_known_namespace_is_listed(self):
        listed = parse_authority_selection_file(_SELECTION_FILE)
        missing = [ns for ns in _KNOWN if ns not in listed]
        self.assertEqual(
            missing, [],
            f"absent from authority-selection.md, so a default run would drop them "
            f"AND delete their staged artefacts: {missing}",
        )

    def test_default_run_selects_every_namespace(self):
        """Guards the box state, not just the presence of a line."""
        selected = resolve_selected_authorities(_SELECTION_FILE, _KNOWN)
        self.assertEqual(
            sorted(selected), sorted(_KNOWN),
            "authority-selection.md does not currently select the full corpus; "
            "if that is deliberate for a scoped run, revert it before committing",
        )

    def test_no_listed_namespace_is_unknown(self):
        """A typo'd or retired entry should not sit here looking meaningful."""
        listed = parse_authority_selection_file(_SELECTION_FILE)
        # Two legitimate kinds of entry are absent from INGESTION_ORDER: WHG
        # sub-namespaces (`whg:892`), discovered remotely at run time, and
        # relations-only sources (`loc`), which contribute no place records and
        # are consumed by the Batch 12 hard-link harvest instead.
        unknown = [
            ns for ns in listed
            if ns not in _KNOWN
            and not ns.startswith("whg:")
            and ns not in RELATIONS_ONLY_NAMESPACES
        ]
        self.assertEqual(unknown, [], f"unknown entries in authority-selection.md: {unknown}")


if __name__ == "__main__":
    unittest.main()
