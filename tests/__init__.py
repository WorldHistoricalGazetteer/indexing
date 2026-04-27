"""Test package init.

Sets safe sandbox paths for ``STAGED_BASE_DIR`` / ``GEOM_STORE_DIR`` /
``STAGED_RUNS_DIR`` / ``NAMESPACE_RUNTIME_HISTORY_FILE`` *before* any test
module imports ``processing.settings``. This isolates tests from the real
``/vast/...`` and ``/ix1/...`` paths and prevents test-discovery order
from flaking when multiple test modules touch the staged pipeline.

Tests that need a fresh per-test sandbox can override these via env vars
inside their own ``setUpClass`` and reload the relevant settings — but the
package-level defaults below mean even a discovery-order import won't try
to mkdir ``/vast``.
"""

from __future__ import annotations

import os
import tempfile

_TEST_SANDBOX = tempfile.mkdtemp(prefix="whg-tests-")

os.environ.setdefault("STAGED_BASE_DIR", os.path.join(_TEST_SANDBOX, "staged"))
os.environ.setdefault("STAGED_RUNS_DIR",
                      os.path.join(_TEST_SANDBOX, "staged", "runs"))
os.environ.setdefault("GEOM_STORE_DIR", os.path.join(_TEST_SANDBOX, "geom"))
os.environ.setdefault("NAMESPACE_RUNTIME_HISTORY_FILE",
                      os.path.join(_TEST_SANDBOX, "namespace-runtime-history.json"))
os.environ.setdefault("ES_HOST", "")
