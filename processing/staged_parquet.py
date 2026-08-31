"""Shared helpers for converting staged JSONL snapshots to parquet sidecars.

Several stages (``boundary_merge``, ``h3_merge``) write a canonical
``places.jsonl`` and a parquet sidecar in ``places.parquet``. The parquet
conversion has three recurring schema-stability issues with
``pyarrow.json.read_json``:

1. **Empty nested-list fields** (``geometries=[]``, ``toponyms=[]``, …)
   cause row-by-row inference to alternate between ``list<null>`` and
   ``list<struct>``. ``normalize_for_parquet`` swaps empty lists for
   ``None`` so the inferred schema stays stable.

2. **Variable-depth ``geometries[].hull.coordinates``** (Polygon
   ``[[lon,lat], …]`` vs MultiPolygon ``[[[lon,lat], …], …]``) is
   legitimate across our authority sources but pyarrow rejects it during
   schema inference. ``strip_hull_for_parquet`` drops ``hull`` from each
   geometry before parquet conversion. Hull is consumed by
   ``ccode_enrichment`` and ``generate_tiles``, both of which read the
   JSONL (or the staged geom store) — so the parquet sidecar staying
   hull-less is lossless.

3. **Explicit JSON nulls inside struct-typed fields** — e.g. a timespan
   serialised as ``{"start": {"in": 1500}, "end": null}`` (because the
   source had only ``start_date``). ``pyarrow.read_json`` can write
   parquet with nullable struct values but cannot *read* them from JSON,
   producing ``ArrowNotImplementedError: JSON conversion to struct<...>
   is not supported``. ``drop_nulls_for_parquet`` recursively removes
   ``None`` values from the doc before parquet conversion. Because the
   absent-vs-null distinction has no semantic meaning at the storage
   level (parquet writes both as null), this is lossless for the
   parquet sidecar.

Use ``write_parquet_from_jsonl(jsonl_path, parquet_path)`` to do all
three preprocessing steps + parquet conversion in one call. Callers are
expected to apply ``normalize_for_parquet`` to docs *before* writing the
canonical JSONL (so the empty-list normalisation is also visible to
downstream JSONL readers, which generally want it too).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pyarrow as pa
import pyarrow.json as paj
import pyarrow.parquet as pq


def normalize_for_parquet(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert empty nested-list fields to None for stable parquet schema inference.

    Applied to the canonical JSONL — downstream JSONL readers also benefit
    from empty-list → None normalisation.
    """
    normalized = dict(doc)
    for key in ("geometries", "toponyms", "types", "relations"):
        value = normalized.get(key)
        if isinstance(value, list) and len(value) == 0:
            normalized[key] = None
    return normalized


