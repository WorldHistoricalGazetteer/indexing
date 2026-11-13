"""Refuse heavy in-place rewrites of the LIVE index while Elasticsearch runs the small serving heap.

The production node normally runs a ~20 GB *serving* heap (more page cache for search). Big
rewrites create HNSW merge pressure on the ~73M-document toponyms index; with a small heap that
has run out of memory before. Raise the heap first (``es -heap rebuild`` then ``es-restart``) and
put it back afterwards (``es -heap serving``).

Override with ``ES_REBUILD_HEAP_MIN_GB`` (default 26) or, knowingly, ``ES_ALLOW_SMALL_HEAP=1``.
"""
from __future__ import annotations

import os
import sys

DEFAULT_MIN_GB = 26.0


def require_rebuild_heap(sess, es_url: str) -> None:
    """Exit(2) if the node's max heap is below the rebuild threshold. ``sess`` is a requests session."""
    if os.environ.get("ES_ALLOW_SMALL_HEAP") == "1":
        print("heap guard: ES_ALLOW_SMALL_HEAP=1, skipping", flush=True)
        return
    need = float(os.environ.get("ES_REBUILD_HEAP_MIN_GB", DEFAULT_MIN_GB))
    r = sess.get(f"{es_url}/_nodes/jvm", timeout=30)
    r.raise_for_status()
    have = min(n["jvm"]["mem"]["heap_max_in_bytes"] for n in r.json()["nodes"].values()) / 2**30
    if have < need:
        sys.exit(f"heap guard: Elasticsearch heap is {have:.0f} GB, below the {need:.0f} GB needed for a "
                 f"bulk rewrite of the live index.\n  Run `es -heap rebuild` then `es-restart` "
                 f"(a short search outage), re-run this, then `es -heap serving` and `es-restart` again.\n"
                 f"  See staff/gateway-operations in the documentation.")
    print(f"heap guard: heap {have:.0f} GB >= {need:.0f} GB, ok", flush=True)
