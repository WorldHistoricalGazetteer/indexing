"""A full rebuild must target staging, and promotion must move both indices.

Two rules this pins, both learned the expensive way:

1. ``index_from_stage`` refuses a production host. A full rebuild takes hours;
   pointed at the live cluster it would serve a half-loaded index under the
   ``places`` alias for all of them. ES_HOST is unset on the production VM, so
   the natural thing to type when a tool says "no host resolved" is
   ``http://localhost:9201`` — which is production.

2. ``promote_to_production`` swaps every alias in ONE ``_aliases`` request.
   ``places`` and ``toponyms`` reference each other (a toponym's
   ``attestations[]`` are place_ids); a non-atomic swap, or one that moves a
   single alias, silently drops the hits whose ids exist on only one side.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class ProductionHostClassification(unittest.TestCase):

    def test_localhost_counts_as_production(self):
        from processing.settings import is_production_host
        # ES binds to localhost on the production VM, so an unqualified
        # localhost URL is production, not a harmless default.
        self.assertTrue(is_production_host("http://localhost:9201"))
        self.assertTrue(is_production_host("http://127.0.0.1:9200"))

    def test_compute_node_is_not_production(self):
        from processing.settings import is_production_host
        # Staging runs on an ephemeral Slurm node.
        self.assertFalse(is_production_host("http://smp-n217:9201"))
        self.assertFalse(is_production_host("http://htc-n42:9201"))

    def test_empty_is_not_production(self):
        from processing.settings import is_production_host
        self.assertFalse(is_production_host(None))
        self.assertFalse(is_production_host(""))


class IndexFromStageGuard(unittest.TestCase):

    def test_guard_present_and_gated(self):
        src = (REPO / "processing" / "index_from_stage.py").read_text()
        self.assertIn("is_production_host(args.es_host)", src,
                      "index_from_stage must classify its --es-host")
        self.assertIn("--allow-production", src,
                      "the guard needs a deliberate override, not no override")

    def test_guard_runs_before_any_indexing(self):
        """The check must precede the work, not follow it."""
        src = (REPO / "processing" / "index_from_stage.py").read_text()
        guard = src.index("is_production_host(args.es_host)")
        # `run_index` / `bulk` are where documents actually start moving.
        for marker in ("def main()",):
            self.assertLess(src.index(marker), guard,
                            "guard should live inside main()")
        tail = src[guard:]
        self.assertIn("sys.exit(2)", tail.split("\n\n")[0] + tail[:400],
                      "the guard must exit, not merely warn")


class PromotionAtomicity(unittest.TestCase):

    def setUp(self):
        self.src = (REPO / "processing" / "promote_to_production.py").read_text()
        self.tree = ast.parse(self.src)

    def _func(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"{name}() not found")

    def test_single_update_aliases_call(self):
        """One request for all aliases — ES applies an _aliases body atomically."""
        calls = [n for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "update_aliases"]
        self.assertEqual(len(calls), 1,
                         "exactly one update_aliases call: several calls are "
                         "several atomic operations, which is not atomic")

    def test_swap_is_not_reached_when_verification_fails(self):
        """Verification failure must stop the promotion, not warn past it."""
        main = self._func("main")
        body = ast.get_source_segment(self.src, main)
        verify_at = body.index("ok, lines = verify(")
        swap_at = body.index("swap_aliases(")
        between = body[verify_at:swap_at]
        self.assertIn("raise SystemExit", between,
                      "a failed verify must abort before the alias swap")

    def test_both_indices_default(self):
        self.assertIn('DEFAULT_ALIASES = ("places", "toponyms")', self.src,
                      "both indices are promoted together by default")

    def test_pipeline_presence_is_verified(self):
        """A restore does not recreate ingest pipelines; writes then 400."""
        self.assertIn("extract_namespace", self.src)
        verify = ast.get_source_segment(self.src, self._func("verify"))
        self.assertIn("get_pipeline", verify)


class RecoveryWaitIsNotFooledByAQuietStart(unittest.TestCase):
    """`restore(wait_for_completion=False)` returns BEFORE recovery starts.

    Treating "0 active recoveries" as "finished" exits that window instantly.
    It did: the promotion reported "recovery complete (yellow)" with prod at
    180M/361M places and 18M/72M toponyms, then aborted on a 503 from counting
    shards that were not yet searchable.
    """

    def setUp(self):
        from pathlib import Path
        self.src = Path("processing/promote_to_production.py").read_text()
        i = self.src.index("def wait_recovery(")
        self.body = self.src[i:self.src.index("\ndef ", i + 10)]

    def test_checks_shard_states_not_just_active_recoveries(self):
        for field in ("initializing_shards", "relocating_shards",
                      "unassigned_shards"):
            self.assertIn(field, self.body,
                          "shard state is the evidence; an empty recovery list "
                          "is not")

    def test_requires_the_quiet_state_to_persist(self):
        self.assertIn("stable_polls", self.body,
                      "one quiet poll can just mean recovery has not begun")

    def test_proves_it_by_counting(self):
        """The count is what the next stage does, so it is the real proof."""
        self.assertIn("prod.count(index=index)", self.body)

    def test_count_tolerates_transient_503(self):
        i = self.src.index("def doc_count(")
        body = self.src[i:self.src.index("\ndef ", i + 10)]
        self.assertIn("attempts", body,
                      "a fresh restore emits transient 503s; aborting the "
                      "promotion on one leaves a half-verified restore")

if __name__ == "__main__":
    unittest.main()
