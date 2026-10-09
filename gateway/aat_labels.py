# gateway/aat_labels.py
"""
Per-worker cache of AAT concept labels (``aat_id -> term``) from the ``types``
index, used to label ``facets.aat_types`` on /api/search.

Until 2026-10-09 every search resolved its facet labels with a fresh 10-second
``terms`` lookup against ``types``, and *any* failure of that lookup (timeout,
ES busy, a 5xx) silently returned ``{}`` — so every AAT facet in that response
was labelled with its bare numeric id, exactly as if the concept had no label.
The two cases were indistinguishable in the log and in the response.

Measured live the same day: 1,089 distinct AAT ids occur on ``places`` and
1,084 of them have a ``types`` doc with a non-empty ``term``; only 5 (41 of
3,153,048 nested type entries) are genuinely unlabelled. So nearly every bare id
a user saw was a *lookup failure*, not missing data.

How it works now:

* **Memoised lookups.** A label, once read, is kept for the life of the worker
  (the ``types`` index only changes on a rebuild, which restarts nothing here
  but also renames nothing). Only ids not yet seen are looked up, so after a
  few searches nearly every request is served with no ES round trip at all.
  A failed lookup is retried once.
* **Background warm-up.** At startup each worker also loads the whole index
  (58,996 docs on 2026-10-09 — the "~5.8k" in older notes is the place-type
  subset) in the background. On the live cluster that read takes ~25 s
  (~4 s per 10k page), so requests NEVER wait for it — they use whatever is
  cached plus a targeted lookup.
* **Two distinct log lines.** ``lookup FAILED`` means the types index could not
  be read (labels unknown — the response shows bare ids for those concepts);
  ``no label in types`` means the read worked and the concept is not there (a
  data gap; logged once per id per worker, re-checked after an hour).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from .config import ES_BACKEND
from .es_helpers import ES_HEADERS

logger = logging.getLogger("gateway.aat_labels")

TYPES_INDEX = "types"
#: How long a "no label in types" answer is trusted before the id is re-checked.
MISSING_RECHECK_SECONDS = 3600
LOOKUP_TIMEOUT = 10
_PAGE = 10000
_WARM_PAGE_TIMEOUT = 60

_cache: dict[int, str] = {}
_missing: dict[int, float] = {}  # aat_id -> monotonic time it was found missing
_warmed = False


def reset_cache() -> None:
    """Forget everything (tests)."""
    global _warmed
    _cache.clear()
    _missing.clear()
    _warmed = False


def cache_size() -> int:
    return len(_cache)


async def _fetch_all(auth) -> dict[int, str]:
    """Read every ``types`` doc's ``(aat_id, term)``, paging with search_after."""
    labels: dict[int, str] = {}
    search_after = None
    async with httpx.AsyncClient(timeout=_WARM_PAGE_TIMEOUT) as client:
        while True:
            body = {
                "size": _PAGE,
                "query": {"match_all": {}},
                "_source": ["aat_id", "term"],
                "sort": [{"aat_id": "asc"}],
            }
            if search_after is not None:
                body["search_after"] = search_after
            resp = await client.post(
                f"{ES_BACKEND}/{TYPES_INDEX}/_search",
                json=body, auth=auth, headers=ES_HEADERS,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
            for h in hits:
                src = h.get("_source") or {}
                aid = src.get("aat_id")
                term = src.get("term") or ""
                if isinstance(aid, int) and term:
                    labels[aid] = term
            if len(hits) < _PAGE:
                break
            search_after = hits[-1].get("sort")
            if not search_after:
                break
    if not labels:
        # An empty read is a broken read, not "no concept has a label".
        raise RuntimeError("types index returned no labelled concepts")
    return labels


async def warm(auth, *, retries: int = 1) -> bool:
    """Bulk-load the whole index into the cache. Never raises; never blocks
    requests (they do not wait on it). Returns True on success."""
    global _warmed
    for attempt in range(1, retries + 2):
        started = time.monotonic()
        try:
            labels = await _fetch_all(auth)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment
            logger.warning("AAT label cache warm-up: lookup FAILED (attempt %d of %d): %s",
                           attempt, retries + 1, exc)
            if attempt <= retries:
                await asyncio.sleep(5)
            continue
        _cache.update(labels)
        _warmed = True
        logger.info("AAT label cache: warmed with %d labels from '%s' in %.1fs (pid %d)",
                    len(labels), TYPES_INDEX, time.monotonic() - started, os.getpid())
        return True
    logger.warning("AAT label cache warm-up gave up; labels will be looked up per request")
    return False


async def _lookup(aat_ids: list[int], auth) -> dict[int, str]:
    """Targeted ``terms`` lookup. Raises on failure (the caller logs it)."""
    body = {
        "size": len(aat_ids),
        "query": {"terms": {"aat_id": aat_ids}},
        "_source": ["aat_id", "term"],
    }
    async with httpx.AsyncClient(timeout=LOOKUP_TIMEOUT) as client:
        resp = await client.post(
            f"{ES_BACKEND}/{TYPES_INDEX}/_search",
            json=body, auth=auth, headers=ES_HEADERS,
        )
        resp.raise_for_status()
        out: dict[int, str] = {}
        for h in resp.json().get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            if isinstance(src.get("aat_id"), int) and src.get("term"):
                out[src["aat_id"]] = src["term"]
        return out


async def resolve_labels(aat_ids: list[int], auth, *, retries: int = 1) -> dict[int, str]:
    """``{aat_id: term}`` for the ids that have a label. Never raises."""
    if not aat_ids:
        return {}
    now = time.monotonic()
    need = [a for a in aat_ids
            if a not in _cache
            and not (a in _missing and now - _missing[a] < MISSING_RECHECK_SECONDS)]
    if need:
        found = None
        for attempt in range(1, retries + 2):
            try:
                found = await _lookup(need, auth)
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("AAT label lookup FAILED for %d id(s) (attempt %d of %d; "
                               "labels unknown, not missing): %s",
                               len(need), attempt, retries + 1, exc)
        if found is not None:
            _cache.update(found)
            newly_missing = [a for a in need if a not in found]
            if newly_missing:
                fresh = [a for a in newly_missing if a not in _missing]
                for a in newly_missing:
                    _missing[a] = now
                if fresh:
                    logger.info("AAT label: no label in '%s' for %d concept(s) (data gap, "
                                "not a lookup failure): %s",
                                TYPES_INDEX, len(fresh), fresh[:20])
    return {a: _cache[a] for a in aat_ids if a in _cache}
