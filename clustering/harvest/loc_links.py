"""Batch 12 — LOC relations harvest (LOC's only entry point in the rebuild).

LOC has no place records of its own in the WHG index. Its sole contribution
is a body of MADS/RDF authority records that link a LOC place identifier
(``loc:sh85042412``) to one or more external authorities (``gn:``, ``wd:``,
``viaf:``). For Batch 12 we treat every LOC record as a *transitivity hub*:
all C(N, 2) pairs among its in-scope external targets become a single
``hard_link_assertions`` row with ``source_id = 'loc'``.

Pair semantics:

* ``sameAs`` only when both contributing LOC link types were exact
  (``hasExactExternalAuthority`` / ``identifiesRWO``); otherwise
  ``closeMatch``.
* Targets in unknown / out-of-scope namespaces (``viaf:``, anything not in
  ``KNOWN_ES_NAMESPACES``) are dropped at harvest time with a logged count.
* LOC records yielding fewer than 2 in-scope targets contribute nothing.

Source files: NDJSON (optionally gzipped) under
``${DATA_DIR}/authorities/loc/`` — discovered automatically when no
``--source`` is given. The MADS/RDF parser is implemented locally; the
former ES-coupled ``authorities/loc-relations.py`` has been retired.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Iterator

from clustering.config import KNOWN_ES_NAMESPACES
from clustering.sqlite_overlay import builder, insert_rows
from processing.settings import DATA_DIR


_LOC_SOURCE_CATEGORY = "authority"
_LOC_SOURCE_ID = "loc"


# ---------------------------------------------------------------------------
# MADS/RDF parsing (local implementation; the legacy
# authorities/loc-relations.py path has been retired)
# ---------------------------------------------------------------------------


_EXTERNAL_LINK_FIELDS_EXACT = (
    "madsrdf:hasExactExternalAuthority",
    "madsrdf:identifiesRWO",
)
_EXTERNAL_LINK_FIELDS_CLOSE = (
    "madsrdf:hasCloseExternalAuthority",
)


def _coerce_uri(value: Any) -> str | None:
    if isinstance(value, dict) and "@id" in value:
        uri = value["@id"]
        return uri if isinstance(uri, str) else None
    if isinstance(value, str):
        return value
    return None


def _resolve_external_link(uri: str) -> tuple[str, str] | None:
    """Return ``(namespace, place_id)`` for a known external authority URI."""
    if "geonames.org" in uri:
        ident = uri.rstrip("/").split("/")[-1]
        if ident.isdigit():
            return ("gn", f"gn:{ident}")
    if "wikidata.org" in uri:
        ident = uri.rstrip("/").split("/")[-1]
        if ident.startswith("Q"):
            return ("wd", f"wd:{ident}")
    return None


def parse_loc_record(record: Any) -> Iterator[dict[str, Any]]:
    """Yield ``{loc_id, label, external_links}`` dicts from one MADS/RDF record.

    ``external_links`` items are shaped ``{place_id, namespace, exact}``
    where ``exact`` is ``True`` for the Exact* / RWO link types.
    """
    if isinstance(record, dict) and "@graph" in record:
        items = record["@graph"]
    elif isinstance(record, list):
        items = record
    elif isinstance(record, dict):
        items = [record]
    else:
        return

    for item in items:
        if not isinstance(item, dict):
            continue
        item_types = item.get("@type", [])
        if isinstance(item_types, str):
            item_types = [item_types]
        if not any("Geographic" in t for t in item_types):
            continue
        loc_id = item.get("@id", "")
        if not isinstance(loc_id, str) or not loc_id:
            continue
        if loc_id.startswith("http://id.loc.gov/"):
            loc_id = loc_id.replace("http://id.loc.gov/", "")
        if "/" in loc_id:
            loc_id = loc_id.rstrip("/").split("/")[-1]

        label = None
        label_obj = item.get("madsrdf:authoritativeLabel")
        if isinstance(label_obj, dict):
            label = label_obj.get("@value")
        elif isinstance(label_obj, str):
            label = label_obj
        if not label:
            label_obj = item.get("rdfs:label")
            if isinstance(label_obj, dict):
                label = label_obj.get("@value")
            elif isinstance(label_obj, str):
                label = label_obj

        external_links: list[dict[str, Any]] = []
        for field, exact in (
            *((f, True) for f in _EXTERNAL_LINK_FIELDS_EXACT),
            *((f, False) for f in _EXTERNAL_LINK_FIELDS_CLOSE),
        ):
            value = item.get(field)
            if value is None:
                continue
            if not isinstance(value, list):
                value = [value]
            for entry in value:
                uri = _coerce_uri(entry)
                if not uri:
                    continue
                resolved = _resolve_external_link(uri)
                if resolved is None:
                    continue
                ns, place_id = resolved
                if ns not in KNOWN_ES_NAMESPACES:
                    continue
                external_links.append({
                    "place_id": place_id,
                    "namespace": ns,
                    "exact": exact,
                })

        if external_links:
            yield {
                "loc_id": loc_id,
                "label": label,
                "external_links": external_links,
            }


# ---------------------------------------------------------------------------
# Pair generation
# ---------------------------------------------------------------------------


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _pair_relation(a_exact: bool, b_exact: bool) -> str:
    return "sameAs" if (a_exact and b_exact) else "closeMatch"


def _iter_loc_files(source: Path | None) -> Iterator[Path]:
    """Yield every LOC NDJSON / NDJSON.gz file under the source directory."""
    if source is None:
        source = Path(DATA_DIR) / "authorities" / "loc"
    if source.is_file():
        yield source
        return
    if not source.is_dir():
        return
    for ext in ("*.ndjson", "*.ndjson.gz", "*.json", "*.json.gz", "*.jsonld",
                "*.jsonld.gz"):
        yield from sorted(source.glob(ext))


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def iter_hard_link_rows(source: Path | None = None) -> Iterator[dict[str, Any]]:
    """Yield validated hard-link rows derived from LOC NDJSON sources."""
    for path in _iter_loc_files(source):
        with _open_text(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for parsed in parse_loc_record(record):
                    links = parsed["external_links"]
                    if len(links) < 2:
                        continue
                    label = parsed.get("label") or ""
                    loc_id = parsed["loc_id"]
                    justification = (
                        f"via loc:{loc_id} ({label})" if label else f"via loc:{loc_id}"
                    )
                    for a, b in combinations(links, 2):
                        if a["namespace"] == b["namespace"]:
                            continue  # Same-namespace pairs add no information.
                        place_a, place_b = _canonical_pair(a["place_id"], b["place_id"])
                        yield {
                            "place_a": place_a,
                            "place_b": place_b,
                            "relation_type": _pair_relation(a["exact"], b["exact"]),
                            "source_category": _LOC_SOURCE_CATEGORY,
                            "source_id": _LOC_SOURCE_ID,
                            "asserted_at": None,
                            "justification": justification,
                        }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def harvest(
    *,
    db_path: Path,
    source: Path | None = None,
    batch_size: int = 5_000,
) -> dict[str, Any]:
    with builder(db_path) as conn:
        stats = insert_rows(conn, iter_hard_link_rows(source), batch_size=batch_size)
    stats["db_path"] = str(db_path)
    stats["source"] = str(source) if source else str(Path(DATA_DIR) / "authorities" / "loc")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Harvest LOC-mediated hard-link assertions into the SQLite overlay"
    )
    parser.add_argument("--db-path", required=True, help="SQLite output path")
    parser.add_argument("--source",
                        help="LOC NDJSON file or directory (default: "
                             "$DATA_DIR/authorities/loc)")
    parser.add_argument("--batch-size", type=int, default=5_000)
    args = parser.parse_args()

    source = Path(args.source) if args.source else None
    stats = harvest(db_path=Path(args.db_path), source=source, batch_size=args.batch_size)
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
