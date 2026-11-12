"""The serving process caps OpenMP/MKL threads at one (see gateway/threads.py).

Measured on production, this took sustained reconcile throughput from ~3.4 to
~11 queries/second on the same host. A regression here is silent — nothing
fails, the service just gets ~3x slower under load — so it is pinned.

``python -m gateway`` cannot be imported in a test (it starts uvicorn), so the
wiring is checked from the AST: the cap must be called, and called before
``uvicorn`` is imported and before ``uvicorn.run``.
"""

from __future__ import annotations

import ast
import os
import pathlib
import unittest
from unittest import mock

from gateway import threads


class CapCpuThreads(unittest.TestCase):
    def test_defaults_to_one_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            threads.cap_cpu_threads()
            self.assertEqual(os.environ["OMP_NUM_THREADS"], "1")
            self.assertEqual(os.environ["MKL_NUM_THREADS"], "1")

    def test_an_explicit_environment_value_wins(self):
        with mock.patch.dict(os.environ, {"OMP_NUM_THREADS": "4"}, clear=True):
            threads.cap_cpu_threads()
            self.assertEqual(os.environ["OMP_NUM_THREADS"], "4")
            self.assertEqual(os.environ["MKL_NUM_THREADS"], "1")


class WiredIntoTheEntrypointFirst(unittest.TestCase):
    def test_cap_runs_before_uvicorn_is_imported_or_started(self):
        src = pathlib.Path(__file__).resolve().parent.parent / "gateway" / "__main__.py"
        body = ast.parse(src.read_text(encoding="utf-8")).body

        def pos(pred):
            return next((i for i, n in enumerate(body) if pred(n)), None)

        call = pos(lambda n: isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                   and getattr(n.value.func, "id", None) == "cap_cpu_threads")
        imp = pos(lambda n: isinstance(n, ast.Import)
                  and any(a.name == "uvicorn" for a in n.names))
        run = pos(lambda n: isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                  and getattr(n.value.func, "attr", None) == "run")
        self.assertIsNotNone(call, "__main__ no longer calls cap_cpu_threads()")
        self.assertLess(call, imp, "thread cap must precede `import uvicorn`")
        self.assertLess(call, run, "thread cap must precede uvicorn.run")

    def test_the_package_init_does_not_apply_it(self):
        """Batch jobs import gateway.* and want every core."""
        init = pathlib.Path(__file__).resolve().parent.parent / "gateway" / "__init__.py"
        self.assertNotIn("cap_cpu_threads", init.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
