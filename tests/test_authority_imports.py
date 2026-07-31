"""Every authority script in INGESTION_ORDER must have resolvable imports.

`ukhc-places.py` shipped for a day with ``from processing.temporal import
lifespan, AUTHORITIES`` — ``AUTHORITIES`` lives in ``processing.settings``. It
raised ImportError on the first line of the module, so the failure surfaced
only when the rebuild actually submitted the job, behind two unrelated
infrastructure faults that had been masking it in earlier rounds.

Nothing else catches this: the module is never imported by anything (it runs as
``python -m authorities.<name>``), so neither the test suite nor an editor's
import resolution would have touched it.

Checked on the AST rather than by executing the modules, so it runs anywhere:
several authority scripts need third-party packages that are only installed on
the CRC compute nodes (``osmium`` for osm/ohm), and several do real work at
import time.
"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

from processing.ingest_all_authorities import INGESTION_ORDER

_REPO = Path(__file__).resolve().parent.parent
_AUTHORITIES = _REPO / "authorities"


def _module_path(script_name: str) -> Path:
    """``geonames-places`` → authorities/geonames-places.py; ``chgis.places`` → chgis/places.py."""
    return _AUTHORITIES / (script_name.replace(".", "/") + ".py")


class AuthorityScriptImportTests(unittest.TestCase):
    def test_every_ingestion_script_exists(self):
        missing = [
            f"{ns}:{script}"
            for ns, script, _desc, _sid in INGESTION_ORDER
            if not _module_path(script).is_file()
        ]
        self.assertEqual(missing, [], f"INGESTION_ORDER names absent scripts: {missing}")

    def test_every_first_party_import_resolves(self):
        """`from processing.X import a, b` — X must import and expose a and b."""
        failures: list[str] = []
        for ns, script, _desc, _sid in INGESTION_ORDER:
            path = _module_path(script)
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level:
                    continue
                module = node.module or ""
                if not module.startswith(("processing", "typesystem", "phonetics")):
                    continue
                try:
                    imported = importlib.import_module(module)
                except Exception as exc:  # pragma: no cover - a broken module is the finding
                    failures.append(f"{path.name}:{node.lineno} cannot import {module}: {exc}")
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if not hasattr(imported, alias.name):
                        failures.append(
                            f"{path.name}:{node.lineno} imports '{alias.name}' "
                            f"from {module}, which does not define it"
                        )
        self.assertEqual(failures, [], "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
