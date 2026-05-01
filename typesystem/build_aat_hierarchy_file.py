# typesystem/build_aat_hierarchy_file.py

"""Export AAT id → ``{path, label_en}`` from the production ``types`` index.

Produces ``typesystem/data/aat_hierarchy.json`` for ``processing.aat_enrich``
to consume on Slurm compute nodes (which can't reach the production gateway).
The file is a small, deterministic sidecar to the per-vocab data files —
it carries the hierarchy info those files don't (each vocab file only stores
the leaf ``aat_id`` from its native concept's mapping; this file maps that
``aat_id`` to its full materialised path).

Output shape::

    {
      "300008389": {"path": "/300000000/300006300/300006520/300008389",
                    "label_en": "city"},
      …
    }

Run on the Pitt VM (``ssh pitt``) where ``http://localhost:9201`` is reachable
without SSH tunnelling. Bundled into the prep-phase command
``bash scripts/types.sh --build-vocabs``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from elasticsearch import helpers

from typesystem.es_client import create_client

DEFAULT_INDEX = "types"
DEFAULT_ES_HOST = "http://localhost:9201"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent / "data" / "aat_hierarchy.json"
)
# Pull just the fields we need; everything else is extra bytes over the wire.
_SCROLL_FIELDS = ["aat_id", "path", "term", "labels"]


def _english_label(src: dict[str, Any]) -> str | None:
    """Pick the most natural English label for a types doc.

    Preference order: ``labels.en`` (preferred-language harvest) →
    ``term`` (fallback singular). ``term`` may be the bare ``aat:NNN`` for
    concepts without a harvested label, which we treat as missing.
    """
    labels = src.get("labels") or {}
    if isinstance(labels, dict):
        en = labels.get("en")
        if en:
            return en if isinstance(en, str) else (en[0] if isinstance(en, list) and en else None)
    term = src.get("term")
    if isinstance(term, str) and term and not term.startswith("aat:"):
        return term
    return None


def build(
    *,
    es_host: str = DEFAULT_ES_HOST,
    index_name: str = DEFAULT_INDEX,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    # Use the shared client helper so the elastic password file at
    # /ix1/ishi/es/config/elastic.password is picked up automatically when
    # the URL omits inline credentials (matches the other build_* scripts).
    es = create_client(es_host, request_timeout=120)
    if not es.indices.exists(index=index_name):
        raise RuntimeError(f"types index '{index_name}' missing on {es_host}")

    out: dict[str, dict[str, Any]] = {}
    no_path = 0
    no_label = 0
    for hit in helpers.scan(
        es,
        index=index_name,
        query={"query": {"match_all": {}}, "_source": _SCROLL_FIELDS},
        scroll="5m",
        size=2000,
    ):
        src = hit.get("_source") or {}
        aat_id = src.get("aat_id")
        path = src.get("path")
        if aat_id is None or not path:
            no_path += 1
            continue
        entry: dict[str, Any] = {"path": path}
        label = _english_label(src)
        if label:
            entry["label_en"] = label
        else:
            no_label += 1
        out[str(int(aat_id))] = entry

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")

    return {
        "es_host": es_host,
        "index": index_name,
        "concepts_written": len(out),
        "concepts_skipped_no_path": no_path,
        "concepts_no_english_label": no_label,
        "output_path": str(output_path),
        "output_size_bytes": output_path.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export AAT id → {path, label_en} from production ES "
                    "types index to typesystem/data/aat_hierarchy.json"
    )
    parser.add_argument("--es-host", default=DEFAULT_ES_HOST,
                        help=f"ES URL (default: {DEFAULT_ES_HOST})")
    parser.add_argument("--index", default=DEFAULT_INDEX,
                        help=f"Index name (default: {DEFAULT_INDEX})")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"Output JSON path (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    summary = build(
        es_host=args.es_host,
        index_name=args.index,
        output_path=Path(args.output),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
