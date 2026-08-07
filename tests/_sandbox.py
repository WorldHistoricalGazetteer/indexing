"""Test sandbox: redirect the pipeline's real paths into a tempdir, and — more
importantly — **prove** the redirect actually took effect before a test writes.

Why the assertion exists as well as the redirect
------------------------------------------------
``tests/__init__.py`` installs sandbox paths via ``os.environ.setdefault`` before
any test imports ``processing.settings``. That works when tests are run
package-qualified::

    python -m unittest tests.test_update_merge        # __init__.py RUNS

It does **not** work under discovery::

    python -m unittest discover -s tests              # __init__.py NEVER RUNS

``discover -s tests`` puts ``tests/`` on ``sys.path`` and imports each module
top-level (``test_update_merge``, not ``tests.test_update_merge``), so the
package ``__init__`` is never executed, the sandbox is never installed, and
``processing.settings`` resolves to the real ``/vast/ishi/...`` locations. Every
test that writes then writes to production.

That is not hypothetical. On 2026-08-07 a ``discover -s tests`` run on the
indexing host stubbed the ``gn`` and ``wd`` staged snapshots, truncated
``/vast/ishi/geom/index.sqlite`` to the two synthetic features built by
``test_staged_pipeline_e2e``, and overwrote ``geom_shard_0001.bin`` with 186
bytes — blocking the tileset rebuild until the store was repaired.

So the redirect is best-effort and the **assertion is the guarantee**: it reads
the paths as they actually resolved and refuses to let the test proceed if they
point anywhere real. A loud failure at setUpClass is always better than a silent
write to shared storage.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# One sandbox per interpreter, created lazily so merely importing this module
# from a non-writing test costs nothing surprising.
_SANDBOX: str | None = None

# Roots that must never appear in a resolved test path. These are the shared
# filesystems on the CRC hosts; a path under any of them means the redirect
# did not take effect.
_FORBIDDEN_ROOTS = ("/vast/", "/ix1/", "/ix3/", "/bgfs/")

_SAFE_INVOCATION = (
    "Run the suite package-qualified so tests/__init__.py executes:\n"
    "    python -m unittest tests.test_module\n"
    "    python -m unittest discover -s tests -t .   # -t . keeps the package\n"
    "or export the sandbox yourself before running anything:\n"
    "    export STAGED_BASE_DIR=$(mktemp -d) GEOM_STORE_DIR=$(mktemp -d)"
)


def install() -> str:
    """Point the pipeline's path settings at a per-interpreter tempdir.

    Idempotent, and uses ``setdefault`` so an explicitly-exported sandbox (or a
    test that manages its own) wins. Must run *before* ``processing.settings``
    is first imported to have any effect — which is exactly the fragility
    :func:`assert_sandboxed` exists to catch.
    """
    global _SANDBOX
    if _SANDBOX is None:
        _SANDBOX = tempfile.mkdtemp(prefix="whg-tests-")
    os.environ.setdefault("STAGED_BASE_DIR", os.path.join(_SANDBOX, "staged"))
    os.environ.setdefault("STAGED_RUNS_DIR",
                          os.path.join(_SANDBOX, "staged", "runs"))
    os.environ.setdefault("GEOM_STORE_DIR", os.path.join(_SANDBOX, "geom"))
    # Consolidation copies index.sqlite here. Unsandboxed it defaults under
    # IX1_BASE, so a test that consolidates would drop files on real storage.
    os.environ.setdefault("GEOM_STORE_BACKUP_DIR",
                          os.path.join(_SANDBOX, "geom-backups"))
    os.environ.setdefault("NAMESPACE_RUNTIME_HISTORY_FILE",
                          os.path.join(_SANDBOX, "namespace-runtime-history.json"))
    os.environ.setdefault("ES_HOST", "")
    return _SANDBOX


def assert_sandboxed() -> None:
    """Refuse to continue if the pipeline paths resolved to real storage.

    Call this from ``setUpClass`` of any test that writes through
    ``processing.settings`` rather than through its own ``mock.patch``. Reads
    the settings module as the test will actually see it, so it catches the
    case where an earlier import froze the real paths in place.
    """
    from processing.settings import (  # imported late: reads env at import time
        GEOM_STORE_BACKUP_DIR, GEOM_STORE_DIR, STAGED_BASE_DIR, STAGED_RUNS_DIR,
    )
    offenders = []
    for name, value in (("STAGED_BASE_DIR", STAGED_BASE_DIR),
                        ("STAGED_RUNS_DIR", STAGED_RUNS_DIR),
                        ("GEOM_STORE_DIR", GEOM_STORE_DIR),
                        ("GEOM_STORE_BACKUP_DIR", GEOM_STORE_BACKUP_DIR)):
        if value is None:
            continue
        resolved = str(Path(value).resolve())
        if any(resolved.startswith(root) for root in _FORBIDDEN_ROOTS):
            offenders.append(f"  {name} = {resolved}")
    if offenders:
        raise RuntimeError(
            "REFUSING TO RUN: this test writes through processing.settings, "
            "but those settings point at REAL shared storage:\n"
            + "\n".join(offenders)
            + "\n\nThe tests/__init__.py sandbox did not take effect — almost "
              "certainly because the suite was started with "
              "`unittest discover -s tests`, which imports test modules "
              "top-level and never executes the package __init__.\n\n"
            + _SAFE_INVOCATION
        )