def strip_hull(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``doc`` with ``geometries[].hull`` removed.

    ``hull`` is a derived convex hull used only as an ingestion intermediate
    (h3-cover computation, ccode containment); it is never read at query time
    (the gateway filters on ``repr_point`` / ``h3_cover``). It is dropped from
    parquet (its variable-depth coordinates break parquet schema inference) and,
    for index-path consistency, from every doc indexed to ES — so ES never
    carries ``hull`` regardless of source format (parquet or JSONL).
    """
    geometries = doc.get("geometries")
    if not isinstance(geometries, list) or not any(
        isinstance(g, dict) and "hull" in g for g in geometries
    ):
        return doc  # nothing to strip — return unchanged (no copy on the hot path)
    stripped = dict(doc)
    stripped["geometries"] = [
        {k: v for k, v in g.items() if k != "hull"}
        if isinstance(g, dict) and "hull" in g else g
        for g in geometries
    ]
    return stripped


def strip_hull_for_parquet(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop ``geometries[].hull`` before parquet conversion (see module docstring)."""
    return strip_hull(doc)


def drop_nulls_for_parquet(obj: Any) -> Any:
    """Recursively drop ``None`` values from dicts (see module docstring).

    JSON nulls inside struct-typed columns trip ``pyarrow.read_json``
    when adjacent rows have a struct value at the same path. Round-tripping
    a doc through parquet and back to JSONL — as ``h3_merge`` does after
    reading from ``boundary_merged.parquet`` — introduces these explicit
    nulls (parquet preserves nullable struct slots; ``json.dumps`` writes
    them as JSON null). Stripping them out matches what the original
    JSONL emitter would have produced (``ts['end'] = ...`` only when an
    end_year was parsed) and keeps the parquet input pyarrow-clean.
    """
    if isinstance(obj, dict):
        return {k: drop_nulls_for_parquet(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [drop_nulls_for_parquet(v) for v in obj]
    return obj


def write_parquet_from_jsonl(jsonl_path: Path, parquet_path: Path) -> bool:
    """Convert a canonical JSONL snapshot to a parquet sidecar (best-effort).

    Streams ``jsonl_path`` through ``strip_hull_for_parquet`` and
    ``drop_nulls_for_parquet`` into a sibling ``*.parquet_input.jsonl``
    temp file (so the canonical JSONL keeps hull and explicit nulls for
    downstream consumers), then feeds the temp file to pyarrow for
    parquet conversion. The temp file is removed even if parquet writing
    fails, so callers don't need their own cleanup.

    Caller is expected to have already applied ``normalize_for_parquet``
    to the docs in ``jsonl_path``.

    Returns ``True`` if the parquet sidecar was written, ``False`` if
    pyarrow's schema inference failed mid-conversion. Common failure
    modes (handled gracefully):

    * Mixed types across rows in nested fields (e.g. WHG LPF timespans
      where ``earliest`` is sometimes a string ISO date, sometimes a
      bare number — the staging contract doesn't enforce a single type
      and different LPF datasets normalise differently).
    * Other ``ArrowInvalid`` / ``ArrowNotImplementedError`` cases that
      slip through ``strip_hull_for_parquet`` / ``drop_nulls_for_parquet``.

    The canonical JSONL is left intact in either case. Downstream readers
    (``ccode_enrichment``, ``gazetteer_temporal_extent``, ``boundary_merge``,
    ``h3_merge``) all use a "parquet if exists, else jsonl" priority,
    so a missing parquet falls through to JSONL automatically — slower
    on big namespaces but functionally complete.
    """
    parquet_input_path = parquet_path.with_suffix(".parquet_input.jsonl")
    parquet_written = False
    try:
        with jsonl_path.open("r", encoding="utf-8") as in_fh, \
             parquet_input_path.open("w", encoding="utf-8") as out_fh:
            for line in in_fh:
                if not line.strip():
                    continue
                doc = json.loads(line)
                doc = strip_hull_for_parquet(doc)
                doc = drop_nulls_for_parquet(doc)
                out_fh.write(json.dumps(doc, ensure_ascii=True) + "\n")
        try:
            table = paj.read_json(str(parquet_input_path))
            pq.write_table(table, str(parquet_path))
            parquet_written = True
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
            print(
                f"WARN: parquet sidecar at {parquet_path} skipped — "
                f"pyarrow schema inference failed ({exc.__class__.__name__}: {exc})",
                flush=True,
            )
            print(
                f"      JSONL is intact at {jsonl_path}; downstream stages "
                "fall back to JSONL automatically.",
                flush=True,
            )
            try:
                parquet_path.unlink()
            except FileNotFoundError:
                pass
    finally:
        try:
            parquet_input_path.unlink()
        except FileNotFoundError:
            pass
    return parquet_written


# ---------------------------------------------------------------------------
# Atomic publication of a staged snapshot pair (§2.8)
# ---------------------------------------------------------------------------
#
# Every consumer resolves a namespace's snapshot by walking
# ``_STAGED_SOURCE_PRIORITY`` — (final, h3_merged, boundary_merged,
# update_merged, extract) — and testing ``.exists()`` ONLY, with no size or
# completeness check, preferring ``places.parquet`` over ``places.jsonl``
# within a stage. Writing the JSONL in place therefore publishes a ZERO-BYTE
# file on the first instant, which immediately outranks the complete upstream
# stage it supersedes: readers get no rows at all, silently, for as long as
# the merge runs — hours, on ``gn`` and ``wd``. Worse than truncation, because
# a partial JSONL is valid JSONL as far as it has reached.
#
# So a stage writes to temp files, which no resolver can see (different
# names), and renames them into place only once both are complete.

#: Rename order. The pair cannot be made atomic — two files, two syscalls —
#: so the order chooses WHICH file is briefly stale, and the resolvers prefer
#: ``places.parquet``. Parquet is renamed FIRST so that the preferred, and
#: therefore authoritative, file is the correct one in the state that
#: persists: if the process dies BETWEEN the two renames, parquet-first
#: strands a new parquet beside an old JSONL and the stage still resolves to
#: the new data, whereas jsonl-first would strand a stale parquet that every
#: resolver prefers — serving old data indefinitely and silently, which is a
#: quieter version of the very defect this exists to fix.
#:
#: ⚠️ Residual, not fixable by any rename order: ``write_parquet_from_jsonl``
#: strips ``hull``, so the JSONL is canonical for hull-consumers
#: (``ccode_enrichment``, ``generate_tiles``). A crash between the renames
#: leaves the two files disagreeing whichever order is used. Making the PAIR
#: atomic needs a directory-symlink swap, which changes an on-disk shape five
#: resolver copies assume — deliberately out of scope here.
_PARQUET_RENAMED_FIRST = True


def _unlink_quietly(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class _CountingJsonlWriter:
    """Thin wrapper over the temp handle that counts published rows."""

    __slots__ = ("_handle", "docs_written", "parquet_written")

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self.docs_written = 0
        #: Set on clean exit. False means pyarrow schema inference failed and
        #: only the JSONL was published — callers that warn about a missing
        #: sidecar read this after the ``with`` block.
        self.parquet_written = False

    def write(self, line: str) -> None:
        self._handle.write(line)
        self.docs_written += 1


@contextmanager
def atomic_staged_snapshot(
    jsonl_path: Path,
    parquet_path: Path,
    *,
    label: str,
) -> Iterator[_CountingJsonlWriter]:
    """Publish a staged ``places.jsonl`` + ``places.parquet`` pair atomically.

    Yields a writer whose ``.write()`` takes one already-serialised JSONL
    line. On clean exit the parquet sidecar is derived from the temp JSONL
    and both are renamed into place (parquet first — see
    ``_PARQUET_RENAMED_FIRST``). If the body raises, **nothing is
    published**: the temps are removed and any snapshot already in place is
    left byte-for-byte untouched. The exception always propagates — a merge
    that fails must fail loudly, never leave a silent partial.

    If parquet conversion fails (pyarrow schema inference — see
    ``write_parquet_from_jsonl``), any pre-existing sidecar is removed before
    the JSONL is published, so resolvers fall through to the fresh JSONL
    rather than preferring a stale parquet.
    """
    jsonl_tmp = jsonl_path.with_name(jsonl_path.name + ".tmp")
    parquet_tmp = parquet_path.with_name(parquet_path.name + ".tmp")
    _unlink_quietly(jsonl_tmp, parquet_tmp)

    try:
        with jsonl_tmp.open("w", encoding="utf-8") as handle:
            writer = _CountingJsonlWriter(handle)
            yield writer

        print(f"  {label}: merged {writer.docs_written:,} docs; "
              "converting to Parquet ...", flush=True)
        parquet_written = write_parquet_from_jsonl(jsonl_tmp, parquet_tmp)
        writer.parquet_written = parquet_written

        if parquet_written:
            print(f"  {label}: Parquet written", flush=True)
            if _PARQUET_RENAMED_FIRST:
                os.replace(parquet_tmp, parquet_path)
        else:
            # No sidecar this run: drop any previous one so the chain cannot
            # resolve to a stale parquet beside the JSONL we publish next.
            _unlink_quietly(parquet_path)

        os.replace(jsonl_tmp, jsonl_path)

        if parquet_written and not _PARQUET_RENAMED_FIRST:
            os.replace(parquet_tmp, parquet_path)
    except BaseException:
        # Cleanup must never replace the real failure. _unlink_quietly swallows
        # only FileNotFoundError — deliberately, because the failed-conversion
        # branch above RELIES on a stale sidecar actually being removed — but a
        # PermissionError or an NFS EIO here would propagate in place of the
        # original exception and lose the cause. This cluster has produced both.
        try:
            _unlink_quietly(jsonl_tmp, parquet_tmp)
        except Exception:
            pass
        raise
