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
from pathlib import Path
from typing import Any

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
