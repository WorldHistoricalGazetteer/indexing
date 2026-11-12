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

⚠️ **This file does not run under ``unittest discover -s tests``.** That form
puts ``tests/`` on ``sys.path`` and imports each module top-level, so the
package ``__init__`` is skipped entirely and the sandbox is never installed —
which on 2026-08-07 let a suite run overwrite the real geom store and staged
snapshots. Any test that writes through ``processing.settings`` must therefore
also call :func:`tests._sandbox.assert_sandboxed` in ``setUpClass``; that check
reads the paths as they actually resolved and fails loudly instead of writing
to shared storage. See ``tests/_sandbox.py`` for the full account.
"""

from __future__ import annotations

import os

from ._sandbox import install

install()

# The reconcile endpoint now has a per-process result cache (gateway/result_cache.py).
# Many tests call it repeatedly with identical requests and expect each call to reach
# the (stubbed) Elasticsearch, so a shared module-level cache would make them
# order-dependent. Off for the suite; tests/test_reconcile_result_cache.py installs
# its own cache explicitly.
os.environ.setdefault("GATEWAY_RESULT_CACHE_SIZE", "0")
