# gateway/result_cache.py
"""A small in-process TTL + LRU cache for reconcile responses.

Why: genealogy and archival batches repeat place strings heavily, and a repeated
request recomputes everything (embedding, five Elasticsearch searches). A cache hit
costs microseconds. Measured on production, an unscoped fuzzy query costs ~0.2-0.6 s
of mostly Elasticsearch time, and Elasticsearch is the capacity ceiling.

Deliberately simple and conservative:

* PER WORKER PROCESS. The gateway runs several uvicorn workers; each has its own
  cache, so a repeat lands on the same worker only part of the time. No shared
  state, no new dependency, nothing to keep consistent.
* SHORT TTL (default 120 s) bounds staleness after an index or overlay change.
  A dataset published and reconciled against within the TTL can see the earlier
  answer. Set ``GATEWAY_RESULT_CACHE_TTL`` lower, or ``GATEWAY_RESULT_CACHE_SIZE=0``
  to disable.
* BOUNDED (default 2048 entries) with least-recently-used eviction.
* The caller decides what is cacheable; this class only stores. Degraded answers
  (an enrichment failure, a missing KNN pass, a scope that could not be applied)
  must never be stored, or a transient fault would outlive itself by the TTL.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

# Importing config loads .env then .env.local, so GATEWAY_RESULT_CACHE_* set there are
# visible below. Without this the order of imports in reconcile.py decided whether a
# per-host override took effect.
from . import config  # noqa: F401


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


RESULT_CACHE_SIZE = int(_env_number("GATEWAY_RESULT_CACHE_SIZE", 2048))
RESULT_CACHE_TTL = _env_number("GATEWAY_RESULT_CACHE_TTL", 120.0)


class TTLCache:
    """Thread-safe LRU with a per-entry time-to-live. ``max_entries <= 0`` disables it."""

    def __init__(self, max_entries: int, ttl: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_entries = int(max_entries)
        self.ttl = float(ttl)
        self._clock = clock
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = self.expired = 0

    @property
    def enabled(self) -> bool:
        return self.max_entries > 0 and self.ttl > 0

    def get(self, key: str) -> Optional[Any]:
        if not self.enabled:
            return None
        now = self._clock()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            stored_at, value = item
            if now - stored_at > self.ttl:
                del self._data[key]
                self.expired += 1
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
                self.evictions += 1

    def __len__(self) -> int:
        return len(self._data)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"entries": len(self._data), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else None,
                "evictions": self.evictions, "expired": self.expired,
                "max_entries": self.max_entries, "ttl_s": self.ttl}
