import pytest
from processing.heap_guard import require_rebuild_heap


class _R:
    def __init__(self, gb): self._gb = gb
    def raise_for_status(self): pass
    def json(self): return {"nodes": {"n": {"jvm": {"mem": {"heap_max_in_bytes": int(self._gb * 2**30)}}}}}


class _S:
    def __init__(self, gb): self.gb = gb
    def get(self, *a, **k): return _R(self.gb)


def test_small_heap_refused(monkeypatch):
    monkeypatch.delenv("ES_ALLOW_SMALL_HEAP", raising=False)
    with pytest.raises(SystemExit) as e:
        require_rebuild_heap(_S(20), "http://x")
    assert "es -heap rebuild" in str(e.value)


def test_large_heap_allowed(monkeypatch):
    monkeypatch.delenv("ES_ALLOW_SMALL_HEAP", raising=False)
    require_rebuild_heap(_S(28), "http://x")


def test_override_allows_small(monkeypatch):
    monkeypatch.setenv("ES_ALLOW_SMALL_HEAP", "1")
    require_rebuild_heap(_S(8), "http://x")


def test_threshold_env(monkeypatch):
    monkeypatch.delenv("ES_ALLOW_SMALL_HEAP", raising=False)
    monkeypatch.setenv("ES_REBUILD_HEAP_MIN_GB", "10")
    require_rebuild_heap(_S(12), "http://x")
