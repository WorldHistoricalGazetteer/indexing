"""A full toponyms rebuild must refuse an alias target at STARTUP, not at STEP 4.

THE DEFECT THIS PINS. ``rebuild_toponyms_index`` STEP 4 deletes and recreates
``--toponyms-index``, which defaults to ``toponyms``. In production that name is
an ALIAS (measured 6 Sep 2026: ``toponyms`` → ``toponyms_temporal-20260731t160000z``,
72,703,777 docs), and ``scripts/symphonym.sh``'s full-rebuild invocation does not
override the default. Both halves were measured against the live cluster:

  * ``HEAD /toponyms`` → 200, so ``indices.exists`` is true and the delete
    branch is entered;
  * ``DELETE /<alias>`` → 400 ``illegal_argument_exception`` in ES 9.0.0, and
    the concrete index survives.

So this is not a data-loss bug — it is a FAIL-LATE bug, which is worse than it
sounds: the run dies at the final step having already scanned 51.2M places,
built the vocabulary and computed IPA + PanPhon embeddings. Hours discarded for
a condition knowable before any work starts.

The test drives the guard with a fake client rather than a live cluster, so it
pins the decision (alias → refuse, concrete → proceed, partial-update → exempt)
and not the wiring.
"""
import argparse
import unittest

from phonetics.extraction.rebuild_toponyms_index import _assert_rebuild_target_is_concrete


class FakeES:
    """get_alias raises for a concrete name, as the real client does on 404."""

    def __init__(self, aliases):
        self._aliases = aliases

    def indices_get_alias(self, name):        # pragma: no cover - shim
        raise NotImplementedError

    @property
    def indices(self):
        outer = self

        class _I:
            @staticmethod
            def get_alias(name):
                if name in outer._aliases:
                    return {outer._aliases[name]: {"aliases": {name: {}}}}
                raise RuntimeError("index_not_found_exception")

        return _I()


def _args(**kw):
    d = dict(toponyms_index="toponyms", partial_update=False, skip_es_index=False)
    d.update(kw)
    return argparse.Namespace(**d)


class RebuildTargetTest(unittest.TestCase):

    LIVE = {"toponyms": "toponyms_temporal-20260731t160000z"}

    def test_alias_target_exits_before_any_work(self):
        with self.assertRaises(SystemExit) as cm:
            _assert_rebuild_target_is_concrete(FakeES(self.LIVE), _args())
        self.assertEqual(cm.exception.code, 1)

    def test_concrete_dated_target_is_allowed(self):
        _assert_rebuild_target_is_concrete(
            FakeES(self.LIVE), _args(toponyms_index="toponyms_tgn-20260906"))

    def test_partial_update_may_target_the_alias(self):
        """--partial-update never deletes, so aiming it at the alias is right."""
        _assert_rebuild_target_is_concrete(
            FakeES(self.LIVE), _args(partial_update=True))

    def test_staged_only_run_is_exempt(self):
        _assert_rebuild_target_is_concrete(
            FakeES(self.LIVE), _args(skip_es_index=True))


if __name__ == "__main__":
    unittest.main()
