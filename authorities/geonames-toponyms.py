# authorities/geonames-toponyms.py

"""
Stage GeoNames alternate-names as a Phase 3 update patch.

Reads ``alternateNamesV2.zip`` and emits a per-place patch JSONL at
``staged/gn/update_patch/places.update.jsonl`` consumed by
``processing/update_merge.py``. Each row carries the toponyms to add (as
``toponyms_to_add``), the preferred title (when present), and any
cross-authority relations derived from the ``wkdt`` / ``link`` lines
(``relations_to_add``).

Per Master Plan + Batch 4c Phase 3: this script never contacts
Elasticsearch. The ``update_merge`` stage collapses the patch into the
namespace's ``update_merged/`` snapshot before H3 derivation.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from processing.settings import BATCH_SIZE, DATA_DIR, STAGED_BASE_DIR
from processing.staging_contract import UPDATE_PATCH_FILENAME
from processing.utilities import stream_file


# ----------------------------------------------------------------------------
# Source-line parsing (carried over from the legacy ES path)
# ----------------------------------------------------------------------------


def normalize_lst(name, lang="und"):
    """Ensure toponym is in LST format (name@lang)."""
    if not name:
        return None
    if "@" in name:
        return name
    return f"{name}@{lang}"


def parse_year(year_str):
    """Parse year string, handling empty, positive, and negative years."""
    if not year_str or year_str.strip() == "":
        return None
    try:
        return int(year_str.strip())
    except ValueError:
        return None


def parse_alternatename_line(line):
    """Parse one alternateNamesV2 line.

    Returns one of:
    - ``("toponym", lst, timespans_list, is_preferred, place_id)``
    - ``("relation", place_id, relation_dict)``
    - ``(None,)`` for skipped/non-linguistic entries.
    """
    fields = line.split("\t")

    lang_code = fields[2] if len(fields) > 2 else ""
    geoname_id = fields[1]
    value = fields[3] if len(fields) > 3 else ""
    place_id = f"gn:{geoname_id}"

    # Cross-authority: Wikidata QID
    if lang_code == "wkdt" and value:
        return (
            "relation", place_id,
            {
                "relation_type": "sameAs",
                "related_place_id": f"wd:{value}",
                "label": "Wikidata",
            },
        )

    # External link
    if lang_code == "link" and value:
        return (
            "relation", place_id,
            {
                "relation_type": "describedBy",
                "related_place_id": value,
                "label": "External Link",
            },
        )

    # Skip non-linguistic entries (postal codes, airport codes, etc.).
    if lang_code in {"post", "iata", "icao", "faac", "unlc", "tcid", "abbr"}:
        return (None,)

    if not value:
        return (None,)

    if lang_code:
        lang_code = lang_code.replace("_", "-")
        lst = normalize_lst(value, lang_code)
    else:
        lst = normalize_lst(value, "und")

    year_from = parse_year(fields[8]) if len(fields) > 8 else None
    year_to = parse_year(fields[9]) if len(fields) > 9 else None

    timespans_list = None
    if year_from is not None or year_to is not None:
        ts: dict = {}
        if year_from is not None:
            ts["start"] = {"in": year_from}
        if year_to is not None:
            ts["end"] = {"in": year_to}
        timespans_list = [ts]

    is_preferred = fields[4] == "1" if len(fields) > 4 else False
    return ("toponym", lst, timespans_list, is_preferred, place_id)


# ----------------------------------------------------------------------------
# Patch emission
# ----------------------------------------------------------------------------


def _patch_path() -> Path:
    return Path(STAGED_BASE_DIR) / "gn" / "update_patch" / UPDATE_PATCH_FILENAME


def _flush(buffer: dict, fh) -> int:
    """Write one JSONL row per place_id to ``fh``; return count written."""
    written = 0
    for place_id, data in buffer.items():
        toponyms_to_add = data["toponyms"]
        relations_to_add = data["relations"]
        title = data["title"]
        if not toponyms_to_add and not relations_to_add and not title:
            continue
        row: dict = {"place_id": place_id}
        if title:
            row["title"] = title
        if toponyms_to_add:
            row["toponyms_to_add"] = toponyms_to_add
        if relations_to_add:
            row["relations_to_add"] = relations_to_add
        fh.write(json.dumps(row, ensure_ascii=True) + "\n")
        written += 1
    buffer.clear()
    return written


def stage_alternatenames(file_path: str) -> dict:
    """Stream the alternateNames file, emit one patch row per place.

    Buffer size matches ``BATCH_SIZE`` so memory stays bounded; the same
    ``place_id`` may appear in multiple flush windows when its lines aren't
    contiguous in the source — ``update_merge`` folds those rows on read.
    """
    out_path = _patch_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    buffer = defaultdict(
        lambda: {"toponyms": [], "seen": set(), "relations": [], "title": None}
    )
    rows_written = 0
    processed = 0
    skipped = 0

    print("=" * 80)
    print("GEONAMES TOPONYMS — STAGED PATCH EMISSION")
    print("=" * 80)
    print(f"Source: {file_path}")
    print(f"Output: {out_path}")

    with out_path.open("w", encoding="utf-8") as fh:
        for line in stream_file(file_path):
            if not line or line.startswith("#"):
                continue
            processed += 1
            if processed % 100_000 == 0:
                sys.stdout.write(
                    f"\r  Processed {processed:,} lines | rows {rows_written:,}"
                )
                sys.stdout.flush()

            try:
                result = parse_alternatename_line(line)
            except Exception:
                skipped += 1
                continue

            if result[0] == "toponym":
                _, lst, timespans_list, is_preferred, place_id = result
                if lst is None or lst in buffer[place_id]["seen"]:
                    continue
                buffer[place_id]["seen"].add(lst)
                entry: dict = {"toponym_id": lst}
                if timespans_list:
                    entry["timespans"] = timespans_list
                buffer[place_id]["toponyms"].append(entry)
                if is_preferred and buffer[place_id]["title"] is None:
                    name = lst.split("@")[0] if "@" in lst else lst
                    buffer[place_id]["title"] = name
            elif result[0] == "relation":
                _, place_id, relation = result
                # Cheap dedupe within a flush window; update_merge does the
                # final cross-window dedupe by (relation_type, related_place_id).
                key = (relation["relation_type"], relation["related_place_id"])
                if key in buffer[place_id].setdefault("rel_seen", set()):
                    continue
                buffer[place_id]["rel_seen"].add(key)
                buffer[place_id]["relations"].append(relation)
            else:
                skipped += 1

            if len(buffer) >= BATCH_SIZE:
                rows_written += _flush(buffer, fh)

        rows_written += _flush(buffer, fh)

    print(
        f"\nProcessed {processed:,} lines | skipped {skipped:,} | "
        f"emitted {rows_written:,} patch rows"
    )
    print(f"Patch written: {out_path}")
    return {
        "lines_processed": processed,
        "lines_skipped": skipped,
        "rows_written": rows_written,
        "patch_path": str(out_path),
    }


def main():
    alternatenames_file = os.environ.get(
        "GEONAMES_ALTERNATENAMES_FILE",
        f"{DATA_DIR}/authorities/gn/alternateNamesV2.zip",
    )
    if not os.path.exists(alternatenames_file):
        print(f"ERROR: source not found: {alternatenames_file}", file=sys.stderr)
        sys.exit(1)
    summary = stage_alternatenames(alternatenames_file)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
