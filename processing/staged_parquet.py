"""Shared helpers for converting staged JSONL snapshots to parquet sidecars.

Several stages (``boundary_merge``, ``h3_merge``) write a canonical
``places.jsonl`` and a parquet sidecar in ``places.parquet``. The parquet
conversion has two recurring schema-stability issues with
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

Use ``write_parquet_from_jsonl(jsonl_path, parquet_path)`` to do the
hull-strip + parquet conversion in one call. Callers are expected to
apply ``normalize_for_parquet`` to docs *before* writing the canonical
JSONL (so the empty-list normalisation is also visible to downstream
JSONL readers, which generally want it too).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def strip_hull_for_parquet(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop ``geometries[].hull`` before parquet conversion (see module docstring)."""
    stripped = dict(doc)
    geometries = stripped.get("geometries")
    if isinstance(geometries, list):
        new_geoms = []
        for geom in geometries:
            if isinstance(geom, dict) and "hull" in geom:
                geom = {k: v for k, v in geom.items() if k != "hull"}
            new_geoms.append(geom)
        stripped["geometries"] = new_geoms
    return stripped


def write_parquet_from_jsonl(jsonl_path: Path, parquet_path: Path) -> None:
    """Convert a canonical JSONL snapshot to a parquet sidecar.

    Streams ``jsonl_path`` through ``strip_hull_for_parquet`` into a
    sibling ``*.parquet_input.jsonl`` temp file (so the canonical JSONL
    keeps hull for downstream consumers), then feeds the temp file to
    pyarrow for parquet conversion. The temp file is removed even if
    parquet writing fails, so callers don't need their own cleanup.

    Caller is expected to have already applied ``normalize_for_parquet``
    to the docs in ``jsonl_path``.
    """
    parquet_input_path = parquet_path.with_suffix(".parquet_input.jsonl")
    try:
        with jsonl_path.open("r", encoding="utf-8") as in_fh, \
             parquet_input_path.open("w", encoding="utf-8") as out_fh:
            for line in in_fh:
                if not line.strip():
                    continue
                doc = json.loads(line)
                stripped = strip_hull_for_parquet(doc)
                out_fh.write(json.dumps(stripped, ensure_ascii=True) + "\n")
        table = paj.read_json(str(parquet_input_path))
        pq.write_table(table, str(parquet_path))
    finally:
        try:
            parquet_input_path.unlink()
        except FileNotFoundError:
            pass
